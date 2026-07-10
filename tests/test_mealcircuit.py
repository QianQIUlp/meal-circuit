from __future__ import annotations

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

from mealcircuit import ai, checkins, personalization, service
from mealcircuit import storage
from mealcircuit.configuration import configuration_status, initialize_private_home
from mealcircuit.db import init_db
from mealcircuit.migration import apply_migration, migration_preview
from mealcircuit.server import Handler, ThreadingHTTPServer, origin_matches_host, parse_host_endpoint
from mealcircuit.storage import db_path, resolve_data_path, upload_root
from mealcircuit.validation import ValidationError
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
        with self.assertRaises(ValidationError):
            service.complete_daily_review(today, daily_review_result(today))
        service.add_daily_record(today, "补充：晚间饥饿低")
        reopened = service.get_daily_review(today)
        self.assertEqual(reopened["status"], "pending")
        self.assertIsNone(reopened["result_json"])
        self.assertEqual(len(reopened["source_record_ids_json"]), 3)
        self.assertEqual(len(reopened["history"]), 1)
        self.assertEqual(reopened["history"][0]["result_json"]["tomorrow_menu"]["protein_target_g"], [100, 130])

    def test_daily_review_rejects_missing_or_wrong_menu(self):
        today = date.today().isoformat()
        service.add_daily_record(today, "测试记录")
        with self.assertRaises(ValidationError):
            service.complete_daily_review(today, {"system_status": "observe"})
        invalid = daily_review_result(today)
        invalid["tomorrow_menu"]["protein_target_g"] = [200, 220]
        with self.assertRaises(ValidationError):
            service.complete_daily_review(today, invalid)

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
        self.assertEqual(len(first_requeue["history"]), 1)
        service.skip_checkin_module(today, "gut", 0)
        second_update = service.get_daily_review(today)
        self.assertEqual(len(second_update["history"]), 1)
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
        with self.assertRaisesRegex(ValidationError, "连续晚餐不得重复"):
            service.complete_daily_review(second_date, repeated)

        missing_carryover = home_cooking_review_result(
            second_date, dish_key="tomato_tofu", flavor="tomato_savory"
        )
        with self.assertRaisesRegex(ValidationError, "食材承接裁决不完整"):
            service.complete_daily_review(second_date, missing_carryover)

        reuse_same_ingredient = add_carryover_decisions(
            home_cooking_review_result(second_date, dish_key="tomato_tofu", flavor="tomato_savory"),
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

        repeated = home_cooking_review_result(second_date)
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

    def test_pages_and_material_form(self):
        for path in ("/", "/daily", "/history", "/tasks/photo", "/tasks/material", "/tasks", "/ai", "/foods", "/overview"):
            status, _, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertIn(b"MealCircuit", body)
        status, home_headers, home = self.request("GET", "/")
        decoded_home = home.decode("utf-8")
        for label in ("今日结论", "今日状态", "明日计划", "处理队列", "照片任务", "原材料"):
            self.assertIn(label, decoded_home)
        self.assertIn('class="app-sidebar"', decoded_home)
        self.assertIn('aria-current="page"', decoded_home)
        self.assertIn('href="/assets/ui/app.css?v=', decoded_home)
        self.assertIn('rel="icon" href="/assets/ui/favicon.svg"', decoded_home)
        self.assertIn('src="/assets/ui/theme-init.js?v=', decoded_home)
        self.assertIn('data-theme-toggle', decoded_home)
        self.assertIn('href="/history"', decoded_home)
        self.assertNotIn("最近任务", decoded_home)
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
        status, _, daily = self.request("GET", "/daily")
        self.assertIn("尚未记录", daily.decode("utf-8"))
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
        self.assertIn("API Key 接入", decoded)
        self.assertIn("启用本次运行的 API Key 模式", decoded)
        self.assertNotIn("secret-runtime-key", decoded)

        form = urllib.parse.urlencode({
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "api_key": "secret-runtime-key",
            "timeout_seconds": "33",
            "max_output_tokens": "444",
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

        status, _, configured = self.request("GET", "/ai")
        decoded_configured = configured.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("已启用", decoded_configured)
        self.assertIn("MEALCIRCUIT_DEEPSEEK_API_KEY 已设置", decoded_configured)
        self.assertNotIn("secret-runtime-key", decoded_configured)

        status, headers, _ = self.request("POST", "/ai/disable", b"", {"Content-Length": "0"})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/ai")
        self.assertNotIn("MEALCIRCUIT_AI_PROVIDER", os.environ)
        self.assertNotIn("MEALCIRCUIT_DEEPSEEK_API_KEY", os.environ)

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
        self.assertEqual(headers["Location"], f"/reviews/{review_date}")
        pending_status, _, pending_body = self.request("GET", headers["Location"])
        self.assertEqual(pending_status, 200)
        self.assertIn("等待 Agent 生成核心建议和次日菜单", pending_body.decode("utf-8"))
        daily_pending_status, _, daily_pending = self.request("GET", "/daily")
        self.assertEqual(daily_pending_status, 200)
        decoded_daily_pending = daily_pending.decode("utf-8")
        self.assertIn("等待 Agent 生成核心建议和明日菜单", decoded_daily_pending)
        self.assertIn(f'action="/reviews/{review_date}/generate"', decoded_daily_pending)
        self.assertIn("用 API Key 生成今日建议", decoded_daily_pending)
        result = daily_review_result(review_date, unsafe=True)
        result["priority_food_decisions"] = [{"food_id": priority_food["id"], "decision": "use", "reason": "早餐主食优先"}]
        service.complete_daily_review(review_date, result)
        status, _, detail = self.request("GET", f"/reviews/{review_date}")
        decoded = detail.decode("utf-8")
        self.assertEqual(status, 200)
        for label in ("核心建议", "明日计划", "蛋白目标", "早餐", "午餐", "晚餐", "条件加餐", "训练日调整", "肠胃异常调整"):
            self.assertIn(label, decoded)
        self.assertIn("优先食品裁决", decoded)
        self.assertIn("优先全麦面包", decoded)
        self.assertIn(f'/foods/{priority_food["id"]}', decoded)
        self.assertIn("&lt;script&gt;推断&lt;/script&gt;", decoded)
        self.assertNotIn("<script>推断</script>", decoded)
        status, _, daily = self.request("GET", "/daily")
        self.assertEqual(status, 200)
        self.assertIn("今日建议与明日菜单", daily.decode("utf-8"))
        status, _, history = self.request("GET", "/history")
        decoded_history = history.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("历史建议", decoded_history)
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
            json.dumps({**TEST_SETTINGS, "home_cooking": HOME_COOKING}, ensure_ascii=False), encoding="utf-8"
        )
        review_date = date.today().isoformat()
        service.add_daily_record(review_date, "独居晚餐页面测试")
        result = home_cooking_review_result(review_date)
        result["tomorrow_menu"]["meals"][2]["recipe_card"]["title"] = "番茄<script>鸡肉</script>"
        service.complete_daily_review(review_date, result)
        status, _, body = self.request("GET", f"/reviews/{review_date}")
        decoded = body.decode("utf-8")
        self.assertEqual(status, 200)
        for label in (
            "快速组装", "食堂 / 外食", "在家下厨", "BEGINNER DINNER", "明日采购清单",
            "可选网购组件", "3 日食材复用方向", "失败补救", "清洁成本", "肠胃降级",
        ):
            self.assertIn(label, decoded)
        self.assertIn("番茄&lt;script&gt;鸡肉&lt;/script&gt;", decoded)
        self.assertNotIn("番茄<script>鸡肉</script>", decoded)

    def test_checkin_web_question_flow_settings_and_origin(self):
        today = date.today().isoformat()
        status, _, hub = self.request("GET", f"/check-ins/{today}")
        decoded = hub.decode("utf-8")
        self.assertEqual(status, 200)
        for label in ("每日状态", "体重", "训练", "饥饿与饱腹", "睡眠", "肠胃反应", "0/5"):
            self.assertIn(label, decoded)
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
