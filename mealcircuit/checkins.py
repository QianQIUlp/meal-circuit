from __future__ import annotations

from copy import deepcopy

from .contracts import load_contract
from .validation import ValidationError


SCHEMA_VERSION = 1

MODULES = (
    {"key": "weight", "label": "体重", "description": "记录今天的体重和测量条件", "max_steps": 3},
    {"key": "training", "label": "训练", "description": "记录训练内容、时长和主观状态", "max_steps": 5},
    {"key": "hunger", "label": "饥饿与饱腹", "description": "记录全天饥饿、餐后饱腹和食欲信号", "max_steps": 5},
    {"key": "sleep", "label": "睡眠", "description": "记录睡眠时长、质量和恢复感", "max_steps": 4},
    {"key": "gut", "label": "肠胃反应", "description": "记录实际症状，不根据跳过推断正常", "max_steps": 5},
)

MODULE_BY_KEY = {item["key"]: item for item in MODULES}


def _options(*items: tuple[str, str]) -> list[dict]:
    return [{"value": value, "label": label} for value, label in items]


QUESTIONS = {
    "weight": [
        {"id": "measured", "label": "今天测体重了吗？", "type": "single", "options": _options(("yes", "测了"), ("no", "今天没测"))},
        {"id": "weight_kg", "label": "今天的体重是多少？", "type": "number", "suffix": "kg", "min": 20, "max": 400, "step": "0.1", "when": ("measured", {"yes"})},
        {"id": "measurement_context", "label": "这次是在什么情况下测的？", "type": "single", "options": _options(("morning_fasted", "晨起空腹"), ("before_meal", "饭前"), ("after_meal", "饭后"), ("other", "其他时间")), "allow_other_text": True, "when": ("measured", {"yes"})},
    ],
    "training": [
        {"id": "trained", "label": "今天训练了吗？", "type": "single", "options": _options(("yes", "训练了"), ("no", "没有训练"))},
        {"id": "rest_reason", "label": "今天没有训练的情况是？", "type": "single", "options": _options(("rest_day", "计划休息"), ("planned_not_done", "原本计划但没完成"), ("recovery", "身体不适或恢复中")), "when": ("trained", {"no"})},
        {"id": "training_types", "label": "今天做了哪些训练？", "type": "multi", "options": _options(("strength", "力量训练"), ("cardio", "有氧"), ("sport", "球类或其他运动"), ("mobility", "拉伸或活动度")), "when": ("trained", {"yes"})},
        {"id": "body_parts", "label": "力量训练覆盖了哪些部位？", "type": "multi", "options": _options(("chest", "胸"), ("back", "背"), ("shoulders", "肩"), ("biceps", "肱二头"), ("triceps", "肱三头"), ("legs", "腿"), ("core", "核心"), ("full_body", "全身"), ("other", "其他")), "allow_other_text": True, "when_contains": ("training_types", "strength")},
        {"id": "duration", "label": "训练了多长时间？", "type": "single", "options": _options(("under_30", "30 分钟以内"), ("30_60", "30–60 分钟"), ("60_90", "60–90 分钟"), ("over_90", "90 分钟以上")), "when": ("trained", {"yes"})},
        {"id": "effort", "label": "今天训练时的状态如何？", "type": "single", "options": _options(("easy", "比平时轻松"), ("normal", "和平时差不多"), ("hard", "明显更吃力"), ("poor", "状态明显较差")), "when": ("trained", {"yes"})},
    ],
    "hunger": [
        {"id": "hunger_level", "label": "今天整体的饥饿感如何？", "type": "single", "options": _options(("1", "1 · 几乎不饿"), ("2", "2 · 偏低"), ("3", "3 · 合适"), ("4", "4 · 偏高"), ("5", "5 · 很强"))},
        {"id": "hunger_time", "label": "饥饿最明显在什么时候？", "type": "single", "options": _options(("none", "没有明显时段"), ("morning", "上午或早餐前"), ("before_lunch", "午餐前"), ("afternoon", "下午"), ("before_dinner", "晚餐前"), ("late_night", "晚间或睡前"))},
        {"id": "satiety", "label": "餐后的饱腹感更接近哪种？", "type": "single", "options": _options(("comfortable", "大多舒适"), ("still_hungry", "吃完仍饿"), ("too_full", "吃得过饱"), ("mixed", "不同餐差异较大"))},
        {"id": "satiety_meals", "label": "主要出现在哪一餐？", "type": "multi", "options": _options(("breakfast", "早餐"), ("lunch", "午餐"), ("dinner", "晚餐"), ("snack", "加餐后")), "when": ("satiety", {"still_hungry", "too_full", "mixed"})},
        {"id": "cravings", "label": "今天有没有明显食欲或失控感？", "type": "single", "options": _options(("none", "没有"), ("mild", "轻微想吃特定食物"), ("strong", "食欲很强"), ("loss_of_control", "出现失控感"))},
    ],
    "sleep": [
        {"id": "sleep_duration", "label": "昨晚大约睡了多久？", "type": "duration", "options": _options(("under_5", "少于 5 小时"), ("5_6", "5–6 小时"), ("6_7", "6–7 小时"), ("7_8", "7–8 小时"), ("8_9", "8–9 小时"), ("over_9", "超过 9 小时"))},
        {"id": "sleep_quality", "label": "整体睡眠质量如何？", "type": "single", "options": _options(("1", "1 · 很差"), ("2", "2 · 偏差"), ("3", "3 · 一般"), ("4", "4 · 较好"), ("5", "5 · 很好"))},
        {"id": "awakenings", "label": "夜里醒来了几次？", "type": "single", "options": _options(("none", "没有"), ("once", "1 次"), ("twice", "2 次"), ("three_plus", "3 次或更多"), ("unknown", "不记得"))},
        {"id": "morning_energy", "label": "起床后的恢复感如何？", "type": "single", "options": _options(("refreshed", "精神恢复得好"), ("okay", "基本正常"), ("tired", "仍然疲倦"), ("exhausted", "非常疲惫"))},
    ],
    "gut": [
        {"id": "gut_state", "label": "今天有肠胃异常吗？", "type": "single", "options": _options(("none", "没有明显异常"), ("symptoms", "有一些症状"))},
        {"id": "symptoms", "label": "出现了哪些症状？", "type": "multi", "options": _options(("bloating", "腹胀"), ("gas", "胀气"), ("reflux", "反酸"), ("stomach_pain", "胃痛"), ("nausea", "恶心"), ("diarrhea", "腹泻"), ("constipation", "便秘"), ("burning", "灼烧感"), ("other", "其他")), "allow_other_text": True, "when": ("gut_state", {"symptoms"})},
        {"id": "severity", "label": "症状对今天的影响有多大？", "type": "single", "options": _options(("mild", "轻微，不影响活动"), ("moderate", "中等，需要调整饮食或活动"), ("severe", "严重，明显影响活动")), "when": ("gut_state", {"symptoms"})},
        {"id": "timing", "label": "症状主要在什么时候出现？", "type": "multi", "options": _options(("morning", "早晨"), ("after_meal", "餐后"), ("afternoon", "下午"), ("evening", "晚间"), ("ongoing", "全天持续")), "when": ("gut_state", {"symptoms"})},
        {"id": "bowel_state", "label": "今天的排便情况更接近哪种？", "type": "single", "options": _options(("normal", "正常"), ("loose", "偏稀"), ("hard", "偏硬"), ("none", "今天没有排便"), ("not_relevant", "不确定或不相关")), "when": ("gut_state", {"symptoms"})},
    ],
}


# The shared contract is authoritative.  The literal definitions above remain
# readable for old source distributions, while every supported build replaces
# them from the same JSON consumed by Android.
_CHECKIN_CONTRACT = load_contract("checkin-modules-v1.json")
SCHEMA_VERSION = int(_CHECKIN_CONTRACT["schema_version"])
MODULES = tuple(
    {
        "key": module["key"],
        "label": module["label"],
        "description": module["description"],
        "max_steps": len(module["questions"]),
    }
    for module in _CHECKIN_CONTRACT["modules"]
)
MODULE_BY_KEY = {item["key"]: item for item in MODULES}


def _contract_question(value: dict) -> dict:
    question = deepcopy(value)
    condition = question.pop("when", None)
    if condition:
        question["when"] = (condition["question_id"], set(condition["values"]))
    contains = question.pop("when_contains", None)
    if contains:
        question["when_contains"] = (contains["question_id"], contains["value"])
    if "step" in question:
        question["step"] = str(question["step"])
    return question


QUESTIONS = {
    module["key"]: [_contract_question(question) for question in module["questions"]]
    for module in _CHECKIN_CONTRACT["modules"]
}


def module_definition(module_key: str) -> dict:
    if module_key not in MODULE_BY_KEY:
        raise ValidationError("未知的每日状态模块")
    return MODULE_BY_KEY[module_key]


def _is_applicable(question: dict, answers: dict) -> bool:
    if "when" in question:
        key, allowed = question["when"]
        if answers.get(key) not in allowed:
            return False
    if "when_contains" in question:
        key, expected = question["when_contains"]
        if expected not in (answers.get(key) or []):
            return False
    return True


def applicable_questions(module_key: str, answers: dict | None = None) -> list[dict]:
    module_definition(module_key)
    answers = answers or {}
    return [deepcopy(question) for question in QUESTIONS[module_key] if _is_applicable(question, answers)]


def prune_answers(module_key: str, answers: dict) -> dict:
    cleaned: dict = {}
    for question in QUESTIONS[module_key]:
        if _is_applicable(question, cleaned) and question["id"] in answers:
            cleaned[question["id"]] = answers[question["id"]]
    return cleaned


def question_definition(module_key: str, question_id: str, answers: dict | None = None) -> dict:
    for question in applicable_questions(module_key, answers):
        if question["id"] == question_id:
            return question
    raise ValidationError("该问题不适用于当前答案")


def validate_answer(question: dict, value: object) -> object:
    kind = question["type"]
    other_text = ""
    if question.get("allow_other_text") and isinstance(value, dict):
        other_text = str(value.get("other_text", "")).strip()
        value = value.get("values") if kind == "multi" else value.get("value")
    if kind == "number":
        try:
            number = round(float(value), 1)
        except (TypeError, ValueError) as exc:
            raise ValidationError("请输入有效数字") from exc
        if number < question["min"] or number > question["max"]:
            raise ValidationError(f"数值必须在 {question['min']}–{question['max']} 之间")
        return number
    if kind == "duration" and isinstance(value, (int, float)) and not isinstance(value, bool):
        number = round(float(value), 1)
        if number < 0 or number > 24:
            raise ValidationError("睡眠时长必须在 0–24 小时之间")
        return number
    allowed = {option["value"] for option in question.get("options", [])}
    if kind in {"single", "duration"}:
        if not isinstance(value, str) or value not in allowed:
            raise ValidationError("请选择一个有效选项")
        if question.get("allow_other_text") and value == "other":
            if not other_text:
                raise ValidationError("选择其他时请补充简短说明")
            if len(other_text) > 200:
                raise ValidationError("补充说明不能超过 200 字")
            return {"value": value, "other_text": other_text}
        return value
    if kind == "multi":
        if not isinstance(value, list) or not value:
            raise ValidationError("请至少选择一项")
        if any(not isinstance(item, str) or item not in allowed for item in value):
            raise ValidationError("选择中包含无效选项")
        values = list(dict.fromkeys(value))
        if question.get("allow_other_text") and "other" in values:
            if not other_text:
                raise ValidationError("选择其他时请补充简短说明")
            if len(other_text) > 200:
                raise ValidationError("补充说明不能超过 200 字")
            return {"values": values, "other_text": other_text}
        return values
    raise ValidationError("未知问题类型")


def validate_module_answers(module_key: str, answers: dict) -> dict:
    if not isinstance(answers, dict):
        raise ValidationError("模块答案必须是对象")
    cleaned: dict = {}
    for question in applicable_questions(module_key, answers):
        question_id = question["id"]
        if question_id not in answers:
            raise ValidationError(f"缺少答案：{question['label']}")
        cleaned[question_id] = validate_answer(question, answers[question_id])
    if set(cleaned) != set(answers):
        raise ValidationError("答案包含不适用于当前分支的问题")
    return cleaned


def next_question(module_key: str, answers: dict) -> dict | None:
    for question in applicable_questions(module_key, answers):
        if question["id"] not in answers:
            return question
    return None


def _label(module_key: str, question_id: str, value: object) -> str:
    question = next(item for item in QUESTIONS[module_key] if item["id"] == question_id)
    if question["type"] in {"number"}:
        return f"{value:g} kg"
    if question["type"] == "duration" and isinstance(value, (int, float)):
        return f"{value:g} 小时"
    other_text = ""
    if isinstance(value, dict):
        other_text = value.get("other_text", "")
        value = value.get("values") if question["type"] == "multi" else value.get("value")
    labels = {option["value"]: option["label"] for option in question.get("options", [])}
    if isinstance(value, list):
        shown = "、".join(labels[item] for item in value)
    else:
        shown = labels.get(value, str(value))
    return f"{shown}（{other_text}）" if other_text else shown


def summarize(module_key: str, answers: dict, status: str = "completed") -> str:
    if status == "skipped":
        return "用户选择今天不提供"
    if module_key == "weight":
        if answers["measured"] == "no":
            return "今天未测体重"
        return f"{_label(module_key, 'weight_kg', answers['weight_kg'])} · {_label(module_key, 'measurement_context', answers['measurement_context'])}"
    if module_key == "training":
        if answers["trained"] == "no":
            return f"今天未训练 · {_label(module_key, 'rest_reason', answers['rest_reason'])}"
        parts = [_label(module_key, "training_types", answers["training_types"])]
        if "body_parts" in answers:
            parts.append(_label(module_key, "body_parts", answers["body_parts"]))
        parts.extend((_label(module_key, "duration", answers["duration"]), _label(module_key, "effort", answers["effort"])))
        return " · ".join(parts)
    if module_key == "hunger":
        return f"饥饿 {_label(module_key, 'hunger_level', answers['hunger_level'])} · 餐后{_label(module_key, 'satiety', answers['satiety'])} · {_label(module_key, 'cravings', answers['cravings'])}"
    if module_key == "sleep":
        return f"{_label(module_key, 'sleep_duration', answers['sleep_duration'])} · 质量 {_label(module_key, 'sleep_quality', answers['sleep_quality'])} · {_label(module_key, 'morning_energy', answers['morning_energy'])}"
    if answers["gut_state"] == "none":
        return "今天没有明显肠胃异常"
    return f"{_label(module_key, 'symptoms', answers['symptoms'])} · {_label(module_key, 'severity', answers['severity'])}"
