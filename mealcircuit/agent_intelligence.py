from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import date, timedelta
from typing import Any

from .db import connect, init_db, row_dict
from .domain import new_id, utc_now


FACT_BUNDLE_VERSION = 1
INTENT_SIGNAL_VERSION = 1
GOAL_CONTRACT_VERSION = 1
GOAL_PROGRAM_VERSION = 1
MEAL_EPISODE_VERSION = 1
OUTCOME_ATTRIBUTION_VERSION = 1
VALIDATION_FINDING_VERSION = 1

SOURCE_PRECEDENCE = (
    "user_correction",
    "user_execution_description",
    "multi_photo_observation",
    "single_photo_candidate",
    "model_inference",
    "unknown",
)

_DURABLE_MARKERS = ("以后", "今后", "长期", "默认", "不要再", "不再", "以后都", "一直", "不需要")
_TEMPORARY_MARKERS = ("今天", "这次", "临时", "今晚", "这一顿", "明天")
_HIGH_IMPACT_MARKERS = (
    "过敏", "禁忌", "疾病", "用药", "药物", "怀孕", "孕期", "哺乳", "未成年",
    "治疗", "医生", "热量目标", "蛋白目标", "完全不吃", "永久不吃",
)
_COST_MARKERS = ("太贵", "很贵", "贵了", "高价", "买不起", "吃不起", "不划算", "性价比", "预算")
_COMPLEXITY_MARKERS = ("太麻烦", "步骤太多", "不想做", "做不动", "费事", "复杂")
_TIME_MARKERS = ("来不及", "没时间", "时间不够", "只有", "赶时间")
_PORTION_LOW_MARKERS = ("太少", "没吃饱", "不够吃", "菜量少", "份量少")
_PORTION_HIGH_MARKERS = ("太多", "吃不完", "太撑", "份量大")
_TASTE_MARKERS = ("不想吃", "不好吃", "不合口味", "吃腻", "不喜欢")

_GOAL_PROGRAMS = {
    "fat_loss": {
        "label": "减脂",
        "required_dimensions": ["可持续能量策略", "饱腹", "执行率", "趋势"],
        "selection_priority": ["安全", "可持续", "饱腹", "执行概率", "训练兼容"],
    },
    "muscle_gain": {
        "label": "增肌",
        "required_dimensions": ["能量充足", "训练恢复", "蛋白分配", "训练表现"],
        "selection_priority": ["安全", "恢复", "能量充足", "执行概率", "口味"],
    },
    "body_recomposition": {
        "label": "身体重组",
        "required_dimensions": ["减脂趋势", "训练恢复", "饱腹", "长期执行"],
        "selection_priority": ["安全", "恢复", "饱腹", "可持续", "执行概率"],
    },
    "performance": {
        "label": "训练表现",
        "required_dimensions": ["训练供能", "恢复", "消化耐受", "执行时间"],
        "selection_priority": ["安全", "训练表现", "恢复", "消化耐受", "执行概率"],
    },
    "maintenance": {
        "label": "保持状态",
        "required_dimensions": ["饮食结构", "稳定执行", "身体状态", "生活适配"],
        "selection_priority": ["安全", "稳定", "执行概率", "口味", "便利"],
    },
    "eating_consistency": {
        "label": "规律饮食",
        "required_dimensions": ["餐次稳定", "低摩擦", "饱腹", "可恢复性"],
        "selection_priority": ["安全", "低摩擦", "执行概率", "饱腹", "便利"],
    },
    "general_wellbeing": {
        "label": "一般健康",
        "required_dimensions": ["饮食结构", "多样性", "身体感受", "可持续习惯"],
        "selection_priority": ["安全", "均衡", "可持续", "执行概率", "口味"],
    },
    "custom": {
        "label": "自定义目标",
        "required_dimensions": ["用户定义的成功指标", "现实约束", "可持续执行"],
        "selection_priority": ["安全", "用户优先级", "执行概率", "现实成本"],
    },
}


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    return f"{prefix}_{_hash(parts)[:16]}"


def goal_contract_projection(personalization: dict, *, include_versioned_rows: bool = False) -> dict:
    profile_row = personalization.get("profile") or {}
    profile = profile_row.get("profile_json") or {}
    strategy_row = personalization.get("strategy") or {}
    strategy = strategy_row.get("strategy_json") or {}
    all_goals = sorted(
        personalization.get("goals") or [],
        key=lambda item: (item.get("goal_json") or {}).get("priority", 99),
    )
    safety = deepcopy(personalization.get("safety") or {})
    safety_mode = safety.get("mode") or "setup_required"
    restricted = safety_mode in {"clinician_guided", "observation", "halt_and_refer"}
    goals = [] if restricted else all_goals
    target_rows = list(personalization.get("targets") or [])
    if restricted:
        target_rows = [
            item for item in target_rows
            if safety_mode == "clinician_guided"
            and safety.get("professional_guidance_current")
            and item.get("source_kind") == "clinician_provided"
            and item.get("safety_mode") == "clinician_guided"
        ]
    constraints = profile.get("constraints") or {}
    contract = {
        "schema_version": GOAL_CONTRACT_VERSION,
        "profile_version_id": profile_row.get("id"),
        "strategy_version_id": strategy_row.get("id"),
        "goals": [
            {
                "id": item.get("id"),
                "type": (item.get("goal_json") or {}).get("type"),
                "why": (item.get("goal_json") or {}).get("motivation") or "",
                "priority": (item.get("goal_json") or {}).get("priority"),
                "success_metrics": (item.get("goal_json") or {}).get("success_metrics") or [],
                "target_weight_kg": (item.get("goal_json") or {}).get("target_weight_kg"),
                "horizon": (item.get("goal_json") or {}).get("horizon") or "",
            }
            for item in goals
        ],
        "non_negotiables": constraints.get("non_negotiables") or [],
        "priority_tradeoffs": constraints.get("priority_tradeoffs") or [],
        "meal_modes": strategy.get("meal_modes") or constraints.get("meal_modes") or {},
        "collaboration": {
            "recording_intensity": constraints.get("recording_intensity", "light"),
            "followup_intensity": constraints.get("followup_intensity", "only_when_needed"),
            "question_budget": constraints.get("question_budget", 2),
            "portion_method": constraints.get("portion_method") or strategy.get("portion_method") or "",
        },
        "safety": safety,
        "suspended_goal_count": len(all_goals) if restricted else 0,
        "nutrition_targets": [
            {
                "id": item.get("id"),
                "key": item.get("target_key"),
                "value": item.get("value_json"),
                "unit": item.get("unit"),
                "source_kind": item.get("source_kind"),
                "method": item.get("method"),
                "applicability": item.get("applicability_json") or {},
                "confirmed_at": item.get("confirmed_at"),
                "valid_from": item.get("valid_from"),
                "valid_until": item.get("valid_until"),
                "policy_version": item.get("policy_version"),
            }
            for item in target_rows
        ],
    }
    contract["contract_id"] = f"goal_contract_{_hash(contract)[:20]}"
    if include_versioned_rows:
        contract["versioned_rows"] = {
            "profile": deepcopy(profile_row) if profile_row else None,
            "goals": deepcopy(all_goals),
            "strategy": deepcopy(strategy_row) if strategy_row else None,
            "targets": deepcopy(personalization.get("targets") or []),
        }
    return contract


def goal_program(personalization: dict) -> dict:
    contract = goal_contract_projection(personalization)
    programs = []
    for goal in contract["goals"]:
        base = deepcopy(_GOAL_PROGRAMS.get(goal.get("type"), _GOAL_PROGRAMS["custom"]))
        base.update({"goal_id": goal.get("id"), "goal_type": goal.get("type"), "priority": goal.get("priority")})
        programs.append(base)
    safety_mode = (contract.get("safety") or {}).get("mode") or "setup_required"
    if safety_mode in {"clinician_guided", "observation", "halt_and_refer"}:
        programs = [{
            "goal_id": None,
            "goal_type": safety_mode,
            "label": "专业边界内协作" if safety_mode == "clinician_guided" else "事实观察",
            "priority": 0,
            "required_dimensions": ["专业指导有效性", "事实与未知", "安全边界"],
            "selection_priority": ["安全", "专业指导", "事实完整性"],
        }]
    return {
        "version": GOAL_PROGRAM_VERSION,
        "contract_id": contract["contract_id"],
        "programs": programs,
        "must_not_sacrifice": contract["non_negotiables"],
    }


def professional_envelope(personalization: dict, knowledge: dict) -> dict:
    contract = goal_contract_projection(personalization)
    program = goal_program(personalization)
    safety = contract.get("safety") or {}
    targets = contract.get("nutrition_targets") or []
    return {
        "version": 1,
        "safety_mode": safety.get("mode") or "setup_required",
        "professional_guidance_current": bool(safety.get("professional_guidance_current")),
        "goal_program": program,
        "confirmed_targets": targets,
        "numeric_boundaries": {
            item["key"]: {
                "range": item["value"],
                "unit": item["unit"],
                "method": item["method"],
                "applicability": item["applicability"],
                "valid_until": item["valid_until"],
            }
            for item in targets
        },
        "selected_principles": deepcopy(knowledge.get("principles") or []),
        "unknown_policy": "缺少已确认数值时保持区间或未知；模型不得自行创造目标。",
        "forbidden_inference": ["诊断", "药物调整", "未确认的热量或蛋白目标", "普通成人目标泄漏到受限模式"],
    }


def _store_sync_projection(kind: str, payload: dict) -> None:
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    timestamp = utc_now()
    with connect() as conn:
        conn.execute(
            """INSERT INTO config_documents(kind,content,content_sha256,revision_id,updated_at)
               VALUES(?,?,?,NULL,?)
               ON CONFLICT(kind) DO UPDATE SET content=excluded.content,
                   content_sha256=excluded.content_sha256,updated_at=excluded.updated_at""",
            (kind, content, hashlib.sha256(content.encode("utf-8")).hexdigest(), timestamp),
        )
        from .domain_store import capture_entity, preference_entity_id

        capture_entity(conn, "preferences", preference_entity_id(kind), created_at=timestamp)


def refresh_goal_contract_sync(personalization: dict) -> dict:
    contract = goal_contract_projection(personalization, include_versioned_rows=True)
    _store_sync_projection("goal_contract", contract)
    return contract


def refresh_meal_episode_sync() -> dict:
    cutoff = (date.today() - timedelta(days=180)).isoformat()
    with connect() as conn:
        rows = [row_dict(row) for row in conn.execute(
            """SELECT id,event_date,meal_slot,projection_json,source_ids_json,version,created_at,updated_at
               FROM meal_episode_projections WHERE event_date>=?
               ORDER BY event_date,meal_slot""",
            (cutoff,),
        ).fetchall()]
    payload = {
        "schema_version": MEAL_EPISODE_VERSION,
        "window_days": 180,
        "episodes": rows,
    }
    _store_sync_projection("meal_episode_projection", payload)
    return payload


def validation_finding(
    severity: str,
    code: str,
    message: str,
    *,
    stage: str,
    user_harm: str = "",
    repair_instruction: str = "",
) -> dict:
    if severity not in {"block", "repair", "warn", "info"}:
        raise ValueError(severity)
    return {
        "version": VALIDATION_FINDING_VERSION,
        "severity": severity,
        "code": code,
        "message": message,
        "stage": stage,
        "user_harm": user_harm,
        "repair_instruction": repair_instruction,
    }


def _natural_language_sources(context: dict) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for item in (context.get("today") or {}).get("records") or []:
        source_id = str(item.get("id") or "")
        text = str(item.get("raw_input") or "").strip()
        if source_id and text:
            result.append({
                "source_id": source_id,
                "source_type": "daily_record",
                "text": text,
                "observed_date": item.get("record_date"),
                "explicit_user": True,
            })
            seen.add(source_id)
    for item in (context.get("today") or {}).get("agent_intake") or []:
        source_id = str(item.get("id") or "")
        linked_record_id = str(item.get("record_id") or "")
        text = str(item.get("input_text") or "").strip()
        if source_id and source_id not in seen and linked_record_id not in seen and text:
            result.append({
                "source_id": source_id,
                "source_type": "workspace_intake",
                "text": text,
                "observed_date": (context.get("today") or {}).get("date"),
                "explicit_user": True,
            })
    for item in (context.get("longitudinal") or {}).get("selected_execution_feedback") or []:
        source_id = str(item.get("id") or "")
        text = str(item.get("actual_text") or item.get("actual") or "").strip()
        if source_id and text:
            result.append({
                "source_id": source_id,
                "source_type": "execution_feedback",
                "text": text,
                "observed_date": item.get("plan_date") or item.get("date"),
                "explicit_user": True,
            })
    return result


def _entity_before_marker(text: str, markers: tuple[str, ...]) -> str:
    for marker in markers:
        index = text.find(marker)
        if index <= 0:
            continue
        prefix = re.sub(r"[，。；、,:：!！?？]", " ", text[:index]).split()
        if prefix:
            candidate = prefix[-1][-16:]
            candidate = re.sub(r"^(因为|主要是|觉得|这个|那个|我|现在)", "", candidate)
            if candidate:
                return candidate
    return ""


def _clean_entity_candidate(value: str) -> str:
    candidate = re.split(r"[，。；、,:：!！?？]", str(value or ""), maxsplit=1)[0].strip()
    if not candidate:
        return ""
    parts = re.split(
        r"(?:因为|由于|就是|觉得|认为|发现|执行时|执行|安排|计划|主要是|换成|改成)",
        candidate,
    )
    candidate = parts[-1].strip()
    candidate = re.sub(r"^(?:我|今天|明天|以后|现在|这次|午饭|午餐|晚饭|晚餐|早餐)+", "", candidate)
    candidate = re.sub(r"(?:价格|这个|那个|了|也)$", "", candidate).strip()
    return candidate[-16:]


def _cost_entity(text: str, markers: tuple[str, ...]) -> str:
    high_price = re.search(r"高价([\u4e00-\u9fffA-Za-z0-9]{1,12})", text)
    if high_price:
        return _clean_entity_candidate(high_price.group(1))
    value_after = re.search(
        r"性价比(?:不高|太低|低|不好)?(?:的)?([\u4e00-\u9fffA-Za-z0-9]{1,12})",
        text,
    )
    if value_after:
        return _clean_entity_candidate(value_after.group(1))
    unaffordable = re.search(r"(?:买不起|吃不起)([\u4e00-\u9fffA-Za-z0-9]{1,12})", text)
    if unaffordable:
        after = _clean_entity_candidate(unaffordable.group(1))
        if after and not any(after.startswith(word) for word in ("以后", "今后", "算了", "不再")):
            return after
    return _clean_entity_candidate(_entity_before_marker(text, markers))


def detect_intent_signals(source: dict, review_date: str) -> list[dict]:
    text = str(source.get("text") or "").strip()
    if not text:
        return []
    explicit_horizon = any(marker in text for marker in _DURABLE_MARKERS if marker != "不需要")
    temporary_horizon = any(marker in text for marker in _TEMPORARY_MARKERS)
    durable = explicit_horizon or ("不需要" in text and not temporary_horizon)
    temporary = temporary_horizon and not explicit_horizon
    high_impact = any(marker in text for marker in _HIGH_IMPACT_MARKERS)
    plan_date = (date.fromisoformat(review_date) + timedelta(days=1)).isoformat()
    valid_until = plan_date if "明天" in text else review_date if temporary else None
    categories: list[tuple[str, tuple[str, ...]]] = [
        ("cost", _COST_MARKERS),
        ("complexity", _COMPLEXITY_MARKERS),
        ("time", _TIME_MARKERS),
        ("portion_low", _PORTION_LOW_MARKERS),
        ("portion_high", _PORTION_HIGH_MARKERS),
        ("taste", _TASTE_MARKERS),
    ]
    signals = []
    for category, markers in categories:
        matched = [marker for marker in markers if marker in text]
        if not matched:
            continue
        entity = _cost_entity(text, markers) if category == "cost" else _entity_before_marker(text, markers)
        if category == "taste":
            match = re.search(r"(?:不想吃|不喜欢|吃腻)([\u4e00-\u9fffA-Za-z0-9]{1,12})", text)
            if match:
                entity = match.group(1)
        risk = "high" if high_impact or (durable and category == "taste" and any(token in text for token in ("完全", "永远", "永久"))) else "low"
        lifetime = "durable" if durable else "temporary" if temporary else "candidate"
        signal = {
            "signal_id": _stable_id("intent", source.get("source_id"), category, matched[0]),
            "source_id": source.get("source_id"),
            "source_type": source.get("source_type"),
            "category": category,
            "markers": matched,
            "entity": entity,
            "lifetime": lifetime,
            "risk_level": risk,
            "explicit_user_statement": bool(source.get("explicit_user")),
            "excerpt": text[:1000],
            "valid_until": valid_until,
        }
        if category == "cost":
            subject = entity or "日常食材"
            signal["proposed_claim"] = {
                "claim_type": "soft_need_hypothesis",
                "claim_dimension": "resource_constraint",
                "statement": f"日常方案要优先选择长期负担得起、性价比合适的来源；{subject}不作为默认项。",
                "scope": {"resource": "budget", "item": subject, "not_a_ban": True},
                "planning_effect": {
                    "ranking": "同等满足目标时优先性价比更高且能长期购买的食物",
                    "budget": f"{subject}不作为默认采购；只有用户主动提出或价格条件变化时再考虑",
                    "alternatives": "优先比较鸡蛋、禽肉、豆制品、普通瘦肉等现实可得来源",
                },
            }
        elif category in {"complexity", "time"}:
            signal["proposed_claim"] = {
                "claim_type": "friction_hypothesis",
                "claim_dimension": "execution_friction",
                "statement": "主动操作时间或步骤过多会降低这类餐次的执行概率。",
                "scope": {"friction": category},
                "planning_effect": {"complexity": "减少持续看火和切配步骤，并保留更短备选"},
            }
        elif category in {"portion_low", "portion_high"}:
            signal["proposed_claim"] = {
                "claim_type": "body_response_hypothesis",
                "claim_dimension": "satiety_pattern",
                "statement": "这类餐次的计划份量与实际食欲可能不匹配。",
                "scope": {"direction": "increase" if category == "portion_low" else "decrease"},
                "planning_effect": {
                    "portion": "明确加量顺序并提高蔬菜体积" if category == "portion_low" else "允许降低总体积但保留主要蛋白"
                },
            }
        else:
            subject = entity or "这类食物"
            signal["proposed_claim"] = {
                "claim_type": "stable_preference" if durable else "temporary_state",
                "claim_dimension": "stable_preference" if durable else "temporary_state",
                "statement": f"当前不希望默认安排{subject}。" if temporary else f"日常方案默认避免安排{subject}，但这不是安全禁食。",
                "scope": {"item": subject, "not_a_safety_exclusion": True},
                "planning_effect": {"ranking": f"降低{subject}的默认排序", "alternatives": "提供不同主蛋白或风味"},
            }
        signals.append(signal)
    if high_impact and not signals:
        signals.append({
            "signal_id": _stable_id("intent", source.get("source_id"), "high_impact"),
            "source_id": source.get("source_id"),
            "source_type": source.get("source_type"),
            "category": "high_impact",
            "markers": [marker for marker in _HIGH_IMPACT_MARKERS if marker in text],
            "entity": "",
            "lifetime": "requires_confirmation",
            "risk_level": "high",
            "explicit_user_statement": bool(source.get("explicit_user")),
            "excerpt": text[:1000],
            "valid_until": None,
            "proposed_claim": {
                "claim_type": "confirmed_fact",
                "claim_dimension": "high_impact_candidate",
                "statement": "用户提到了可能改变安全或专业边界的信息，需要在目标与安全档案中确认。",
                "scope": {"source_id": source.get("source_id")},
                "planning_effect": {},
            },
        })
    return signals


def compile_fact_bundle(context: dict, *, episodes: list[dict] | None = None) -> dict:
    review_date = (context.get("today") or {}).get("date") or context.get("decision_task", {}).get("review_date")
    sources = _natural_language_sources(context)
    signals = [signal for source in sources for signal in detect_intent_signals(source, review_date)]
    facts = []
    for source in sources:
        facts.append({
            "fact_id": _stable_id("fact", source["source_id"]),
            "source_id": source["source_id"],
            "kind": "user_statement",
            "value": source["text"],
            "certainty": "user_reported",
        })
    checkin = (context.get("today") or {}).get("checkin") or {}
    if checkin:
        facts.append({
            "fact_id": _stable_id("fact", review_date, "checkin"),
            "source_id": f"checkin:{review_date}",
            "kind": "structured_status",
            "value": checkin,
            "certainty": "user_reported",
        })
    bundle = {
        "schema_version": FACT_BUNDLE_VERSION,
        "review_date": review_date,
        "facts": facts,
        "meal_episodes": deepcopy(episodes or []),
        "natural_language_sources": sources,
        "detected_intent_signals": signals,
        "unknowns": [
            "照片不可见的油、酱汁、重量和品牌保持未知",
            "没有用户执行描述时，照片盛装量不能自动当作实际吃下量",
        ],
        "conflict_policy": "用户明确纠正优先，但原始观察保留为来源证据。",
        "source_precedence": list(SOURCE_PRECEDENCE),
    }
    bundle["bundle_hash"] = _hash(bundle)
    return bundle


def _slot_from_name(value: str) -> str:
    return {"早餐": "breakfast", "午餐": "lunch", "晚餐": "dinner"}.get(value, "unknown")


def refresh_meal_episode(event_date: str, meal_slot: str) -> dict:
    from . import adaptive

    init_db()
    evidence = [item for item in adaptive.meal_evidence(event_date, event_date) if item.get("meal_slot") == meal_slot]
    feedback = next((item for item in adaptive.list_plan_feedback(event_date) if _slot_from_name(item.get("meal_name") or "") == meal_slot), None)
    plan = adaptive.get_plan_for_date(event_date, include_restricted_history=True)
    planned = None
    if plan:
        planned = next((item for item in (plan.get("menu") or {}).get("meals") or [] if _slot_from_name(item.get("name") or "") == meal_slot), None)
    sources = []
    corrections = []
    for item in evidence:
        task_id = item.get("task_id")
        task_source = {
            "source_id": task_id,
            "source_kind": "photo_candidate" if item.get("type") == "photo" else "material_candidate",
            "observation": item.get("result_json") or {},
            "completed_at": item.get("completed_at"),
        }
        sources.append(task_source)
        for correction in item.get("corrections") or []:
            corrections.append({
                "source_id": correction.get("id"),
                "task_id": task_id,
                "text": (correction.get("correction_json") or {}).get("text") or "",
                "created_at": correction.get("created_at"),
            })
    actual_text = str((feedback or {}).get("actual_text") or "").strip()
    if corrections:
        current_fact = corrections[-1]["text"]
        current_fact_source = "user_correction"
    elif actual_text:
        current_fact = actual_text
        current_fact_source = "user_execution_description"
    elif len(sources) > 1:
        current_fact = [item["observation"] for item in sources]
        current_fact_source = "multi_photo_observation"
    elif sources:
        current_fact = sources[0]["observation"]
        current_fact_source = "single_photo_candidate"
    else:
        current_fact = None
        current_fact_source = "unknown"
    projection = {
        "schema_version": MEAL_EPISODE_VERSION,
        "event_date": event_date,
        "meal_slot": meal_slot,
        "planned": {
            "plan_item_id": (planned or {}).get("plan_item_id"),
            "purpose": (planned or {}).get("purpose") or "",
            "meal": planned,
        } if planned else None,
        "photo_and_material_sources": sources,
        "user_corrections": corrections,
        "execution_feedback": feedback,
        "current_fact": current_fact,
        "current_fact_source": current_fact_source,
        "source_precedence": list(SOURCE_PRECEDENCE),
    }
    source_ids = [item["source_id"] for item in sources if item.get("source_id")]
    source_ids.extend(item["source_id"] for item in corrections if item.get("source_id"))
    if feedback:
        source_ids.append(feedback["id"])
    timestamp = utc_now()
    episode_id = _stable_id("meal_episode", event_date, meal_slot)
    with connect() as conn:
        existing = conn.execute(
            "SELECT version,projection_json FROM meal_episode_projections WHERE event_date=? AND meal_slot=?",
            (event_date, meal_slot),
        ).fetchone()
        encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True)
        if existing and existing["projection_json"] == encoded:
            return row_dict(conn.execute("SELECT * FROM meal_episode_projections WHERE id=?", (episode_id,)).fetchone())
        if existing:
            conn.execute(
                """UPDATE meal_episode_projections SET projection_json=?,source_ids_json=?,version=version+1,updated_at=?
                   WHERE event_date=? AND meal_slot=?""",
                (encoded, json.dumps(source_ids, ensure_ascii=False), timestamp, event_date, meal_slot),
            )
        else:
            conn.execute(
                """INSERT INTO meal_episode_projections(
                       id,event_date,meal_slot,projection_json,source_ids_json,version,created_at,updated_at
                   ) VALUES(?,?,?,?,?,1,?,?)""",
                (episode_id, event_date, meal_slot, encoded, json.dumps(source_ids, ensure_ascii=False), timestamp, timestamp),
            )
        row = conn.execute("SELECT * FROM meal_episode_projections WHERE event_date=? AND meal_slot=?", (event_date, meal_slot)).fetchone()
    result = row_dict(row)
    refresh_meal_episode_sync()
    return result


def refresh_meal_episodes(event_date: str) -> list[dict]:
    return [refresh_meal_episode(event_date, slot) for slot in ("breakfast", "lunch", "dinner")]


def attribute_feedback(feedback: dict, *, learning_links: list[str] | None = None) -> dict:
    reasons = set(feedback.get("reason_codes_json") or [])
    actual = str(feedback.get("actual_text") or "")
    durable_request = any(marker in actual for marker in _DURABLE_MARKERS)
    snapshot = feedback.get("planned_snapshot_json") or {}
    if "too_expensive" in reasons or any(marker in actual for marker in _COST_MARKERS):
        cause, stable = "price", durable_request
        next_change = "默认改用长期负担得起的同功能食材，不把高价食材当作执行前提。"
    elif "missing_ingredient" in reasons:
        cause, stable = "inventory", False
        next_change = "先核对现有库存，并提供不额外采购的替代。"
    elif "not_enough_time" in reasons:
        cause, stable = "time", durable_request
        next_change = "减少主动操作时间，保留更短执行路径。"
    elif "too_complex" in reasons:
        cause, stable = "complexity", durable_request
        next_change = "减少步骤和持续看火，只保留一个新技巧。"
    elif "hunger_mismatch" in reasons or any(marker in actual for marker in _PORTION_LOW_MARKERS + _PORTION_HIGH_MARKERS):
        cause, stable = "portion", durable_request
        next_change = "调整这类餐次的总体积，并明确加减量顺序。"
    elif "did_not_want_it" in reasons or any(marker in actual for marker in _TASTE_MARKERS):
        cause, stable = "taste", False
        next_change = "先作为当日意愿处理；重复出现后再提出稳定偏好。"
    elif "gut_change" in reasons:
        cause, stable = "body_state", False
        next_change = "只做当日保守调整，不把一次身体状态写成长期偏好。"
    elif "schedule_change" in reasons or "ate_out" in reasons:
        cause, stable = "temporary_event", False
        next_change = "下次保留低摩擦备选，不把临时变化归因于执行意愿。"
    else:
        cause, stable = "none" if feedback.get("status") == "followed" else "unknown", False
        next_change = "保持有效做法。" if cause == "none" else "保留未知，等待更具体的执行描述。"
    purpose = str(snapshot.get("purpose") or snapshot.get("whole_day_role") or snapshot.get("portion_guidance") or "")
    achieved = feedback.get("status") in {"followed", "modified"}
    attribution = {
        "schema_version": OUTCOME_ATTRIBUTION_VERSION,
        "feedback_id": feedback.get("id"),
        "purpose": purpose,
        "purpose_achieved": achieved,
        "primary_cause": cause,
        "likely_repeat": stable,
        "next_change": next_change,
        "evidence": {"reason_codes": sorted(reasons), "actual_text": actual},
        "learning_links": learning_links or [],
    }
    timestamp = utc_now()
    attribution_id = _stable_id("outcome", feedback.get("id"))
    meal_slot = _slot_from_name(str(feedback.get("meal_name") or ""))
    prediction = {
        "satiety": snapshot.get("predicted_satiety") or snapshot.get("adjustment_logic") or {},
        "time_minutes": (snapshot.get("recipe_card") or {}).get("total_minutes"),
        "cost": snapshot.get("predicted_cost") or "unknown",
        "execution_risks": snapshot.get("execution_risks") or [],
    }
    with connect() as conn:
        conn.execute(
            """INSERT INTO plan_outcome_attributions(
                   id,feedback_id,plan_date,meal_slot,purpose,prediction_json,outcome_json,
                   attribution_json,learning_links_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(feedback_id) DO UPDATE SET purpose=excluded.purpose,
                   prediction_json=excluded.prediction_json,outcome_json=excluded.outcome_json,
                   attribution_json=excluded.attribution_json,learning_links_json=excluded.learning_links_json,
                   updated_at=excluded.updated_at""",
            (
                attribution_id, feedback["id"], feedback["plan_date"], meal_slot, purpose,
                json.dumps(prediction, ensure_ascii=False),
                json.dumps(feedback.get("outcome_json") or {}, ensure_ascii=False),
                json.dumps(attribution, ensure_ascii=False),
                json.dumps(learning_links or [], ensure_ascii=False),
                timestamp, timestamp,
            ),
        )
    refresh_meal_episode(feedback["plan_date"], meal_slot)
    return attribution


def list_outcome_attributions(start_date: str, end_date: str | None = None) -> list[dict]:
    end_date = end_date or start_date
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM plan_outcome_attributions WHERE plan_date BETWEEN ? AND ?
               ORDER BY plan_date,meal_slot""",
            (start_date, end_date),
        ).fetchall()
    return [row_dict(row) for row in rows]
