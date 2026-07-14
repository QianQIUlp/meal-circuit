from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import ai, professional
from .db import connect, init_db, row_dict
from .domain import new_id, utc_now
from .personalization import active_personalization, require_generation
from .validation import ValidationError


AGENT_CONTEXT_VERSION = 2
CASE_FORMULATION_VERSION = 1
DAILY_PLAN_VERSION = 3
PLAN_REVIEW_VERSION = 1
CLAIM_VERSION = 1
LOW_RISK_EFFECT_KEYS = {"ranking", "portion", "flavor", "complexity", "communication", "alternatives"}
CLAIM_TYPES = {
    "confirmed_fact", "stable_preference", "soft_need_hypothesis", "friction_hypothesis",
    "body_response_hypothesis", "causal_hypothesis", "interaction_preference", "temporary_state",
}
WORKSPACE_STATUSES = {
    "collecting", "needs_clarification", "formulating", "planning", "reviewing",
    "ready_draft", "stale", "accepted", "active", "completed", "failed", "interrupted",
}
_TIMERS: dict[str, threading.Timer] = {}
_TIMER_LOCK = threading.Lock()


def _now() -> str:
    return utc_now()


def _validate_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("日期必须是 YYYY-MM-DD") from exc
    return value


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _provider_metadata(provider: ai.AIProvider) -> tuple[str, str]:
    config = getattr(provider, "config", None)
    return (
        str(getattr(config, "provider", provider.__class__.__name__)),
        str(getattr(config, "model", "unspecified")),
    )


def _claim_payload(row: dict) -> dict:
    return {
        key: row.get(key)
        for key in (
            "claim_type", "statement", "scope_json", "status", "confidence", "risk_level",
            "effect_json", "support_count", "counter_count", "first_observed_at",
            "last_observed_at", "valid_until", "source", "version", "rollback_parent_id",
            "last_used_at",
        )
    }


def _append_claim_version(conn, claim: dict, reason: str, timestamp: str) -> None:
    conn.execute(
        """INSERT INTO user_model_claim_versions(id,claim_id,version,payload_json,change_reason,created_at)
           VALUES(?,?,?,?,?,?)""",
        (
            new_id("claim_version"), claim["id"], claim["version"],
            json.dumps(_claim_payload(claim), ensure_ascii=False, sort_keys=True), reason, timestamp,
        ),
    )


def _refresh_user_model_projection(conn, timestamp: str) -> None:
    claims = []
    for row in conn.execute(
        """SELECT id,claim_type,statement,scope_json,status,confidence,effect_json,
                  support_count,counter_count,valid_until,version,updated_at
           FROM user_model_claims
           WHERE status IN ('active','pending_confirmation','paused','refuted')
           ORDER BY updated_at DESC,id"""
    ).fetchall():
        item = row_dict(row)
        claims.append({
            "id": item["id"], "type": item["claim_type"], "statement": item["statement"],
            "scope": item["scope_json"], "status": item["status"],
            "confidence": item["confidence"], "effect": item["effect_json"],
            "support_count": item["support_count"], "counter_count": item["counter_count"],
            "valid_until": item["valid_until"], "version": item["version"], "updated_at": item["updated_at"],
        })
    content = json.dumps(
        {"schema_version": CLAIM_VERSION, "updated_at": timestamp, "claims": claims},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    conn.execute(
        """INSERT INTO config_documents(kind,content,content_sha256,revision_id,updated_at)
           VALUES('agent_user_model',?,?,NULL,?)
           ON CONFLICT(kind) DO UPDATE SET content=excluded.content,
               content_sha256=excluded.content_sha256,updated_at=excluded.updated_at""",
        (content, hashlib.sha256(content.encode("utf-8")).hexdigest(), timestamp),
    )
    from .domain_store import capture_entity, preference_entity_id

    capture_entity(conn, "preferences", preference_entity_id("agent_user_model"), created_at=timestamp)


def _hydrate_user_model_projection() -> None:
    timestamp = _now()
    with connect() as conn:
        document = conn.execute(
            "SELECT content FROM config_documents WHERE kind='agent_user_model'"
        ).fetchone()
        if not document:
            return
        try:
            projection = json.loads(str(document["content"]))
        except json.JSONDecodeError:
            return
        for item in projection.get("claims") or []:
            if not isinstance(item, dict) or item.get("type") not in CLAIM_TYPES:
                continue
            existing = conn.execute("SELECT source,version FROM user_model_claims WHERE id=?", (item.get("id"),)).fetchone()
            if existing and (existing["source"] != "sync_projection" or existing["version"] >= int(item.get("version") or 1)):
                continue
            values = (
                item["id"], item["type"], str(item.get("statement") or "")[:600],
                json.dumps(item.get("scope") or {}, ensure_ascii=False), item.get("status") or "pending_confirmation",
                min(1.0, max(0.0, float(item.get("confidence") or 0))),
                _risk_level(item["type"], str(item.get("statement") or ""), "low"),
                json.dumps(_safe_effect(item.get("effect") or {}), ensure_ascii=False),
                int(item.get("support_count") or 0), int(item.get("counter_count") or 0),
                item.get("updated_at") or timestamp, item.get("updated_at") or timestamp,
                item.get("valid_until"), int(item.get("version") or 1), timestamp, timestamp,
            )
            if existing:
                conn.execute(
                    """UPDATE user_model_claims SET claim_type=?,statement=?,scope_json=?,status=?,confidence=?,
                           risk_level=?,effect_json=?,support_count=?,counter_count=?,last_observed_at=?,valid_until=?,
                           version=?,updated_at=? WHERE id=? AND source='sync_projection'""",
                    (
                        values[1], values[2], values[3], values[4], values[5], values[6], values[7],
                        values[8], values[9], values[11], values[12], values[13], timestamp, values[0],
                    ),
                )
            elif values[2]:
                conn.execute(
                    """INSERT INTO user_model_claims(
                           id,claim_type,statement,scope_json,status,confidence,risk_level,effect_json,
                           support_count,counter_count,first_observed_at,last_observed_at,valid_until,source,
                           version,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'sync_projection',?,?,?)""",
                    values,
                )


def _migrate_legacy_rules_once() -> None:
    with connect() as conn:
        marker = conn.execute(
            "SELECT value FROM app_metadata WHERE key='agent_user_model_legacy_migration'"
        ).fetchone()
        if marker:
            return
        rules = [row_dict(row) for row in conn.execute(
            "SELECT * FROM adaptive_rules WHERE status='active' ORDER BY created_at,id"
        ).fetchall()]
    type_map = {
        "preference": "stable_preference", "friction": "friction_hypothesis",
        "association": "causal_hypothesis", "strategy": "soft_need_hypothesis",
    }
    for rule in rules:
        upsert_claim(
            claim_type=type_map.get(rule.get("kind"), "confirmed_fact"),
            statement=rule["statement"], scope=rule.get("scope_json") or {},
            effect=rule.get("effect_json") or {}, evidence_type="legacy_rule",
            evidence_id=rule["id"], excerpt=f"旧版已确认规则 {rule['id']}", explicit=True,
            proposed_risk="low", source="legacy_confirmed_rule",
        )
    with connect() as conn:
        conn.execute(
            """INSERT INTO app_metadata(key,value) VALUES('agent_user_model_legacy_migration','1')
               ON CONFLICT(key) DO UPDATE SET value='1'"""
        )


def list_claims(*, include_inactive: bool = True) -> list[dict]:
    init_db()
    _migrate_legacy_rules_once()
    _hydrate_user_model_projection()
    sql = "SELECT * FROM user_model_claims"
    if not include_inactive:
        sql += " WHERE status IN ('active','pending_confirmation')"
    sql += " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'pending_confirmation' THEN 1 ELSE 2 END,updated_at DESC"
    with connect() as conn:
        rows = [row_dict(row) for row in conn.execute(sql).fetchall()]
        for item in rows:
            item["evidence"] = [row_dict(row) for row in conn.execute(
                "SELECT * FROM user_model_evidence WHERE claim_id=? ORDER BY observed_at DESC,created_at DESC",
                (item["id"],),
            ).fetchall()]
            item["effective_confidence"] = _effective_confidence(item)
    return rows


def _effective_confidence(claim: dict) -> float:
    confidence = float(claim.get("confidence") or 0)
    try:
        observed = datetime.fromisoformat(claim["last_observed_at"])
    except (KeyError, TypeError, ValueError):
        return confidence
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age_days = max(0, (datetime.now(timezone.utc) - observed).days)
    if claim.get("claim_type") == "friction_hypothesis" and age_days > 30:
        confidence *= max(0.25, 1 - ((age_days - 30) / 90))
    if claim.get("claim_type") == "stable_preference" and age_days > 180:
        confidence *= 0.75
    return round(confidence, 3)


def _risk_level(claim_type: str, statement: str, proposed: str) -> str:
    high_tokens = (
        "过敏", "禁忌", "疾病", "药", "孕", "哺乳", "未成年", "热量", "卡路里",
        "蛋白目标", "减重目标", "治疗", "医生", "永远不吃", "完全排除",
    )
    if claim_type == "confirmed_fact" or proposed == "high" or any(token in statement for token in high_tokens):
        return "high"
    return "low"


def _safe_effect(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in LOW_RISK_EFFECT_KEYS if key in value}


def _current_claim_binding() -> dict:
    current = active_personalization()
    return {
        "profile_version_id": (current.get("profile") or {}).get("id"),
        "goal_version_ids": sorted(item["id"] for item in current.get("goals") or []),
        "strategy_version_id": (current.get("strategy") or {}).get("id"),
        "safety_mode": (current.get("safety") or {}).get("mode") or "setup_required",
        "policy_version": (current.get("safety") or {}).get("policy_version") or "target-policy-v2",
    }


def _scoped_claim(value: dict | None) -> dict:
    scope = deepcopy(value) if isinstance(value, dict) else {}
    scope["binding"] = _current_claim_binding()
    return scope


def _claim_scope_is_current(scope: object, binding: dict) -> bool:
    if not isinstance(scope, dict) or not isinstance(scope.get("binding"), dict):
        return False
    return scope["binding"] == binding


def upsert_claim(
    *,
    claim_type: str,
    statement: str,
    scope: dict | None,
    effect: dict | None,
    evidence_type: str,
    evidence_id: str,
    excerpt: str = "",
    explicit: bool = False,
    stance: str = "support",
    proposed_risk: str = "low",
    valid_until: str | None = None,
    source: str = "agent_inference",
) -> dict:
    if claim_type not in CLAIM_TYPES:
        raise ValidationError("用户模型 claim 类型无效")
    clean_statement = str(statement or "").strip()
    if not clean_statement or len(clean_statement) > 600:
        raise ValidationError("用户模型理解必须是 1–600 字")
    scope = _scoped_claim(scope)
    risk = _risk_level(claim_type, clean_statement, proposed_risk)
    effect = _safe_effect(effect) if risk == "low" else {}
    stable_key = _hash({"type": claim_type, "statement": clean_statement, "scope": scope})[:20]
    claim_id = f"claim_{stable_key}"
    timestamp = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM user_model_claims WHERE id=?", (claim_id,)).fetchone()
        if existing is None:
            support = 1 if stance == "support" else 0
            counter = 1 if stance == "counterexample" else 0
            status = "active" if risk == "low" and explicit and stance == "support" else "pending_confirmation"
            confidence = 0.7 if status == "active" else (0.45 if support else 0.2)
            conn.execute(
                """INSERT INTO user_model_claims(
                       id,claim_type,statement,scope_json,status,confidence,risk_level,effect_json,
                       support_count,counter_count,first_observed_at,last_observed_at,valid_until,
                       source,version,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                (
                    claim_id, claim_type, clean_statement, json.dumps(scope, ensure_ascii=False), status,
                    confidence, risk, json.dumps(effect, ensure_ascii=False), support, counter,
                    timestamp, timestamp, valid_until, source, timestamp, timestamp,
                ),
            )
        conn.execute(
            """INSERT OR IGNORE INTO user_model_evidence(
                   id,claim_id,evidence_type,evidence_id,stance,explicit,excerpt,observed_at,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                new_id("claim_evidence"), claim_id, evidence_type, evidence_id, stance,
                1 if explicit else 0, str(excerpt or "")[:1000], timestamp, timestamp,
            ),
        )
        support_count = conn.execute(
            "SELECT COUNT(DISTINCT evidence_type || ':' || evidence_id) FROM user_model_evidence WHERE claim_id=? AND stance='support'",
            (claim_id,),
        ).fetchone()[0]
        actionable_support_count = conn.execute(
            """SELECT COUNT(DISTINCT evidence_type || ':' || evidence_id)
               FROM user_model_evidence
               WHERE claim_id=? AND stance='support' AND evidence_type!='agent_hypothesis'""",
            (claim_id,),
        ).fetchone()[0]
        counter_count = conn.execute(
            "SELECT COUNT(DISTINCT evidence_type || ':' || evidence_id) FROM user_model_evidence WHERE claim_id=? AND stance='counterexample'",
            (claim_id,),
        ).fetchone()[0]
        explicit_support = conn.execute(
            "SELECT 1 FROM user_model_evidence WHERE claim_id=? AND stance='support' AND explicit=1 LIMIT 1",
            (claim_id,),
        ).fetchone() is not None
        row = row_dict(conn.execute("SELECT * FROM user_model_claims WHERE id=?", (claim_id,)).fetchone())
        status = row["status"]
        if risk == "low" and status not in {"paused", "refuted", "forgotten", "expired"}:
            status = "active" if explicit_support or actionable_support_count >= 2 else "pending_confirmation"
        if counter_count and counter_count >= support_count:
            status = "refuted" if status == "active" else status
        confidence = min(0.92, max(0.1, 0.35 + support_count * 0.2 - counter_count * 0.25))
        version = row["version"] + (1 if existing is not None else 0)
        conn.execute(
            """UPDATE user_model_claims SET status=?,confidence=?,support_count=?,counter_count=?,
                   last_observed_at=?,valid_until=COALESCE(?,valid_until),version=?,updated_at=? WHERE id=?""",
            (status, confidence, support_count, counter_count, timestamp, valid_until, version, timestamp, claim_id),
        )
        updated = row_dict(conn.execute("SELECT * FROM user_model_claims WHERE id=?", (claim_id,)).fetchone())
        _append_claim_version(conn, updated, "evidence_added", timestamp)
        _refresh_user_model_projection(conn, timestamp)
    return updated


def update_claim(claim_id: str, action: str, *, correction: str = "") -> dict:
    actions = {"confirm", "correct", "today", "stable", "pause", "forget", "resume"}
    if action not in actions:
        raise ValidationError("用户模型操作无效")
    timestamp = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = row_dict(conn.execute("SELECT * FROM user_model_claims WHERE id=?", (claim_id,)).fetchone())
        if not row:
            raise KeyError(claim_id)
        if row["risk_level"] == "high" and action in {"confirm", "today", "stable", "resume"}:
            raise ValidationError("高影响信息必须在目标与安全档案中确认，学习中心不能直接让它生效")
        statement = row["statement"]
        claim_type = row["claim_type"]
        status = row["status"]
        valid_until = row.get("valid_until")
        confidence = row["confidence"]
        if action == "confirm":
            status, confidence = "active", max(0.85, confidence)
        elif action == "pause":
            status = "paused"
        elif action == "forget":
            status = "forgotten"
        elif action == "resume":
            status = "active" if row["risk_level"] == "low" else "pending_confirmation"
        elif action == "today":
            claim_type, status = "temporary_state", "active"
            valid_until = date.today().isoformat()
        elif action == "stable":
            if row["risk_level"] == "high":
                raise ValidationError("高影响信息必须在目标与安全档案中确认，不能从学习中心固定")
            claim_type, status = "stable_preference", "active"
        elif action == "correct":
            clean = correction.strip()
            if not clean:
                raise ValidationError("纠正内容不能为空")
            status = "refuted"
            conn.execute(
                """INSERT OR IGNORE INTO user_model_evidence(
                       id,claim_id,evidence_type,evidence_id,stance,explicit,excerpt,observed_at,created_at
                   ) VALUES(?,?,?,?, 'counterexample',1,?,?,?)""",
                (new_id("claim_evidence"), claim_id, "user_correction", new_id("correction"), clean[:1000], timestamp, timestamp),
            )
        conn.execute(
            """UPDATE user_model_claims SET claim_type=?,statement=?,status=?,confidence=?,valid_until=?,
                   version=version+1,updated_at=? WHERE id=?""",
            (claim_type, statement, status, confidence, valid_until, timestamp, claim_id),
        )
        updated = row_dict(conn.execute("SELECT * FROM user_model_claims WHERE id=?", (claim_id,)).fetchone())
        _append_claim_version(conn, updated, f"user_{action}", timestamp)
        _refresh_user_model_projection(conn, timestamp)
    mark_all_drafts_stale(f"用户模型已{action}")
    if action == "correct" and correction.strip():
        updated["replacement_claim"] = upsert_claim(
            claim_type=row["claim_type"], statement=correction.strip(), scope=row.get("scope_json") or {},
            effect=row.get("effect_json") or {}, evidence_type="user_correction",
            evidence_id=new_id("correction"), excerpt=correction.strip(), explicit=True,
            proposed_risk=row["risk_level"], source="user_correction",
        )
    return updated


def _compact_claims() -> list[dict]:
    result = []
    today = date.today().isoformat()
    binding = _current_claim_binding()
    for claim in list_claims(include_inactive=False):
        if claim["status"] != "active":
            continue
        if claim.get("valid_until") and claim["valid_until"] < today:
            continue
        if not _claim_scope_is_current(claim.get("scope_json"), binding):
            continue
        result.append({
            "id": claim["id"], "type": claim["claim_type"], "statement": claim["statement"],
            "scope": claim["scope_json"], "effect": claim["effect_json"],
            "confidence": claim["effective_confidence"], "version": claim["version"],
            "evidence": {"support": claim["support_count"], "counter": claim["counter_count"]},
        })
    return result


def build_agent_context(review_date: str) -> dict:
    _validate_date(review_date)
    require_generation("daily")
    from . import service

    base = service.daily_review_context(review_date)
    personalization = active_personalization()
    knowledge = professional.applicable_knowledge(personalization)
    claims = _compact_claims()
    records_today = [item for item in base["recent_records"] if item["record_date"] == review_date]
    feedback = []
    for item in base.get("recent_execution_feedback") or []:
        snapshot = item.get("planned_snapshot_json") or {}
        feedback.append({
            "id": item["id"], "date": item["plan_date"], "meal": item.get("meal_name"),
            "status": item["status"], "reasons": item.get("reason_codes_json") or [],
            "actual": item.get("actual_text") or "", "outcome": item.get("outcome_json") or {},
            "planned_purpose": snapshot.get("purpose") or snapshot.get("portion_guidance") or "",
        })
    with connect() as conn:
        intake = [row_dict(row) for row in conn.execute(
            "SELECT * FROM agent_workspace_events WHERE event_date=? ORDER BY created_at", (review_date,)
        ).fetchall()]
        answered = [row_dict(row) for row in conn.execute(
            """SELECT * FROM agent_clarifications WHERE review_date=? AND status='answered'
               ORDER BY updated_at""", (review_date,)
        ).fetchall()]
    person = {
        "profile": personalization.get("profile"),
        "goals": personalization.get("goals") or [],
        "strategy": personalization.get("strategy"),
        "targets": personalization.get("targets") or [],
        "safety": personalization.get("safety") or {},
        "long_term_meal_modes": (base.get("meal_mode_resolution") or {}).get("default_meal_modes") or {},
        "active_user_model": claims,
        "confirmed_rules": base.get("confirmed_rules") or [],
    }
    today_payload = {
        "date": review_date,
        "records": records_today,
        "checkin": base.get("target_checkin") or {},
        "checkin_coverage": base.get("checkin_coverage") or {},
        "inventory": base.get("inventory") or [],
        "temporary_meal_arrangements": base.get("meal_mode_resolution") or {},
        "planning_answers": base.get("planning_answers") or [],
        "agent_intake": [
            {"id": item["id"], "input_text": item["input_text"], "created_at": item["created_at"]}
            for item in intake
        ],
        "clarification_answers": answered,
    }
    longitudinal = {
        "selected_execution_feedback": feedback[-30:],
        "relevant_meal_evidence": base.get("meal_evidence") or [],
        "recent_meal_semantics": base.get("recent_meal_semantics") or [],
        "recent_home_meals": base.get("recent_home_meals") or [],
        "ingredient_carryover": base.get("ingredient_carryover_obligations") or [],
        "active_experiment": base.get("active_experiment"),
        "transient_adaptations": base.get("transient_adaptations") or [],
    }
    immutable = {
        "effective_meal_modes": (base.get("meal_mode_resolution") or {}).get("effective_meal_modes") or {},
        "priority_foods": base.get("priority_foods") or [],
        "carryover_obligations": base.get("ingredient_carryover_obligations") or [],
        "cooking_constraints": base.get("home_cooking_preferences") or {},
        "doctrine": base.get("doctrine") or {},
        "generation_policy": base.get("generation_policy") or {},
    }
    selected_ids = {
        "records": [item["id"] for item in records_today],
        "feedback": [item["id"] for item in feedback],
        "claims": [{"id": item["id"], "version": item["version"]} for item in claims],
        "intake": [item["id"] for item in intake],
    }
    source_manifest = deepcopy(base.get("source_manifest") or {})
    source_manifest.update({
        "agent_context_version": AGENT_CONTEXT_VERSION,
        "case_formulation_version": CASE_FORMULATION_VERSION,
        "daily_plan_version": DAILY_PLAN_VERSION,
        "plan_review_version": PLAN_REVIEW_VERSION,
        "user_model_claim_version": CLAIM_VERSION,
        "user_model_claims": selected_ids["claims"],
        "professional_knowledge": {"version": knowledge["version"], "sha256": knowledge["sha256"]},
        "workspace_events": selected_ids["intake"],
    })
    context = {
        "context_schema": "AgentContextV2",
        "context_schema_version": AGENT_CONTEXT_VERSION,
        "generation_policy": base["generation_policy"],
        "person": person,
        "today": today_payload,
        "longitudinal": longitudinal,
        "professional_basis": knowledge,
        "decision_task": {
            "task": "理解今天这个人的真实状态，形成次日可执行计划草案",
            "review_date": review_date,
            "plan_date": (date.fromisoformat(review_date) + timedelta(days=1)).isoformat(),
            "ask_only_if_decision_changes": True,
            "question_budget": min(3, int((((personalization.get("profile") or {}).get("profile_json") or {}).get("constraints") or {}).get("question_budget", 2))),
            "allowed_soft_assumptions": ["排序", "份量", "风味", "复杂度", "沟通方式", "备选方案"],
            "must_confirm": ["目标", "营养目标", "安全模式", "过敏禁忌", "疾病药物", "孕期哺乳", "长期排除", "专业指导"],
            "immutable_constraints": immutable,
            "required_output_contract": "DailyPlanV3",
        },
        "source_manifest": source_manifest,
        "context_inspector": {
            "included": [
                {"section": "person", "reason": "当前目标、安全边界、逐餐策略与生效中的用户模型"},
                {"section": "today", "reason": "只包含会影响本次决定的当天事实、状态、库存和临时安排"},
                {"section": "longitudinal", "reason": "选择最近执行结果、菜单语义、承接食材和当前实验"},
                {"section": "professional_basis", "reason": "只选择适用于当前目标和安全模式的离线权威原则"},
            ],
            "excluded": [
                {"kind": "unrelated_history", "reason": "不把全部历史原文倾倒给模型"},
                {"kind": "inactive_or_refuted_claims", "reason": "不会影响计划，但仍在学习中心保留"},
                {"kind": "expired_state", "reason": "过期临时状态不进入规划"},
            ],
            "selected_source_ids": selected_ids,
        },
    }
    context["context_hash"] = _hash(context)
    return context


def case_formulation_schema() -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": [
            "current_state", "explicit_goals", "underlying_needs", "tensions",
            "decisive_constraints", "historical_patterns", "soft_assumptions",
            "uncertainties", "intake_classifications", "clarification_questions", "planning_priorities",
        ],
        "properties": {
            "current_state": {"type": "array", "items": {"type": "string"}},
            "explicit_goals": {"type": "array", "items": {"type": "string"}},
            "underlying_needs": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "tensions": {"type": "array", "items": {"type": "string"}},
            "decisive_constraints": {"type": "array", "items": {"type": "string"}},
            "historical_patterns": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "soft_assumptions": {"type": "array", "items": _claim_candidate_schema()},
            "uncertainties": {"type": "array", "items": {"type": "string"}},
            "intake_classifications": {
                "type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["event_id", "kind", "summary", "scope", "affects_plan"],
                    "properties": {
                        "event_id": {"type": "string"},
                        "kind": {"type": "string", "enum": [
                            "fact", "preference_signal", "temporary_state", "plan_change", "goal_candidate"
                        ]},
                        "summary": {"type": "string"},
                        "scope": {"type": "string", "enum": ["today", "candidate_long_term"]},
                        "affects_plan": {"type": "boolean"},
                    },
                },
            },
            "clarification_questions": {
                "type": "array", "maxItems": 3,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["key", "prompt", "reason", "decision_impact", "answer_schema"],
                    "properties": {
                        "key": {"type": "string"}, "prompt": {"type": "string"},
                        "reason": {"type": "string"}, "decision_impact": {"type": "string"},
                        "answer_schema": {"type": "object", "additionalProperties": True},
                    },
                },
            },
            "planning_priorities": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string"}},
        },
    }


def _portion_contract_schema() -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": [
            "item", "gram_range", "measurement_basis", "household_measure",
            "nutrition_estimate", "confidence", "increase_if", "decrease_if",
        ],
        "properties": {
            "item": {"type": "string"},
            "gram_range": {"anyOf": [
                {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number", "minimum": 0}},
                {"type": "null"},
            ]},
            "measurement_basis": {"type": "string", "enum": ["raw", "cooked", "as_served", "not_applicable"]},
            "household_measure": {"type": "string"},
            "nutrition_estimate": {"type": "object", "additionalProperties": True},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "increase_if": {"type": "string"}, "decrease_if": {"type": "string"},
        },
    }


def _claim_candidate_schema() -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": [
            "claim_type", "statement", "scope", "planning_effect", "evidence_ids",
            "evidence_summary", "risk_level", "explicit_user_statement", "valid_until",
        ],
        "properties": {
            "claim_type": {"type": "string", "enum": sorted(CLAIM_TYPES)},
            "statement": {"type": "string"},
            "scope": {"type": "object", "additionalProperties": True},
            "planning_effect": {"type": "object", "additionalProperties": True},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "evidence_summary": {"type": "string"},
            "risk_level": {"type": "string", "enum": ["low", "high"]},
            "explicit_user_statement": {"type": "boolean"},
            "valid_until": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
    }


def daily_plan_v3_schema(context: dict) -> dict:
    from .ai import result_json_schema

    base_context = {"generation_policy": context["generation_policy"]}
    base_context["settings"] = _daily_settings_from_agent_context(context)
    schema = deepcopy(result_json_schema("daily", base_context))
    schema["properties"].update({
        "case_summary": {"type": "string"},
        "planning_rationale": {"type": "array", "items": {"type": "string"}},
        "evidence_summary": {"type": "array", "items": {"type": "string"}},
        "possible_resistance": {"type": "array", "items": {"type": "string"}},
        "adjustment_conditions": {"type": "array", "items": {"type": "string"}},
        "day_nutrition": {
            "type": "object", "additionalProperties": False,
            "required": ["energy_kcal", "protein_g", "confidence", "method", "unknowns"],
            "properties": {
                "energy_kcal": {"anyOf": [
                    {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number", "minimum": 0}},
                    {"type": "null"},
                ]},
                "protein_g": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number", "minimum": 0}},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "method": {"type": "string"},
                "unknowns": {"type": "array", "items": {"type": "string"}},
            },
        },
    })
    schema["required"].extend([
        "case_summary", "planning_rationale", "evidence_summary", "possible_resistance", "adjustment_conditions",
        "day_nutrition",
    ])
    meal = schema["properties"]["tomorrow_menu"]["properties"]["meals"]["items"]
    meal["required"].extend([
        "purpose", "why_today", "portion_contracts", "whole_day_role", "adjustment_logic",
    ])
    meal["properties"].update({
        "purpose": {"type": "string"}, "why_today": {"type": "string"},
        "whole_day_role": {"type": "string"},
        "portion_contracts": {"type": "array", "minItems": 1, "items": _portion_contract_schema()},
        "adjustment_logic": {
            "type": "object", "additionalProperties": False,
            "required": ["if_hungry", "if_low_appetite", "if_gut_unwell"],
            "properties": {
                "if_hungry": {"type": "string"}, "if_low_appetite": {"type": "string"},
                "if_gut_unwell": {"type": "string"},
            },
        },
    })
    return schema


def _daily_settings_from_agent_context(context: dict) -> dict:
    constraints = context["decision_task"]["immutable_constraints"]
    cooking = deepcopy(constraints.get("cooking_constraints") or {})
    modes = deepcopy(constraints.get("effective_meal_modes") or {})
    return {
        "meal_modes": modes,
        "home_cooking": cooking,
        "meal_environment": "mixed" if len(set(modes.values())) > 1 else next(iter(modes.values()), "unknown"),
    }


def plan_review_schema() -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["approved", "human_fit_summary", "issues", "claim_candidates"],
        "properties": {
            "approved": {"type": "boolean"},
            "human_fit_summary": {"type": "string"},
            "issues": {
                "type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["severity", "dimension", "description", "affected_meals", "suggested_change"],
                    "properties": {
                        "severity": {"type": "string", "enum": ["blocking", "important", "minor"]},
                        "dimension": {"type": "string"}, "description": {"type": "string"},
                        "affected_meals": {"type": "array", "items": {"type": "string"}},
                        "suggested_change": {"type": "string"},
                    },
                },
            },
            "claim_candidates": {"type": "array", "items": _claim_candidate_schema()},
        },
    }


def targeted_revision_schema(context: dict) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["affected_meals", "global_balance_changed", "change_summary", "updated_result", "claim_candidates"],
        "properties": {
            "affected_meals": {"type": "array", "items": {"type": "string", "enum": ["早餐", "午餐", "晚餐"]}},
            "global_balance_changed": {"type": "boolean"},
            "change_summary": {"type": "array", "items": {"type": "string"}},
            "updated_result": daily_plan_v3_schema(context),
            "claim_candidates": {"type": "array", "items": _claim_candidate_schema()},
        },
    }


def _validate_formulation(result: dict, question_budget: int) -> dict:
    if not isinstance(result, dict):
        raise ValidationError("个案理解必须是 JSON 对象")
    required = set(case_formulation_schema()["required"])
    missing = sorted(required - set(result))
    if missing:
        raise ValidationError(f"个案理解缺少字段：{missing}")
    questions = result.get("clarification_questions")
    if not isinstance(questions, list):
        raise ValidationError("clarification_questions 必须是数组")
    filtered = []
    for item in questions[: min(3, question_budget)]:
        if not isinstance(item, dict) or not str(item.get("decision_impact") or "").strip():
            continue
        filtered.append(item)
    result = deepcopy(result)
    result["clarification_questions"] = filtered
    priorities = result.get("planning_priorities")
    if not isinstance(priorities, list) or not 1 <= len(priorities) <= 3:
        raise ValidationError("planning_priorities 必须包含 1–3 项")
    return result


def _validate_portions(result: dict, context: dict) -> None:
    meals = ((result.get("tomorrow_menu") or {}).get("meals") or [])
    if len(meals) != 3:
        raise ValidationError("DailyPlanV3 必须包含三餐")
    for meal in meals:
        contracts = meal.get("portion_contracts")
        if not isinstance(contracts, list) or not contracts:
            raise ValidationError(f"{meal.get('name','餐次')}缺少可执行份量合同")
        for contract in contracts:
            if not isinstance(contract, dict):
                raise ValidationError("份量合同必须是对象")
            required = {
                "item", "gram_range", "measurement_basis", "household_measure",
                "nutrition_estimate", "confidence", "increase_if", "decrease_if",
            }
            if required - set(contract):
                raise ValidationError(f"{meal.get('name','餐次')}份量合同字段不完整")
            grams = contract.get("gram_range")
            if grams is not None and (
                not isinstance(grams, list) or len(grams) != 2 or
                not all(isinstance(value, (int, float)) and value >= 0 for value in grams) or grams[0] > grams[1]
            ):
                raise ValidationError(f"{meal.get('name','餐次')}克数范围无效")
            if grams is None and not str(contract.get("household_measure") or "").strip():
                raise ValidationError(f"{meal.get('name','餐次')}未知克数时必须提供生活量具")
        adjustment = meal.get("adjustment_logic")
        if not isinstance(adjustment, dict) or not all(
            str(adjustment.get(key) or "").strip()
            for key in ("if_hungry", "if_low_appetite", "if_gut_unwell")
        ):
            raise ValidationError(f"{meal.get('name','餐次')}必须说明饥饿、低食欲和肠胃不适时怎样调整")
    day_nutrition = result.get("day_nutrition")
    if not isinstance(day_nutrition, dict):
        raise ValidationError("DailyPlanV3 缺少全天营养覆盖说明")
    planned_protein = day_nutrition.get("protein_g")
    if not isinstance(planned_protein, list) or len(planned_protein) != 2 or planned_protein[0] > planned_protein[1]:
        raise ValidationError("day_nutrition.protein_g 必须是有效范围")
    meal_ranges = [meal.get("protein_g") for meal in meals]
    if all(isinstance(value, list) and len(value) == 2 for value in meal_ranges):
        meal_floor = sum(value[0] for value in meal_ranges)
        meal_ceiling = sum(value[1] for value in meal_ranges)
        if meal_ceiling < planned_protein[0] or meal_floor > planned_protein[1]:
            raise ValidationError("三餐蛋白范围与全天蛋白范围没有合理重叠")
    targets = context["person"].get("targets") or []
    protein_target = next((item.get("value_json") for item in targets if item.get("target_key") == "protein_g"), None)
    if isinstance(protein_target, list) and len(protein_target) == 2 and planned_protein[0] < protein_target[0]:
        raise ValidationError(f"全天计划蛋白下界必须覆盖已确认目标下界 {protein_target[0]}g")
    energy_target = next((item.get("value_json") for item in targets if item.get("target_key") == "energy_kcal"), None)
    energy = day_nutrition.get("energy_kcal")
    if isinstance(energy_target, list) and len(energy_target) == 2:
        if not isinstance(energy, list) or len(energy) != 2:
            raise ValidationError("数值辅助模式有有效能量目标时，全天能量必须提供范围")
        if energy[1] < energy_target[0] or energy[0] > energy_target[1]:
            raise ValidationError("全天能量范围与当前有效目标没有合理重叠")


def _stage_generate(provider: ai.AIProvider, kind: str, context: dict, schema: dict, attempts: list[dict]) -> dict:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            value = ai.generate_stage_json(context, kind, schema, provider)
            attempts.append({"stage": kind, "attempt": attempt, "status": "completed"})
            return value
        except Exception as exc:  # network, parse and provider errors all leave no product history
            last_error = exc
            attempts.append({"stage": kind, "attempt": attempt, "status": "failed", "error": str(exc)[:800]})
            if attempt == 3:
                break
    assert last_error is not None
    raise last_error


def _create_or_replace_draft(
    run_id: str,
    review_date: str,
    status: str,
    context: dict,
    *,
    formulation: dict | None = None,
    result: dict | None = None,
    review: dict | None = None,
    stale_reason: str = "",
) -> dict:
    timestamp = _now()
    with connect() as conn:
        existing = conn.execute(
            "SELECT id,version,created_at,result_json FROM agent_drafts WHERE review_date=?", (review_date,)
        ).fetchone()
        if existing:
            next_version = existing["version"] + (
                1 if existing["result_json"] is not None and result is not None else 0
            )
            conn.execute(
                """UPDATE agent_drafts SET run_id=?,status=?,formulation_json=?,result_json=?,review_json=?,
                       context_hash=?,source_manifest_json=?,stale_reason=?,version=?,updated_at=?
                   WHERE review_date=?""",
                (
                    run_id, status, json.dumps(formulation or {}, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    json.dumps(review or {}, ensure_ascii=False), context["context_hash"],
                    json.dumps(context["source_manifest"], ensure_ascii=False, sort_keys=True), stale_reason,
                    next_version, timestamp, review_date,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO agent_drafts(
                       id,review_date,run_id,status,formulation_json,result_json,review_json,context_hash,
                       source_manifest_json,stale_reason,version,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                (
                    new_id("draft"), review_date, run_id, status,
                    json.dumps(formulation or {}, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    json.dumps(review or {}, ensure_ascii=False), context["context_hash"],
                    json.dumps(context["source_manifest"], ensure_ascii=False, sort_keys=True), stale_reason,
                    timestamp, timestamp,
                ),
            )
        row = conn.execute("SELECT * FROM agent_drafts WHERE review_date=?", (review_date,)).fetchone()
    return row_dict(row)


def _update_run(run_id: str, status: str, *, formulation: dict | None = None, review: dict | None = None,
                clarifications: list[dict] | None = None, attempts: list[dict] | None = None,
                error: str = "", completed: bool = False) -> None:
    if status not in WORKSPACE_STATUSES:
        raise ValidationError("Agent 运行状态无效")
    timestamp = _now()
    fields = ["status=?", "updated_at=?"]
    values: list[Any] = [status, timestamp]
    if formulation is not None:
        fields.append("formulation_json=?")
        values.append(json.dumps(formulation, ensure_ascii=False))
    if review is not None:
        fields.append("review_json=?")
        values.append(json.dumps(review, ensure_ascii=False))
    if clarifications is not None:
        fields.append("clarification_json=?")
        values.append(json.dumps(clarifications, ensure_ascii=False))
    if attempts is not None:
        fields.append("attempts_json=?")
        values.append(json.dumps(attempts, ensure_ascii=False))
    if error:
        fields.append("error_summary=?")
        values.append(error[:2000])
    if completed:
        fields.append("completed_at=?")
        values.append(timestamp)
    values.append(run_id)
    with connect() as conn:
        conn.execute(f"UPDATE agent_planning_runs SET {','.join(fields)} WHERE id=?", tuple(values))


def _save_clarifications(run_id: str, review_date: str, questions: list[dict]) -> list[dict]:
    timestamp = _now()
    with connect() as conn:
        for item in questions[:3]:
            conn.execute(
                """INSERT INTO agent_clarifications(
                       id,run_id,review_date,question_key,prompt,reason,expected_impact,answer_schema_json,
                       status,version,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,'pending',0,?,?)""",
                (
                    new_id("agent_question"), run_id, review_date, str(item["key"])[:100],
                    str(item["prompt"])[:500], str(item.get("reason") or "")[:1000],
                    str(item.get("decision_impact") or "")[:1000],
                    json.dumps(item.get("answer_schema") or {}, ensure_ascii=False), timestamp, timestamp,
                ),
            )
        rows = conn.execute(
            "SELECT * FROM agent_clarifications WHERE run_id=? ORDER BY created_at", (run_id,)
        ).fetchall()
    return [row_dict(row) for row in rows]


def _apply_intake_classifications(review_date: str, formulation: dict, context: dict) -> None:
    allowed = set(context["context_inspector"]["selected_source_ids"].get("intake") or [])
    with connect() as conn:
        for item in formulation.get("intake_classifications") or []:
            if not isinstance(item, dict) or item.get("event_id") not in allowed:
                continue
            conn.execute(
                """UPDATE agent_workspace_events SET event_kind=?,classification_json=?,affects_plan=?
                   WHERE id=? AND event_date=?""",
                (
                    item.get("kind") or "unclassified",
                    json.dumps(item, ensure_ascii=False), 1 if item.get("affects_plan") else 0,
                    item["event_id"], review_date,
                ),
            )


def _learn_candidates(
    candidates: object,
    run_id: str,
    *,
    allowed_evidence_ids: set[str] | None = None,
    default_evidence_id: str | None = None,
    explicit_default: bool = False,
) -> None:
    if not isinstance(candidates, list):
        return
    allowed_evidence_ids = allowed_evidence_ids or set()
    for item in candidates[:10]:
        if not isinstance(item, dict):
            continue
        evidence_ids = [
            str(value) for value in item.get("evidence_ids") or []
            if str(value) in allowed_evidence_ids
        ]
        if not evidence_ids and default_evidence_id:
            evidence_ids = [default_evidence_id]
        evidence_type = "agent_workspace_event" if evidence_ids else "agent_hypothesis"
        evidence_ids = evidence_ids or [run_id]
        try:
            for evidence_id in evidence_ids:
                upsert_claim(
                    claim_type=str(item.get("claim_type") or "soft_need_hypothesis"),
                    statement=str(item.get("statement") or ""),
                    scope=item.get("scope") if isinstance(item.get("scope"), dict) else {},
                    effect=item.get("planning_effect") if isinstance(item.get("planning_effect"), dict) else {},
                    evidence_type=evidence_type, evidence_id=evidence_id,
                    excerpt=str(item.get("evidence_summary") or "")[:1000],
                    explicit=(
                        evidence_type == "agent_workspace_event"
                        and (explicit_default or bool(item.get("explicit_user_statement")))
                    ),
                    proposed_risk=str(item.get("risk_level") or "low"),
                    valid_until=item.get("valid_until"),
                )
        except ValidationError:
            continue


def run_agent_draft(review_date: str, client: ai.AIProvider | None = None, *, force: bool = False) -> dict:
    _validate_date(review_date)
    context = build_agent_context(review_date)
    existing = get_draft(review_date)
    if existing and existing["status"] == "ready_draft" and existing["context_hash"] == context["context_hash"] and not force:
        return existing
    case_provider = client or ai.provider_for_stage("case")
    plan_provider = client or ai.provider_for_stage("plan")
    review_provider = client or ai.provider_for_stage("review")
    provider_name, case_model = _provider_metadata(case_provider)
    _, plan_model = _provider_metadata(plan_provider)
    _, review_model = _provider_metadata(review_provider)
    model = json.dumps(
        {"case": case_model, "plan": plan_model, "review": review_model},
        ensure_ascii=False, sort_keys=True,
    )
    run_id = new_id("agent_run")
    timestamp = _now()
    with connect() as conn:
        conn.execute(
            """INSERT INTO agent_planning_runs(
                   id,review_date,status,provider,model,context_hash,source_manifest_json,started_at,updated_at
               ) VALUES(?,?,'formulating',?,?,?,?,?,?)""",
            (
                run_id, review_date, provider_name, model, context["context_hash"],
                json.dumps(context["source_manifest"], ensure_ascii=False, sort_keys=True), timestamp, timestamp,
            ),
        )
    _create_or_replace_draft(run_id, review_date, "formulating", context)
    attempts: list[dict] = []
    try:
        formulation = _stage_generate(case_provider, "case_formulation", context, case_formulation_schema(), attempts)
        formulation = _validate_formulation(
            formulation, int(context["decision_task"].get("question_budget") or 0)
        )
        _apply_intake_classifications(review_date, formulation, context)
        _update_run(run_id, "formulating", formulation=formulation, attempts=attempts)
        questions = formulation["clarification_questions"]
        if questions:
            saved = _save_clarifications(run_id, review_date, questions)
            _update_run(run_id, "needs_clarification", formulation=formulation, clarifications=saved, attempts=attempts)
            return _create_or_replace_draft(
                run_id, review_date, "needs_clarification", context, formulation=formulation
            )
        _update_run(run_id, "planning", formulation=formulation, attempts=attempts)
        planning_context = {
            "context_schema": "DailyPlanV3Input",
            "generation_policy": context["generation_policy"],
            "case_formulation": formulation,
            "person": context["person"],
            "today": context["today"],
            "longitudinal": context["longitudinal"],
            "professional_basis": context["professional_basis"],
            "decision_task": context["decision_task"],
        }
        candidate = _stage_generate(
            plan_provider, "daily_plan_v3", planning_context, daily_plan_v3_schema(context), attempts
        )
        _validate_portions(candidate, context)
        from . import service

        candidate = service.validate_daily_review_candidate(review_date, candidate)
        _update_run(run_id, "reviewing", attempts=attempts)
        review_context = {
            "context_schema": "PlanReviewV1Input",
            "generation_policy": context["generation_policy"],
            "case_formulation": formulation,
            "candidate_plan": candidate,
            "professional_basis": context["professional_basis"],
            "decision_task": context["decision_task"],
            "review_dimensions": [
                "真实需求", "菜量与生活量具", "训练和恢复", "食欲睡眠肠胃", "菜单轮换",
                "时间厨具能力", "历史纠正", "可执行性", "自然程度", "安全与伪精确",
            ],
        }
        review = _stage_generate(review_provider, "plan_review", review_context, plan_review_schema(), attempts)
        if review.get("approved") is not True:
            planning_context["independent_review_feedback"] = review.get("issues") or []
            planning_context["revision_instruction"] = "只修正审查指出的问题；保留已满足的决定。"
            candidate = _stage_generate(
                plan_provider, "daily_plan_v3_revision", planning_context, daily_plan_v3_schema(context), attempts
            )
            _validate_portions(candidate, context)
            candidate = service.validate_daily_review_candidate(review_date, candidate)
            review_context["candidate_plan"] = candidate
            review_context["previous_review"] = review
            review = _stage_generate(review_provider, "plan_review", review_context, plan_review_schema(), attempts)
            if review.get("approved") is not True:
                raise ValidationError("独立审查后仍有阻断问题；已保留原正式计划")
        current_context = build_agent_context(review_date)
        if current_context["context_hash"] != context["context_hash"]:
            _update_run(run_id, "interrupted", formulation=formulation, review=review, attempts=attempts,
                        error="规划期间上下文发生变化", completed=True)
            return _create_or_replace_draft(
                run_id, review_date, "stale", context, formulation=formulation, result=candidate,
                review=review, stale_reason="规划期间新增了记录或状态，草案没有发布",
            )
        candidate["agent_workbench"] = {
            "case_formulation_version": CASE_FORMULATION_VERSION,
            "case_summary": formulation,
            "plan_review_version": PLAN_REVIEW_VERSION,
            "review_summary": review,
            "context_hash": context["context_hash"],
            "professional_basis": {
                "version": context["professional_basis"]["version"],
                "principles": [
                    {"id": item["id"], "principle": item["principle"], "source": item["source"]}
                    for item in context["professional_basis"]["principles"]
                ],
            },
        }
        draft = _create_or_replace_draft(
            run_id, review_date, "ready_draft", context, formulation=formulation, result=candidate, review=review
        )
        _update_run(run_id, "ready_draft", formulation=formulation, review=review, attempts=attempts, completed=True)
        _learn_candidates(
            formulation.get("soft_assumptions"), run_id,
            allowed_evidence_ids=set(
                context["context_inspector"]["selected_source_ids"].get("intake") or []
            ),
        )
        _learn_candidates(review.get("claim_candidates"), run_id)
        return draft
    except Exception as exc:
        _update_run(run_id, "failed", attempts=attempts, error=str(exc), completed=True)
        _create_or_replace_draft(run_id, review_date, "failed", context, stale_reason=str(exc)[:1000])
        raise


def get_draft(review_date: str) -> dict | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM agent_drafts WHERE review_date=?", (review_date,)).fetchone()
    return row_dict(row)


def get_workspace_state(review_date: str) -> dict:
    _validate_date(review_date)
    draft = get_draft(review_date)
    with connect() as conn:
        questions = [row_dict(row) for row in conn.execute(
            """SELECT * FROM agent_clarifications WHERE review_date=? AND status IN ('pending','answered')
               ORDER BY created_at""", (review_date,)
        ).fetchall()]
        events = [row_dict(row) for row in conn.execute(
            "SELECT * FROM agent_workspace_events WHERE event_date=? ORDER BY created_at DESC", (review_date,)
        ).fetchall()]
        latest_run = row_dict(conn.execute(
            "SELECT * FROM agent_planning_runs WHERE review_date=? ORDER BY started_at DESC LIMIT 1", (review_date,)
        ).fetchone())
    stale = False
    if draft and draft["status"] == "ready_draft":
        try:
            current_hash = build_agent_context(review_date)["context_hash"]
            stale = current_hash != draft["context_hash"]
        except ValidationError:
            stale = True
        if stale:
            mark_draft_stale(review_date, "影响计划的上下文已经变化")
            draft = get_draft(review_date)
    return {
        "review_date": review_date,
        "draft": draft,
        "questions": questions,
        "events": events,
        "latest_run": latest_run,
        "claims": list_claims(include_inactive=True),
        "ai": ai.ai_status(),
        "stale": stale,
    }


def record_intake(review_date: str, text: str) -> dict:
    _validate_date(review_date)
    clean = str(text or "").strip()
    if not clean:
        raise ValidationError("请先告诉 Agent 今天发生了什么")
    if len(clean) > 4000:
        raise ValidationError("单次补充不能超过 4000 字")
    from . import service

    record = service.add_daily_record(review_date, clean, {"source": "agent_workspace"})
    event_id = new_id("agent_event")
    timestamp = _now()
    with connect() as conn:
        conn.execute(
            """INSERT INTO agent_workspace_events(
                   id,event_date,input_text,event_kind,classification_json,affects_plan,created_at
               ) VALUES(?,?,?,'unclassified',?,1,?)""",
            (event_id, review_date, clean, json.dumps({"record_id": record["id"]}, ensure_ascii=False), timestamp),
        )
    mark_draft_stale(review_date, "新增了今天的真实情况")
    schedule_auto_draft(review_date)
    return {"id": event_id, "record": record, "scheduled": True}


def record_diagnostic_event(event_date: str, event_kind: str, message: str, details: dict | None = None) -> dict:
    """Record a local Agent failure without changing facts, plans, or learning evidence."""
    _validate_date(event_date)
    clean_kind = str(event_kind or "diagnostic").strip()[:80] or "diagnostic"
    clean_message = str(message or "Agent 后处理失败").strip()[:1000]
    event_id = new_id("agent_event")
    timestamp = _now()
    with connect() as conn:
        conn.execute(
            """INSERT INTO agent_workspace_events(
                   id,event_date,input_text,event_kind,classification_json,affects_plan,created_at
               ) VALUES(?,?,?,?,?,0,?)""",
            (
                event_id,
                event_date,
                clean_message,
                clean_kind,
                json.dumps(details or {}, ensure_ascii=False),
                timestamp,
            ),
        )
    return {"id": event_id, "event_date": event_date, "event_kind": clean_kind}


def answer_clarification(question_id: str, answer: object, expected_version: int) -> dict:
    timestamp = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = row_dict(conn.execute("SELECT * FROM agent_clarifications WHERE id=?", (question_id,)).fetchone())
        if not row:
            raise KeyError(question_id)
        if row["status"] != "pending" or row["version"] != expected_version:
            raise ValidationError("问题状态已经变化，请刷新后重试")
        if answer is None or (isinstance(answer, str) and not answer.strip()):
            raise ValidationError("回答不能为空")
        conn.execute(
            """UPDATE agent_clarifications SET status='answered',answer_json=?,version=version+1,updated_at=?
               WHERE id=? AND version=?""",
            (json.dumps(answer, ensure_ascii=False), timestamp, question_id, expected_version),
        )
        updated = row_dict(conn.execute("SELECT * FROM agent_clarifications WHERE id=?", (question_id,)).fetchone())
        remaining = conn.execute(
            "SELECT COUNT(*) FROM agent_clarifications WHERE run_id=? AND status='pending'", (row["run_id"],)
        ).fetchone()[0]
    mark_draft_stale(row["review_date"], "关键问题已有新回答")
    if remaining == 0:
        schedule_auto_draft(row["review_date"], delay_seconds=0.1, force=True)
    return updated


def revise_draft(review_date: str, instruction: str, client: ai.AIProvider | None = None) -> dict:
    clean = str(instruction or "").strip()
    if not clean:
        raise ValidationError("请说明想修改什么")
    draft = get_draft(review_date)
    if not draft or draft["status"] != "ready_draft" or not draft.get("result_json"):
        raise ValidationError("当前没有可协商的最新草案")
    context = build_agent_context(review_date)
    if context["context_hash"] != draft["context_hash"]:
        mark_draft_stale(review_date, "上下文已变化，需重新理解后再修改")
        raise ValidationError("草案已经过期，系统正在基于新情况重新规划")
    provider = client or ai.provider_for_stage("plan")
    revision_context = {
        "context_schema": "TargetedPlanRevisionV1Input",
        "generation_policy": context["generation_policy"],
        "user_instruction": clean,
        "case_formulation": draft["formulation_json"],
        "current_draft": draft["result_json"],
        "immutable_constraints": context["decision_task"]["immutable_constraints"],
        "instruction": "只重算受影响餐次、全天平衡、购物和食材承接；未受影响餐次必须逐字段保持。",
    }
    attempts: list[dict] = []
    output = _stage_generate(provider, "targeted_plan_revision", revision_context, targeted_revision_schema(context), attempts)
    affected = set(output.get("affected_meals") or [])
    if not affected:
        raise ValidationError("局部修改没有指出受影响餐次")
    before = draft["result_json"]
    after = output.get("updated_result")
    if not isinstance(after, dict):
        raise ValidationError("局部修改没有返回完整可验证草案")
    old_meals = {item.get("name"): deepcopy(item) for item in before["tomorrow_menu"]["meals"]}
    new_menu = after.get("tomorrow_menu") or {}
    new_meals = {item.get("name"): item for item in new_menu.get("meals") or []}
    if set(old_meals) != {"早餐", "午餐", "晚餐"} or set(new_meals) != set(old_meals):
        raise ValidationError("局部修改必须保留完整三餐结构")
    for name, meal in old_meals.items():
        if name not in affected:
            new_meals[name] = meal
    new_menu["meals"] = [new_meals[name] for name in ("早餐", "午餐", "晚餐")]
    after["tomorrow_menu"] = new_menu
    _validate_portions(after, context)
    from . import service

    after = service.validate_daily_review_candidate(review_date, after)
    after["agent_workbench"] = deepcopy(before.get("agent_workbench") or {})
    after["agent_workbench"]["last_local_revision"] = {
        "instruction": clean, "affected_meals": sorted(affected),
        "change_summary": output.get("change_summary") or [], "revised_at": _now(),
    }
    timestamp = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT version,status FROM agent_drafts WHERE review_date=?", (review_date,)).fetchone()
        if not current or current["status"] != "ready_draft" or current["version"] != draft["version"]:
            raise ValidationError("草案已经变化，请刷新后重试")
        conn.execute(
            """UPDATE agent_drafts SET result_json=?,review_json=?,version=version+1,updated_at=?
               WHERE review_date=? AND version=?""",
            (
                json.dumps(after, ensure_ascii=False),
                json.dumps({"targeted_revision": output, "attempts": attempts}, ensure_ascii=False),
                timestamp, review_date, draft["version"],
            ),
        )
        event_id = new_id("agent_event")
        conn.execute(
            """INSERT INTO agent_workspace_events(
                   id,event_date,input_text,event_kind,classification_json,affects_plan,created_at
               ) VALUES(?,?,?,'plan_edit',?,1,?)""",
            (
                event_id, review_date, clean,
                json.dumps({"affected_meals": sorted(affected), "draft_version": draft["version"] + 1}, ensure_ascii=False),
                timestamp,
            ),
        )
        updated = row_dict(conn.execute("SELECT * FROM agent_drafts WHERE review_date=?", (review_date,)).fetchone())
    _learn_candidates(
        output.get("claim_candidates"), draft["run_id"],
        default_evidence_id=event_id, explicit_default=True,
    )
    return updated


def accept_draft(review_date: str) -> dict:
    draft = get_draft(review_date)
    if not draft or draft["status"] != "ready_draft" or not draft.get("result_json"):
        raise ValidationError("当前没有可接受的最新草案")
    context = build_agent_context(review_date)
    if context["context_hash"] != draft["context_hash"]:
        mark_draft_stale(review_date, "接受前发现上下文已变化")
        raise ValidationError("草案已经过期，没有覆盖现有正式计划")
    from . import service

    base_context = service.daily_review_context(review_date)
    base_context["source_manifest"].update(draft.get("source_manifest_json") or {})
    base_context["context_hash"] = draft["context_hash"]
    with connect() as conn:
        run = conn.execute(
            "SELECT provider,model FROM agent_planning_runs WHERE id=?", (draft["run_id"],)
        ).fetchone()
    result = service.complete_daily_review(
        review_date, deepcopy(draft["result_json"]), provenance_context=base_context,
        agent_run_id=draft["run_id"], generator={
            "provider": run["provider"] if run else "agent_workbench",
            "model": run["model"] if run else "three_stage",
        },
    )
    timestamp = _now()
    with connect() as conn:
        conn.execute(
            """UPDATE agent_drafts SET status='accepted',accepted_review_id=?,accepted_result_version=?,
                   accepted_at=?,updated_at=? WHERE review_date=?""",
            (result["id"], result["result_version"], timestamp, timestamp, review_date),
        )
        conn.execute(
            "UPDATE agent_planning_runs SET status='accepted',updated_at=?,completed_at=COALESCE(completed_at,?) WHERE id=?",
            (timestamp, timestamp, draft["run_id"]),
        )
        claim_ids = [item["id"] for item in context["person"].get("active_user_model") or []]
        if claim_ids:
            placeholders = ",".join("?" for _ in claim_ids)
            conn.execute(
                f"UPDATE user_model_claims SET last_used_at=?,updated_at=? WHERE id IN ({placeholders})",
                (timestamp, timestamp, *claim_ids),
            )
    return result


def mark_draft_stale(review_date: str, reason: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """UPDATE agent_drafts SET status='stale',stale_reason=?,version=version+1,updated_at=?
               WHERE review_date=? AND status IN ('formulating','planning','reviewing','ready_draft','needs_clarification')""",
            (str(reason or "上下文变化")[:1000], _now(), review_date),
        )


def mark_all_drafts_stale(reason: str) -> None:
    init_db()
    with connect() as conn:
        review_dates = [row["review_date"] for row in conn.execute(
            """SELECT review_date FROM agent_drafts
               WHERE status IN ('formulating','planning','reviewing','ready_draft','needs_clarification')"""
        ).fetchall()]
        conn.execute(
            """UPDATE agent_drafts SET status='stale',stale_reason=?,version=version+1,updated_at=?
               WHERE status IN ('formulating','planning','reviewing','ready_draft','needs_clarification')""",
            (str(reason or "用户模型变化")[:1000], _now()),
        )
    for review_date in review_dates:
        schedule_auto_draft(review_date)


def note_feedback_signal(feedback: dict) -> dict | None:
    reasons = set(feedback.get("reason_codes_json") or [])
    actual = str(feedback.get("actual_text") or "")
    outcome = feedback.get("outcome_json") or {}
    satiety = outcome.get("satiety")
    too_little = (
        satiety in {"not_enough", "hungry"}
        or any(token in actual for token in ("太少", "偏少", "没吃饱", "不够", "菜少"))
    )
    too_much = (
        satiety in {"too_much", "too_full"}
        or any(token in actual for token in ("太多", "吃不完", "撑"))
    )
    if "hunger_mismatch" in reasons or too_little or too_much:
        statement = (
            "这类餐次可能需要更高的蔬菜体积或更明确的加量条件"
            if too_little else (
                "这类餐次的计划份量可能超过实际食欲"
                if too_much else "这类餐次的计划份量与实际饥饿感不匹配"
            )
        )
        portion_effect = (
            "优先提高蔬菜体积，并明确吃不饱时的加量顺序"
            if too_little else "适度降低总体积，并明确食欲低时先减少什么"
        )
        return upsert_claim(
            claim_type="body_response_hypothesis", statement=statement,
            scope={"meal": feedback.get("meal_name") or "unknown"},
            effect={"portion": portion_effect},
            evidence_type="execution_feedback", evidence_id=feedback["id"], excerpt=actual,
            explicit=bool(actual or satiety), source="execution_feedback",
        )
    if reasons.intersection({"not_enough_time", "too_complex"}):
        return upsert_claim(
            claim_type="friction_hypothesis", statement="这类餐次的主动操作时间或步骤可能超过可接受范围",
            scope={"meal": feedback.get("meal_name") or "unknown"},
            effect={"complexity": "减少持续看火步骤并提供更短备选"},
            evidence_type="execution_feedback", evidence_id=feedback["id"], excerpt=actual,
            explicit=bool(actual), source="execution_feedback",
        )
    if "did_not_want_it" in reasons:
        return upsert_claim(
            claim_type="soft_need_hypothesis", statement="当日意愿会显著影响这类餐次的执行",
            scope={"meal": feedback.get("meal_name") or "unknown", "temporary_first": True},
            effect={"alternatives": "提供不同主蛋白或风味的低摩擦备选"},
            evidence_type="execution_feedback", evidence_id=feedback["id"], excerpt=actual,
            explicit=bool(actual), source="execution_feedback",
        )
    return None


def update_execution_state(review_id: str, result_version: int, meal_count: int) -> None:
    with connect() as conn:
        completed = conn.execute(
            """SELECT COUNT(*) FROM plan_execution_feedback
               WHERE review_id=? AND result_version=?""", (review_id, result_version)
        ).fetchone()[0]
        status = "completed" if completed >= meal_count else "active"
        timestamp = _now()
        draft = conn.execute(
            "SELECT run_id FROM agent_drafts WHERE accepted_review_id=? AND accepted_result_version=?",
            (review_id, result_version),
        ).fetchone()
        conn.execute(
            """UPDATE agent_drafts SET status=?,updated_at=?
               WHERE accepted_review_id=? AND accepted_result_version=?""",
            (status, timestamp, review_id, result_version),
        )
        if draft:
            conn.execute(
                "UPDATE agent_planning_runs SET status=?,updated_at=? WHERE id=?",
                (status, timestamp, draft["run_id"]),
            )


def reflection_status() -> dict:
    init_db()
    with connect() as conn:
        last = conn.execute(
            """SELECT created_at FROM agent_workspace_events
               WHERE event_kind='reflection_completed' ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        since = last["created_at"] if last else "1970-01-01T00:00:00+00:00"
        completed_dates = conn.execute(
            """SELECT COUNT(DISTINCT plan_date) FROM plan_execution_feedback
               WHERE updated_at>? AND status IN ('followed','modified','skipped')""", (since,)
        ).fetchone()[0]
        evidence_count = conn.execute(
            "SELECT COUNT(*) FROM plan_execution_feedback WHERE updated_at>?", (since,)
        ).fetchone()[0]
    last_time = datetime.fromisoformat(last["created_at"]) if last else None
    age_days = (datetime.now(timezone.utc) - last_time).days if last_time else None
    due = completed_dates >= 5 or (last_time is not None and age_days >= 7 and evidence_count > 0)
    return {
        "due": due, "last_reflection_at": last["created_at"] if last else None,
        "completed_plan_dates_since": completed_dates, "evidence_events_since": evidence_count,
        "rule": "累计 5 个已完成计划，或距上次反思 7 天且有新执行证据",
    }


def run_longitudinal_reflection(client: ai.AIProvider | None = None) -> dict:
    status = reflection_status()
    if not status["due"]:
        return {**status, "ran": False}
    policy = require_generation("adaptation")
    provider = client or ai.provider_from_environment()
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    with connect() as conn:
        feedback = [row_dict(row) for row in conn.execute(
            """SELECT id,plan_date,meal_name,status,reason_codes_json,actual_text,outcome_json,updated_at
               FROM plan_execution_feedback WHERE plan_date>=? ORDER BY plan_date,updated_at""", (cutoff,)
        ).fetchall()]
        edits = [row_dict(row) for row in conn.execute(
            """SELECT * FROM agent_workspace_events WHERE event_kind='plan_edit' AND event_date>=?
               ORDER BY created_at""", (cutoff,)
        ).fetchall()]
    allowed_ids = {item["id"] for item in feedback} | {item["id"] for item in edits}
    context = {
        "context_schema": "LongitudinalReflectionV1Input",
        "generation_policy": policy,
        "window_days": 30,
        "existing_user_model": _compact_claims(),
        "execution_feedback": feedback,
        "plan_edits": edits,
        "allowed_evidence_ids": sorted(allowed_ids),
        "rules": {
            "low_risk_only": True,
            "minimum_support": "一次明确纠正或两个独立隐式证据",
            "counterevidence_must_be_preserved": True,
            "forbidden": ["目标", "营养目标", "安全模式", "过敏禁忌", "疾病药物", "专业指导"],
        },
    }
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["summary", "claim_candidates", "counterexamples", "no_change_reasons"],
        "properties": {
            "summary": {"type": "string"},
            "claim_candidates": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "counterexamples": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "no_change_reasons": {"type": "array", "items": {"type": "string"}},
        },
    }
    attempts: list[dict] = []
    result = _stage_generate(provider, "longitudinal_reflection", context, schema, attempts)
    run_id = new_id("reflection")
    for index, item in enumerate(result.get("claim_candidates") or []):
        if not isinstance(item, dict):
            continue
        evidence_ids = [value for value in item.get("evidence_ids") or [] if value in allowed_ids]
        if not evidence_ids:
            continue
        for evidence_id in evidence_ids:
            upsert_claim(
                claim_type=str(item.get("claim_type") or "soft_need_hypothesis"),
                statement=str(item.get("statement") or ""), scope=item.get("scope") or {},
                effect=item.get("planning_effect") or {}, evidence_type="longitudinal_evidence",
                evidence_id=evidence_id, excerpt=str(item.get("evidence_summary") or ""),
                explicit=False, proposed_risk=str(item.get("risk_level") or "low"),
                valid_until=item.get("valid_until"), source="longitudinal_reflection",
            )
    timestamp = _now()
    with connect() as conn:
        conn.execute(
            """INSERT INTO agent_workspace_events(
                   id,event_date,input_text,event_kind,classification_json,affects_plan,created_at
               ) VALUES(?,?,?,'reflection_completed',?,0,?)""",
            (run_id, date.today().isoformat(), result.get("summary") or "纵向反思完成",
             json.dumps({"result": result, "attempts": attempts}, ensure_ascii=False), timestamp),
        )
    return {**status, "ran": True, "result": result, "attempts": attempts}


def schedule_reflection_if_due() -> dict:
    status = reflection_status()
    ai_state = ai.ai_status()
    if not status["due"] or not (
        ai_state["provider_valid"] and ai_state["model_configured"] and ai_state["key_configured"]
    ):
        return {**status, "scheduled": False}

    def worker() -> None:
        try:
            run_longitudinal_reflection()
        except Exception:
            pass

    timer = threading.Timer(0.1, worker)
    timer.daemon = True
    timer.start()
    return {**status, "scheduled": True}


def auto_generation_status(review_date: str) -> dict:
    try:
        require_generation("daily")
        from . import service
        service.ensure_daily_review(review_date)
    except (ValidationError, KeyError) as exc:
        return {"eligible": False, "reason": str(exc)}
    status = ai.ai_status()
    if not (status["provider_valid"] and status["model_configured"] and status["key_configured"]):
        return {"eligible": False, "reason": "未配置模型；可以查看和导出 AgentContextV2 手动处理"}
    return {"eligible": True, "reason": "满足自动草案条件"}


def schedule_auto_draft(review_date: str, *, delay_seconds: float = 30.0, force: bool = False) -> dict:
    eligibility = auto_generation_status(review_date)
    if not eligibility["eligible"]:
        return {**eligibility, "scheduled": False}

    def worker() -> None:
        try:
            run_agent_draft(review_date, force=force)
        except Exception:
            pass
        finally:
            with _TIMER_LOCK:
                _TIMERS.pop(review_date, None)

    with _TIMER_LOCK:
        previous = _TIMERS.get(review_date)
        previous = _TIMERS.pop(review_date, None)
        if previous is not None:
            previous.cancel()
        timer = threading.Timer(max(0.05, delay_seconds), worker)
        timer.daemon = True
        _TIMERS[review_date] = timer
        timer.start()
    return {**eligibility, "scheduled": True, "delay_seconds": delay_seconds}
