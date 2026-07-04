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

from mealcircuit import service
from mealcircuit import storage
from mealcircuit.configuration import configuration_status, initialize_private_home
from mealcircuit.db import init_db
from mealcircuit.migration import apply_migration, migration_preview
from mealcircuit.server import Handler, ThreadingHTTPServer
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


def configure_private_home(path: Path) -> dict[str, str | None]:
    old = {key: os.environ.get(key) for key in ("MEALCIRCUIT_HOME", "MEALCIRCUIT_DB")}
    os.environ["MEALCIRCUIT_HOME"] = str(path)
    os.environ["MEALCIRCUIT_DB"] = str(path / "mealcircuit.db")
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


class MealCircuitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_environment = configure_private_home(Path(self.temp.name))
        init_db()

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
        result = daily_review_result(today)
        result["tomorrow_menu"]["protein_target_g"] = [120, 150]
        result["tomorrow_menu"]["environment"] = "家庭用餐"
        self.assertEqual(service.complete_daily_review(today, result)["status"], "completed")

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

    def test_pages_and_material_form(self):
        for path in ("/", "/daily", "/history", "/tasks/photo", "/tasks/material", "/foods", "/overview"):
            status, _, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertIn(b"MealCircuit", body)
        status, _, home = self.request("GET", "/")
        decoded_home = home.decode("utf-8")
        for label in ("今日建议", "食物照片", "原材料分析"):
            self.assertIn(label, decoded_home)
        self.assertIn('href="/history"', decoded_home)
        self.assertNotIn("最近任务", decoded_home)
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
        self.assertIn("等待 Agent 生成核心建议和明日菜单", daily_pending.decode("utf-8"))
        result = daily_review_result(review_date, unsafe=True)
        result["priority_food_decisions"] = [{"food_id": priority_food["id"], "decision": "use", "reason": "早餐主食优先"}]
        service.complete_daily_review(review_date, result)
        status, _, detail = self.request("GET", f"/reviews/{review_date}")
        decoded = detail.decode("utf-8")
        self.assertEqual(status, 200)
        for label in ("核心建议", "食堂菜单", "每日蛋白目标", "早餐", "午餐", "晚餐", "条件加餐", "训练日调整", "肠胃异常调整"):
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
