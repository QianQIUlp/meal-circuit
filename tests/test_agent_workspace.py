from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

from mealcircuit import agent_workspace, personalization, server, service
from mealcircuit.configuration import load_resolved_settings
from mealcircuit.db import connect, init_db
from tests.test_adaptive import SETTINGS, _review_result


def _portion(item: str, low: int, high: int, measure: str) -> dict:
    return {
        "item": item,
        "gram_range": [low, high],
        "measurement_basis": "cooked",
        "household_measure": measure,
        "nutrition_estimate": {"protein_g": [5, 15]},
        "confidence": "medium",
        "increase_if": "餐后仍明显饥饿时先增加半拳蔬菜或半拳主食",
        "decrease_if": "食欲低时先减主食，不减主蛋白",
    }


def _v3_result(review_date: str) -> dict:
    result = _review_result(review_date)
    result.update({
        "case_summary": "兼顾训练恢复、减脂和真实饱腹感。",
        "planning_rationale": ["午餐保证训练前能量", "晚餐用更大蔬菜体积提高饱腹感"],
        "evidence_summary": ["用户已确认身体重组目标", "今天有真实饮食记录"],
        "possible_resistance": ["晚餐步骤过多会降低执行率"],
        "adjustment_conditions": ["睡眠不足时不继续压低份量"],
        "day_nutrition": {
            "energy_kcal": None, "protein_g": [115, 145], "confidence": "medium",
            "method": "三餐范围相加；能量因食品数据不足保持未知", "unknowns": ["外食用油"],
        },
    })
    protein_ranges = ([30, 35], [45, 55], [40, 55])
    for index, meal in enumerate(result["tomorrow_menu"]["meals"]):
        meal["protein_g"] = list(protein_ranges[index])
        meal.update({
            "purpose": ["低摩擦启动", "稳定下午精力", "训练恢复和晚间饱腹"][index],
            "why_today": "根据今天的目标、状态和餐次安排决定。",
            "whole_day_role": "与其他餐共同覆盖已确认目标，不机械追加。",
            "portion_contracts": [_portion(meal["foods"][0], 100 + index * 20, 140 + index * 20, "约一掌")],
            "adjustment_logic": {
                "if_hungry": "先加蔬菜，再按训练情况加主食",
                "if_low_appetite": "减主食但保留蛋白",
                "if_gut_unwell": "降油、降辣并改软烂",
            },
        })
    return result


class ScriptedProvider:
    def __init__(
        self,
        review_date: str,
        *,
        plan: dict | None = None,
        questions: list[dict] | None = None,
        soft_assumptions: list[dict] | None = None,
        review_claims: list[dict] | None = None,
    ):
        self.review_date = review_date
        self.plan = plan or _v3_result(review_date)
        self.questions = questions or []
        self.soft_assumptions = soft_assumptions or []
        self.review_claims = review_claims or []
        self.kinds: list[str] = []
        self.requests = []

    def generate(self, request):
        self.kinds.append(request.kind)
        self.requests.append(request)
        if request.kind == "case_formulation":
            return {
                "current_state": ["今天已经提供真实饮食记录"],
                "explicit_goals": ["身体重组"],
                "underlying_needs": [{"need": "计划必须吃得饱且能执行", "evidence": "用户记录"}],
                "tensions": ["控制能量与训练恢复需要平衡"],
                "decisive_constraints": ["晚餐时间有限"],
                "historical_patterns": [],
                "soft_assumptions": deepcopy(self.soft_assumptions),
                "uncertainties": [],
                "intake_classifications": [],
                "clarification_questions": deepcopy(self.questions),
                "planning_priorities": ["可执行", "训练恢复", "饱腹"],
            }
        if request.kind in {"daily_plan_v3", "daily_plan_v3_revision"}:
            return deepcopy(self.plan)
        if request.kind == "plan_review":
            return {
                "approved": True,
                "human_fit_summary": "份量、执行性和当天目标相互一致。",
                "issues": [],
                "claim_candidates": deepcopy(self.review_claims),
            }
        if request.kind == "targeted_plan_revision":
            updated = deepcopy(self.plan)
            for meal in updated["tomorrow_menu"]["meals"]:
                meal["purpose"] = "模型尝试改动所有餐次"
            updated["tomorrow_menu"]["meals"][1]["mode"] = "eat_out"
            updated["tomorrow_menu"]["meals"][1].pop("recipe_card", None)
            updated["tomorrow_menu"]["meals"][1]["eat_out_guidance"] = {
                "protein_anchor": "一掌瘦肉或豆制品", "staple": "一拳主食",
                "vegetables": "至少两拳", "sauce_rule": "酱汁分开",
                "fallback": "便利店无糖奶加饭团和沙拉",
            }
            return {
                "affected_meals": ["午餐"], "global_balance_changed": True,
                "change_summary": ["午餐改外食"], "updated_result": updated,
                "claim_candidates": [],
            }
        raise AssertionError(request.kind)


class AgentWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.old = {key: os.environ.get(key) for key in ("MEALCIRCUIT_HOME", "MEALCIRCUIT_DB")}
        os.environ["MEALCIRCUIT_HOME"] = str(self.home)
        os.environ["MEALCIRCUIT_DB"] = str(self.home / "mealcircuit.db")
        (self.home / "settings.json").write_text(json.dumps(SETTINGS, ensure_ascii=False), encoding="utf-8")
        (self.home / "profile.md").write_text("# 测试档案\n", encoding="utf-8")
        init_db()
        self._complete_standard_profile()
        self.review_date = date.today().isoformat()
        service.add_daily_record(self.review_date, "今天完成力量训练，晚餐希望吃得更饱。")

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def _complete_standard_profile(self):
        session = personalization.start_onboarding()
        payloads = {
            "welcome": {"privacy_ack": True},
            "goals": {
                "primary_goal": "body_recomposition", "secondary_goals": ["performance"],
                "motivation": "降低脂肪，同时维持力量训练表现。",
                "success_metrics": ["weight_trend", "training_performance", "execution_rate"],
                "target_weight_kg": None,
            },
            "baseline": {"age_years": 30, "height_cm": 180, "weight_kg": 80, "physiological_input": "male", "activity_level": "moderate"},
            "safety": {
                "life_stage": "adult", "therapeutic_diet": False,
                "medication_affects_nutrition": False, "eating_disorder_risk": False,
                "rapid_unexplained_change": False, "severe_persistent_symptoms": False,
                "severe_allergy_management": False,
            },
            "training": {"types": ["strength"], "frequency_per_week": 4},
            "constraints": {
                "meal_environment": "工作日食堂，晚餐在家", "portion_method": "手掌与拳头份量法",
                "cooking_time_minutes": 20, "equipment": ["stovetop_pan", "rice_cooker"],
                "food_exclusions": [], "preferences": ["酸辣", "番茄"], "question_budget": 2,
            },
        }
        current = session
        for step, payload in payloads.items():
            current = personalization.save_onboarding_step(current["id"], step, payload, current["version"])
        personalization.complete_onboarding(
            current["id"], current["version"],
            {"accept_profile": True, "accept_strategy": True, "planning_mode": "portion_guided"},
        )

    def _provider(self) -> ScriptedProvider:
        result = _v3_result(self.review_date)
        settings = load_resolved_settings()
        result["tomorrow_menu"]["protein_target_g"] = settings["protein_target_g"]
        result["tomorrow_menu"]["environment"] = settings["meal_environment"]
        return ScriptedProvider(self.review_date, plan=result)

    def test_agent_context_is_layered_and_explainable(self):
        service.add_daily_record((date.today() - timedelta(days=3)).isoformat(), "与今天决策无关的旧记录")
        context = agent_workspace.build_agent_context(self.review_date)
        self.assertEqual("AgentContextV2", context["context_schema"])
        self.assertEqual({"person", "today", "longitudinal", "professional_basis", "decision_task"}, {
            key for key in context if key in {"person", "today", "longitudinal", "professional_basis", "decision_task"}
        })
        self.assertTrue(all(item["record_date"] == self.review_date for item in context["today"]["records"]))
        self.assertTrue(context["context_inspector"]["excluded"])
        self.assertFalse(context["professional_basis"]["runtime_network_access"])
        self.assertIn(
            "training-fuel-recovery",
            {item["id"] for item in context["professional_basis"]["principles"]},
        )
        self.assertTrue(all(
            item["source"].get("verified_on") == "2026-07-14"
            for item in context["professional_basis"]["principles"]
        ))

    def test_three_stage_run_creates_replaceable_draft_until_accept(self):
        provider = self._provider()
        draft = agent_workspace.run_agent_draft(self.review_date, provider)
        self.assertEqual("ready_draft", draft["status"])
        self.assertEqual(["case_formulation", "daily_plan_v3", "plan_review"], provider.kinds)
        self.assertEqual("AgentContextV2", provider.requests[0].context["context_schema"])
        self.assertEqual("DailyPlanV3Input", provider.requests[1].context["context_schema"])
        self.assertEqual("PlanReviewV1Input", provider.requests[2].context["context_schema"])
        self.assertNotIn("candidate_plan", provider.requests[0].context)
        self.assertNotIn("today", provider.requests[2].context)
        self.assertEqual("pending", service.get_daily_review(self.review_date)["status"])
        plan_date = (date.fromisoformat(self.review_date) + timedelta(days=1)).isoformat()
        self.assertIsNone(__import__("mealcircuit.adaptive", fromlist=["get_plan_for_date"]).get_plan_for_date(plan_date))
        draft_workspace_html = server.render_today_workspace(self.review_date)
        self.assertIn("今天的核心建议", draft_workspace_html)
        self.assertIn("维持三餐并记录真实执行阻力", draft_workspace_html)

        accepted = agent_workspace.accept_draft(self.review_date)
        self.assertEqual("completed", accepted["status"])
        manifest = accepted["source_manifest_json"]
        self.assertEqual(2, manifest["agent_context_version"])
        self.assertEqual("professional-basis-2026-07-v1", manifest["professional_knowledge"]["version"])
        self.assertEqual(draft["run_id"], manifest["agent_run_id"])
        self.assertIn("user_model_claims", manifest)
        self.assertIsNotNone(__import__("mealcircuit.adaptive", fromlist=["get_plan_for_date"]).get_plan_for_date(plan_date))
        self.assertEqual("accepted", agent_workspace.get_draft(self.review_date)["status"])
        workspace_html = server.render_today_workspace(self.review_date)
        self.assertIn("今天的核心建议", workspace_html)
        self.assertIn("维持三餐并记录真实执行阻力", workspace_html)
        self.assertIn(f'/reviews/{self.review_date}', workspace_html)
        self.assertIn("明天的安排已经准备好", workspace_html)
        self.assertIn(f'/plans/{plan_date}', workspace_html)
        self.assertNotIn("未配置模型；可以查看和导出", workspace_html)
        self.assertNotIn("AgentContextV2", workspace_html)
        self.assertNotIn("Three-stage planning", workspace_html)
        plan_html = server.render_plan_page(plan_date)
        self.assertIn("为什么这样安排", plan_html)
        self.assertIn("这次参考了什么", plan_html)
        self.assertNotIn("source_manifest", plan_html)

    def test_decision_changing_questions_stop_before_planning(self):
        provider = ScriptedProvider(self.review_date, plan=self._provider().plan, questions=[{
            "key": "training_time", "prompt": "明天几点训练？", "reason": "会改变训练前后餐次",
            "decision_impact": "碳水和蛋白分配", "answer_schema": {"kind": "text"},
        }])
        draft = agent_workspace.run_agent_draft(self.review_date, provider)
        self.assertEqual("needs_clarification", draft["status"])
        self.assertEqual(["case_formulation"], provider.kinds)
        state = agent_workspace.get_workspace_state(self.review_date)
        self.assertEqual(1, len([item for item in state["questions"] if item["status"] == "pending"]))

    def test_targeted_revision_preserves_unaffected_meals(self):
        provider = self._provider()
        before = agent_workspace.run_agent_draft(self.review_date, provider)["result_json"]
        revision_provider = ScriptedProvider(self.review_date, plan=before)
        after = agent_workspace.revise_draft(self.review_date, "午饭改外食，其他餐不变", revision_provider)["result_json"]
        old = {item["name"]: item for item in before["tomorrow_menu"]["meals"]}
        new = {item["name"]: item for item in after["tomorrow_menu"]["meals"]}
        self.assertEqual(old["早餐"], new["早餐"])
        self.assertEqual(old["晚餐"], new["晚餐"])
        self.assertEqual("eat_out", new["午餐"]["mode"])
        self.assertEqual(2, agent_workspace.get_draft(self.review_date)["version"])

    def test_claim_requires_explicit_or_two_independent_signals_and_syncs_projection(self):
        first = agent_workspace.upsert_claim(
            claim_type="friction_hypothesis", statement="晚餐步骤过多时更难执行", scope={"meal": "晚餐"},
            effect={"complexity": "减少持续看火"}, evidence_type="feedback", evidence_id="one",
        )
        self.assertEqual("pending_confirmation", first["status"])
        second = agent_workspace.upsert_claim(
            claim_type="friction_hypothesis", statement="晚餐步骤过多时更难执行", scope={"meal": "晚餐"},
            effect={"complexity": "减少持续看火"}, evidence_type="feedback", evidence_id="two",
        )
        self.assertEqual("active", second["status"])
        self.assertEqual(
            personalization.active_personalization()["strategy"]["id"],
            second["scope_json"]["binding"]["strategy_version_id"],
        )
        self.assertEqual(
            "standard", second["scope_json"]["binding"]["safety_mode"],
        )
        with connect() as conn:
            projection = json.loads(conn.execute(
                "SELECT content FROM config_documents WHERE kind='agent_user_model'"
            ).fetchone()[0])
        self.assertEqual("active", projection["claims"][0]["status"])
        self.assertNotIn("food_exclusion", projection["claims"][0]["effect"])

    def test_model_hypothesis_is_not_real_evidence_but_explicit_intake_can_activate(self):
        hypothesis = {
            "claim_type": "soft_need_hypothesis",
            "statement": "晚餐需要更强的饱腹感",
            "scope": {"meal": "晚餐"},
            "planning_effect": {"portion": "增加蔬菜体积"},
            "evidence_ids": [],
            "evidence_summary": "模型根据个案提出，尚无直接用户证据",
            "risk_level": "low",
            "explicit_user_statement": False,
            "valid_until": None,
        }
        provider = ScriptedProvider(
            self.review_date,
            plan=self._provider().plan,
            soft_assumptions=[hypothesis],
            review_claims=[hypothesis],
        )
        agent_workspace.run_agent_draft(self.review_date, provider)
        claims = [item for item in agent_workspace.list_claims() if item["statement"] == hypothesis["statement"]]
        self.assertEqual(1, len(claims))
        self.assertEqual("pending_confirmation", claims[0]["status"])
        self.assertEqual(1, claims[0]["support_count"])

        intake = agent_workspace.record_intake(self.review_date, "我明确觉得晚餐菜量太少，希望以后更有饱腹感。")
        explicit = {**hypothesis, "evidence_ids": [intake["id"]], "explicit_user_statement": True}
        agent_workspace.run_agent_draft(
            self.review_date,
            ScriptedProvider(self.review_date, plan=self._provider().plan, soft_assumptions=[explicit]),
            force=True,
        )
        updated = [item for item in agent_workspace.list_claims() if item["statement"] == hypothesis["statement"]][0]
        self.assertEqual("active", updated["status"])

    def test_high_impact_claim_never_auto_activates_or_changes_planning(self):
        claim = agent_workspace.upsert_claim(
            claim_type="confirmed_fact", statement="用户处于孕期并应采用治疗性热量目标",
            scope={"person": True}, effect={"portion": "限制热量"},
            evidence_type="agent_workspace_event", evidence_id="unsafe", explicit=True,
        )
        self.assertEqual("high", claim["risk_level"])
        self.assertEqual("pending_confirmation", claim["status"])
        self.assertEqual({}, claim["effect_json"])

    def test_claim_from_previous_strategy_remains_visible_but_stops_affecting_context(self):
        claim = agent_workspace.upsert_claim(
            claim_type="stable_preference", statement="旧策略下偏好很小的晚餐",
            scope={"meal": "晚餐"}, effect={"portion": "旧策略份量"},
            evidence_type="user_correction", evidence_id="old-strategy", explicit=True,
        )
        self.assertIn(claim["id"], {
            item["id"] for item in agent_workspace.build_agent_context(self.review_date)["person"]["active_user_model"]
        })
        self._complete_standard_profile()
        context_claims = agent_workspace.build_agent_context(self.review_date)["person"]["active_user_model"]
        self.assertNotIn(claim["id"], {item["id"] for item in context_claims})
        self.assertIn(claim["id"], {item["id"] for item in agent_workspace.list_claims()})

    def test_context_change_during_planning_interrupts_without_publishing(self):
        parent = self

        class MutatingProvider(ScriptedProvider):
            def generate(self, request):
                value = super().generate(request)
                if request.kind == "daily_plan_v3":
                    service.add_daily_record(parent.review_date, "规划期间新增：明天改为晚上训练。")
                return value

        draft = agent_workspace.run_agent_draft(
            self.review_date, MutatingProvider(self.review_date, plan=self._provider().plan)
        )
        self.assertEqual("stale", draft["status"])
        self.assertEqual("pending", service.get_daily_review(self.review_date)["status"])
        with connect() as conn:
            run = conn.execute(
                "SELECT status FROM agent_planning_runs WHERE id=?", (draft["run_id"],)
            ).fetchone()
        self.assertEqual("interrupted", run["status"])

    def test_new_evidence_marks_unpublished_draft_stale_without_touching_formal_history(self):
        draft = agent_workspace.run_agent_draft(self.review_date, self._provider())
        self.assertEqual("ready_draft", draft["status"])
        service.add_daily_record(self.review_date, "补充：明天改成早上训练。")
        stale = agent_workspace.get_draft(self.review_date)
        self.assertEqual("stale", stale["status"])
        self.assertEqual([], service.get_daily_review(self.review_date)["history"])
