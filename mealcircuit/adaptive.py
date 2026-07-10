from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .db import connect, init_db, row_dict
from .personalization import active_personalization
from .validation import ValidationError


MEAL_SLOTS = {"breakfast", "lunch", "dinner", "snack", "unknown"}
EVIDENCE_ROLES = {"consumed", "planned", "inventory", "reference"}
FEEDBACK_STATUSES = {"followed", "modified", "skipped", "not_applicable"}
REASON_CODES = {
    "missing_ingredient",
    "not_enough_time",
    "too_complex",
    "ate_out",
    "did_not_want_it",
    "hunger_mismatch",
    "gut_change",
    "schedule_change",
    "other",
}
INVENTORY_STATUSES = {"available", "used", "not_bought", "discarded", "unknown"}
FRICTION_LABELS = {
    "missing_ingredient": "缺少所需食材",
    "not_enough_time": "可用时间不足",
    "too_complex": "执行步骤过于复杂",
    "ate_out": "临时改为外食",
    "did_not_want_it": "当时不想吃该方案",
    "hunger_mismatch": "份量与实际饥饿不匹配",
    "gut_change": "肠胃状态发生变化",
    "schedule_change": "临时日程变化",
    "other": "存在重复的其他执行阻力",
}
QUESTION_CATALOG = {
    "tomorrow_training": {
        "category": "planning",
        "reason": "明天是否训练会改变主食与餐次安排。",
        "expected_impact": "训练日调整",
        "priority": 60,
    },
    "tomorrow_environment": {
        "category": "planning",
        "reason": "明天的用餐地点会改变菜单可执行方式。",
        "expected_impact": "外食或在家方案",
        "priority": 55,
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "|".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


def _validate_date(value: str, name: str = "日期") -> str:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} 必须是 YYYY-MM-DD") from exc
    return value


def _text(value: object, name: str, *, required: bool = True, maximum: int = 2000) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValidationError(f"{name} 必须是非空文本" if required else f"{name} 必须是文本")
    clean = value.strip()
    if len(clean) > maximum:
        raise ValidationError(f"{name} 不能超过 {maximum} 字")
    return clean


def _string_list(value: object, name: str, allowed: set[str] | None = None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise ValidationError(f"{name} 必须是数组")
    result = []
    for item in value:
        clean = _text(item, name, maximum=100)
        if allowed is not None and clean not in allowed:
            raise ValidationError(f"{name} 包含无效值：{clean}")
        if clean not in result:
            result.append(clean)
    return result


def _queue_changed_date(record_date: str, reason: str) -> None:
    from . import service

    service.queue_review_for_external_change(record_date, reason)


def _scope_snapshot() -> dict:
    current = active_personalization()
    return {
        "profile_version_id": (current.get("profile") or {}).get("id"),
        "strategy_version_id": (current.get("strategy") or {}).get("id"),
        "goal_version_ids": [item["id"] for item in current.get("goals") or []],
        "safety_mode": (current.get("safety") or {}).get("mode", "setup_required"),
        "policy_version": (current.get("safety") or {}).get("policy_version", ""),
    }


def _scope_matches(row: dict, current: dict | None = None) -> bool:
    current = current or _scope_snapshot()
    if row.get("profile_version_id") and row["profile_version_id"] != current["profile_version_id"]:
        return False
    if row.get("strategy_version_id") and row["strategy_version_id"] != current["strategy_version_id"]:
        return False
    if row.get("safety_mode") not in (None, "", "setup_required") and row["safety_mode"] != current["safety_mode"]:
        return False
    bound_goals = set(row.get("goal_version_ids_json") or [])
    if bound_goals and bound_goals != set(current["goal_version_ids"]):
        return False
    if row.get("scope_policy_version") and row["scope_policy_version"] != current["policy_version"]:
        return False
    return True


def link_task_evidence(
    task_id: str,
    observed_date: str,
    role: str,
    meal_slot: str = "unknown",
) -> dict:
    observed_date = _validate_date(observed_date, "证据日期")
    if role not in EVIDENCE_ROLES:
        raise ValidationError("证据角色无效")
    if meal_slot not in MEAL_SLOTS:
        raise ValidationError("餐次无效")
    init_db()
    timestamp = _now()
    link_id = _id("evidence")
    with connect() as conn:
        task = conn.execute("SELECT id,status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise KeyError(task_id)
        conn.execute(
            """INSERT OR IGNORE INTO task_evidence_links(
                   id,task_id,observed_date,meal_slot,role,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (link_id, task_id, observed_date, meal_slot, role, timestamp),
        )
        row = conn.execute(
            """SELECT * FROM task_evidence_links
               WHERE task_id=? AND observed_date=? AND meal_slot=? AND role=?""",
            (task_id, observed_date, meal_slot, role),
        ).fetchone()
    if task["status"] == "completed":
        _queue_changed_date(observed_date, "task_evidence_linked")
    return row_dict(row)


def task_evidence_links(task_id: str) -> list[dict]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM task_evidence_links WHERE task_id=? ORDER BY observed_date,created_at", (task_id,)
        ).fetchall()
    return [row_dict(row) for row in rows]


def linked_dates(task_id: str) -> list[str]:
    return sorted({item["observed_date"] for item in task_evidence_links(task_id)})


def meal_evidence(start_date: str, end_date: str) -> list[dict]:
    _validate_date(start_date, "开始日期")
    _validate_date(end_date, "结束日期")
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """SELECT l.*,t.type,t.status,t.original_input,t.image_path,t.result_json,t.result_version,t.completed_at
               FROM task_evidence_links l JOIN tasks t ON t.id=l.task_id
               WHERE l.observed_date BETWEEN ? AND ?
               ORDER BY l.observed_date,l.created_at""",
            (start_date, end_date),
        ).fetchall()
        result = []
        for row in rows:
            item = row_dict(row)
            item["corrections"] = [row_dict(value) for value in conn.execute(
                "SELECT * FROM task_corrections WHERE task_id=? ORDER BY created_at", (item["task_id"],)
            ).fetchall()]
            result.append(item)
    return result


def _plan_item_id(review: dict, meal: dict) -> str:
    return meal.get("plan_item_id") or _stable_id(
        "plan", review["id"], review["result_version"], meal.get("name", "meal")
    )


def _strategy_key(menu: dict, meal: dict) -> str:
    if meal.get("strategy_key"):
        return str(meal["strategy_key"])
    rotation = menu.get("rotation") or {}
    if meal.get("name") == "晚餐" and rotation.get("dish_key"):
        return str(rotation["dish_key"])
    foods = meal.get("foods") or []
    normalized = "|".join(sorted(str(item).strip().lower() for item in foods))
    return _stable_id("meal", meal.get("name", "meal"), normalized)


def get_plan_for_date(plan_date: str) -> dict | None:
    _validate_date(plan_date, "计划日期")
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_reviews WHERE status='completed' ORDER BY review_date DESC,updated_at DESC"
        ).fetchall()
    for row in rows:
        review = row_dict(row)
        menu = (review.get("result_json") or {}).get("tomorrow_menu") or {}
        if menu.get("date") != plan_date:
            continue
        meals = []
        for meal in menu.get("meals") or []:
            enriched = dict(meal)
            enriched["plan_item_id"] = _plan_item_id(review, meal)
            enriched["strategy_key"] = _strategy_key(menu, meal)
            meals.append(enriched)
        feedback = list_plan_feedback(plan_date, review_id=review["id"], result_version=review["result_version"])
        feedback_by_item = {item["plan_item_id"]: item for item in feedback}
        return {
            "review_id": review["id"],
            "review_date": review["review_date"],
            "result_version": review["result_version"],
            "plan_date": plan_date,
            "menu": {**menu, "meals": meals},
            "feedback": feedback_by_item,
            "source_manifest": review.get("source_manifest_json") or {},
            "safety_mode": review.get("review_mode") or "setup_required",
            "policy_version": review.get("policy_version") or "",
        }
    return None


def save_plan_feedback(
    plan_date: str,
    plan_item_id: str,
    status: str,
    *,
    reason_codes: list[str] | None = None,
    actual_text: str = "",
    outcome: dict | None = None,
    expected_version: int | None = None,
    actor_source: str = "user",
) -> dict:
    if status not in FEEDBACK_STATUSES:
        raise ValidationError("执行状态无效")
    reasons = _string_list(reason_codes, "偏离原因", REASON_CODES)
    if status in {"modified", "skipped"} and not reasons:
        raise ValidationError("调整或未执行时必须选择至少一个原因")
    if status in {"followed", "not_applicable"} and reasons:
        raise ValidationError("按计划执行或不适用时不应提交偏离原因")
    if outcome is None:
        outcome = {}
    if not isinstance(outcome, dict):
        raise ValidationError("执行结果必须是对象")
    actor_source = _text(actor_source, "回执来源", maximum=50)
    plan = get_plan_for_date(plan_date)
    if not plan:
        raise ValidationError("该日期没有已发布计划")
    meal = next((item for item in plan["menu"]["meals"] if item["plan_item_id"] == plan_item_id), None)
    if not meal:
        raise ValidationError("计划项目不存在或已变更")
    timestamp = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT * FROM plan_execution_feedback
               WHERE review_id=? AND result_version=? AND plan_item_id=?""",
            (plan["review_id"], plan["result_version"], plan_item_id),
        ).fetchone()
        if existing:
            if expected_version is None or existing["version"] != expected_version:
                raise ValidationError("执行回执已变化，请刷新后重试")
            conn.execute(
                """UPDATE plan_execution_feedback SET status=?,reason_codes_json=?,actual_text=?,
                       outcome_json=?,version=version+1,updated_at=? WHERE id=? AND version=?""",
                (
                    status,
                    json.dumps(reasons, ensure_ascii=False),
                    _text(actual_text, "实际执行补充", required=False, maximum=2000),
                    json.dumps(outcome, ensure_ascii=False),
                    timestamp,
                    existing["id"],
                    expected_version,
                ),
            )
            feedback_id = existing["id"]
            event_version = existing["version"] + 1
        else:
            if expected_version not in (None, 0):
                raise ValidationError("执行回执尚不存在，版本必须为空")
            feedback_id = _id("feedback")
            event_version = 1
            conn.execute(
                """INSERT INTO plan_execution_feedback(
                       id,review_id,result_version,plan_date,plan_item_id,meal_name,strategy_key,
                       planned_snapshot_json,goal_version_ids_json,profile_version_id,strategy_version_id,
                       safety_mode,scope_policy_version,status,reason_codes_json,actual_text,outcome_json,
                       version,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                (
                    feedback_id,
                    plan["review_id"],
                    plan["result_version"],
                    plan_date,
                    plan_item_id,
                    meal.get("name") or "未命名餐次",
                    meal["strategy_key"],
                    json.dumps(meal, ensure_ascii=False),
                    json.dumps([
                        item["id"] for item in (plan["source_manifest"].get("goals") or [])
                    ], ensure_ascii=False),
                    (plan["source_manifest"].get("profile") or {}).get("id"),
                    (plan["source_manifest"].get("strategy") or {}).get("id"),
                    plan["safety_mode"],
                    plan["policy_version"],
                    status,
                    json.dumps(reasons, ensure_ascii=False),
                    _text(actual_text, "实际执行补充", required=False, maximum=2000),
                    json.dumps(outcome, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
        row = conn.execute("SELECT * FROM plan_execution_feedback WHERE id=?", (feedback_id,)).fetchone()
        event_payload = row_dict(row)
        conn.execute(
            """INSERT INTO plan_execution_feedback_events(
                   id,feedback_id,event_version,payload_json,actor_source,occurred_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                _id("feedback_event"),
                feedback_id,
                event_version,
                json.dumps(event_payload, ensure_ascii=False),
                actor_source,
                timestamp,
            ),
        )
    recompute_candidates(plan_date)
    return row_dict(row)


def plan_feedback_history(feedback_id: str) -> list[dict]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM plan_execution_feedback_events
               WHERE feedback_id=? ORDER BY event_version""",
            (feedback_id,),
        ).fetchall()
    return [row_dict(row) for row in rows]


def list_plan_feedback(
    start_date: str,
    end_date: str | None = None,
    *,
    review_id: str | None = None,
    result_version: int | None = None,
) -> list[dict]:
    _validate_date(start_date, "反馈日期")
    end_date = end_date or start_date
    _validate_date(end_date, "反馈结束日期")
    sql = "SELECT * FROM plan_execution_feedback WHERE plan_date BETWEEN ? AND ?"
    params: list[Any] = [start_date, end_date]
    if review_id:
        sql += " AND review_id=?"
        params.append(review_id)
    if result_version is not None:
        sql += " AND result_version=?"
        params.append(result_version)
    sql += " ORDER BY plan_date,created_at"
    init_db()
    with connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [row_dict(row) for row in rows]


def _candidate_upsert(
    candidate_id: str,
    kind: str,
    statement: str,
    scope: dict,
    summary: dict,
    confidence: str,
    effect: dict,
    supports: list[dict],
    counterexamples: list[dict],
) -> None:
    timestamp = _now()
    scope_snapshot = _scope_snapshot()
    goal_ids = scope_snapshot["goal_version_ids"]
    with connect() as conn:
        conn.execute(
            """INSERT INTO adaptation_candidates(
                   id,kind,statement,scope_json,evidence_summary_json,confidence,status,
                   proposed_effect_json,goal_version_ids_json,profile_version_id,strategy_version_id,
                   safety_mode,scope_policy_version,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   statement=excluded.statement,scope_json=excluded.scope_json,
                   evidence_summary_json=excluded.evidence_summary_json,confidence=excluded.confidence,
                   proposed_effect_json=excluded.proposed_effect_json,
                   goal_version_ids_json=excluded.goal_version_ids_json,
                   profile_version_id=excluded.profile_version_id,
                   strategy_version_id=excluded.strategy_version_id,
                   safety_mode=excluded.safety_mode,
                   scope_policy_version=excluded.scope_policy_version,
                   updated_at=excluded.updated_at""",
            (
                candidate_id,
                kind,
                statement,
                json.dumps(scope, ensure_ascii=False),
                json.dumps(summary, ensure_ascii=False),
                confidence,
                json.dumps(effect, ensure_ascii=False),
                json.dumps(goal_ids, ensure_ascii=False),
                scope_snapshot["profile_version_id"],
                scope_snapshot["strategy_version_id"],
                scope_snapshot["safety_mode"],
                scope_snapshot["policy_version"],
                timestamp,
                timestamp,
            ),
        )
        conn.execute("DELETE FROM adaptation_evidence WHERE candidate_id=?", (candidate_id,))
        for evidence, stance in ((supports, "support"), (counterexamples, "counterexample")):
            for item in evidence:
                conn.execute(
                    """INSERT OR IGNORE INTO adaptation_evidence(
                           id,candidate_id,evidence_type,evidence_id,stance,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (_id("candidate_evidence"), candidate_id, "plan_feedback", item["id"], stance, timestamp),
                )


def _positive_outcome(item: dict) -> bool:
    if item["status"] != "followed":
        return False
    outcome = item.get("outcome_json") or {}
    return bool(outcome.get("would_repeat")) or outcome.get("result") in {"appropriate", "would_repeat"}


def recompute_candidates(as_of: str | None = None) -> list[dict]:
    as_of_date = date.fromisoformat(_validate_date(as_of or date.today().isoformat()))
    cutoff = (as_of_date - timedelta(days=41)).isoformat()
    feedback = list_plan_feedback(cutoff, as_of_date.isoformat())
    scope_snapshot = _scope_snapshot()
    feedback = [item for item in feedback if _scope_matches(item, scope_snapshot)]
    scope_key = "|".join((
        scope_snapshot["profile_version_id"] or "",
        scope_snapshot["strategy_version_id"] or "",
        scope_snapshot["safety_mode"],
        scope_snapshot["policy_version"],
        *scope_snapshot["goal_version_ids"],
    ))

    by_meal: dict[str, list[dict]] = defaultdict(list)
    for item in feedback:
        by_meal[item["meal_name"]].append(item)
    recent_cutoff = as_of_date - timedelta(days=20)
    for meal_name, opportunities_all in by_meal.items():
        opportunities = [item for item in opportunities_all if date.fromisoformat(item["plan_date"]) >= recent_cutoff]
        for reason in REASON_CODES:
            supports = [item for item in opportunities if reason in (item.get("reason_codes_json") or [])]
            if len(supports) < 2:
                continue
            rate = len(supports) / len(opportunities) if opportunities else 0
            if len(opportunities) >= 3 and rate < 0.5:
                continue
            if len(opportunities) < 3:
                confidence = "weak"
            elif len(supports) >= 3 and rate >= 0.75:
                confidence = "strong"
            else:
                confidence = "emerging"
            candidate_id = _stable_id("candidate", "friction", meal_name, reason, scope_key)
            counterexamples = [item for item in opportunities if item not in supports]
            _candidate_upsert(
                candidate_id,
                "friction",
                f"{meal_name}反复出现“{FRICTION_LABELS[reason]}”，后续方案应先降低这类摩擦。",
                {"meal_name": meal_name, "reason_code": reason, "window_days": 21},
                {
                    "support_count": len(supports),
                    "opportunity_count": len(opportunities),
                    "rate": round(rate, 3),
                    "counterexample_count": len(counterexamples),
                },
                confidence,
                {
                    "action": "temporary_downrank",
                    "reason_code": reason,
                    "meal_name": meal_name,
                    "expires_after_days": 7,
                    "expires_after_plans": 3,
                },
                supports,
                counterexamples,
            )

    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for item in feedback:
        if item.get("strategy_key"):
            by_strategy[item["strategy_key"]].append(item)
    for strategy_key, opportunities in by_strategy.items():
        positives = [item for item in opportunities if _positive_outcome(item)]
        rate = len(positives) / len(opportunities) if opportunities else 0
        if len(opportunities) < 4 or len(positives) < 3 or rate < 0.75:
            continue
        counterexamples = [item for item in opportunities if item not in positives]
        meal_name = opportunities[0]["meal_name"]
        _candidate_upsert(
            _stable_id("candidate", "strategy", strategy_key, scope_key),
            "strategy",
            f"{meal_name}方案“{strategy_key}”在重复执行中表现稳定，可在相似场景优先复用。",
            {"meal_name": meal_name, "strategy_key": strategy_key, "window_days": 42},
            {
                "support_count": len(positives),
                "opportunity_count": len(opportunities),
                "rate": round(rate, 3),
                "counterexample_count": len(counterexamples),
            },
            "strong" if len(positives) >= 4 and rate >= 0.8 else "emerging",
            {"action": "prefer_strategy", "strategy_key": strategy_key, "meal_name": meal_name},
            positives,
            counterexamples,
        )
    return list_candidates()


def list_candidates(status: str | None = None, *, current_scope_only: bool = True) -> list[dict]:
    init_db()
    sql = "SELECT * FROM adaptation_candidates"
    params: tuple[Any, ...] = ()
    if status:
        if status not in {"pending", "accepted", "rejected", "snoozed", "expired"}:
            raise ValidationError("候选状态无效")
        sql += " WHERE status=?"
        params = (status,)
    sql += " ORDER BY created_at DESC,id"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = row_dict(row)
            item["evidence"] = [dict(value) for value in conn.execute(
                "SELECT evidence_type,evidence_id,stance,created_at FROM adaptation_evidence WHERE candidate_id=? ORDER BY stance,evidence_id",
                (item["id"],),
            ).fetchall()]
            result.append(item)
    if current_scope_only:
        current = _scope_snapshot()
        result = [item for item in result if _scope_matches(item, current)]
    return result


def decide_candidate(candidate_id: str, decision: str, *, statement: str | None = None) -> dict:
    mapping = {"accept": "accepted", "reject": "rejected", "snooze": "snoozed"}
    if decision not in mapping:
        raise ValidationError("候选操作无效")
    timestamp = _now()
    current_scope = _scope_snapshot()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM adaptation_candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            raise KeyError(candidate_id)
        if row["status"] not in {"pending", "snoozed"}:
            raise ValidationError("该候选已处理")
        if not _scope_matches(row_dict(row), current_scope):
            raise ValidationError("该候选属于旧目标或安全策略，不能直接接受")
        clean_statement = _text(statement if statement is not None else row["statement"], "规则内容", maximum=2000)
        conn.execute(
            "UPDATE adaptation_candidates SET statement=?,status=?,updated_at=?,decided_at=? WHERE id=?",
            (clean_statement, mapping[decision], timestamp, timestamp, candidate_id),
        )
        rule_id = None
        if decision == "accept":
            rule_id = _id("rule")
            conn.execute(
                """INSERT INTO adaptive_rules(
                       id,origin,kind,statement,scope_json,effect_json,goal_version_ids_json,
                       profile_version_id,strategy_version_id,safety_mode,scope_policy_version,
                       status,created_at,updated_at
                   ) VALUES(?,'candidate',?,?,?,?,?,?,?,?,?,'active',?,?)""",
                (
                    rule_id,
                    row["kind"],
                    clean_statement,
                    row["scope_json"],
                    row["proposed_effect_json"],
                    row["goal_version_ids_json"],
                    row["profile_version_id"],
                    row["strategy_version_id"],
                    row["safety_mode"],
                    row["scope_policy_version"],
                    timestamp,
                    timestamp,
                ),
            )
        updated = conn.execute("SELECT * FROM adaptation_candidates WHERE id=?", (candidate_id,)).fetchone()
    result = row_dict(updated)
    result["rule_id"] = rule_id
    return result


def create_rule(statement: str, kind: str = "constraint", scope: dict | None = None, effect: dict | None = None) -> dict:
    timestamp = _now()
    rule_id = _id("rule")
    scope_snapshot = _scope_snapshot()
    goals = scope_snapshot["goal_version_ids"]
    with connect() as conn:
        conn.execute(
                """INSERT INTO adaptive_rules(
                       id,origin,kind,statement,scope_json,effect_json,goal_version_ids_json,
                       profile_version_id,strategy_version_id,safety_mode,scope_policy_version,
                       status,created_at,updated_at
                   ) VALUES(?,'user_declared',?,?,?,?,?,?,?,?,?,'active',?,?)""",
            (
                rule_id,
                _text(kind, "规则类型", maximum=50),
                _text(statement, "规则内容", maximum=2000),
                json.dumps(scope or {}, ensure_ascii=False),
                json.dumps(effect or {}, ensure_ascii=False),
                json.dumps(goals, ensure_ascii=False),
                scope_snapshot["profile_version_id"],
                scope_snapshot["strategy_version_id"],
                scope_snapshot["safety_mode"],
                scope_snapshot["policy_version"],
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute("SELECT * FROM adaptive_rules WHERE id=?", (rule_id,)).fetchone()
    return row_dict(row)


def list_rules(active_only: bool = True) -> list[dict]:
    init_db()
    today = date.today().isoformat()
    with connect() as conn:
        conn.execute(
            "UPDATE adaptive_rules SET status='expired',updated_at=? WHERE status='active' AND expires_on IS NOT NULL AND expires_on<?",
            (_now(), today),
        )
        sql = "SELECT * FROM adaptive_rules" + (" WHERE status='active'" if active_only else "") + " ORDER BY created_at DESC"
        rows = conn.execute(sql).fetchall()
    result = [row_dict(row) for row in rows]
    if active_only:
        current = _scope_snapshot()
        result = [item for item in result if _scope_matches(item, current)]
    return result


def set_rule_status(rule_id: str, status: str) -> dict:
    if status not in {"active", "inactive"}:
        raise ValidationError("规则状态无效")
    with connect() as conn:
        updated = conn.execute(
            "UPDATE adaptive_rules SET status=?,updated_at=? WHERE id=?", (status, _now(), rule_id)
        )
        if updated.rowcount != 1:
            raise KeyError(rule_id)
        row = conn.execute("SELECT * FROM adaptive_rules WHERE id=?", (rule_id,)).fetchone()
    return row_dict(row)


def active_adaptations(as_of: str | None = None) -> dict:
    today = date.fromisoformat(_validate_date(as_of or date.today().isoformat()))
    cutoff = (today - timedelta(days=6)).isoformat()
    pending = [
        item for item in list_candidates("pending")
        if item["kind"] == "friction" and item["confidence"] in {"emerging", "strong"}
        and item["created_at"][:10] >= cutoff
    ]
    return {
        "confirmed_rules": list_rules(),
        "transient": [],
        "candidate_suggestions": [{
            "candidate_id": item["id"],
            "statement": item["statement"],
            "effect": item["proposed_effect_json"],
            "expires_on": (today + timedelta(days=7)).isoformat(),
            "reversible": True,
        } for item in pending],
    }


def create_inventory_item(
    name: str,
    amount_text: str = "",
    *,
    expires_on: str | None = None,
    source_kind: str = "user",
    source_id: str | None = None,
) -> dict:
    if expires_on:
        _validate_date(expires_on, "期限")
    item_id = _id("inventory")
    timestamp = _now()
    payload = {"name": _text(name, "食材名称", maximum=200), "amount_text": _text(amount_text, "数量", required=False, maximum=200), "expires_on": expires_on}
    with connect() as conn:
        conn.execute(
            """INSERT INTO inventory_items(
                   id,name,status,amount_text,expires_on,source_kind,source_id,version,created_at,updated_at
               ) VALUES(?,?,'available',?,?,?,?,1,?,?)""",
            (item_id, payload["name"], payload["amount_text"], expires_on, source_kind, source_id, timestamp, timestamp),
        )
        conn.execute(
            "INSERT INTO inventory_events(id,inventory_id,action,payload_json,occurred_at) VALUES(?,?,?,?,?)",
            (_id("inventory_event"), item_id, "create", json.dumps(payload, ensure_ascii=False), timestamp),
        )
        row = conn.execute("SELECT * FROM inventory_items WHERE id=?", (item_id,)).fetchone()
    return row_dict(row)


def update_inventory_status(inventory_id: str, status: str, expected_version: int, amount_text: str | None = None) -> dict:
    if status not in INVENTORY_STATUSES:
        raise ValidationError("库存状态无效")
    timestamp = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM inventory_items WHERE id=?", (inventory_id,)).fetchone()
        if not row:
            raise KeyError(inventory_id)
        if row["version"] != expected_version:
            raise ValidationError("库存状态已变化，请刷新后重试")
        new_amount = row["amount_text"] if amount_text is None else _text(amount_text, "数量", required=False, maximum=200)
        conn.execute(
            """UPDATE inventory_items SET status=?,amount_text=?,version=version+1,updated_at=?
               WHERE id=? AND version=?""",
            (status, new_amount, timestamp, inventory_id, expected_version),
        )
        conn.execute(
            "INSERT INTO inventory_events(id,inventory_id,action,payload_json,occurred_at) VALUES(?,?,?,?,?)",
            (
                _id("inventory_event"), inventory_id, status,
                json.dumps({"before_status": row["status"], "status": status, "amount_text": new_amount}, ensure_ascii=False),
                timestamp,
            ),
        )
        updated = conn.execute("SELECT * FROM inventory_items WHERE id=?", (inventory_id,)).fetchone()
    return row_dict(updated)


def list_inventory(active_only: bool = True) -> list[dict]:
    init_db()
    sql = "SELECT * FROM inventory_items"
    if active_only:
        sql += " WHERE status IN ('available','unknown')"
    sql += " ORDER BY CASE WHEN expires_on IS NULL THEN 1 ELSE 0 END,expires_on,updated_at DESC"
    with connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [row_dict(row) for row in rows]


def schedule_questions(question_date: str | None = None) -> list[dict]:
    target = _validate_date(question_date or date.today().isoformat(), "问题日期")
    current = active_personalization()
    proposals = []
    if current["status"] == "setup_required":
        proposals.append({
            "key": "complete_setup", "category": "setup", "priority": 100,
            "reason": "完成目标和安全档案后，系统才能生成适合你的计划。",
            "expected_impact": "启用个性化工作台",
        })
        budget = 1
    else:
        budget = int(current["profile"]["profile_json"]["constraints"].get("question_budget", 2))
        plan = get_plan_for_date(target)
        if plan:
            for meal in plan["menu"]["meals"]:
                if meal["plan_item_id"] not in plan["feedback"]:
                    proposals.append({
                        "key": f"feedback:{meal['plan_item_id']}",
                        "category": "feedback",
                        "priority": 80,
                        "reason": f"确认{meal.get('name','这餐')}是否执行，才能区分计划问题和执行摩擦。",
                        "expected_impact": "下一次计划与阻力学习",
                    })
        proposals.extend({"key": key, **value} for key, value in QUESTION_CATALOG.items())
    proposals.sort(key=lambda item: (-item["priority"], item["key"]))
    selected = proposals[:budget]
    timestamp = _now()
    with connect() as conn:
        for proposal in selected:
            conn.execute(
                """INSERT OR IGNORE INTO question_events(
                       id,question_date,question_key,category,status,priority,reason,expected_impact,
                       version,created_at,updated_at
                   ) VALUES(?,?,?,?,'pending',?,?,?,?,?,?)""",
                (
                    _id("question"), target, proposal["key"], proposal["category"], proposal["priority"],
                    proposal["reason"], proposal["expected_impact"], 0, timestamp, timestamp,
                ),
            )
        rows = conn.execute(
            "SELECT * FROM question_events WHERE question_date=? AND status='pending' ORDER BY priority DESC,created_at",
            (target,),
        ).fetchall()
    return [row_dict(row) for row in rows[:budget]]


def answer_question(question_id: str, answer: object, expected_version: int, *, skip: bool = False) -> dict:
    timestamp = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM question_events WHERE id=?", (question_id,)).fetchone()
        if not row:
            raise KeyError(question_id)
        if row["status"] != "pending" or row["version"] != expected_version:
            raise ValidationError("问题状态已变化，请刷新后重试")
        status = "skipped" if skip else "answered"
        answer_json = None if skip else json.dumps(answer, ensure_ascii=False)
        conn.execute(
            """UPDATE question_events SET status=?,answer_json=?,version=version+1,updated_at=?
               WHERE id=? AND version=?""",
            (status, answer_json, timestamp, question_id, expected_version),
        )
        updated = conn.execute("SELECT * FROM question_events WHERE id=?", (question_id,)).fetchone()
    return row_dict(updated)


def propose_experiment(variable_key: str, plan: dict) -> dict:
    if not isinstance(plan, dict) or not plan.get("action") or not plan.get("success_signal"):
        raise ValidationError("实验必须包含 action 和 success_signal")
    init_db()
    timestamp = _now()
    experiment_id = _id("experiment")
    scope_snapshot = _scope_snapshot()
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM adaptive_experiments WHERE status IN ('proposed','active') LIMIT 1"
        ).fetchone()
        if existing:
            raise ValidationError("同时只能存在一个待确认或进行中的实验")
        conn.execute(
            """INSERT INTO adaptive_experiments(
                   id,status,variable_key,plan_json,goal_version_ids_json,profile_version_id,
                   strategy_version_id,safety_mode,scope_policy_version,created_at,updated_at
               ) VALUES(?,'proposed',?,?,?,?,?,?,?,?,?)""",
            (
                experiment_id,
                _text(variable_key, "实验变量", maximum=100),
                json.dumps(plan, ensure_ascii=False),
                json.dumps(scope_snapshot["goal_version_ids"], ensure_ascii=False),
                scope_snapshot["profile_version_id"],
                scope_snapshot["strategy_version_id"],
                scope_snapshot["safety_mode"],
                scope_snapshot["policy_version"],
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute("SELECT * FROM adaptive_experiments WHERE id=?", (experiment_id,)).fetchone()
    return row_dict(row)


def activate_experiment(experiment_id: str, starts_on: str, days: int) -> dict:
    starts = date.fromisoformat(_validate_date(starts_on, "实验开始日期"))
    if days < 3 or days > 7:
        raise ValidationError("实验观察窗必须是 3–7 天")
    ends = starts + timedelta(days=days - 1)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM adaptive_experiments WHERE id=?", (experiment_id,)).fetchone()
        if not row:
            raise KeyError(experiment_id)
        if row["status"] != "proposed":
            raise ValidationError("只有待确认实验可以开始")
        conn.execute(
            """UPDATE adaptive_experiments SET status='active',starts_on=?,ends_on=?,updated_at=? WHERE id=?""",
            (starts.isoformat(), ends.isoformat(), _now(), experiment_id),
        )
        updated = conn.execute("SELECT * FROM adaptive_experiments WHERE id=?", (experiment_id,)).fetchone()
    return row_dict(updated)


def finish_experiment(experiment_id: str, result: dict, *, cancel: bool = False) -> dict:
    if not isinstance(result, dict):
        raise ValidationError("实验结果必须是对象")
    with connect() as conn:
        row = conn.execute("SELECT * FROM adaptive_experiments WHERE id=?", (experiment_id,)).fetchone()
        if not row:
            raise KeyError(experiment_id)
        if row["status"] not in {"proposed", "active"}:
            raise ValidationError("实验已结束")
        status = "cancelled" if cancel else "completed"
        conn.execute(
            "UPDATE adaptive_experiments SET status=?,result_json=?,updated_at=? WHERE id=?",
            (status, json.dumps(result, ensure_ascii=False), _now(), experiment_id),
        )
        updated = conn.execute("SELECT * FROM adaptive_experiments WHERE id=?", (experiment_id,)).fetchone()
    return row_dict(updated)


def active_experiment() -> dict | None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM adaptive_experiments WHERE status='active' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    item = row_dict(row)
    return item if item and _scope_matches(item) else None


def calibration_snapshot(as_of: str | None = None) -> dict:
    target = date.fromisoformat(_validate_date(as_of or date.today().isoformat()))
    cutoff = (target - timedelta(days=13)).isoformat()
    feedback = list_plan_feedback(cutoff, target.isoformat())
    feedback_days = {item["plan_date"] for item in feedback}
    with connect() as conn:
        weight_rows = conn.execute(
            """SELECT * FROM metric_observations
               WHERE metric_key='weight_kg' AND observed_date BETWEEN ? AND ? ORDER BY observed_date""",
            (cutoff, target.isoformat()),
        ).fetchall()
    comparable_weights = [row_dict(row) for row in weight_rows]
    return {
        "window_start": cutoff,
        "window_end": target.isoformat(),
        "feedback_days": len(feedback_days),
        "feedback_events": len(feedback),
        "comparable_weight_events": len(comparable_weights),
        "eligible_for_strategy_review": len(feedback_days) >= 5 and len(feedback) >= 12,
        "eligible_for_weight_calibration": len(feedback_days) >= 5 and len(feedback) >= 12 and len(comparable_weights) >= 6,
        "rule": "证据不足时只分析执行摩擦；营养目标变化始终需要用户确认。",
    }


def create_rescue_session(
    plan_date: str,
    plan_item_id: str,
    issue_code: str,
    input_text: str = "",
) -> dict:
    plan = get_plan_for_date(plan_date)
    if not plan or not any(item["plan_item_id"] == plan_item_id for item in plan["menu"]["meals"]):
        raise ValidationError("救场计划项目不存在")
    issue_code = _text(issue_code, "救场问题", maximum=100)
    timestamp = _now()
    rescue_id = _id("rescue")
    with connect() as conn:
        conn.execute(
            """INSERT INTO rescue_sessions(
                   id,review_id,result_version,plan_item_id,issue_code,input_text,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,'pending',?,?)""",
            (
                rescue_id, plan["review_id"], plan["result_version"], plan_item_id, issue_code,
                _text(input_text, "救场补充", required=False, maximum=2000), timestamp, timestamp,
            ),
        )
        row = conn.execute("SELECT * FROM rescue_sessions WHERE id=?", (rescue_id,)).fetchone()
    return row_dict(row)


def complete_rescue_session(rescue_id: str, result: dict) -> dict:
    if not isinstance(result, dict) or not result.get("steps") or not result.get("reason"):
        raise ValidationError("救场结果必须包含 reason 和 steps")
    timestamp = _now()
    with connect() as conn:
        updated = conn.execute(
            """UPDATE rescue_sessions SET status='completed',result_json=?,updated_at=?,completed_at=?
               WHERE id=? AND status='pending'""",
            (json.dumps(result, ensure_ascii=False), timestamp, timestamp, rescue_id),
        )
        if updated.rowcount != 1:
            raise ValidationError("救场任务不存在或已完成")
        row = conn.execute("SELECT * FROM rescue_sessions WHERE id=?", (rescue_id,)).fetchone()
    return row_dict(row)
