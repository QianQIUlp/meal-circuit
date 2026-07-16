from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import agent_intelligence as intelligence
from . import ai, professional
from .db import connect, init_db, row_dict
from .domain import new_id, utc_now
from .personalization import active_personalization, require_generation
from .validation import ValidationError


AGENT_CONTEXT_VERSION = 3
CASE_FORMULATION_VERSION = 1
DAILY_PLAN_VERSION = 3
PLAN_REVIEW_VERSION = 1
CLAIM_VERSION = 1
AGENT_RUN_PROTOCOL_VERSION = 1
AGENT_RUN_STAGES = (
    "facts",
    "intent_learning",
    "case_formulation",
    "professional_boundary",
    "strategy_comparison",
    "plan_design",
    "independent_review",
)
DETERMINISTIC_STAGES = {"facts", "professional_boundary"}
LOW_RISK_EFFECT_KEYS = {
    "ranking", "portion", "flavor", "complexity", "communication", "alternatives",
    "budget", "availability", "defaults",
}
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
_DURABLE_FEEDBACK_MARKERS = (
    "以后", "今后", "经常", "总是", "每次", "通常", "一直", "长期", "默认",
)
_AUDIT_ONLY_CONTEXT_KEYS = {
    "audit_context_hash", "context_hash", "source_manifest", "context_inspector",
    "created_at", "updated_at", "completed_at", "started_at", "accepted_at",
    "first_observed_at", "last_observed_at", "last_used_at", "revision_id",
    "version", "schema_version", "context_schema_version", "result_schema_version",
    "validator_version", "bundle_hash",
}


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


def _without_audit_metadata(value: object) -> object:
    """Keep decision content stable when only receipts, timestamps, or revisions change."""
    if isinstance(value, dict):
        return {
            key: _without_audit_metadata(item)
            for key, item in value.items()
            if key not in _AUDIT_ONLY_CONTEXT_KEYS
        }
    if isinstance(value, list):
        return [_without_audit_metadata(item) for item in value]
    return value


def _decision_context_projection(context: dict) -> dict:
    """Return only information whose meaning can change the plan."""
    projection = deepcopy(context)
    projection.pop("source_manifest", None)
    projection.pop("context_inspector", None)
    projection.pop("context_hash", None)
    projection.pop("audit_context_hash", None)
    claims = ((projection.get("person") or {}).get("active_user_model") or [])
    if projection.get("person") is not None:
        projection["person"]["active_user_model"] = [
            {
                key: claim.get(key)
                for key in (
                    "id", "type", "claim_type", "statement", "scope", "effect",
                    "planning_impact",
                )
            }
            for claim in claims
        ]
    return _without_audit_metadata(projection)


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
            "claim_dimension", "planning_impact_json", "last_plan_ids_json",
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
        """SELECT id,claim_type,claim_dimension,statement,scope_json,status,confidence,effect_json,
                  planning_impact_json,last_plan_ids_json,
                  support_count,counter_count,valid_until,version,updated_at
           FROM user_model_claims
           WHERE status IN ('active','pending_confirmation','paused','refuted')
           ORDER BY updated_at DESC,id"""
    ).fetchall():
        item = row_dict(row)
        claims.append({
            "id": item["id"], "type": item["claim_type"],
            "dimension": item.get("claim_dimension") or item["claim_type"],
            "statement": item["statement"],
            "scope": item["scope_json"], "status": item["status"],
            "confidence": item["confidence"], "effect": item["effect_json"],
            "support_count": item["support_count"], "counter_count": item["counter_count"],
            "valid_until": item["valid_until"], "version": item["version"], "updated_at": item["updated_at"],
            "planning_impact": item.get("planning_impact_json") or item["effect_json"],
            "last_plan_ids": item.get("last_plan_ids_json") or [],
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
            dimension = str(item.get("dimension") or item.get("type") or "")[:80]
            impact = _safe_effect(item.get("planning_impact") or item.get("effect") or {})
            conn.execute(
                """UPDATE user_model_claims SET claim_dimension=?,planning_impact_json=?,last_plan_ids_json=?
                   WHERE id=?""",
                (
                    dimension,
                    json.dumps(impact, ensure_ascii=False),
                    json.dumps(item.get("last_plan_ids") or [], ensure_ascii=False),
                    item["id"],
                ),
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


def _apply_detected_signals(signals: list[dict]) -> list[dict]:
    applied = []
    for signal in signals:
        candidate = signal.get("proposed_claim")
        if not isinstance(candidate, dict):
            applied.append({
                "signal_id": signal["signal_id"],
                "disposition": "requires_profile_confirmation" if signal.get("risk_level") == "high" else "noted",
                "claim_id": None,
            })
            continue
        lifetime = signal.get("lifetime")
        explicit_activation = bool(
            signal.get("explicit_user_statement")
            and lifetime in {"durable", "temporary"}
        )
        try:
            claim = upsert_claim(
                claim_type=str(candidate.get("claim_type") or "soft_need_hypothesis"),
                claim_dimension=str(candidate.get("claim_dimension") or candidate.get("claim_type") or ""),
                statement=str(candidate.get("statement") or ""),
                scope=candidate.get("scope") if isinstance(candidate.get("scope"), dict) else {},
                effect=candidate.get("planning_effect") if isinstance(candidate.get("planning_effect"), dict) else {},
                evidence_type=str(signal.get("source_type") or "user_statement"),
                evidence_id=str(signal.get("source_id") or signal["signal_id"]),
                excerpt=str(signal.get("excerpt") or ""),
                explicit=explicit_activation,
                proposed_risk=str(signal.get("risk_level") or "low"),
                valid_until=signal.get("valid_until"),
                source="deterministic_intent_signal",
            )
            applied.append({
                "signal_id": signal["signal_id"],
                "disposition": (
                    "active_soft_understanding"
                    if claim["status"] == "active"
                    else "requires_profile_confirmation"
                    if claim["risk_level"] == "high"
                    else "pending_confirmation"
                ),
                "claim_id": claim["id"],
            })
        except ValidationError as exc:
            applied.append({
                "signal_id": signal["signal_id"],
                "disposition": "rejected",
                "claim_id": None,
                "reason": str(exc),
            })
    return applied


def process_natural_language_input(
    source_id: str,
    text: str,
    source_date: str,
    *,
    source_type: str,
) -> dict:
    _validate_date(source_date)
    source = {
        "source_id": source_id,
        "source_type": source_type,
        "text": str(text or "").strip(),
        "observed_date": source_date,
        "explicit_user": True,
    }
    signals = intelligence.detect_intent_signals(source, source_date)
    counterevidence = []
    if any(marker in source["text"] for marker in ("不贵了", "价格能接受", "价格可以接受", "现在买得起", "现在可以买")):
        with connect() as conn:
            resource_claims = [row_dict(row) for row in conn.execute(
                """SELECT * FROM user_model_claims
                   WHERE claim_dimension='resource_constraint' AND status IN ('active','pending_confirmation')"""
            ).fetchall()]
        for claim in resource_claims:
            scope = claim.get("scope_json") or {}
            item = str(scope.get("item") or "").strip()
            if claim.get("claim_dimension") != "resource_constraint" or not item or item not in source["text"]:
                continue
            updated = upsert_claim(
                claim_type=claim["claim_type"], claim_dimension="resource_constraint",
                statement=claim["statement"], scope=scope,
                effect=claim.get("effect_json") or {}, evidence_type=source_type,
                evidence_id=source_id, excerpt=source["text"], explicit=True,
                stance="counterexample", proposed_risk="low", source="user_counterevidence",
            )
            counterevidence.append({"claim_id": updated["id"], "status": updated["status"]})
    return {
        "source_id": source_id,
        "processed": True,
        "signals": signals,
        "applied": _apply_detected_signals(signals),
        "counterevidence": counterevidence,
        "disposition": "signals_processed" if signals else "no_planning_signal",
    }


def _recover_recent_user_learning_once() -> None:
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    with connect() as conn:
        marker = conn.execute(
            "SELECT value FROM app_metadata WHERE key='agent_recent_user_learning_recovery_v1'"
        ).fetchone()
        if marker:
            return
        records = [row_dict(row) for row in conn.execute(
            """SELECT id,record_date,raw_input FROM daily_records
               WHERE record_date>=? ORDER BY record_date,created_at,id""",
            (cutoff,),
        ).fetchall()]
        feedback = [row_dict(row) for row in conn.execute(
            """SELECT f.* FROM plan_execution_feedback f
               WHERE f.plan_date>=? AND TRIM(f.actual_text)!=''
                 AND EXISTS (
                    SELECT 1 FROM plan_execution_feedback_events e
                    WHERE e.feedback_id=f.id AND e.actor_source IN ('user','web','cli')
                 )
               ORDER BY f.plan_date,f.created_at,f.id""",
            (cutoff,),
        ).fetchall()]
    for item in records:
        process_natural_language_input(
            item["id"], item["raw_input"], item["record_date"], source_type="daily_record"
        )
    for item in feedback:
        process_natural_language_input(
            item["id"], item["actual_text"], item["plan_date"], source_type="execution_feedback"
        )
    with connect() as conn:
        conn.execute(
            """INSERT INTO app_metadata(key,value) VALUES('agent_recent_user_learning_recovery_v1',?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (_now(),),
        )


def _feedback_requests_durable_change(text: str) -> bool:
    clean = str(text or "").strip()
    return bool(clean) and any(marker in clean for marker in _DURABLE_FEEDBACK_MARKERS)


def _repair_overeager_single_feedback_claims() -> None:
    """Return one-off friction observations to confirmation without erasing evidence."""
    timestamp = _now()
    changed = False
    with connect() as conn:
        rows = [row_dict(row) for row in conn.execute(
            """SELECT * FROM user_model_claims
               WHERE status='active' AND risk_level='low'
                 AND claim_dimension='execution_friction' AND source='execution_feedback'"""
        ).fetchall()]
        for claim in rows:
            user_decision = conn.execute(
                """SELECT 1 FROM user_model_claim_versions
                   WHERE claim_id=? AND change_reason IN (
                       'user_confirm','user_stable','user_resume','user_today'
                   ) LIMIT 1""",
                (claim["id"],),
            ).fetchone()
            if user_decision:
                continue
            evidence = [row_dict(row) for row in conn.execute(
                """SELECT * FROM user_model_evidence
                   WHERE claim_id=? AND stance='support' AND active=1""",
                (claim["id"],),
            ).fetchall()]
            if (
                len(evidence) != 1
                or evidence[0]["evidence_type"] != "execution_feedback"
                or _feedback_requests_durable_change(evidence[0].get("excerpt") or "")
            ):
                continue
            conn.execute(
                "UPDATE user_model_evidence SET explicit=0 WHERE id=?",
                (evidence[0]["id"],),
            )
            conn.execute(
                """UPDATE user_model_claims
                   SET status='pending_confirmation',confidence=MIN(confidence,0.55),
                       version=version+1,updated_at=? WHERE id=?""",
                (timestamp, claim["id"]),
            )
            updated = row_dict(conn.execute(
                "SELECT * FROM user_model_claims WHERE id=?", (claim["id"],)
            ).fetchone())
            _append_claim_version(conn, updated, "single_feedback_needs_confirmation", timestamp)
            changed = True
        if changed:
            _refresh_user_model_projection(conn, timestamp)


def list_claims(*, include_inactive: bool = True) -> list[dict]:
    init_db()
    _migrate_legacy_rules_once()
    _hydrate_user_model_projection()
    _recover_recent_user_learning_once()
    _repair_overeager_single_feedback_claims()
    timestamp = _now()
    with connect() as conn:
        expired = conn.execute(
            """UPDATE user_model_claims SET status='expired',version=version+1,updated_at=?
               WHERE status='active' AND valid_until IS NOT NULL AND valid_until<?""",
            (timestamp, date.today().isoformat()),
        )
        if expired.rowcount:
            _refresh_user_model_projection(conn, timestamp)
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
    profile = (current.get("profile") or {}).get("profile_json") or {}
    return {
        "profile_version_id": (current.get("profile") or {}).get("id"),
        "goal_version_ids": sorted(item["id"] for item in current.get("goals") or []),
        "goal_types": sorted(
            str((item.get("goal_json") or {}).get("type") or "")
            for item in current.get("goals") or []
            if (item.get("goal_json") or {}).get("type")
        ),
        "strategy_version_id": (current.get("strategy") or {}).get("id"),
        "life_stage": profile.get("life_stage") or "other",
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
    recorded = scope["binding"]
    for key in ("life_stage", "safety_mode", "policy_version"):
        if recorded.get(key) is not None and recorded.get(key) != binding.get(key):
            return False
    applicable_goals = scope.get("goal_types") or scope.get("applies_to_goal_types") or []
    if applicable_goals and not set(map(str, applicable_goals)).intersection(binding.get("goal_types") or []):
        return False
    return True


def _claim_identity_scope(scope: dict) -> dict:
    """Use semantic applicability for identity while retaining version IDs as provenance."""
    identity = deepcopy(scope)
    binding = identity.get("binding") if isinstance(identity.get("binding"), dict) else {}
    identity["binding"] = {
        key: binding.get(key)
        for key in ("safety_mode", "policy_version")
        if binding.get(key) is not None
    }
    return identity


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
    claim_dimension: str | None = None,
) -> dict:
    if claim_type not in CLAIM_TYPES:
        raise ValidationError("用户模型 claim 类型无效")
    clean_statement = str(statement or "").strip()
    if not clean_statement or len(clean_statement) > 600:
        raise ValidationError("用户模型理解必须是 1–600 字")
    scope = _scoped_claim(scope)
    clean_dimension = str(claim_dimension or claim_type).strip()[:80] or claim_type
    risk = _risk_level(claim_type, clean_statement, proposed_risk)
    effect = _safe_effect(effect) if risk == "low" else {}
    stable_key = _hash({
        "type": claim_type, "dimension": clean_dimension,
        "statement": clean_statement, "scope": _claim_identity_scope(scope),
    })[:20]
    claim_id = f"claim_{stable_key}"
    timestamp = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM user_model_claims WHERE id=?", (claim_id,)).fetchone()
        if existing is None:
            for candidate_row in conn.execute(
                """SELECT * FROM user_model_claims
                   WHERE claim_type=? AND claim_dimension=? AND statement=?""",
                (claim_type, clean_dimension, clean_statement),
            ).fetchall():
                candidate = row_dict(candidate_row)
                if _claim_identity_scope(candidate.get("scope_json") or {}) == _claim_identity_scope(scope):
                    existing = candidate_row
                    claim_id = candidate["id"]
                    break
        if existing is None:
            support = 1 if stance == "support" else 0
            counter = 1 if stance == "counterexample" else 0
            status = "active" if risk == "low" and explicit and stance == "support" else "pending_confirmation"
            confidence = 0.7 if status == "active" else (0.45 if support else 0.2)
            conn.execute(
                """INSERT INTO user_model_claims(
                       id,claim_type,claim_dimension,statement,scope_json,status,confidence,risk_level,effect_json,
                       planning_impact_json,
                       support_count,counter_count,first_observed_at,last_observed_at,valid_until,
                       source,version,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                (
                    claim_id, claim_type, clean_dimension, clean_statement, json.dumps(scope, ensure_ascii=False), status,
                    confidence, risk, json.dumps(effect, ensure_ascii=False),
                    json.dumps(effect, ensure_ascii=False), support, counter,
                    timestamp, timestamp, valid_until, source, timestamp, timestamp,
                ),
            )
        evidence_insert = conn.execute(
            """INSERT INTO user_model_evidence(
                   id,claim_id,evidence_type,evidence_id,stance,explicit,excerpt,active,observed_at,created_at
               ) VALUES(?,?,?,?,?,?,?,1,?,?)
               ON CONFLICT(claim_id,evidence_type,evidence_id,stance) DO UPDATE SET
                   explicit=excluded.explicit,excerpt=excluded.excerpt,active=1,
                   observed_at=excluded.observed_at
               WHERE user_model_evidence.active=0""",
            (
                new_id("claim_evidence"), claim_id, evidence_type, evidence_id, stance,
                1 if explicit else 0, str(excerpt or "")[:1000], timestamp, timestamp,
            ),
        )
        if existing is not None and evidence_insert.rowcount == 0:
            return row_dict(conn.execute(
                "SELECT * FROM user_model_claims WHERE id=?", (claim_id,)
            ).fetchone())
        support_count = conn.execute(
            """SELECT COUNT(DISTINCT evidence_type || ':' || evidence_id)
               FROM user_model_evidence WHERE claim_id=? AND stance='support' AND active=1""",
            (claim_id,),
        ).fetchone()[0]
        actionable_support_count = conn.execute(
            """SELECT COUNT(DISTINCT evidence_type || ':' || evidence_id)
               FROM user_model_evidence
               WHERE claim_id=? AND stance='support' AND evidence_type!='agent_hypothesis' AND active=1""",
            (claim_id,),
        ).fetchone()[0]
        counter_count = conn.execute(
            """SELECT COUNT(DISTINCT evidence_type || ':' || evidence_id)
               FROM user_model_evidence WHERE claim_id=? AND stance='counterexample' AND active=1""",
            (claim_id,),
        ).fetchone()[0]
        explicit_support = conn.execute(
            """SELECT 1 FROM user_model_evidence
               WHERE claim_id=? AND stance='support' AND explicit=1 AND active=1 LIMIT 1""",
            (claim_id,),
        ).fetchone() is not None
        row = row_dict(conn.execute("SELECT * FROM user_model_claims WHERE id=?", (claim_id,)).fetchone())
        status = row["status"]
        if risk == "low" and status not in {"paused", "forgotten"}:
            status = "active" if explicit_support or actionable_support_count >= 2 else "pending_confirmation"
        if counter_count and counter_count >= support_count:
            status = "refuted" if status == "active" else status
        confidence = min(0.92, max(0.1, 0.35 + support_count * 0.2 - counter_count * 0.25))
        version = row["version"] + (1 if existing is not None else 0)
        conn.execute(
            """UPDATE user_model_claims SET status=?,confidence=?,support_count=?,counter_count=?,scope_json=?,
                   last_observed_at=?,valid_until=COALESCE(?,valid_until),version=?,updated_at=? WHERE id=?""",
            (
                status, confidence, support_count, counter_count, json.dumps(scope, ensure_ascii=False),
                timestamp, valid_until, version, timestamp, claim_id,
            ),
        )
        updated = row_dict(conn.execute("SELECT * FROM user_model_claims WHERE id=?", (claim_id,)).fetchone())
        _append_claim_version(conn, updated, "evidence_added", timestamp)
        _refresh_user_model_projection(conn, timestamp)
    return updated


def update_claim(claim_id: str, action: str, *, correction: str = "") -> dict:
    actions = {"confirm", "reject", "correct", "today", "stable", "pause", "forget", "resume"}
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
        counter_count = int(row.get("counter_count") or 0)
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
        elif action == "reject":
            status = "refuted"
            confidence = min(confidence, 0.1)
            counter_count += 1
            conn.execute(
                """INSERT OR IGNORE INTO user_model_evidence(
                       id,claim_id,evidence_type,evidence_id,stance,explicit,excerpt,observed_at,created_at
                   ) VALUES(?,?,?,?, 'counterexample',1,?,?,?)""",
                (
                    new_id("claim_evidence"), claim_id, "user_rejection", new_id("rejection"),
                    "用户明确表示这条理解不对。", timestamp, timestamp,
                ),
            )
        elif action == "correct":
            clean = correction.strip()
            if not clean:
                raise ValidationError("纠正内容不能为空")
            status = "refuted"
            confidence = min(confidence, 0.1)
            counter_count += 1
            conn.execute(
                """INSERT OR IGNORE INTO user_model_evidence(
                       id,claim_id,evidence_type,evidence_id,stance,explicit,excerpt,observed_at,created_at
                   ) VALUES(?,?,?,?, 'counterexample',1,?,?,?)""",
                (new_id("claim_evidence"), claim_id, "user_correction", new_id("correction"), clean[:1000], timestamp, timestamp),
            )
        conn.execute(
            """UPDATE user_model_claims SET claim_type=?,statement=?,status=?,confidence=?,counter_count=?,valid_until=?,
                   version=version+1,updated_at=? WHERE id=?""",
            (claim_type, statement, status, confidence, counter_count, valid_until, timestamp, claim_id),
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
            claim_dimension=row.get("claim_dimension") or row["claim_type"],
        )
    return updated


def _compact_claims(*, effective_on: str | None = None) -> list[dict]:
    result = []
    effective_on = effective_on or date.today().isoformat()
    binding = _current_claim_binding()
    for claim in list_claims(include_inactive=False):
        if claim["status"] != "active":
            continue
        if claim.get("valid_until") and claim["valid_until"] < effective_on:
            continue
        if not _claim_scope_is_current(claim.get("scope_json"), binding):
            continue
        result.append({
            "id": claim["id"], "type": claim.get("claim_dimension") or claim["claim_type"],
            "claim_type": claim["claim_type"], "statement": claim["statement"],
            "scope": claim["scope_json"], "effect": claim["effect_json"],
            "planning_impact": claim.get("planning_impact_json") or claim["effect_json"],
            "confidence": claim["effective_confidence"], "version": claim["version"],
            "evidence": {"support": claim["support_count"], "counter": claim["counter_count"]},
        })
    return result


def _required_outcome_adjustments(rows: list[dict]) -> list[dict]:
    """Select recent, repeatable execution lessons that the next plan must address."""
    selected: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in reversed(rows):
        attribution = row.get("attribution_json") or {}
        cause = str(attribution.get("primary_cause") or "")
        next_change = str(attribution.get("next_change") or "").strip()
        meal_slot = str(row.get("meal_slot") or "unknown")
        key = (meal_slot, cause)
        if (
            key in seen
            or cause in {"", "none", "unknown", "temporary_event", "body_state"}
            or not next_change
            or attribution.get("likely_repeat") is not True
        ):
            continue
        seen.add(key)
        selected.append({
            "outcome_id": row.get("id") or attribution.get("feedback_id"),
            "feedback_id": attribution.get("feedback_id"),
            "plan_date": row.get("plan_date"),
            "meal_slot": meal_slot,
            "cause": cause,
            "purpose": attribution.get("purpose") or "",
            "required_change": next_change,
            "evidence": attribution.get("evidence") or {},
        })
        if len(selected) >= 5:
            break
    return list(reversed(selected))


def build_agent_context(review_date: str) -> dict:
    _validate_date(review_date)
    require_generation("daily")
    from . import service

    base = service.daily_review_context(review_date)
    personalization = active_personalization()
    knowledge = professional.applicable_knowledge(personalization)
    goal_contract = intelligence.goal_contract_projection(personalization)
    goal_program = intelligence.goal_program(personalization)
    professional_envelope = intelligence.professional_envelope(personalization, knowledge)
    claims = _compact_claims(
        effective_on=(date.fromisoformat(review_date) + timedelta(days=1)).isoformat()
    )
    episodes = intelligence.refresh_meal_episodes(review_date)
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
    restricted = (personalization.get("safety") or {}).get("mode") in {
        "clinician_guided", "observation", "halt_and_refer",
    }
    context_goals = [] if restricted else personalization.get("goals") or []
    context_targets = [
        item for item in personalization.get("targets") or []
        if not restricted or (
            (personalization.get("safety") or {}).get("mode") == "clinician_guided"
            and (personalization.get("safety") or {}).get("professional_guidance_current")
            and item.get("source_kind") == "clinician_provided"
        )
    ]
    raw_strategy = personalization.get("strategy") or {}
    if restricted and raw_strategy:
        strategy_json = raw_strategy.get("strategy_json") or {}
        context_strategy = {
            "id": raw_strategy.get("id"),
            "strategy_json": {
                "meal_modes": strategy_json.get("meal_modes") or {},
                "home_cooking": strategy_json.get("home_cooking") or {},
                "planning_mode": (personalization.get("safety") or {}).get("planning_mode"),
            },
        }
    else:
        context_strategy = raw_strategy
    person = {
        "profile": personalization.get("profile"),
        "goals": context_goals,
        "strategy": context_strategy,
        "targets": context_targets,
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
            {
                "id": item["id"], "input_text": item["input_text"], "created_at": item["created_at"],
                "record_id": (item.get("classification_json") or {}).get("record_id"),
            }
            for item in intake
        ],
        "clarification_answers": answered,
    }
    outcome_attributions = intelligence.list_outcome_attributions(
        (date.fromisoformat(review_date) - timedelta(days=30)).isoformat(), review_date
    )
    longitudinal = {
        "selected_execution_feedback": feedback[-30:],
        "relevant_meal_evidence": base.get("meal_evidence") or [],
        "recent_meal_semantics": base.get("recent_meal_semantics") or [],
        "recent_home_meals": base.get("recent_home_meals") or [],
        "ingredient_carryover": base.get("ingredient_carryover_obligations") or [],
        "active_experiment": base.get("active_experiment"),
        "transient_adaptations": base.get("transient_adaptations") or [],
        "meal_episodes": [item.get("projection_json") or {} for item in episodes],
        "outcome_attributions": outcome_attributions,
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
        "goal_contract": {"id": goal_contract["contract_id"], "version": intelligence.GOAL_CONTRACT_VERSION},
        "goal_program_version": intelligence.GOAL_PROGRAM_VERSION,
        "agent_run_protocol_version": AGENT_RUN_PROTOCOL_VERSION,
        "meal_episode_version": intelligence.MEAL_EPISODE_VERSION,
        "outcome_attribution_version": intelligence.OUTCOME_ATTRIBUTION_VERSION,
    })
    context = {
        "context_schema": "AgentContextV3",
        "context_schema_version": AGENT_CONTEXT_VERSION,
        "generation_policy": base["generation_policy"],
        "person": {**person, "goal_contract": goal_contract, "goal_program": goal_program},
        "today": today_payload,
        "longitudinal": longitudinal,
        "professional_basis": {**knowledge, "planning_envelope": professional_envelope},
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
            "mandatory_stages": [
                "facts", "intent_learning", "case_formulation", "professional_boundary",
                "strategy_comparison", "plan_design", "independent_review",
            ],
            "required_goal_dimensions": list(dict.fromkeys(
                dimension
                for program in goal_program.get("programs") or []
                for dimension in program.get("required_dimensions") or []
            )),
            "required_non_negotiables": goal_contract.get("non_negotiables") or [],
            "required_outcome_adjustments": _required_outcome_adjustments(outcome_attributions),
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
    context["fact_bundle"] = intelligence.compile_fact_bundle(
        context, episodes=context["longitudinal"]["meal_episodes"]
    )
    context["audit_context_hash"] = _hash(context)
    context["context_hash"] = _hash(_decision_context_projection(context))
    context["source_manifest"]["audit_context_hash"] = context["audit_context_hash"]
    context["source_manifest"]["decision_context_hash"] = context["context_hash"]
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
            "underlying_needs": {
                "type": "array", "items": {"anyOf": [
                    {"type": "string"},
                    {"type": "object", "additionalProperties": True},
                ]},
            },
            "tensions": {"type": "array", "items": {"type": "string"}},
            "decisive_constraints": {"type": "array", "items": {"type": "string"}},
            "historical_patterns": {
                "type": "array", "items": {"anyOf": [
                    {"type": "string"},
                    {"type": "object", "additionalProperties": True},
                ]},
            },
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


def intent_learning_schema(context: dict) -> dict:
    """Force every user-authored source and deterministic signal to be acknowledged."""
    bundle = context.get("fact_bundle") or {}
    return {
        "type": "object", "additionalProperties": False,
        "required": ["source_dispositions", "signal_dispositions", "learning_summary"],
        "properties": {
            "source_dispositions": {
                "type": "array",
                "minItems": len(bundle.get("natural_language_sources") or []),
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["source_id", "disposition", "summary"],
                    "properties": {
                        "source_id": {"type": "string"},
                        "disposition": {"type": "string", "enum": [
                            "today_fact", "temporary_state", "long_term_signal",
                            "high_impact_candidate", "no_planning_effect",
                        ]},
                        "summary": {"type": "string"},
                    },
                },
            },
            "signal_dispositions": {
                "type": "array",
                "minItems": len(bundle.get("detected_intent_signals") or []),
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["signal_id", "disposition", "explanation"],
                    "properties": {
                        "signal_id": {"type": "string"},
                        "disposition": {"type": "string", "enum": [
                            "active_soft_understanding", "temporary_understanding",
                            "pending_confirmation", "requires_profile_confirmation", "noted_no_change",
                        ]},
                        "explanation": {"type": "string"},
                    },
                },
            },
            "learning_summary": {"type": "array", "items": {"type": "string"}},
        },
    }


def strategy_comparison_schema() -> dict:
    score_properties = {
        key: {"type": "number", "minimum": 0, "maximum": 5}
        for key in (
            "safety", "goal_coverage", "budget", "time", "satiety",
            "taste", "rotation", "waste", "execution_probability",
        )
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": ["options", "selected_id", "selection_reason", "rejected_reasons"],
        "properties": {
            "options": {
                "type": "array", "minItems": 2, "maxItems": 3,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["id", "label", "summary", "scores", "tradeoffs", "coverage"],
                    "properties": {
                        "id": {"type": "string"}, "label": {"type": "string"},
                        "summary": {"type": "string"},
                        "scores": {
                            "type": "object", "additionalProperties": False,
                            "required": list(score_properties), "properties": score_properties,
                        },
                        "tradeoffs": {"type": "array", "items": {"type": "string"}},
                        "coverage": {
                            "type": "array", "minItems": 1,
                            "items": {
                                "type": "object", "additionalProperties": False,
                                "required": ["requirement_ref", "approach", "tradeoff"],
                                "properties": {
                                    "requirement_ref": {"type": "string"},
                                    "approach": {"type": "string"},
                                    "tradeoff": {"type": "string"},
                                },
                            },
                        },
                        "solves_priorities": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "selected_id": {"type": "string"},
            "selection_reason": {"type": "string"},
            "rejected_reasons": {
                "type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["id", "reason"],
                    "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
                },
            },
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
        "problems_to_solve": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "problem_responses": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["problem_ref", "response"],
                "properties": {
                    "problem_ref": {"type": "string"},
                    "response": {"type": "string"},
                },
            },
        },
        "selected_strategy": {"type": "string"},
        "strategy_tradeoffs": {"type": "array", "items": {"type": "string"}},
        "predictions": {
            "type": "object", "additionalProperties": False,
            "required": ["satiety", "recovery", "cost", "time", "execution_risks", "adjustment_triggers"],
            "properties": {
                "satiety": {"type": "string"}, "recovery": {"type": "string"},
                "cost": {"type": "string"}, "time": {"type": "string"},
                "execution_risks": {"type": "array", "items": {"type": "string"}},
                "adjustment_triggers": {"type": "array", "items": {"type": "string"}},
            },
        },
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
        "advice_evidence": {
            "type": "array", "minItems": 1, "maxItems": 3,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["advice", "basis_kind", "source_refs"],
                "properties": {
                    "advice": {"type": "string"},
                    "basis_kind": {
                        "type": "string",
                        "enum": ["user_context", "professional_principle"],
                    },
                    "source_refs": {
                        "type": "array", "minItems": 1,
                        "items": {"type": "string"},
                    },
                    "source_ids": {
                        "type": "array", "minItems": 1,
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "portion_reality": {"type": "object", "additionalProperties": True},
    })
    schema["required"].extend([
        "problem_responses", "selected_strategy", "strategy_tradeoffs", "predictions",
        "case_summary", "planning_rationale", "evidence_summary", "possible_resistance", "adjustment_conditions",
        "day_nutrition", "advice_evidence",
    ])
    meal = schema["properties"]["tomorrow_menu"]["properties"]["meals"]["items"]
    meal["required"].extend([
        "purpose", "why_today", "portion_contracts", "whole_day_role", "adjustment_logic",
        "predicted_satiety", "predicted_cost", "execution_risks",
    ])
    meal["properties"].update({
        "purpose": {"type": "string"}, "why_today": {"type": "string"},
        "whole_day_role": {"type": "string"},
        "predicted_satiety": {"type": "string"},
        "predicted_cost": {"type": "string"},
        "execution_risks": {"type": "array", "items": {"type": "string"}},
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
        "required": [
            "approved", "human_fit_summary", "problem_coverage", "dimension_coverage",
            "evidence_checks", "issues", "claim_candidates",
        ],
        "properties": {
            "approved": {"type": "boolean"},
            "human_fit_summary": {"type": "string"},
            "problem_coverage": {
                "type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["problem_ref", "addressed", "evidence"],
                    "properties": {
                        "problem_ref": {"type": "string"}, "problem": {"type": "string"},
                        "addressed": {"type": "boolean"},
                        "evidence": {"type": "string"},
                    },
                },
            },
            "dimension_coverage": {
                "type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["dimension_ref", "addressed", "evidence"],
                    "properties": {
                        "dimension_ref": {"type": "string"}, "dimension": {"type": "string"},
                        "addressed": {"type": "boolean"},
                        "evidence": {"type": "string"},
                    },
                },
            },
            "evidence_checks": {
                "type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["advice_ref", "supported", "source_or_boundary"],
                    "properties": {
                        "advice_ref": {"type": "string"}, "claim": {"type": "string"},
                        "supported": {"type": "boolean"},
                        "source_or_boundary": {"type": "string"},
                    },
                },
            },
            "issues": {
                "type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": [
                        "severity", "dimension", "description", "affected_meals",
                        "suggested_change", "user_harm",
                    ],
                    "properties": {
                        "severity": {"type": "string", "enum": ["block", "repair", "warn", "info"]},
                        "dimension": {"type": "string"}, "description": {"type": "string"},
                        "affected_meals": {"type": "array", "items": {"type": "string"}},
                        "suggested_change": {"type": "string"},
                        "user_harm": {"type": "string"},
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
    decision_changing_terms = (
        "安全", "过敏", "健康", "份量", "菜量", "蛋白", "能量", "训练", "恢复",
        "餐次", "外食", "自炊", "用餐", "时间", "预算", "库存", "食材", "饥饿",
        "食欲", "肠胃", "睡眠", "执行", "厨具", "目标", "safety", "portion",
        "training", "recovery", "meal", "budget", "inventory", "hunger", "appetite",
    )
    for item in questions[: min(3, question_budget)]:
        if not isinstance(item, dict):
            continue
        explanation = f"{item.get('reason') or ''} {item.get('decision_impact') or ''}".lower()
        if not explanation.strip() or not any(term in explanation for term in decision_changing_terms):
            continue
        filtered.append(item)
    result = deepcopy(result)
    result["underlying_needs"] = [
        {"need": item, "reason": "由个案阶段提出，具体依据见本次事实包。"}
        if isinstance(item, str) else item
        for item in result.get("underlying_needs") or []
    ]
    result["historical_patterns"] = [
        {"pattern": item, "evidence_ids": []}
        if isinstance(item, str) else item
        for item in result.get("historical_patterns") or []
    ]
    result["clarification_questions"] = filtered
    priorities = result.get("planning_priorities")
    if not isinstance(priorities, list) or not 1 <= len(priorities) <= 3:
        raise ValidationError("planning_priorities 必须包含 1–3 项")
    return result


def _validate_portions(result: dict, context: dict) -> dict:
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
    confidence = str(day_nutrition.get("confidence") or "low")
    uncertainty_ratio = {"high": 0.05, "medium": 0.10, "low": 0.15}.get(confidence, 0.15)
    coverage = "no_confirmed_numeric_target"
    if isinstance(protein_target, list) and len(protein_target) == 2:
        target_floor = float(protein_target[0])
        tolerated_floor = target_floor * (1 - uncertainty_ratio)
        if planned_protein[0] >= target_floor:
            coverage = "covers_confirmed_floor"
        elif planned_protein[1] >= target_floor and planned_protein[0] >= tolerated_floor:
            coverage = "reasonable_overlap_with_estimation_uncertainty"
        else:
            raise ValidationError(
                "全天计划蛋白范围明显低于已确认目标；不能用宽区间或单个高估值伪装覆盖"
            )
    energy_target = next((item.get("value_json") for item in targets if item.get("target_key") == "energy_kcal"), None)
    energy = day_nutrition.get("energy_kcal")
    if isinstance(energy_target, list) and len(energy_target) == 2:
        if not isinstance(energy, list) or len(energy) != 2:
            raise ValidationError("数值辅助模式有有效能量目标时，全天能量必须提供范围")
        if energy[1] < energy_target[0] or energy[0] > energy_target[1]:
            raise ValidationError("全天能量范围与当前有效目标没有合理重叠")
    hunger_module = (((context.get("today") or {}).get("checkin") or {}).get("modules") or {}).get("hunger") or {}
    hunger_answers = hunger_module.get("answers_json") or hunger_module.get("answers") or hunger_module
    if not isinstance(hunger_answers, dict):
        hunger_answers = {}
    appetite_context = {
        "hunger_level": hunger_answers.get("hunger_level"),
        "satiety": hunger_answers.get("satiety"),
        "cravings": hunger_answers.get("cravings"),
    }
    cost_context = [
        item.get("statement") for item in context["person"].get("active_user_model") or []
        if item.get("type") == "resource_constraint"
    ]
    execution_context = [
        item.get("statement") for item in context["person"].get("active_user_model") or []
        if item.get("type") in {"execution_friction", "satiety_pattern"}
    ]
    return {
        "protein_target_g": protein_target,
        "planned_protein_g": planned_protein,
        "estimate_confidence": confidence,
        "uncertainty_ratio": uncertainty_ratio,
        "coverage": coverage,
        "appetite_context": appetite_context,
        "cost_context": [item for item in cost_context if item],
        "execution_context": [item for item in execution_context if item],
        "review_question": (
            "这组份量是否在合理估算误差内支持已确认目标，同时尊重当天食欲、预算、"
            "实际可执行性和长期负担，而不是为了碰到一个精确下界机械加量？"
        ),
    }


def _stage_generate(provider: ai.AIProvider, kind: str, context: dict, schema: dict, attempts: list[dict]) -> dict:
    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            value = ai.generate_stage_json(context, kind, schema, provider)
            attempts.append({"stage": kind, "attempt": attempt, "status": "completed"})
            return value
        except Exception as exc:  # network, parse and provider errors all leave no product history
            last_error = exc
            attempts.append({"stage": kind, "attempt": attempt, "status": "failed", "error": str(exc)[:800]})
            if attempt == 2:
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
            existing = conn.execute(
                "SELECT classification_json FROM agent_workspace_events WHERE id=?", (item["event_id"],)
            ).fetchone()
            classification = dict(item)
            if existing:
                previous = json.loads(existing["classification_json"] or "{}")
                if previous.get("record_id"):
                    classification["record_id"] = previous["record_id"]
            conn.execute(
                """UPDATE agent_workspace_events SET event_kind=?,classification_json=?,affects_plan=?
                   WHERE id=? AND event_date=?""",
                (
                    item.get("kind") or "unclassified",
                    json.dumps(classification, ensure_ascii=False), 1 if item.get("affects_plan") else 0,
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


def _run_status_label(stage: str) -> str:
    if stage in {"facts", "intent_learning", "case_formulation", "professional_boundary"}:
        return "formulating"
    if stage in {"strategy_comparison", "plan_design"}:
        return "planning"
    return "reviewing"


def _agent_run_row(run_id: str) -> dict:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM agent_planning_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return row_dict(row)


def _stage_receipt_map(run_id: str) -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_stage_receipts WHERE run_id=? ORDER BY stage_order", (run_id,)
        ).fetchall()
    return {row["stage_key"]: row_dict(row) for row in rows}


def _save_stage_receipt(
    run_id: str,
    stage: str,
    stage_input: dict,
    schema: dict,
    *,
    status: str,
    result: dict | None,
    findings: list[dict] | None = None,
    submitted_by: str = "system",
) -> dict:
    if stage not in AGENT_RUN_STAGES:
        raise ValidationError("未知 Agent 阶段")
    timestamp = _now()
    values = (
        new_id("stage_receipt"), run_id, stage, AGENT_RUN_STAGES.index(stage), status,
        _hash(stage_input), json.dumps(stage_input, ensure_ascii=False, sort_keys=True, default=str),
        json.dumps(schema, ensure_ascii=False, sort_keys=True),
        json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None,
        json.dumps(findings or [], ensure_ascii=False, sort_keys=True), submitted_by,
        timestamp, timestamp,
    )
    with connect() as conn:
        conn.execute(
            """INSERT INTO agent_stage_receipts(
                   id,run_id,stage_key,stage_order,status,input_hash,input_json,schema_json,result_json,
                   findings_json,submitted_by,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id,stage_key) DO UPDATE SET
                   status=excluded.status,input_hash=excluded.input_hash,input_json=excluded.input_json,
                   schema_json=excluded.schema_json,result_json=excluded.result_json,
                   findings_json=excluded.findings_json,submitted_by=excluded.submitted_by,
                   updated_at=excluded.updated_at""",
            values,
        )
        row = conn.execute(
            "SELECT * FROM agent_stage_receipts WHERE run_id=? AND stage_key=?", (run_id, stage)
        ).fetchone()
    return row_dict(row)


def _set_run_cursor(
    run_id: str,
    cursor: int,
    *,
    status: str | None = None,
    state: dict | None = None,
    revision_count: int | None = None,
) -> None:
    current_stage = AGENT_RUN_STAGES[cursor] if cursor < len(AGENT_RUN_STAGES) else "finalize"
    fields = ["stage_cursor=?", "current_stage=?", "updated_at=?"]
    values: list[Any] = [cursor, current_stage, _now()]
    if status is not None:
        fields.append("status=?")
        values.append(status)
    if state is not None:
        fields.append("stage_state_json=?")
        values.append(json.dumps(state, ensure_ascii=False, sort_keys=True))
    if revision_count is not None:
        fields.append("revision_count=?")
        values.append(revision_count)
    values.append(run_id)
    with connect() as conn:
        conn.execute(f"UPDATE agent_planning_runs SET {','.join(fields)} WHERE id=?", tuple(values))


def _assert_run_context_current(run: dict) -> dict:
    context = build_agent_context(run["review_date"])
    if context["context_hash"] != run["context_hash"]:
        raise ValidationError("规划期间情况发生了变化，这次运行已失效；原计划保持不变")
    return context


def _review_evidence_pack(context: dict) -> dict:
    return {
        "user_facts": [
            {
                "source_id": item.get("source_id"),
                "kind": item.get("kind"),
                "value": item.get("value"),
                "certainty": item.get("certainty"),
            }
            for item in context["fact_bundle"].get("facts") or []
        ],
        "active_understandings": [
            {
                "id": item.get("id"),
                "statement": item.get("statement"),
                "scope": item.get("scope"),
                "planning_impact": item.get("planning_impact"),
            }
            for item in context["person"].get("active_user_model") or []
        ],
        "goal_contract": {
            "contract_id": context["person"]["goal_contract"].get("contract_id"),
            "goals": context["person"]["goal_contract"].get("goals") or [],
            "non_negotiables": context["person"]["goal_contract"].get("non_negotiables") or [],
        },
        "professional_principles": [
            {
                "id": item.get("id"),
                "principle": item.get("principle"),
                "boundary": item.get("boundary"),
            }
            for item in context["professional_basis"].get("principles") or []
        ],
    }


def _planning_requirement_catalog(formulation: dict, context: dict) -> list[dict]:
    catalog: list[dict] = []
    for index, statement in enumerate(formulation.get("planning_priorities") or [], 1):
        catalog.append({"ref": f"case:{index}", "kind": "case_priority", "statement": str(statement)})
    programs = ((context.get("person") or {}).get("goal_program") or {}).get("programs") or []
    if programs:
        for index, program in enumerate(programs, 1):
            dimensions = [str(item) for item in program.get("required_dimensions") or []]
            catalog.append({
                "ref": f"goal:{index}",
                "kind": "goal_program",
                "statement": f"{program.get('label') or '当前目标'}：{'、'.join(dimensions)}",
                "dimensions": dimensions,
            })
    else:
        for index, statement in enumerate(context["decision_task"].get("required_goal_dimensions") or [], 1):
            catalog.append({
                "ref": f"goal:{index}", "kind": "goal_program", "statement": str(statement),
                "dimensions": [str(statement)],
            })
    for index, statement in enumerate(context["decision_task"].get("required_non_negotiables") or [], 1):
        catalog.append({"ref": f"boundary:{index}", "kind": "non_negotiable", "statement": str(statement)})
    for index, item in enumerate(context["decision_task"].get("required_outcome_adjustments") or [], 1):
        catalog.append({
            "ref": f"outcome:{index}",
            "kind": "execution_learning",
            "statement": str(item.get("required_change") or ""),
            "evidence": {
                "feedback_id": item.get("feedback_id"),
                "plan_date": item.get("plan_date"),
                "meal_slot": item.get("meal_slot"),
                "cause": item.get("cause"),
            },
        })
    return catalog


def _plan_problem_catalog(formulation: dict, context: dict) -> list[dict]:
    return [
        item for item in _planning_requirement_catalog(formulation, context)
        if item["kind"] in {"case_priority", "execution_learning"}
    ]


def _evidence_catalog(context: dict) -> list[dict]:
    catalog: list[dict] = []
    for index, item in enumerate(context["fact_bundle"].get("facts") or [], 1):
        catalog.append({
            "ref": f"evidence.fact:{index}", "kind": "user_context",
            "source_id": item.get("source_id"), "summary": item.get("value"),
        })
    for index, item in enumerate(context["person"].get("active_user_model") or [], 1):
        catalog.append({
            "ref": f"evidence.understanding:{index}", "kind": "user_context",
            "source_id": item.get("id"), "summary": item.get("statement"),
        })
    contract = context["person"].get("goal_contract") or {}
    catalog.append({
        "ref": "evidence.goal:1", "kind": "user_context",
        "source_id": contract.get("contract_id"),
        "summary": {
            "goals": contract.get("goals") or [],
            "non_negotiables": contract.get("non_negotiables") or [],
        },
    })
    for index, item in enumerate(context["professional_basis"].get("principles") or [], 1):
        catalog.append({
            "ref": f"evidence.principle:{index}", "kind": "professional_principle",
            "source_id": item.get("id"), "summary": item.get("principle"),
            "boundary": item.get("boundary"),
        })
    return [item for item in catalog if item.get("source_id")]


def _review_catalog(formulation: dict, context: dict, candidate: dict) -> dict:
    requirements = _planning_requirement_catalog(formulation, context)
    dimensions = [
        item for item in requirements
        if item["kind"] in {"goal_program", "non_negotiable"}
    ]
    portion = candidate.get("portion_reality") or {}
    dimensions.append({
        "ref": "system:portion_reality",
        "kind": "system_review",
        "statement": str(portion.get("review_question") or "份量要兼顾目标区间、估算误差、食欲、成本和长期执行。"),
    })
    return {
        "problems": _plan_problem_catalog(formulation, context),
        "dimensions": dimensions,
        "advice": [
            {"ref": f"advice:{index}", "statement": str(statement)}
            for index, statement in enumerate(candidate.get("core_advice") or [], 1)
        ],
    }


def _stage_input(run: dict, context: dict, stage: str) -> tuple[dict, dict]:
    receipts = _stage_receipt_map(run["id"])
    state = run.get("stage_state_json") or {}
    formulation = (receipts.get("case_formulation") or {}).get("result_json") or {}
    strategy = (receipts.get("strategy_comparison") or {}).get("result_json") or {}
    candidate = (receipts.get("plan_design") or {}).get("result_json") or {}
    if stage == "facts":
        payload = {
            "context_schema": "FactBundleV1Input",
            "generation_policy": context["generation_policy"],
            "fact_bundle": context["fact_bundle"],
        }
        schema = {"type": "object", "additionalProperties": True}
    elif stage == "intent_learning":
        payload = {
            "context_schema": "IntentLearningV1Input",
            "generation_policy": context["generation_policy"],
            "fact_bundle": context["fact_bundle"],
            "deterministic_dispositions": state.get("deterministic_dispositions") or [],
            "rule": "逐条处理所有用户文字和系统标出的信号；高影响信息只能要求档案确认。",
        }
        schema = intent_learning_schema(context)
    elif stage == "case_formulation":
        payload = {
            "context_schema": "CaseFormulationV1Input",
            "generation_policy": context["generation_policy"],
            "fact_bundle": context["fact_bundle"],
            "intent_learning": (receipts.get("intent_learning") or {}).get("result_json") or {},
            "person": context["person"], "today": context["today"],
            "longitudinal": context["longitudinal"],
            "decision_task": context["decision_task"],
        }
        schema = case_formulation_schema()
    elif stage == "professional_boundary":
        payload = {
            "context_schema": "ProfessionalBoundaryV1Input",
            "generation_policy": context["generation_policy"],
            "goal_contract": context["person"]["goal_contract"],
            "professional_basis": context["professional_basis"],
            "case_formulation": formulation,
        }
        schema = {"type": "object", "additionalProperties": True}
    elif stage == "strategy_comparison":
        requirement_catalog = _planning_requirement_catalog(formulation, context)
        payload = {
            "context_schema": "StrategyComparisonV1Input",
            "generation_policy": context["generation_policy"],
            "case_formulation": formulation,
            "professional_boundary": (receipts.get("professional_boundary") or {}).get("result_json") or {},
            "active_user_model": context["person"]["active_user_model"],
            "immutable_constraints": context["decision_task"]["immutable_constraints"],
            "requirement_catalog": requirement_catalog,
            "goal_contract": context["person"]["goal_contract"],
            "instruction": (
                "比较2–3个现实可行方向，只选择一个；不能把选择负担交还给用户。"
                "用 requirement_ref 说明每项现实要求怎样被处理，不要复制原文凑覆盖。"
            ),
        }
        schema = strategy_comparison_schema()
    elif stage == "plan_design":
        problem_catalog = _plan_problem_catalog(formulation, context)
        evidence_catalog = _evidence_catalog(context)
        payload = {
            "context_schema": "DailyPlanV3Input",
            "generation_policy": context["generation_policy"],
            "case_formulation": formulation, "selected_strategy": strategy,
            "person": context["person"], "today": context["today"],
            "longitudinal": context["longitudinal"],
            "professional_basis": context["professional_basis"],
            "decision_task": context["decision_task"],
            "problem_catalog": problem_catalog,
            "evidence_catalog": [
                {key: item.get(key) for key in ("ref", "kind", "summary", "boundary") if item.get(key) is not None}
                for item in evidence_catalog
            ],
            "compiler_contract": (
                "problem_responses 使用 problem_ref；advice_evidence 使用 source_refs。"
                "系统会把引用编译为真实问题和来源 ID，模型不得填写数据库 ID。"
            ),
        }
        if state.get("review_feedback"):
            payload["independent_review_feedback"] = state["review_feedback"]
            payload["revision_instruction"] = "只修复审查指出的问题，保留已满足需求的部分。"
        schema = daily_plan_v3_schema(context)
    elif stage == "independent_review":
        review_catalog = _review_catalog(formulation, context, candidate)
        payload = {
            "context_schema": "PlanReviewV1Input",
            "generation_policy": context["generation_policy"],
            "case_formulation": formulation,
            "selected_strategy": strategy,
            "candidate_plan": candidate,
            "professional_boundary": (receipts.get("professional_boundary") or {}).get("result_json") or {},
            "evidence_pack": _review_evidence_pack(context),
            "decision_task": context["decision_task"],
            "review_catalog": review_catalog,
            "review_dimensions": [
                "真实需求", "专业与安全", "菜量与量具", "训练和恢复", "食欲睡眠肠胃",
                "预算", "时间与复杂度", "历史纠正", "菜单语义", "人的接受度",
            ],
        }
        schema = plan_review_schema()
    else:
        raise ValidationError("未知 Agent 阶段")
    repair = (state.get("stage_repairs") or {}).get(stage)
    if repair:
        payload["previous_rejection"] = deepcopy(repair)
        payload["repair_instruction"] = "只修复指出的问题；不得用改名、虚构事实或无关改动绕过。"
    return payload, schema


def _validate_intent_result(result: dict, context: dict) -> None:
    bundle = context["fact_bundle"]
    expected_sources = {item["source_id"] for item in bundle.get("natural_language_sources") or []}
    expected_signals = {item["signal_id"] for item in bundle.get("detected_intent_signals") or []}
    actual_sources = [str(item.get("source_id") or "") for item in result.get("source_dispositions") or []]
    actual_signals = [str(item.get("signal_id") or "") for item in result.get("signal_dispositions") or []]
    if set(actual_sources) != expected_sources or len(actual_sources) != len(set(actual_sources)):
        raise ValidationError("意图识别必须逐条处理本次所有用户文字，不能遗漏或重复")
    if set(actual_signals) != expected_signals or len(actual_signals) != len(set(actual_signals)):
        raise ValidationError("意图识别必须逐条处理系统标出的长期、临时或高影响信号")


def _validate_stage_schema(value: object, schema: dict, path: str = "结果") -> None:
    """Small dependency-free validator for the stage contracts emitted by this module."""
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                _validate_stage_schema(value, option, path)
                return
            except ValidationError:
                continue
        raise ValidationError(f"{path}不符合任何允许的结构")
    expected = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)
    if not valid:
        raise ValidationError(f"{path}类型应为 {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{path}不是允许的值")
    if isinstance(value, dict):
        missing = [key for key in schema.get("required") or [] if key not in value]
        if missing:
            raise ValidationError(f"{path}缺少字段：{'、'.join(missing)}")
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ValidationError(f"{path}包含未允许字段：{'、'.join(extras)}")
        for key, child in properties.items():
            if key in value:
                _validate_stage_schema(value[key], child, f"{path}.{key}")
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems") or 0):
            raise ValidationError(f"{path}项目数量不足")
        if schema.get("maxItems") is not None and len(value) > int(schema["maxItems"]):
            raise ValidationError(f"{path}项目数量过多")
        child = schema.get("items")
        if isinstance(child, dict):
            for index, item in enumerate(value):
                _validate_stage_schema(item, child, f"{path}[{index}]")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if schema.get("minimum") is not None and value < schema["minimum"]:
            raise ValidationError(f"{path}小于允许下界")
        if schema.get("maximum") is not None and value > schema["maximum"]:
            raise ValidationError(f"{path}超过允许上界")


def _compile_strategy_result(result: dict, context: dict, formulation: dict) -> dict:
    options = result.get("options")
    if not isinstance(options, list) or not 2 <= len(options) <= 3:
        raise ValidationError("策略比较必须提供 2–3 个现实可行方向")
    ids = [str(item.get("id") or "") for item in options if isinstance(item, dict)]
    if not all(ids) or len(ids) != len(set(ids)) or result.get("selected_id") not in ids:
        raise ValidationError("策略比较必须明确选择一个有效方向")
    compiled = deepcopy(result)
    catalog = _planning_requirement_catalog(formulation, context)
    by_ref = {item["ref"]: item for item in catalog}
    if not by_ref:
        raise ValidationError("系统没有形成可供策略比较的真实要求")
    for option in compiled["options"]:
        rows = option.get("coverage") or []
        refs = [str(item.get("requirement_ref") or "") for item in rows if isinstance(item, dict)]
        if len(refs) != len(set(refs)) or any(ref not in by_ref for ref in refs):
            raise ValidationError("策略覆盖引用了未知或重复的现实要求")
        option["solves_priorities"] = [by_ref[ref]["statement"] for ref in refs]
    selected = next(
        item for item in compiled["options"] if item.get("id") == result.get("selected_id")
    )
    selected_rows = selected.get("coverage") or []
    covered = {str(item.get("requirement_ref") or "") for item in selected_rows if isinstance(item, dict)}
    missing = sorted(set(by_ref) - covered)
    if missing:
        labels = [by_ref[ref]["statement"] for ref in missing]
        raise ValidationError("所选策略没有说明怎样处理本次真实要求：" + "、".join(labels))
    if any(
        not str(item.get("approach") or "").strip()
        or not str(item.get("tradeoff") or "").strip()
        for item in selected_rows
    ):
        raise ValidationError("所选策略必须说明每项真实要求的做法和代价，不能只复制引用")
    return compiled


def _compile_plan_references(result: dict, context: dict, formulation: dict) -> dict:
    compiled = deepcopy(result)
    problems = {item["ref"]: item for item in _plan_problem_catalog(formulation, context)}
    responses = compiled.get("problem_responses") or []
    response_refs = [str(item.get("problem_ref") or "") for item in responses if isinstance(item, dict)]
    if len(response_refs) != len(set(response_refs)) or any(ref not in problems for ref in response_refs):
        raise ValidationError("计划引用了未知或重复的真实问题")
    missing = sorted(set(problems) - set(response_refs))
    if missing:
        raise ValidationError(
            "计划没有说明怎样回应这些真实问题：" + "、".join(problems[ref]["statement"] for ref in missing)
        )
    if any(not str(item.get("response") or "").strip() for item in responses):
        raise ValidationError("计划必须写清每个真实问题如何落实，不能只复制引用")
    compiled["problems_to_solve"] = [problems[ref]["statement"] for ref in response_refs]

    evidence_catalog = {item["ref"]: item for item in _evidence_catalog(context)}
    evidence_rows = compiled.get("advice_evidence") or []
    for row in evidence_rows:
        refs = [str(item) for item in row.get("source_refs") or []]
        if len(refs) != len(set(refs)) or any(ref not in evidence_catalog for ref in refs):
            raise ValidationError("核心建议引用了本次上下文中不存在的证据")
        expected_kind = str(row.get("basis_kind") or "")
        if not any(evidence_catalog[ref]["kind"] == expected_kind for ref in refs):
            raise ValidationError("核心建议的依据类型与所引用证据不一致")
        row["source_ids"] = [str(evidence_catalog[ref]["source_id"]) for ref in refs]
    return compiled


def _positive_plan_food_texts(result: dict) -> list[str]:
    menu = result.get("tomorrow_menu") or {}
    texts: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
        elif isinstance(value, list):
            for item in value:
                add(item)

    for meal in menu.get("meals") or []:
        add(meal.get("foods") or [])
        add(meal.get("substitutions") or [])
        for item in meal.get("portion_contracts") or []:
            if isinstance(item, dict):
                add(item.get("item"))
        recipe = meal.get("recipe_card") or {}
        for item in recipe.get("ingredients") or []:
            if isinstance(item, dict):
                add(item.get("name") or item.get("item") or item.get("ingredient"))
            else:
                add(item)
        guidance = meal.get("eat_out_guidance") or {}
        if isinstance(guidance, dict):
            for value in guidance.values():
                add(value)
    for collection_name in ("shopping_list", "online_options"):
        for item in menu.get(collection_name) or []:
            if isinstance(item, dict):
                for key in ("name", "item", "ingredient", "product", "search_keywords", "specification"):
                    add(item.get(key))
            else:
                add(item)
    reuse = menu.get("reuse_plan") or {}
    for item in ((reuse.get("items") or []) if isinstance(reuse, dict) else []):
        if isinstance(item, dict):
            add(item.get("ingredient") or item.get("item"))
            add(item.get("later_uses") or [])
    return texts


def _plan_affirmatively_uses_item(result: dict, item: str) -> bool:
    needle = item.casefold()
    negative_markers = ("不", "避免", "无需", "取消", "别", "排除", "跳过", "不用")
    for text in _positive_plan_food_texts(result):
        value = text.casefold()
        cursor = 0
        while True:
            index = value.find(needle, cursor)
            if index < 0:
                break
            prefix = value[max(0, index - 12):index]
            suffix = value[index + len(needle):index + len(needle) + 6]
            explanatory_tail = value[index + len(needle):index + len(needle) + 24]
            repeated_as_negative = (
                needle in explanatory_tail
                and any(marker in explanatory_tail for marker in negative_markers)
            )
            if (
                not any(marker in prefix for marker in negative_markers)
                and "除外" not in suffix
                and not repeated_as_negative
            ):
                return True
            cursor = index + len(needle)
    return False


def _validate_plan_result(result: dict, context: dict, formulation: dict, strategy: dict) -> dict:
    result = _compile_plan_references(result, context, formulation)
    selected = str(strategy.get("selected_id") or "")
    selected_option = next(
        (item for item in strategy.get("options") or [] if item.get("id") == selected), {}
    )
    if result.get("selected_strategy") not in {selected, selected_option.get("label")}:
        raise ValidationError("计划没有采用策略比较阶段选中的方向")
    advice = [str(item).strip() for item in result.get("core_advice") or [] if str(item).strip()]
    evidence_rows = result.get("advice_evidence") or []
    evidence_by_advice = {
        str(item.get("advice") or "").strip(): item
        for item in evidence_rows if isinstance(item, dict) and str(item.get("advice") or "").strip()
    }
    if len(evidence_by_advice) != len(evidence_rows) or set(advice) != set(evidence_by_advice):
        raise ValidationError("每条核心建议都必须逐条绑定本次事实或适用的专业原则")
    professional_ids = {
        str(item.get("id") or "") for item in context["professional_basis"].get("principles") or []
    }
    user_context_ids = {
        str(item.get("source_id") or "") for item in context["fact_bundle"].get("facts") or []
    }
    user_context_ids.update(
        str(item.get("id") or "") for item in context["person"].get("active_user_model") or []
    )
    user_context_ids.add(str(context["person"]["goal_contract"].get("contract_id") or ""))
    allowed_source_ids = (professional_ids | user_context_ids) - {""}
    for row in evidence_rows:
        source_ids = {str(item) for item in row.get("source_ids") or []}
        if not source_ids or not source_ids.issubset(allowed_source_ids):
            raise ValidationError("核心建议引用了本次上下文中不存在的证据")
        if row.get("basis_kind") == "professional_principle" and not source_ids.intersection(professional_ids):
            raise ValidationError("一般专业建议必须引用本次适用的专业原则")
        if row.get("basis_kind") == "user_context" and not source_ids.intersection(user_context_ids):
            raise ValidationError("个案建议必须引用本次真实用户上下文")
    for claim in context["person"].get("active_user_model") or []:
        scope = claim.get("scope") or {}
        if claim.get("type") in {"resource_constraint", "stable_preference", "temporary_state"}:
            item = str(scope.get("item") or "").strip()
            if (
                item and item not in {"日常食材", "这类食物"}
                and _plan_affirmatively_uses_item(result, item)
            ):
                raise ValidationError(
                    f"用户已明确要求 {item} 不作为默认项；当前计划仍在默认安排它"
                )
    result["portion_reality"] = _validate_portions(result, context)
    from . import service

    return service.validate_daily_review_candidate(context["today"]["date"], result)


def _compile_review_result(
    result: dict, formulation: dict, context: dict, candidate: dict | None = None
) -> dict:
    candidate = candidate or {}
    catalog = _review_catalog(formulation, context, candidate)
    compiled = deepcopy(result)
    mappings = (
        ("problem_coverage", "problem_ref", "problem", "problems"),
        ("dimension_coverage", "dimension_ref", "dimension", "dimensions"),
        ("evidence_checks", "advice_ref", "claim", "advice"),
    )
    for collection, ref_key, text_key, catalog_key in mappings:
        by_ref = {item["ref"]: item["statement"] for item in catalog[catalog_key]}
        rows = compiled.get(collection) or []
        refs = [str(item.get(ref_key) or "") for item in rows if isinstance(item, dict)]
        if len(refs) != len(set(refs)) or any(ref not in by_ref for ref in refs):
            raise ValidationError("独立审查引用了未知或重复的审查项")
        for row in rows:
            row[text_key] = by_ref[str(row[ref_key])]
    return compiled


def _validate_review_result(
    result: dict, formulation: dict, context: dict, candidate: dict | None = None
) -> dict:
    result = _compile_review_result(result, formulation, context, candidate)
    catalog = _review_catalog(formulation, context, candidate or {})
    expected_problem_refs = {item["ref"] for item in catalog["problems"]}
    coverage = result.get("problem_coverage") or []
    covered_problem_refs = {
        str(item.get("problem_ref") or "") for item in coverage if item.get("addressed") is True
    }
    if expected_problem_refs - covered_problem_refs:
        raise ValidationError("独立审查没有确认计划逐项解决个案问题与执行学习")
    if any(
        item.get("addressed") is True and not str(item.get("evidence") or "").strip()
        for item in coverage
    ):
        raise ValidationError("独立审查必须说明计划在哪里实际回应了问题")
    required_dimension_refs = {item["ref"] for item in catalog["dimensions"]}
    dimension_coverage = result.get("dimension_coverage") or []
    covered_dimension_refs = {
        str(item.get("dimension_ref") or "")
        for item in dimension_coverage if item.get("addressed") is True
    }
    if required_dimension_refs - covered_dimension_refs:
        raise ValidationError("独立审查没有确认计划覆盖目标边界和现实份量")
    if any(
        item.get("addressed") is True and not str(item.get("evidence") or "").strip()
        for item in dimension_coverage
    ):
        raise ValidationError("独立审查必须说明目标边界和现实份量如何落实")
    expected_advice_refs = {item["ref"] for item in catalog["advice"]}
    evidence_checks = result.get("evidence_checks") or []
    checked_advice_refs = {
        str(item.get("advice_ref") or "")
        for item in evidence_checks if item.get("supported") is True
    }
    if expected_advice_refs - checked_advice_refs:
        raise ValidationError("独立审查没有逐条核对核心建议的事实或专业依据")
    if any(
        item.get("supported") is True and not str(item.get("source_or_boundary") or "").strip()
        for item in evidence_checks
    ):
        raise ValidationError("独立审查必须说明核心建议由什么事实或专业边界支持")
    if any(item.get("supported") is not True for item in evidence_checks):
        if result.get("approved") is True:
            raise ValidationError("存在无证据建议时，独立审查不能批准计划")
    if any(not str(item.get("user_harm") or "").strip() for item in result.get("issues") or []):
        raise ValidationError("每个审查问题都必须说明它避免的真实用户伤害")
    blocking = [item for item in result.get("issues") or [] if item.get("severity") in {"block", "repair"}]
    if blocking and result.get("approved") is True:
        raise ValidationError("独立审查仍有必须修复的问题，不能批准计划")
    return result


def _finding_for_error(stage: str, exc: Exception) -> dict:
    message = str(exc)
    block_tokens = ("安全", "过敏", "疾病", "孕", "药物", "历史", "已锁定", "确认")
    severity = "block" if any(token in message for token in block_tokens) else "repair"
    return intelligence.validation_finding(
        severity, "stage_validation_failed", message, stage=stage,
        user_harm=(
            "防止越过用户确认或安全边界发布计划。" if severity == "block"
            else "防止把遗漏、矛盾或结构迁就现实的问题交给用户执行。"
        ),
        repair_instruction="只修复指出的问题后重新提交当前阶段。" if severity == "repair" else "需要先满足安全或确认边界。",
    )


def begin_agent_run(
    review_date: str,
    *,
    provider: str = "external_agent",
    model: str = "unspecified",
    force: bool = False,
) -> dict:
    _validate_date(review_date)
    initial = build_agent_context(review_date)
    deterministic = _apply_detected_signals(
        initial.get("fact_bundle", {}).get("detected_intent_signals") or []
    )
    context = build_agent_context(review_date)
    timestamp = _now()
    run_id = new_id("agent_run")
    state = {"deterministic_dispositions": deterministic, "protocol_version": AGENT_RUN_PROTOCOL_VERSION}
    with connect() as conn:
        active = conn.execute(
            """SELECT id FROM agent_planning_runs WHERE review_date=? AND status IN (
                   'formulating','planning','reviewing','needs_clarification','ready_draft'
               ) ORDER BY started_at DESC LIMIT 1""",
            (review_date,),
        ).fetchone()
        if active and not force:
            existing_draft = get_draft(review_date)
            if existing_draft and existing_draft.get("run_id") == active["id"]:
                return agent_run_status(active["id"])
        if active:
            conn.execute(
                """UPDATE agent_planning_runs SET status='interrupted',error_summary=?,updated_at=?,
                       completed_at=COALESCE(completed_at,?) WHERE id=?""",
                ("由新的完整 Agent 运行替代", timestamp, timestamp, active["id"]),
            )
        conn.execute(
            """INSERT INTO agent_planning_runs(
                   id,review_date,status,provider,model,context_hash,source_manifest_json,
                   current_stage,stage_cursor,revision_count,stage_state_json,started_at,updated_at
               ) VALUES(?,?,'formulating',?,?,?,?, 'facts',0,0,?,?,?)""",
            (
                run_id, review_date, provider, model, context["context_hash"],
                json.dumps(context["source_manifest"], ensure_ascii=False, sort_keys=True),
                json.dumps(state, ensure_ascii=False, sort_keys=True), timestamp, timestamp,
            ),
        )
    _create_or_replace_draft(run_id, review_date, "formulating", context)
    return next_agent_stage(run_id)


def next_agent_stage(run_id: str) -> dict:
    run = _agent_run_row(run_id)
    if run["status"] in {"failed", "interrupted", "stale", "accepted", "active", "completed"}:
        return agent_run_status(run_id)
    if run["status"] == "needs_clarification":
        return agent_run_status(run_id)
    context = _assert_run_context_current(run)
    cursor = int(run.get("stage_cursor") or 0)
    while cursor < len(AGENT_RUN_STAGES):
        stage = AGENT_RUN_STAGES[cursor]
        stage_input, schema = _stage_input(run, context, stage)
        if stage not in DETERMINISTIC_STAGES:
            return {
                **agent_run_status(run_id), "stage": stage,
                "stage_context": stage_input, "stage_schema": schema,
            }
        result = context["fact_bundle"] if stage == "facts" else context["professional_basis"]["planning_envelope"]
        _save_stage_receipt(
            run_id, stage, stage_input, schema, status="completed", result=result, submitted_by="system"
        )
        cursor += 1
        _set_run_cursor(run_id, cursor, status=_run_status_label(
            AGENT_RUN_STAGES[cursor] if cursor < len(AGENT_RUN_STAGES) else "independent_review"
        ))
        run = _agent_run_row(run_id)
    return {**agent_run_status(run_id), "ready_to_finalize": True, "stage": "finalize"}


def submit_agent_stage(
    run_id: str,
    stage: str,
    result: dict,
    *,
    submitted_by: str = "external_agent",
) -> dict:
    run = _agent_run_row(run_id)
    if run["status"] not in {"formulating", "planning", "reviewing"}:
        raise ValidationError("当前 Agent 运行不接受阶段提交")
    cursor = int(run.get("stage_cursor") or 0)
    expected = AGENT_RUN_STAGES[cursor] if cursor < len(AGENT_RUN_STAGES) else "finalize"
    if stage != expected or stage in DETERMINISTIC_STAGES:
        raise ValidationError(f"当前必须提交阶段 {expected}，不能跳过或调换顺序")
    context = _assert_run_context_current(run)
    stage_input, schema = _stage_input(run, context, stage)
    findings: list[dict] = []
    try:
        if not isinstance(result, dict):
            raise ValidationError("阶段结果必须是 JSON 对象")
        _validate_stage_schema(result, schema)
        receipts = _stage_receipt_map(run_id)
        formulation = (receipts.get("case_formulation") or {}).get("result_json") or {}
        if stage == "intent_learning":
            _validate_intent_result(result, context)
        elif stage == "case_formulation":
            result = _validate_formulation(result, int(context["decision_task"].get("question_budget") or 0))
            _apply_intake_classifications(run["review_date"], result, context)
        elif stage == "strategy_comparison":
            result = _compile_strategy_result(result, context, formulation)
        elif stage == "plan_design":
            strategy = (receipts.get("strategy_comparison") or {}).get("result_json") or {}
            result = _validate_plan_result(result, context, formulation, strategy)
        elif stage == "independent_review":
            candidate = (receipts.get("plan_design") or {}).get("result_json") or {}
            result = _validate_review_result(result, formulation, context, candidate)
    except Exception as exc:
        findings.append(_finding_for_error(stage, exc))
        _save_stage_receipt(
            run_id, stage, stage_input, schema, status="rejected", result=result,
            findings=findings, submitted_by=submitted_by,
        )
        state = run.get("stage_state_json") or {}
        repairs = dict(state.get("stage_repairs") or {})
        repairs[stage] = findings
        state["stage_repairs"] = repairs
        _set_run_cursor(run_id, cursor, status=run["status"], state=state)
        raise
    _save_stage_receipt(
        run_id, stage, stage_input, schema, status="completed", result=result,
        findings=findings, submitted_by=submitted_by,
    )
    if stage == "case_formulation" and result.get("clarification_questions"):
        saved = _save_clarifications(run_id, run["review_date"], result["clarification_questions"])
        _update_run(
            run_id, "needs_clarification", formulation=result, clarifications=saved,
        )
        with connect() as conn:
            conn.execute(
                "UPDATE agent_planning_runs SET current_stage='clarification',updated_at=? WHERE id=?",
                (_now(), run_id),
            )
        _create_or_replace_draft(
            run_id, run["review_date"], "needs_clarification", context, formulation=result
        )
        return agent_run_status(run_id)
    if stage == "independent_review" and result.get("approved") is not True:
        state = run.get("stage_state_json") or {}
        if int(run.get("revision_count") or 0) >= 1:
            _update_run(run_id, "failed", review=result, error="独立审查修订后仍未通过", completed=True)
            _create_or_replace_draft(
                run_id, run["review_date"], "failed", context,
                formulation=formulation, review=result, stale_reason="独立审查修订后仍未通过",
            )
            return agent_run_status(run_id)
        state["review_feedback"] = result.get("issues") or []
        state.setdefault("review_history", []).append(result)
        with connect() as conn:
            conn.execute(
                "UPDATE agent_stage_receipts SET status='superseded',updated_at=? WHERE run_id=? AND stage_key='plan_design'",
                (_now(), run_id),
            )
        _set_run_cursor(
            run_id, AGENT_RUN_STAGES.index("plan_design"), status="planning",
            state=state, revision_count=1,
        )
        return next_agent_stage(run_id)
    next_cursor = cursor + 1
    if stage == "case_formulation":
        _update_run(run_id, "formulating", formulation=result)
    elif stage == "independent_review":
        _update_run(run_id, "reviewing", review=result)
    _set_run_cursor(
        run_id, next_cursor,
        status=_run_status_label(AGENT_RUN_STAGES[next_cursor]) if next_cursor < len(AGENT_RUN_STAGES) else "reviewing",
    )
    return next_agent_stage(run_id)


def agent_run_status(run_id: str) -> dict:
    run = _agent_run_row(run_id)
    receipts = list(_stage_receipt_map(run_id).values())
    return {
        "run_id": run_id, "review_date": run["review_date"], "status": run["status"],
        "current_stage": run.get("current_stage"), "stage_cursor": run.get("stage_cursor"),
        "revision_count": run.get("revision_count"), "context_hash": run["context_hash"],
        "error_summary": run.get("error_summary") or "", "receipts": receipts,
        "stages_completed": [item["stage_key"] for item in receipts if item["status"] == "completed"],
    }


def finalize_agent_run(run_id: str) -> dict:
    run = _agent_run_row(run_id)
    if int(run.get("stage_cursor") or 0) < len(AGENT_RUN_STAGES):
        raise ValidationError("Agent 运行尚未完成全部必需阶段")
    context = _assert_run_context_current(run)
    receipts = _stage_receipt_map(run_id)
    missing = [stage for stage in AGENT_RUN_STAGES if (receipts.get(stage) or {}).get("status") != "completed"]
    if missing:
        raise ValidationError("Agent 运行缺少已完成阶段：" + "、".join(missing))
    review = receipts["independent_review"]["result_json"]
    if review.get("approved") is not True:
        raise ValidationError("独立审查尚未批准计划")
    formulation = receipts["case_formulation"]["result_json"]
    strategy = receipts["strategy_comparison"]["result_json"]
    candidate = deepcopy(receipts["plan_design"]["result_json"])
    stage_manifest = _agent_stage_manifest(run_id)
    manifest = deepcopy(context["source_manifest"])
    manifest.update({
        "agent_run_id": run_id, "agent_run_protocol_version": AGENT_RUN_PROTOCOL_VERSION,
        "agent_stage_receipts": stage_manifest,
    })
    candidate["agent_workbench"] = {
        "case_formulation_version": CASE_FORMULATION_VERSION,
        "case_summary": formulation,
        "selected_strategy": strategy,
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
    context = deepcopy(context)
    context["source_manifest"] = manifest
    draft = _create_or_replace_draft(
        run_id, run["review_date"], "ready_draft", context,
        formulation=formulation, result=candidate, review=review,
    )
    _update_run(run_id, "ready_draft", formulation=formulation, review=review, completed=True)
    return draft


def _agent_stage_manifest(run_id: str) -> list[dict]:
    receipts = _stage_receipt_map(run_id)
    return [
        {
            "stage": stage,
            "receipt_id": receipts[stage]["id"],
            "input_hash": receipts[stage]["input_hash"],
            "result_hash": _hash(receipts[stage].get("result_json") or {}),
        }
        for stage in AGENT_RUN_STAGES
        if (receipts.get(stage) or {}).get("status") == "completed"
    ]


def assert_agent_run_publishable(run_id: str, review_date: str, result: dict | None = None) -> dict:
    run = _agent_run_row(run_id)
    if run["review_date"] != review_date or run["status"] != "ready_draft":
        raise ValidationError("只能发布已完成全部阶段且仍然有效的草案")
    draft = get_draft(review_date)
    if not draft or draft.get("run_id") != run_id or draft.get("status") != "ready_draft":
        raise ValidationError("Agent 运行没有可发布的当前草案")
    _assert_run_context_current(run)
    if result is not None and _hash(result) != _hash(draft.get("result_json") or {}):
        raise ValidationError("提交结果与该 Agent 运行审查通过的草案不一致")
    return draft


def accept_agent_run(run_id: str) -> dict:
    run = _agent_run_row(run_id)
    assert_agent_run_publishable(run_id, run["review_date"])
    return accept_draft(run["review_date"])


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
    started = begin_agent_run(review_date, provider=provider_name, model=model, force=force)
    run_id = started["run_id"]
    attempts: list[dict] = []
    validation_retries: dict[str, int] = {}
    try:
        state = started
        while state.get("status") not in {"needs_clarification", "failed", "interrupted"}:
            if state.get("ready_to_finalize") or state.get("stage") == "finalize":
                _update_run(run_id, "reviewing", attempts=attempts)
                return finalize_agent_run(run_id)
            stage = state.get("stage") or state.get("current_stage")
            if stage not in AGENT_RUN_STAGES:
                state = next_agent_stage(run_id)
                continue
            if stage in {"intent_learning", "case_formulation"}:
                provider = case_provider
            elif stage in {"strategy_comparison", "plan_design"}:
                provider = plan_provider
            else:
                provider = review_provider
            kind = {
                "intent_learning": "intent_learning",
                "case_formulation": "case_formulation",
                "strategy_comparison": "strategy_comparison",
                "plan_design": (
                    "daily_plan_v3_revision"
                    if int(state.get("revision_count") or 0) else "daily_plan_v3"
                ),
                "independent_review": "plan_review",
            }[stage]
            generated = _stage_generate(
                provider, kind, state["stage_context"], state["stage_schema"], attempts
            )
            try:
                state = submit_agent_stage(run_id, stage, generated, submitted_by="built_in_model")
            except ValidationError:
                receipt = _stage_receipt_map(run_id).get(stage) or {}
                findings = receipt.get("findings_json") or []
                can_repair = any(item.get("severity") == "repair" for item in findings)
                used = validation_retries.get(stage, 0)
                if not can_repair or used >= 1:
                    raise
                validation_retries[stage] = used + 1
                run = _agent_run_row(run_id)
                run_state = run.get("stage_state_json") or {}
                repairs = dict(run_state.get("stage_repairs") or {})
                repairs[stage] = findings
                run_state["stage_repairs"] = repairs
                _set_run_cursor(
                    run_id, int(run.get("stage_cursor") or 0),
                    status=run.get("status") or _run_status_label(stage), state=run_state,
                )
                state = next_agent_stage(run_id)
                continue
            _update_run(run_id, state["status"], attempts=attempts)
        draft = get_draft(review_date)
        return draft or state
    except Exception as exc:
        current = build_agent_context(review_date)
        if current["context_hash"] != _agent_run_row(run_id)["context_hash"]:
            _update_run(run_id, "interrupted", attempts=attempts, error=str(exc), completed=True)
            return _create_or_replace_draft(
                run_id, review_date, "stale", context,
                stale_reason="规划期间新增了记录或状态，草案没有发布",
            )
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


def update_intake(record_id: str, review_date: str, text: str) -> dict:
    _validate_date(review_date)
    clean = str(text or "").strip()
    if not clean:
        raise ValidationError("记录内容不能为空")
    if len(clean) > 4000:
        raise ValidationError("单次补充不能超过 4000 字")
    from . import service

    existing = next(
        (item for item in service.list_daily_records(review_date) if item["id"] == record_id),
        None,
    )
    if existing is None:
        raise KeyError(record_id)
    if existing["raw_input"] == clean:
        return {"record": existing, "scheduled": False}
    record = service.update_daily_record(record_id, review_date, clean)
    with connect() as conn:
        events = conn.execute(
            "SELECT id,classification_json FROM agent_workspace_events WHERE event_date=?",
            (review_date,),
        ).fetchall()
        for event in events:
            classification = json.loads(event["classification_json"] or "{}")
            if classification.get("record_id") != record_id:
                continue
            conn.execute(
                """UPDATE agent_workspace_events
                   SET input_text=?,event_kind='unclassified',classification_json=?,affects_plan=1
                   WHERE id=?""",
                (clean, json.dumps({"record_id": record_id}, ensure_ascii=False), event["id"]),
            )
    mark_draft_stale(review_date, "修改了今天的真实情况")
    schedule_auto_draft(review_date)
    return {"record": record, "scheduled": True}


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
    timestamp = _now()
    event_id = new_id("agent_event")
    with connect() as conn:
        conn.execute(
            """INSERT INTO agent_workspace_events(
                   id,event_date,input_text,event_kind,classification_json,affects_plan,created_at
               ) VALUES(?,?,?,'plan_edit',?,1,?)""",
            (
                event_id, review_date, clean,
                json.dumps({"requested_at_draft_version": draft["version"]}, ensure_ascii=False),
                timestamp,
            ),
        )
    process_natural_language_input(event_id, clean, review_date, source_type="plan_edit")
    mark_draft_stale(review_date, "正在根据你的修改重新检查安排")
    schedule_auto_draft(review_date)
    working_draft = get_draft(review_date)
    if not working_draft:
        raise ValidationError("草案状态已经变化，请刷新后重试")
    context = build_agent_context(review_date)
    plan_provider = client or ai.provider_for_stage("plan")
    review_provider = client or ai.provider_for_stage("review")
    base_revision_context = {
        "context_schema": "TargetedPlanRevisionV1Input",
        "generation_policy": context["generation_policy"],
        "user_instruction": clean,
        "case_formulation": draft["formulation_json"],
        "current_draft": draft["result_json"],
        "active_user_model": context["person"]["active_user_model"],
        "immutable_constraints": context["decision_task"]["immutable_constraints"],
        "instruction": "只重算受影响餐次、全天平衡、购物和食材承接；未受影响餐次必须逐字段保持。",
    }
    attempts: list[dict] = []
    before = draft["result_json"]
    receipts = _stage_receipt_map(draft["run_id"])
    formulation = (receipts.get("case_formulation") or {}).get("result_json") or draft["formulation_json"]
    strategy = (receipts.get("strategy_comparison") or {}).get("result_json") or {}
    professional_boundary = (receipts.get("professional_boundary") or {}).get("result_json") or {}
    output: dict = {}
    after: dict = {}
    affected: set[str] = set()
    review: dict = {}
    revision_context = deepcopy(base_revision_context)
    review_context: dict = {}
    revision_schema = targeted_revision_schema(context)
    review_schema = plan_review_schema()

    for review_attempt in range(2):
        output = _stage_generate(
            plan_provider, "targeted_plan_revision", revision_context, revision_schema, attempts
        )
        affected = set(output.get("affected_meals") or [])
        if not affected:
            raise ValidationError("局部修改没有指出受影响餐次")
        after = output.get("updated_result")
        if not isinstance(after, dict):
            raise ValidationError("局部修改没有返回完整可验证草案")
        after = deepcopy(after)
        after.pop("agent_workbench", None)
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
        after = _validate_plan_result(after, context, formulation, strategy)
        review_context = {
            "context_schema": "PlanReviewV1Input",
            "generation_policy": context["generation_policy"],
            "case_formulation": formulation,
            "selected_strategy": strategy,
            "candidate_plan": after,
            "professional_boundary": professional_boundary,
            "evidence_pack": _review_evidence_pack(context),
            "decision_task": context["decision_task"],
            "review_catalog": _review_catalog(formulation, context, after),
            "review_dimensions": [
                "真实需求", "专业与安全", "菜量与量具", "训练和恢复", "食欲睡眠肠胃",
                "预算", "时间与复杂度", "历史纠正", "菜单语义", "人的接受度",
            ],
            "revision_instruction": clean,
        }
        review = _stage_generate(review_provider, "plan_review", review_context, review_schema, attempts)
        _validate_stage_schema(review, review_schema)
        review = _validate_review_result(review, formulation, context, after)
        if review.get("approved") is True:
            break
        if review_attempt == 1:
            raise ValidationError("局部修改两次仍未通过独立审查，原草案保持不变")
        revision_context = deepcopy(base_revision_context)
        revision_context["current_draft"] = after
        revision_context["independent_review_feedback"] = review.get("issues") or []
        revision_context["instruction"] = "根据独立审查只修复指出的问题；未受影响餐次必须逐字段保持。"

    _save_stage_receipt(
        draft["run_id"], "plan_design", revision_context, revision_schema,
        status="completed", result=after, submitted_by="user_targeted_revision",
    )
    _save_stage_receipt(
        draft["run_id"], "independent_review", review_context, review_schema,
        status="completed", result=review, submitted_by="independent_review",
    )
    manifest = deepcopy(context["source_manifest"])
    manifest.update({
        "agent_run_id": draft["run_id"],
        "agent_run_protocol_version": AGENT_RUN_PROTOCOL_VERSION,
        "agent_stage_receipts": _agent_stage_manifest(draft["run_id"]),
    })
    after["agent_workbench"] = deepcopy(before.get("agent_workbench") or {})
    after["agent_workbench"]["review_summary"] = review
    after["agent_workbench"]["context_hash"] = context["context_hash"]
    after["agent_workbench"]["last_local_revision"] = {
        "instruction": clean, "affected_meals": sorted(affected),
        "change_summary": output.get("change_summary") or [], "revised_at": _now(),
    }
    timestamp = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT version,status FROM agent_drafts WHERE review_date=?", (review_date,)).fetchone()
        if not current or current["status"] != "stale" or current["version"] != working_draft["version"]:
            raise ValidationError("草案已经变化，请刷新后重试")
        conn.execute(
            """UPDATE agent_drafts SET status='ready_draft',result_json=?,review_json=?,
                   context_hash=?,source_manifest_json=?,stale_reason='',updated_at=?
               WHERE review_date=? AND version=?""",
            (
                json.dumps(after, ensure_ascii=False),
                json.dumps({
                    "targeted_revision": output, "independent_review": review, "attempts": attempts,
                }, ensure_ascii=False),
                context["context_hash"],
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                timestamp, review_date, working_draft["version"],
            ),
        )
        conn.execute(
            """UPDATE agent_workspace_events SET classification_json=? WHERE id=?""",
            (json.dumps({
                "affected_meals": sorted(affected), "draft_version": working_draft["version"],
            }, ensure_ascii=False), event_id),
        )
        conn.execute(
            """UPDATE agent_planning_runs SET status='ready_draft',context_hash=?,review_json=?,
                   source_manifest_json=?,updated_at=?
               WHERE id=?""",
            (
                context["context_hash"],
                json.dumps(review, ensure_ascii=False),
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                timestamp, draft["run_id"],
            ),
        )
        updated = row_dict(conn.execute("SELECT * FROM agent_drafts WHERE review_date=?", (review_date,)).fetchone())
    return updated


def supersede_source_evidence(evidence_type: str, evidence_id: str, *, reason: str) -> list[str]:
    """Keep prior evidence traceable while removing it from the current user-model projection."""
    clean_type = str(evidence_type or "").strip()
    clean_id = str(evidence_id or "").strip()
    if not clean_type or not clean_id:
        return []
    timestamp = _now()
    changed: list[str] = []
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        claim_ids = [row["claim_id"] for row in conn.execute(
            """SELECT DISTINCT claim_id FROM user_model_evidence
               WHERE evidence_type=? AND evidence_id=? AND active=1""",
            (clean_type, clean_id),
        ).fetchall()]
        if not claim_ids:
            return []
        conn.execute(
            """UPDATE user_model_evidence SET active=0
               WHERE evidence_type=? AND evidence_id=? AND active=1""",
            (clean_type, clean_id),
        )
        for claim_id in claim_ids:
            row = row_dict(conn.execute(
                "SELECT * FROM user_model_claims WHERE id=?", (claim_id,)
            ).fetchone())
            if not row:
                continue
            support_count = conn.execute(
                """SELECT COUNT(DISTINCT evidence_type || ':' || evidence_id)
                   FROM user_model_evidence
                   WHERE claim_id=? AND stance='support' AND active=1""",
                (claim_id,),
            ).fetchone()[0]
            actionable_support_count = conn.execute(
                """SELECT COUNT(DISTINCT evidence_type || ':' || evidence_id)
                   FROM user_model_evidence
                   WHERE claim_id=? AND stance='support' AND active=1
                     AND evidence_type!='agent_hypothesis'""",
                (claim_id,),
            ).fetchone()[0]
            counter_count = conn.execute(
                """SELECT COUNT(DISTINCT evidence_type || ':' || evidence_id)
                   FROM user_model_evidence
                   WHERE claim_id=? AND stance='counterexample' AND active=1""",
                (claim_id,),
            ).fetchone()[0]
            explicit_support = conn.execute(
                """SELECT 1 FROM user_model_evidence
                   WHERE claim_id=? AND stance='support' AND explicit=1 AND active=1 LIMIT 1""",
                (claim_id,),
            ).fetchone() is not None
            status = row["status"]
            if status not in {"paused", "forgotten"}:
                status = (
                    "active"
                    if row["risk_level"] == "low" and (explicit_support or actionable_support_count >= 2)
                    else "pending_confirmation"
                )
                if counter_count and counter_count >= support_count:
                    status = "refuted"
            confidence = min(0.92, max(0.1, 0.35 + support_count * 0.2 - counter_count * 0.25))
            conn.execute(
                """UPDATE user_model_claims SET status=?,confidence=?,support_count=?,counter_count=?,
                       version=version+1,updated_at=? WHERE id=?""",
                (status, confidence, support_count, counter_count, timestamp, claim_id),
            )
            updated = row_dict(conn.execute(
                "SELECT * FROM user_model_claims WHERE id=?", (claim_id,)
            ).fetchone())
            _append_claim_version(conn, updated, f"source_superseded:{reason[:120]}", timestamp)
            changed.append(claim_id)
        _refresh_user_model_projection(conn, timestamp)
    mark_all_drafts_stale("用户修改了学习证据来源")
    return changed


def accept_draft(review_date: str) -> dict:
    draft = get_draft(review_date)
    if not draft or draft["status"] != "ready_draft" or not draft.get("result_json"):
        raise ValidationError("当前没有可接受的最新草案")
    assert_agent_run_publishable(draft["run_id"], review_date)
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
            "model": run["model"] if run else "mandatory_case_agent",
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
            plan_id = result.get("plan_version_id")
            for claim_id in claim_ids:
                row = conn.execute(
                    "SELECT last_plan_ids_json FROM user_model_claims WHERE id=?", (claim_id,)
                ).fetchone()
                plan_ids = json.loads(row["last_plan_ids_json"] or "[]") if row else []
                if plan_id and plan_id not in plan_ids:
                    plan_ids = [*plan_ids[-9:], plan_id]
                conn.execute(
                    """UPDATE user_model_claims SET last_used_at=?,last_plan_ids_json=?,updated_at=?
                       WHERE id=?""",
                    (timestamp, json.dumps(plan_ids, ensure_ascii=False), timestamp, claim_id),
                )
    receipts = _stage_receipt_map(draft["run_id"])
    allowed_evidence = {
        *context["context_inspector"]["selected_source_ids"].get("records", []),
        *context["context_inspector"]["selected_source_ids"].get("feedback", []),
        *context["context_inspector"]["selected_source_ids"].get("intake", []),
    }
    _learn_candidates(
        ((receipts.get("case_formulation") or {}).get("result_json") or {}).get("soft_assumptions"),
        draft["run_id"], allowed_evidence_ids=allowed_evidence,
    )
    _learn_candidates(
        ((receipts.get("independent_review") or {}).get("result_json") or {}).get("claim_candidates"),
        draft["run_id"], allowed_evidence_ids=allowed_evidence,
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
            claim_type="body_response_hypothesis", claim_dimension="satiety_pattern", statement=statement,
            scope={"meal": feedback.get("meal_name") or "unknown"},
            effect={"portion": portion_effect},
            evidence_type="execution_feedback", evidence_id=feedback["id"], excerpt=actual,
            explicit=bool(actual or satiety), source="execution_feedback",
        )
    if reasons.intersection({"not_enough_time", "too_complex"}):
        return upsert_claim(
            claim_type="friction_hypothesis", claim_dimension="execution_friction",
            statement="这类餐次的主动操作时间或步骤可能超过可接受范围",
            scope={"meal": feedback.get("meal_name") or "unknown"},
            effect={"complexity": "减少持续看火步骤并提供更短备选"},
            evidence_type="execution_feedback", evidence_id=feedback["id"], excerpt=actual,
            explicit=_feedback_requests_durable_change(actual), source="execution_feedback",
        )
    if "did_not_want_it" in reasons:
        return upsert_claim(
            claim_type="soft_need_hypothesis", claim_dimension="temporary_state",
            statement="当日意愿会显著影响这类餐次的执行",
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
            """SELECT COUNT(*) FROM (
                   SELECT plan_date FROM plan_execution_feedback
                   WHERE updated_at>? AND status IN ('followed','modified','skipped')
                   GROUP BY plan_date HAVING COUNT(DISTINCT plan_item_id)>=3
               )""",
            (since,),
        ).fetchone()[0]
        evidence_count = conn.execute(
            "SELECT COUNT(*) FROM plan_execution_feedback WHERE updated_at>?", (since,)
        ).fetchone()[0]
    last_time = datetime.fromisoformat(last["created_at"]) if last else None
    age_days = (datetime.now(timezone.utc) - last_time).days if last_time else None
    due = completed_dates >= 3 or (last_time is not None and age_days >= 7 and evidence_count > 0)
    return {
        "due": due, "last_reflection_at": last["created_at"] if last else None,
        "completed_plan_dates_since": completed_dates, "evidence_events_since": evidence_count,
        "rule": "累计 3 个已完成计划，或距上次反思 7 天且有新执行证据",
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
        return {"eligible": False, "reason": "未配置模型；可以查看和导出本次规划参考，交给模型手动处理"}
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
