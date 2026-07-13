from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from mealcircuit import adaptive, personalization, portability, service
from mealcircuit.configuration import initialize_private_home, load_resolved_settings
from mealcircuit.db import CURRENT_SCHEMA_VERSION, connect, init_db, row_dict
from mealcircuit.validation import ValidationError


SETTINGS = {
    "meal_environment": "旧设置环境",
    "protein_target_g": [90, 120],
    "portion_method": "旧份量方式",
    "missing_training_default": "按普通日生成",
    "compensation_boundary": "不跳餐、不清零主食、不极端压低热量",
}


def _nutrition(low: int = 10, high: int = 20) -> dict:
    return {
        "energy_kcal": [low, high], "protein_g": [low, high],
        "carbs_g": [low, high], "fat_g": [low, high],
    }


def _review_result(review_date: str, dinner_food: str = "番茄鸡肉") -> dict:
    tomorrow = date.fromisoformat(review_date) + timedelta(days=1)
    return {
        "system_status": "observe",
        "facts": ["已记录实际饮食"],
        "inferences": ["先观察执行情况"],
        "core_advice": ["维持三餐并记录真实执行阻力"],
        "do_not_adjust": ["不跳餐"],
        "risk_signals": [],
        "priority_food_decisions": [],
        "tomorrow_menu": {
            "date": tomorrow.isoformat(),
            "environment": "旧设置环境",
            "protein_target_g": [90, 120],
            "meals": [
                {"name": "早餐", "foods": ["鸡蛋", "牛奶"], "portion_guidance": "标准份", "protein_g": [20, 25], "substitutions": ["豆浆"]},
                {"name": "午餐", "foods": ["食堂瘦肉", "米饭", "蔬菜"], "portion_guidance": "标准份", "protein_g": [35, 45], "substitutions": ["鱼类"]},
                {"name": "晚餐", "foods": [dinner_food, "米饭", "蔬菜"], "portion_guidance": "标准份", "protein_g": [35, 45], "substitutions": ["豆腐"]},
            ],
            "conditional_snack": {"condition": "确有饥饿时", "options": ["酸奶"]},
            "training_adjustment": "训练日按实际状态增加主食。",
            "gut_adjustment": "异常时降低辣、酸和油。",
        },
        "one_line_review": "先保证计划可执行，再调整营养策略。",
    }


class AdaptiveDomainTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.old = {key: os.environ.get(key) for key in ("MEALCIRCUIT_HOME", "MEALCIRCUIT_DB")}
        os.environ["MEALCIRCUIT_HOME"] = str(self.home)
        os.environ["MEALCIRCUIT_DB"] = str(self.home / "mealcircuit.db")
        (self.home / "settings.json").write_text(json.dumps(SETTINGS, ensure_ascii=False), encoding="utf-8")
        (self.home / "profile.md").write_text("# 旧档案\n\n喜欢简单晚餐。\n", encoding="utf-8")
        init_db()

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def _fill_session(
        self,
        *,
        pregnant: bool = False,
        target_weight: float | None = None,
        safety_overrides: dict | None = None,
        professional_guidance: dict | None = None,
        constraint_overrides: dict | None = None,
    ):
        session = personalization.start_onboarding()
        payloads = {
            "welcome": {"privacy_ack": True},
            "goals": {
                "primary_goal": "body_recomposition",
                "secondary_goals": ["performance"],
                "motivation": "降低脂肪，同时维持力量训练表现。",
                "success_metrics": ["weight_trend", "training_performance", "execution_rate"],
                "target_weight_kg": target_weight,
            },
            "baseline": {
                "age_years": 30,
                "height_cm": 180,
                "weight_kg": 80,
                "physiological_input": "male",
                "activity_level": "moderate",
            },
            "safety": {
                "life_stage": "pregnant" if pregnant else "adult",
                "therapeutic_diet": False,
                "medication_affects_nutrition": False,
                "eating_disorder_risk": False,
                "rapid_unexplained_change": False,
                "severe_persistent_symptoms": False,
                "severe_allergy_management": False,
            },
            "training": {"types": ["strength"], "frequency_per_week": 4},
            "constraints": {
                "meal_environment": "工作日食堂，晚餐在家",
                "portion_method": "手掌与拳头份量法",
                "cooking_time_minutes": 20,
                "equipment": ["stovetop_pan", "rice_cooker"],
                "food_exclusions": ["花生"],
                "preferences": ["酸辣", "番茄"],
                "question_budget": 2,
            },
        }
        if safety_overrides:
            payloads["safety"].update(safety_overrides)
        if professional_guidance is not None:
            payloads["safety"]["professional_guidance"] = professional_guidance
        if constraint_overrides:
            payloads["constraints"].update(constraint_overrides)
        current = session
        for step, payload in payloads.items():
            current = personalization.save_onboarding_step(current["id"], step, payload, current["version"])
        return current

    def _complete_standard_profile(self):
        session = self._fill_session()
        return personalization.complete_onboarding(
            session["id"], session["version"],
            {"accept_profile": True, "accept_strategy": True, "planning_mode": "portion_guided"},
        )

    def _publish_plan(self, review_date: str, dinner_food: str = "番茄鸡肉") -> dict:
        if personalization.onboarding_status()["status"] == "setup_required":
            self._complete_standard_profile()
        service.add_daily_record(review_date, "记录当天实际饮食。")
        result = _review_result(review_date, dinner_food)
        variant = date.fromisoformat(review_date).day
        meals = {meal["name"]: meal for meal in result["tomorrow_menu"]["meals"]}
        meals["早餐"]["foods"] = [f"早餐谷物{variant}", "牛奶", f"水果{variant}"]
        meals["午餐"]["foods"] = [f"午餐蛋白{variant}", "米饭", f"蔬菜{variant}"]
        meals["晚餐"]["foods"] = [f"{dinner_food}·轮换{variant}", "米饭", f"蔬菜{variant}"]
        meals["晚餐"]["strategy_key"] = f"strategy:{dinner_food}"
        settings = load_resolved_settings()
        result["tomorrow_menu"]["protein_target_g"] = settings["protein_target_g"]
        result["tomorrow_menu"]["environment"] = settings["meal_environment"]
        service.complete_daily_review(review_date, result)
        return adaptive.get_plan_for_date((date.fromisoformat(review_date) + timedelta(days=1)).isoformat())

    def test_schema_migration_and_json_columns(self):
        with closing(sqlite3.connect(self.home / "mealcircuit.db")) as conn:
            version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(CURRENT_SCHEMA_VERSION, version)
        self.assertTrue({
            "onboarding_sessions", "profile_versions", "goal_versions", "strategy_versions",
            "task_evidence_links", "plan_execution_feedback", "adaptation_candidates", "agent_runs",
        }.issubset(tables))

    def test_old_database_is_backed_up_before_schema_upgrade(self):
        other = self.home / "old.db"
        with closing(sqlite3.connect(other)) as conn:
            conn.execute("CREATE TABLE legacy_marker(value TEXT)")
            conn.execute("INSERT INTO legacy_marker VALUES('kept')")
            conn.commit()
        init_db(other)
        backups = list((self.home / "backups").glob(f"pre-schema-v{CURRENT_SCHEMA_VERSION}-*.db"))
        self.assertEqual(1, len(backups))
        with closing(sqlite3.connect(backups[0])) as conn:
            self.assertEqual("kept", conn.execute("SELECT value FROM legacy_marker").fetchone()[0])
            self.assertEqual("ok", conn.execute("PRAGMA integrity_check").fetchone()[0])

    def test_v3_database_migrates_rule_and_experiment_versions_without_data_loss(self):
        legacy = self.home / "legacy-v3.db"
        init_db(legacy)
        with closing(sqlite3.connect(legacy)) as conn:
            conn.execute("ALTER TABLE adaptive_rules DROP COLUMN version")
            conn.execute("ALTER TABLE adaptive_experiments DROP COLUMN version")
            conn.execute("DELETE FROM schema_migrations WHERE version=4")
            conn.commit()
        init_db(legacy)
        with closing(sqlite3.connect(legacy)) as conn:
            rule_columns = {row[1] for row in conn.execute("PRAGMA table_info(adaptive_rules)")}
            experiment_columns = {row[1] for row in conn.execute("PRAGMA table_info(adaptive_experiments)")}
            schema_version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        self.assertIn("version", rule_columns)
        self.assertIn("version", experiment_columns)
        self.assertEqual(CURRENT_SCHEMA_VERSION, schema_version)
        self.assertEqual(1, len(list((self.home / "backups").glob("pre-schema-v4-*.db"))))

    def test_standard_onboarding_creates_versioned_profile_goal_and_strategy(self):
        session = self._fill_session()
        preview = personalization.onboarding_preview(session["id"])
        self.assertEqual("standard", preview["safety"]["mode"])
        self.assertEqual(1780, preview["target_assessment"]["resting_energy_estimate_kcal"])
        self.assertEqual([2492, 2848], preview["target_assessment"]["maintenance_energy_estimate_kcal"])
        self.assertEqual([112, 160], preview["target_assessment"]["protein_candidates"][0]["target_g"])

        current = personalization.complete_onboarding(
            session["id"], session["version"],
            {"accept_profile": True, "accept_strategy": True, "planning_mode": "portion_guided"},
        )
        self.assertEqual("standard", current["safety"]["mode"])
        self.assertEqual("body_recomposition", current["goals"][0]["goal_json"]["type"])
        self.assertEqual([112, 160], current["strategy"]["strategy_json"]["protein_target_g"])
        self.assertEqual(1, len(current["targets"]))
        self.assertEqual("user_confirmed_suggestion", current["targets"][0]["source_kind"])
        self.assertEqual("goal_and_training_factor", current["targets"][0]["method"])
        self.assertEqual(personalization.TARGET_POLICY_VERSION, current["targets"][0]["policy_version"])
        resolved = personalization.resolved_settings(SETTINGS)
        self.assertEqual([112, 160], resolved["protein_target_g"])
        self.assertEqual("工作日食堂，晚餐在家", resolved["meal_environment"])
        self.assertEqual(1, resolved["sources"]["strategy"]["version"])

    def test_onboarding_persists_per_meal_preparation_modes_in_versioned_personal_strategy(self):
        modes = {
            "breakfast": "quick_assembly",
            "lunch": "home_cook",
            "dinner": "home_cook",
        }
        session = self._fill_session(constraint_overrides={
            "meal_environment": "午餐和晚餐在家做",
            "meal_modes": modes,
            "cooking_time_minutes": 25,
        })
        current = personalization.complete_onboarding(
            session["id"], session["version"],
            {"accept_profile": True, "accept_strategy": True, "planning_mode": "portion_guided"},
        )
        self.assertEqual(modes, current["profile"]["profile_json"]["constraints"]["meal_modes"])
        self.assertEqual(modes, current["strategy"]["strategy_json"]["meal_modes"])
        resolved = personalization.resolved_settings(SETTINGS)
        self.assertEqual(modes, resolved["meal_modes"])
        self.assertTrue(resolved["home_cooking"]["enabled"])
        self.assertEqual("lunch_and_dinner", resolved["home_cooking"]["meal_scope"])
        self.assertEqual(25, resolved["home_cooking"]["weekday_time_limit_minutes"])

        revision = personalization.start_onboarding()
        self.assertEqual(modes, revision["answers_json"]["constraints"]["meal_modes"])

    def test_single_day_meal_modes_override_personal_defaults_without_changing_strategy(self):
        defaults = {"breakfast": "quick_assembly", "lunch": "home_cook", "dinner": "home_cook"}
        session = self._fill_session(constraint_overrides={"meal_modes": defaults})
        personalization.complete_onboarding(
            session["id"], session["version"],
            {"accept_profile": True, "accept_strategy": True, "planning_mode": "portion_guided"},
        )
        review_date = (date.today() - timedelta(days=1)).isoformat()
        questions = adaptive.schedule_questions(review_date)
        meal_question = next(item for item in questions if item["question_key"] == "tomorrow_meal_modes")
        adaptive.answer_question(
            meal_question["id"],
            {"breakfast": "inherit", "lunch": "eat_out", "dinner": "inherit"},
            meal_question["version"],
        )

        service.add_daily_record(review_date, "明天午餐外食，晚餐自己做。")
        context = service.daily_review_context(review_date)
        self.assertEqual(
            {"breakfast": "quick_assembly", "lunch": "eat_out", "dinner": "home_cook"},
            context["meal_mode_resolution"]["effective_meal_modes"],
        )
        self.assertEqual({"lunch": "eat_out"}, context["meal_mode_resolution"]["overrides"])
        self.assertEqual(date.today().isoformat(), context["meal_mode_resolution"]["target_date"])
        schema_meals = {item["name"]: item for item in context["result_schema"]["tomorrow_menu"]["meals"]}
        self.assertEqual("eat_out", schema_meals["午餐"]["mode"])
        self.assertIn("eat_out_guidance", schema_meals["午餐"])
        self.assertNotIn("recipe_card", schema_meals["午餐"])
        self.assertIn("recipe_card", schema_meals["晚餐"])
        self.assertEqual(defaults, personalization.active_personalization()["strategy"]["strategy_json"]["meal_modes"])

        answered = adaptive.get_question_event(meal_question["id"])
        with patch("mealcircuit.adaptive._queue_changed_date") as requeue:
            revised = adaptive.answer_question(
                answered["id"],
                {"breakfast": "inherit", "lunch": "home_cook", "dinner": "inherit"},
                answered["version"],
            )
        self.assertEqual("home_cook", revised["answer_json"]["lunch"])
        self.assertEqual(answered["version"] + 1, revised["version"])
        requeue.assert_called_once_with(review_date, "明日逐餐准备方式已修订")
        self.assertEqual(defaults, personalization.active_personalization()["strategy"]["strategy_json"]["meal_modes"])

        service.add_daily_record(date.today().isoformat(), "下一天没有逐餐临时覆盖。")
        next_context = service.daily_review_context(date.today().isoformat())
        self.assertEqual(defaults, next_context["meal_mode_resolution"]["effective_meal_modes"])

    def test_legacy_mixed_environment_answer_does_not_guess_meal_overrides(self):
        self._complete_standard_profile()
        review_date = (date.today() - timedelta(days=1)).isoformat()
        questions = adaptive.schedule_questions(review_date)
        for item in questions:
            if item["question_key"] == "tomorrow_training":
                adaptive.answer_question(item["id"], "unknown", item["version"])
            elif item["question_key"] == "tomorrow_meal_modes":
                adaptive.answer_question(
                    item["id"],
                    {"breakfast": "inherit", "lunch": "inherit", "dinner": "inherit"},
                    item["version"],
                )
        questions = adaptive.schedule_questions(review_date)
        environment = next(item for item in questions if item["question_key"] == "tomorrow_environment")
        adaptive.answer_question(environment["id"], "mixed", environment["version"])
        service.add_daily_record(review_date, "明天用餐地点混合，但没有说明具体餐次。")
        context = service.daily_review_context(review_date)
        self.assertEqual({}, context["meal_mode_resolution"]["overrides"])

    def test_first_meal_override_answer_requeues_an_already_completed_review(self):
        self._complete_standard_profile()
        review_date = (date.today() - timedelta(days=1)).isoformat()
        question = next(
            item for item in adaptive.schedule_questions(review_date)
            if item["question_key"] == "tomorrow_meal_modes"
        )
        with (
            patch("mealcircuit.service.get_daily_review", return_value={"status": "completed"}),
            patch("mealcircuit.adaptive._queue_changed_date") as requeue,
        ):
            adaptive.answer_question(
                question["id"],
                {"breakfast": "inherit", "lunch": "eat_out", "dinner": "inherit"},
                question["version"],
            )
        requeue.assert_called_once_with(review_date, "明日逐餐准备方式已修订")

    def test_web_first_run_template_becomes_valid_after_onboarding(self):
        (self.home / "settings.json").unlink()
        initialized = initialize_private_home()
        created = {Path(item).resolve() for item in initialized["created"]}
        self.assertIn((self.home / "settings.json").resolve(), created)
        self._complete_standard_profile()
        resolved = load_resolved_settings()
        self.assertEqual([112.0, 160.0], resolved["protein_target_g"])
        self.assertEqual("工作日食堂，晚餐在家", resolved["meal_environment"])

        revision = personalization.start_onboarding()
        baseline = dict(revision["answers_json"]["baseline"])
        baseline.update({"height_cm": None, "weight_kg": None, "physiological_input": "unspecified"})
        revision = personalization.save_onboarding_step(
            revision["id"], "baseline", baseline, revision["version"]
        )
        personalization.complete_onboarding(
            revision["id"], revision["version"],
            {"accept_profile": True, "accept_strategy": True, "planning_mode": "portion_guided"},
        )
        self.assertIsNone(load_resolved_settings()["protein_target_g"])
        service.add_daily_record("2026-07-10", "没有体重时仍使用份量法。")
        result = _review_result("2026-07-10")
        result["tomorrow_menu"]["protein_target_g"] = None
        result["tomorrow_menu"]["environment"] = load_resolved_settings()["meal_environment"]
        completed = service.submit_daily_review("2026-07-10", result)
        self.assertIsNone(completed["result_json"]["tomorrow_menu"]["protein_target_g"])

    def test_observation_mode_never_creates_nutrition_targets(self):
        session = self._fill_session(pregnant=True)
        preview = personalization.onboarding_preview(session["id"])
        self.assertEqual("clinician_guided", preview["safety"]["mode"])
        self.assertIsNone(preview["target_assessment"]["resting_energy_estimate_kcal"])
        self.assertEqual([], preview["target_assessment"]["protein_candidates"])
        current = personalization.complete_onboarding(
            session["id"], session["version"], {"accept_profile": True}
        )
        self.assertEqual("observation", current["strategy"]["mode"])
        self.assertIsNone(current["strategy"]["strategy_json"]["protein_target_g"])
        self.assertIsNone(personalization.resolved_settings(SETTINGS)["protein_target_g"])
        self.assertFalse(personalization.generation_policy("daily")["allowed"])

    def test_large_weight_difference_requires_explicit_protein_choice(self):
        session = self._fill_session(target_weight=60)
        preview = personalization.onboarding_preview(session["id"])
        self.assertEqual(2, len(preview["target_assessment"]["protein_candidates"]))
        with self.assertRaisesRegex(ValidationError, "请选择蛋白目标参考体重"):
            personalization.complete_onboarding(
                session["id"], session["version"], {"accept_profile": True, "accept_strategy": True}
            )
        current = personalization.complete_onboarding(
            session["id"], session["version"],
            {"accept_profile": True, "accept_strategy": True, "protein_target_g": [84, 120]},
        )
        self.assertEqual([84.0, 120.0], current["strategy"]["strategy_json"]["protein_target_g"])

    def test_custom_goal_requires_the_users_own_outcome_text(self):
        session = personalization.start_onboarding()
        with self.assertRaisesRegex(ValidationError, "自定义目标"):
            personalization.save_onboarding_step(session["id"], "goals", {
                "primary_goal": "custom", "custom_goal_text": "", "secondary_goals": [],
                "motivation": "轮班生活", "success_metrics": ["execution_rate"],
            }, session["version"])
        updated = personalization.save_onboarding_step(session["id"], "goals", {
            "primary_goal": "custom", "custom_goal_text": "轮班期间稳定进食并保持白天精力",
            "secondary_goals": [], "motivation": "降低轮班饮食摩擦",
            "success_metrics": ["execution_rate", "energy_state"],
        }, session["version"])
        self.assertEqual(
            "轮班期间稳定进食并保持白天精力",
            personalization.get_onboarding(updated["id"])["answers_json"]["goals"]["custom_goal_text"],
        )

    def test_target_validity_is_enforced_and_expired_targets_leave_planning_context(self):
        session = self._fill_session()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValidationError, "有效期不能早于今天"):
            personalization.complete_onboarding(
                session["id"], session["version"], {
                    "accept_profile": True, "accept_strategy": True,
                    "target_valid_until": yesterday,
                },
            )
        current = personalization.complete_onboarding(
            session["id"], session["version"],
            {"accept_profile": True, "accept_strategy": True},
        )
        self.assertEqual(1, len(current["targets"]))
        with connect() as conn:
            conn.execute(
                "UPDATE nutrition_target_versions SET valid_until=? WHERE id=?",
                (yesterday, current["targets"][0]["id"]),
            )
        self.assertEqual([], personalization.active_personalization()["targets"])
        self.assertIsNone(personalization.resolved_settings(SETTINGS)["protein_target_g"])

    def test_onboarding_uses_optimistic_version_and_metric_history(self):
        session = personalization.start_onboarding()
        updated = personalization.save_onboarding_step(
            session["id"], "welcome", {"privacy_ack": True}, session["version"]
        )
        with self.assertRaisesRegex(ValidationError, "已变化"):
            personalization.save_onboarding_step(
                session["id"], "welcome", {"privacy_ack": True}, session["version"]
            )
        self.assertEqual(session["version"] + 1, updated["version"])
        metric = personalization.record_metric("waist_cm", "2026-07-10", {"value": 82.5})
        self.assertEqual({"value": 82.5}, metric["value_json"])
        self.assertEqual(metric["id"], personalization.list_metrics("waist_cm")[0]["id"])

    def test_setup_gate_and_restricted_fact_only_schema_are_enforced(self):
        task = service.create_photo_task(
            __import__("io").BytesIO(b"\x89PNG\r\n\x1a\n" + b"meal"), "事实记录"
        )
        with self.assertRaisesRegex(ValidationError, "先完成"):
            service.task_context(task["id"])
        with self.assertRaisesRegex(ValidationError, "先完成"):
            service.complete_task(task["id"], {})

        session = self._fill_session(pregnant=True)
        personalization.complete_onboarding(
            session["id"], session["version"], {"accept_profile": True}
        )
        context = service.task_context(task["id"])
        self.assertTrue(context["generation_policy"]["fact_only"])
        self.assertNotIn("advice", context["result_schema"])
        with self.assertRaisesRegex(ValidationError, "不得包含 advice"):
            service.complete_task(task["id"], {
                "summary": "可见一份餐食",
                "candidates": [{
                    "name": "未知餐食", "portion_range": "可见一份",
                    "nutrition": _nutrition(), "confidence": 0.5,
                }],
                "unknowns": ["油量未知"],
                "advice": ["不应出现"],
            })
        service.complete_task(task["id"], {
            "summary": "可见一份餐食",
            "candidates": [{
                "name": "未知餐食", "portion_range": "可见一份",
                "nutrition": _nutrition(), "confidence": 0.5,
            }],
            "unknowns": ["油量未知"],
        })
        service.add_daily_record("2026-07-10", "仅记录实际情况。")
        with self.assertRaisesRegex(ValidationError, "专业指导"):
            service.daily_review_context("2026-07-10")

    def test_clinician_guided_targets_require_source_and_keep_provenance(self):
        session = self._fill_session(
            pregnant=True,
            professional_guidance={
                "confirmed": True,
                "source": "注册营养师书面计划",
                "summary": "按个人情况维持规律三餐并使用给定蛋白范围。",
                "confirmed_on": "2026-07-10",
                "valid_until": "2026-08-10",
            },
        )
        current = personalization.complete_onboarding(
            session["id"],
            session["version"],
            {
                "accept_profile": True,
                "accept_strategy": True,
                "professional_targets": {"protein_g": [90, 110]},
            },
        )
        self.assertEqual("clinician_guided", current["safety"]["mode"])
        self.assertTrue(personalization.generation_policy("daily")["allowed"])
        self.assertEqual([90.0, 110.0], personalization.resolved_settings(SETTINGS)["protein_target_g"])
        self.assertEqual("clinician_provided", current["targets"][0]["source_kind"])
        self.assertEqual("professional_constraint", current["targets"][0]["method"])

    def test_feedback_revisions_are_append_only(self):
        self._complete_standard_profile()
        plan = self._publish_plan("2026-07-09")
        dinner = next(item for item in plan["menu"]["meals"] if item["name"] == "晚餐")
        first = adaptive.save_plan_feedback(
            plan["plan_date"], dinner["plan_item_id"], "modified",
            reason_codes=["not_enough_time"], actor_source="web",
        )
        second = adaptive.save_plan_feedback(
            plan["plan_date"], dinner["plan_item_id"], "followed",
            outcome={"result": "appropriate"}, expected_version=first["version"], actor_source="cli",
        )
        history = adaptive.plan_feedback_history(first["id"])
        self.assertEqual([1, 2], [item["event_version"] for item in history])
        self.assertEqual(["modified", "followed"], [item["payload_json"]["status"] for item in history])
        self.assertEqual(["web", "cli"], [item["actor_source"] for item in history])
        self.assertEqual(2, second["version"])

    def test_adaptive_feedback_question_updates_the_plan_event(self):
        self._complete_standard_profile()
        plan = self._publish_plan("2026-07-09")
        questions = adaptive.schedule_questions(plan["plan_date"])
        feedback_question = next(item for item in questions if item["category"] == "feedback")
        answered = adaptive.answer_question(
            feedback_question["id"],
            {"status": "skipped", "reason_codes": ["schedule_change"], "actual_text": "临时加班"},
            feedback_question["version"],
        )
        self.assertEqual("answered", answered["status"])
        updated_plan = adaptive.get_plan_for_date(plan["plan_date"])
        feedback = updated_plan["feedback"][feedback_question["subject_json"]["plan_item_id"]]
        self.assertEqual("skipped", feedback["status"])
        self.assertEqual("question", adaptive.plan_feedback_history(feedback["id"])[0]["actor_source"])

    def test_agent_run_and_task_source_manifest_are_persisted_without_key(self):
        self._complete_standard_profile()
        task = service.create_photo_task(
            __import__("io").BytesIO(b"\x89PNG\r\n\x1a\n" + b"meal"), "午餐"
        )

        class Provider:
            def generate(self, request):
                return {
                    "summary": "可见一份午餐",
                    "candidates": [{
                        "name": "午餐", "portion_range": "一份",
                        "nutrition": _nutrition(), "confidence": 0.6,
                    }],
                    "unknowns": ["油量未知"],
                    "advice": ["结合全天执行记录"],
                }

        completed = service.generate_task_result(task["id"], Provider())
        self.assertTrue(completed["agent_run_id"])
        self.assertEqual(completed["agent_run_id"], completed["source_manifest_json"]["agent_run_id"])
        self.assertTrue(completed["source_manifest_json"]["doctrine"]["sha256"])
        self.assertEqual(personalization.TARGET_POLICY_VERSION, completed["policy_version"])
        with connect() as conn:
            run = row_dict(conn.execute("SELECT * FROM agent_runs WHERE id=?", (completed["agent_run_id"],)).fetchone())
        self.assertEqual("completed", run["status"])
        self.assertTrue(run["result_hash"])
        self.assertNotIn("api_key", json.dumps(run, ensure_ascii=False).lower())

        manual = service.create_material_task("豆腐半盒")
        submitted = service.submit_task_result(manual["id"], {
            "summary": "可见原材料事实", "combinations": ["豆腐蔬菜"],
            "batch_nutrition": _nutrition(), "per_serving_nutrition": _nutrition(),
            "gaps": ["总量未知"], "risks": [], "minimal_adjustments": ["按实际份量调整"],
        })
        self.assertEqual(submitted["agent_run_id"], submitted["source_manifest_json"]["agent_run_id"])
        with connect() as conn:
            external = row_dict(conn.execute(
                "SELECT * FROM agent_runs WHERE id=?", (submitted["agent_run_id"],)
            ).fetchone())
        self.assertEqual("external_agent", external["provider"])
        self.assertEqual("completed", external["status"])

        invalid_manual = service.create_material_task("未知原料")
        with self.assertRaises(ValidationError):
            service.submit_task_result(invalid_manual["id"], {"summary": "缺少必要字段"})
        self.assertEqual("pending", service.get_task(invalid_manual["id"])["status"])
        with connect() as conn:
            failed = row_dict(conn.execute(
                "SELECT * FROM agent_runs WHERE status='failed' ORDER BY started_at DESC LIMIT 1"
            ).fetchone())
        self.assertEqual("external_agent", failed["provider"])
        self.assertIn("ValidationError", failed["error_summary"])

    def test_portable_bundle_preview_and_restore_round_trip(self):
        self._complete_standard_profile()
        service.add_daily_record("2026-07-09", "导出前记录。")
        adaptive.create_inventory_item("北豆腐", "半盒", expires_on="2026-07-12")
        bundle = self.home / "exports" / "round-trip.zip"
        exported = portability.export_bundle(bundle)
        self.assertTrue(Path(exported["path"]).samefile(bundle))
        preview = portability.preview_import(bundle)
        self.assertEqual("ok", preview["database_integrity"])
        self.assertGreaterEqual(preview["table_counts"]["daily_records"], 1)
        service.add_daily_record("2026-07-10", "导出后临时记录。")
        (self.home / "uploads").mkdir(exist_ok=True)
        (self.home / "uploads" / "stale-after-export.png").write_bytes(b"stale")
        changed_settings = {**SETTINGS, "meal_environment": "导出后临时环境"}
        (self.home / "settings.json").write_text(
            json.dumps(changed_settings, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValidationError, "confirm=True"):
            portability.restore_bundle(bundle)
        restored = portability.restore_bundle(bundle, confirm=True)
        self.assertTrue(restored["restored"])
        self.assertTrue(Path(restored["pre_restore_backup"]).is_file())
        self.assertEqual(".zip", Path(restored["pre_restore_backup"]).suffix)
        self.assertEqual("ok", portability.preview_import(restored["pre_restore_backup"])["database_integrity"])
        self.assertFalse((self.home / "uploads" / "stale-after-export.png").exists())
        restored_settings = json.loads((self.home / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual("旧设置环境", restored_settings["meal_environment"])
        with connect() as conn:
            dates = [row[0] for row in conn.execute("SELECT record_date FROM daily_records ORDER BY record_date")]
            inventory_count = conn.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0]
        self.assertEqual(["2026-07-09"], dates)
        self.assertEqual(1, inventory_count)

    def test_closed_loop_cli_surfaces_plan_inventory_setup_and_export(self):
        self._complete_standard_profile()
        plan = self._publish_plan("2026-07-09")
        adaptive.create_inventory_item("豆腐", "半盒")

        def run(*arguments: str) -> dict:
            completed = subprocess.run(
                [sys.executable, "-m", "mealcircuit.agent_cli", *arguments],
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=os.environ.copy(),
            )
            return json.loads(completed.stdout)

        self.assertEqual("completed", run("setup", "status")["status"])
        self.assertEqual(plan["plan_version_id"], run("plan", plan["plan_date"])["plan_version_id"])
        self.assertEqual("豆腐", run("inventory", "list")[0]["name"])
        bundle = self.home / "exports" / "cli.zip"
        self.assertTrue(Path(run("export-bundle", "--output", str(bundle))["path"]).samefile(bundle))
        self.assertEqual("ok", run("import-bundle", str(bundle))["database_integrity"])
        experiment_file = self.home / "experiment.json"
        experiment_file.write_text(json.dumps({
            "action": "晚餐主动时间降到15分钟", "success_signal": "连续三次完成",
        }, ensure_ascii=False), encoding="utf-8")
        experiment = run(
            "learning", "experiment-propose", "dinner_active_minutes", "--file", str(experiment_file)
        )
        active = run(
            "learning", "experiment-start", experiment["id"], "2026-07-10", "--days", "5"
        )
        self.assertEqual("active", active["status"])
        result_file = self.home / "experiment-result.json"
        result_file.write_text(json.dumps({"summary": "执行完成"}, ensure_ascii=False), encoding="utf-8")
        finished = run(
            "learning", "experiment-finish", experiment["id"], "--file", str(result_file)
        )
        self.assertEqual("completed", finished["status"])

    def test_completed_task_evidence_enters_daily_context_and_requeues_on_correction(self):
        self._complete_standard_profile()
        task = service.create_photo_task(
            __import__("io").BytesIO(b"\x89PNG\r\n\x1a\n" + b"meal"), "实际午餐"
        )
        adaptive.link_task_evidence(task["id"], "2026-07-10", "consumed", "lunch")
        service.complete_task(task["id"], {
            "summary": "可见一份午餐",
            "candidates": [{
                "name": "米饭和鸡肉", "portion_range": "一份", "nutrition": _nutrition(), "confidence": 0.7,
            }],
            "unknowns": ["油量不可见"],
            "advice": ["结合全天记录判断"],
        })
        review = service.get_daily_review("2026-07-10")
        self.assertEqual("pending", review["status"])
        context = service.daily_review_context("2026-07-10")
        self.assertEqual(task["id"], context["meal_evidence"][0]["task_id"])
        self.assertEqual(task["id"], context["source_manifest"]["meal_evidence"][0]["task_id"])
        review_result = _review_result("2026-07-10")
        review_result["tomorrow_menu"]["protein_target_g"] = load_resolved_settings()["protein_target_g"]
        review_result["tomorrow_menu"]["environment"] = load_resolved_settings()["meal_environment"]
        service.complete_daily_review("2026-07-10", review_result)
        service.add_correction(task["id"], {"text": "鸡肉实际约一掌。"})
        reopened = service.get_daily_review("2026-07-10")
        self.assertEqual("pending", reopened["status"])
        self.assertEqual(1, len(reopened["history"]))

    def test_feedback_creates_friction_candidate_then_confirmed_rule(self):
        self._complete_standard_profile()
        plans = [self._publish_plan(day) for day in ("2026-07-07", "2026-07-08", "2026-07-09")]
        dinners = [next(item for item in plan["menu"]["meals"] if item["name"] == "晚餐") for plan in plans]
        adaptive.save_plan_feedback(
            plans[0]["plan_date"], dinners[0]["plan_item_id"], "modified", reason_codes=["not_enough_time"]
        )
        adaptive.save_plan_feedback(
            plans[1]["plan_date"], dinners[1]["plan_item_id"], "skipped", reason_codes=["not_enough_time"]
        )
        adaptive.save_plan_feedback(
            plans[2]["plan_date"], dinners[2]["plan_item_id"], "followed",
            outcome={"result": "appropriate", "would_repeat": True},
        )
        candidates = [item for item in adaptive.list_candidates("pending") if item["kind"] == "friction"]
        self.assertEqual(1, len(candidates))
        self.assertEqual("emerging", candidates[0]["confidence"])
        self.assertEqual(2, candidates[0]["evidence_summary_json"]["support_count"])
        self.assertEqual(1, candidates[0]["evidence_summary_json"]["counterexample_count"])
        active = adaptive.active_adaptations("2026-07-10")
        self.assertEqual([], active["transient"])
        self.assertEqual(1, len(active["candidate_suggestions"]))
        accepted = adaptive.decide_candidate(candidates[0]["id"], "accept", statement="晚餐主动时间最多 15 分钟。")
        self.assertIsNotNone(accepted["rule_id"])
        rules = adaptive.list_rules()
        self.assertEqual("晚餐主动时间最多 15 分钟。", rules[0]["statement"])
        self.assertEqual([], adaptive.active_adaptations("2026-07-10")["transient"])

        service.add_daily_record("2026-07-10", "验证确认规则进入下一份计划。")
        constrained = _review_result("2026-07-10", "快速番茄鸡肉")
        settings = load_resolved_settings()
        constrained["tomorrow_menu"]["protein_target_g"] = settings["protein_target_g"]
        constrained["tomorrow_menu"]["environment"] = settings["meal_environment"]
        dinner = next(item for item in constrained["tomorrow_menu"]["meals"] if item["name"] == "晚餐")
        dinner["execution"] = {"active_minutes": 20, "total_minutes": 25, "cookware": ["pan"]}
        with self.assertRaisesRegex(ValidationError, "违反确认规则"):
            service.complete_daily_review("2026-07-10", constrained)
        dinner["execution"]["active_minutes"] = 15
        completed = service.complete_daily_review("2026-07-10", constrained)
        self.assertEqual(2, completed["result_json"]["result_schema_version"])
        self.assertIn(rules[0]["id"], completed["result_json"]["decision_trace"]["confirmed_rule_ids"])
        self.assertEqual(1, completed["source_manifest_json"]["rules"][0]["version"])
        next_plan = adaptive.get_plan_for_date("2026-07-11")
        self.assertTrue(next_plan["plan_version_id"])
        with connect() as conn:
            item_count = conn.execute(
                "SELECT COUNT(*) FROM plan_items WHERE plan_version_id=?", (next_plan["plan_version_id"],)
            ).fetchone()[0]
        self.assertEqual(3, item_count)

    def test_repeated_success_creates_strategy_candidate(self):
        self._complete_standard_profile()
        for index, day in enumerate(("2026-07-05", "2026-07-06", "2026-07-07", "2026-07-08")):
            plan = self._publish_plan(day, "固定番茄鸡肉")
            dinner = next(item for item in plan["menu"]["meals"] if item["name"] == "晚餐")
            if index < 3:
                adaptive.save_plan_feedback(
                    plan["plan_date"], dinner["plan_item_id"], "followed",
                    outcome={"result": "appropriate", "would_repeat": True},
                )
            else:
                adaptive.save_plan_feedback(
                    plan["plan_date"], dinner["plan_item_id"], "modified", reason_codes=["schedule_change"]
                )
        strategies = [item for item in adaptive.list_candidates("pending") if item["kind"] == "strategy"]
        self.assertEqual(1, len(strategies))
        self.assertEqual(3, strategies[0]["evidence_summary_json"]["support_count"])
        self.assertEqual(4, strategies[0]["evidence_summary_json"]["opportunity_count"])

    def test_inventory_questions_experiment_and_calibration_are_versioned(self):
        self._complete_standard_profile()
        item = adaptive.create_inventory_item("北豆腐", "半盒", expires_on="2026-07-12")
        used = adaptive.update_inventory_status(item["id"], "used", item["version"], "0")
        self.assertEqual(2, used["version"])
        with self.assertRaisesRegex(ValidationError, "已变化"):
            adaptive.update_inventory_status(item["id"], "available", item["version"])

        questions = adaptive.schedule_questions("2026-07-10")
        self.assertEqual(2, len(questions))
        answer_value = questions[0]["question_schema_json"]["options"][0]
        answered = adaptive.answer_question(questions[0]["id"], answer_value, questions[0]["version"])
        self.assertEqual("answered", answered["status"])

        experiment = adaptive.propose_experiment("dinner_active_minutes", {
            "action": "晚餐主动时间降到15分钟", "success_signal": "两次按计划执行",
        })
        active = adaptive.activate_experiment(experiment["id"], "2026-07-10", 5)
        self.assertEqual("2026-07-14", active["ends_on"])
        self.assertEqual(2, active["version"])
        service.add_daily_record("2026-07-10", "验证实验版本进入 source manifest。")
        context = service.daily_review_context("2026-07-10")
        self.assertEqual(2, context["source_manifest"]["experiment"]["version"])
        finished = adaptive.finish_experiment(active["id"], {"summary": "执行完成"})
        self.assertEqual("completed", finished["status"])
        self.assertEqual(3, finished["version"])
        self.assertFalse(adaptive.calibration_snapshot("2026-07-10")["eligible_for_strategy_review"])

    def test_rescue_session_is_bound_to_published_plan(self):
        plan = self._publish_plan("2026-07-09")
        dinner = next(item for item in plan["menu"]["meals"] if item["name"] == "晚餐")
        rescue = adaptive.create_rescue_session(
            plan["plan_date"], dinner["plan_item_id"], "ingredient_missing", "鸡肉没有解冻"
        )
        completed = adaptive.complete_rescue_session(rescue["id"], {
            "reason": "改用可直接加热的豆腐。", "steps": ["豆腐沥水", "按原调味完成"],
            "replacement_foods": ["豆腐"], "portion_change": "保持原份量", "safety_notes": [],
        })
        self.assertEqual("completed", completed["status"])
        self.assertEqual(2, completed["result_json"]["result_schema_version"])
        self.assertTrue(completed["context_hash"])
        self.assertTrue(completed["source_manifest_json"]["doctrine"]["sha256"])
        updated_plan = adaptive.get_plan_for_date(plan["plan_date"])
        feedback = updated_plan["feedback"][dinner["plan_item_id"]]
        self.assertEqual("modified", feedback["status"])
        self.assertEqual("rescue", adaptive.plan_feedback_history(feedback["id"])[0]["actor_source"])

    def test_restricted_profile_hides_old_prescriptive_plan_but_keeps_factual_feedback_history(self):
        plan = self._publish_plan("2026-07-10")
        plan_item = plan["menu"]["meals"][0]
        session = self._fill_session(safety_overrides={"therapeutic_diet": True})
        current = personalization.complete_onboarding(
            session["id"], session["version"], {"accept_profile": True}
        )
        self.assertEqual("clinician_guided", current["safety"]["mode"])
        self.assertIsNone(adaptive.get_plan_for_date(plan["plan_date"]))
        questions = adaptive.schedule_questions(plan["plan_date"])
        self.assertEqual(["professional_guidance_review"], [item["question_key"] for item in questions])
        self.assertNotIn("早餐", json.dumps(questions, ensure_ascii=False))
        feedback = adaptive.save_plan_feedback(
            plan["plan_date"], plan_item["plan_item_id"], "followed", actor_source="test"
        )
        self.assertEqual("followed", feedback["status"])
        with self.assertRaisesRegex(ValidationError, "缺少仍有效的专业指导"):
            adaptive.create_rescue_session(
                plan["plan_date"], plan_item["plan_item_id"], "not_enough_time"
            )
        with self.assertRaisesRegex(ValidationError, "不能生成菜单、适应分析或救场建议"):
            adaptive.propose_experiment("meal_timing", {
                "action": "改变餐次时间", "success_signal": "观察完成",
            })


if __name__ == "__main__":
    unittest.main()
