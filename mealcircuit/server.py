from __future__ import annotations

import argparse
import html
import io
import ipaddress
import json
import mimetypes
import os
import re
import sys
import urllib.parse
from datetime import date
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import adaptive, ai, checkins, personalization, portability, service
from .configuration import initialize_private_home
from .db import init_db
from .storage import exports_root, port_value, upload_root
from .validation import ValidationError, nutrition_number


LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
STATIC_ROOT = Path(__file__).with_name("static")


def is_loopback_host(host_name: str | None) -> bool:
    if not host_name:
        return False
    normalized = host_name.lower().rstrip(".")
    if normalized in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def parse_host_endpoint(host_header: str, default_port: int) -> tuple[str, int]:
    try:
        parsed = urllib.parse.urlsplit(f"//{host_header}")
        host_name = parsed.hostname
        host_port = parsed.port or default_port
    except ValueError as exc:
        raise ValidationError("Host 请求头无效") from exc
    if not host_name:
        raise ValidationError("Host 请求头无效")
    return host_name.lower().rstrip("."), host_port


def origin_matches_host(host_name: str, host_port: int, origin: str | None, fetch_site: str | None) -> bool:
    if not origin:
        return True
    if origin == "null":
        return is_loopback_host(host_name) and (fetch_site or "").lower() in {"same-origin", "none"}
    try:
        parsed = urllib.parse.urlsplit(origin)
        origin_name = parsed.hostname
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    if not origin_name or parsed.scheme not in {"http", "https"} or origin_port != host_port:
        return False
    origin_name = origin_name.lower().rstrip(".")
    if is_loopback_host(host_name) and is_loopback_host(origin_name):
        return True
    return origin_name == host_name


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def icon(name: str) -> str:
    return f'<span class="icon icon-{esc(name)}" aria-hidden="true"></span>'


def _selected(value: object, expected: object) -> str:
    return " selected" if value == expected else ""


def _checked(values: object, expected: object) -> str:
    return " checked" if expected in (values or []) else ""


GOAL_LABELS = {
    "fat_loss": "减脂",
    "muscle_gain": "增肌",
    "body_recomposition": "减脂增肌 / 身体重组",
    "performance": "训练表现",
    "maintenance": "维持当前状态",
    "eating_consistency": "建立稳定饮食",
    "general_wellbeing": "一般健康与精力",
    "custom": "其他自定义目标",
}
METRIC_LABELS = {
    "execution_rate": "计划执行率",
    "weight_trend": "体重趋势",
    "waist_trend": "腰围趋势",
    "training_performance": "训练表现",
    "energy_state": "精力与恢复",
    "gut_comfort": "肠胃舒适度",
}

FEEDBACK_LABELS = {
    "followed": "按计划完成",
    "modified": "调整后完成",
    "skipped": "没有执行",
    "not_applicable": "今天不适用",
}
REASON_LABELS = {
    "missing_ingredient": "缺少食材", "not_enough_time": "时间不足", "too_complex": "步骤太复杂",
    "ate_out": "临时外食", "did_not_want_it": "当时不想吃", "hunger_mismatch": "饥饿或份量不匹配",
    "gut_change": "肠胃状态变化", "schedule_change": "日程变化", "other": "其他",
}
RESCUE_LABELS = {
    "ingredient_missing": "缺少食材", "not_enough_time": "时间不足", "too_complex": "做起来太复杂",
    "gut_change": "肠胃状态变化", "schedule_change": "日程变化", "other": "其他临时情况",
}


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]


def _number_or_none(value: str) -> float | None:
    return float(value) if value.strip() else None


def render_setup_start(status: dict) -> str:
    session = status.get("session")
    if session:
        action = f'<a class="button" href="/setup/{esc(session["current_step"])}">继续初始化</a>'
    else:
        action = '<form method="post" action="/setup/start"><button type="submit">开始建立目标契约</button></form>'
    return f'''<section class="setup-shell panel"><p class="eyebrow">Local adaptive workspace</p><h1>先让 MealCircuit 真正理解你的目标</h1>
    <p class="lede">初始化会确认你想达成什么、安全边界、训练与生活约束。答案逐步保存在本机，可随时退出继续；没有确认前不会生成处方型计划。</p>
    <div class="setup-principles"><p><strong>目标可修改</strong><br><span class="muted">历史计划仍按当时版本解释。</span></p><p><strong>未知保持未知</strong><br><span class="muted">不知道或暂不回答不会被当成否定。</span></p><p><strong>安全许可先于生成</strong><br><span class="muted">专业指导模式只执行有来源的约束。</span></p></div>{action}
    <p class="muted small">原有记录、照片和库存入口始终可用；初始化只限制生成和提交。</p></section>'''


def render_setup_step(session: dict, step: str, *, error: str = "", override: dict | None = None) -> str:
    answers = session.get("answers_json") or {}
    value = override if override is not None else (answers.get(step) or {})
    order = list(personalization.ONBOARDING_STEPS)
    index = order.index(step)
    progress = round((index + 1) / len(order) * 100)
    error_html = f'<div class="form-error" role="alert" tabindex="-1"><strong>请检查这一页</strong><p>{esc(error)}</p></div>' if error else ""
    hidden = f'<input type="hidden" name="session_id" value="{esc(session["id"])}"><input type="hidden" name="version" value="{esc(session["version"])}">'
    if step == "welcome":
        fields = '''<p>你的结构化档案、真实执行数据和照片路径保存在本机。只有你显式配置模型和 API Key 并点击生成时，当前上下文才会发送给所选服务。</p>
        <label class="choice-row"><input type="checkbox" name="privacy_ack" value="yes" required><span>我理解本地保存与模型发送边界</span></label>'''
    elif step == "goals":
        primary = value.get("primary_goal", "body_recomposition")
        primary_options = "".join(f'<option value="{key}"{_selected(primary, key)}>{esc(label)}</option>' for key, label in GOAL_LABELS.items())
        secondary = value.get("secondary_goals") or []
        secondary_html = "".join(
            f'<label class="choice-row"><input type="checkbox" name="secondary_goals" value="{key}"{_checked(secondary, key)}><span>{esc(label)}</span></label>'
            for key, label in GOAL_LABELS.items()
        )
        metrics = value.get("success_metrics") or ["execution_rate"]
        metrics_html = "".join(
            f'<label class="choice-row"><input type="checkbox" name="success_metrics" value="{key}"{_checked(metrics, key)}><span>{esc(label)}</span></label>'
            for key, label in METRIC_LABELS.items()
        )
        fields = f'''<label for="primary-goal">当前最重要的目标</label><select id="primary-goal" name="primary_goal">{primary_options}</select>
        <label for="custom-goal">如果选择“其他”，请写下真正想达成的状态</label><input id="custom-goal" name="custom_goal_text" value="{esc(value.get('custom_goal_text',''))}" placeholder="例如：在轮班生活中保持稳定进食与精力">
        <fieldset class="form-section"><legend>次要目标（可选）</legend><div class="option-grid">{secondary_html}</div></fieldset>
        <label for="motivation">为什么现在想做这件事？</label><textarea id="motivation" name="motivation">{esc(value.get("motivation", ""))}</textarea>
        <fieldset class="form-section"><legend>怎样算有进展？</legend><div class="option-grid">{metrics_html}</div></fieldset>
        <div class="row"><div><label for="target-weight">目标体重 kg（可选，不会直接变成热量处方）</label><input id="target-weight" type="number" step="0.1" name="target_weight_kg" value="{esc(value.get("target_weight_kg", ""))}"></div><div><label for="horizon">期望周期（可选）</label><input id="horizon" name="horizon" value="{esc(value.get("horizon", ""))}" placeholder="例如：先观察 8 周"></div></div>'''
    elif step == "baseline":
        physiological = value.get("physiological_input", "unspecified")
        activity = value.get("activity_level", "moderate")
        fields = f'''<p class="muted">身高和体重可以稍后补充；缺失时系统使用份量法，不猜测数值目标。</p><div class="row"><div><label for="age">年龄 *</label><input id="age" type="number" name="age_years" min="13" max="120" required value="{esc(value.get("age_years", ""))}"></div><div><label for="height">身高 cm（可选）</label><input id="height" type="number" step="0.1" name="height_cm" value="{esc(value.get("height_cm", ""))}"></div><div><label for="weight">当前体重 kg（可选）</label><input id="weight" type="number" step="0.1" name="weight_kg" value="{esc(value.get("weight_kg", ""))}"></div><div><label for="physiological">仅用于能量估算的生理参数</label><select id="physiological" name="physiological_input"><option value="unspecified"{_selected(physiological,"unspecified")}>不提供</option><option value="male"{_selected(physiological,"male")}>男性公式参数</option><option value="female"{_selected(physiological,"female")}>女性公式参数</option></select></div></div><label for="activity">日常活动水平</label><select id="activity" name="activity_level"><option value="low"{_selected(activity,"low")}>较低</option><option value="moderate"{_selected(activity,"moderate")}>中等</option><option value="high"{_selected(activity,"high")}>较高</option><option value="very_high"{_selected(activity,"very_high")}>极高 / 不使用通用系数</option></select>'''
    elif step == "safety":
        life = value.get("life_stage", "adult")
        flag_labels = {
            "therapeutic_diet": "正在执行治疗性饮食",
            "medication_affects_nutrition": "药物可能影响食欲、体重或营养",
            "eating_disorder_risk": "当前存在高风险限制、暴食、清除或补偿行为",
            "rapid_unexplained_change": "近期有快速且原因不明的体重变化",
            "severe_persistent_symptoms": "存在严重或持续的身体症状",
            "severe_allergy_management": "需要严格管理严重食物过敏",
        }
        flag_html = "".join(
            f'<div><label for="safe-{key}">{esc(label)}</label><select id="safe-{key}" name="{key}"><option value="no"{_selected(bool(value.get(key)),False)}>否</option><option value="yes"{_selected(bool(value.get(key)),True)}>是</option></select></div>'
            for key, label in flag_labels.items()
        )
        guidance = value.get("professional_guidance") or {}
        fields = f'''<div class="notice panel"><strong>这不是诊断问卷。</strong><p>回答只用于确定 MealCircuit 可以做什么；严重症状和高风险进食行为会停止营养优化并建议寻求专业帮助。</p></div><label for="life-stage">生命阶段</label><select id="life-stage" name="life_stage"><option value="adult"{_selected(life,"adult")}>成年人</option><option value="pregnant"{_selected(life,"pregnant")}>孕期</option><option value="breastfeeding"{_selected(life,"breastfeeding")}>哺乳期</option><option value="minor"{_selected(life,"minor")}>未成年人</option><option value="other"{_selected(life,"other")}>其他</option></select><div class="row">{flag_html}</div><details><summary>录入已确认的专业指导（可选）</summary><label class="choice-row"><input type="checkbox" name="guidance_confirmed" value="yes"{' checked' if guidance.get('confirmed') else ''}><span>我有仍有效的专业指导</span></label><label for="guidance-source">来源</label><input id="guidance-source" name="guidance_source" value="{esc(guidance.get('source',''))}" placeholder="例如：注册营养师书面计划"><label for="guidance-summary">必须遵守的摘要</label><textarea id="guidance-summary" name="guidance_summary">{esc(guidance.get('summary',''))}</textarea><div class="row"><div><label for="guidance-on">确认日期</label><input id="guidance-on" type="date" name="guidance_confirmed_on" value="{esc(guidance.get('confirmed_on',''))}"></div><div><label for="guidance-until">有效期</label><input id="guidance-until" type="date" name="guidance_valid_until" value="{esc(guidance.get('valid_until',''))}"></div></div></details>'''
    elif step == "training":
        types = value.get("types") or []
        options = {"strength": "力量训练", "cardio": "耐力训练", "sport": "专项运动", "mobility": "灵活性 / 恢复", "other": "其他"}
        checks = "".join(f'<label class="choice-row"><input type="checkbox" name="types" value="{key}"{_checked(types,key)}><span>{label}</span></label>' for key,label in options.items())
        fields = f'''<fieldset class="form-section"><legend>训练类型（可多选）</legend><div class="option-grid">{checks}</div></fieldset><label for="frequency">每周训练次数</label><input id="frequency" type="number" min="0" max="14" name="frequency_per_week" value="{esc(value.get('frequency_per_week',0))}">'''
    elif step == "constraints":
        fields = f'''<label for="meal-environment">典型用餐环境</label><input id="meal-environment" name="meal_environment" required value="{esc(value.get('meal_environment',''))}" placeholder="工作日食堂，晚餐在家"><label for="portion-method">希望怎样表达份量</label><input id="portion-method" name="portion_method" required value="{esc(value.get('portion_method','手掌与拳头份量法'))}"><div class="row"><div><label for="cooking-time">通常可用做饭时间（分钟）</label><input id="cooking-time" type="number" min="0" max="180" name="cooking_time_minutes" value="{esc(value.get('cooking_time_minutes',25))}"></div><div><label for="question-budget">每天最多主动问几题</label><input id="question-budget" type="number" min="0" max="5" name="question_budget" value="{esc(value.get('question_budget',2))}"></div></div><label for="equipment">可用厨具（逗号分隔）</label><input id="equipment" name="equipment" value="{esc(', '.join(value.get('equipment') or []))}"><label for="exclusions">排除食品（逗号分隔）</label><input id="exclusions" name="food_exclusions" value="{esc(', '.join(value.get('food_exclusions') or []))}"><label for="preferences">偏好（逗号分隔）</label><input id="preferences" name="preferences" value="{esc(', '.join(value.get('preferences') or []))}">'''
    else:
        preview = personalization.onboarding_preview(session["id"])
        safety = preview["safety"]
        assessment = preview["target_assessment"]
        primary_goal = preview["goals"][0]
        primary_goal_label = primary_goal.get("custom_label") or GOAL_LABELS.get(primary_goal["type"], primary_goal["type"])
        target_options = "".join(
            f'<label class="choice-row"><input type="radio" name="protein_candidate_id" value="{esc(item["candidate_id"])}"{ " checked" if len(assessment["protein_candidates"]) == 1 else ""}><span>{esc(item["target_g"])} g/天 · {esc(item["basis"])}</span></label>'
            for item in assessment["protein_candidates"]
        ) or '<p class="muted">当前不建立蛋白目标；继续使用份量与执行策略。</p>'
        professional_target_fields = ""
        if safety["mode"] == "clinician_guided" and safety["professional_guidance_current"]:
            professional_target_fields = '''<fieldset class="form-section"><legend>专业指导中的蛋白范围（可选）</legend><p class="muted">只录入指导中明确给出的范围；来源和有效期沿用上一页的专业指导，不做系统推算。</p><div class="row"><label>下界 g/天<input type="number" step="0.1" name="professional_protein_low"></label><label>上界 g/天<input type="number" step="0.1" name="professional_protein_high"></label></div></fieldset>'''
        strategy_ack = '' if safety["mode"] in {"observation", "halt_and_refer"} or (safety["mode"] == "clinician_guided" and not safety["professional_guidance_current"]) else '<label class="choice-row"><input type="checkbox" name="accept_strategy" value="yes" required><span>我确认采用这份初始策略</span></label>'
        fields = f'''<div class="contract-summary"><p><span class="status">安全模式</span><strong>{esc(safety["mode"])}</strong></p><p><span class="status">主目标</span><strong>{esc(primary_goal_label)}</strong></p><p><span class="status">规划模式</span><strong>{esc(assessment["planning_default"])}</strong></p></div><h2>蛋白目标候选</h2><div class="option-list">{target_options}</div>{professional_target_fields}<input type="hidden" name="planning_mode" value="portion_guided"><label class="choice-row"><input type="checkbox" name="accept_profile" value="yes" required><span>我确认系统对目标与安全边界的理解</span></label>{strategy_ack}<div class="notice panel"><strong>仍然未知</strong><ul>{''.join(f'<li>{esc(note)}</li>' for note in assessment['notes']) or '<li>没有额外提示</li>'}</ul></div>'''
    action = "/setup/complete" if step == "review" else f"/setup/save/{step}"
    submit = "确认并进入工作台" if step == "review" else "保存并继续"
    return f'''<section class="setup-shell"><div class="setup-progress"><p class="eyebrow">步骤 {index + 1} / {len(order)}</p><div class="progress-track" role="progressbar" aria-valuemin="1" aria-valuemax="{len(order)}" aria-valuenow="{index + 1}"><span class="progress-fill" style="width:{progress}%"></span></div></div>{error_html}<form class="panel setup-form" method="post" action="{action}">{hidden}<h1>{esc({"welcome":"隐私与边界","goals":"目标契约","baseline":"当前基线","safety":"安全边界","training":"训练需求","constraints":"现实约束","review":"确认理解"}[step])}</h1>{fields}<div class="form-actions"><button type="submit">{submit}</button></div></form></section>'''


def render_today_workspace(work_date: str) -> str:
    current = personalization.active_personalization()
    if current["status"] == "setup_required":
        return render_setup_start(personalization.onboarding_status())
    plan = adaptive.get_plan_for_date(work_date)
    questions = adaptive.schedule_questions(work_date)
    safety = current["safety"]
    goal = (current.get("goals") or [{}])[0].get("goal_json") or {}
    goal_label = goal.get("custom_label") or GOAL_LABELS.get(goal.get("type"), goal.get("type") or "未设定")
    if plan:
        completed = len(plan["feedback"])
        plan_block = (
            f'<section class="panel today-plan"><div class="section-header"><div><p class="eyebrow">Today plan</p><h2>今天按这份计划走</h2>'
            f'<p class="muted">{completed} / {len(plan["menu"]["meals"])} 项已有回执</p></div><a class="button" href="/plans/{esc(work_date)}">打开计划</a></div></section>'
        ) if plan.get("scope_current") else (
            '<section class="panel today-plan"><p class="eyebrow">Stale plan</p><h2>旧目标版本的计划不再作为今天指令</h2>'
            '<p>可以保留历史执行回执，但需要按当前目标与边界重新生成计划。</p><a class="button" href="/daily">重新生成</a></section>'
        )
    else:
        policy = personalization.generation_policy("daily")
        plan_block = (
            '<section class="panel today-plan"><p class="eyebrow">Next action</p><h2>先记录今天，再生成下一份正式计划</h2>'
            f'<p>{esc(policy["reason"] or "记录会进入证据层；只有正式发布的计划才会进入执行学习。")}</p>'
            f'<div class="actions"><a class="button" href="/capture">记录真实情况</a><a class="button secondary" href="/daily">查看建议状态</a></div></section>'
        )
    question_block = (
        f'<section class="panel"><div class="section-header"><div><p class="eyebrow">Low-friction check</p><h2>只问最有用的问题</h2><p class="muted">今天还有 {len(questions)} 个问题</p></div>'
        f'<a class="button secondary" href="/questions/{esc(work_date)}">回答</a></div></section>' if questions else
        '<section class="panel quiet-success"><h2>今天不需要再回答问题</h2><p class="muted">MealCircuit 会继续从执行回执中积累证据。</p></section>'
    )
    return (
        f'<section class="today-hero"><div><p class="eyebrow">Adaptive workspace · {esc(work_date)}</p><h1>今天只处理下一步</h1>'
        f'<p class="lede">当前目标：{esc(goal_label)}。安全模式：{esc(safety["mode"])}。</p></div><a class="button secondary" href="/profile">目标与边界</a></section>'
        f'<div class="today-grid">{plan_block}{question_block}</div>'
        '<section class="quick-actions" aria-label="快速操作"><a class="quick-card" href="/capture"><strong>记录事实</strong><span>饮食、照片、原材料和状态</span></a>'
        f'<a class="quick-card" href="/plans/{esc(work_date)}"><strong>执行计划</strong><span>查看步骤、反馈或临时救场</span></a>'
        '<a class="quick-card" href="/inventory"><strong>管理库存</strong><span>临期、用完和未购买都保留事件</span></a>'
        '<a class="quick-card" href="/learning"><strong>确认学习</strong><span>候选规则不会静默生效</span></a></section>'
    )


def _plan_step_text(item: str | dict) -> str:
    if isinstance(item, str):
        return item
    minutes = f"{item.get('minutes')} 分钟" if item.get("minutes") else None
    return " · ".join(str(value) for value in (
        item.get("instruction") or item.get("text"),
        minutes,
        item.get("heat"),
        item.get("done_signal"),
    ) if value)


def render_plan_page(plan_date: str) -> str:
    plan = adaptive.get_plan_for_date(plan_date)
    if not plan:
        policy = personalization.generation_policy("daily")
        return f'<section class="panel"><h1>{esc(plan_date)} 没有可执行计划</h1><p>{esc(policy["reason"] or "先记录真实情况并完成每日复盘；草稿不会进入执行学习。")}</p><a class="button" href="/capture">去记录</a></section>'
    cards = []
    for meal in plan["menu"]["meals"]:
        feedback = plan["feedback"].get(meal["plan_item_id"])
        recipe = meal.get("recipe_card") or {}
        ingredients = meal.get("ingredients") or recipe.get("ingredients") or meal.get("foods") or []
        steps = meal.get("steps") or recipe.get("steps") or []
        detail = "".join(
            f'<li>{esc(item if isinstance(item, str) else " · ".join(str(value) for value in (item.get("name"), item.get("amount"), item.get("prep")) if value))}</li>'
            for item in ingredients
        )
        step_html = "".join(f'<li>{esc(_plan_step_text(item))}</li>' for item in steps)
        execution = meal.get("execution") or {}
        execution_bits = [
            f'主动 {execution["active_minutes"]} 分钟' if execution.get("active_minutes") is not None else "",
            f'总计 {execution["total_minutes"]} 分钟' if execution.get("total_minutes") is not None else "",
            f'炊具：{"、".join(execution.get("cookware") or [])}' if execution.get("cookware") else "",
        ]
        execution_html = " · ".join(item for item in execution_bits if item)
        current_status = feedback.get("status") if feedback else ""
        status_options = "".join(f'<option value="{key}"{_selected(current_status,key)}>{label}</option>' for key,label in FEEDBACK_LABELS.items())
        reason_checks = "".join(f'<label class="choice-row compact"><input type="checkbox" name="reason_codes" value="{key}"><span>{label}</span></label>' for key,label in REASON_LABELS.items())
        version = feedback.get("version", 0) if feedback else 0
        cards.append(f'''<article class="plan-card"><div class="section-header"><div><p class="eyebrow">{esc(meal.get('slot') or meal.get('meal_type') or '')}</p><h2>{esc(meal.get('name') or '未命名餐次')}</h2></div>{f'<span class="status completed">{esc(FEEDBACK_LABELS.get(current_status,current_status))}</span>' if feedback else '<span class="status pending">待回执</span>'}</div>
        {f'<p><strong>份量：</strong>{esc(meal.get("portion_guidance"))}</p>' if meal.get('portion_guidance') else ''}{f'<p class="muted">{esc(execution_html)}</p>' if execution_html else ''}{f'<h3>食材</h3><ul>{detail}</ul>' if detail else ''}{f'<h3>执行步骤</h3><ol>{step_html}</ol>' if step_html else ''}
        <details class="feedback-box"{' open' if not feedback else ''}><summary>{'修订执行回执' if feedback else '记录实际执行结果'}</summary><form method="post" action="/plans/{esc(plan_date)}/{esc(meal['plan_item_id'])}/feedback"><input type="hidden" name="expected_version" value="{version}"><label>执行状态<select name="status" required><option value="">请选择</option>{status_options}</select></label><fieldset><legend>偏离原因（调整或未执行时必选）</legend><div class="option-grid">{reason_checks}</div></fieldset><label>实际怎么做的（可选）<textarea name="actual_text">{esc(feedback.get('actual_text','') if feedback else '')}</textarea></label><button type="submit">保存回执</button></form></details>
        {f'<form class="rescue-form" method="post" action="/rescue/start"><input type="hidden" name="plan_date" value="{esc(plan_date)}"><input type="hidden" name="plan_item_id" value="{esc(meal["plan_item_id"])}"><label>计划临时做不了怎么办？<select name="issue_code">{"".join(f"<option value={key!r}>{label}</option>" for key,label in RESCUE_LABELS.items())}</select></label><input name="input_text" aria-label="救场补充" placeholder="可补充当前手边条件"><button class="secondary" type="submit">生成救场任务</button></form>' if plan.get('scope_current') else ''}</article>''')
    stale = '' if plan.get('scope_current') else '<div class="form-error" role="alert"><strong>这是旧目标或策略版本的历史计划</strong><p>仍可补录实际执行结果，但不能据此生成新的救场建议。</p></div>'
    return f'<section class="section-header"><div><p class="eyebrow">Published plan · v{esc(plan["result_version"])}</p><h1>{esc(plan_date)} 执行计划</h1><p class="muted">每次修改回执都会追加事件；反馈绑定这份正式计划的目标与安全版本。</p></div><a class="button secondary" href="/questions/{esc(plan_date)}">最少提问</a></section>{stale}<div class="plan-list">{"".join(cards)}</div>'


def render_questions_page(question_date: str) -> str:
    pending = adaptive.schedule_questions(question_date)
    if not pending:
        return f'<section class="panel quiet-success"><h1>{esc(question_date)} 没有待回答问题</h1><p>问题预算已用完，或当前没有会实质改变计划的未知项。</p><a class="button secondary" href="/">返回今天</a></section>'
    cards = []
    choice_labels = {"yes":"是", "no":"否", "unknown":"暂不确定", "home":"在家", "away":"外食", "mixed":"混合"}
    for item in pending:
        schema = item.get("question_schema_json") or {}
        if schema.get("kind") == "action":
            control = f'<a class="button" href="{esc(schema.get("href","/setup"))}">完成此操作</a>'
        elif schema.get("kind") == "plan_feedback":
            statuses = "".join(f'<option value="{key}">{FEEDBACK_LABELS.get(key,key)}</option>' for key in schema.get("statuses", []))
            reasons = "".join(f'<label class="choice-row compact"><input type="checkbox" name="reason_codes" value="{key}"><span>{REASON_LABELS.get(key,key)}</span></label>' for key in schema.get("reason_codes", []))
            control = f'<label>执行状态<select name="feedback_status" required><option value="">请选择</option>{statuses}</select></label><fieldset><legend>偏离原因</legend><div class="option-grid">{reasons}</div></fieldset><label>补充<textarea name="actual_text"></textarea></label><button>保存答案</button>'
        else:
            options = "".join(f'<label class="choice-row"><input type="radio" name="answer" value="{esc(value)}" required><span>{esc(choice_labels.get(value,value))}</span></label>' for value in schema.get("options", []))
            control = f'<div class="option-list">{options}</div><button>保存答案</button>'
        cards.append(f'''<article class="panel question-card"><p class="eyebrow">{esc(item['category'])}</p><h2>{esc(item.get('prompt') or item['reason'])}</h2><p>{esc(item['reason'])}</p><p class="muted">会影响：{esc(item['expected_impact'])}</p><form method="post" action="/questions/{esc(item['id'])}/answer"><input type="hidden" name="version" value="{esc(item['version'])}">{control}</form><form method="post" action="/questions/{esc(item['id'])}/skip"><input type="hidden" name="version" value="{esc(item['version'])}"><button class="link-button" type="submit">暂时跳过</button></form></article>''')
    return f'<section class="section-header"><div><p class="eyebrow">Question budget</p><h1>只补齐会改变行动的信息</h1><p class="muted">跳过会明确记录为未知，不会被推断成“否”。</p></div><span class="history-count">{len(cards)} 题</span></section><div class="question-list">{"".join(cards)}</div>'


def render_learning_page() -> str:
    candidates = adaptive.list_candidates("pending")
    rules = adaptive.list_rules(active_only=False)
    experiments = adaptive.list_experiments()
    candidate_html = "".join(
        f'''<article class="learning-card panel"><p class="eyebrow">{esc(item['kind'])} · {esc(item['confidence'])}</p><h2>{esc(item['statement'])}</h2><p class="muted">支持证据 {len([e for e in item['evidence'] if e['stance']=='support'])} · 反例 {len([e for e in item['evidence'] if e['stance']=='counterexample'])}</p><form method="post" action="/learning/{esc(item['id'])}/decide"><label>确认后的规则文字<textarea name="statement">{esc(item['statement'])}</textarea></label><div class="actions"><button name="decision" value="accept">接受规则</button><button class="secondary" name="decision" value="snooze">稍后再看</button><button class="danger" name="decision" value="reject">拒绝</button></div></form></article>'''
        for item in candidates
    ) or '<section class="panel quiet-success"><h2>没有待确认的学习候选</h2><p>系统需要重复证据才会提出规则，也不会自动应用候选。</p></section>'
    rule_html = "".join(
        f'<li><div><strong>{esc(item["statement"])}</strong><br><span class="muted small">{esc(item["origin"])} · {esc(item["status"])}</span></div><form method="post" action="/learning/rules/{esc(item["id"])}/status"><input type="hidden" name="status" value="{"inactive" if item["status"]=="active" else "active"}"><button class="secondary">{"停用" if item["status"]=="active" else "启用"}</button></form></li>'
        for item in rules
    ) or '<li class="muted">暂无正式规则</li>'
    experiment_cards = []
    for item in experiments:
        plan = item.get("plan_json") or {}
        if item["status"] == "proposed":
            action = f'''<form method="post" action="/learning/experiments/{esc(item['id'])}/start"><label>开始日期<input type="date" name="starts_on" value="{date.today().isoformat()}" required></label><label>观察天数（3–7）<input type="number" name="days" min="3" max="7" value="5" required></label><button>确认开始</button></form>'''
        elif item["status"] == "active":
            action = f'''<form method="post" action="/learning/experiments/{esc(item['id'])}/finish"><label>观察结果<textarea name="summary" required></textarea></label><div class="actions"><button name="decision" value="complete">完成实验</button><button class="danger" name="decision" value="cancel">取消实验</button></div></form>'''
        else:
            result = item.get("result_json") or {}
            action = f'<p class="muted">结果：{esc(result.get("summary") or "未填写")}</p>'
        experiment_cards.append(f'<article class="panel"><p class="eyebrow">{esc(item["status"])} · v{esc(item["version"])}</p><h3>{esc(plan.get("action") or item["variable_key"])}</h3><p>成功信号：{esc(plan.get("success_signal") or "未设置")}</p>{action}</article>')
    experiment_policy = personalization.generation_policy("adaptation")
    propose_form = (
        '''<form method="post" action="/learning/experiments"><label>只改变一个变量<input name="variable_key" required placeholder="例如：dinner_active_minutes"></label><label>具体动作<textarea name="action" required placeholder="例如：晚餐主动准备时间控制在 15 分钟"></textarea></label><label>怎样算有效<input name="success_signal" required placeholder="例如：连续 3 次完成且主观负担可接受"></label><button>提出可撤销实验</button></form>'''
        if experiment_policy["allowed"] else f'<p class="muted">{esc(experiment_policy["reason"])}</p>'
    )
    return f'<section class="section-header"><div><p class="eyebrow">Deterministic learning</p><h1>由你确认，系统才学习</h1><p class="muted">候选、正式规则和实验均绑定当前目标、策略与安全模式。</p></div></section><div class="learning-grid"><div>{candidate_html}</div><section class="panel"><h2>正式规则</h2><ul class="rule-list">{rule_html}</ul></section></div><section class="section-header"><div><p class="eyebrow">One variable at a time</p><h2>可撤销实验</h2><p class="muted">同时最多一个待确认或进行中的实验；实验不会自动修改营养目标。</p></div></section><div class="learning-grid"><section class="panel"><h3>提出实验</h3>{propose_form}</section><div>{"".join(experiment_cards) or '<section class="panel"><p class="muted">暂无实验历史</p></section>'}</div></div>'


def render_inventory_page() -> str:
    items = adaptive.list_inventory(active_only=False)
    rows = "".join(
        f'''<tr><td><strong>{esc(item['name'])}</strong><br><span class="muted small">{esc(item.get('amount_text') or '数量未知')}</span></td><td>{esc(item.get('expires_on') or '未设期限')}</td><td>{esc(item['status'])}</td><td><form class="inline-form" method="post" action="/inventory/{esc(item['id'])}"><input type="hidden" name="version" value="{esc(item['version'])}"><input name="amount_text" value="{esc(item.get('amount_text') or '')}" aria-label="更新数量"><select name="status" aria-label="更新状态">{''.join(f'<option value="{state}"{_selected(item["status"],state)}>{state}</option>' for state in sorted(adaptive.INVENTORY_STATUSES))}</select><button>更新</button></form></td></tr>'''
        for item in items
    ) or '<tr><td colspan="4" class="muted">还没有库存记录</td></tr>'
    return f'''<section class="section-header"><div><p class="eyebrow">Inventory events</p><h1>食材库存与临期状态</h1><p class="muted">每次状态变化都有事件记录，不把“没买到”误当成“吃完”。</p></div></section><section class="panel"><form class="inventory-add" method="post" action="/inventory"><label>食材名称<input name="name" required></label><label>大概数量<input name="amount_text"></label><label>期限<input type="date" name="expires_on"></label><button>加入库存</button></form><div class="table-scroll"><table><thead><tr><th>食材</th><th>期限</th><th>状态</th><th>更新</th></tr></thead><tbody>{rows}</tbody></table></div></section>'''


def render_profile_page() -> str:
    current = personalization.active_personalization()
    if current["status"] == "setup_required":
        return render_setup_start(personalization.onboarding_status())
    profile = current["profile"]
    goals = "".join(f'<li>{esc(item["goal_json"].get("custom_label") or GOAL_LABELS.get(item["goal_json"].get("type"), item["goal_json"].get("type")))}</li>' for item in current["goals"])
    targets = "".join(f'<li><strong>{esc(item["target_key"])}</strong> {esc(item["value_json"])}<br><span class="muted small">{esc(item["source_kind"])} · {esc(item["method"])} · policy {esc(item["policy_version"])}</span></li>' for item in current["targets"]) or '<li class="muted">当前没有数值营养目标</li>'
    return f'''<section class="section-header"><div><p class="eyebrow">Versioned contract</p><h1>目标、边界与来源</h1><p class="muted">当前档案 v{esc(profile['version'])}；修改会创建新版本，旧计划仍能按原版本解释。</p></div><div class="actions"><form method="post" action="/setup/start"><button>修订档案</button></form><a class="button secondary" href="/data">备份与迁移</a></div></section><div class="grid"><section class="panel"><h2>目标</h2><ol>{goals}</ol></section><section class="panel"><h2>安全模式</h2><p class="status">{esc(current['safety']['mode'])}</p><p>{esc(', '.join(current['safety'].get('flags') or []) or '无额外安全标志')}</p></section><section class="panel"><h2>营养目标与 provenance</h2><ul>{targets}</ul></section></div>'''


def render_insights_page() -> str:
    snapshot = adaptive.calibration_snapshot()
    observations = personalization.list_metrics(limit=20)
    observation_rows = "".join(
        f'<tr><td>{esc(item["observed_date"])}</td><td>{esc(METRIC_LABELS.get(item["metric_key"], item["metric_key"]))}</td><td>{esc(item["value_json"])}</td><td>{esc(item["source"])}</td></tr>'
        for item in observations
    ) or '<tr><td colspan="4" class="muted">暂无独立指标记录</td></tr>'
    return f'''<section class="section-header"><div><p class="eyebrow">Calibration</p><h1>证据覆盖与校准资格</h1><p class="muted">证据不足时只解释执行摩擦，不擅自修改营养目标。</p></div></section><div class="metric-grid"><article class="metric-card"><span>反馈天数</span><strong>{esc(snapshot['feedback_days'])}</strong></article><article class="metric-card"><span>执行事件</span><strong>{esc(snapshot['feedback_events'])}</strong></article><article class="metric-card"><span>可比体重记录</span><strong>{esc(snapshot['comparable_weight_events'])}</strong></article></div><div class="grid"><section class="panel"><h2>当前判断</h2><p>{esc(snapshot['rule'])}</p><p><strong>策略复盘：</strong>{'证据已达到最低门槛' if snapshot['eligible_for_strategy_review'] else '继续收集真实执行回执'}</p><p><strong>体重校准：</strong>{'具备分析资格，仍需用户确认任何目标变化' if snapshot['eligible_for_weight_calibration'] else '数据不足，不调整目标'}</p></section><section class="panel"><h2>记录可比指标</h2><form method="post" action="/metrics"><label>日期<input type="date" name="observed_date" value="{date.today().isoformat()}" required></label><label>指标<select name="metric_key"><option value="weight_kg">体重 kg</option><option value="waist_cm">腰围 cm</option><option value="execution_rate">计划执行率 %</option><option value="energy_state">精力（文字）</option><option value="training_performance">训练表现（文字）</option></select></label><label>观测值<input name="value" required></label><button>追加观测</button></form></section></div><section class="panel"><h2>最近指标历史</h2><div class="table-scroll"><table><thead><tr><th>日期</th><th>指标</th><th>值</th><th>来源</th></tr></thead><tbody>{observation_rows}</tbody></table></div></section>'''


def render_data_page(message: str = "") -> str:
    message_html = f'<div class="quiet-success panel" role="status">{esc(message)}</div>' if message else ""
    return f'''<section class="section-header"><div><p class="eyebrow">Portable local data</p><h1>备份、恢复与设备迁移</h1><p class="muted">导出包包含数据库快照、配置与本地媒体，并用 manifest 和 SHA-256 校验。</p></div></section>{message_html}<div class="grid"><section class="panel"><h2>导出完整工作台</h2><p>生成可验证 ZIP；API Key 从不写入数据库或导出包。</p><a class="button" href="/data/export">生成并下载</a></section><section class="panel"><h2>恢复完整工作台</h2><p>上传后先验证路径、哈希、格式、schema 和数据库完整性。应用前会自动创建当前数据库备份。</p><form method="post" enctype="multipart/form-data" action="/data/import"><label>MealCircuit ZIP<input type="file" name="bundle" accept="application/zip,.zip" required></label><label class="choice-row"><input type="checkbox" name="confirm_restore" value="yes" required><span>我理解恢复会替换当前数据库，并确认应用</span></label><button class="danger" type="submit">验证并恢复</button></form></section></div>'''


def render_capture_page() -> str:
    today = date.today().isoformat()
    return f'''<section class="section-header"><div><p class="eyebrow">Evidence first</p><h1>记录发生了什么</h1><p class="muted">记录是事实层，不等于计划已执行；照片与原材料在安全受限模式下自动使用事实型 schema。</p></div></section><div class="capture-grid"><section class="panel"><h2>自然语言记录</h2><form method="post" action="/records"><input type="hidden" name="record_date" value="{today}"><label>今天吃了什么、身体或日程怎样<textarea name="raw_input" required placeholder="例如：午餐临时外食，晚饭时间只有 10 分钟；训练完成但食欲一般"></textarea></label><button>保存事实</button></form></section><section class="panel"><h2>每日状态</h2><p>用结构化问答记录睡眠、训练、肠胃等；跳过保持未知。</p><a class="button secondary" href="/check-ins/{today}">记录状态</a></section><section class="panel"><h2>照片证据</h2><p>看不见的油、重量、酱汁和品牌保持未知。</p><a class="button secondary" href="/tasks/photo">上传照片</a></section><section class="panel"><h2>原材料 / 库存</h2><p>分析已有食材，或直接更新可用库存。</p><div class="actions"><a class="button secondary" href="/tasks/material">分析原材料</a><a class="button secondary" href="/inventory">库存</a></div></section></div>'''


def render_rescue_page(rescue_id: str) -> str:
    session = adaptive.get_rescue_session(rescue_id)
    if session["status"] == "completed":
        result = session.get("result_json") or {}
        steps = "".join(f'<li>{esc(item)}</li>' for item in result.get("steps") or [])
        replacements = "、".join(result.get("replacement_foods") or [])
        safety_notes = "".join(f'<li>{esc(item)}</li>' for item in result.get("safety_notes") or [])
        return f'<section class="panel"><p class="eyebrow">Rescue completed</p><h1>当前这一步的救场方案</h1><p>{esc(result.get("reason") or "")}</p>{f"<p><strong>替代食材：</strong>{esc(replacements)}</p>" if replacements else ""}{f"<p><strong>份量变化：</strong>{esc(result.get('portion_change'))}</p>" if result.get('portion_change') else ""}<h2>现在这样做</h2><ol>{steps}</ol>{f"<h2>安全提示</h2><ul>{safety_notes}</ul>" if safety_notes else ""}<p class="muted">此结果已通过当前计划硬约束校验，并绑定原计划与来源清单。</p><a class="button" href="/plans/{esc(session["plan_date"])}">回到正式计划</a></section>'
    policy = personalization.generation_policy("rescue")
    control = (
        f'<form method="post" action="/rescue/{esc(rescue_id)}/generate"><button>用当前模型生成救场方案</button></form>'
        if policy["allowed"] else f'<div class="form-error" role="alert"><strong>当前不能生成救场建议</strong><p>{esc(policy["reason"])}</p></div>'
    )
    return f'''<section class="panel"><p class="eyebrow">Bound rescue session</p><h1>修复当前这一步，不重写整份计划</h1><p>问题：{esc(RESCUE_LABELS.get(session['issue_code'],session['issue_code']))}</p><p class="muted">{esc(session.get('input_text') or '没有额外补充')}</p>{control}<p class="muted small">救场结果会绑定原计划版本、上下文哈希、policy 与 validator，并自动追加执行回执事件。</p><a class="button secondary" href="/plans/{esc(session['plan_date'])}">返回计划</a></section>'''


def layout(title: str, body: str) -> bytes:
    today = date.today()
    checkin_path = f"/check-ins/{today.isoformat()}"
    nav_groups = (
        ("工作台", (
            ("/", "今天", "dashboard", title == "今天"),
            (f"/plans/{today.isoformat()}", "计划", "advice", title == "执行计划"),
            ("/capture", "记录", "checkin", title in {"记录", "今日状态", "状态问答", "状态设置"}),
            ("/insights", "洞察", "history", title == "洞察"),
        )),
        ("自适应", (
            ("/learning", "学习确认", "memory", title == "学习确认"),
            ("/inventory", "库存", "foods", title == "库存"),
            ("/profile", "目标与边界", "settings", title in {"目标与边界", "初始化"}),
        )),
        ("高级工具", (
            ("/daily", "建议生成", "advice", title in {"今日建议与明日菜单", "每日复盘"}),
            ("/tasks/photo", "照片任务", "photo", title in {"上传食物照片", "任务详情"}),
            ("/tasks/material", "原材料", "material", title == "原材料分析"),
            ("/tasks", "全部任务", "tasks", title == "任务列表"),
            ("/ai", "API 接入", "settings", title == "API 接入"),
            ("/foods", "食品营养库", "foods", title in {"食品营养库", "新增食品", "编辑食品"}),
            ("/history", "历史建议", "history", title == "历史建议"),
            ("/overview", "记录与记忆", "memory", title == "记录与记忆"),
        )),
    )
    nav_sections = []
    for label, items in nav_groups:
        links = []
        for href, item_label, icon_name, current in items:
            current_attr = ' aria-current="page"' if current else ""
            links.append(
                f'<a class="nav-link" href="{href}"{current_attr} title="{esc(item_label)}">'
                f'{icon(icon_name)}<span class="nav-label">{esc(item_label)}</span></a>'
            )
        nav_sections.append(
            f'<section class="nav-group" aria-label="{esc(label)}"><p class="nav-group-label">{esc(label)}</p>{"".join(links)}</section>'
        )
    page_titles = {
        "今天": "今天", "执行计划": "执行计划", "记录": "记录", "洞察": "洞察",
        "学习确认": "学习确认", "库存": "库存", "目标与边界": "目标与边界", "初始化": "初始化",
        "今日建议与明日菜单": "今日建议", "每日复盘": "每日复盘",
        "今日状态": "今日状态", "状态问答": "状态问答", "状态设置": "状态设置",
        "上传食物照片": "照片任务", "原材料分析": "原材料分析", "任务列表": "全部任务",
        "任务详情": "任务详情", "API 接入": "API 接入", "食品营养库": "食品营养库", "新增食品": "新增食品",
        "编辑食品": "编辑食品", "历史建议": "历史建议", "记录与记忆": "记录与记忆",
        "操作失败": "操作失败", "未找到": "未找到",
    }
    top_action = "" if title in {"记录", "今日状态", "状态问答", "状态设置", "初始化"} else (
        f'<a class="button" href="/capture" aria-label="记录真实情况" title="记录真实情况">{icon("checkin")}记录</a>'
    )
    date_label = f"{today.month}月{today.day}日 周{'一二三四五六日'[today.weekday()]}"
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · MealCircuit</title><link rel="icon" href="/assets/ui/favicon.svg" type="image/svg+xml"><script src="/assets/ui/theme-init.js?v=20260711a"></script><link rel="stylesheet" href="/assets/ui/app.css?v=20260711a"><script src="/assets/ui/app.js?v=20260711a" defer></script></head><body>
    <a class="skip-link" href="#main-content">跳到主要内容</a>
    <div class="app-shell"><aside class="app-sidebar" id="app-sidebar" aria-label="主导航"><a class="sidebar-brand" href="/">MealCircuit</a><nav class="sidebar-nav">{"".join(nav_sections)}</nav><div class="sidebar-footer"><button class="icon-button" type="button" data-nav-collapse aria-label="收起侧栏" title="收起侧栏">{icon("collapse")}</button></div></aside>
    <button class="nav-scrim" type="button" data-nav-close aria-label="关闭导航"></button>
    <header class="app-topbar"><div class="topbar-start"><button class="icon-button mobile-menu" type="button" data-nav-open aria-controls="app-sidebar" aria-expanded="false" aria-label="打开导航">{icon("menu")}</button><p class="topbar-title">{esc(page_titles.get(title, title))}</p></div><div class="topbar-end"><button class="icon-button theme-toggle" type="button" data-theme-toggle aria-label="切换到浅色主题" title="切换到浅色主题" hidden><span class="icon icon-sun theme-target-light" aria-hidden="true"></span><span class="icon icon-moon theme-target-dark" aria-hidden="true"></span></button><span class="utility muted">{date_label}</span><span class="local-status">{icon("local")}<span>仅存于本机</span></span>{top_action}</div></header>
    <main class="app-content" id="main-content" tabindex="-1">{body}</main></div></body></html>"""
    return page.encode("utf-8")


def task_table(tasks: list[dict]) -> str:
    if not tasks:
        return '<p class="muted">暂无任务。</p>'
    rows = "".join(
        f'<tr><td><a href="/tasks/{esc(t["id"])}">{esc(t["id"])}</a></td><td>{"照片识别" if t["type"] == "photo" else "原材料分析"}</td><td><span class="status {esc(t["status"])}">{esc(t["status"])}</span></td><td>{esc(t["created_at"])}</td></tr>'
        for t in tasks
    )
    return f'<div class="table-scroll" tabindex="0" role="region" aria-label="任务列表"><table><thead><tr><th scope="col">任务</th><th scope="col">类型</th><th scope="col">状态</th><th scope="col">创建时间</th></tr></thead><tbody>{rows}</tbody></table></div>'


def render_task_input(task: dict) -> str:
    history = task.get("input_history") or []
    history_items = "".join(
        f'<li><p class="muted small">版本 {esc(item["version"])} · {esc(item["archived_at"])}</p>'
        f'<pre>{esc(item["input_text"])}</pre></li>'
        for item in history
    )
    history_block = (
        f'<details><summary>输入修改历史（{len(history)}）</summary><ol class="structured-list">{history_items}</ol></details>'
        if history else '<p class="muted small">尚无输入修改历史。</p>'
    )
    if task["status"] == "pending":
        label = "现有食材及粗略数量" if task["type"] == "material" else "照片补充说明"
        required = " required maxlength=\"10000\"" if task["type"] == "material" else ""
        return (
            '<h2>用户输入</h2><p class="muted small">任务处理前可修改；保存时会保留当前版本。</p>'
            f'<form method="post" action="/tasks/{esc(task["id"])}/input">'
            f'<input type="hidden" name="expected_version" value="{esc(task["input_version"])}">'
            f'<label for="task-input">{label}</label>'
            f'<textarea id="task-input" name="text"{required}>{esc(task["original_input"])}</textarea>'
            '<div class="form-actions"><button type="submit">保存输入</button></div></form>'
            f'{history_block}'
        )
    current = f'<pre>{esc(task["original_input"])}</pre>' if task["original_input"] else '<p class="muted">无补充说明</p>'
    return (
        '<h2>用户输入</h2><p class="muted small">任务完成后输入已锁定；事实变化请追加用户校正。</p>'
        f'{current}{history_block}'
    )


def render_task_generate_controls(task_id: str) -> str:
    return (
        f'<form method="post" action="/tasks/{esc(task_id)}/generate">'
        '<div class="form-actions"><button type="submit">用 API Key 生成</button></div></form>'
    )


def render_daily_generate_controls(review_date: str) -> str:
    return (
        f'<form method="post" action="/reviews/{esc(review_date)}/generate">'
        '<div class="form-actions"><button type="submit">用 API Key 生成今日建议</button></div></form>'
    )


def render_review_cards(reviews: list[dict]) -> str:
    status_labels = {
        "stable": "稳定", "observe": "观察", "adjust": "需调整", "risk": "风险",
        "pending": "待生成",
    }
    cards = []
    for review in reviews:
        result = review.get("result_json") or {}
        completed = review.get("status") == "completed" and bool(result)
        signal = result.get("system_status", "pending") if completed else "pending"
        summary = result.get("one_line_review") or "记录已保存，等待生成当日建议。"
        advice_items = result.get("core_advice") or []
        advice = advice_items[0] if advice_items else "生成后将在这里显示最重要的一条建议。"
        menu = result.get("tomorrow_menu") or {}
        menu_date = menu.get("date")
        meta = f'次日菜单 · {esc(menu_date)}' if menu_date else "等待复盘"
        review_date = esc(review["review_date"])
        cards.append(
            f'<article class="review-card" data-status="{esc(signal)}">'
            f'<header class="review-card__top"><time class="review-date" datetime="{review_date}">{review_date}</time>'
            f'<span class="review-signal">{esc(status_labels.get(signal, signal))}</span></header>'
            f'<p class="review-summary">{esc(summary)}</p><p class="review-advice">{esc(advice)}</p>'
            f'<footer class="review-card__footer"><span class="review-meta">{meta}</span>'
            f'<a class="review-link" href="/reviews/{review_date}">打开复盘 {icon("chevron")}</a></footer></article>'
        )
    return '<div class="review-grid">' + ("".join(cards) or '<p class="review-empty">还没有历史建议。保存每日记录后，复盘会按日期出现在这里。</p>') + "</div>"


def render_checkin_callout(checkin_date: str) -> str:
    state = service.get_checkin_state(checkin_date)
    coverage = state["coverage"]
    due, handled = coverage["due"], coverage["handled"]
    label = "今日状态已补全" if due == handled else f"今日状态 {handled}/{due}"
    action = "查看或更新" if handled else "开始记录"
    return (
        f'<section class="card"><div class="section-header"><div><p class="eyebrow">Daily signals</p>'
        f'<h2>{esc(label)}</h2><p class="muted">用点击补充体重、训练、饥饿饱腹、睡眠和肠胃反应。</p></div>'
        f'<a class="button secondary" href="/check-ins/{esc(checkin_date)}">{action}</a></div></section>'
    )


def _trend_cell(module: dict | None, kind: str) -> str:
    if module is None:
        return '<span class="trend-cell" data-state="missing" title="缺失">·</span>'
    if module["status"] == "skipped":
        return '<span class="trend-cell" data-state="skipped" title="用户跳过">—</span>'
    if kind == "weight":
        if module.get("measured") != "yes":
            return '<span class="trend-cell" data-state="unmeasured" title="当天未测体重">未</span>'
        value = module.get("weight_kg")
        return f'<span class="trend-cell" data-state="recorded" title="体重 {esc(value)} kg">{esc(value)}</span>'
    if kind == "training":
        shown = "训" if module.get("trained") == "yes" else "休"
        return f'<span class="trend-cell" data-state="recorded" title="{esc(module["summary"])}">{shown}</span>'
    level = module.get("hunger_level")
    if level is None:
        return '<span class="trend-cell" data-state="recorded" title="饥饿感已记录">记</span>'
    return f'<span class="trend-cell" data-state="recorded" data-level="{level}" title="{esc(module["summary"])}">{level}</span>'


def render_dashboard(snapshot: dict) -> str:
    daily = snapshot["daily"]
    if daily["status"] == "completed":
        conclusion = snapshot["conclusion"] or snapshot["core_advice"][0]
        decision_meta = snapshot["core_advice"][0] if snapshot["core_advice"] else "复盘已完成"
    elif daily["status"] == "pending":
        conclusion = "记录已保存，等待生成今日判断"
        decision_meta = "处理待办后，这里会显示核心建议与次日菜单。"
    else:
        conclusion = "今天尚未形成判断"
        decision_meta = "先记录今日状态或饮食，系统会建立可追溯的复盘待办。"

    dates = "".join(
        f'<span class="trend-date">{date.fromisoformat(item["date"]).month}.{date.fromisoformat(item["date"]).day}</span>'
        for item in snapshot["trend"]
    )
    rows = []
    for key, label in (("weight", "体重"), ("training", "训练"), ("hunger", "饥饿")):
        cells = "".join(_trend_cell(item["modules"].get(key), key) for item in snapshot["trend"])
        rows.append(f'<div class="trend-row"><span class="trend-label">{label}</span>{cells}</div>')
    trend = (
        '<div class="trend" role="img" aria-label="近14天已发布状态趋势。点表示缺失，虚线表示用户跳过。">'
        '<div class="trend-head"><h2>14天趋势</h2><div class="trend-legend">'
        '<span><i class="trend-key recorded"></i>已记录</span><span><i class="trend-key skipped"></i>跳过或缺失</span>'
        f'</div></div><div class="trend-days">{dates}</div>{"".join(rows)}</div>'
    )

    state_labels = {"not_started": "待填写", "in_progress": "有草稿", "completed": "已记录", "skipped": "已跳过"}
    module_rows = []
    for module in snapshot["checkin"]["modules"]:
        if not module["enabled"]:
            continue
        display_state = "in_progress" if module["has_draft"] else module["status"]
        summary = module["summary"] or module["description"]
        module_rows.append(
            '<div class="module-row"><div class="module-main">'
            f'<div class="module-name">{esc(module["label"])}</div><div class="module-summary">{esc(summary)}</div></div>'
            f'<span class="module-state {esc(display_state)}"><i class="state-dot"></i>{esc(state_labels.get(display_state, display_state))}</span></div>'
        )

    menu = snapshot["tomorrow_menu"]
    if menu:
        meal_items = []
        for meal in menu["meals"]:
            foods = "、".join(meal["foods"])
            meal_items.append(
                f'<li class="meal-item"><span class="meal-node" aria-hidden="true"></span><div class="meal-title">'
                f'<span>{esc(meal["name"])}</span><span class="meal-time">次日</span></div>'
                f'<p class="meal-foods">{esc(foods)}</p></li>'
            )
        snack = menu["conditional_snack"]
        meal_items.append(
            '<li class="meal-item"><span class="meal-node" aria-hidden="true"></span>'
            f'<div class="meal-title"><span>条件加餐</span><span class="meal-time">按条件</span></div>'
            f'<p class="meal-foods">{esc(snack["condition"])} · {esc("、".join(snack["options"]))}</p></li>'
        )
        menu_html = f'<ol class="meal-timeline">{"".join(meal_items)}</ol>'
    else:
        menu_html = '<div class="empty-state">完成当日复盘后，这里会显示真实的次日菜单。</div>'

    if snapshot["queue"]:
        queue_rows = "".join(
            f'<tr><td>{esc(item["label"])}</td><td>{esc(item["evidence"])}</td>'
            f'<td><span class="status pending">待处理</span></td><td><a class="queue-link" href="{esc(item["href"])}">打开{icon("chevron")}</a></td></tr>'
            for item in snapshot["queue"][:8]
        )
        queue_html = (
            '<div class="table-scroll" tabindex="0" role="region" aria-label="处理队列"><table class="queue-table"><thead>'
            '<tr><th scope="col">任务类型</th><th scope="col">关联证据</th><th scope="col">状态</th><th scope="col">下一步</th></tr>'
            f'</thead><tbody>{queue_rows}</tbody></table></div>'
        )
    else:
        queue_html = '<div class="empty-state">当前没有待处理任务。</div>'

    coverage = snapshot["checkin"]["coverage"]
    return (
        '<div class="dashboard-grid">'
        f'<section class="panel decision-panel"><p class="eyebrow">今日结论</p><h1 class="decision-copy">{esc(conclusion)}</h1>'
        f'<p class="decision-meta">{esc(decision_meta)}</p>{trend}<p class="utility muted">状态覆盖 {coverage["handled"]} / {coverage["due"]}</p></section>'
        f'<section class="panel"><div class="panel-title"><h2>今日状态</h2><a href="/check-ins/{esc(snapshot["date"])}">查看</a></div><div class="module-list">{"".join(module_rows)}</div></section>'
        f'<section class="panel"><div class="panel-title"><h2>明日计划</h2><a href="/daily">完整建议</a></div>{menu_html}</section>'
        '</div>'
        f'<section class="panel queue-panel"><div class="panel-title"><h2>处理队列</h2><span class="queue-count">待处理 {len(snapshot["queue"])}</span></div>{queue_html}</section>'
    )


def render_checkin_hub(checkin_date: str) -> str:
    state = service.get_checkin_state(checkin_date)
    daily = service.daily_state(checkin_date)
    coverage = state["coverage"]
    due, handled = coverage["due"], coverage["handled"]
    percent = round(handled / due * 100) if due else 100
    cards = []
    status_labels = {
        "not_started": "待填写", "in_progress": "进行中", "completed": "已完成", "skipped": "已跳过",
    }
    for module in state["modules"]:
        if not module["enabled"]:
            continue
        display_state = "in_progress" if module["has_draft"] else module["status"]
        if module["has_draft"] and module["version"]:
            state_text = "有未提交修改"
        else:
            state_text = status_labels.get(display_state, display_state)
        frequency = "按需记录" if module["frequency"] == "optional" else "每日"
        summary = module["summary"] or module["description"]
        cards.append(
            f'<li class="signal-item" data-state="{esc(display_state)}"><span class="signal-node" aria-hidden="true"></span>'
            f'<a class="signal-card" href="/check-ins/{esc(checkin_date)}/{esc(module["module_key"])}">'
            f'<span><h2>{esc(module["label"])}</h2><p>{esc(summary)}</p></span>'
            f'<span class="signal-state">{esc(state_text)} · {frequency}</span></a></li>'
        )
    empty = '<p class="card muted">所有模块都已隐藏。可进入设置重新启用。</p>'
    review_link = (
        f'<a class="button secondary" href="/reviews/{esc(checkin_date)}">查看当日复盘</a>'
        if daily["review"] is not None else ""
    )
    return (
        f'<section class="checkin-hero"><div><p class="eyebrow">Daily signal circuit</p>'
        f'<h1>每日状态 · <span class="checkin-date">{esc(checkin_date)}</span></h1><p class="muted">每次只回答一个问题，途中退出也会保留草稿。</p></div>'
        f'<div class="checkin-progress" role="status"><strong>{handled}/{due}</strong><span>每日模块已处理</span>'
        f'<div class="progress-track" aria-hidden="true"><span class="progress-fill" style="width:{percent}%"></span></div></div></section>'
        + ('<ol class="signal-list">' + "".join(cards) + "</ol>" if cards else empty)
        + f'<div class="actions"><a class="button secondary" href="/check-ins/settings">调整模块</a>'
        f'{review_link}</div>'
    )


def _question_value(module: dict, question_id: str):
    return (module.get("active_answers") or {}).get(question_id)


def render_checkin_question(checkin_date: str, module_key: str, requested_question: str | None = None) -> str:
    module = service.get_checkin_module(checkin_date, module_key)
    definition = checkins.module_definition(module_key)
    active = module["active_answers"]
    questions = checkins.applicable_questions(module_key, active)
    if requested_question:
        question = checkins.question_definition(module_key, requested_question, active)
    else:
        question = module.get("next_question") or questions[0]
    question_ids = [item["id"] for item in questions]
    index = question_ids.index(question["id"])
    previous = question_ids[index - 1] if index else None
    common = (
        f'<input type="hidden" name="question_id" value="{esc(question["id"])}">'
        f'<input type="hidden" name="expected_version" value="{esc(module["version"])}">'
    )
    current = _question_value(module, question["id"])
    if question["type"] == "single" and not question.get("allow_other_text"):
        options = []
        for option in question["options"]:
            selected = ' <span class="muted">· 当前答案</span>' if current == option["value"] else ""
            options.append(
                f'<form method="post" action="/check-ins/{esc(checkin_date)}/{esc(module_key)}/answer">{common}'
                f'<button class="option-button" type="submit" name="value" value="{esc(option["value"])}">'
                f'{esc(option["label"])}{selected}</button></form>'
            )
        control = '<div class="option-list">' + "".join(options) + "</div>"
    elif question["type"] in {"single", "multi"}:
        if isinstance(current, dict):
            other_text = current.get("other_text", "")
            current_value = current.get("values") if question["type"] == "multi" else current.get("value")
        else:
            other_text = ""
            current_value = current
        selected = set(current_value or []) if question["type"] == "multi" else {current_value}
        choices = []
        for option in question["options"]:
            checked = " checked" if option["value"] in selected else ""
            field_id = f'{module_key}-{question["id"]}-{option["value"]}'
            input_type = "checkbox" if question["type"] == "multi" else "radio"
            choices.append(
                f'<div class="choice-row"><input id="{esc(field_id)}" type="{input_type}" name="value" '
                f'value="{esc(option["value"])}"{checked}><label for="{esc(field_id)}">{esc(option["label"])}</label></div>'
            )
        other = (
            f'<div class="duration-exact"><label for="other-text">其他说明</label>'
            f'<input id="other-text" name="other_text" maxlength="200" value="{esc(other_text)}" '
            f'placeholder="选择“其他”时填写"></div>' if question.get("allow_other_text") else ""
        )
        legend = "可多选" if question["type"] == "multi" else "请选择一项"
        control = (
            f'<form method="post" action="/check-ins/{esc(checkin_date)}/{esc(module_key)}/answer">{common}'
            f'<fieldset class="option-list"><legend class="small muted">{legend}</legend>{"".join(choices)}</fieldset>{other}'
            f'<div class="quiz-actions"><span></span><button type="submit">下一题</button></div></form>'
        )
    elif question["type"] == "number":
        value = "" if current is None else esc(current)
        control = (
            f'<form method="post" action="/check-ins/{esc(checkin_date)}/{esc(module_key)}/answer">{common}'
            f'<label for="question-number">体重（kg）</label><input id="question-number" name="value" type="number" '
            f'min="{esc(question["min"])}" max="{esc(question["max"])}" step="{esc(question["step"])}" value="{value}" required autofocus>'
            f'<div class="quiz-actions"><span></span><button type="submit">下一题</button></div></form>'
        )
    else:
        exact = current if isinstance(current, (int, float)) else ""
        radios = []
        for option in question["options"]:
            checked = " checked" if current == option["value"] else ""
            field_id = f'sleep-{option["value"]}'
            radios.append(
                f'<div class="choice-row"><input id="{esc(field_id)}" type="radio" name="value" '
                f'value="{esc(option["value"])}"{checked}><label for="{esc(field_id)}">{esc(option["label"])}</label></div>'
            )
        control = (
            f'<form method="post" action="/check-ins/{esc(checkin_date)}/{esc(module_key)}/answer">{common}'
            f'<fieldset class="option-list"><legend class="small muted">选择区间</legend>{"".join(radios)}</fieldset>'
            f'<div class="duration-exact"><label for="sleep-exact">或者精确填写小时数</label>'
            f'<input id="sleep-exact" type="number" name="exact_value" min="0" max="24" step="0.1" value="{esc(exact)}"></div>'
            f'<div class="quiz-actions"><span></span><button type="submit">下一题</button></div></form>'
        )
    back_href = f'/check-ins/{checkin_date}/{module_key}?q={previous}' if previous else f'/check-ins/{checkin_date}'
    severe = '<p class="danger-note" role="note">严重或持续症状需要停止自行加压并寻求医疗判断；这里仅记录信号，不做诊断。</p>' if module_key == "gut" and active.get("severity") == "severe" else ""
    history = ""
    if module.get("history"):
        items = "".join(
            f'<li>版本 {esc(item["version"])} · {esc(item["status"])} · {esc(item["archived_at"])}</li>'
            for item in module["history"]
        )
        history = f'<details><summary>查看旧版本</summary><ul class="structured-list">{items}</ul></details>'
    return (
        f'<div class="quiz-shell"><section class="quiz-card"><div class="quiz-top">'
        f'<p class="quiz-step">{esc(definition["label"])} · {index + 1}/{esc(definition["max_steps"])}</p>'
        f'<form class="skip-form" method="post" action="/check-ins/{esc(checkin_date)}/{esc(module_key)}/skip">'
        f'<input type="hidden" name="expected_version" value="{esc(module["version"])}">'
        f'<button class="skip-link-button" type="submit">跳过本模块</button></form></div>'
        f'<h1 class="question-title">{esc(question["label"])}</h1>{control}{severe}'
        f'<div class="quiz-actions"><a class="back-link" href="{esc(back_href)}">返回</a>'
        + (f'<form method="post" action="/check-ins/{esc(checkin_date)}/{esc(module_key)}/discard-draft">'
           f'<input type="hidden" name="expected_version" value="{esc(module["version"])}">'
           f'<button class="secondary" type="submit">放弃草稿</button></form>' if module["has_draft"] else '<span></span>')
        + f'</div></section>{history}</div>'
    )


def render_checkin_settings() -> str:
    settings = service.checkin_module_settings()
    rows = []
    for index, setting in enumerate(settings):
        definition = checkins.module_definition(setting["module_key"])
        key = setting["module_key"]
        checked = " checked" if setting["enabled"] else ""
        daily = " selected" if setting["frequency"] == "daily" else ""
        optional = " selected" if setting["frequency"] == "optional" else ""
        move = []
        if index:
            move.append(f'<button class="secondary" name="move" value="{esc(key)}:up" aria-label="上移{esc(definition["label"])}" title="上移">{icon("up")}</button>')
        if index < len(settings) - 1:
            move.append(f'<button class="secondary" name="move" value="{esc(key)}:down" aria-label="下移{esc(definition["label"])}" title="下移">{icon("down")}</button>')
        rows.append(
            f'<div class="settings-row"><label><input type="checkbox" name="enabled_{esc(key)}" value="1"{checked}> '
            f'{esc(definition["label"])}</label><span class="muted small">{esc(definition["description"])}</span>'
            f'<select name="frequency_{esc(key)}" aria-label="{esc(definition["label"])}询问频率">'
            f'<option value="daily"{daily}>每日</option><option value="optional"{optional}>按需</option></select>'
            f'<div class="move-actions">{"".join(move)}</div></div>'
        )
    return (
        '<section class="card"><div class="section-header"><div><p class="eyebrow">Signal preferences</p>'
        '<h1>每日状态设置</h1><p class="muted">隐藏、排序或把模块改为按需记录。</p></div>'
        f'<a class="button secondary" href="/check-ins/{date.today().isoformat()}">返回今日状态</a></div>'
        f'<form method="post" action="/check-ins/settings"><div class="settings-list">{"".join(rows)}</div>'
        '<div class="form-actions"><button type="submit">保存设置</button></div></form></section>'
    )


def render_ai_settings() -> str:
    status = ai.ai_status()
    provider = status.get("provider") or ""
    model_value = esc(os.environ.get("MEALCIRCUIT_AI_MODEL", ""))
    provider_options = "".join(
        f'<option value="{name}"{" selected" if provider == name else ""}>{label}</option>'
        for name, label in (
            ("deepseek", "DeepSeek"),
            ("openai", "OpenAI"),
            ("anthropic", "Anthropic"),
        )
    )
    state = "已启用" if status["provider_valid"] and status["model_configured"] and status["key_configured"] else "未启用"
    configured = (
        f'<p><span class="status completed">{state}</span> · provider={esc(provider or "未设置")} · '
        f'model={"已设置" if status["model_configured"] else "未设置"} · '
        f'key={esc(status.get("key_name") or "未设置")} {"已设置" if status["key_configured"] else "未设置"}</p>'
    )
    disable = (
        '<form method="post" action="/ai/disable">'
        '<div class="form-actions"><button class="secondary" type="submit">关闭本次运行的 API Key 模式</button></div></form>'
        if provider or status["model_configured"] or status["key_configured"] else ""
    )
    return f'''<section class="card"><div class="section-header"><div><p class="eyebrow">Runtime AI mode</p>
<h1>API Key 接入</h1><p class="muted">只在当前服务进程内启用；不写入数据库、配置文件或页面。</p></div></div>{configured}
<form method="post" action="/ai/configure">
<label for="ai-provider">供应商</label><select id="ai-provider" name="provider">{provider_options}</select>
<label for="ai-model">模型名</label><input id="ai-model" name="model" value="{model_value}" placeholder="例如 deepseek-v4-flash" required>
<label for="ai-key">API Key</label><input id="ai-key" name="api_key" type="password" autocomplete="off" required>
<div class="grid two"><div><label for="ai-timeout">超时秒数</label><input id="ai-timeout" name="timeout_seconds" type="number" min="1" value="{esc(status["timeout_seconds"])}"></div>
<div><label for="ai-max-output">最大输出 token</label><input id="ai-max-output" name="max_output_tokens" type="number" min="1" value="{esc(status["max_output_tokens"])}"></div></div>
<div class="form-actions"><button type="submit">启用本次运行的 API Key 模式</button></div></form>{disable}</section>'''


def food_form(item: dict | None = None) -> str:
    item = item or {}
    action = f'/foods/{esc(item["id"])}' if item.get("id") else "/foods"
    selected100 = "selected" if item.get("basis", "100g") == "100g" else ""
    selected_serving = "selected" if item.get("basis") == "serving" else ""
    category_options = {
        "protein": "蛋白质", "staple": "主食", "vegetable": "蔬菜", "fruit": "水果",
        "fat": "脂肪", "snack": "零食", "flavor": "调味", "other": "其他",
    }
    priority_options = {"high": "高优先级", "normal": "普通", "low": "低优先级", "excluded": "不用于菜单"}
    def val(name: str) -> str: return esc(item.get(name, ""))
    categories = "".join(
        f'<option value="{key}" {"selected" if item.get("category", "other") == key else ""}>{label}</option>'
        for key, label in category_options.items()
    )
    priorities = "".join(
        f'<option value="{key}" {"selected" if item.get("menu_priority", "normal") == key else ""}>{label}</option>'
        for key, label in priority_options.items()
    )
    return f"""
    <form method="post" action="{action}"><input type="hidden" name="source_key" value="{val('source_key')}">
    <fieldset class="form-section"><legend>基本信息</legend><div class="row"><div><label for="food-name">名称 *</label><input id="food-name" name="name" required value="{val('name')}"></div><div><label for="food-brand">品牌</label><input id="food-brand" name="brand" value="{val('brand')}"></div></div>
    <div class="row"><div><label for="food-basis">营养基准 *</label><select id="food-basis" name="basis"><option value="100g" {selected100}>每 100g</option><option value="serving" {selected_serving}>每份</option></select></div><div><label for="food-serving">份量单位（按份时必填）</label><input id="food-serving" name="serving_unit" placeholder="例如：1 片 / 1 包（35g）" value="{val('serving_unit')}"></div></div></fieldset>
    <fieldset class="form-section"><legend>营养数据</legend><div class="row"><div><label for="food-energy">能量 kcal</label><input id="food-energy" type="number" min="0" step="any" name="energy_kcal" value="{val('energy_kcal')}"></div><div><label for="food-protein">蛋白质 g</label><input id="food-protein" type="number" min="0" step="any" name="protein_g" value="{val('protein_g')}"></div><div><label for="food-carbs">碳水 g</label><input id="food-carbs" type="number" min="0" step="any" name="carbs_g" value="{val('carbs_g')}"></div><div><label for="food-fat">脂肪 g</label><input id="food-fat" type="number" min="0" step="any" name="fat_g" value="{val('fat_g')}"></div><div><label for="food-fiber">膳食纤维 g</label><input id="food-fiber" type="number" min="0" step="any" name="fiber_g" value="{val('fiber_g')}"></div><div><label for="food-sodium">钠 mg</label><input id="food-sodium" type="number" min="0" step="any" name="sodium_mg" value="{val('sodium_mg')}"></div></div></fieldset>
    <fieldset class="form-section"><legend>菜单规则</legend><div class="row"><div><label for="food-category">食品类别</label><select id="food-category" name="category">{categories}</select></div><div><label for="food-priority">菜单优先级</label><select id="food-priority" name="menu_priority">{priorities}</select></div></div>
    <label for="food-default-portion">默认份量</label><input id="food-default-portion" name="default_portion" placeholder="例如：50–100g / 1包40g" value="{val('default_portion')}"><label for="food-usage-rule">菜单使用条件</label><textarea id="food-usage-rule" name="usage_rule">{val('usage_rule')}</textarea></fieldset>
    <fieldset class="form-section"><legend>来源与备注</legend><label for="food-source">来源链接</label><input id="food-source" type="url" name="source_url" value="{val('source_url')}"><label for="food-photo-path">包装照片路径</label><input id="food-photo-path" name="package_photo_path" placeholder="可记录本机路径" value="{val('package_photo_path')}"><label for="food-notes">备注</label><textarea id="food-notes" name="notes">{val('notes')}</textarea></fieldset>
    <div class="actions form-actions"><button type="submit">保存</button><a class="button secondary" href="/foods">取消</a></div></form>"""


def render_list(items: list, empty_text: str = "暂无") -> str:
    if not items:
        return f'<p class="muted">{esc(empty_text)}</p>'
    return '<ul class="structured-list">' + "".join(f"<li>{esc(item)}</li>" for item in items) + "</ul>"


def render_nutrition(value: dict) -> str:
    labels = (
        ("energy_kcal", "能量", "kcal"),
        ("protein_g", "蛋白质", "g"),
        ("carbs_g", "碳水", "g"),
        ("fat_g", "脂肪", "g"),
    )
    cells = []
    for key, label, unit in labels:
        interval = value.get(key)
        shown = "未知" if interval is None else f"{esc(interval[0])}–{esc(interval[1])} {unit}"
        cells.append(f"<tr><th>{label}</th><td>{shown}</td></tr>")
    return '<table class="nutrition"><tbody>' + "".join(cells) + "</tbody></table>"


def render_result(task_type: str, result: dict) -> str:
    summary = f'<p class="notice card"><strong>综合判断：</strong>{esc(result["summary"])}</p>'
    if task_type == "photo":
        candidates = []
        for candidate in result["candidates"]:
            confidence = round(float(candidate["confidence"]) * 100)
            candidates.append(
                f'<article class="card"><h3>{esc(candidate["name"])}</h3>'
                f'<p><strong>份量：</strong>{esc(candidate["portion_range"])}</p>'
                f'<p><strong>置信度：</strong>{confidence}%</p>'
                f'{render_nutrition(candidate["nutrition"])}</article>'
            )
        friendly = (
            summary + '<h3>候选食物</h3><div class="grid">' + "".join(candidates) + "</div>"
            + '<h3>未知项</h3>' + render_list(result["unknowns"])
            + '<h3>综合建议</h3>' + render_list(result["advice"])
        )
    else:
        friendly = (
            summary + '<h3>可做组合 / 菜品方向</h3>' + render_list(result["combinations"])
            + '<div class="grid"><section class="card"><h3>整批营养估算</h3>'
            + render_nutrition(result["batch_nutrition"])
            + '</section><section class="card"><h3>单份营养估算</h3>'
            + render_nutrition(result["per_serving_nutrition"])
            + '</section></div><h3>当前缺口</h3>' + render_list(result["gaps"])
            + '<h3>肠胃 / 执行风险</h3>' + render_list(result["risks"])
            + '<h3>最小调整</h3>' + render_list(result["minimal_adjustments"])
        )
    raw = esc(json.dumps(result, ensure_ascii=False, indent=2))
    return friendly + f'<details><summary>查看原始 JSON</summary><pre>{raw}</pre></details>'


EQUIPMENT_LABELS = {
    "rice_cooker": "电饭煲", "stovetop_pan": "炒锅", "stovetop_pot": "汤锅", "refrigerator": "冰箱",
}
MEAL_MODE_LABELS = {"quick_assembly": "快速组装", "eat_out": "食堂 / 外食", "home_cook": "在家下厨"}


def render_home_cooking_menu(menu: dict) -> str:
    dinner = next((meal for meal in menu.get("meals", []) if meal.get("name") == "晚餐"), {})
    recipe = dinner.get("recipe_card")
    if not recipe:
        return ""
    cookware = "、".join(EQUIPMENT_LABELS.get(item, item) for item in recipe["cookware"])
    ingredients = render_list([
        f'{item["name"]}：{item["amount"]}；{item["prep"]}' for item in recipe["ingredients"]
    ])
    seasonings = render_list([
        f'{item["name"]}：{item["amount"]}；{item["timing"]}' for item in recipe["seasonings"]
    ])
    steps = "".join(
        f'<li><strong>{esc(item["instruction"])}</strong>'
        f'<span class="step-meta">{esc(item["heat"])} · {esc(item["minutes"])} 分钟 · '
        f'完成标志：{esc(item["done_signal"])}</span></li>'
        for item in recipe["steps"]
    )
    shopping = "".join(
        f'<div class="menu-fact"><p><strong>{esc(item["name"])}</strong> · {esc(item["amount"])}'
        f'{" · 必买" if item["required"] else " · 缺少时再买"}</p>'
        f'<p>{esc(item["purpose"])}</p><p class="muted small">挑选：{esc(item["selection_guide"])}；'
        f'保存：{esc(item["storage"])}</p></div>'
        for item in menu["shopping_list"]
    )
    online = "".join(
        f'<div class="menu-fact"><p><strong>{esc(item["category"])}</strong> · {esc(item["package_size"])}</p>'
        f'<p>搜索：{esc(" / ".join(item["search_keywords"]))}</p>'
        f'<p class="muted small">筛选：{esc("；".join(item["selection_criteria"]))}<br>'
        f'适用：{esc("、".join(item["pairs_with"]))}<br>跳过：{esc(item["skip_if"])}</p></div>'
        for item in menu["online_options"]
    )
    reuse = "".join(
        f'<div class="menu-fact"><p><strong>{esc(item["ingredient"])}</strong></p>'
        f'<p>明日：{esc(item["tomorrow_use"])}</p>'
        f'<p class="muted small">后续：{esc("；".join("{} {}".format(use["date"], use["use"]) for use in item["later_uses"]))}<br>'
        f'保存：{esc(item["storage"])}</p></div>'
        for item in menu["reuse_plan"]["items"]
    )
    online_section = (
        '<section class="menu-section"><h3>可选网购组件</h3><div class="menu-facts">' + online + "</div></section>"
        if online else ""
    )
    return (
        '<section class="menu-section"><p class="eyebrow">BEGINNER DINNER</p>'
        f'<h2>{esc(recipe["title"])}</h2><div class="recipe-meta">'
        f'<span>1 人份</span><span>主动 {esc(recipe["active_minutes"])} 分钟</span>'
        f'<span>总计 {esc(recipe["total_minutes"])} 分钟</span><span>{esc(cookware)}</span></div>'
        '<div class="recipe-columns"><div><h3>食材</h3>' + ingredients
        + '<h3>调味</h3>' + seasonings + '</div><div><h3>按顺序操作</h3><ol class="recipe-steps">'
        + steps + '</ol></div></div><h3>失败补救</h3>' + render_list(recipe["failure_rescue"])
        + f'<p><strong>清洁成本：</strong>{esc(recipe["cleanup"])}</p>'
        + f'<p><strong>肠胃降级：</strong>{esc(recipe["gut_fallback"])}</p></section>'
        + '<section class="menu-section"><h3>明日采购清单</h3><div class="menu-facts">' + shopping + "</div></section>"
        + online_section
        + f'<section class="menu-section"><h3>{esc(menu["reuse_plan"]["horizon_days"])} 日食材复用方向</h3>'
        + '<p class="muted">后续用途会继续根据每日状态校准，不是锁死的周计划。</p>'
        + '<div class="menu-facts">' + reuse + "</div></section>"
    )


def render_daily_review_result(result: dict) -> str:
    status_labels = {"stable": "稳定", "observe": "观察", "adjust": "需要调整", "risk": "风险上升"}
    menu = result["tomorrow_menu"]
    meals = []
    for meal in menu["meals"]:
        protein = meal["protein_g"]
        mode = MEAL_MODE_LABELS.get(meal.get("mode"), "")
        mode_html = f'<span class="meal-time">{esc(mode)}</span>' if mode else '<span class="meal-time">次日</span>'
        meals.append(
            '<li class="meal-item"><span class="meal-node" aria-hidden="true"></span>'
            f'<div class="meal-title"><span>{esc(meal["name"])}</span>{mode_html}</div>'
            f'<p class="meal-foods">{esc("、".join(meal["foods"]))}</p>'
            f'<p class="meal-foods">{esc(meal["portion_guidance"])} · 蛋白 {esc(protein[0])}–{esc(protein[1])}g</p>'
            f'<p class="meal-foods">替换：{esc("、".join(meal["substitutions"]) or "无")}</p></li>'
        )
    snack = menu["conditional_snack"]
    priority_decisions = []
    for decision in result["priority_food_decisions"]:
        try:
            food = service.get_food(decision["food_id"])
            food_label = food["name"]
            food_link = f'<a href="/foods/{esc(food["id"])}">{esc(food_label)}</a>'
        except KeyError:
            food_link = esc(decision["food_id"])
        action = "使用" if decision["decision"] == "use" else "跳过"
        priority_decisions.append(f'{food_link}：<strong>{action}</strong> — {esc(decision["reason"])}')
    priority_html = '<ul class="structured-list">' + ''.join(f'<li>{item}</li>' for item in priority_decisions) + '</ul>'
    raw = esc(json.dumps(result, ensure_ascii=False, indent=2))
    return (
        '<div class="report-grid"><section class="panel">'
        + f'<p class="notice card"><strong>今日状态：</strong>{esc(status_labels[result["system_status"]])}</p>'
        + '<div class="report-section"><h2>事实</h2>' + render_list(result["facts"]) + '</div>'
        + '<div class="report-section"><h2>系统推断</h2>' + render_list(result["inferences"]) + '</div>'
        + '<div class="report-section"><h2>核心建议</h2>' + render_list(result["core_advice"]) + '</div>'
        + '<div class="report-section"><h2>不需要调整</h2>' + render_list(result["do_not_adjust"]) + '</div>'
        + '<div class="report-section"><h2>风险信号</h2>' + render_list(result["risk_signals"]) + '</div>'
        + '<div class="report-section"><h2>优先食品裁决</h2>' + priority_html + '</div></section>'
        + f'<aside class="panel report-aside"><p class="eyebrow">{esc(menu["date"])}</p><h2>明日计划</h2>'
        + f'<p class="muted small">{esc(menu["environment"])} · 蛋白目标 {esc(menu["protein_target_g"][0])}–{esc(menu["protein_target_g"][1])}g</p>'
        + '<ol class="meal-timeline">' + ''.join(meals) + '</ol>'
        + '<div class="report-section"><h3>条件加餐</h3>'
        + f'<p>{esc(snack["condition"])}</p>{render_list(snack["options"])}</div>'
        + f'<div class="report-section"><h3>训练日调整</h3><p>{esc(menu["training_adjustment"])}</p></div>'
        + f'<div class="report-section"><h3>肠胃异常调整</h3><p>{esc(menu["gut_adjustment"])}</p></div></aside></div>'
        + render_home_cooking_menu(menu)
        + f'<p class="panel"><strong>一句话复盘：</strong>{esc(result["one_line_review"])}</p>'
        + f'<details><summary>查看原始 JSON</summary><pre>{raw}</pre></details>'
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "MealCircuit/0.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def send_html(self, title: str, body: str, status: int = 200) -> None:
        payload = layout(title, body)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def send_static(self, relative_path: str) -> None:
        root = STATIC_ROOT.resolve()
        try:
            target = (root / urllib.parse.unquote(relative_path)).resolve()
            target.relative_to(root)
        except (ValueError, OSError):
            raise FileNotFoundError(relative_path)
        if not target.is_file():
            raise FileNotFoundError(relative_path)
        payload = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_security_headers()
        self.end_headers()

    def send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; form-action 'self'; frame-ancestors 'none'",
        )

    def validate_origin(self) -> None:
        host_header = self.headers.get("Host", "")
        bound_port = int(self.server.server_address[1])
        origin = self.headers.get("Origin")
        fetch_site = self.headers.get("Sec-Fetch-Site")
        try:
            host_name, host_port = parse_host_endpoint(host_header, bound_port)
        except ValidationError:
            self.log_message(
                "Rejected POST origin Host=%r Origin=%r Sec-Fetch-Site=%r",
                host_header, origin, fetch_site,
            )
            raise
        bound_host = str(self.server.server_address[0])
        allow_remote = bool(getattr(self.server, "allow_remote", False))
        if not allow_remote and not (is_loopback_host(host_name) or host_name == bound_host.lower().rstrip(".")):
            self.log_message(
                "Rejected POST origin Host=%r Origin=%r Sec-Fetch-Site=%r",
                host_header, origin, fetch_site,
            )
            raise ValidationError("Host 请求头不在允许范围")
        if not origin_matches_host(host_name, host_port, origin, fetch_site):
            self.log_message(
                "Rejected POST origin Host=%r Origin=%r Sec-Fetch-Site=%r",
                host_header, origin, fetch_site,
            )
            raise ValidationError("拒绝跨来源写入请求")

    def read_urlencoded(self) -> dict[str, str]:
        values = self.read_urlencoded_values()
        return {key: items[-1] for key, items in values.items()}

    def read_urlencoded_values(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2 * 1024 * 1024:
            raise ValidationError("表单过大")
        raw = self.rfile.read(length).decode("utf-8")
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def read_multipart(self, max_bytes: int | None = None) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise ValidationError("上传必须使用 multipart/form-data")
        length = int(self.headers.get("Content-Length", "0"))
        if length > (max_bytes or service.MAX_UPLOAD_BYTES + 1024 * 1024):
            raise ValidationError("上传内容过大")
        raw = self.rfile.read(length)
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw
        )
        fields: dict[str, str] = {}
        files: dict[str, tuple[str, bytes]] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            data = part.get_payload(decode=True) or b""
            if filename:
                files[name] = (filename, data)
            elif name:
                fields[name] = data.decode(part.get_content_charset() or "utf-8")
        return fields, files

    def render_error(self, error: Exception, status: int = 400) -> None:
        self.send_html("操作失败", f'<section class="card error"><h1>操作失败</h1><p>{esc(error)}</p><a class="button secondary" href="/">返回总览</a></section>', status)

    @staticmethod
    def setup_step_payload(step: str, values: dict[str, list[str]]) -> dict:
        last = lambda key, default="": (values.get(key) or [default])[-1].strip()
        if step == "welcome":
            return {"privacy_ack": last("privacy_ack") == "yes"}
        if step == "goals":
            return {
                "primary_goal": last("primary_goal"),
                "custom_goal_text": last("custom_goal_text"),
                "secondary_goals": values.get("secondary_goals") or [],
                "motivation": last("motivation"),
                "success_metrics": values.get("success_metrics") or [],
                "target_weight_kg": _number_or_none(last("target_weight_kg")),
                "horizon": last("horizon"),
            }
        if step == "baseline":
            return {
                "age_years": int(last("age_years")),
                "height_cm": _number_or_none(last("height_cm")),
                "weight_kg": _number_or_none(last("weight_kg")),
                "physiological_input": last("physiological_input", "unspecified"),
                "activity_level": last("activity_level", "moderate"),
            }
        if step == "safety":
            guidance = {
                "confirmed": last("guidance_confirmed") == "yes",
                "source": last("guidance_source"), "summary": last("guidance_summary"),
                "confirmed_on": last("guidance_confirmed_on"), "valid_until": last("guidance_valid_until"),
            }
            return {
                "life_stage": last("life_stage", "adult"),
                **{key: last(key) == "yes" for key in personalization.OBSERVATION_FLAGS},
                "professional_guidance": guidance if guidance["confirmed"] else None,
            }
        if step == "training":
            return {"types": values.get("types") or [], "frequency_per_week": int(last("frequency_per_week", "0"))}
        if step == "constraints":
            return {
                "meal_environment": last("meal_environment"), "portion_method": last("portion_method"),
                "cooking_time_minutes": int(last("cooking_time_minutes", "25")),
                "question_budget": int(last("question_budget", "2")),
                "equipment": _csv(last("equipment")), "food_exclusions": _csv(last("food_exclusions")),
                "preferences": _csv(last("preferences")),
            }
        raise ValidationError("未知的初始化步骤")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path.rstrip("/") or "/", urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/assets/ui/"):
                self.send_static(path.removeprefix("/assets/ui/"))
            elif path == "/setup":
                self.send_html("初始化", render_setup_start(personalization.onboarding_status()))
            elif path.startswith("/setup/"):
                step = path.split("/")[2]
                if step not in personalization.ONBOARDING_STEPS:
                    raise KeyError(step)
                status = personalization.onboarding_status()
                session = status.get("session")
                if not session:
                    self.redirect("/setup")
                    return
                self.send_html("初始化", render_setup_step(session, step))
            elif path == "/":
                legacy_snapshot = re.sub(
                    r"<h1(\b[^>]*)>", r"<h2\1>", render_dashboard(service.dashboard_snapshot())
                ).replace("</h1>", "</h2>")
                self.send_html(
                    "今天",
                    render_today_workspace(date.today().isoformat())
                    + f'<details class="legacy-dashboard"><summary>查看原有今日总览</summary>{legacy_snapshot}</details>',
                )
            elif path == "/capture":
                self.send_html("记录", render_capture_page())
            elif path.startswith("/plans/"):
                self.send_html("执行计划", render_plan_page(path.split("/")[2]))
            elif path.startswith("/questions/"):
                self.send_html("今天", render_questions_page(path.split("/")[2]))
            elif path == "/learning":
                self.send_html("学习确认", render_learning_page())
            elif path == "/inventory":
                self.send_html("库存", render_inventory_page())
            elif path == "/profile":
                self.send_html("目标与边界", render_profile_page())
            elif path == "/insights":
                self.send_html("洞察", render_insights_page())
            elif path == "/data":
                self.send_html("目标与边界", render_data_page())
            elif path == "/data/export":
                exported = portability.export_bundle()
                target = Path(exported["path"])
                data = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
                self.send_header("Content-Length", str(len(data)))
                self.send_security_headers()
                self.end_headers()
                self.wfile.write(data)
            elif path.startswith("/rescue/"):
                self.send_html("执行计划", render_rescue_page(path.split("/")[2]))
            elif path == "/daily":
                daily = service.daily_state()
                if daily["status"] == "completed":
                    review = daily["review"]
                    content = render_daily_review_result(review["result_json"])
                    state = f'<p><span class="status completed">completed</span> · 版本 {esc(review["result_version"])}</p>'
                elif daily["status"] == "pending":
                    state = '<p><span class="status pending">pending</span></p>'
                    content = (
                        '<p>今日记录已经保存，等待 Agent 生成核心建议和明日菜单。</p>'
                        f'<pre>python -m mealcircuit.agent_cli day-context {esc(daily["date"])} --output context.json\n'
                        f'python -m mealcircuit.agent_cli day-complete {esc(daily["date"])} --file result.json\n'
                        f'python -m mealcircuit.agent_cli day-generate {esc(daily["date"])}</pre>'
                        f'{render_daily_generate_controls(daily["date"])}'
                    )
                else:
                    state = '<p><span class="status pending">尚未记录</span></p>'
                    content = f'''<p>直接记录今天吃了什么和身体状态，保存后系统会创建每日复盘待办。</p><form method="post" action="/records"><input type="hidden" name="record_date" value="{esc(daily["date"])}"><label for="daily-input">今日自然语言记录</label><textarea id="daily-input" name="raw_input" required></textarea><div class="form-actions"><button>保存并创建复盘</button></div></form>'''
                content_shell = content if daily["status"] == "completed" else f'<section class="panel">{content}</section>'
                self.send_html("今日建议与明日菜单", f'<section class="panel"><div class="section-header"><div><h1>今日建议与明日菜单</h1>{state}</div><a class="button secondary" href="/history">查看历史建议</a></div></section>{render_checkin_callout(daily["date"])}{content_shell}')
            elif path == "/history":
                reviews = service.list_daily_reviews()
                body = (
                    '<section class="history-heading"><div><p class="eyebrow">Advice archive</p>'
                    '<h1>历史建议</h1><p class="muted">按日期回看系统判断、核心动作和次日菜单，不再翻阅冗长的原始记录。</p></div>'
                    f'<span class="history-count">{len(reviews)} 天</span></section>'
                    + render_review_cards(reviews)
                )
                self.send_html("历史建议", body)
            elif path == "/check-ins":
                self.redirect(f"/check-ins/{date.today().isoformat()}")
            elif path == "/check-ins/settings":
                self.send_html("状态设置", render_checkin_settings())
            elif path.startswith("/check-ins/"):
                parts = path.strip("/").split("/")
                if len(parts) == 2:
                    self.send_html("今日状态", render_checkin_hub(parts[1]))
                elif len(parts) == 3:
                    requested = query.get("q", [None])[0]
                    self.send_html("状态问答", render_checkin_question(parts[1], parts[2], requested))
                else:
                    self.send_html("未找到", '<section class="card"><h1>404</h1><p>页面不存在。</p></section>', 404)
            elif path == "/tasks/photo":
                self.send_html("上传食物照片", '<section class="card"><h1>食物识别任务</h1><p class="muted">照片仅用于候选识别与区间估算。看不见的油、酱汁、重量和品牌必须列为未知项。</p><form method="post" enctype="multipart/form-data" action="/tasks/photo"><label for="task-photo">食物照片 *</label><input id="task-photo" type="file" name="photo" accept="image/jpeg,image/png,image/gif,image/webp" required><label for="task-note">补充说明</label><textarea id="task-note" name="note" placeholder="例如：这是训练后外食；酱汁没有全部吃完"></textarea><div class="form-actions"><button type="submit">创建待处理任务</button></div></form></section>')
            elif path == "/tasks/material":
                self.send_html("原材料分析", '<section class="card"><h1>原材料分析任务</h1><p class="muted">输入已有食材与粗略数量，Agent 会结合总纲、营养库、近 14 天记录和长期记忆分析。</p><form method="post" action="/tasks/material"><label for="task-materials">现有食材及粗略数量 *</label><textarea id="task-materials" name="materials" required placeholder="例如：鸡胸肉约 500g、冷冻西兰花一袋、米、鸡蛋 6 个"></textarea><div class="form-actions"><button type="submit">创建待处理任务</button></div></form></section>')
            elif path == "/tasks":
                self.send_html("任务列表", f'<section class="card"><h1>全部任务</h1>{task_table(service.list_tasks())}</section>')
            elif path == "/ai":
                self.send_html("API 接入", render_ai_settings())
            elif path.startswith("/tasks/"):
                task_id = path.split("/")[2]
                task = service.get_task(task_id)
                media = f'<img class="photo" src="/media/{Path(task["image_path"]).name}" alt="上传的食物照片">' if task.get("image_path") else ""
                if task["result_json"]:
                    result = render_result(task["type"], task["result_json"])
                else:
                    result = (
                        f'<p>等待 Agent 处理。</p><pre>python -m mealcircuit.agent_cli context {esc(task_id)} --output context.json\n'
                        f'python -m mealcircuit.agent_cli complete {esc(task_id)} --file result.json\n'
                        f'python -m mealcircuit.agent_cli generate {esc(task_id)}</pre>'
                        f'{render_task_generate_controls(task_id)}'
                    )
                corrections = "".join(f'<li>{esc(c["correction_json"]["text"])} <span class="muted small">{esc(c["created_at"])}</span></li>' for c in task["corrections"]) or '<li class="muted">暂无用户校正</li>'
                correction_form = f'<form method="post" action="/tasks/{esc(task_id)}/corrections"><label for="task-correction">新增用户校正（保留原结果，不覆盖）</label><textarea id="task-correction" name="text" required></textarea><div class="form-actions"><button type="submit">保存校正</button></div></form>' if task["status"] == "completed" else ""
                body = f'<section class="card"><h1>{"食物识别" if task["type"]=="photo" else "原材料分析"}</h1><p><span class="status {esc(task["status"])}">{esc(task["status"])}</span> · {esc(task_id)}</p>{media}{render_task_input(task)}</section><section class="card"><h2>Agent 分析结果</h2>{result}</section><section class="card"><h2>用户校正历史</h2><ul>{corrections}</ul>{correction_form}</section>'
                self.send_html("任务详情", body)
            elif path == "/foods":
                q = query.get("q", [""])[0]
                foods = service.list_foods(q)
                priority_labels = {"high": "高", "normal": "普通", "low": "低", "excluded": "不使用"}
                rows = "".join(f'<tr><td>{esc(f["name"])}</td><td>{esc(f["brand"])}</td><td>{esc(priority_labels.get(f["menu_priority"], f["menu_priority"]))}</td><td>{"每100g" if f["basis"]=="100g" else esc(f["serving_unit"])}</td><td>{esc(f["energy_kcal"])}</td><td>{esc(f["protein_g"])}</td><td><a href="/foods/{esc(f["id"])}">编辑</a></td></tr>' for f in foods)
                body = f'<section class="card"><div class="actions"><h1 class="section-heading">食品营养库</h1><a class="button" href="/foods/new">新增食品</a></div><form method="get"><label for="food-search">检索名称或品牌</label><div class="actions"><input class="search-control" id="food-search" name="q" value="{esc(q)}"><button>检索</button></div></form><div class="table-scroll" tabindex="0" role="region" aria-label="食品营养库"><table><thead><tr><th scope="col">名称</th><th scope="col">品牌</th><th scope="col">菜单优先级</th><th scope="col">基准</th><th scope="col">kcal</th><th scope="col">蛋白质</th><th scope="col"></th></tr></thead><tbody>{rows}</tbody></table></div><p class="muted">高优先级表示同功能下优先选择，不表示每天强制追加。</p></section>'
                self.send_html("食品营养库", body)
            elif path == "/foods/new":
                self.send_html("新增食品", f'<div class="section-header"><div><h1>新增食品 / 原料</h1><p class="muted">按包装标签或可靠来源保存，未知数据保持为空。</p></div></div>{food_form()}')
            elif path.startswith("/foods/"):
                food = service.get_food(path.split("/")[2])
                self.send_html("编辑食品", f'<div class="section-header"><div><h1>编辑食品 / 原料</h1><p class="muted">修改会保留历史版本。</p></div></div>{food_form(food)}<section class="panel error"><h2>危险操作</h2><p>删除后不会再用于菜单，但历史仍会保留。</p><form method="post" action="/foods/{esc(food["id"])}/delete" onsubmit="return confirm(\'确认删除？历史仍会保留。\')"><button class="danger">删除食品</button></form></section>')
            elif path == "/overview":
                info = service.overview()
                memories = "".join(f'<li><strong>{esc(m["kind"])}</strong> {esc(m["content"])} <span class="muted">{esc(m["evidence"])}</span></li>' for m in info["memories"]) or '<li class="muted">暂无长期记忆</li>'
                adjustments = "".join(f'<li>{esc(a["content"])} <span class="muted">{esc(a["reason"])}</span></li>' for a in info["adjustments"]) or '<li class="muted">暂无当前调整</li>'
                recent_reviews = info["daily_reviews"][:6]
                body = f'''<div class="grid"><section class="card"><h1>新增每日记录</h1><form method="post" action="/records"><label for="record-date">日期</label><input id="record-date" type="date" name="record_date" value="{date.today().isoformat()}" required><label for="record-input">自然语言记录</label><textarea id="record-input" name="raw_input" required></textarea><div class="form-actions"><button>保存</button></div></form></section><section class="card"><h2>新增长期记忆</h2><form method="post" action="/memories"><label for="memory-kind">类型</label><select id="memory-kind" name="kind"><option value="preference">已验证偏好</option><option value="gut_trigger">肠胃触发</option><option value="constraint">约束</option><option value="other">其他</option></select><label for="memory-content">内容</label><textarea id="memory-content" name="content" required></textarea><label for="memory-evidence">证据</label><input id="memory-evidence" name="evidence"><div class="form-actions"><button>保存</button></div></form></section><section class="card"><h2>新增当前有效调整</h2><form method="post" action="/adjustments"><label for="adjustment-content">调整内容</label><textarea id="adjustment-content" name="content" required></textarea><label for="adjustment-reason">原因</label><input id="adjustment-reason" name="reason"><div class="form-actions"><button>保存</button></div></form></section></div><section class="card"><div class="section-header"><div><p class="eyebrow">Advice archive</p><h2>最近建议</h2></div><a class="button secondary" href="/history">查看全部</a></div>{render_review_cards(recent_reviews)}</section><section class="card"><h2>长期记忆</h2><ul>{memories}</ul></section><section class="card"><h2>当前有效调整</h2><ul>{adjustments}</ul></section>'''
                self.send_html("记录与记忆", body)
            elif path.startswith("/reviews/"):
                review_date = path.split("/")[2]
                review = service.get_daily_review(review_date)
                if review["status"] == "completed":
                    result = render_daily_review_result(review["result_json"])
                else:
                    result = (
                        '<p>等待 Agent 生成核心建议和次日菜单。</p>'
                        f'<pre>python -m mealcircuit.agent_cli day-context {esc(review_date)} --output context.json\n'
                        f'python -m mealcircuit.agent_cli day-complete {esc(review_date)} --file result.json\n'
                        f'python -m mealcircuit.agent_cli day-generate {esc(review_date)}</pre>'
                        f'{render_daily_generate_controls(review_date)}'
                    )
                result_shell = result if review["status"] == "completed" else f'<section class="panel"><h2>核心建议与次日菜单</h2>{result}</section>'
                body = (
                    f'<section class="panel"><h1>{esc(review_date)} 每日复盘</h1>'
                    f'<p><span class="status {esc(review["status"])}">{esc(review["status"])}</span> · '
                    f'版本 {esc(review["result_version"])}</p></section>'
                    + render_checkin_callout(review_date)
                    + result_shell
                )
                self.send_html("每日复盘", body)
            elif path.startswith("/media/"):
                filename = Path(path).name
                target = (upload_root() / filename).resolve()
                if target.parent != upload_root().resolve() or not target.is_file():
                    raise FileNotFoundError(filename)
                data = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_security_headers()
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_html("未找到", '<section class="card"><h1>404</h1><p>页面不存在。</p></section>', 404)
        except KeyError:
            self.send_html("未找到", '<section class="card"><h1>404</h1><p>记录不存在。</p></section>', 404)
        except FileNotFoundError:
            self.send_html("未找到", '<section class="card"><h1>404</h1><p>文件不存在。</p></section>', 404)
        except (ValidationError, ValueError) as exc:
            self.render_error(exc)

    def food_payload(self, form: dict[str, str]) -> dict:
        return {
            "name": form.get("name", ""), "brand": form.get("brand", ""), "basis": form.get("basis", "100g"),
            "energy_kcal": nutrition_number(form.get("energy_kcal"), "能量"),
            "protein_g": nutrition_number(form.get("protein_g"), "蛋白质"),
            "carbs_g": nutrition_number(form.get("carbs_g"), "碳水"),
            "fat_g": nutrition_number(form.get("fat_g"), "脂肪"),
            "fiber_g": nutrition_number(form.get("fiber_g"), "膳食纤维"),
            "sodium_mg": nutrition_number(form.get("sodium_mg"), "钠"),
            "serving_unit": form.get("serving_unit", ""), "source_url": form.get("source_url", ""),
            "category": form.get("category", "other"), "menu_priority": form.get("menu_priority", "normal"),
            "default_portion": form.get("default_portion", ""), "usage_rule": form.get("usage_rule", ""),
            "source_key": form.get("source_key") or None,
            "package_photo_path": form.get("package_photo_path") or None, "notes": form.get("notes", ""),
        }

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            self.validate_origin()
            if path == "/setup/start":
                session = personalization.start_onboarding()
                self.redirect(f'/setup/{session["current_step"]}')
            elif path.startswith("/setup/save/"):
                step = path.split("/")[3]
                values = self.read_urlencoded_values()
                session_id = (values.get("session_id") or [""])[-1]
                version = int((values.get("version") or ["-1"])[-1])
                payload = self.setup_step_payload(step, values)
                try:
                    updated = personalization.save_onboarding_step(session_id, step, payload, version)
                except (ValidationError, ValueError) as exc:
                    session = personalization.get_onboarding(session_id)
                    self.send_html("初始化", render_setup_step(session, step, error=str(exc), override=payload), 400)
                    return
                order = list(personalization.ONBOARDING_STEPS)
                next_step = order[min(order.index(step) + 1, len(order) - 1)]
                self.redirect(f"/setup/{next_step}")
            elif path == "/setup/complete":
                values = self.read_urlencoded_values()
                session_id = (values.get("session_id") or [""])[-1]
                version = int((values.get("version") or ["-1"])[-1])
                confirmation = {
                    "accept_profile": "accept_profile" in values,
                    "accept_strategy": "accept_strategy" in values,
                    "planning_mode": (values.get("planning_mode") or ["portion_guided"])[-1],
                    "protein_candidate_id": (values.get("protein_candidate_id") or [None])[-1],
                }
                professional_low = (values.get("professional_protein_low") or [""])[-1].strip()
                professional_high = (values.get("professional_protein_high") or [""])[-1].strip()
                if professional_low or professional_high:
                    if not professional_low or not professional_high:
                        raise ValidationError("专业蛋白范围必须同时填写下界和上界")
                    confirmation["professional_targets"] = {
                        "protein_g": [float(professional_low), float(professional_high)]
                    }
                try:
                    personalization.complete_onboarding(session_id, version, confirmation)
                except (ValidationError, ValueError) as exc:
                    session = personalization.get_onboarding(session_id)
                    self.send_html("初始化", render_setup_step(session, "review", error=str(exc)), 400)
                    return
                self.redirect("/")
            elif path.startswith("/plans/") and path.endswith("/feedback"):
                parts = path.strip("/").split("/")
                plan_date, plan_item_id = parts[1], parts[2]
                values = self.read_urlencoded_values()
                expected = int((values.get("expected_version") or ["0"])[-1])
                adaptive.save_plan_feedback(
                    plan_date, plan_item_id, (values.get("status") or [""])[-1],
                    reason_codes=values.get("reason_codes") or [],
                    actual_text=(values.get("actual_text") or [""])[-1],
                    expected_version=expected or None, actor_source="web",
                )
                self.redirect(f"/plans/{plan_date}")
            elif path.startswith("/questions/") and path.endswith("/answer"):
                question_id = path.split("/")[2]
                values = self.read_urlencoded_values()
                version = int((values.get("version") or ["-1"])[-1])
                question = adaptive.get_question_event(question_id)
                schema = question.get("question_schema_json") or {}
                if schema.get("kind") == "plan_feedback":
                    answer: object = {
                        "status": (values.get("feedback_status") or [""])[-1],
                        "reason_codes": values.get("reason_codes") or [],
                        "actual_text": (values.get("actual_text") or [""])[-1],
                    }
                else:
                    answer = (values.get("answer") or [""])[-1]
                adaptive.answer_question(question_id, answer, version)
                self.redirect(f'/questions/{question["question_date"]}')
            elif path.startswith("/questions/") and path.endswith("/skip"):
                question_id = path.split("/")[2]
                values = self.read_urlencoded_values()
                version = int((values.get("version") or ["-1"])[-1])
                question_date = adaptive.get_question_event(question_id)["question_date"]
                adaptive.answer_question(question_id, None, version, skip=True)
                self.redirect(f"/questions/{question_date}")
            elif path.startswith("/learning/") and path.endswith("/decide"):
                candidate_id = path.split("/")[2]
                form = self.read_urlencoded()
                adaptive.decide_candidate(candidate_id, form.get("decision", ""), statement=form.get("statement") or None)
                self.redirect("/learning")
            elif path.startswith("/learning/rules/") and path.endswith("/status"):
                rule_id = path.split("/")[3]
                adaptive.set_rule_status(rule_id, self.read_urlencoded().get("status", ""))
                self.redirect("/learning")
            elif path == "/learning/experiments":
                form = self.read_urlencoded()
                adaptive.propose_experiment(form.get("variable_key", ""), {
                    "action": form.get("action", ""), "success_signal": form.get("success_signal", ""),
                })
                self.redirect("/learning")
            elif path.startswith("/learning/experiments/") and path.endswith("/start"):
                experiment_id = path.split("/")[3]
                form = self.read_urlencoded()
                adaptive.activate_experiment(
                    experiment_id, form.get("starts_on", ""), int(form.get("days", "0"))
                )
                self.redirect("/learning")
            elif path.startswith("/learning/experiments/") and path.endswith("/finish"):
                experiment_id = path.split("/")[3]
                form = self.read_urlencoded()
                adaptive.finish_experiment(
                    experiment_id, {"summary": form.get("summary", "")},
                    cancel=form.get("decision") == "cancel",
                )
                self.redirect("/learning")
            elif path == "/inventory":
                form = self.read_urlencoded()
                adaptive.create_inventory_item(form.get("name", ""), form.get("amount_text", ""), expires_on=form.get("expires_on") or None)
                self.redirect("/inventory")
            elif path == "/metrics":
                form = self.read_urlencoded()
                metric_key = form.get("metric_key", "")
                raw_value = form.get("value", "").strip()
                value: object = (
                    float(raw_value)
                    if metric_key in {"weight_kg", "waist_cm", "execution_rate"}
                    else raw_value
                )
                personalization.record_metric(
                    metric_key, form.get("observed_date", ""), value, source="user"
                )
                self.redirect("/insights")
            elif path.startswith("/inventory/"):
                inventory_id = path.split("/")[2]
                form = self.read_urlencoded()
                adaptive.update_inventory_status(inventory_id, form.get("status", ""), int(form.get("version", "-1")), form.get("amount_text"))
                self.redirect("/inventory")
            elif path == "/rescue/start":
                form = self.read_urlencoded()
                rescue = adaptive.create_rescue_session(form.get("plan_date", ""), form.get("plan_item_id", ""), form.get("issue_code", ""), form.get("input_text", ""))
                self.redirect(f'/rescue/{rescue["id"]}')
            elif path == "/data/import":
                fields, files = self.read_multipart(max_bytes=256 * 1024 * 1024)
                if fields.get("confirm_restore") != "yes" or "bundle" not in files:
                    raise ValidationError("必须选择导入包并明确确认恢复")
                _, data = files["bundle"]
                exports_root().mkdir(parents=True, exist_ok=True)
                target = exports_root() / f"pending-web-import-{os.urandom(8).hex()}.zip"
                target.write_bytes(data)
                try:
                    restored = portability.restore_bundle(target, confirm=True)
                finally:
                    target.unlink(missing_ok=True)
                self.send_html("目标与边界", render_data_page(f'恢复完成；恢复前备份：{restored.get("pre_restore_backup") or "无"}'))
            elif path.startswith("/rescue/") and path.endswith("/generate"):
                rescue_id = path.split("/")[2]
                service.generate_rescue(rescue_id)
                self.redirect(f"/rescue/{rescue_id}")
            elif path == "/check-ins/settings":
                values = self.read_urlencoded_values()
                current = service.checkin_module_settings()
                ordered_keys = [item["module_key"] for item in current]
                move = (values.get("move") or [""])[-1]
                if ":" in move:
                    module_key, direction = move.split(":", 1)
                    if module_key in ordered_keys:
                        index = ordered_keys.index(module_key)
                        swap = index - 1 if direction == "up" else index + 1
                        if 0 <= swap < len(ordered_keys):
                            ordered_keys[index], ordered_keys[swap] = ordered_keys[swap], ordered_keys[index]
                by_key = {item["module_key"]: item for item in current}
                settings = []
                for module_key in ordered_keys:
                    frequency = (values.get(f"frequency_{module_key}") or [by_key[module_key]["frequency"]])[-1]
                    settings.append({
                        "module_key": module_key,
                        "enabled": f"enabled_{module_key}" in values,
                        "frequency": frequency,
                    })
                service.update_checkin_module_settings(settings)
                self.redirect("/check-ins/settings")
            elif path == "/ai/configure":
                form = self.read_urlencoded()
                ai.configure_runtime(
                    form.get("provider", ""),
                    form.get("model", ""),
                    form.get("api_key", ""),
                    form.get("timeout_seconds", ""),
                    form.get("max_output_tokens", ""),
                )
                self.redirect("/ai")
            elif path == "/ai/disable":
                ai.clear_runtime()
                self.redirect("/ai")
            elif path.startswith("/check-ins/"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise ValidationError("每日状态路径无效")
                _, checkin_date, module_key, action = parts
                values = self.read_urlencoded_values()
                expected_version = int((values.get("expected_version") or ["0"])[-1])
                if action == "answer":
                    question_id = (values.get("question_id") or [""])[-1]
                    module = service.get_checkin_module(checkin_date, module_key)
                    question = checkins.question_definition(module_key, question_id, module["active_answers"])
                    exact = (values.get("exact_value") or [""])[-1].strip()
                    other_text = (values.get("other_text") or [""])[-1].strip()
                    if question["type"] == "duration" and exact:
                        value: object = float(exact)
                    elif question["type"] == "multi":
                        selected_values = values.get("value") or []
                        value = {"values": selected_values, "other_text": other_text} if question.get("allow_other_text") else selected_values
                    else:
                        selected_value = (values.get("value") or [""])[-1]
                        value = {"value": selected_value, "other_text": other_text} if question.get("allow_other_text") else selected_value
                    updated = service.save_checkin_answer(
                        checkin_date, module_key, question_id, value, expected_version
                    )
                    questions = checkins.applicable_questions(module_key, updated["active_answers"])
                    question_ids = [item["id"] for item in questions]
                    current_index = question_ids.index(question_id)
                    if current_index == len(question_ids) - 1:
                        service.complete_checkin_module(checkin_date, module_key, expected_version)
                        self.redirect(f"/check-ins/{checkin_date}")
                    else:
                        self.redirect(f"/check-ins/{checkin_date}/{module_key}?q={question_ids[current_index + 1]}")
                elif action == "complete":
                    service.complete_checkin_module(checkin_date, module_key, expected_version)
                    self.redirect(f"/check-ins/{checkin_date}")
                elif action == "skip":
                    service.skip_checkin_module(checkin_date, module_key, expected_version)
                    self.redirect(f"/check-ins/{checkin_date}")
                elif action == "discard-draft":
                    service.discard_checkin_draft(checkin_date, module_key, expected_version)
                    self.redirect(f"/check-ins/{checkin_date}")
                else:
                    raise ValidationError("未知的每日状态操作")
            elif path == "/tasks/photo":
                fields, files = self.read_multipart()
                if "photo" not in files:
                    raise ValidationError("请选择食物照片")
                _, data = files["photo"]
                task = service.create_photo_task(io.BytesIO(data), fields.get("note", ""))
                self.redirect(f'/tasks/{task["id"]}')
            elif path == "/tasks/material":
                task = service.create_material_task(self.read_urlencoded().get("materials", ""))
                self.redirect(f'/tasks/{task["id"]}')
            elif path.startswith("/tasks/") and path.endswith("/generate"):
                task_id = path.split("/")[2]
                service.generate_task_result(task_id)
                self.redirect(f"/tasks/{task_id}")
            elif path == "/foods":
                food = service.create_food(self.food_payload(self.read_urlencoded()))
                self.redirect(f'/foods/{food["id"]}')
            elif path.startswith("/foods/") and path.endswith("/delete"):
                service.delete_food(path.split("/")[2])
                self.redirect("/foods")
            elif path.startswith("/foods/"):
                food_id = path.split("/")[2]
                service.update_food(food_id, self.food_payload(self.read_urlencoded()))
                self.redirect(f"/foods/{food_id}")
            elif path.startswith("/tasks/") and path.endswith("/input"):
                task_id = path.split("/")[2]
                form = self.read_urlencoded()
                service.update_task_input(task_id, form.get("text", ""), int(form.get("expected_version", "")))
                self.redirect(f"/tasks/{task_id}")
            elif path.startswith("/tasks/") and path.endswith("/corrections"):
                task_id = path.split("/")[2]
                service.add_correction(task_id, {"text": self.read_urlencoded().get("text", "")})
                self.redirect(f"/tasks/{task_id}")
            elif path == "/records":
                form = self.read_urlencoded()
                record_date = form.get("record_date", "")
                service.add_daily_record(record_date, form.get("raw_input", ""))
                self.redirect(f"/reviews/{record_date}")
            elif path.startswith("/reviews/") and path.endswith("/generate"):
                review_date = path.split("/")[2]
                service.generate_daily_review(review_date)
                self.redirect(f"/reviews/{review_date}")
            elif path == "/memories":
                form = self.read_urlencoded()
                service.add_memory(form.get("kind", ""), form.get("content", ""), form.get("evidence", ""))
                self.redirect("/overview")
            elif path == "/adjustments":
                form = self.read_urlencoded()
                service.add_adjustment(form.get("content", ""), form.get("reason", ""))
                self.redirect("/overview")
            else:
                self.send_html("未找到", '<section class="card"><h1>404</h1></section>', 404)
        except KeyError:
            self.send_html("未找到", '<section class="card"><h1>404</h1><p>记录不存在。</p></section>', 404)
        except (ValidationError, ValueError) as exc:
            self.render_error(exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 MealCircuit（食回路）本地 Web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=port_value())
    parser.add_argument("--allow-remote", action="store_true", help="允许监听非回环地址（无认证、无 TLS）")
    args = parser.parse_args()
    try:
        is_loopback = args.host.lower() == "localhost" or ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback and not args.allow_remote:
        parser.error("非回环地址必须显式传入 --allow-remote；该模式无认证、无 TLS")
    if not is_loopback:
        print("警告：远程监听模式没有认证和 TLS，请只在受信任网络中使用。", file=sys.stderr)
    initialize_private_home()
    init_db()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.allow_remote = args.allow_remote
    print(f"MealCircuit 已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
