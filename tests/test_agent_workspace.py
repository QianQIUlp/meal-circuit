from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from mealcircuit import adaptive, agent_intelligence, agent_workspace, personalization, server, service
from mealcircuit.configuration import load_resolved_settings
from mealcircuit.db import connect, init_db
from mealcircuit.validation import ValidationError
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
        "problems_to_solve": ["可执行", "训练恢复", "饱腹"],
        "selected_strategy": "balanced",
        "strategy_tradeoffs": ["不追求最低成本，保留训练恢复所需的便利性"],
        "predictions": {
            "satiety": "三餐份量和蔬菜体积足以覆盖日常饥饿",
            "recovery": "训练前后都有蛋白和主食",
            "cost": "使用常见食材，成本可控",
            "time": "自炊餐不超过个人时间上限",
            "execution_risks": ["晚餐如果太晚开始，切配可能成为阻力"],
            "adjustment_triggers": ["训练取消时减少半拳主食"],
        },
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
        meal["mode"] = ("quick_assembly", "eat_out", "home_cook")[index]
        if index == 1:
            meal["eat_out_guidance"] = {
                "protein_anchor": "一掌瘦肉或豆制品", "staple": "一拳主食",
                "vegetables": "至少两拳", "sauce_rule": "酱汁分开",
                "fallback": "便利店无糖奶加饭团和沙拉",
            }
        meal.update({
            "purpose": ["低摩擦启动", "稳定下午精力", "训练恢复和晚间饱腹"][index],
            "why_today": "根据今天的目标、状态和餐次安排决定。",
            "whole_day_role": "与其他餐共同覆盖已确认目标，不机械追加。",
            "predicted_satiety": "预计餐后舒适；饥饿时有明确加量顺序。",
            "predicted_cost": "使用日常可购买食材。",
            "execution_risks": ["开始太晚会压缩烹饪时间"] if index else [],
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
        if request.kind == "intent_learning":
            bundle = request.context["fact_bundle"]
            return {
                "source_dispositions": [{
                    "source_id": item["source_id"], "disposition": "today_fact",
                    "summary": item["text"],
                } for item in bundle.get("natural_language_sources") or []],
                "signal_dispositions": [{
                    "signal_id": item["signal_id"],
                    "disposition": "active_soft_understanding" if item.get("lifetime") == "durable" else "pending_confirmation",
                    "explanation": "已按风险和适用时间处理。",
                } for item in bundle.get("detected_intent_signals") or []],
                "learning_summary": ["已逐条处理本次用户输入。"],
            }
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
        if request.kind == "strategy_comparison":
            required = [
                "可执行", "训练恢复", "饱腹",
                *request.context.get("required_goal_dimensions", []),
                *request.context.get("required_non_negotiables", []),
            ]
            return {
                "options": [
                    {
                        "id": "balanced", "label": "平衡现实执行", "summary": "兼顾恢复、饱腹和时间。",
                        "scores": {key: 4 for key in (
                            "safety", "goal_coverage", "budget", "time", "satiety", "taste",
                            "rotation", "waste", "execution_probability",
                        )},
                        "tradeoffs": ["不是最低成本"], "solves_priorities": required,
                    },
                    {
                        "id": "cheapest", "label": "最低成本", "summary": "优先成本。",
                        "scores": {key: (5 if key == "budget" else 3) for key in (
                            "safety", "goal_coverage", "budget", "time", "satiety", "taste",
                            "rotation", "waste", "execution_probability",
                        )},
                        "tradeoffs": ["口味和便利性较弱"], "solves_priorities": required,
                    },
                ],
                "selected_id": "balanced", "selection_reason": "更适合长期执行。",
                "rejected_reasons": [{"id": "cheapest", "reason": "会牺牲便利性。"}],
            }
        if request.kind in {"daily_plan_v3", "daily_plan_v3_revision"}:
            plan = deepcopy(self.plan)
            source_id = request.context["today"]["records"][0]["id"]
            plan["advice_evidence"] = [{
                "advice": advice, "basis_kind": "user_context", "source_ids": [source_id],
            } for advice in plan["core_advice"]]
            return plan
        if request.kind == "plan_review":
            candidate = request.context["candidate_plan"]
            return {
                "approved": True,
                "human_fit_summary": "份量、执行性和当天目标相互一致。",
                "problem_coverage": [{
                    "problem": item, "addressed": True, "evidence": "计划有对应餐次与调整条件。",
                } for item in request.context["case_formulation"]["planning_priorities"]],
                "dimension_coverage": [{
                    "dimension": item, "addressed": True, "evidence": "所选策略和餐次安排有对应体现。",
                } for item in request.context.get("required_dimensions", [])],
                "evidence_checks": [{
                    "claim": advice, "supported": True,
                    "source_or_boundary": "对应 advice_evidence 中的当天用户记录。",
                } for advice in candidate["core_advice"]],
                "issues": [],
                "claim_candidates": deepcopy(self.review_claims),
            }
        if request.kind == "targeted_plan_revision":
            updated = deepcopy(request.context["current_draft"])
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
        self.assertEqual("AgentContextV3", context["context_schema"])
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
            item["source"].get("verified_on") == "2026-07-15"
            for item in context["professional_basis"]["principles"]
        ))

    def test_three_stage_run_creates_replaceable_draft_until_accept(self):
        provider = self._provider()
        draft = agent_workspace.run_agent_draft(self.review_date, provider)
        self.assertEqual("ready_draft", draft["status"])
        self.assertEqual(
            ["intent_learning", "case_formulation", "strategy_comparison", "daily_plan_v3", "plan_review"],
            provider.kinds,
        )
        self.assertEqual("IntentLearningV1Input", provider.requests[0].context["context_schema"])
        self.assertEqual("CaseFormulationV1Input", provider.requests[1].context["context_schema"])
        self.assertEqual("StrategyComparisonV1Input", provider.requests[2].context["context_schema"])
        self.assertEqual("DailyPlanV3Input", provider.requests[3].context["context_schema"])
        self.assertEqual("PlanReviewV1Input", provider.requests[4].context["context_schema"])
        self.assertNotIn("candidate_plan", provider.requests[1].context)
        self.assertNotIn("today", provider.requests[4].context)
        self.assertIn("evidence_pack", provider.requests[4].context)
        self.assertTrue(any(
            item["kind"] == "user_statement" and "今天完成力量训练" in item["value"]
            for item in provider.requests[4].context["evidence_pack"]["user_facts"]
        ))
        self.assertEqual("pending", service.get_daily_review(self.review_date)["status"])
        plan_date = (date.fromisoformat(self.review_date) + timedelta(days=1)).isoformat()
        self.assertIsNone(__import__("mealcircuit.adaptive", fromlist=["get_plan_for_date"]).get_plan_for_date(plan_date))
        draft_workspace_html = server.render_today_workspace(self.review_date)
        self.assertIn("今天的核心建议", draft_workspace_html)
        self.assertIn("维持三餐并记录真实执行阻力", draft_workspace_html)

        accepted = agent_workspace.accept_draft(self.review_date)
        self.assertEqual("completed", accepted["status"])
        manifest = accepted["source_manifest_json"]
        self.assertEqual(3, manifest["agent_context_version"])
        self.assertEqual("professional-basis-2026-07-v2", manifest["professional_knowledge"]["version"])
        self.assertEqual(draft["run_id"], manifest["agent_run_id"])
        self.assertEqual(7, len(manifest["agent_stage_receipts"]))
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
        self.assertEqual(["intent_learning", "case_formulation"], provider.kinds)
        state = agent_workspace.get_workspace_state(self.review_date)
        self.assertEqual(1, len([item for item in state["questions"] if item["status"] == "pending"]))

    def test_irrelevant_model_question_does_not_interrupt_planning(self):
        provider = ScriptedProvider(self.review_date, plan=self._provider().plan, questions=[{
            "key": "plate_color", "prompt": "你喜欢什么颜色的盘子？",
            "reason": "让描述更有趣", "decision_impact": "只改变文字风格",
            "answer_schema": {"kind": "text"},
        }])
        draft = agent_workspace.run_agent_draft(self.review_date, provider)
        self.assertEqual("ready_draft", draft["status"])
        self.assertEqual([], agent_workspace.get_workspace_state(self.review_date)["questions"])

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
        self.assertEqual(["targeted_plan_revision", "plan_review"], revision_provider.kinds)
        self.assertEqual(2, agent_workspace.get_draft(self.review_date)["version"])
        agent_workspace.assert_agent_run_publishable(
            agent_workspace.get_draft(self.review_date)["run_id"], self.review_date
        )

    def test_targeted_revision_keeps_original_when_independent_review_rejects_twice(self):
        before = agent_workspace.run_agent_draft(self.review_date, self._provider())["result_json"]

        class RejectingReviewProvider(ScriptedProvider):
            def generate(self, request):
                if request.kind == "plan_review":
                    self.kinds.append(request.kind)
                    self.requests.append(request)
                    return {
                        "approved": False,
                        "human_fit_summary": "午餐修改后仍没有覆盖预算约束。",
                        "problem_coverage": [{
                            "problem": item, "addressed": True, "evidence": "其余问题已有对应安排。",
                        } for item in request.context["case_formulation"]["planning_priorities"]],
                        "dimension_coverage": [{
                            "dimension": item, "addressed": True, "evidence": "其余维度已有对应安排。",
                        } for item in request.context.get("required_dimensions", [])],
                        "evidence_checks": [{
                            "claim": advice, "supported": True,
                            "source_or_boundary": "对应草案已绑定的当天记录。",
                        } for advice in request.context["candidate_plan"]["core_advice"]],
                        "issues": [{
                            "severity": "repair", "dimension": "budget",
                            "description": "外食选择仍缺少预算边界。",
                            "affected_meals": ["午餐"],
                            "suggested_change": "增加可执行的价格上限和备用选择。",
                            "user_harm": "可能再次给出用户长期负担不起、不会执行的选择。",
                        }],
                        "claim_candidates": [],
                    }
                return super().generate(request)

        provider = RejectingReviewProvider(self.review_date, plan=before)
        with self.assertRaisesRegex(ValidationError, "原草案保持不变"):
            agent_workspace.revise_draft(self.review_date, "午饭改外食", provider)
        current = agent_workspace.get_draft(self.review_date)
        self.assertEqual("stale", current["status"])
        self.assertEqual(2, current["version"])
        self.assertEqual(before, current["result_json"])
        self.assertEqual(
            ["targeted_plan_revision", "plan_review", "targeted_plan_revision", "plan_review"],
            provider.kinds,
        )

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
        harmless_revision = agent_workspace._current_claim_binding()
        harmless_revision["strategy_version_id"] = "strategy_new-version"
        self.assertTrue(agent_workspace._claim_scope_is_current(second["scope_json"], harmless_revision))
        changed_life_stage = dict(harmless_revision, life_stage="pregnant", safety_mode="clinician_guided")
        self.assertFalse(agent_workspace._claim_scope_is_current(second["scope_json"], changed_life_stage))
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
        draft = agent_workspace.run_agent_draft(self.review_date, provider)
        agent_workspace.accept_agent_run(draft["run_id"])
        claims = [item for item in agent_workspace.list_claims() if item["statement"] == hypothesis["statement"]]
        self.assertEqual(1, len(claims))
        self.assertEqual("pending_confirmation", claims[0]["status"])
        self.assertEqual(1, claims[0]["support_count"])

        intake = agent_workspace.record_intake(self.review_date, "我明确觉得晚餐菜量太少，希望以后更有饱腹感。")
        deterministic = next(
            item for item in agent_workspace.list_claims()
            if item.get("claim_dimension") == "satiety_pattern"
            and any(
                evidence["evidence_id"] == intake["record"]["id"]
                for evidence in item["evidence"]
            )
        )
        self.assertEqual("active", deterministic["status"])
        explicit = {**hypothesis, "evidence_ids": [intake["id"]], "explicit_user_statement": True}
        revised_draft = agent_workspace.run_agent_draft(
            self.review_date,
            ScriptedProvider(self.review_date, plan=self._provider().plan, soft_assumptions=[explicit]),
            force=True,
        )
        self.assertEqual("pending_confirmation", [
            item for item in agent_workspace.list_claims() if item["statement"] == hypothesis["statement"]
        ][0]["status"])
        agent_workspace.accept_agent_run(revised_draft["run_id"])
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

    def test_claim_from_previous_strategy_remains_visible_and_keeps_affecting_context(self):
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
        self.assertIn(claim["id"], {item["id"] for item in context_claims})
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

    def test_explicit_budget_need_is_learned_immediately_and_changes_planning(self):
        service.add_daily_record(
            self.review_date,
            "主要今天午饭没法执行是因为牛肉太贵了，吃不起，以后也算了。其余都正常。",
        )
        claims = [
            item for item in agent_workspace.list_claims()
            if item.get("claim_dimension") == "resource_constraint"
        ]
        self.assertEqual(1, len(claims))
        self.assertEqual("active", claims[0]["status"])
        self.assertEqual("牛肉", claims[0]["scope_json"]["item"])
        context = agent_workspace.build_agent_context(self.review_date)
        self.assertIn(claims[0]["id"], {
            item["id"] for item in context["person"]["active_user_model"]
        })

        bad = self._provider().plan
        bad["tomorrow_menu"]["meals"][2]["foods"] = ["牛肉", "米饭", "蔬菜"]
        with self.assertRaisesRegex(ValidationError, "不作为默认项"):
            agent_workspace.run_agent_draft(
                self.review_date, ScriptedProvider(self.review_date, plan=bad), force=True
            )
        self.assertEqual("pending", service.get_daily_review(self.review_date)["status"])

    def test_explicit_high_price_product_need_names_the_real_item(self):
        service.add_daily_record(self.review_date, "不需要高价酸奶，以后优先普通食物。")
        claim = next(
            item for item in agent_workspace.list_claims()
            if item.get("claim_dimension") == "resource_constraint"
        )
        self.assertEqual("active", claim["status"])
        self.assertEqual("酸奶", claim["scope_json"]["item"])
        self.assertIn("长期负担得起", claim["statement"])

    def test_execution_feedback_text_is_a_required_intent_source(self):
        previous_date = (date.fromisoformat(self.review_date) - timedelta(days=1)).isoformat()
        service.add_daily_record(previous_date, "昨天按计划吃饭。")
        previous_result = _review_result(previous_date)
        settings = load_resolved_settings()
        previous_result["tomorrow_menu"]["protein_target_g"] = settings["protein_target_g"]
        previous_result["tomorrow_menu"]["environment"] = settings["meal_environment"]
        service.complete_daily_review(previous_date, previous_result)
        plan = adaptive.get_plan_for_date(self.review_date)
        lunch = next(item for item in plan["menu"]["meals"] if item["name"] == "午餐")
        feedback = adaptive.save_plan_feedback(
            self.review_date, lunch["plan_item_id"], "modified",
            reason_codes=["too_expensive"],
            actual_text="午餐的牛肉太贵，以后不要默认安排。",
        )
        context = agent_workspace.build_agent_context(self.review_date)
        source = next(
            item for item in context["fact_bundle"]["natural_language_sources"]
            if item["source_id"] == feedback["id"]
        )
        self.assertEqual("execution_feedback", source["source_type"])
        self.assertEqual(self.review_date, source["observed_date"])
        self.assertEqual("午餐的牛肉太贵，以后不要默认安排。", source["text"])

    def test_temporary_food_signal_expires_before_next_day_but_tomorrow_signal_applies(self):
        today_record = service.add_daily_record(self.review_date, "今天不想吃鱼。")
        today_claim = next(
            item for item in agent_workspace.list_claims()
            if any(evidence["evidence_id"] == today_record["id"] for evidence in item["evidence"])
        )
        self.assertEqual(self.review_date, today_claim["valid_until"])
        next_context = agent_workspace.build_agent_context(self.review_date)
        self.assertNotIn(today_claim["id"], {
            item["id"] for item in next_context["person"]["active_user_model"]
        })

        tomorrow_record = service.add_daily_record(self.review_date, "明天不想吃鱼。")
        tomorrow_claim = next(
            item for item in agent_workspace.list_claims()
            if any(evidence["evidence_id"] == tomorrow_record["id"] for evidence in item["evidence"])
        )
        self.assertIn(tomorrow_claim["id"], {
            item["id"] for item in agent_workspace.build_agent_context(self.review_date)["person"]["active_user_model"]
        })

    def test_manual_stage_run_cannot_skip_or_publish_an_unreviewed_result(self):
        started = agent_workspace.begin_agent_run(self.review_date, force=True)
        self.assertEqual("intent_learning", started["stage"])
        with self.assertRaisesRegex(ValidationError, "不能跳过"):
            agent_workspace.submit_agent_stage(
                started["run_id"], "plan_design", self._provider().plan
            )
        with self.assertRaisesRegex(ValidationError, "全部必需阶段"):
            agent_workspace.finalize_agent_run(started["run_id"])
        with self.assertRaisesRegex(ValidationError, "Agent run ID"):
            service.submit_daily_review(self.review_date, self._provider().plan)

    def test_weak_model_cannot_hide_explicit_intent_with_empty_coverage(self):
        class WeakProvider(ScriptedProvider):
            def generate(self, request):
                if request.kind == "intent_learning":
                    self.kinds.append(request.kind)
                    self.requests.append(request)
                    return {"source_dispositions": [], "signal_dispositions": [], "learning_summary": []}
                return super().generate(request)

        with self.assertRaisesRegex(ValidationError, "项目数量不足|逐条处理"):
            agent_workspace.run_agent_draft(
                self.review_date, WeakProvider(self.review_date, plan=self._provider().plan), force=True
            )
        self.assertEqual("pending", service.get_daily_review(self.review_date)["status"])

    def test_weak_model_cannot_omit_a_goal_contract_dimension(self):
        class WeakStrategyProvider(ScriptedProvider):
            def generate(self, request):
                result = super().generate(request)
                if request.kind == "strategy_comparison":
                    missing = request.context["required_goal_dimensions"][0]
                    for option in result["options"]:
                        option["solves_priorities"] = [
                            item for item in option["solves_priorities"] if item != missing
                        ]
                return result

        with self.assertRaisesRegex(ValidationError, "必需目标维度"):
            agent_workspace.run_agent_draft(
                self.review_date, WeakStrategyProvider(self.review_date, plan=self._provider().plan),
                force=True,
            )
        self.assertEqual("pending", service.get_daily_review(self.review_date)["status"])

    def test_repair_retry_is_part_of_the_persisted_stage_input(self):
        class RepairOnceProvider(ScriptedProvider):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.plan_calls = 0

            def generate(self, request):
                if request.kind == "daily_plan_v3":
                    self.plan_calls += 1
                    if self.plan_calls == 1:
                        self.kinds.append(request.kind)
                        self.requests.append(request)
                        invalid = deepcopy(self.plan)
                        invalid["problems_to_solve"] = ["只覆盖一个无关问题"]
                        return invalid
                return super().generate(request)

        provider = RepairOnceProvider(self.review_date, plan=self._provider().plan)
        draft = agent_workspace.run_agent_draft(self.review_date, provider, force=True)
        self.assertEqual("ready_draft", draft["status"])
        receipt = agent_workspace.agent_run_status(draft["run_id"])["receipts"]
        plan_receipt = next(item for item in receipt if item["stage_key"] == "plan_design")
        self.assertIn("previous_rejection", plan_receipt["input_json"])
        self.assertEqual(2, provider.plan_calls)

    def test_weak_model_cannot_publish_advice_with_fake_evidence(self):
        class FakeEvidenceProvider(ScriptedProvider):
            def generate(self, request):
                result = super().generate(request)
                if request.kind in {"daily_plan_v3", "daily_plan_v3_revision"}:
                    result["advice_evidence"] = [{
                        "advice": advice,
                        "basis_kind": "user_context",
                        "source_ids": ["invented-source"],
                    } for advice in result["core_advice"]]
                return result

        with self.assertRaisesRegex(ValidationError, "不存在的证据"):
            agent_workspace.run_agent_draft(
                self.review_date,
                FakeEvidenceProvider(self.review_date, plan=self._provider().plan),
                force=True,
            )
        self.assertEqual("pending", service.get_daily_review(self.review_date)["status"])

    def test_independent_review_cannot_skip_core_advice_evidence_checks(self):
        class EmptyEvidenceReviewProvider(ScriptedProvider):
            def generate(self, request):
                result = super().generate(request)
                if request.kind == "plan_review":
                    result["evidence_checks"] = []
                return result

        with self.assertRaisesRegex(ValidationError, "逐条核对核心建议"):
            agent_workspace.run_agent_draft(
                self.review_date,
                EmptyEvidenceReviewProvider(self.review_date, plan=self._provider().plan),
                force=True,
            )
        self.assertEqual("pending", service.get_daily_review(self.review_date)["status"])

    def test_resource_constraint_can_be_refuted_by_new_user_evidence(self):
        service.add_daily_record(self.review_date, "牛肉太贵，以后算了。")
        service.add_daily_record(self.review_date, "现在牛肉价格可以接受，可以买。")
        claim = next(
            item for item in agent_workspace.list_claims()
            if item.get("claim_dimension") == "resource_constraint"
        )
        self.assertEqual("refuted", claim["status"])
        self.assertEqual(1, claim["counter_count"])

    def test_editing_user_text_supersedes_old_learning_without_erasing_trace(self):
        record = service.add_daily_record(self.review_date, "牛肉太贵，以后算了。")
        claim = next(
            item for item in agent_workspace.list_claims()
            if item.get("claim_dimension") == "resource_constraint"
        )
        self.assertEqual("active", claim["status"])

        service.update_daily_record(record["id"], self.review_date, "今天饮食和预算都正常。")
        updated = next(item for item in agent_workspace.list_claims() if item["id"] == claim["id"])
        self.assertEqual("pending_confirmation", updated["status"])
        self.assertEqual(0, updated["support_count"])
        old_evidence = next(
            item for item in updated["evidence"]
            if item["evidence_type"] == "daily_record" and item["evidence_id"] == record["id"]
        )
        self.assertEqual(0, old_evidence["active"])

        service.update_daily_record(record["id"], self.review_date, "牛肉太贵，以后还是不要默认安排。")
        reactivated = next(item for item in agent_workspace.list_claims() if item["id"] == claim["id"])
        self.assertEqual("active", reactivated["status"])
        self.assertEqual(1, reactivated["support_count"])
        self.assertEqual(1, next(
            item for item in reactivated["evidence"]
            if item["evidence_type"] == "daily_record" and item["evidence_id"] == record["id"]
        )["active"])

    def test_today_page_explains_new_low_risk_learning_in_plain_language(self):
        service.add_daily_record(self.review_date, "牛肉太贵，以后算了。")
        claim = next(
            item for item in agent_workspace.list_claims()
            if item.get("claim_dimension") == "resource_constraint"
        )
        html = server.render_today_workspace(self.review_date)
        self.assertIn("我准备这样调整", html)
        self.assertIn("以后安排牛肉时，要优先考虑长期负担得起的选择吗？", html)
        self.assertIn("你刚才提到：“牛肉太贵，以后算了。”", html)
        self.assertIn("只是今天", html)
        self.assertIn('name="action" value="reject"', html)
        self.assertNotIn("resource_constraint", html)
        self.assertNotIn("confidence", html)
        rejected = agent_workspace.update_claim(claim["id"], "reject")
        self.assertEqual("refuted", rejected["status"])
        self.assertEqual(1, rejected["counter_count"])

    def test_one_off_time_feedback_waits_for_confirmation_and_explains_the_change(self):
        draft = agent_workspace.run_agent_draft(self.review_date, self._provider())
        accepted = agent_workspace.accept_agent_run(draft["run_id"])
        plan_date = accepted["result_json"]["tomorrow_menu"]["date"]
        plan = adaptive.get_plan_for_date(plan_date)
        breakfast = next(item for item in plan["menu"]["meals"] if item["name"] == "早餐")
        feedback = adaptive.save_plan_feedback(
            plan_date,
            breakfast["plan_item_id"],
            "modified",
            reason_codes=["not_enough_time"],
            actual_text="今天起晚了，所以没时间煮鸡蛋，改成了豆浆和面包。",
            actor_source="web",
        )
        claim = next(
            item for item in agent_workspace.list_claims()
            if item.get("claim_dimension") == "execution_friction"
            and (item.get("scope_json") or {}).get("meal") == "早餐"
        )
        self.assertEqual("pending_confirmation", claim["status"])
        self.assertEqual(0, next(
            item for item in claim["evidence"] if item["evidence_id"] == feedback["id"]
        )["explicit"])

        with patch("mealcircuit.server._render_today_state", return_value=""):
            html = server.render_today_workspace(plan_date)
        self.assertIn("我想确认一下", html)
        self.assertIn("以后需要给早餐准备一个不用开火或更快的备选吗？", html)
        self.assertIn("你刚才提到：“今天起晚了，所以没时间煮鸡蛋，改成了豆浆和面包。”", html)
        self.assertIn("以后都准备", html)
        self.assertIn("只是今天", html)
        self.assertIn("不用这样调整", html)
        self.assertLess(html.index("以后都准备"), html.index("只是今天"))
        self.assertLess(html.index("只是今天"), html.index("不用这样调整"))
        self.assertNotIn("我从这次记录里记住了", html)

    def test_existing_single_feedback_friction_is_returned_to_confirmation(self):
        claim = agent_workspace.upsert_claim(
            claim_type="friction_hypothesis",
            claim_dimension="execution_friction",
            statement="这类餐次的主动操作时间或步骤可能超过可接受范围",
            scope={"meal": "早餐"},
            effect={"complexity": "减少持续看火步骤并提供更短备选"},
            evidence_type="execution_feedback",
            evidence_id="legacy-single-feedback",
            excerpt="起晚了所以没时间煮鸡蛋。",
            explicit=True,
            source="execution_feedback",
        )
        self.assertEqual("active", claim["status"])

        repaired = next(
            item for item in agent_workspace.list_claims() if item["id"] == claim["id"]
        )
        self.assertEqual("pending_confirmation", repaired["status"])
        self.assertEqual(0, repaired["evidence"][0]["explicit"])
        with connect() as conn:
            reason = conn.execute(
                """SELECT change_reason FROM user_model_claim_versions
                   WHERE claim_id=? ORDER BY version DESC LIMIT 1""",
                (claim["id"],),
            ).fetchone()[0]
        self.assertEqual("single_feedback_needs_confirmation", reason)
        agent_workspace.update_claim(claim["id"], "confirm")
        confirmed = next(
            item for item in agent_workspace.list_claims() if item["id"] == claim["id"]
        )
        self.assertEqual("active", confirmed["status"])

    def test_three_photos_and_user_correction_form_one_traceable_meal_episode(self):
        task_ids = []
        for index in range(3):
            task = service.create_photo_task(
                io.BytesIO(b"\x89PNG\r\n\x1a\n" + f"meal-{index}".encode()),
                f"午餐第 {index + 1} 张照片",
            )
            adaptive.link_task_evidence(task["id"], self.review_date, "consumed", "lunch")
            service.complete_task(task["id"], {
                "summary": f"照片 {index + 1} 的保守观察",
                "candidates": [{
                    "name": "带骨禽肉", "portion_range": "约一块",
                    "nutrition": {
                        "energy_kcal": None, "protein_g": None,
                        "carbs_g": None, "fat_g": None,
                    },
                    "confidence": 0.5,
                }],
                "unknowns": ["实际吃下量"],
                "advice": ["结合餐后图和用户描述再判断"],
            })
            task_ids.append(task["id"])
        episode = agent_intelligence.refresh_meal_episode(self.review_date, "lunch")
        projection = episode["projection_json"]
        self.assertEqual("multi_photo_observation", projection["current_fact_source"])
        self.assertEqual(task_ids, episode["source_ids_json"])
        self.assertTrue(all(
            "image_path" not in item for item in projection["photo_and_material_sources"]
        ))

        service.add_correction(task_ids[0], {"text": "实际是一个鸡腿、一个鸭腿和两个豆干。"})
        corrected = agent_intelligence.refresh_meal_episode(self.review_date, "lunch")["projection_json"]
        self.assertEqual("user_correction", corrected["current_fact_source"])
        self.assertIn("两个豆干", corrected["current_fact"])
        self.assertEqual(3, len(corrected["photo_and_material_sources"]))

    def test_feedback_attribution_distinguishes_price_time_inventory_and_taste(self):
        draft = agent_workspace.run_agent_draft(self.review_date, self._provider())
        accepted = agent_workspace.accept_agent_run(draft["run_id"])
        plan_date = accepted["result_json"]["tomorrow_menu"]["date"]
        plan = adaptive.get_plan_for_date(plan_date)
        item_id = plan["menu"]["meals"][2]["plan_item_id"]
        version = 0
        for reason, expected in (
            ("too_expensive", "price"),
            ("not_enough_time", "time"),
            ("missing_ingredient", "inventory"),
            ("did_not_want_it", "taste"),
        ):
            feedback = adaptive.save_plan_feedback(
                plan_date, item_id, "modified", reason_codes=[reason],
                actual_text=f"这次因为{reason}调整了", expected_version=version,
            )
            version = feedback["version"]
            attribution = agent_intelligence.list_outcome_attributions(plan_date, plan_date)[0]
            self.assertEqual(expected, attribution["attribution_json"]["primary_cause"])
