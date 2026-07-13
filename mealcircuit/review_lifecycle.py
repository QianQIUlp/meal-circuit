from __future__ import annotations

import json
from datetime import date


def _json_object(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def revision_policy(connection, review: dict, today: date) -> dict:
    result = _json_object(review.get("result_json"))
    menu = result.get("tomorrow_menu") or {}
    plan_date_text = str(menu.get("date") or "")
    reasons: list[str] = []
    if plan_date_text:
        try:
            if date.fromisoformat(plan_date_text) < today:
                reasons.append("past_plan_date")
        except ValueError:
            reasons.append("invalid_plan_date")
    review_id = review.get("id")
    if review_id:
        if connection.execute(
            "SELECT 1 FROM plan_execution_feedback WHERE review_id=? LIMIT 1", (review_id,)
        ).fetchone():
            reasons.append("execution_feedback")
        if connection.execute(
            "SELECT 1 FROM rescue_sessions WHERE review_id=? LIMIT 1", (review_id,)
        ).fetchone():
            reasons.append("rescue_session")
        if connection.execute(
            """SELECT 1 FROM adaptation_evidence e
               JOIN plan_execution_feedback f ON e.evidence_type='plan_feedback' AND e.evidence_id=f.id
               WHERE f.review_id=? LIMIT 1""",
            (review_id,),
        ).fetchone():
            reasons.append("learning_evidence")
    return {
        "mode": "locked" if reasons else "replaceable",
        "reasons": list(dict.fromkeys(reasons)),
        "plan_date": plan_date_text or None,
    }
