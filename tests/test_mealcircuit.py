from __future__ import annotations

import copy
import io
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr
from datetime import date, timedelta
from pathlib import Path

from mealcircuit import adaptive, ai, checkins, personalization, service
from mealcircuit import storage
from mealcircuit.configuration import configuration_status, initialize_private_home
from mealcircuit.db import connect, init_db
from mealcircuit.menu_semantics import compare_signatures, semantic_signature
from mealcircuit.migration import apply_migration, migration_preview
from mealcircuit.server import Handler, ThreadingHTTPServer, origin_matches_host, parse_host_endpoint
from mealcircuit.storage import db_path, resolve_data_path, upload_root
from mealcircuit.validation import ValidationError, validate_daily_review_result
from tools.release_check import scan as release_scan


TEST_SETTINGS = {
    "meal_environment": "测试用餐环境",
    "protein_target_g": [100, 130],
    "portion_method": "测试份量方式",
    "missing_training_default": "按普通日生成",
    "compensation_boundary": "不跳餐、不清零主食、不极端压低热量",
}

HOME_COOKING = {
    "enabled": True,
    "region": "china",
    "meal_scope": "dinner",
    "servings": 1,
    "weekday_time_limit_minutes": 25,
    "equipment": ["rice_cooker", "stovetop_pan", "stovetop_pot", "refrigerator"],
    "recipe_detail": "beginner_card",
    "rotation_window_days": 3,
    "reuse_policy": "reuse_ingredients_rotate_dishes",
    "flavor_preferences": ["bold", "sour_spicy", "tomato", "xiaomi_chili"],
    "online_purchase_mode": "spec_and_search_keywords",
    "food_exclusions": [],
}
HOME_COOKING_LUNCH_DINNER = {
    **HOME_COOKING,
    "meal_scope": "lunch_and_dinner",
    "meal_modes": {
        "breakfast": "quick_assembly",
        "lunch": "home_cook",
        "dinner": "home_cook",
    },
}


def configure_private_home(path: Path) -> dict[str, str | None]:
    old = {key: os.environ.get(key) for key in (
        "MEALCIRCUIT_HOME", "MEALCIRCUIT_DB", "MEALCIRCUIT_AI_PROVIDER",
        "MEALCIRCUIT_AI_MODEL", "MEALCIRCUIT_OPENAI_API_KEY",
        "MEALCIRCUIT_ANTHROPIC_API_KEY", "MEALCIRCUIT_DEEPSEEK_API_KEY", "MEALCIRCUIT_AI_TIMEOUT_SECONDS",
        "MEALCIRCUIT_AI_MAX_OUTPUT_TOKENS",
    )}
    os.environ["MEALCIRCUIT_HOME"] = str(path)
    os.environ["MEALCIRCUIT_DB"] = str(path / "mealcircuit.db")
    for key in old:
        if key not in {"MEALCIRCUIT_HOME", "MEALCIRCUIT_DB"}:
            os.environ.pop(key, None)
    path.mkdir(parents=True, exist_ok=True)
    (path / "settings.json").write_text(json.dumps(TEST_SETTINGS, ensure_ascii=False), encoding="utf-8")
    (path / "doctrine.private.md").write_text("# 系统最高目标\n\n测试私人总纲。\n", encoding="utf-8")
    return old


def restore_environment(old: dict[str, str | None]) -> None:
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def complete_standard_test_onboarding() -> dict:
    session = personalization.start_onboarding()
    payloads = {
        "welcome": {"privacy_ack": True},
        "goals": {
            "primary_goal": "eating_consistency",
            "secondary_goals": [],
            "motivation": "测试完整生成路径。",
            "success_metrics": ["execution_rate"],
        },
        "baseline": {
            "age_years": 30,
            "height_cm": None,
            "weight_kg": None,
            "physiological_input": "unspecified",
            "activity_level": "moderate",
        },
        "safety": {
            "life_stage": "adult",
            "therapeutic_diet": False,
            "medication_affects_nutrition": False,
            "eating_disorder_risk": False,
            "rapid_unexplained_change": False,
            "severe_persistent_symptoms": False,
            "severe_allergy_management": False,
        },
        "training": {"types": [], "frequency_per_week": 0},
        "constraints": {
            "meal_environment": TEST_SETTINGS["meal_environment"],
            "portion_method": TEST_SETTINGS["portion_method"],
            "cooking_time_minutes": 25,
            "equipment": [],
            "food_exclusions": [],
            "preferences": [],
            "question_budget": 2,
        },
    }
    current = session
    for step, payload in payloads.items():
        current = personalization.save_onboarding_step(current["id"], step, payload, current["version"])
    return personalization.complete_onboarding(
        current["id"],
        current["version"],
        {"accept_profile": True, "accept_strategy": True, "planning_mode": "portion_guided"},
    )


def nutrition(low=1, high=2):
    return {
        "energy_kcal": [low, high], "protein_g": [low, high],
        "carbs_g": [low, high], "fat_g": [low, high],
    }


def daily_review_result(review_date: str, unsafe: bool = False):
    tomorrow = (date.fromisoformat(review_date) + timedelta(days=1)).isoformat()
    return {
        "system_status": "observe",
        "facts": ["今日已有多个蛋白来源"],
        "inferences": ["<script>推断</script>" if unsafe else "额外蛋白重复叠加"],
        "core_advice": ["明天不跳餐，撤掉重复加餐并恢复标准份量"],
        "do_not_adjust": ["不清零主食"],
        "risk_signals": ["高钠可能造成短期水重"],
        "priority_food_decisions": [],
        "tomorrow_menu": {
            "date": tomorrow,
            "environment": "测试用餐环境",
            "protein_target_g": [100, 130],
            "meals": [
                {"name": "早餐", "foods": ["鸡蛋2个", "无糖豆浆"], "portion_guidance": "半个至1个馒头", "protein_g": [20, 25], "substitutions": ["牛奶替换豆浆"]},
                {"name": "午餐", "foods": ["瘦肉主菜", "米饭", "蔬菜"], "portion_guidance": "1.5掌蛋白、1拳米饭、2拳蔬菜", "protein_g": [45, 55], "substitutions": ["鱼类替换瘦肉"]},
                {"name": "晚餐", "foods": ["鸡肉", "主食", "蔬菜"], "portion_guidance": "1.5掌蛋白、0.5–1拳主食、2拳蔬菜", "protein_g": [45, 55], "substitutions": ["豆腐加鸡蛋替换鸡肉"]},
            ],
            "conditional_snack": {"condition": "未达到测试目标下界时才吃", "options": ["测试加餐A", "测试加餐B"]},
            "training_adjustment": "训练前后增加0.5–1拳主食，不额外堆鸡胸肉。",
            "gut_adjustment": "辣、麻、酸、油降一级，保留鲜香，不喝汤汁。",
        },
        "one_line_review": "达到个人目标下界后停止机械加餐。",
    }


def home_cooking_review_result(review_date: str, dish_key="tomato_chicken", flavor="tomato_sour_spicy"):
    result = daily_review_result(review_date)
    menu_date = date.fromisoformat(result["tomorrow_menu"]["date"])
    meals = result["tomorrow_menu"]["meals"]
    meals[0]["mode"] = "quick_assembly"
    meals[1]["mode"] = "eat_out"
    meals[1]["eat_out_guidance"] = {
        "protein_anchor": "选择可见的瘦肉、鱼虾、鸡蛋或豆制品",
        "staple": "保留约1拳主食",
        "vegetables": "至少1–2拳蔬菜",
        "sauce_rule": "酱汁分开，不喝油汤",
        "fallback": "蛋白明显不足时再用一份便捷蛋白补位",
    }
    meals[2]["mode"] = "home_cook"
    meals[2]["recipe_card"] = {
        "title": "番茄小米椒鸡肉",
        "servings": 1,
        "active_minutes": 15,
        "total_minutes": 22,
        "cookware": ["stovetop_pan"],
        "ingredients": [
            {"name": "鸡胸肉", "amount": "180g", "prep": "切成约2cm小块"},
            {"name": "番茄", "amount": "1个", "prep": "切块"},
        ],
        "seasonings": [
            {"name": "小米椒", "amount": "半根", "timing": "关火前加入"},
            {"name": "生抽", "amount": "1茶匙", "timing": "番茄出汁后加入"},
        ],
        "steps": [
            {"instruction": "鸡肉下锅摊开", "minutes": 4, "heat": "中火", "done_signal": "表面全部变白"},
            {"instruction": "加入番茄翻炒", "minutes": 6, "heat": "中小火", "done_signal": "番茄明显出汁"},
        ],
        "failure_rescue": ["锅太干时加2汤匙清水，不继续加油"],
        "cleanup": "1口炒锅、1块砧板和1把刀",
        "gut_fallback": "去掉小米椒，番茄减半并加少量清水保留鲜味。",
    }
    result["tomorrow_menu"].update({
        "shopping_list": [{
            "name": "番茄", "amount": "3个", "purpose": "明日晚餐及后两日复用", "required": True,
            "selection_guide": "表皮完整、拿起有重量感", "storage": "室温避光，熟透后冷藏",
        }],
        "online_options": [{
            "category": "低油番茄调味", "selection_criteria": ["配料表短", "无明显糖油前排"],
            "package_size": "200g以内小包装", "search_keywords": ["无添加糖番茄碎小包装"],
            "pairs_with": ["鸡肉", "豆腐"], "skip_if": "能稳定买到新鲜番茄时跳过",
        }],
        "reuse_plan": {
            "horizon_days": 3,
            "items": [{
                "ingredient": "番茄", "tomorrow_use": "番茄小米椒鸡肉",
                "later_uses": [
                    {"date": (menu_date + timedelta(days=1)).isoformat(), "use": "番茄豆腐汤"},
                    {"date": (menu_date + timedelta(days=2)).isoformat(), "use": "番茄炒蛋"},
                ],
                "storage": "未切常温，切开后密封冷藏并在次日用完",
            }],
        },
        "rotation": {
            "dish_key": dish_key, "primary_protein": "chicken", "primary_vegetable": "tomato",
            "flavor_profile": flavor, "technique": "stir_fry",
        },
    })
    return result


def change_home_dinner(
    result: dict,
    *,
    title: str,
    protein: str,
    vegetable: str,
    seasoning: str,
    instruction: str,
    dish_key: str,
    flavor: str,
    technique: str,
) -> dict:
    dinner = next(meal for meal in result["tomorrow_menu"]["meals"] if meal["name"] == "晚餐")
    dinner["foods"] = [title, "主食", "蔬菜"]
    dinner["recipe_card"]["title"] = title
    dinner["recipe_card"]["ingredients"] = [
        {"name": protein, "amount": "一人份", "prep": "切成易熟大小"},
        {"name": vegetable, "amount": "1份", "prep": "洗净切块"},
    ]
    dinner["recipe_card"]["seasonings"] = [
        {"name": seasoning, "amount": "1茶匙", "timing": "烹饪中加入"}
    ]
    dinner["recipe_card"]["steps"] = [
        {"instruction": instruction, "minutes": 10, "heat": "中火", "done_signal": "食材熟透"}
    ]
    result["tomorrow_menu"]["rotation"] = {
        "dish_key": dish_key,
        "primary_protein": protein,
        "primary_vegetable": vegetable,
        "flavor_profile": flavor,
        "technique": technique,
    }
    return result


def change_breakfast_and_lunch(result: dict) -> dict:
    meals = {meal["name"]: meal for meal in result["tomorrow_menu"]["meals"]}
    meals["早餐"]["foods"] = ["全麦面包", "原味酸奶", "蓝莓"]
    meals["早餐"]["substitutions"] = ["燕麦替换面包"]
    meals["午餐"]["foods"] = ["清蒸鱼", "杂粮饭", "绿叶菜"]
    meals["午餐"]["substitutions"] = ["豆腐替换鱼类"]
    if meals["午餐"].get("eat_out_guidance"):
        meals["午餐"]["eat_out_guidance"]["protein_anchor"] = "优先选择清蒸鱼或豆腐"
    return result


def lunch_dinner_home_cooking_result(review_date: str) -> dict:
    result = home_cooking_review_result(review_date)
    menu = result["tomorrow_menu"]
    lunch = next(meal for meal in menu["meals"] if meal["name"] == "午餐")
    dinner = next(meal for meal in menu["meals"] if meal["name"] == "晚餐")
    dinner["rotation"] = menu.pop("rotation")
    lunch["mode"] = "home_cook"
    lunch.pop("eat_out_guidance", None)
    lunch["foods"] = ["番茄鸡蛋", "米饭", "蔬菜"]
    lunch["recipe_card"] = copy.deepcopy(dinner["recipe_card"])
    lunch["recipe_card"].update({
        "title": "番茄鸡蛋午餐",
        "active_minutes": 12,
        "total_minutes": 18,
        "ingredients": [
            {"name": "鸡蛋", "amount": "2个", "prep": "打散"},
            {"name": "番茄", "amount": "1个", "prep": "切块"},
        ],
        "steps": [
            {"instruction": "鸡蛋炒至凝固后盛出", "minutes": 4, "heat": "中火", "done_signal": "蛋液完全凝固"},
            {"instruction": "番茄出汁后倒回鸡蛋", "minutes": 5, "heat": "中小火", "done_signal": "汤汁均匀裹住鸡蛋"},
        ],
    })
    lunch["rotation"] = {
        "dish_key": "tomato_egg_lunch",
        "primary_protein": "egg",
        "primary_vegetable": "tomato",
        "flavor_profile": "tomato_savory",
        "technique": "stir_fry",
    }
    return result


def lunch_eat_out_dinner_home_result(review_date: str) -> dict:
    result = lunch_dinner_home_cooking_result(review_date)
    menu = result["tomorrow_menu"]
    menu["environment"] = "早餐快速组装、午餐外食、晚餐独居下厨"
    lunch = next(meal for meal in menu["meals"] if meal["name"] == "午餐")
    lunch["mode"] = "eat_out"
    lunch.pop("recipe_card", None)
    lunch.pop("rotation", None)
    lunch["foods"] = ["外食选择一份可见主蛋白", "正常份主食", "至少一份蔬菜"]
    lunch["portion_guidance"] = "1–1.5掌蛋白、1拳主食、1–2拳蔬菜；酱汁分开。"
    lunch["eat_out_guidance"] = {
        "protein_anchor": "瘦肉、鱼虾、鸡蛋或豆制品",
        "staple": "约1拳米饭或等量主食",
        "vegetables": "至少1–2拳",
        "sauce_rule": "酱汁分开，不喝油汤",
        "fallback": "蛋白明显不足时再使用1包即食鸡胸",
    }
    return result


def add_carryover_decisions(result: dict, context: dict, decision: str = "use") -> dict:
    result["ingredient_carryover_decisions"] = [
        {
            "carryover_id": item["id"],
            "ingredient": item["ingredient"],
            "decision": decision,
            "reason": "测试中承接上一轮可能剩余食材，避免重复采购或浪费。",
            "planned_use": item["planned_use"],
        }
        for item in context["ingredient_carryover_obligations"]
    ]
    return result


class MealCircuitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_environment = configure_private_home(Path(self.temp.name))
        init_db()
        complete_standard_test_onboarding()

    def tearDown(self):
        restore_environment(self.old_environment)
        self.temp.cleanup()

    def test_material_task_persists(self):
        task = service.create_material_task("鸡胸肉 500g，米 200g")
        self.assertEqual(task["status"], "pending")
        self.assertEqual(service.get_task(task["id"])["original_input"], "鸡胸肉 500g，米 200g")

    def test_photo_task_safely_stores_image(self):
        task = service.create_photo_task(io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"safe"), "午餐")
        path = resolve_data_path(task["image_path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent.resolve(), upload_root().resolve())
        with self.assertRaises(ValidationError):
            service.create_photo_task(io.BytesIO(b"not-an-image"))

    def test_food_crud_search_and_soft_delete_history(self):
        food = service.create_food({
            "name": "测试燕麦", "brand": "测试品牌", "basis": "100g", "energy_kcal": 380,
            "protein_g": 13, "carbs_g": 68, "fat_g": 7, "serving_unit": "", "source_url": "",
            "package_photo_path": None, "notes": "包装录入",
        })
        self.assertEqual(len(service.list_foods("燕麦")), 1)
        updated = service.update_food(food["id"], {**food, "protein_g": 14})
        self.assertEqual(updated["protein_g"], 14)
        service.delete_food(food["id"])
        self.assertEqual(service.list_foods("燕麦"), [])
        conn = sqlite3.connect(db_path())
        try:
            events = [row[0] for row in conn.execute("SELECT event FROM food_item_history WHERE food_id=? ORDER BY created_at,rowid", (food["id"],))]
        finally:
            conn.close()
        self.assertEqual(events, ["create", "update", "delete"])

    def test_priority_food_upsert_and_daily_context_requires_decision(self):
        food_data = {
            "name": "测试全麦面包", "brand": "测试品牌", "basis": "100g",
            "energy_kcal": 245, "protein_g": 9.7, "carbs_g": 48.9, "fat_g": 0,
            "fiber_g": 3.5, "sodium_mg": 253, "serving_unit": "", "category": "staple",
            "menu_priority": "high", "default_portion": "50–100g",
            "usage_rule": "需要便捷主食时优先", "source_key": "test-bread",
            "source_url": "", "package_photo_path": "labels/test.png", "notes": "",
        }
        food = service.upsert_food_by_source(food_data)
        same = service.upsert_food_by_source({**food_data, "notes": "已更新"})
        self.assertEqual(food["id"], same["id"])
        self.assertEqual(len(service.list_foods()), 1)
        self.assertEqual(service.list_priority_foods()[0]["default_portion"], "50–100g")
        today = date.today().isoformat()
        service.add_daily_record(today, "今天吃了食堂餐")
        context = service.daily_review_context(today)
        self.assertEqual(context["priority_foods"][0]["id"], food["id"])
        result = daily_review_result(today)
        with self.assertRaises(ValidationError):
            service.complete_daily_review(today, result)
        result["priority_food_decisions"] = [{"food_id": food["id"], "decision": "use", "reason": "早餐需要便捷主食"}]
        self.assertEqual(service.complete_daily_review(today, result)["status"], "completed")

    def test_context_contains_rule_recent_data_food_and_memory(self):
        food = service.create_food({"name": "鸡胸肉", "brand": "", "basis": "100g", "energy_kcal": 120, "protein_g": 23, "carbs_g": 0, "fat_g": 2, "serving_unit": "", "source_url": "", "package_photo_path": None, "notes": ""})
        service.add_daily_record(date.today().isoformat(), "今天训练后饥饿 7/10")
        service.add_daily_record((date.today() - timedelta(days=20)).isoformat(), "旧记录")
        service.add_memory("gut_trigger", "空腹大量辣椒会胃痛", "连续两次记录")
        service.add_adjustment("训练日前后保留主食", "训练表现")
        task = service.create_material_task("鸡胸肉 300g")
        context = service.task_context(task["id"])
        self.assertIn("系统最高目标", context["doctrine"]["content"])
        self.assertEqual(len(context["recent_records"]), 1)
        self.assertEqual(context["food_library_matches"][0]["id"], food["id"])
        self.assertEqual(len(context["long_term_memories"]), 1)
        self.assertEqual(len(context["current_adjustments"]), 1)

    def test_complete_photo_validation_and_no_overwrite(self):
        task = service.create_photo_task(io.BytesIO(b"\xff\xd8\xff" + b"photo"))
        with self.assertRaises(ValidationError):
            service.complete_task(task["id"], {"summary": "缺字段"})
        valid = {"summary": "区间估算", "candidates": [{"name": "米饭", "portion_range": "150–250g", "nutrition": nutrition(), "confidence": 0.7}], "unknowns": ["用油不可见"], "advice": ["按中位数暂记"]}
        done = service.complete_task(task["id"], valid)
        self.assertEqual(done["status"], "completed")
        with self.assertRaises(ValidationError):
            service.complete_task(task["id"], valid)
        corrected = service.add_correction(task["id"], {"text": "实际米饭约 180g"})
        self.assertEqual(len(corrected["corrections"]), 1)
        self.assertEqual(corrected["result_json"], valid)

    def test_pending_task_input_edit_history_validation_and_context(self):
        chicken = service.create_food({"name": "鸡胸肉", "brand": "", "basis": "100g", "energy_kcal": 120, "protein_g": 23, "carbs_g": 0, "fat_g": 2, "serving_unit": "", "source_url": "", "package_photo_path": None, "notes": ""})
        egg = service.create_food({"name": "鸡蛋", "brand": "", "basis": "100g", "energy_kcal": 140, "protein_g": 13, "carbs_g": 1, "fat_g": 9, "serving_unit": "", "source_url": "", "package_photo_path": None, "notes": ""})
        task = service.create_material_task("鸡胸肉 300g")
        updated = service.update_task_input(task["id"], "  鸡蛋 6 个  ", 1)
        self.assertEqual(updated["original_input"], "鸡蛋 6 个")
        self.assertEqual(updated["input_version"], 2)
        self.assertEqual(updated["input_history"][0]["version"], 1)
        self.assertEqual(updated["input_history"][0]["input_text"], "鸡胸肉 300g")
        unchanged = service.update_task_input(task["id"], "鸡蛋 6 个", 2)
        self.assertEqual(len(unchanged["input_history"]), 1)
        context = service.task_context(task["id"])
        self.assertEqual(context["task"]["original_input"], "鸡蛋 6 个")
        self.assertEqual([item["id"] for item in context["food_library_matches"]], [egg["id"]])
        self.assertNotIn(chicken["id"], [item["id"] for item in context["food_library_matches"]])
        with self.assertRaises(ValidationError):
            service.update_task_input(task["id"], "鸡蛋 8 个", 1)
        with self.assertRaises(ValidationError):
            service.update_task_input(task["id"], "   ", 2)

        photo = service.create_photo_task(io.BytesIO(b"GIF89a" + b"photo"), "训练后")
        cleared = service.update_task_input(photo["id"], "", 1)
        self.assertEqual(cleared["original_input"], "")
        self.assertEqual(cleared["input_history"][0]["input_text"], "训练后")

        result = {
            "summary": "可分两份", "combinations": ["鸡蛋配主食与蔬菜"],
            "batch_nutrition": nutrition(), "per_serving_nutrition": nutrition(),
            "gaps": ["蔬菜未知"], "risks": ["数量粗略"], "minimal_adjustments": ["补充蔬菜"],
        }
        service.complete_task(task["id"], result)
        with self.assertRaises(ValidationError):
            service.update_task_input(task["id"], "鸡蛋 8 个", 2)

    def test_complete_material_result(self):
        task = service.create_material_task("鸡蛋 6 个")
        result = {
            "summary": "可分两份", "combinations": ["鸡蛋配主食与蔬菜"],
            "batch_nutrition": nutrition(), "per_serving_nutrition": nutrition(),
            "gaps": ["蔬菜未知"], "risks": ["数量粗略"], "minimal_adjustments": ["补充蔬菜"],
        }
        self.assertEqual(service.complete_task(task["id"], result)["result_json"], result)

    def test_invalid_food_and_result_are_rejected(self):
        with self.assertRaises(ValidationError):
            service.create_food({"name": "坏数据", "basis": "100g", "energy_kcal": -1})
        task = service.create_photo_task(io.BytesIO(b"GIF89a" + b"photo"))
        invalid = {"summary": "x", "candidates": [{"name": "x", "portion_range": "x", "nutrition": nutrition(), "confidence": 1.2}], "unknowns": [], "advice": []}
        with self.assertRaises(ValidationError):
            service.complete_task(task["id"], invalid)

    def test_generate_requires_user_api_environment_and_preserves_pending(self):
        task = service.create_material_task("鸡蛋 2 个")
        env = os.environ.copy()
        for key in (
            "MEALCIRCUIT_AI_PROVIDER", "MEALCIRCUIT_AI_MODEL",
            "MEALCIRCUIT_OPENAI_API_KEY", "MEALCIRCUIT_ANTHROPIC_API_KEY", "MEALCIRCUIT_DEEPSEEK_API_KEY",
        ):
            env.pop(key, None)
        env["PYTHONUTF8"] = "1"
        completed = subprocess.run(
            [sys.executable, "-m", "mealcircuit.agent_cli", "generate", task["id"]],
            cwd=Path(__file__).resolve().parent.parent,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("MEALCIRCUIT_AI_PROVIDER", completed.stderr)
        self.assertEqual(service.get_task(task["id"])["status"], "pending")

    def test_openai_generate_photo_uses_image_payload_and_validates_result(self):
        task = service.create_photo_task(io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"ai-photo"), "午餐")
        valid = {
            "summary": "照片可见米饭和肉类",
            "candidates": [{"name": "米饭配肉", "portion_range": "一盘", "nutrition": nutrition(), "confidence": 0.8}],
            "unknowns": ["用油不可见"],
            "advice": ["按区间记录"],
        }
        payloads = []

        def transport(url, headers, payload, timeout):
            payloads.append(payload)
            return {"output": [{"content": [{"text": json.dumps(valid, ensure_ascii=False)}]}]}

        provider = ai.OpenAIProvider(
            ai.AIConfig("openai", "test-openai-model", "test-key"),
            transport=transport,
        )
        completed = service.generate_task_result(task["id"], provider)
        self.assertEqual(completed["status"], "completed")
        content = payloads[0]["input"][0]["content"]
        self.assertEqual(content[0]["type"], "input_image")
        self.assertTrue(content[0]["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(payloads[0]["text"]["format"]["type"], "json_schema")

    def test_anthropic_generate_daily_uses_forced_tool_result(self):
        today = date.today().isoformat()
        service.add_daily_record(today, "今天记录待复盘")
        valid = daily_review_result(today)
        payloads = []

        def transport(url, headers, payload, timeout):
            payloads.append(payload)
            return {
                "content": [{
                    "type": "tool_use",
                    "name": "submit_mealcircuit_result",
                    "input": valid,
                }]
            }

        provider = ai.AnthropicProvider(
            ai.AIConfig("anthropic", "test-claude-model", "test-key"),
            transport=transport,
        )
        completed = service.generate_daily_review(today, provider)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(payloads[0]["tool_choice"], {"type": "tool", "name": "submit_mealcircuit_result"})
        self.assertEqual(payloads[0]["tools"][0]["input_schema"]["type"], "object")

    def test_deepseek_generate_material_uses_chat_json_mode_and_rejects_photo(self):
        task = service.create_material_task("鸡蛋 2 个")
        valid = {
            "summary": "可分一餐使用", "combinations": ["鸡蛋配主食和蔬菜"],
            "batch_nutrition": nutrition(), "per_serving_nutrition": nutrition(),
            "gaps": ["蔬菜未知"], "risks": ["数量粗略"], "minimal_adjustments": ["补一份蔬菜"],
        }
        payloads = []

        def transport(url, headers, payload, timeout):
            payloads.append(payload)
            return {"choices": [{"message": {"content": json.dumps(valid, ensure_ascii=False)}}]}

        provider = ai.DeepSeekProvider(
            ai.AIConfig("deepseek", "deepseek-v4-flash", "test-key"),
            transport=transport,
        )
        completed = service.generate_task_result(task["id"], provider)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(payloads[0]["response_format"], {"type": "json_object"})
        self.assertEqual(payloads[0]["thinking"], {"type": "disabled"})

        photo = service.create_photo_task(io.BytesIO(b"GIF89a" + b"photo"))
        with self.assertRaisesRegex(ValidationError, "图片输入"):
            service.generate_task_result(photo["id"], provider)
        self.assertEqual(service.get_task(photo["id"])["status"], "pending")

    def test_generate_does_not_complete_when_model_json_is_invalid(self):
        task = service.create_material_task("鸡蛋 2 个")

        class InvalidClient:
            def generate(self, request):
                return {"summary": "缺字段"}

        with self.assertRaises(ValidationError):
            service.generate_task_result(task["id"], InvalidClient())
        self.assertEqual(service.get_task(task["id"])["status"], "pending")

    def test_daily_review_queue_context_complete_and_reopen_history(self):
        today = date.today().isoformat()
        first = service.add_daily_record(today, "早餐两个蛋")
        review = service.get_daily_review(today)
        self.assertEqual(review["status"], "pending")
        self.assertEqual(review["source_record_ids_json"], [first["id"]])
        service.add_daily_record(today, "补充：午餐鸡肉和米饭")
        self.assertEqual(len(service.list_daily_reviews()), 1)
        context = service.daily_review_context(today)
        self.assertEqual(context["settings"]["protein_target_g"], [100, 130])
        self.assertEqual(context["settings"]["meal_environment"], "测试用餐环境")
        self.assertEqual(len(context["recent_records"]), 2)
        completed = service.complete_daily_review(today, daily_review_result(today))
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result_version"], 1)
        replacement = daily_review_result(today)
        replacement["one_line_review"] = "未执行生成物可以直接修正，不污染正式历史。"
        replaced = service.complete_daily_review(today, replacement)
        self.assertEqual(replaced["result_version"], 1)
        self.assertEqual(replaced["history"], [])
        service.add_daily_record(today, "补充：晚间饥饿低")
        reopened = service.get_daily_review(today)
        self.assertEqual(reopened["status"], "pending")
        self.assertEqual(reopened["result_json"]["one_line_review"], replacement["one_line_review"])
        self.assertEqual(len(reopened["source_record_ids_json"]), 3)
        self.assertEqual(reopened["history"], [])
        self.assertEqual(reopened["revision_policy"]["mode"], "replaceable")

    def test_daily_review_rejects_missing_or_wrong_menu(self):
        today = date.today().isoformat()
        service.add_daily_record(today, "测试记录")
        with self.assertRaises(ValidationError):
            service.complete_daily_review(today, {"system_status": "observe"})
        invalid = daily_review_result(today)
        invalid["tomorrow_menu"]["protein_target_g"] = [200, 220]
        with self.assertRaises(ValidationError):
            service.complete_daily_review(today, invalid)

    def test_semantic_signature_rejects_renames_reordering_and_legacy_rotation(self):
        previous = home_cooking_review_result(date.today().isoformat())["tomorrow_menu"]["meals"][2]
        previous.pop("rotation", None)
        renamed = copy.deepcopy(previous)
        renamed["recipe_card"]["title"] = "鸡肉小米椒番茄"
        renamed["recipe_card"]["ingredients"].reverse()
        renamed["recipe_card"]["steps"].reverse()
        comparison = compare_signatures(
            semantic_signature(renamed), semantic_signature(previous), home_cook=True
        )
        self.assertTrue(comparison["duplicate"])
        self.assertTrue(comparison["near"])

        breakfast_a = {"mode": "quick_assembly", "foods": ["鸡蛋", "全麦面包", "牛奶"]}
        breakfast_b = {"mode": "quick_assembly", "foods": ["鸡蛋", "全麦面包", "原味酸奶"]}
        breakfast_comparison = compare_signatures(
            semantic_signature(breakfast_b), semantic_signature(breakfast_a), home_cook=False
        )
        self.assertFalse(breakfast_comparison["duplicate"])

    def test_replaceable_plan_is_updated_in_place_but_feedback_locks_history(self):
        today = date.today().isoformat()
        plan_date = (date.today() + timedelta(days=1)).isoformat()
        service.add_daily_record(today, "今天按计划记录")
        first = service.complete_daily_review(today, daily_review_result(today))
        plan_id = first["plan_version_id"]
        second_result = daily_review_result(today)
        second_result["one_line_review"] = "修正尚未执行的生成内容。"
        second = service.submit_daily_review(today, second_result)
        self.assertEqual(second["result_version"], 1)
        self.assertEqual(second["plan_version_id"], plan_id)
        self.assertEqual(second["history"], [])
        plan = adaptive.get_plan_for_date(plan_date)
        meal = plan["menu"]["meals"][0]
        adaptive.save_plan_feedback(
            plan_date, meal["plan_item_id"], "followed", expected_version=0
        )
        self.assertEqual(service.get_daily_review(today)["revision_policy"]["mode"], "locked")
        third_result = daily_review_result(today)
        third_result["one_line_review"] = "执行反馈后修订必须形成正式版本。"
        third = service.submit_daily_review(today, third_result)
        self.assertEqual(third["result_version"], 2)
        self.assertEqual(len(third["history"]), 1)
        with connect() as conn:
            plans = conn.execute(
                "SELECT status,result_version FROM plan_versions WHERE review_id=? ORDER BY result_version",
                (third["id"],),
            ).fetchall()
        self.assertEqual([(row["status"], row["result_version"]) for row in plans], [
            ("superseded", 1), ("published", 2)
        ])

    def test_past_plan_is_locked(self):
        review_date = (date.today() - timedelta(days=2)).isoformat()
        service.add_daily_record(review_date, "过去的真实记录")
        completed = service.complete_daily_review(review_date, daily_review_result(review_date))
        self.assertEqual(completed["revision_policy"]["mode"], "locked")
        self.assertIn("past_plan_date", completed["revision_policy"]["reasons"])

    def test_daily_generation_retries_semantic_rejections_without_persisting_candidates(self):
        previous_date = (date.today() - timedelta(days=1)).isoformat()
        today = date.today().isoformat()
        service.add_daily_record(previous_date, "前一天记录")
        service.complete_daily_review(previous_date, daily_review_result(previous_date))
        service.add_daily_record(today, "今天记录")
        repeated = daily_review_result(today)
        distinct = daily_review_result(today)
        meals = {meal["name"]: meal for meal in distinct["tomorrow_menu"]["meals"]}
        meals["早餐"]["foods"] = ["燕麦", "原味酸奶", "草莓"]
        meals["午餐"]["foods"] = ["清蒸鱼", "杂粮饭", "西兰花"]
        meals["晚餐"]["foods"] = ["豆腐汤", "红薯", "菠菜"]

        class Provider:
            provider_name = "test"
            model = "semantic-retry"

            def __init__(self):
                self.requests = []
                self.results = [copy.deepcopy(repeated), copy.deepcopy(repeated), distinct]

            def generate(self, request):
                self.requests.append(copy.deepcopy(request))
                return self.results.pop(0)

        provider = Provider()
        completed = service.generate_daily_review(today, provider)
        self.assertEqual(completed["result_version"], 1)
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(len(provider.requests[1].context["candidate_rejections"]), 1)
        with connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM plan_versions WHERE review_id=?", (completed["id"],)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM daily_review_history WHERE review_id=?", (completed["id"],)).fetchone()[0], 0)
            attempts = json.loads(conn.execute("SELECT validation_attempts_json FROM agent_runs WHERE id=?", (completed["agent_run_id"],)).fetchone()[0])
        self.assertEqual([item["status"] for item in attempts], ["rejected", "rejected", "valid"])

    def test_failed_regeneration_keeps_replaceable_current_plan_unchanged(self):
        today = date.today().isoformat()
        service.add_daily_record(today, "今天记录")
        original = service.complete_daily_review(today, daily_review_result(today))

        class InvalidProvider:
            def generate(self, request):
                return {"system_status": "缺少其余字段"}

        with self.assertRaises(ValidationError):
            service.generate_daily_review(today, InvalidProvider())
        current = service.get_daily_review(today)
        self.assertEqual(current["result_version"], 1)
        self.assertEqual(current["plan_version_id"], original["plan_version_id"])
        self.assertEqual(current["result_json"], original["result_json"])
        self.assertEqual(current["history"], [])

    def test_targeted_cleanup_collapses_only_unexecuted_generated_versions(self):
        today = date.today().isoformat()
        service.add_daily_record(today, "今天记录")
        current = service.complete_daily_review(today, daily_review_result(today))
        with connect() as conn:
            conn.execute("UPDATE daily_reviews SET result_version=5 WHERE id=?", (current["id"],))
            conn.execute("UPDATE plan_versions SET result_version=5 WHERE id=?", (current["plan_version_id"],))
            seeded = conn.execute("SELECT * FROM daily_reviews WHERE id=?", (current["id"],)).fetchone()
            menu_json = json.dumps(current["result_json"]["tomorrow_menu"], ensure_ascii=False)
            obsolete_result = service.capture_derived_result(
                conn,
                source_entity_id=current["id"],
                source_kind="daily_review",
                result_version=2,
                result={"mistaken": True},
                provenance={},
            )
            for version in range(1, 5):
                archived = dict(seeded)
                archived["result_version"] = version
                service._archive_review(conn, archived, service.now(), "mistaken_generated_candidate")
                conn.execute(
                    """INSERT INTO plan_versions(
                           id,review_id,result_version,plan_date,status,schema_version,menu_json,
                           source_manifest_json,context_hash,policy_version,validator_version,agent_run_id,created_at
                       ) VALUES(?,?,?,?, 'superseded',2,?,?,?,?,?,?,?)""",
                    (
                        f"stale_plan_{version}", current["id"], version,
                        current["result_json"]["tomorrow_menu"]["date"], menu_json,
                        "{}", "stale", "test", "validator-v2", None, service.now(),
                    ),
                )
        preview = service.cleanup_generated_review_history(
            today, expected_current_version=5
        )
        self.assertFalse(preview["applied"])
        self.assertEqual(preview["history_versions"], [1, 2, 3, 4])
        applied = service.cleanup_generated_review_history(
            today, apply=True, expected_current_version=5
        )
        self.assertTrue(applied["applied"])
        self.assertIn(obsolete_result.entity_id, applied["tombstoned_result_entities"])
        cleaned = service.get_daily_review(today)
        self.assertEqual(cleaned["result_version"], 1)
        self.assertEqual(cleaned["history"], [])
        with connect() as conn:
            plans = conn.execute(
                "SELECT id,result_version,status FROM plan_versions WHERE review_id=?",
                (current["id"],),
            ).fetchall()
            obsolete_head = conn.execute(
                """SELECT r.deleted FROM entity_heads h JOIN domain_revisions r
                   ON r.revision_id=h.revision_id WHERE h.entity_id=?""",
                (obsolete_result.entity_id,),
            ).fetchone()
        self.assertEqual([(row["id"], row["result_version"], row["status"]) for row in plans], [
            (current["plan_version_id"], 1, "published")
        ])
        self.assertEqual(obsolete_head["deleted"], 1)

    def test_checkin_draft_publish_and_day_context(self):
        today = date.today().isoformat()
        first = service.save_checkin_answer(today, "weight", "measured", "yes", 0)
        self.assertTrue(first["has_draft"])
        self.assertEqual(first["version"], 0)
        with self.assertRaises(ValidationError):
            service.daily_review_context(today)
        service.save_checkin_answer(today, "weight", "weight_kg", "72.4", 0)
        ready = service.save_checkin_answer(today, "weight", "measurement_context", "morning_fasted", 0)
        self.assertTrue(ready["ready"])
        published = service.complete_checkin_module(today, "weight", 0)
        self.assertEqual(published["version"], 1)
        self.assertEqual(published["answers_json"]["weight_kg"], 72.4)
        review = service.get_daily_review(today)
        self.assertEqual(review["source_record_ids_json"], [])
        self.assertEqual(review["source_checkin_versions_json"], {"weight": 1})
        context = service.daily_review_context(today)
        self.assertEqual(context["target_checkin"]["modules"][0]["summary"], "72.4 kg · 晨起空腹")
        self.assertEqual(context["recent_checkins"][0]["version"], 1)
        self.assertNotIn("draft_json", context["recent_checkins"][0])

    def test_dashboard_snapshot_is_read_only_and_preserves_unknowns(self):
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        snapshot = service.dashboard_snapshot(today)
        self.assertEqual(snapshot["daily"]["status"], "unrecorded")
        self.assertEqual(len(snapshot["trend"]), 14)
        conn = sqlite3.connect(db_path())
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM daily_reviews").fetchone()[0], 0)
        finally:
            conn.close()

        service.save_checkin_answer(today, "weight", "measured", "yes", 0)
        service.save_checkin_answer(today, "weight", "weight_kg", "72.4", 0)
        service.save_checkin_answer(today, "weight", "measurement_context", "morning_fasted", 0)
        service.complete_checkin_module(today, "weight", 0)
        service.save_checkin_answer(today, "hunger", "hunger_level", "4", 0)
        service.skip_checkin_module(yesterday, "gut", 0)

        snapshot = service.dashboard_snapshot(today)
        self.assertEqual(snapshot["trend"][-1]["modules"]["weight"]["weight_kg"], 72.4)
        self.assertNotIn("hunger", snapshot["trend"][-1]["modules"])
        self.assertEqual(snapshot["trend"][-2]["modules"]["gut"]["status"], "skipped")
        self.assertIsNone(snapshot["tomorrow_menu"])
        self.assertTrue(any(item["kind"] == "review" for item in snapshot["queue"]))

    def test_checkin_branch_update_history_and_stale_version(self):
        today = date.today().isoformat()
        answers = (
            ("trained", "yes"),
            ("training_types", ["strength"]),
            ("body_parts", ["chest", "biceps"]),
            ("duration", "60_90"),
            ("effort", "normal"),
        )
        for question_id, value in answers:
            service.save_checkin_answer(today, "training", question_id, value, 0)
        service.complete_checkin_module(today, "training", 0)
        changed = service.save_checkin_answer(today, "training", "trained", "no", 1)
        self.assertEqual(changed["active_answers"], {"trained": "no"})
        service.save_checkin_answer(today, "training", "rest_reason", "rest_day", 1)
        updated = service.complete_checkin_module(today, "training", 1)
        self.assertEqual(updated["version"], 2)
        self.assertEqual(len(updated["history"]), 1)
        self.assertEqual(updated["history"][0]["answers_json"]["body_parts"], ["chest", "biceps"])
        with self.assertRaises(ValidationError):
            service.save_checkin_answer(today, "training", "trained", "yes", 1)

    def test_checkin_all_module_schemas_skip_settings_and_future_date(self):
        valid = {
            "weight": {"measured": "no"},
            "training": {"trained": "no", "rest_reason": "recovery"},
            "hunger": {"hunger_level": "3", "hunger_time": "afternoon", "satiety": "comfortable", "cravings": "none"},
            "sleep": {"sleep_duration": 7.5, "sleep_quality": "4", "awakenings": "once", "morning_energy": "okay"},
            "gut": {"gut_state": "symptoms", "symptoms": ["bloating"], "severity": "mild", "timing": ["after_meal"], "bowel_state": "normal"},
        }
        for module_key, answers in valid.items():
            self.assertEqual(checkins.validate_module_answers(module_key, answers), answers)
        other_gut = {**valid["gut"], "symptoms": {"values": ["other"], "other_text": "餐后轻微绞痛"}}
        self.assertEqual(checkins.validate_module_answers("gut", other_gut), other_gut)
        invalid_other = {**valid["gut"], "symptoms": ["other"]}
        with self.assertRaises(ValidationError):
            checkins.validate_module_answers("gut", invalid_other)
        with self.assertRaises(ValidationError):
            checkins.validate_module_answers("gut", {"gut_state": "none", "severity": "severe"})
        today = date.today().isoformat()
        skipped = service.skip_checkin_module(today, "gut", 0)
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(skipped["summary"], "用户选择今天不提供")
        settings = service.checkin_module_settings()
        reordered = []
        for item in reversed(settings):
            reordered.append({
                "module_key": item["module_key"],
                "enabled": item["module_key"] != "weight",
                "frequency": "optional" if item["module_key"] == "gut" else "daily",
            })
        updated = service.update_checkin_module_settings(reordered)
        self.assertEqual(updated[0]["module_key"], "gut")
        state = service.get_checkin_state(today)
        self.assertEqual(state["coverage"]["due"], 3)
        self.assertEqual(state["coverage"]["handled"], 0)
        with self.assertRaises(ValidationError):
            service.get_checkin_state((date.today() + timedelta(days=1)).isoformat())

    def test_checkin_requeues_completed_review_only_once(self):
        today = date.today().isoformat()
        service.add_daily_record(today, "测试饮食记录")
        service.complete_daily_review(today, daily_review_result(today))
        service.save_checkin_answer(today, "weight", "measured", "no", 0)
        service.complete_checkin_module(today, "weight", 0)
        first_requeue = service.get_daily_review(today)
        self.assertEqual(first_requeue["status"], "pending")
        self.assertEqual(len(first_requeue["history"]), 0)
        self.assertIsNotNone(first_requeue["result_json"])
        service.skip_checkin_module(today, "gut", 0)
        second_update = service.get_daily_review(today)
        self.assertEqual(len(second_update["history"]), 0)
        self.assertEqual(second_update["source_checkin_versions_json"], {"gut": 1, "weight": 1})

    def test_checkin_schema_migrates_existing_review_tables(self):
        legacy_path = Path(self.temp.name) / "legacy-checkin.db"
        conn = sqlite3.connect(legacy_path)
        try:
            conn.executescript(
                """
                CREATE TABLE daily_reviews (
                    id TEXT PRIMARY KEY, review_date TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                    source_record_ids_json TEXT NOT NULL DEFAULT '[]', result_json TEXT,
                    result_version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, completed_at TEXT
                );
                CREATE TABLE daily_review_history (
                    id TEXT PRIMARY KEY, review_id TEXT NOT NULL, version INTEGER NOT NULL,
                    source_record_ids_json TEXT NOT NULL, result_json TEXT NOT NULL,
                    completed_at TEXT, archived_at TEXT NOT NULL, archive_reason TEXT NOT NULL DEFAULT ''
                );
                """
            )
            conn.commit()
        finally:
            conn.close()
        init_db(legacy_path)
        init_db(legacy_path)
        conn = sqlite3.connect(legacy_path)
        try:
            review_columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_reviews)")}
            history_columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_review_history)")}
            checkin_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'daily_checkin%'")}
        finally:
            conn.close()
        self.assertIn("source_checkin_versions_json", review_columns)
        self.assertIn("source_checkin_versions_json", history_columns)
        self.assertEqual(checkin_tables, {"daily_checkins", "daily_checkin_modules", "daily_checkin_module_history"})

    def test_task_input_history_schema_migrates_existing_tasks(self):
        legacy_path = Path(self.temp.name) / "legacy-task-input.db"
        conn = sqlite3.connect(legacy_path)
        try:
            conn.executescript(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, type TEXT NOT NULL, status TEXT NOT NULL,
                    original_input TEXT NOT NULL DEFAULT '', image_path TEXT,
                    created_at TEXT NOT NULL, completed_at TEXT, result_json TEXT,
                    result_version INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO tasks(id,type,status,original_input,created_at)
                VALUES('legacy','material','pending','鸡蛋 2 个','2026-01-01T00:00:00+00:00');
                """
            )
            conn.commit()
        finally:
            conn.close()
        init_db(legacy_path)
        init_db(legacy_path)
        conn = sqlite3.connect(legacy_path)
        try:
            task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            history_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_input_history'")}
            input_version = conn.execute("SELECT input_version FROM tasks WHERE id='legacy'").fetchone()[0]
        finally:
            conn.close()
        self.assertIn("input_version", task_columns)
        self.assertEqual(history_tables, {"task_input_history"})
        self.assertEqual(input_version, 1)

    def test_init_doctor_and_dynamic_settings(self):
        settings_path = Path(self.temp.name) / "settings.json"
        original = settings_path.read_text(encoding="utf-8")
        first = initialize_private_home()
        second = initialize_private_home()
        self.assertEqual(settings_path.read_text(encoding="utf-8"), original)
        self.assertTrue(any(Path(item).samefile(settings_path) for item in first["skipped"]))
        self.assertTrue(any(Path(item).samefile(settings_path) for item in second["skipped"]))
        status = configuration_status()
        self.assertTrue(status["settings_valid"])
        self.assertEqual(status["doctrine_mode"], "private_override")
        custom = {**TEST_SETTINGS, "protein_target_g": [120, 150], "meal_environment": "家庭用餐"}
        settings_path.write_text(json.dumps(custom, ensure_ascii=False), encoding="utf-8")
        today = date.today().isoformat()
        service.add_daily_record(today, "测试动态目标")
        context = service.daily_review_context(today)
        self.assertEqual(context["result_schema"]["tomorrow_menu"]["protein_target_g"], [120, 150])
        self.assertEqual(context["home_cooking_preferences"], {"enabled": False})
        result = daily_review_result(today)
        result["tomorrow_menu"]["protein_target_g"] = [120, 150]
        result["tomorrow_menu"]["environment"] = "家庭用餐"
        self.assertEqual(service.complete_daily_review(today, result)["status"], "completed")

    def test_home_cooking_schema_validation_and_time_limit(self):
        settings_path = Path(self.temp.name) / "settings.json"
        settings_path.write_text(
            json.dumps({**TEST_SETTINGS, "home_cooking": HOME_COOKING}, ensure_ascii=False), encoding="utf-8"
        )
        today = date.today().isoformat()
        service.add_daily_record(today, "测试独居晚餐")
        context = service.daily_review_context(today)
        self.assertTrue(context["home_cooking_preferences"]["enabled"])
        self.assertEqual(context["result_schema"]["tomorrow_menu"]["meals"][2]["mode"], "home_cook")
        self.assertTrue(context["home_cooking_generation_protocol"])

        missing = daily_review_result(today)
        with self.assertRaisesRegex(ValidationError, "早餐.mode"):
            service.complete_daily_review(today, missing)
        too_slow = home_cooking_review_result(today)
        too_slow["tomorrow_menu"]["meals"][2]["recipe_card"]["total_minutes"] = 26
        with self.assertRaisesRegex(ValidationError, "不得超过 25 分钟"):
            service.complete_daily_review(today, too_slow)
        wrong_date = home_cooking_review_result(today)
        wrong_date["tomorrow_menu"]["reuse_plan"]["items"][0]["later_uses"][0]["date"] = (
            date.fromisoformat(today) + timedelta(days=8)
        ).isoformat()
        with self.assertRaisesRegex(ValidationError, "复用窗口"):
            service.complete_daily_review(today, wrong_date)
        self.assertEqual(service.complete_daily_review(today, home_cooking_review_result(today))["status"], "completed")

    def test_lunch_and_dinner_home_cooking_have_independent_cards_constraints_and_history(self):
        settings_path = Path(self.temp.name) / "settings.json"
        settings_path.write_text(
            json.dumps({**TEST_SETTINGS, "home_cooking": HOME_COOKING_LUNCH_DINNER}, ensure_ascii=False),
            encoding="utf-8",
        )
        today_date = date.today() - timedelta(days=1)
        today = today_date.isoformat()
        service.add_daily_record(today, "午餐和晚餐都在家做")
        context = service.daily_review_context(today)
        schema_meals = {meal["name"]: meal for meal in context["result_schema"]["tomorrow_menu"]["meals"]}
        self.assertEqual("quick_assembly", schema_meals["早餐"]["mode"])
        self.assertEqual("home_cook", schema_meals["午餐"]["mode"])
        self.assertEqual("home_cook", schema_meals["晚餐"]["mode"])
        self.assertIn("recipe_card", schema_meals["午餐"])
        self.assertIn("recipe_card", schema_meals["晚餐"])

        missing_lunch_card = home_cooking_review_result(today)
        missing_lunch_card["tomorrow_menu"]["meals"][1]["mode"] = "home_cook"
        missing_lunch_card["tomorrow_menu"]["meals"][1].pop("eat_out_guidance", None)
        with self.assertRaisesRegex(ValidationError, "午餐.recipe_card"):
            service.complete_daily_review(today, missing_lunch_card)

        too_slow = lunch_dinner_home_cooking_result(today)
        too_slow["tomorrow_menu"]["meals"][1]["recipe_card"]["total_minutes"] = 26
        with self.assertRaisesRegex(ValidationError, "午餐总时间不得超过 25 分钟"):
            service.complete_daily_review(today, too_slow)

        completed = service.complete_daily_review(today, lunch_dinner_home_cooking_result(today))
        self.assertEqual("completed", completed["status"])
        plan = adaptive.get_plan_for_date((today_date + timedelta(days=1)).isoformat())
        strategy_keys = {meal["name"]: meal["strategy_key"] for meal in plan["menu"]["meals"]}
        self.assertEqual("tomato_egg_lunch", strategy_keys["午餐"])
        self.assertEqual("tomato_chicken", strategy_keys["晚餐"])

        next_day = date.today().isoformat()
        service.add_daily_record(next_day, "检查分餐次历史")
        next_context = service.daily_review_context(next_day)
        recent = {(item["meal_name"], item["rotation"]["dish_key"]) for item in next_context["recent_home_meals"]}
        self.assertIn(("午餐", "tomato_egg_lunch"), recent)
        self.assertIn(("晚餐", "tomato_chicken"), recent)

    def test_eat_out_override_requires_guidance_and_forbids_home_recipe(self):
        settings = {
            **TEST_SETTINGS,
            "meal_environment": "早餐快速组装、午餐外食、晚餐独居下厨",
            "meal_modes": {"breakfast": "quick_assembly", "lunch": "eat_out", "dinner": "home_cook"},
            "home_cooking": {
                **HOME_COOKING_LUNCH_DINNER,
                "meal_scope": "custom",
                "meal_modes": {"breakfast": "quick_assembly", "lunch": "eat_out", "dinner": "home_cook"},
            },
        }
        review_date = date.today().isoformat()
        result = lunch_eat_out_dinner_home_result(review_date)
        self.assertIs(result, validate_daily_review_result(result, settings))

        missing_guidance = copy.deepcopy(result)
        missing_guidance["tomorrow_menu"]["meals"][1].pop("eat_out_guidance")
        with self.assertRaisesRegex(ValidationError, "午餐.eat_out_guidance"):
            validate_daily_review_result(missing_guidance, settings)

        leaked_recipe = copy.deepcopy(result)
        leaked_recipe["tomorrow_menu"]["meals"][1]["recipe_card"] = copy.deepcopy(
            leaked_recipe["tomorrow_menu"]["meals"][2]["recipe_card"]
        )
        with self.assertRaisesRegex(ValidationError, "不得包含 recipe_card"):
            validate_daily_review_result(leaked_recipe, settings)

    def test_home_cooking_history_rotation_and_repeat_reason(self):
        settings_path = Path(self.temp.name) / "settings.json"
        settings_path.write_text(
            json.dumps({**TEST_SETTINGS, "home_cooking": HOME_COOKING}, ensure_ascii=False), encoding="utf-8"
        )
        first_date = (date.today() - timedelta(days=1)).isoformat()
        second_date = date.today().isoformat()
        service.add_daily_record(first_date, "第一天独居晚餐")
        service.complete_daily_review(first_date, home_cooking_review_result(first_date))
        service.add_daily_record(second_date, "第二天独居晚餐")
        context = service.daily_review_context(second_date)
        self.assertEqual(context["recent_home_dinners"][0]["rotation"]["dish_key"], "tomato_chicken")
        self.assertEqual(context["recent_online_categories"], ["低油番茄调味"])
        obligations = context["ingredient_carryover_obligations"]
        self.assertEqual(len(obligations), 1)
        self.assertEqual(obligations[0]["ingredient"], "番茄")
        self.assertEqual(obligations[0]["planned_use_date"], (date.today() + timedelta(days=1)).isoformat())
        self.assertEqual(obligations[0]["urgency"], "use_tomorrow")
        self.assertEqual(obligations[0]["shopping_items"][0]["amount"], "3个")

        repeated = home_cooking_review_result(second_date)
        with self.assertRaisesRegex(ValidationError, "菜单语义重复"):
            service.complete_daily_review(second_date, repeated)

        missing_carryover = change_home_dinner(
            change_breakfast_and_lunch(home_cooking_review_result(second_date)),
            title="蒜香番茄豆腐汤", protein="豆腐", vegetable="番茄", seasoning="蒜末",
            instruction="豆腐与番茄加水煮开", dish_key="tomato_tofu_soup",
            flavor="garlic_tomato", technique="simmer",
        )
        with self.assertRaisesRegex(ValidationError, "食材承接裁决不完整"):
            service.complete_daily_review(second_date, missing_carryover)

        reuse_same_ingredient = add_carryover_decisions(
            change_home_dinner(
                change_breakfast_and_lunch(home_cooking_review_result(second_date)),
                title="蒜香番茄豆腐汤", protein="豆腐", vegetable="番茄", seasoning="蒜末",
                instruction="豆腐与番茄加水煮开", dish_key="tomato_tofu_soup",
                flavor="garlic_tomato", technique="simmer",
            ),
            context,
        )
        self.assertEqual(service.complete_daily_review(second_date, reuse_same_ingredient)["status"], "completed")

    def test_home_cooking_carryover_allows_explicit_repeat_exception(self):
        settings_path = Path(self.temp.name) / "settings.json"
        settings_path.write_text(
            json.dumps({**TEST_SETTINGS, "home_cooking": HOME_COOKING}, ensure_ascii=False), encoding="utf-8"
        )
        first_date = (date.today() - timedelta(days=1)).isoformat()
        second_date = date.today().isoformat()
        service.add_daily_record(first_date, "第一天独居晚餐")
        service.complete_daily_review(first_date, home_cooking_review_result(first_date))
        service.add_daily_record(second_date, "第二天独居晚餐")
        context = service.daily_review_context(second_date)

        repeated = change_home_dinner(
            change_breakfast_and_lunch(home_cooking_review_result(second_date)),
            title="番茄蘑菇小米椒鸡肉", protein="鸡胸肉", vegetable="番茄", seasoning="小米椒",
            instruction="鸡肉、番茄和蘑菇下锅翻炒至熟", dish_key="tomato_mushroom_chicken",
            flavor="tomato_sour_spicy", technique="stir_fry",
        )
        repeated["tomorrow_menu"]["rotation"]["repeat_reason"] = "ingredient_expiry"
        add_carryover_decisions(repeated, context)
        self.assertEqual(service.complete_daily_review(second_date, repeated)["status"], "completed")

    def test_legacy_database_environment_warns(self):
        legacy = str(Path(self.temp.name) / "legacy.db")
        current_home = os.environ.pop("MEALCIRCUIT_HOME")
        current_db = os.environ.pop("MEALCIRCUIT_DB")
        old_legacy = os.environ.get("DIETOS_DB")
        storage._WARNED_LEGACY.clear()
        os.environ["DIETOS_DB"] = legacy
        output = io.StringIO()
        try:
            with redirect_stderr(output):
                self.assertEqual(storage.db_path(), Path(legacy).resolve())
            self.assertIn("已弃用", output.getvalue())
        finally:
            os.environ["MEALCIRCUIT_HOME"] = current_home
            os.environ["MEALCIRCUIT_DB"] = current_db
            if old_legacy is None:
                os.environ.pop("DIETOS_DB", None)
            else:
                os.environ["DIETOS_DB"] = old_legacy

    def test_migration_preview_apply_and_repeat(self):
        with tempfile.TemporaryDirectory() as source_name, tempfile.TemporaryDirectory() as target_name:
            source = Path(source_name)
            target = Path(target_name)
            source_db = source / "data" / "dietos.db"
            init_db(source_db)
            connection = sqlite3.connect(source_db)
            try:
                connection.execute(
                    "INSERT INTO daily_records(id,record_date,raw_input,created_at) VALUES(?,?,?,?)",
                    ("record_test", "2026-01-01", "synthetic record", "2026-01-01T00:00:00+00:00"),
                )
                connection.commit()
            finally:
                connection.close()
            (source / "data" / "food-labels").mkdir(parents=True)
            (source / "data" / "food-labels" / "label.png").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
            (source / "减脂增肌饮食系统总纲.md").write_text("# synthetic doctrine\n", encoding="utf-8")
            old_home = os.environ["MEALCIRCUIT_HOME"]
            old_db = os.environ["MEALCIRCUIT_DB"]
            os.environ["MEALCIRCUIT_HOME"] = str(target)
            os.environ["MEALCIRCUIT_DB"] = str(target / "mealcircuit.db")
            try:
                preview = migration_preview(source)
                self.assertEqual(preview["mode"], "preview")
                self.assertFalse((target / "mealcircuit.db").exists())
                applied = apply_migration(source)
                self.assertTrue((target / "mealcircuit.db").is_file())
                self.assertTrue((source / "data" / "dietos.db").is_file())
                self.assertEqual(applied["database"]["integrity"], "ok")
                repeated = apply_migration(source)
                self.assertEqual(repeated["database"]["status"], "identical")
            finally:
                os.environ["MEALCIRCUIT_HOME"] = old_home
                os.environ["MEALCIRCUIT_DB"] = old_db

    def test_release_check_detects_private_database(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            (root / "safe.py").write_text("print('safe')\n", encoding="utf-8")
            self.assertEqual(release_scan(root), [])
            (root / "data").mkdir()
            (root / "data" / "private.db").write_bytes(b"SQLite format 3\x00private")
            reasons = {item["reason"] for item in release_scan(root)}
            self.assertIn("forbidden_private_directory", reasons)


class FirstRunWebAppTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.old_environment = {
            key: os.environ.get(key) for key in ("MEALCIRCUIT_HOME", "MEALCIRCUIT_DB")
        }
        os.environ["MEALCIRCUIT_HOME"] = str(self.home)
        os.environ["MEALCIRCUIT_DB"] = str(self.home / "mealcircuit.db")
        init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        restore_environment(self.old_environment)
        self.temp.cleanup()

    def test_brand_new_home_renders_onboarding_without_settings(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            conn.request("GET", "/")
            response = conn.getresponse()
            body = response.read().decode("utf-8")
        finally:
            conn.close()
        self.assertEqual(200, response.status)
        self.assertIn("初始化", body)
        self.assertFalse((self.home / "settings.json").exists())


class WebAppTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_environment = configure_private_home(Path(self.temp.name))
        init_db()
        complete_standard_test_onboarding()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        restore_environment(self.old_environment)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.headers), response.read()
        finally:
            conn.close()

    def rejected_post(self, path, body=None, headers=None):
        transient = (ConnectionAbortedError, ConnectionResetError, http.client.RemoteDisconnected)
        for attempt in range(3):
            try:
                return self.request("POST", path, body, headers)
            except transient:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    @staticmethod
    def multipart_form(fields, files=()):
        boundary = "----MealCircuitTestBoundary"
        parts = []
        for name, value in fields:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )
        for name, filename, content_type, data in files:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f'Content-Type: {content_type}\r\n\r\n'.encode() + data + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        return body, {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        }

    def test_plan_feedback_preserves_inputs_and_photo_after_missing_reason(self):
        review_date = date.today().isoformat()
        service.add_daily_record(review_date, "今天按计划记录实际饮食。")
        service.complete_daily_review(review_date, daily_review_result(review_date))
        plan_date = (date.today() + timedelta(days=1)).isoformat()
        plan = adaptive.get_plan_for_date(plan_date)
        meal = plan["menu"]["meals"][0]
        item_id = meal["plan_item_id"]
        image = b"\x89PNG\r\n\x1a\nmeal-feedback"

        body, headers = self.multipart_form(
            [
                ("expected_version", "0"),
                ("status", "modified"),
                ("satiety", "not_enough"),
                ("actual_text", "临时多吃了一碗饭"),
            ],
            [("photo", "actual.png", "image/png", image)],
        )
        status, _, raw = self.request(
            "POST", f"/plans/{plan_date}/{item_id}/feedback", body, headers
        )
        page = raw.decode("utf-8")
        self.assertEqual(400, status)
        self.assertIn("调整或未执行时必须选择至少一个原因", page)
        self.assertIn('<option value="modified" selected>', page)
        self.assertIn('<option value="not_enough" selected>', page)
        self.assertIn("临时多吃了一碗饭", page)
        self.assertIn('<details class="feedback-box" open>', page)
        self.assertIsNone(adaptive.get_plan_for_date(plan_date)["feedback"].get(item_id))

        photo_task = service.list_tasks()[0]
        self.assertEqual("photo", photo_task["type"])
        self.assertEqual(image, resolve_data_path(photo_task["image_path"]).read_bytes())
        self.assertIn(f'name="photo_task_id" value="{photo_task["id"]}"', page)
        self.assertIn(f'/media/{Path(photo_task["image_path"]).name}', page)

        corrected_body, corrected_headers = self.multipart_form([
            ("expected_version", "0"),
            ("status", "modified"),
            ("satiety", "not_enough"),
            ("reason_codes", "schedule_change"),
            ("reason_codes", "hunger_mismatch"),
            ("actual_text", "临时多吃了一碗饭"),
            ("photo_task_id", photo_task["id"]),
        ])
        status, response_headers, _ = self.request(
            "POST", f"/plans/{plan_date}/{item_id}/feedback", corrected_body, corrected_headers
        )
        self.assertEqual(303, status)
        self.assertEqual(f"/plans/{plan_date}", response_headers["Location"])
        feedback = adaptive.get_plan_for_date(plan_date)["feedback"][item_id]
        self.assertEqual(["schedule_change", "hunger_mismatch"], feedback["reason_codes_json"])
        self.assertEqual("not_enough", feedback["outcome_json"]["satiety"])
        self.assertEqual(photo_task["id"], feedback["outcome_json"]["photo_task_id"])
        links = adaptive.task_evidence_links(photo_task["id"])
        self.assertEqual("consumed", links[0]["role"])
        self.assertEqual(plan_date, links[0]["observed_date"])

        status, _, app_script = self.request("GET", "/assets/ui/app.js")
        self.assertEqual(200, status)
        self.assertIn("请选择这顿发生变化的原因", app_script.decode("utf-8"))

    def test_pages_and_material_form(self):
        for path in ("/", "/plans", "/me", "/history", "/tasks/photo", "/tasks/material", "/tasks", "/ai", "/sync", "/foods", "/overview"):
            status, _, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertIn(b"MealCircuit", body)
            if path == "/sync":
                self.assertIn("同步服务 URL", body.decode("utf-8"))
        for old_path, destination in (
            ("/capture", "/#record"),
            ("/daily", "/plans"),
            ("/insights", "/me#progress"),
        ):
            status, headers, _ = self.request("GET", old_path)
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], destination)
        status, home_headers, home = self.request("GET", "/")
        decoded_home = home.decode("utf-8")
        for label in ("今天感觉怎么样？", "今天有什么变化？", "今天的状态", "今天", "计划", "我的", "记一笔"):
            self.assertIn(label, decoded_home)
        self.assertIn('class="app-sidebar"', decoded_home)
        self.assertIn('aria-current="page"', decoded_home)
        self.assertEqual(3, decoded_home.count('class="nav-link"'))
        self.assertIn('href="/assets/ui/app.css?v=', decoded_home)
        self.assertIn('rel="icon" href="/assets/ui/favicon.svg"', decoded_home)
        self.assertIn('src="/assets/ui/theme-init.js?v=', decoded_home)
        self.assertIn('data-theme-toggle', decoded_home)
        self.assertEqual(1, decoded_home.count("<h1"))
        for hidden_copy in ("建议生成", "洞察", "学习确认", "Three-stage planning", "AgentContextV2", "查看原有今日总览"):
            self.assertNotIn(hidden_copy, decoded_home)
        self.assertIn("script-src 'self'", home_headers["Content-Security-Policy"])
        status, headers, css = self.request("GET", "/assets/ui/app.css")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/css"))
        self.assertIn(b"prefers-color-scheme: light", css)
        self.assertIn(b':root[data-theme="light"]', css)
        status, headers, favicon = self.request("GET", "/assets/ui/favicon.svg")
        self.assertEqual(status, 200)
        self.assertIn("image/svg+xml", headers["Content-Type"])
        self.assertIn(b"#a9d2bf", favicon)
        status, headers, theme_script = self.request("GET", "/assets/ui/theme-init.js")
        self.assertEqual(status, 200)
        javascript_type = headers["Content-Type"].split(";", 1)[0]
        self.assertIn(javascript_type, {"text/javascript", "application/javascript"})
        self.assertIn(b"mealcircuit.theme", theme_script)
        status, _, _ = self.request("GET", "/assets/ui/%2e%2e/server.py")
        self.assertEqual(status, 404)
        status, _, app_script = self.request("GET", "/assets/ui/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"sidebarScrollTop", app_script)
        self.assertIn(b'aria-current="page"', app_script)
        body = "materials=" + urllib.parse.quote("鸡胸肉 300g")
        status, headers, _ = self.request("POST", "/tasks/material", body.encode(), {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 303)
        task_id = headers["Location"].rsplit("/", 1)[-1]
        self.assertEqual(service.get_task(task_id)["type"], "material")
        result = {
            "summary": "现有材料可分两份", "combinations": ["<b>鸡胸肉饭</b>"],
            "batch_nutrition": nutrition(10, 20), "per_serving_nutrition": nutrition(5, 10),
            "gaps": ["水果未知"], "risks": ["辣度需控制"], "minimal_adjustments": ["补一份蔬菜"],
        }
        service.complete_task(task_id, result)
        status, _, detail = self.request("GET", f"/tasks/{task_id}")
        decoded = detail.decode("utf-8")
        self.assertEqual(status, 200)
        for label in ("可做组合 / 菜品方向", "整批营养估算", "单份营养估算", "当前缺口", "肠胃 / 执行风险", "最小调整", "查看原始 JSON"):
            self.assertIn(label, decoded)
        self.assertIn("&lt;b&gt;鸡胸肉饭&lt;/b&gt;", decoded)
        self.assertNotIn("<b>鸡胸肉饭</b>", decoded)

    def test_agent_context_inspector_has_human_page_json_export_and_poll_state(self):
        review_date = date.today().isoformat()
        service.add_daily_record(review_date, "今天完成力量训练，明天午餐外食。")
        status, _, page = self.request("GET", f"/agent/context/{review_date}")
        self.assertEqual(200, status)
        decoded = page.decode("utf-8")
        self.assertIn("这次模型实际看到了什么", decoded)
        self.assertIn("为什么选入", decoded)
        self.assertIn("本次采用的专业原则", decoded)
        self.assertIn("仍未知：体重、训练、饥饿感、睡眠、肠胃", decoded)
        self.assertIn("不追求一顿所谓的完美餐", decoded)
        self.assertNotIn("{'due':", decoded)
        self.assertIn(f'/agent/context/{review_date}?format=json', decoded)

        status, headers, raw = self.request("GET", f"/agent/context/{review_date}?format=json")
        self.assertEqual(200, status)
        self.assertIn("application/json", headers["Content-Type"])
        context = json.loads(raw)
        self.assertEqual("AgentContextV2", context["context_schema"])

        status, _, raw_state = self.request("GET", f"/agent/state/{review_date}")
        self.assertEqual(200, status)
        state = json.loads(raw_state)
        self.assertEqual("collecting", state["status"])
        self.assertEqual(0, state["version"])

    def test_adaptive_web_workspace_plan_feedback_inventory_and_setup_revision(self):
        review_date = date.today().isoformat()
        service.add_daily_record(review_date, "今天按计划记录实际饮食。")
        service.complete_daily_review(review_date, daily_review_result(review_date))
        plan_date = (date.today() + timedelta(days=1)).isoformat()
        plan = adaptive.get_plan_for_date(plan_date)
        self.assertIsNotNone(plan)

        for path, label in (
            ("/", "今天有什么变化"), ("/plans", "计划"), ("/me", "目标与饮食偏好"),
            (f"/plans/{plan_date}", "的安排"), (f"/questions/{plan_date}", "只补齐会改变行动的信息"),
            ("/learning", "MealCircuit了解的你"), ("/inventory", "家里有什么"),
            ("/profile", "目标与饮食偏好"), ("/data", "备份与迁移"),
        ):
            status, _, page = self.request("GET", path)
            self.assertEqual(200, status, path)
            self.assertIn(label, page.decode("utf-8"))
        for old_path, destination in (("/capture", "/#record"), ("/insights", "/me#progress")):
            status, headers, _ = self.request("GET", old_path)
            self.assertEqual(303, status, old_path)
            self.assertEqual(destination, headers["Location"])

        status, export_headers, bundle = self.request("GET", "/data/export")
        self.assertEqual(200, status)
        self.assertEqual("application/zip", export_headers["Content-Type"])
        self.assertIn("attachment;", export_headers["Content-Disposition"])
        self.assertTrue(bundle.startswith(b"PK"))

        item_id = plan["menu"]["meals"][0]["plan_item_id"]
        payload = urllib.parse.urlencode({"expected_version": "0", "status": "followed"}).encode()
        status, headers, _ = self.request("POST", f"/plans/{plan_date}/{item_id}/feedback", payload, {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(303, status)
        self.assertEqual(f"/plans/{plan_date}", headers["Location"])
        self.assertEqual("followed", adaptive.get_plan_for_date(plan_date)["feedback"][item_id]["status"])

        lunch = next(item for item in plan["menu"]["meals"] if item["name"] == "午餐")
        rescue = adaptive.create_rescue_session(
            plan_date, lunch["plan_item_id"], "not_enough_time", "午休只剩十分钟"
        )
        adaptive.complete_rescue_session(rescue["id"], {
            "reason": "改用无需复杂处理的现成组合。", "steps": ["选择现成主食和蛋白", "补一份蔬菜"],
            "replacement_foods": ["现成米饭", "即食鸡蛋"], "portion_change": "保持原份量结构",
            "safety_notes": ["不使用排除食品"],
        })
        status, _, rescue_page = self.request("GET", f'/rescue/{rescue["id"]}')
        self.assertEqual(200, status)
        decoded_rescue = rescue_page.decode("utf-8")
        self.assertIn("改用无需复杂处理的现成组合", decoded_rescue)
        self.assertIn("现成米饭", decoded_rescue)
        self.assertIn("保持原份量结构", decoded_rescue)

        metric_payload = urllib.parse.urlencode({
            "observed_date": review_date, "metric_key": "weight_kg", "value": "79.4",
        }).encode()
        status, headers, _ = self.request(
            "POST", "/metrics", metric_payload,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(303, status)
        self.assertEqual("/me#progress", headers["Location"])
        self.assertEqual(79.4, personalization.list_metrics("weight_kg")[0]["value_json"])

        payload = urllib.parse.urlencode({"name": "小白菜", "amount_text": "约一顿", "expires_on": plan_date}).encode()
        status, _, _ = self.request("POST", "/inventory", payload, {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(303, status)
        self.assertEqual("小白菜", adaptive.list_inventory()[0]["name"])

        for prior in (date.today() - timedelta(days=4), date.today() - timedelta(days=3)):
            prior_text = prior.isoformat()
            service.add_daily_record(prior_text, "重复时间阻力的浏览器学习证据。")
            prior_result = daily_review_result(prior_text)
            variant = prior.day
            prior_meals = {meal["name"]: meal for meal in prior_result["tomorrow_menu"]["meals"]}
            prior_meals["早餐"]["foods"] = [f"燕麦{variant}", "酸奶", f"水果{variant}"]
            prior_meals["午餐"]["foods"] = [f"鱼类{variant}", "米饭", f"蔬菜{variant}"]
            prior_meals["晚餐"]["foods"] = [f"快速鸡肉{variant}", "主食", f"蔬菜{variant}"]
            prior_meals["晚餐"]["strategy_key"] = "web-time-friction-dinner"
            service.complete_daily_review(prior_text, prior_result)
            prior_plan = adaptive.get_plan_for_date((prior + timedelta(days=1)).isoformat())
            dinner = next(item for item in prior_plan["menu"]["meals"] if item["name"] == "晚餐")
            adaptive.save_plan_feedback(
                prior_plan["plan_date"], dinner["plan_item_id"], "modified",
                reason_codes=["not_enough_time"], actor_source="web_test",
            )
        candidate = adaptive.list_candidates("pending")[0]
        decision = urllib.parse.urlencode({
            "decision": "accept", "statement": "晚餐主动准备时间最多 15 分钟。",
        }).encode()
        status, _, _ = self.request(
            "POST", f'/learning/{candidate["id"]}/decide', decision,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(303, status)
        status, _, learning_page = self.request("GET", "/learning")
        self.assertEqual(200, status)
        self.assertIn("MealCircuit了解的你", learning_page.decode("utf-8"))
        self.assertNotIn("置信度", learning_page.decode("utf-8"))
        self.assertIn("晚餐主动准备时间最多 15 分钟。", [item["statement"] for item in adaptive.list_rules()])

        experiment_payload = urllib.parse.urlencode({
            "variable_key": "dinner_active_minutes", "action": "晚餐主动时间控制在15分钟",
            "success_signal": "连续三次完成且负担可接受",
        }).encode()
        status, _, _ = self.request(
            "POST", "/learning/experiments", experiment_payload,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(303, status)
        experiment = adaptive.list_experiments()[0]
        start_payload = urllib.parse.urlencode({"starts_on": review_date, "days": "5"}).encode()
        status, _, _ = self.request(
            "POST", f'/learning/experiments/{experiment["id"]}/start', start_payload,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(303, status)
        finish_payload = urllib.parse.urlencode({"summary": "验证完成", "decision": "complete"}).encode()
        status, _, _ = self.request(
            "POST", f'/learning/experiments/{experiment["id"]}/finish', finish_payload,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(303, status)
        self.assertEqual("completed", adaptive.list_experiments()[0]["status"])

        status, headers, _ = self.request("POST", "/setup/start", b"", {"Content-Length": "0"})
        self.assertEqual(303, status)
        self.assertEqual("/setup/welcome", headers["Location"])
        status, _, page = self.request("GET", "/setup/welcome")
        self.assertEqual(200, status)
        self.assertIn("隐私与边界", page.decode("utf-8"))
        status, _, constraints_page = self.request("GET", "/setup/constraints")
        self.assertEqual(200, status)
        decoded_constraints = constraints_page.decode("utf-8")
        for label in ("早餐通常怎样准备", "午餐通常怎样准备", "晚餐通常怎样准备", "在家下厨（生成执行卡）"):
            self.assertIn(label, decoded_constraints)
        session = personalization.onboarding_status()["session"]
        invalid_welcome = urllib.parse.urlencode({
            "session_id": session["id"], "version": str(session["version"]),
        }).encode()
        status, _, invalid_page = self.request(
            "POST", "/setup/save/welcome", invalid_welcome,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(400, status)
        self.assertIn('role="alert"', invalid_page.decode("utf-8"))
        self.assertIn("必须确认本地存储与模型发送边界", invalid_page.decode("utf-8"))
        steps = [
            ("welcome", {"privacy_ack": "yes"}),
            ("goals", {"primary_goal": "eating_consistency", "success_metrics": ["execution_rate"], "motivation": "降低操作摩擦"}),
            ("baseline", {"age_years": "31", "physiological_input": "unspecified", "activity_level": "moderate"}),
            ("safety", {"life_stage": "adult", **{key: "no" for key in personalization.OBSERVATION_FLAGS}}),
            ("training", {"types": ["strength"], "frequency_per_week": "3"}),
            ("constraints", {"meal_environment": "午餐和晚餐在家", "portion_method": "手掌份量", "meal_mode_breakfast": "quick_assembly", "meal_mode_lunch": "home_cook", "meal_mode_dinner": "home_cook", "cooking_time_minutes": "20", "question_budget": "2", "equipment": "炒锅, 电饭煲", "food_exclusions": "花生", "preferences": "酸辣"}),
        ]
        version = session["version"]
        for step, values in steps:
            payload = urllib.parse.urlencode({"session_id": session["id"], "version": str(version), **values}, doseq=True).encode()
            status, headers, _ = self.request("POST", f"/setup/save/{step}", payload, {"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(303, status, step)
            version += 1
        payload = urllib.parse.urlencode({"session_id": session["id"], "version": str(version), "accept_profile": "yes", "accept_strategy": "yes", "planning_mode": "portion_guided"}).encode()
        status, headers, _ = self.request("POST", "/setup/complete", payload, {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(303, status)
        self.assertEqual("/", headers["Location"])
        active = personalization.active_personalization()
        self.assertEqual(2, active["profile"]["version"])
        self.assertEqual("home_cook", active["strategy"]["strategy_json"]["meal_modes"]["lunch"])
        self.assertEqual("home_cook", active["strategy"]["strategy_json"]["meal_modes"]["dinner"])

    def test_photo_upload_form(self):
        boundary = "----MealCircuitTestBoundary"
        image = b"\x89PNG\r\n\x1a\nweb-test"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\nweb upload\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"meal.png\"\r\nContent-Type: image/png\r\n\r\n"
        ).encode() + image + f"\r\n--{boundary}--\r\n".encode()
        status, headers, _ = self.request("POST", "/tasks/photo", body, {"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))})
        self.assertEqual(status, 303)
        task_id = headers["Location"].rsplit("/", 1)[-1]
        task = service.get_task(task_id)
        self.assertEqual(task["type"], "photo")
        self.assertTrue(resolve_data_path(task["image_path"]).is_file())
        result = {
            "summary": "<script>alert('x')</script>",
            "candidates": [{"name": "<b>米饭</b>", "portion_range": "150–200g", "nutrition": nutrition(1, 2), "confidence": 0.72}],
            "unknowns": ["用油不可见"], "advice": ["按区间暂记"],
        }
        service.complete_task(task_id, result)
        status, _, detail = self.request("GET", f"/tasks/{task_id}")
        decoded = detail.decode("utf-8")
        self.assertEqual(status, 200)
        for label in ("候选食物", "份量：", "置信度：", "72%", "未知项", "综合建议", "查看原始 JSON"):
            self.assertIn(label, decoded)
        self.assertIn("&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;", decoded)
        self.assertIn("&lt;b&gt;米饭&lt;/b&gt;", decoded)
        self.assertNotIn("<script>alert('x')</script>", decoded)
        self.assertNotIn("<b>米饭</b>", decoded)

    def test_pending_task_input_edit_form_history_and_completed_lock(self):
        task = service.create_material_task("鸡蛋 2 个")
        status, _, pending = self.request("GET", f'/tasks/{task["id"]}')
        decoded_pending = pending.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn(f'action="/tasks/{task["id"]}/input"', decoded_pending)
        self.assertIn(f'action="/tasks/{task["id"]}/generate"', decoded_pending)
        self.assertIn("用 API Key 生成", decoded_pending)
        self.assertIn('name="expected_version" value="1"', decoded_pending)
        self.assertIn('maxlength="10000"', decoded_pending)
        self.assertIn("鸡蛋 2 个</textarea>", decoded_pending)

        form = urllib.parse.urlencode({"text": "<b>鸡蛋 4 个</b>", "expected_version": "1"}).encode()
        status, headers, _ = self.request(
            "POST", f'/tasks/{task["id"]}/input', form,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], f'/tasks/{task["id"]}')
        status, _, edited = self.request("GET", headers["Location"])
        decoded_edited = edited.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("输入修改历史（1）", decoded_edited)
        self.assertIn("&lt;b&gt;鸡蛋 4 个&lt;/b&gt;</textarea>", decoded_edited)
        self.assertNotIn("<b>鸡蛋 4 个</b>", decoded_edited)
        self.assertIn("鸡蛋 2 个", decoded_edited)

        result = {
            "summary": "可分两份", "combinations": ["鸡蛋配主食与蔬菜"],
            "batch_nutrition": nutrition(), "per_serving_nutrition": nutrition(),
            "gaps": ["蔬菜未知"], "risks": ["数量粗略"], "minimal_adjustments": ["补充蔬菜"],
        }
        service.complete_task(task["id"], result)
        status, _, completed = self.request("GET", f'/tasks/{task["id"]}')
        decoded_completed = completed.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertNotIn(f'action="/tasks/{task["id"]}/input"', decoded_completed)
        self.assertIn("任务完成后输入已锁定", decoded_completed)
        self.assertIn(f'action="/tasks/{task["id"]}/corrections"', decoded_completed)

        stale = urllib.parse.urlencode({"text": "鸡蛋 6 个", "expected_version": "2"}).encode()
        status, _, response = self.request(
            "POST", f'/tasks/{task["id"]}/input', stale,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 400)
        self.assertIn("只能修改待处理任务", response.decode("utf-8"))

    def test_web_task_generate_success_and_failure(self):
        task = service.create_material_task("鸡蛋 2 个")
        result = {
            "summary": "可分两份", "combinations": ["鸡蛋配主食与蔬菜"],
            "batch_nutrition": nutrition(), "per_serving_nutrition": nutrition(),
            "gaps": ["蔬菜未知"], "risks": ["数量粗略"], "minimal_adjustments": ["补充蔬菜"],
        }
        original = service.generate_task_result

        def fake_success(task_id):
            return service.complete_task(task_id, result)

        service.generate_task_result = fake_success
        try:
            status, headers, _ = self.request("POST", f'/tasks/{task["id"]}/generate', b"", {"Content-Length": "0"})
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], f'/tasks/{task["id"]}')
            self.assertEqual(service.get_task(task["id"])["status"], "completed")
        finally:
            service.generate_task_result = original

        failing = service.create_material_task("牛奶 1 盒")

        def fake_failure(task_id):
            raise ValidationError("缺少 MEALCIRCUIT_AI_MODEL")

        service.generate_task_result = fake_failure
        try:
            status, _, body = self.request("POST", f'/tasks/{failing["id"]}/generate', b"", {"Content-Length": "0"})
            self.assertEqual(status, 400)
            self.assertIn("缺少 MEALCIRCUIT_AI_MODEL", body.decode("utf-8"))
            self.assertEqual(service.get_task(failing["id"])["status"], "pending")
        finally:
            service.generate_task_result = original

    def test_runtime_ai_key_mode_form_sets_and_clears_process_environment(self):
        status, _, page = self.request("GET", "/ai")
        decoded = page.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("智能规划设置", decoded)
        self.assertIn("启用本次运行的 API Key 模式", decoded)
        self.assertNotIn("secret-runtime-key", decoded)

        form = urllib.parse.urlencode({
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "api_key": "secret-runtime-key",
            "timeout_seconds": "33",
            "max_output_tokens": "444",
            "case_model": "deepseek-case",
            "plan_model": "deepseek-plan",
            "review_model": "deepseek-review",
        }).encode()
        status, headers, _ = self.request(
            "POST", "/ai/configure", form,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/ai")
        self.assertEqual(os.environ["MEALCIRCUIT_AI_PROVIDER"], "deepseek")
        self.assertEqual(os.environ["MEALCIRCUIT_AI_MODEL"], "deepseek-v4-flash")
        self.assertEqual(os.environ["MEALCIRCUIT_DEEPSEEK_API_KEY"], "secret-runtime-key")
        self.assertEqual(os.environ["MEALCIRCUIT_AI_CASE_MODEL"], "deepseek-case")
        self.assertEqual(os.environ["MEALCIRCUIT_AI_PLAN_MODEL"], "deepseek-plan")
        self.assertEqual(os.environ["MEALCIRCUIT_AI_REVIEW_MODEL"], "deepseek-review")

        status, _, configured = self.request("GET", "/ai")
        decoded_configured = configured.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("已启用", decoded_configured)
        self.assertIn("当前使用 deepseek", decoded_configured)
        self.assertNotIn("secret-runtime-key", decoded_configured)

        status, headers, _ = self.request("POST", "/ai/disable", b"", {"Content-Length": "0"})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/ai")
        self.assertNotIn("MEALCIRCUIT_AI_PROVIDER", os.environ)
        self.assertNotIn("MEALCIRCUIT_DEEPSEEK_API_KEY", os.environ)
        self.assertNotIn("MEALCIRCUIT_AI_CASE_MODEL", os.environ)

    def test_daily_review_web_display_and_record_redirect(self):
        review_date = date.today().isoformat()
        priority_food = service.create_food({
            "name": "优先全麦面包", "brand": "测试", "basis": "100g", "energy_kcal": 245,
            "protein_g": 9.7, "carbs_g": 48.9, "fat_g": 0, "fiber_g": 3.5, "sodium_mg": 253,
            "serving_unit": "", "category": "staple", "menu_priority": "high",
            "default_portion": "50–100g", "usage_rule": "早餐主食优先", "source_key": "web-priority-bread",
            "source_url": "", "package_photo_path": None, "notes": "",
        })
        form = urllib.parse.urlencode({"record_date": review_date, "raw_input": "今天蛋白很多"}).encode()
        status, headers, _ = self.request("POST", "/records", form, {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/#record")
        pending_status, _, pending_body = self.request("GET", f"/reviews/{review_date}")
        self.assertEqual(pending_status, 200)
        self.assertIn("复盘还没有准备好", pending_body.decode("utf-8"))
        daily_status, daily_headers, _ = self.request("GET", "/daily")
        self.assertEqual(daily_status, 303)
        self.assertEqual(daily_headers["Location"], "/plans")
        result = daily_review_result(review_date, unsafe=True)
        result["priority_food_decisions"] = [{"food_id": priority_food["id"], "decision": "use", "reason": "早餐主食优先"}]
        service.complete_daily_review(review_date, result)
        status, _, detail = self.request("GET", f"/reviews/{review_date}")
        decoded = detail.decode("utf-8")
        self.assertEqual(status, 200)
        for label in ("接下来最重要", "明天怎么吃", "蛋白目标", "早餐", "午餐", "晚餐", "条件加餐", "训练日调整", "肠胃异常调整"):
            self.assertIn(label, decoded)
        self.assertIn("食材安排", decoded)
        self.assertIn("优先全麦面包", decoded)
        self.assertIn(f'/foods/{priority_food["id"]}', decoded)
        self.assertIn("&lt;script&gt;推断&lt;/script&gt;", decoded)
        self.assertNotIn("<script>推断</script>", decoded)
        status, _, daily = self.request("GET", "/plans")
        self.assertEqual(status, 200)
        self.assertIn("明天", daily.decode("utf-8"))
        status, _, history = self.request("GET", "/history")
        decoded_history = history.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("过去的安排", decoded_history)
        self.assertIn("review-card", decoded_history)
        self.assertIn(review_date, decoded_history)
        self.assertIn(f'/reviews/{review_date}', decoded_history)
        status, _, overview = self.request("GET", "/overview")
        decoded_overview = overview.decode("utf-8")
        self.assertIn("最近建议", decoded_overview)
        self.assertIn("review-card", decoded_overview)
        self.assertNotIn("今天蛋白很多", decoded_overview)

    def test_web_daily_generate_success_and_failure(self):
        review_date = date.today().isoformat()
        service.add_daily_record(review_date, "今天蛋白很多")
        original = service.generate_daily_review

        def fake_success(date_text):
            return service.complete_daily_review(date_text, daily_review_result(date_text))

        service.generate_daily_review = fake_success
        try:
            status, headers, _ = self.request("POST", f"/reviews/{review_date}/generate", b"", {"Content-Length": "0"})
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], f"/reviews/{review_date}")
            self.assertEqual(service.get_daily_review(review_date)["status"], "completed")
        finally:
            service.generate_daily_review = original

        next_date = (date.today() - timedelta(days=1)).isoformat()
        service.add_daily_record(next_date, "昨天记录")

        def fake_failure(date_text):
            raise ValidationError("模型 API 请求失败")

        service.generate_daily_review = fake_failure
        try:
            status, _, body = self.request("POST", f"/reviews/{next_date}/generate", b"", {"Content-Length": "0"})
            self.assertEqual(status, 400)
            self.assertIn("模型 API 请求失败", body.decode("utf-8"))
            self.assertEqual(service.get_daily_review(next_date)["status"], "pending")
        finally:
            service.generate_daily_review = original

    def test_home_cooking_menu_web_display(self):
        settings_path = Path(self.temp.name) / "settings.json"
        settings_path.write_text(
            json.dumps({**TEST_SETTINGS, "home_cooking": HOME_COOKING_LUNCH_DINNER}, ensure_ascii=False), encoding="utf-8"
        )
        review_date = date.today().isoformat()
        service.add_daily_record(review_date, "午餐和晚餐自炊页面测试")
        result = lunch_dinner_home_cooking_result(review_date)
        result["tomorrow_menu"]["meals"][2]["recipe_card"]["title"] = "番茄<script>鸡肉</script>"
        service.complete_daily_review(review_date, result)
        status, _, body = self.request("GET", f"/reviews/{review_date}")
        decoded = body.decode("utf-8")
        self.assertEqual(status, 200)
        for label in (
            "快速组装", "在家下厨", "BEGINNER LUNCH", "BEGINNER DINNER", "明日采购清单",
            "可选网购组件", "3 日食材复用方向", "失败补救", "清洁成本", "肠胃降级",
        ):
            self.assertIn(label, decoded)
        self.assertIn("番茄&lt;script&gt;鸡肉&lt;/script&gt;", decoded)
        self.assertNotIn("番茄<script>鸡肉</script>", decoded)

    def test_review_and_plan_pages_show_effective_lunch_eat_out(self):
        modes = {"breakfast": "quick_assembly", "lunch": "eat_out", "dinner": "home_cook"}
        settings_path = Path(self.temp.name) / "settings.json"
        settings_path.write_text(json.dumps({
            **TEST_SETTINGS,
            "meal_environment": "早餐快速组装、午餐外食、晚餐独居下厨",
            "home_cooking": {**HOME_COOKING_LUNCH_DINNER, "meal_scope": "custom", "meal_modes": modes},
        }, ensure_ascii=False), encoding="utf-8")
        review_date = date.today().isoformat()
        service.add_daily_record(review_date, "明天午餐外食，晚餐自己做")
        service.complete_daily_review(review_date, lunch_eat_out_dinner_home_result(review_date))

        status, _, review_body = self.request("GET", f"/reviews/{review_date}")
        review_html = review_body.decode("utf-8")
        self.assertEqual(200, status)
        self.assertIn("午餐</span><span class=\"meal-time\">食堂 / 外食", review_html)
        self.assertIn("外食提醒", review_html)
        self.assertIn("酱汁分开，不喝油汤", review_html)
        self.assertNotIn("BEGINNER LUNCH", review_html)
        self.assertIn("BEGINNER DINNER", review_html)

        plan_date = (date.today() + timedelta(days=1)).isoformat()
        status, _, plan_body = self.request("GET", f"/plans/{plan_date}")
        plan_html = plan_body.decode("utf-8")
        self.assertEqual(200, status)
        self.assertIn("方式：</strong>食堂 / 外食", plan_html)
        self.assertIn("外食选择", plan_html)
        self.assertIn("酱汁分开，不喝油汤", plan_html)

    def test_checkin_web_question_flow_settings_and_origin(self):
        today = date.today().isoformat()
        status, _, hub = self.request("GET", f"/check-ins/{today}")
        decoded = hub.decode("utf-8")
        self.assertEqual(status, 200)
        for label in ("今天的状态", "体重", "训练", "饥饿与饱腹", "睡眠", "肠胃反应"):
            self.assertIn(label, decoded)
        self.assertNotIn("0/5", decoded)
        status, _, today_question = self.request("GET", f"/check-ins/{today}/training?return_to=today")
        self.assertEqual(status, 200)
        self.assertIn('name="return_to" value="today"', today_question.decode("utf-8"))
        return_payload = urllib.parse.urlencode({"expected_version": "0", "return_to": "today"}).encode()
        status, response_headers, _ = self.request(
            "POST", f"/check-ins/{today}/training/skip", return_payload,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(response_headers["Location"], "/#today-state")
        status, _, question = self.request("GET", f"/check-ins/{today}/weight")
        self.assertEqual(status, 200)
        self.assertIn("今天测体重了吗", question.decode("utf-8"))
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": f"localhost:{self.port}",
            "Origin": f"http://127.0.0.1:{self.port}",
            "Sec-Fetch-Site": "same-site",
        }
        first = urllib.parse.urlencode({"question_id": "measured", "expected_version": "0", "value": "yes"}).encode()
        status, response_headers, _ = self.request("POST", f"/check-ins/{today}/weight/answer", first, headers)
        self.assertEqual(status, 303)
        self.assertEqual(response_headers["Location"], f"/check-ins/{today}/weight?q=weight_kg")
        second = urllib.parse.urlencode({"question_id": "weight_kg", "expected_version": "0", "value": "72.4"}).encode()
        status, response_headers, _ = self.request("POST", f"/check-ins/{today}/weight/answer", second, headers)
        self.assertEqual(response_headers["Location"], f"/check-ins/{today}/weight?q=measurement_context")
        last = urllib.parse.urlencode({"question_id": "measurement_context", "expected_version": "0", "value": "morning_fasted"}).encode()
        status, response_headers, _ = self.request("POST", f"/check-ins/{today}/weight/answer", last, headers)
        self.assertEqual(status, 303)
        self.assertEqual(response_headers["Location"], f"/check-ins/{today}")
        status, _, completed_hub = self.request("GET", f"/check-ins/{today}")
        self.assertIn("72.4 kg", completed_hub.decode("utf-8"))
        status, _, settings = self.request("GET", "/check-ins/settings")
        self.assertEqual(status, 200)
        self.assertIn("每日状态设置", settings.decode("utf-8"))
        rejected = urllib.parse.urlencode({"expected_version": "0"}).encode()
        status, _, response = self.request(
            "POST", f"/check-ins/{today}/gut/skip", rejected,
            {"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://attacker.invalid"},
        )
        self.assertEqual(status, 400)
        self.assertIn("拒绝跨来源写入请求", response.decode("utf-8"))

    def test_loopback_origin_policy_and_real_post_actions(self):
        port = self.port
        self.assertEqual(parse_host_endpoint(f"[::1]:{port}", port), ("::1", port))
        self.assertTrue(origin_matches_host("127.0.0.1", port, f"http://localhost:{port}", "same-site"))
        self.assertTrue(origin_matches_host("localhost", port, f"http://[::1]:{port}", "same-site"))
        self.assertTrue(origin_matches_host("example.test", port, f"http://example.test:{port}", "same-origin"))
        self.assertFalse(origin_matches_host("127.0.0.1", port, f"http://localhost:{port + 1}", "same-site"))
        self.assertFalse(origin_matches_host("example.test", port, f"http://other.test:{port}", "cross-site"))
        self.assertTrue(origin_matches_host("127.0.0.1", port, "null", "same-origin"))
        self.assertTrue(origin_matches_host("localhost", port, "null", "none"))
        self.assertFalse(origin_matches_host("127.0.0.1", port, "null", "cross-site"))

        today = date.today().isoformat()
        base_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": f"localhost:{port}",
            "Origin": f"http://127.0.0.1:{port}",
            "Sec-Fetch-Site": "same-site",
        }
        answer = urllib.parse.urlencode({"question_id": "trained", "expected_version": "0", "value": "yes"}).encode()
        status, headers, _ = self.request("POST", f"/check-ins/{today}/training/answer", answer, base_headers)
        self.assertEqual(status, 303)
        self.assertIn("training_types", headers["Location"])

        multi = urllib.parse.urlencode([
            ("question_id", "training_types"), ("expected_version", "0"),
            ("value", "strength"), ("value", "cardio"),
        ]).encode()
        status, headers, _ = self.request("POST", f"/check-ins/{today}/training/answer", multi, base_headers)
        self.assertEqual(status, 303)
        self.assertIn("body_parts", headers["Location"])

        hunger = urllib.parse.urlencode({"question_id": "hunger_level", "expected_version": "0", "value": "3"}).encode()
        status, _, _ = self.request("POST", f"/check-ins/{today}/hunger/answer", hunger, base_headers)
        self.assertEqual(status, 303)
        discard = urllib.parse.urlencode({"expected_version": "0"}).encode()
        status, headers, _ = self.request("POST", f"/check-ins/{today}/hunger/discard-draft", discard, base_headers)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], f"/check-ins/{today}")

        skip = urllib.parse.urlencode({"expected_version": "0"}).encode()
        status, headers, _ = self.request("POST", f"/check-ins/{today}/gut/skip", skip, base_headers)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], f"/check-ins/{today}")

        settings_values = []
        for module_key in ("weight", "training", "hunger", "sleep", "gut"):
            settings_values.extend(((f"enabled_{module_key}", "1"), (f"frequency_{module_key}", "daily")))
        status, headers, _ = self.request(
            "POST", "/check-ins/settings", urllib.parse.urlencode(settings_values).encode(), base_headers,
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/check-ins/settings")

    def test_origin_policy_rejects_bad_port_null_cross_site_and_invalid_host(self):
        body = urllib.parse.urlencode({"expected_version": "999"}).encode()
        allowed_null = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": f"127.0.0.1:{self.port}", "Origin": "null", "Sec-Fetch-Site": "same-origin",
        }
        status, _, _ = self.request("POST", "/__origin_probe", b"", allowed_null)
        self.assertEqual(status, 404)
        allowed_ipv6_alias = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": f"[::1]:{self.port}", "Origin": f"http://localhost:{self.port}",
        }
        status, _, _ = self.request("POST", "/__origin_probe", b"", allowed_ipv6_alias)
        self.assertEqual(status, 404)
        cases = (
            {"Host": f"127.0.0.1:{self.port}", "Origin": f"http://localhost:{self.port + 1}"},
            {"Host": f"127.0.0.1:{self.port}", "Origin": "null", "Sec-Fetch-Site": "cross-site"},
            {"Host": f"evil.invalid:{self.port}", "Origin": f"http://evil.invalid:{self.port}"},
        )
        output = io.StringIO()
        with redirect_stderr(output):
            for extra in cases:
                headers = {"Content-Type": "application/x-www-form-urlencoded", **extra}
                status, _, response = self.rejected_post(
                    f"/check-ins/{date.today().isoformat()}/weight/skip", body, headers
                )
                self.assertEqual(status, 400)
                self.assertTrue(
                    "拒绝跨来源写入请求" in response.decode("utf-8")
                    or "Host 请求头不在允许范围" in response.decode("utf-8")
                )
        self.assertIn("Rejected POST origin", output.getvalue())
        self.assertIn("Sec-Fetch-Site", output.getvalue())

    def test_cross_origin_post_is_rejected(self):
        body = urllib.parse.urlencode({"materials": "synthetic"}).encode()
        status, _, response = self.request(
            "POST",
            "/tasks/material",
            body,
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://attacker.invalid",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("拒绝跨来源写入请求", response.decode("utf-8"))

    def test_non_loopback_requires_explicit_flag(self):
        result = subprocess.run(
            [sys.executable, "-m", "mealcircuit.server", "--host", "0.0.0.0", "--port", "0"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"--allow-remote", result.stderr)


if __name__ == "__main__":
    unittest.main()
