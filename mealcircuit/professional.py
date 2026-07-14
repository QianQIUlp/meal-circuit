from __future__ import annotations

import hashlib
import json
from copy import deepcopy


KNOWLEDGE_PACK_VERSION = "professional-basis-2026-07-v1"

# Runtime planning never browses the web.  These are deliberately short decision
# principles with explicit boundaries, not a general nutrition corpus.
_PRINCIPLES = (
    {
        "id": "balanced-pattern",
        "applies_to": ["general", "fat_loss", "muscle_gain", "recomposition", "body_recomposition"],
        "principle": "用蔬菜、水果、主食和合适的蛋白来源组成有变化的一日饮食，优先少加工选择，但不把某一顿饭简单分成好或坏。",
        "planning_use": "检查全天结构和现实可行的替换，不追求一顿所谓的完美餐。",
        "boundary": "这条原则不能单独决定个人能量或蛋白质目标。",
        "source": {
            "organization": "World Health Organization",
            "title": "Healthy diet",
            "url": "https://www.who.int/news-room/fact-sheets/healthy-diet",
            "published": "2026-01-26",
            "verified_on": "2026-07-14",
        },
    },
    {
        "id": "training-fuel-recovery",
        "applies_to": ["training", "muscle_gain", "recomposition", "body_recomposition", "fat_loss"],
        "principle": "让饮食和饮水配合实际训练负荷；在已确认目标和食欲允许时，把含蛋白质的食物合理分散到全天。",
        "planning_use": "只有训练的实际时间会改变安排时才调整餐次；已覆盖确认目标后不机械加餐。",
        "boundary": "精确目标必须来自 MealCircuit 已确认且有来源的个人目标，不能来自知识片段或模型记忆。",
        "source": {
            "organization": "Academy of Nutrition and Dietetics, Dietitians of Canada, American College of Sports Medicine",
            "title": "Nutrition and Athletic Performance: Joint Position Statement",
            "url": "https://pubmed.ncbi.nlm.nih.gov/26891166/",
            "published": "2016-03-01",
            "verified_on": "2026-07-14",
        },
    },
    {
        "id": "sustainable-energy-balance",
        "applies_to": ["fat_loss", "recomposition", "body_recomposition", "general"],
        "principle": "优先选择可长期执行的饮食方式和能观察效果的小调整，不用补偿性断食或极端限制纠正一天的波动。",
        "planning_use": "保留正常餐次、饱腹感和训练执行，只采用当前已经确认的能量策略。",
        "boundary": "MealCircuit 未确认时，不推断热量缺口、减重速度或治疗目标。",
        "source": {
            "organization": "National Institute of Diabetes and Digestive and Kidney Diseases",
            "title": "Choosing a Safe & Successful Weight-loss Program",
            "url": "https://www.niddk.nih.gov/health-information/weight-management/choosing-a-safe-successful-weight-loss-program",
            "published": "current web guidance",
            "verified_on": "2026-07-14",
        },
    },
    {
        "id": "pregnancy-guided",
        "applies_to": ["pregnant"],
        "principle": "孕期规划必须遵循仍有效的专业指导和孕期食品安全边界，不能套用普通减重或增肌计划。",
        "planning_use": "工作台只在专业指导范围内协助；指导缺失或过期时明确提示，而不是自行给出处方。",
        "boundary": "MealCircuit 不做诊断、不设定孕期体重目标，也不替代产前医疗照护。",
        "source": {
            "organization": "American College of Obstetricians and Gynecologists",
            "title": "Healthy Eating During Pregnancy",
            "url": "https://www.acog.org/womens-health/faqs/healthy-eating-during-pregnancy",
            "published": "2026-03",
            "reviewed": "2025-12",
            "verified_on": "2026-07-14",
        },
    },
    {
        "id": "life-stage-boundary",
        "applies_to": ["minor", "breastfeeding", "clinician_guided"],
        "principle": "生长发育、孕期、哺乳期和治疗性饮食需要对应生命阶段或专业人员的具体指导，普通成人目标不得泄漏进这些模式。",
        "planning_use": "只做事实观察或采用仍有效的专业指导，并保留未知项。",
        "boundary": "不得为这些用户自行合成普通成人的能量、蛋白质或体重变化目标。",
        "source": {
            "organization": "U.S. Departments of Agriculture and Health and Human Services",
            "title": "Dietary Guidelines for Americans, 2025-2030",
            "url": "https://cdn.realfood.gov/DGA_508.pdf",
            "published": "2026",
            "verified_on": "2026-07-14",
        },
    },
)


def applicable_knowledge(personalization: dict) -> dict:
    profile = ((personalization.get("profile") or {}).get("profile_json") or {})
    safety = personalization.get("safety") or {}
    tags = {"general", str(profile.get("life_stage") or "adult"), str(safety.get("mode") or "setup_required")}
    training = profile.get("training") or {}
    if training.get("frequency_per_week") or training.get("types"):
        tags.add("training")
    for goal in personalization.get("goals") or []:
        goal_type = str((goal.get("goal_json") or {}).get("type") or "")
        if goal_type:
            tags.add(goal_type)
    selected = [deepcopy(item) for item in _PRINCIPLES if tags.intersection(item["applies_to"])]
    payload = {
        "version": KNOWLEDGE_PACK_VERSION,
        "runtime_network_access": False,
        "selection_tags": sorted(tags),
        "principles": selected,
    }
    payload["sha256"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return payload
