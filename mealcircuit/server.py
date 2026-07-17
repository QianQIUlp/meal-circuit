from __future__ import annotations

import argparse
import hashlib
import html
import io
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from datetime import date, timedelta
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import adaptive, agent_workspace, ai, checkins, personalization, portability, service, sync
from .configuration import initialize_private_home
from .db import connect, init_db
from .storage import exports_root, managed_asset_root, port_value, upload_root
from .validation import ValidationError, nutrition_number


LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
STATIC_ROOT = Path(__file__).with_name("static")
_PENDING_RECOVERY: dict[str, dict] = {}
_PENDING_RECOVERY_LOCK = threading.Lock()


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
MEAL_PREPARATION_LABELS = {
    "home_cook": "在家下厨（生成执行卡）",
    "quick_assembly": "在家快速组装",
    "eat_out": "食堂 / 外食",
}

FEEDBACK_LABELS = {
    "followed": "按计划完成",
    "modified": "调整后完成",
    "skipped": "没有执行",
    "not_applicable": "今天不适用",
}
REASON_LABELS = {
    "missing_ingredient": "缺少食材", "too_expensive": "价格不合适",
    "not_enough_time": "时间不足", "too_complex": "步骤太复杂",
    "ate_out": "临时外食", "did_not_want_it": "当时不想吃", "hunger_mismatch": "饥饿或份量不匹配",
    "gut_change": "肠胃状态变化", "schedule_change": "日程变化", "other": "其他",
}
RESCUE_LABELS = {
    "ingredient_missing": "缺少食材", "not_enough_time": "时间不足", "too_complex": "做起来太复杂",
    "gut_change": "肠胃状态变化", "schedule_change": "日程变化", "other": "其他临时情况",
}

PUBLIC_AGENT_STATUS = {
    "collecting": "今天感觉怎么样？",
    "needs_clarification": "还需要你确认一件事",
    "formulating": "正在调整安排",
    "planning": "正在调整安排",
    "reviewing": "正在调整安排",
    "ready_draft": "明天的安排准备好了",
    "stale": "情况有变化，正在重新调整",
    "interrupted": "情况有变化，正在重新调整",
    "failed": "这次没有调整成功，原计划保持不变",
    "accepted": "今天的安排",
    "active": "今天的安排",
    "completed": "今天已经记完",
}
SAFETY_MODE_LABELS = {
    "standard": "按你的目标提供日常饮食建议",
    "observation": "只记录和解释，不给出营养处方",
    "clinician_guided": "按有效的专业指导安排",
    "halt_and_refer": "暂停饮食规划，建议寻求专业判断",
    "setup_required": "还需要完成初始设置",
}
INVENTORY_STATUS_LABELS = {
    "available": "还有",
    "used": "已用完",
    "not_bought": "没有买到",
    "discarded": "已丢弃",
    "unknown": "不确定",
}
TARGET_LABELS = {
    "energy_kcal": "每日能量",
    "energy_target_kcal": "每日能量",
    "protein_g": "每日蛋白质",
    "protein_target_g": "每日蛋白质",
}


def public_agent_status(status: str) -> str:
    return PUBLIC_AGENT_STATUS.get(status, "正在整理今天的情况")


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]


def _number_or_none(value: str) -> float | None:
    return float(value) if value.strip() else None


def render_setup_start(status: dict) -> str:
    session = status.get("session")
    if session:
        action = f'<a class="button" href="/setup/{esc(session["current_step"])}">继续初始化</a>'
    else:
        action = '<form method="post" action="/setup/start"><button type="submit">开始设置</button></form>'
    return f'''<section class="setup-shell panel"><h1>先让MealCircuit了解你的目标</h1>
    <p class="lede">我们会先了解你想达成什么、平时怎样吃、怎样训练，以及哪些情况需要特别注意。答案只保存在本机，可以随时退出后继续。</p>
    <div class="setup-principles"><p><strong>目标可以随时改</strong><br><span class="muted">新的选择只影响之后的安排，过去的记录不会变。</span></p><p><strong>不确定也没关系</strong><br><span class="muted">暂时不知道的内容可以跳过，我们不会替你猜。</span></p><p><strong>先照顾健康需要</strong><br><span class="muted">有特殊健康情况时，只会在可靠指导允许的范围内提供安排。</span></p></div>{action}
    <p class="muted small">即使还没设置完，你也可以继续记录饮食、照片和库存。</p></section>'''


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
        meal_modes = value.get("meal_modes") or personalization.LEGACY_DEFAULT_MEAL_MODES
        mode_fields = "".join(
            f'<div><label for="meal-mode-{key}">{name}通常怎样准备？</label><select id="meal-mode-{key}" name="meal_mode_{key}">'
            + "".join(
                f'<option value="{mode}"{_selected(meal_modes.get(key), mode)}>{label}</option>'
                for mode, label in MEAL_PREPARATION_LABELS.items()
            )
            + "</select></div>"
            for key, name in (("breakfast", "早餐"), ("lunch", "午餐"), ("dinner", "晚餐"))
        )
        value_labels = {
            "health": "健康与安全", "training": "训练与恢复", "satiety": "吃得饱",
            "budget": "长期负担得起", "time": "不占用太多时间", "taste": "合口味",
            "social": "保留社交弹性", "convenience": "足够方便",
        }
        non_negotiables = value.get("non_negotiables") or []
        non_negotiable_fields = "".join(
            f'<label class="choice-row"><input type="checkbox" name="non_negotiables" value="{key}"'
            f'{_checked(non_negotiables, key)}><span>{label}</span></label>'
            for key, label in value_labels.items()
        )
        priorities = value.get("priority_tradeoffs") or []
        priority_selects = "".join(
            f'<div><label for="priority-{index}">第{index}优先</label><select id="priority-{index}" name="priority_tradeoff">'
            '<option value="">暂不指定</option>'
            + "".join(
                f'<option value="{key}"{_selected(priorities[index - 1] if len(priorities) >= index else "", key)}>{label}</option>'
                for key, label in value_labels.items()
            )
            + '</select></div>'
            for index in range(1, 4)
        )
        recording = value.get("recording_intensity", "light")
        followup = value.get("followup_intensity", "only_when_needed")
        fields = f'''<fieldset class="form-section"><legend>逐餐准备标准</legend><p class="muted">这些选择会成为你的长期习惯；选择在家下厨的餐次会分别生成一人份执行卡。</p><div class="row">{mode_fields}</div></fieldset><fieldset class="form-section"><legend>哪些事情不能为了饮食目标被牺牲？</legend><p class="muted">这会在多个方案都可行时决定 MealCircuit 怎样取舍。</p><div class="option-grid">{non_negotiable_fields}</div></fieldset><fieldset class="form-section"><legend>发生冲突时，更看重什么？</legend><div class="row">{priority_selects}</div></fieldset><label for="meal-environment">典型用餐环境</label><input id="meal-environment" name="meal_environment" required value="{esc(value.get('meal_environment',''))}" placeholder="例如：午餐和晚餐在家做"><label for="portion-method">希望怎样表达份量</label><input id="portion-method" name="portion_method" required value="{esc(value.get('portion_method','手掌与拳头份量法'))}"><div class="row"><div><label for="cooking-time">每个自炊餐次可用时间（分钟）</label><input id="cooking-time" type="number" min="10" max="60" name="cooking_time_minutes" value="{esc(value.get('cooking_time_minutes',25))}"></div><div><label for="question-budget">每天最多主动问几题</label><input id="question-budget" type="number" min="0" max="3" name="question_budget" value="{esc(value.get('question_budget',2))}"></div></div><div class="row"><div><label for="recording-intensity">希望记录到什么程度</label><select id="recording-intensity" name="recording_intensity"><option value="light"{_selected(recording,'light')}>尽量少记</option><option value="standard"{_selected(recording,'standard')}>需要时补充</option><option value="detailed"{_selected(recording,'detailed')}>愿意记录更多细节</option></select></div><div><label for="followup-intensity">希望 MealCircuit 怎样追问</label><select id="followup-intensity" name="followup_intensity"><option value="only_when_needed"{_selected(followup,'only_when_needed')}>只问会改变安排的事</option><option value="balanced"{_selected(followup,'balanced')}>适度确认</option><option value="proactive"{_selected(followup,'proactive')}>主动帮我深挖</option></select></div></div><label for="equipment">可用厨具（逗号分隔）</label><input id="equipment" name="equipment" value="{esc(', '.join(value.get('equipment') or []))}" placeholder="例如：炒锅, 电饭煲"><label for="exclusions">排除食品（逗号分隔）</label><input id="exclusions" name="food_exclusions" value="{esc(', '.join(value.get('food_exclusions') or []))}"><label for="preferences">偏好（逗号分隔）</label><input id="preferences" name="preferences" value="{esc(', '.join(value.get('preferences') or []))}">'''
    else:
        preview = personalization.onboarding_preview(session["id"])
        safety = preview["safety"]
        assessment = preview["target_assessment"]
        primary_goal = preview["goals"][0]
        primary_goal_label = primary_goal.get("custom_label") or GOAL_LABELS.get(primary_goal["type"], primary_goal["type"])
        selected_modes = preview["profile"]["constraints"]["meal_modes"]
        contract_constraints = preview["profile"]["constraints"]
        value_labels = {
            "health": "健康与安全", "training": "训练与恢复", "satiety": "吃得饱",
            "budget": "长期负担得起", "time": "不占用太多时间", "taste": "合口味",
            "social": "保留社交弹性", "convenience": "足够方便",
        }
        non_negotiable_summary = "、".join(
            value_labels.get(item, item) for item in contract_constraints.get("non_negotiables") or []
        ) or "暂未指定"
        tradeoff_summary = " → ".join(
            value_labels.get(item, item) for item in contract_constraints.get("priority_tradeoffs") or []
        ) or "根据当天情况协商"
        meal_mode_summary = " · ".join(
            f'{name}：{MEAL_PREPARATION_LABELS[selected_modes[key]]}'
            for key, name in (("breakfast", "早餐"), ("lunch", "午餐"), ("dinner", "晚餐"))
        )
        target_options = "".join(
            f'<label class="choice-row"><input type="radio" name="protein_candidate_id" value="{esc(item["candidate_id"])}"{ " checked" if len(assessment["protein_candidates"]) == 1 else ""}><span>{esc(item["target_g"])} g/天 · {esc(item["basis"])}</span></label>'
            for item in assessment["protein_candidates"]
        ) or '<p class="muted">当前不建立蛋白目标；继续使用份量与执行策略。</p>'
        professional_target_fields = ""
        if safety["mode"] == "clinician_guided" and safety["professional_guidance_current"]:
            professional_target_fields = '''<fieldset class="form-section"><legend>专业指导中的蛋白范围（可选）</legend><p class="muted">只录入指导中明确给出的范围；来源和有效期沿用上一页的专业指导，不做系统推算。</p><div class="row"><label>下界 g/天<input type="number" step="0.1" name="professional_protein_low"></label><label>上界 g/天<input type="number" step="0.1" name="professional_protein_high"></label></div></fieldset>'''
        strategy_ack = '' if safety["mode"] in {"observation", "halt_and_refer"} or (safety["mode"] == "clinician_guided" and not safety["professional_guidance_current"]) else '<label class="choice-row"><input type="checkbox" name="accept_strategy" value="yes" required><span>我确认采用这份初始策略</span></label>'
        safety_label = SAFETY_MODE_LABELS.get(safety["mode"], "按当前健康边界安排")
        fields = f'''<div class="contract-summary"><p><span class="status">想达成</span><strong>{esc(primary_goal_label)}</strong></p><p><span class="status">为什么</span><strong>{esc(primary_goal.get('motivation') or '暂未填写')}</strong></p><p><span class="status">不能牺牲</span><strong>{esc(non_negotiable_summary)}</strong></p><p><span class="status">冲突时优先</span><strong>{esc(tradeoff_summary)}</strong></p><p><span class="status">每天通常怎么吃</span><strong>{esc(meal_mode_summary)}</strong></p><p><span class="status">需要注意</span><strong>{esc(safety_label)}</strong></p></div><h2>每天的蛋白质范围</h2><div class="option-list">{target_options}</div>{professional_target_fields}<input type="hidden" name="planning_mode" value="portion_guided"><label class="choice-row"><input type="checkbox" name="accept_profile" value="yes" required><span>上面的目标和注意事项符合我的情况</span></label>{strategy_ack}<div class="notice panel"><strong>还没有确定</strong><ul>{''.join(f'<li>{esc(note)}</li>' for note in assessment['notes']) or '<li>没有额外提示</li>'}</ul></div>'''
    action = "/setup/complete" if step == "review" else f"/setup/save/{step}"
    submit = "确认并进入工作台" if step == "review" else "保存并继续"
    return f'''<section class="setup-shell"><div class="setup-progress"><p class="eyebrow">步骤 {index + 1} / {len(order)}</p><div class="progress-track" role="progressbar" aria-valuemin="1" aria-valuemax="{len(order)}" aria-valuenow="{index + 1}"><span class="progress-fill" style="width:{progress}%"></span></div></div>{error_html}<form class="panel setup-form" method="post" action="{action}">{hidden}<h1>{esc({"welcome":"隐私与边界","goals":"你想达成什么","baseline":"你的基本情况","safety":"需要注意的健康情况","training":"平时怎样训练","constraints":"怎样安排才做得到","review":"确认理解"}[step])}</h1>{fields}<div class="form-actions"><button type="submit">{submit}</button></div></form></section>'''


def _today_reference_points(state: dict, draft: dict, goal_label: str) -> list[str]:
    formulation = draft.get("formulation_json") or {}
    points = [f"你的目标是{goal_label}"]
    points.extend(str(item) for item in formulation.get("current_state") or [] if item)
    points.extend(
        str(item.get("need") or item.get("statement"))
        for item in formulation.get("underlying_needs") or []
        if isinstance(item, dict) and (item.get("need") or item.get("statement"))
    )
    points.extend(
        str(item["statement"])
        for item in state.get("claims") or []
        if item.get("status") == "active" and item.get("statement")
    )
    unique: list[str] = []
    for point in points:
        if point not in unique:
            unique.append(point)
    return unique[:5]


def _render_today_state(work_date: str) -> str:
    checkin = service.get_checkin_state(work_date)
    enabled = [item for item in checkin["modules"] if item["enabled"]]
    known = [
        f'{item["label"]}：{item["summary"]}'
        for item in enabled
        if item["status"] == "completed" and item.get("summary")
    ]
    pending = next(
        (
            item for item in enabled
            if item["status"] not in {"completed", "skipped"} or item.get("has_draft")
        ),
        None,
    )
    if pending:
        module = service.get_checkin_module(work_date, pending["module_key"])
        question = module.get("next_question")
        prompt = question.get("label") if question else f'补充{pending["label"]}'
        next_action = (
            f'<div class="today-state-next"><div><span class="subtle-label">接下来</span>'
            f'<strong>{esc(prompt)}</strong></div><a class="button secondary" '
            f'href="/check-ins/{esc(work_date)}/{esc(pending["module_key"])}?return_to=today">现在回答</a></div>'
        )
    else:
        next_action = '<p class="quiet-note">今天需要的状态已经记下来了。</p>'
    summary = (
        '<ul class="plain-summary">' + "".join(f'<li>{esc(item)}</li>' for item in known[:4]) + '</ul>'
        if known else '<p class="muted">还没有补充今天的身体和训练状态，跳过也不会被当成“没有问题”。</p>'
    )
    return (
        f'<section class="panel today-state" id="today-state"><div class="section-header"><div>'
        f'<h2>今天的状态</h2><p>只补充会影响份量、训练恢复或饮食安排的信息。</p></div>'
        f'<a href="/check-ins/{esc(work_date)}">查看全部</a></div>{summary}{next_action}</section>'
    )


def _render_draft_meals(result: dict) -> str:
    basis_labels = {"raw": "生重", "cooked": "熟重", "as_served": "上桌重量", "not_applicable": "不适用"}
    cards = []
    for meal in (result.get("tomorrow_menu") or {}).get("meals") or []:
        portions = "".join(
            f'<li><strong>{esc(item.get("item"))}</strong> '
            f'{esc("–".join(str(value) for value in item["gram_range"]) + "g" if item.get("gram_range") else "按食欲调整")} · '
            f'{esc(basis_labels.get(item.get("measurement_basis"), item.get("measurement_basis") or ""))} · '
            f'{esc(item.get("household_measure"))}'
            f'<br><span class="muted small">吃不饱时：{esc(item.get("increase_if"))}；食欲低时：{esc(item.get("decrease_if"))}</span></li>'
            for item in meal.get("portion_contracts") or []
        )
        cards.append(
            f'<article class="draft-meal"><div><span class="subtle-label">'
            f'{esc(MEAL_MODE_LABELS.get(meal.get("mode"), meal.get("mode") or ""))}</span>'
            f'<h3>{esc(meal.get("name"))}</h3><p>{esc(meal.get("purpose") or meal.get("why_today") or "")}</p></div>'
            f'<ul class="portion-list">{portions}</ul></article>'
        )
    return "".join(cards)


def _render_today_core_advice(work_date: str, draft: dict | None = None) -> str:
    result = {}
    detail_link = ""
    try:
        review = service.get_daily_review(work_date)
    except KeyError:
        review = None
    if review and review.get("status") == "completed":
        result = review.get("result_json") or {}
        detail_link = f'<a href="/reviews/{esc(work_date)}">看看我是怎么判断的</a>'
    elif draft and draft.get("status") == "ready_draft":
        result = draft.get("result_json") or {}
    advice = [str(item).strip() for item in result.get("core_advice") or [] if str(item).strip()][:3]
    if not advice:
        return ""
    summary = str(result.get("one_line_review") or "").strip()
    summary_html = f'<p class="lede">{esc(summary)}</p>' if summary else ""
    items = "".join(f'<li>{esc(item)}</li>' for item in advice)
    return (
        '<section class="panel today-advice" aria-labelledby="today-advice-title">'
        '<div class="section-header"><div><h2 id="today-advice-title">今天的核心建议</h2>'
        f'{summary_html}</div>{detail_link}</div>'
        f'<ul class="structured-list">{items}</ul></section>'
    )


def _learning_nudge_copy(claim: dict) -> dict:
    scope = claim.get("scope_json") or {}
    dimension = claim.get("claim_dimension") or claim.get("claim_type") or ""
    meal = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"}.get(
        str(scope.get("meal") or ""), str(scope.get("meal") or "这类餐次")
    )
    evidence = next((
        item for item in claim.get("evidence") or []
        if item.get("active") and item.get("stance") == "support" and item.get("excerpt")
    ), None)
    excerpt = str((evidence or {}).get("excerpt") or "").strip()
    if len(excerpt) > 140:
        excerpt = excerpt[:139].rstrip() + "…"
    pending = claim.get("status") == "pending_confirmation"
    copy = {
        "eyebrow": "我想确认一下" if pending else "我准备这样调整",
        "question": f'“{claim.get("statement") or "这条理解"}”会影响之后的安排，这样理解对吗？',
        "impact": "确认后，之后的安排会按这个方向调整；你也可以把它只留在今天。",
        "confirm": "对，就这样",
        "today": "只是今天",
        "reject": "不用这样调整",
        "excerpt": excerpt,
    }
    if dimension == "execution_friction":
        copy.update({
            "question": f"以后需要给{meal}准备一个不用开火或更快的备选吗？",
            "impact": f"选择“以后都准备”后，之后的{meal}会优先减少需要开火、等待或持续操作的步骤。",
            "confirm": "以后都准备",
        })
    elif dimension == "resource_constraint":
        item_name = str(scope.get("item") or "食材")
        copy.update({
            "question": f"以后安排{item_name}时，要优先考虑长期负担得起的选择吗？",
            "impact": f"选择“对，就这样”后，{item_name}不会再作为默认采购，除非你主动提出或价格条件发生变化。",
        })
    elif dimension == "satiety_pattern":
        copy.update({
            "question": f"以后安排{meal}时，需要按这次的食量调整默认份量吗？",
            "impact": "确认后，之后的份量和加减量顺序会参考这次真实的饱腹反馈。",
        })
    return copy


def render_today_workspace(work_date: str) -> str:
    current = personalization.active_personalization()
    if current["status"] == "setup_required":
        return render_setup_start(personalization.onboarding_status())

    state = agent_workspace.get_workspace_state(work_date)
    draft = state.get("draft") or {}
    if draft.get("status") in {None, "stale", "failed", "interrupted"}:
        agent_workspace.schedule_auto_draft(work_date)
    status = draft.get("status") or (state.get("latest_run") or {}).get("status") or "collecting"
    goal = (current.get("goals") or [{}])[0].get("goal_json") or {}
    goal_label = goal.get("custom_label") or GOAL_LABELS.get(goal.get("type"), goal.get("type") or "你的目标")
    current_plan = adaptive.get_plan_for_date(work_date)
    today_advice = _render_today_core_advice(work_date, draft)

    if current_plan:
        hero_title = "今天按这份安排来"
        hero_text = "需要临时调整时直接记下来，我会保留不受影响的部分。"
    elif status == "needs_clarification":
        hero_title = "还需要你确认一件事"
        hero_text = "只补齐会真正改变份量、训练恢复或用餐方式的信息。"
    elif status == "ready_draft":
        hero_title = "明天的安排准备好了"
        hero_text = "先看看是否符合你的真实情况，再决定要不要采用。"
    elif status in {"formulating", "planning", "reviewing"}:
        hero_title = "正在调整明天的安排"
        hero_text = "你可以先离开，完成后这里会自动更新。"
    elif status in {"stale", "interrupted"}:
        hero_title = "情况有变化，正在重新调整"
        hero_text = "原来的正式计划不会被覆盖。"
    elif status == "failed":
        hero_title = "这次没有调整成功，原计划保持不变"
        hero_text = "可以稍后重试，已经接受的安排不会丢失。"
    else:
        hero_title = "今天感觉怎么样？"
        hero_text = "告诉我饮食、训练、食欲或临时安排的变化，我会据此调整今天和明天。"

    active_plan = ""
    if current_plan:
        meal_names = "、".join(
            str(meal.get("name") or meal.get("slot") or "一餐")
            for meal in current_plan["menu"].get("meals") or []
        )
        active_plan = (
            '<section class="panel today-primary"><div class="section-header"><div><h2>今天怎么吃</h2>'
            f'<p>{esc(meal_names)}</p></div><a class="button" href="/plans/{esc(work_date)}">查看今天安排</a></div></section>'
        )

    saved_records = service.list_daily_records(work_date)
    intake_forms = "".join(
        f'<form class="agent-intake-form is-saved" method="post" action="/agent/intake/{esc(record["id"])}/edit">'
        f'<input type="hidden" name="record_date" value="{esc(work_date)}">'
        f'<label>记一笔<textarea class="agent-intake-text" name="text" maxlength="4000" required>{esc(record["raw_input"])}</textarea></label>'
        '<button>记下来</button></form>'
        for record in saved_records
    )
    if not intake_forms:
        intake_forms = (
            f'<form class="agent-intake-form" method="post" action="/agent/intake">'
            f'<input type="hidden" name="record_date" value="{esc(work_date)}">'
            '<label>记一笔<textarea class="agent-intake-text" name="text" maxlength="4000" required '
            'placeholder="例如：明天中午外食，晚上自己做；今天训练后特别饿。"></textarea></label>'
            '<button>记下来</button></form>'
        )
    feedback_today = adaptive.list_plan_feedback(work_date)
    today_evidence = [*saved_records, *feedback_today]
    evidence_rank = {item["id"]: index for index, item in enumerate(today_evidence)}
    today_evidence_ids = set(evidence_rank)
    learned_today_candidates = [
        item for item in state.get("claims") or []
        if item.get("status") in {"active", "pending_confirmation"}
        and item.get("risk_level") == "low"
        and any(evidence.get("evidence_id") in today_evidence_ids for evidence in item.get("evidence") or [])
    ]
    learned_today = max(
        learned_today_candidates,
        key=lambda item: (
            max((
                evidence_rank.get(evidence.get("evidence_id"), -1)
                for evidence in item.get("evidence") or []
            ), default=-1),
            1 if (item.get("scope_json") or {}).get("meal") else 0,
            1 if (item.get("scope_json") or {}).get("item") else 0,
            1 if item.get("status") == "pending_confirmation" else 0,
            str(item.get("updated_at") or ""),
        ),
        default=None,
    )
    learning_prompt = ""
    if learned_today:
        nudge = _learning_nudge_copy(learned_today)
        evidence_html = (
            f'<p class="learning-evidence">你刚才提到：“{esc(nudge["excerpt"])}”</p>'
            if nudge["excerpt"] else ""
        )
        learning_prompt = (
            '<section class="panel learning-nudge" aria-labelledby="learning-nudge-title">'
            f'<p class="muted">{esc(nudge["eyebrow"])}</p>'
            f'<h2 id="learning-nudge-title">{esc(nudge["question"])}</h2>{evidence_html}'
            f'<p class="muted">{esc(nudge["impact"])}</p><div class="actions">'
            f'<form method="post" action="/learning/claims/{esc(learned_today["id"])}/action">'
            '<input type="hidden" name="action" value="confirm"><input type="hidden" name="return_to" value="/">'
            f'<button class="secondary">{esc(nudge["confirm"])}</button></form>'
            f'<form method="post" action="/learning/claims/{esc(learned_today["id"])}/action">'
            '<input type="hidden" name="action" value="today"><input type="hidden" name="return_to" value="/">'
            f'<button class="secondary">{esc(nudge["today"])}</button></form>'
            f'<form method="post" action="/learning/claims/{esc(learned_today["id"])}/action">'
            '<input type="hidden" name="action" value="reject"><input type="hidden" name="return_to" value="/">'
            f'<button class="secondary">{esc(nudge["reject"])}</button></form></div></section>'
        )

    question_cards = []
    for question in state.get("questions") or []:
        if question.get("status") != "pending":
            continue
        schema = question.get("answer_schema_json") or {}
        options = schema.get("options") or []
        control = (
            '<div class="option-list">' + "".join(
                f'<label class="choice-row"><input type="radio" name="answer" value="{esc(value)}" required>'
                f'<span>{esc(value)}</span></label>' for value in options
            ) + '</div>'
            if options else '<label>你的回答<textarea name="answer" required></textarea></label>'
        )
        question_cards.append(
            f'<article class="agent-question"><h3>{esc(question["prompt"])}</h3><p>{esc(question["reason"])}</p>'
            f'<form method="post" action="/agent/questions/{esc(question["id"])}/answer">'
            f'<input type="hidden" name="version" value="{esc(question["version"])}">{control}'
            '<button>记下来并继续</button></form></article>'
        )
    clarification = (
        f'<section class="panel"><h2>还有一件事想确认</h2><div class="agent-questions">{"".join(question_cards[:3])}</div></section>'
        if question_cards else ""
    )

    draft_html = ""
    result = draft.get("result_json") or {}
    reference_points = _today_reference_points(state, draft, goal_label)
    references = "".join(f'<li>{esc(item)}</li>' for item in reference_points)
    if status == "ready_draft" and result:
        rationale = "".join(f'<li>{esc(item)}</li>' for item in result.get("planning_rationale") or [])
        draft_html = (
            '<section class="panel agent-draft" id="tomorrow-plan"><div class="section-header"><div>'
            f'<h2>明天的安排</h2><p class="lede">{esc(result.get("case_summary") or result.get("one_line_review"))}</p></div></div>'
            f'<div class="draft-meals">{_render_draft_meals(result)}</div>'
            f'<details><summary>为什么这样安排</summary><ol class="rationale-list">{rationale}</ol></details>'
            f'<details><summary>这次参考了什么</summary><ul>{references}</ul></details>'
            f'<form class="agent-revision" method="post" action="/agent/drafts/{esc(work_date)}/revise">'
            '<label>哪里需要调整<textarea name="instruction" required placeholder="例如：午饭改外食，晚饭保持不变；或者晚饭菜量再大一点。"></textarea></label>'
            '<button class="secondary">只调整这些地方</button></form>'
            f'<form method="post" action="/agent/drafts/{esc(work_date)}/accept"><button>采用这份安排</button></form></section>'
        )
    elif status in {"accepted", "active", "completed"}:
        plan_date = ((result.get("tomorrow_menu") or {}).get("date"))
        published = adaptive.get_plan_for_date(plan_date) if plan_date else None
        if published:
            draft_html = (
                '<section class="panel quiet-success" id="tomorrow-plan"><div class="section-header"><div>'
                f'<h2>明天的安排已经准备好</h2><p>{esc(published["plan_date"])}，临时变化仍可随时记录。</p></div>'
                f'<a class="button" href="/plans/{esc(published["plan_date"])}">查看明天安排</a></div></section>'
            )
    elif status in {"formulating", "planning", "reviewing", "stale", "interrupted"}:
        polling = agent_workspace.auto_generation_status(work_date)["eligible"]
        attrs = (
            f' data-agent-state-url="/agent/state/{esc(work_date)}" data-agent-status="{esc(status)}" '
            f'data-agent-version="{esc(draft.get("version") or 0)}"' if polling else ""
        )
        draft_html = (
            f'<section class="panel quiet-progress" aria-live="polite"{attrs}><h2>{esc(public_agent_status(status))}</h2>'
            '<p>你可以先离开，完成后这里会自动更新。</p></section>'
        )
    elif status == "failed":
        draft_html = (
            '<section class="panel form-error"><h2>这次没有调整成功，原计划保持不变</h2>'
            f'<form method="post" action="/agent/drafts/{esc(work_date)}/generate"><button>重新试一次</button></form></section>'
        )
    elif not current_plan:
        eligibility = agent_workspace.auto_generation_status(work_date)
        if not eligibility["eligible"]:
            draft_html = (
                '<section class="panel quiet-note"><h2>智能规划还没有开启</h2>'
                '<p>开启后，MealCircuit会根据你记录的情况准备明日安排。</p>'
                '<a class="button secondary" href="/me#advanced">前往设置</a></section>'
            )

    return (
        f'<section class="today-hero"><div><h1>{esc(hero_title)}</h1><p class="lede">{esc(hero_text)}</p></div></section>'
        f'{active_plan}'
        f'{today_advice}'
        f'<section class="panel agent-intake" id="record"><div><h2>今天有什么变化？</h2>'
        '<p>吃了什么、训练感受、食欲、日程和临时安排都可以直接说。</p></div>'
        f'<div class="agent-intake-entry">{intake_forms}<div class="secondary-actions"><a href="/tasks/photo">上传照片</a>'
        '<a href="/tasks/material">补充食材</a><a href="/inventory">更新库存</a></div></div></section>'
        f'{learning_prompt}{_render_today_state(work_date)}{clarification}{draft_html}'
    )


def render_agent_context_page(work_date: str) -> str:
    context = agent_workspace.build_agent_context(work_date)
    inspector = context["context_inspector"]
    included = "".join(
        f'<li><strong>{esc(item["section"])}</strong><span>{esc(item["reason"])}</span></li>'
        for item in inspector["included"]
    )
    excluded = "".join(
        f'<li><strong>{esc(item["kind"])}</strong><span>{esc(item["reason"])}</span></li>'
        for item in inspector["excluded"]
    )
    person = context["person"]
    goals = "".join(
        f'<li>{esc((item.get("goal_json") or {}).get("custom_label") or (item.get("goal_json") or {}).get("type") or "未命名目标")}</li>'
        for item in person.get("goals") or []
    ) or '<li class="muted">没有生效目标</li>'
    claims = "".join(
        f'<li><strong>{esc(item["statement"])}</strong><span>这条理解正在影响本次安排</span></li>'
        for item in person.get("active_user_model") or []
    ) or '<li class="muted">本次没有使用尚未确认的理解</li>'
    today = context["today"]
    records = "".join(
        f'<li><strong>{esc(item["created_at"])}</strong><span>{esc(item["raw_input"])}</span></li>'
        for item in today.get("records") or []
    ) or '<li class="muted">没有当天文字记录</li>'
    modes = context["decision_task"]["immutable_constraints"].get("effective_meal_modes") or {}
    mode_text = "、".join(
        f'{name}：{MEAL_MODE_LABELS.get(modes.get(key), modes.get(key) or "未知")}'
        for key, name in (("breakfast", "早餐"), ("lunch", "午餐"), ("dinner", "晚餐"))
    )
    coverage = today.get("checkin_coverage") or {}
    module_labels = {
        "weight": "体重", "training": "训练", "hunger": "饥饿感",
        "sleep": "睡眠", "gut": "肠胃",
    }
    missing_text = "、".join(module_labels.get(value, value) for value in coverage.get("missing") or [])
    skipped_text = "、".join(module_labels.get(value, value) for value in coverage.get("skipped") or [])
    coverage_bits = [f'已处理 {coverage.get("handled", 0)} / {coverage.get("due", 0)}']
    if missing_text:
        coverage_bits.append(f"仍未知：{missing_text}")
    if skipped_text:
        coverage_bits.append(f"已跳过：{skipped_text}")
    coverage_text = "；".join(coverage_bits)
    principles = "".join(
        f'<article class="context-principle"><h3>{esc(item["principle"])}</h3>'
        f'<p>{esc(item["planning_use"])}</p><p class="muted">边界：{esc(item["boundary"])}</p>'
        f'<a href="{esc(item["source"]["url"])}" target="_blank" rel="noreferrer">{esc(item["source"]["organization"])} · {esc(item["source"]["title"])}</a></article>'
        for item in context["professional_basis"]["principles"]
    ) or '<p class="muted">当前安全范围没有可用于处方型规划的知识片段。</p>'
    longitudinal = context["longitudinal"]
    return (
        '<section class="section-header"><div>'
        f'<h1>这次模型实际看到了什么</h1><p class="muted">不是把全部历史倾倒给模型，而是只选择会改变 {esc(work_date)} 判断的事实、模式和专业边界。</p></div>'
        f'<a class="button secondary" href="/agent/context/{esc(work_date)}?format=json">导出规划资料</a></section>'
        f'<div class="context-grid"><section class="panel"><h2>为什么选入</h2><ul class="context-list">{included}</ul></section>'
        f'<section class="panel"><h2>为什么没有选入</h2><ul class="context-list">{excluded}</ul></section></div>'
        f'<section class="panel"><p class="eyebrow">已确认</p><h2>你的目标与边界</h2><p><strong>当前饮食边界：</strong>{esc((person.get("safety") or {}).get("mode"))}</p>'
        f'<h3>目标</h3><ul>{goals}</ul><h3>本次生效的用户模型</h3><ul class="context-list">{claims}</ul></section>'
        f'<section class="panel"><p class="eyebrow">今天</p><h2>当天事实与已经确定的安排</h2><p><strong>逐餐方式：</strong>{esc(mode_text)}</p>'
        f'<p><strong>状态覆盖：</strong>{esc(coverage_text)}</p><ul class="context-list">{records}</ul></section>'
        f'<section class="panel"><p class="eyebrow">相关经历</p><h2>这次用到的过往情况</h2><p>执行反馈 {len(longitudinal.get("selected_execution_feedback") or [])} 条；'
        f'近期菜单语义 {len(longitudinal.get("recent_meal_semantics") or [])} 条；承接食材 {len(longitudinal.get("ingredient_carryover") or [])} 条。</p></section>'
        f'<section class="panel"><p class="eyebrow">饮食依据</p><h2>本次采用的专业原则</h2>'
        f'<div class="context-principles">{principles}</div></section>'
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


def render_plan_page(
    plan_date: str,
    *,
    feedback_draft: dict | None = None,
    feedback_error: str = "",
) -> str:
    plan = adaptive.get_plan_for_date(plan_date)
    if not plan:
        return (
            f'<section class="panel"><h1>{esc(plan_date)} 还没有安排</h1>'
            '<p>先记下当天情况，MealCircuit会据此准备合适的安排。</p>'
            '<a class="button" href="/#record">去记一笔</a></section>'
        )
    try:
        source_review = service.get_daily_review(plan["review_date"])
        source_result = source_review.get("result_json") or {}
    except KeyError:
        source_result = {}
    rationale_items = source_result.get("planning_rationale") or [
        item.get("why_today") or item.get("purpose")
        for item in plan["menu"].get("meals") or []
        if item.get("why_today") or item.get("purpose")
    ]
    reference_items = source_result.get("evidence_summary") or []
    explanation = ""
    if rationale_items or reference_items:
        rationale = "".join(f'<li>{esc(item)}</li>' for item in rationale_items)
        references = "".join(f'<li>{esc(item)}</li>' for item in reference_items)
        explanation = (
            '<section class="panel plan-explanation">'
            + (f'<details><summary>为什么这样安排</summary><ul>{rationale}</ul></details>' if rationale else '')
            + (f'<details><summary>这次参考了什么</summary><ul>{references}</ul></details>' if references else '')
            + '</section>'
        )
    cards = []
    for meal in plan["menu"]["meals"]:
        feedback = plan["feedback"].get(meal["plan_item_id"])
        draft = feedback_draft if feedback_draft and feedback_draft.get("plan_item_id") == meal["plan_item_id"] else None
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
        eat_out = meal.get("eat_out_guidance") or {}
        eat_out_html = (
            '<div class="notice"><strong>外食选择</strong>'
            f'<p>蛋白：{esc(eat_out.get("protein_anchor"))}</p>'
            f'<p>主食：{esc(eat_out.get("staple"))}；蔬菜：{esc(eat_out.get("vegetables"))}</p>'
            f'<p>酱汁：{esc(eat_out.get("sauce_rule"))}；备选：{esc(eat_out.get("fallback"))}</p></div>'
            if eat_out else ""
        )
        purpose_html = (
            f'<div class="meal-purpose"><strong>这顿要解决什么</strong><p>{esc(meal.get("purpose"))}</p>'
            f'<p class="muted">{esc(meal.get("why_today") or meal.get("whole_day_role") or "")}</p></div>'
            if meal.get("purpose") else ""
        )
        portion_contract_html = "".join(
            f'<li><strong>{esc(item.get("item"))}</strong> · '
            f'{esc("–".join(str(value) for value in item["gram_range"]) + "g" if item.get("gram_range") else "克数未知")} · '
            f'{esc({"raw":"生重","cooked":"熟重","as_served":"上桌重量","not_applicable":"不适用"}.get(item.get("measurement_basis"), item.get("measurement_basis")))} · '
            f'{esc(item.get("household_measure"))}<br><span class="muted small">吃不饱：{esc(item.get("increase_if"))}；食欲低：{esc(item.get("decrease_if"))}</span></li>'
            for item in meal.get("portion_contracts") or []
        )
        current_status = str(draft.get("status") if draft else (feedback.get("status") if feedback else ""))
        status_options = "".join(f'<option value="{key}"{_selected(current_status,key)}>{label}</option>' for key,label in FEEDBACK_LABELS.items())
        current_outcome = dict((feedback.get("outcome_json") or {}) if feedback else {})
        if draft:
            if draft.get("satiety"):
                current_outcome["satiety"] = draft["satiety"]
            else:
                current_outcome.pop("satiety", None)
            if draft.get("photo_task_ids"):
                current_outcome["photo_task_ids"] = draft["photo_task_ids"]
            elif draft.get("photo_task_id"):
                current_outcome["photo_task_id"] = draft["photo_task_id"]
        current_satiety = current_outcome.get("satiety") or ""
        photo_task_ids = []
        raw_photo_task_ids = current_outcome.get("photo_task_ids")
        if isinstance(raw_photo_task_ids, list):
            photo_task_ids.extend(str(item) for item in raw_photo_task_ids if item)
        if current_outcome.get("photo_task_id"):
            photo_task_ids.append(str(current_outcome["photo_task_id"]))
        photo_task_ids = list(dict.fromkeys(photo_task_ids))
        feedback_photos = []
        for index, photo_task_id in enumerate(photo_task_ids, start=1):
            try:
                photo_task = service.get_task(str(photo_task_id))
            except KeyError:
                photo_task = None
            if photo_task and photo_task.get("image_path"):
                feedback_photos.append(
                    '<figure class="feedback-photo"><img src="/media/'
                    f'{esc(Path(photo_task["image_path"]).name)}" alt="{esc(meal.get("name") or "这一餐")}的实际照片">'
                    f'<figcaption>实际照片 {index}</figcaption></figure>'
                )
        feedback_photo_html = (
            f'<div class="feedback-photos">{"".join(feedback_photos)}</div>'
            if feedback_photos else ""
        )
        satiety_options = "".join(
            f'<option value="{key}"{_selected(current_satiety,key)}>{label}</option>'
            for key, label in (
                ("as_planned", "刚好"), ("not_enough", "没吃饱"), ("too_much", "吃不完 / 太多")
            )
        )
        current_reasons = list(draft.get("reason_codes") or []) if draft else (feedback.get("reason_codes_json") or [] if feedback else [])
        reason_checks = "".join(f'<label class="choice-row compact"><input type="checkbox" name="reason_codes" value="{key}"{_checked(current_reasons,key)}><span>{label}</span></label>' for key,label in REASON_LABELS.items())
        version = draft.get("expected_version", 0) if draft else (feedback.get("version", 0) if feedback else 0)
        actual_text = str(draft.get("actual_text") if draft else (feedback.get("actual_text", "") if feedback else ""))
        inline_error = (
            f'<div class="form-error" role="alert" tabindex="-1" data-feedback-error><strong>还差一项</strong><p>{esc(feedback_error)}</p></div>'
            if draft and feedback_error else ""
        )
        photo_task_fields = "".join(
            f'<input type="hidden" name="photo_task_ids" value="{esc(photo_task_id)}">'
            for photo_task_id in photo_task_ids
        )
        slot_label = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"}.get(
            meal.get("slot") or meal.get("meal_type"), meal.get("slot") or meal.get("meal_type") or ""
        )
        cards.append(f'''<article class="plan-card"><div class="section-header"><div><span class="subtle-label">{esc(slot_label)}</span><h2>{esc(meal.get('name') or '这一餐')}</h2></div>{f'<span class="status completed">{esc(FEEDBACK_LABELS.get(current_status,current_status))}</span>' if feedback else ''}</div>
        {purpose_html}{f'<p><strong>方式：</strong>{esc(MEAL_MODE_LABELS.get(meal.get("mode"), meal.get("mode") or ""))}</p>' if meal.get('mode') else ''}{f'<p><strong>份量：</strong>{esc(meal.get("portion_guidance"))}</p>' if meal.get('portion_guidance') else ''}{f'<h3>具体吃多少</h3><ul class="portion-list">{portion_contract_html}</ul>' if portion_contract_html else ''}{eat_out_html}{f'<p class="muted">{esc(execution_html)}</p>' if execution_html else ''}{f'<h3>{"可选食物" if meal.get("mode")=="eat_out" else "食材"}</h3><ul>{detail}</ul>' if detail else ''}{f'<h3>执行步骤</h3><ol>{step_html}</ol>' if step_html else ''}
        {feedback_photo_html}<details class="feedback-box"{' open' if not feedback or draft else ''}><summary>{'修改这次记录' if feedback else '吃得怎么样？'}</summary><form method="post" enctype="multipart/form-data" action="/plans/{esc(plan_date)}/{esc(meal['plan_item_id'])}/feedback" data-plan-feedback><input type="hidden" name="expected_version" value="{version}">{photo_task_fields}<div data-feedback-error-slot>{inline_error}</div><label>实际情况<select name="status" required><option value="">请选择</option>{status_options}</select></label><label>份量感觉<select name="satiety"><option value="">未记录</option>{satiety_options}</select></label><fieldset><legend>如果有变化，原因是什么？</legend><div class="option-grid">{reason_checks}</div></fieldset><label>实际怎么吃的（可选）<textarea name="actual_text" maxlength="2000">{esc(actual_text)}</textarea></label><label for="feedback-photo-{esc(meal['plan_item_id'])}">{'继续添加实际照片（可选）' if feedback_photo_html else '实际照片（可选）'}</label><input id="feedback-photo-{esc(meal['plan_item_id'])}" type="file" name="photo" accept="image/jpeg,image/png,image/gif,image/webp" multiple><p class="muted small">可以一次选择多张，也可以之后继续添加；照片会和这顿的实际情况一起保存。</p><button type="submit">记下来</button></form></details>
        {f'<form class="rescue-form" method="post" action="/rescue/start"><input type="hidden" name="plan_date" value="{esc(plan_date)}"><input type="hidden" name="plan_item_id" value="{esc(meal["plan_item_id"])}"><label>临时有变化<select name="issue_code">{"".join(f"<option value={key!r}>{label}</option>" for key,label in RESCUE_LABELS.items())}</select></label><input name="input_text" aria-label="补充当前情况" placeholder="可以补充手边的食材或时间"><button class="secondary" type="submit">帮我调整这一餐</button></form>' if plan.get('scope_current') else ''}</article>''')
    stale = '' if plan.get('scope_current') else '<div class="quiet-note panel" role="note"><strong>这是过去的安排</strong><p>仍可以补记实际情况，但不会再按它调整今天。</p></div>'
    return (
        f'<section class="section-header"><div><h1>{esc(plan_date)} 的安排</h1>'
        '<p class="muted">每餐都可以按当天食欲和实际条件调整。</p></div>'
        '<a class="button secondary" href="/plans">返回计划</a></section>'
        f'{stale}{explanation}<div class="plan-list">{"".join(cards)}</div>'
    )


def _plan_names(plan: dict | None) -> str:
    if not plan:
        return ""
    return "、".join(
        str(item.get("name") or item.get("slot") or "一餐")
        for item in plan["menu"].get("meals") or []
    )


def render_plans_hub() -> str:
    today = service.configured_today()
    today_key = today.isoformat()
    tomorrow_key = (today + timedelta(days=1)).isoformat()
    today_plan = adaptive.get_plan_for_date(today_key)
    tomorrow_plan = adaptive.get_plan_for_date(tomorrow_key)
    workspace = agent_workspace.get_workspace_state(today_key)
    draft = workspace.get("draft") or {}

    today_card = (
        f'<section class="panel plan-overview-card"><div><h2>今天</h2><p>{esc(_plan_names(today_plan))}</p></div>'
        f'<a class="button" href="/plans/{esc(today_key)}">查看今天安排</a></section>'
        if today_plan else
        '<section class="panel plan-overview-card"><div><h2>今天</h2><p>还没有安排，先记下今天的情况。</p></div>'
        '<a class="button secondary" href="/#record">去记一笔</a></section>'
    )
    if tomorrow_plan:
        tomorrow_card = (
            f'<section class="panel plan-overview-card"><div><h2>明天</h2><p>{esc(_plan_names(tomorrow_plan))}</p></div>'
            f'<a class="button" href="/plans/{esc(tomorrow_key)}">查看明天安排</a></section>'
        )
    elif draft.get("status") == "ready_draft":
        tomorrow_card = (
            '<section class="panel plan-overview-card"><div><h2>明天</h2><p>草案已经准备好，确认后才会成为正式安排。</p></div>'
            '<a class="button" href="/#tomorrow-plan">查看草案</a></section>'
        )
    else:
        tomorrow_card = (
            '<section class="panel plan-overview-card"><div><h2>明天</h2><p>还没有准备好，新的记录会自动参与下一次调整。</p></div>'
            '<a class="button secondary" href="/">回到今天</a></section>'
        )
    history = service.list_daily_reviews()
    history_link = (
        f'<p>可以回看最近 {len(history)} 天的复盘和安排。</p>' if history else '<p>还没有历史安排。</p>'
    )
    return (
        '<section class="section-header"><div><h1>计划</h1><p class="muted">今天怎么吃、明天怎么安排，都在这里。</p></div></section>'
        f'<div class="plan-overview">{today_card}{tomorrow_card}</div>'
        f'<section class="panel"><div class="section-header"><div><h2>过去的安排</h2>{history_link}</div>'
        '<a class="button secondary" href="/history">查看历史</a></div></section>'
    )


def render_questions_page(question_date: str) -> str:
    pending = adaptive.schedule_questions(question_date)
    answered_modes = [
        item for item in adaptive.question_events_for_date(question_date, include_pending=False)
        if item.get("question_key") == "tomorrow_meal_modes" and item.get("status") == "answered"
    ]
    if answered_modes and not any(item.get("question_key") == "tomorrow_meal_modes" for item in pending):
        pending.append(answered_modes[-1])
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
        elif schema.get("kind") == "meal_mode_overrides":
            labels = {"inherit": "沿用个人默认", "home_cook": "在家下厨", "quick_assembly": "快速组装", "eat_out": "外食"}
            current = item.get("answer_json") or {}
            selects = "".join(
                f'<label>{meal_name}<select name="{key}_mode">'
                + "".join(f'<option value="{mode}"{_selected(current.get(key, "inherit"), mode)}>{labels[mode]}</option>' for mode in schema.get("options", []))
                + '</select></label>'
                for key, meal_name in (("breakfast", "早餐"), ("lunch", "午餐"), ("dinner", "晚餐"))
            )
            control = f'<div class="row">{selects}</div><button>{"更新" if item.get("status") == "answered" else "保存"}明日逐餐安排</button>'
        else:
            options = "".join(f'<label class="choice-row"><input type="radio" name="answer" value="{esc(value)}" required><span>{esc(choice_labels.get(value,value))}</span></label>' for value in schema.get("options", []))
            control = f'<div class="option-list">{options}</div><button>保存答案</button>'
        skip_form = '' if item.get("status") == "answered" else f'<form method="post" action="/questions/{esc(item["id"])}/skip"><input type="hidden" name="version" value="{esc(item["version"])}"><button class="link-button" type="submit">暂时跳过</button></form>'
        cards.append(f'''<article class="panel question-card"><h2>{esc(item.get('prompt') or item['reason'])}</h2><p>{esc(item['reason'])}</p><p class="muted">这会影响：{esc(item['expected_impact'])}</p><form method="post" action="/questions/{esc(item['id'])}/answer"><input type="hidden" name="version" value="{esc(item['version'])}">{control}</form>{skip_form}</article>''')
    return f'<section class="section-header"><div><h1>还有几件事想确认</h1><p class="muted">只会询问真正影响行动的信息；跳过就保持未知。</p></div></section><div class="question-list">{"".join(cards)}</div>'


def render_learning_page() -> str:
    claims = [
        item for item in agent_workspace.list_claims(include_inactive=True)
        if item.get("status") in {"active", "pending_confirmation"}
    ]
    reflection = agent_workspace.reflection_status()
    ai_status = ai.ai_status()
    reflection_html = ""
    if reflection["due"]:
        can_reflect = all(ai_status[key] for key in ("provider_valid", "model_configured", "key_configured"))
        action = (
            '<form method="post" action="/learning/reflect"><button>整理最近的反馈</button></form>'
            if can_reflect else '<a class="button secondary" href="/me#advanced">先开启智能规划</a>'
        )
        reflection_html = (
            '<section class="panel reflection-status"><div><h2>最近有一些新变化</h2>'
            '<p>可以把近期的执行情况整理成更准确的偏好和需要。</p></div>'
            f'{action}</section>'
        )

    cards = []
    for item in claims:
        evidence = "".join(
            f'<li>{esc(value.get("excerpt") or "来自一次真实记录")}'
            f'<span class="muted small">{esc(value["observed_at"])}</span></li>'
            for value in item.get("evidence") or [] if value.get("active", 1)
        ) or '<li class="muted">还没有可以展示的记录</li>'
        state_label = "想和你确认" if item["status"] == "pending_confirmation" else "正在用于安排"
        if item["risk_level"] == "high":
            options = (
                ("", "选择怎么处理"),
                ("correct", "不对，留下正确情况"),
                ("pause", "暂时别用"),
                ("forget", "忘记这条理解"),
            )
            guidance = '<p class="muted">涉及目标或健康边界的内容，需要到“目标与饮食偏好”中确认。</p>'
        else:
            options = (
                ("", "选择怎么处理"),
                ("confirm", "对"),
                ("correct", "不对"),
                ("today", "只适用于今天"),
                ("stable", "以后记住"),
                ("pause", "暂时别用"),
                ("forget", "忘记"),
            )
            guidance = ""
        select = "".join(f'<option value="{key}">{label}</option>' for key, label in options)
        cards.append(
            f'<article class="panel user-claim" id="claim-{esc(item["id"])}"><div class="section-header"><div><span class="subtle-label">{esc(state_label)}</span>'
            f'<h2>{esc(item["statement"])}</h2></div></div>{guidance}'
            f'<details><summary>为什么这样认为</summary><ul class="plain-summary">{evidence}</ul></details>'
            f'<details><summary>调整这条理解</summary><form method="post" action="/learning/claims/{esc(item["id"])}/action">'
            f'<label>怎么处理<select name="action" required>{select}</select></label>'
            '<label>如果不对，实际情况是什么？<textarea name="correction"></textarea></label>'
            '<button>保存</button></form></details></article>'
        )
    content = "".join(cards) or (
        '<section class="panel"><h2>还没有需要你处理的理解</h2>'
        '<p>MealCircuit会先从真实执行中观察，只有需要确认或已经影响安排时才会显示在这里。</p></section>'
    )
    return (
        '<section class="section-header"><div><h1>MealCircuit了解的你</h1>'
        '<p class="muted">这些理解会影响份量、口味、复杂度和沟通方式，你可以随时纠正。</p></div>'
        '<a class="button secondary" href="/me">返回我的</a></section>'
        f'{reflection_html}<div class="claim-list">{content}</div>'
    )


def render_inventory_page() -> str:
    items = adaptive.list_inventory(active_only=False)
    rows = "".join(
        f'''<tr><td><strong>{esc(item['name'])}</strong><br><span class="muted small">{esc(item.get('amount_text') or '数量不确定')}</span></td><td>{esc(item.get('expires_on') or '未填写')}</td><td>{esc(INVENTORY_STATUS_LABELS.get(item['status'], item['status']))}</td><td><form class="inline-form" method="post" action="/inventory/{esc(item['id'])}"><input type="hidden" name="version" value="{esc(item['version'])}"><input name="amount_text" value="{esc(item.get('amount_text') or '')}" aria-label="更新数量"><select name="status" aria-label="更新状态">{''.join(f'<option value="{state}"{_selected(item["status"],state)}>{INVENTORY_STATUS_LABELS.get(state,state)}</option>' for state in sorted(adaptive.INVENTORY_STATUSES))}</select><button>更新</button></form></td></tr>'''
        for item in items
    ) or '<tr><td colspan="4" class="muted">还没有库存记录</td></tr>'
    return f'''<section class="section-header"><div><h1>家里有什么</h1><p class="muted">记下现有食材和大概数量，安排时会优先考虑临期和剩余食材。</p></div><a class="button secondary" href="/me">返回我的</a></section><section class="panel"><form class="inventory-add" method="post" action="/inventory"><label>食材名称<input name="name" required></label><label>大概数量<input name="amount_text"></label><label>大约什么时候要吃完<input type="date" name="expires_on"></label><button>加入库存</button></form><div class="table-scroll"><table><thead><tr><th>食材</th><th>期限</th><th>现在还有吗</th><th>更新</th></tr></thead><tbody>{rows}</tbody></table></div></section>'''


def render_profile_page() -> str:
    current = personalization.active_personalization()
    if current["status"] == "setup_required":
        return render_setup_start(personalization.onboarding_status())
    goals = "".join(f'<li>{esc(item["goal_json"].get("custom_label") or GOAL_LABELS.get(item["goal_json"].get("type"), item["goal_json"].get("type")))}</li>' for item in current["goals"])
    target_rows = []
    for item in current["targets"]:
        value = item["value_json"]
        if isinstance(value, list) and len(value) == 2:
            value_text = f'{value[0]}–{value[1]} {item.get("unit") or ""}'.strip()
        else:
            value_text = str(value)
        target_rows.append(
            f'<li><strong>{esc(TARGET_LABELS.get(item["target_key"], item["target_key"]))}</strong>'
            f'<span>{esc(value_text)}</span></li>'
        )
    targets = "".join(target_rows) or '<li class="muted">当前没有需要展示的数值目标</li>'
    strategy = (current.get("strategy") or {}).get("strategy_json") or {}
    meal_modes = strategy.get("meal_environment") or {}
    if isinstance(meal_modes, dict):
        mode_summary = "、".join(
            f'{label}：{MEAL_MODE_LABELS.get(meal_modes.get(key), meal_modes.get(key) or "未设置")}'
            for key, label in (("breakfast", "早餐"), ("lunch", "午餐"), ("dinner", "晚餐"))
        )
    else:
        mode_summary = "按当前个人设置安排"
    safety_text = SAFETY_MODE_LABELS.get(current["safety"]["mode"], "按当前健康边界安排")
    constraints = ((current.get("profile") or {}).get("profile_json") or {}).get("constraints") or {}
    value_labels = {
        "health": "健康与安全", "training": "训练与恢复", "satiety": "吃得饱",
        "budget": "长期负担得起", "time": "不占用太多时间", "taste": "合口味",
        "social": "保留社交弹性", "convenience": "足够方便",
    }
    non_negotiables = "、".join(
        value_labels.get(item, item) for item in constraints.get("non_negotiables") or []
    ) or "暂未指定"
    priorities = " → ".join(
        value_labels.get(item, item) for item in constraints.get("priority_tradeoffs") or []
    ) or "根据当天情况协商"
    return f'''<section class="section-header"><div><h1>目标与饮食偏好</h1><p class="muted">这里决定MealCircuit长期怎样为你安排；临时变化直接在“今天”里记录。</p></div><div class="actions"><form method="post" action="/setup/start"><button>修改设置</button></form><a class="button secondary" href="/me">返回我的</a></div></section><div class="grid"><section class="panel"><h2>你想达成什么</h2><ol>{goals}</ol></section><section class="panel"><h2>不能为了目标牺牲什么</h2><p>{esc(non_negotiables)}</p><p class="muted">发生冲突时：{esc(priorities)}</p></section><section class="panel"><h2>需要注意的边界</h2><p>{esc(safety_text)}</p></section><section class="panel"><h2>每天通常怎么吃</h2><p>{esc(mode_summary)}</p></section><section class="panel"><h2>当前营养目标</h2><ul class="profile-summary">{targets}</ul></section></div>'''


def render_me_page() -> str:
    current = personalization.active_personalization()
    if current["status"] == "setup_required":
        return render_setup_start(personalization.onboarding_status())
    snapshot = adaptive.calibration_snapshot()
    if snapshot["eligible_for_weight_calibration"]:
        progress = (
            '<section class="panel" id="progress"><h2>最近的进展</h2>'
            '<p>现有记录已经可以形成一段可比趋势。MealCircuit不会自行改变目标，需要调整时仍会先和你确认。</p></section>'
        )
    elif snapshot["eligible_for_strategy_review"]:
        progress = (
            '<section class="panel" id="progress"><h2>最近的进展</h2>'
            '<p>近期执行情况已经足够帮助MealCircuit调整做法，例如份量、复杂度和备选方案。</p></section>'
        )
    else:
        progress = ""
    return (
        '<section class="section-header"><div><h1 data-i18n="page.me">我的</h1><p class="muted" data-i18n="me.description">长期目标、偏好和设备设置都放在这里。</p></div></section>'
        '<div class="me-grid">'
        '<a class="me-card" href="/profile"><h2 data-i18n="me.profile">目标与饮食偏好</h2><p data-i18n="me.profile.help">目标、用餐方式和需要注意的健康边界。</p></a>'
        '<a class="me-card" href="/learning"><h2 data-i18n="me.learning">MealCircuit了解的你</h2><p data-i18n="me.learning.help">查看正在影响安排的偏好和需要。</p></a>'
        '<a class="me-card" href="/inventory"><h2 data-i18n="me.inventory">库存与常用食物</h2><p data-i18n="me.inventory.help">家里有什么、哪些食材需要优先吃。</p></a>'
        '</div>'
        f'{progress}'
        '<section class="panel interface-settings"><h2 data-i18n="settings.appearance">外观与语言</h2><p class="muted" data-i18n="settings.appearance.help">这些偏好保存在当前设备上。</p><div class="settings-list">'
        '<label class="settings-row"><span data-i18n="settings.theme">主题</span><select data-theme-select><option value="system" data-i18n="settings.theme.system">跟随系统</option><option value="light" data-i18n="settings.theme.light">浅色</option><option value="dark" data-i18n="settings.theme.dark">深色</option></select></label>'
        '<label class="settings-row"><span data-i18n="settings.language">语言</span><select data-language-select><option value="en" data-i18n="settings.language.en">English</option><option value="zh-CN" data-i18n="settings.language.zh">简体中文</option></select></label>'
        '</div></section>'
        '<details class="panel advanced-settings" id="advanced"><summary data-i18n="settings.advanced">高级设置</summary><div class="settings-links">'
        '<a href="/ai"><strong data-i18n="settings.ai">智能规划设置</strong><span data-i18n="settings.ai.help">连接模型和调整生成方式</span></a>'
        '<a href="/sync"><strong data-i18n="settings.sync">同步与设备</strong><span data-i18n="settings.sync.help">在自己的设备之间同步</span></a>'
        '<a href="/data"><strong data-i18n="settings.backup">备份与迁移</strong><span data-i18n="settings.backup.help">导出、恢复或迁移本地数据</span></a>'
        '<a href="/foods"><strong data-i18n="settings.foods">食品营养库</strong><span data-i18n="settings.foods.help">维护包装食品和常用原料</span></a>'
        '</div></details>'
    )


def render_data_page(message: str = "") -> str:
    message_html = f'<div class="quiet-success panel" role="status">{esc(message)}</div>' if message else ""
    return f'''<section class="section-header"><div><h1>备份、恢复与设备迁移</h1><p class="muted">可以下载完整备份，也可以把以前的备份恢复到这台设备。智能规划密钥不会包含在备份中。</p></div><a class="button secondary" href="/me#advanced">返回设置</a></section>{message_html}<div class="grid"><section class="panel"><h2>下载备份</h2><p>包含记录、计划、设置和本地照片。</p><a class="button" href="/data/export">生成并下载</a></section><section class="panel"><h2>从备份恢复</h2><p>恢复前会先检查文件，并自动保存当前数据的备份副本。</p><form method="post" enctype="multipart/form-data" action="/data/import"><label>MealCircuit备份文件<input type="file" name="bundle" accept="application/zip,.zip" required></label><label class="choice-row"><input type="checkbox" name="confirm_restore" value="yes" required><span>我确认用这个备份替换当前数据</span></label><button class="danger" type="submit">检查并恢复</button></form></section></div>'''


def render_rescue_page(rescue_id: str) -> str:
    session = adaptive.get_rescue_session(rescue_id)
    if session["status"] == "completed":
        result = session.get("result_json") or {}
        steps = "".join(f'<li>{esc(item)}</li>' for item in result.get("steps") or [])
        replacements = "、".join(result.get("replacement_foods") or [])
        safety_notes = "".join(f'<li>{esc(item)}</li>' for item in result.get("safety_notes") or [])
        replacements_html = f'<p><strong>替代食材：</strong>{esc(replacements)}</p>' if replacements else ""
        portion_html = f'<p><strong>份量变化：</strong>{esc(result.get("portion_change"))}</p>' if result.get("portion_change") else ""
        safety_html = f'<h2>安全提示</h2><ul>{safety_notes}</ul>' if safety_notes else ""
        plan_url = f'/plans/{esc(session["plan_date"])}'
        return f'<section class="panel"><h1>这一餐可以这样调整</h1><p>{esc(result.get("reason") or "")}</p>{replacements_html}{portion_html}<h2>现在这样做</h2><ol>{steps}</ol>{safety_html}<a class="button" href="{plan_url}">回到今天安排</a></section>'
    policy = personalization.generation_policy("rescue")
    control = (
        f'<form method="post" action="/rescue/{esc(rescue_id)}/generate"><button>用当前模型生成救场方案</button></form>'
        if policy["allowed"] else f'<div class="form-error" role="alert"><strong>当前不能生成救场建议</strong><p>{esc(policy["reason"])}</p></div>'
    )
    return f'''<section class="panel"><h1>只调整当前这一餐</h1><p>现在遇到的问题：{esc(RESCUE_LABELS.get(session['issue_code'],session['issue_code']))}</p><p class="muted">{esc(session.get('input_text') or '没有额外补充')}</p>{control}<p class="muted small">其他餐次不会被改动。</p><a class="button secondary" href="/plans/{esc(session['plan_date'])}">返回今天安排</a></section>'''


def layout(title: str, body: str) -> bytes:
    today = service.configured_today()
    nav_items = (
        ("/", "今天", "dashboard", title in {"今天", "今日状态", "状态问答", "状态设置"}),
        ("/plans", "计划", "advice", title in {"计划", "执行计划", "历史建议", "每日复盘", "今日建议与明日菜单"}),
        ("/me", "我的", "settings", title in {
            "我的", "MealCircuit了解的你", "学习确认", "库存", "目标与边界", "初始化",
            "API 接入", "智能规划设置", "同步与设备", "备份与迁移",
            "食品营养库", "新增食品", "编辑食品",
        }),
    )
    links = []
    for href, item_label, icon_name, current in nav_items:
        current_attr = ' aria-current="page"' if current else ""
        nav_key = "today" if href == "/" else "plans" if href == "/plans" else "me"
        links.append(
            f'<a class="nav-link" href="{href}"{current_attr} title="{esc(item_label)}" data-i18n-label="nav.{nav_key}">'
            f'{icon(icon_name)}<span class="nav-label" data-i18n="nav.{nav_key}">{esc(item_label)}</span></a>'
        )
    nav_sections = [f'<section class="nav-group primary-nav" aria-label="主要页面" data-i18n-label="nav.primary">{"".join(links)}</section>']
    page_titles = {
        "今天": "今天", "计划": "计划", "执行计划": "计划", "记录": "今天", "洞察": "我的",
        "我的": "我的", "MealCircuit了解的你": "MealCircuit了解的你", "学习确认": "MealCircuit了解的你",
        "库存": "库存", "目标与边界": "目标与饮食偏好", "初始化": "初始化",
        "今日建议与明日菜单": "今日建议", "每日复盘": "每日复盘",
        "今日状态": "今日状态", "状态问答": "状态问答", "状态设置": "状态设置",
        "上传食物照片": "照片任务", "原材料分析": "原材料分析", "任务列表": "全部任务",
        "任务详情": "任务详情", "API 接入": "智能规划设置", "智能规划设置": "智能规划设置",
        "同步与设备": "同步与设备", "备份与迁移": "备份与迁移", "食品营养库": "食品营养库", "新增食品": "新增食品",
        "编辑食品": "编辑食品", "历史建议": "历史建议", "记录与记忆": "记录与记忆",
        "操作失败": "操作失败", "未找到": "未找到",
    }
    top_action = "" if title == "初始化" else (
        f'<a class="button" href="/#record" aria-label="记一笔" title="记一笔" data-i18n-label="action.record">'
        f'{icon("checkin")}<span data-i18n="action.record">记一笔</span></a>'
    )
    date_label = f"{today.month}月{today.day}日 周{'一二三四五六日'[today.weekday()]}"
    try:
        sync_enabled = bool(sync.sync_status().get("enabled"))
    except Exception:
        sync_enabled = False
    storage_label = "本地优先 · 同步已启用" if sync_enabled else "仅存于本机"
    topbar_i18n = ' data-i18n="page.me"' if page_titles.get(title, title) == "我的" else ""
    body_title_i18n = ' data-i18n-title="title.me"' if title == "我的" else ""
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · MealCircuit</title><link rel="icon" href="/assets/ui/favicon.svg" type="image/svg+xml"><script src="/assets/ui/theme-init.js?v=20260716a"></script><link rel="stylesheet" href="/assets/ui/app.css?v=20260716a"><script src="/assets/ui/app.js?v=20260716a" defer></script></head><body{body_title_i18n}>
    <a class="skip-link" href="#main-content" data-i18n="skip.main">跳到主要内容</a>
    <div class="app-shell"><aside class="app-sidebar" id="app-sidebar" aria-label="主导航"><a class="sidebar-brand" href="/">MealCircuit</a><nav class="sidebar-nav">{"".join(nav_sections)}</nav><div class="sidebar-footer"><button class="icon-button" type="button" data-nav-collapse aria-label="收起侧栏" title="收起侧栏" data-i18n-label="nav.collapse">{icon("collapse")}</button></div></aside>
    <button class="nav-scrim" type="button" data-nav-close aria-label="关闭导航" data-i18n-label="nav.close"></button>
    <header class="app-topbar"><div class="topbar-start"><button class="icon-button mobile-menu" type="button" data-nav-open aria-controls="app-sidebar" aria-expanded="false" aria-label="打开导航" data-i18n-label="nav.open">{icon("menu")}</button><p class="topbar-title"{topbar_i18n}>{esc(page_titles.get(title, title))}</p></div><div class="topbar-end"><button class="icon-button theme-toggle" type="button" data-theme-toggle aria-label="切换到浅色主题" title="切换到浅色主题" hidden><span class="icon icon-sun theme-target-light" aria-hidden="true"></span><span class="icon icon-moon theme-target-dark" aria-hidden="true"></span></button><span class="utility muted" data-local-date="{today.isoformat()}">{date_label}</span><span class="local-status">{icon("local")}<span data-i18n="storage.{"sync" if sync_enabled else "local"}">{esc(storage_label)}</span></span>{top_action}</div></header>
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


def render_provenance_warning(provenance: dict | None) -> str:
    if not provenance or not provenance.get("stale"):
        return ""
    return (
        '<aside class="panel error" role="status"><strong>这份结果使用的是以前的设置</strong>'
        '<p>原内容会保留；需要时可以按现在的情况重新准备。</p></aside>'
    )


def render_review_cards(reviews: list[dict]) -> str:
    status_labels = {
        "stable": "稳定", "observe": "观察", "adjust": "需调整", "risk": "风险",
        "pending": "还在准备",
    }
    cards = []
    for review in reviews:
        result = review.get("result_json") or {}
        completed = review.get("status") == "completed" and bool(result)
        signal = result.get("system_status", "pending") if completed else "pending"
        summary = result.get("one_line_review") or "记录已经记下，复盘还在准备。"
        advice_items = result.get("core_advice") or []
        advice = advice_items[0] if advice_items else "准备好后，这里会显示最重要的一条建议。"
        menu = result.get("tomorrow_menu") or {}
        menu_date = menu.get("date")
        meta = f'第二天的安排 · {esc(menu_date)}' if menu_date else "复盘还在准备"
        review_date = esc(review["review_date"])
        cards.append(
            f'<article class="review-card" data-status="{esc(signal)}">'
            f'<header class="review-card__top"><time class="review-date" datetime="{review_date}">{review_date}</time>'
            f'<span class="review-signal">{esc(status_labels.get(signal, signal))}</span></header>'
            f'<p class="review-summary">{esc(summary)}</p><p class="review-advice">{esc(advice)}</p>'
            f'<footer class="review-card__footer"><span class="review-meta">{meta}</span>'
            f'<a class="review-link" href="/reviews/{review_date}">打开复盘 {icon("chevron")}</a></footer></article>'
        )
    return '<div class="review-grid">' + ("".join(cards) or '<p class="review-empty">还没有过去的安排。每天的复盘会按日期出现在这里。</p>') + "</div>"


def render_checkin_callout(checkin_date: str) -> str:
    state = service.get_checkin_state(checkin_date)
    coverage = state["coverage"]
    due, handled = coverage["due"], coverage["handled"]
    label = "今天的状态"
    action = "查看或补充" if handled else "补充状态"
    return (
        f'<section class="card"><div class="section-header"><div><h2>{esc(label)}</h2>'
        '<p class="muted">补充会影响份量、训练恢复或饮食安排的信息。</p></div>'
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


def render_checkin_hub(checkin_date: str) -> str:
    state = service.get_checkin_state(checkin_date)
    daily = service.daily_state(checkin_date)
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
        f'<section class="checkin-hero"><div><h1>今天的状态</h1>'
        f'<p class="muted">{esc(checkin_date)} · 每次只回答一个问题，途中退出也会保留。</p></div></section>'
        + ('<ol class="signal-list">' + "".join(cards) + "</ol>" if cards else empty)
        + f'<div class="actions"><a class="button secondary" href="/check-ins/settings">调整模块</a>'
        f'{review_link}</div>'
    )


def _question_value(module: dict, question_id: str):
    return (module.get("active_answers") or {}).get(question_id)


def _next_checkin_step(checkin_date: str) -> str:
    state = service.get_checkin_state(checkin_date)
    pending = next(
        (
            module for module in state["modules"]
            if module["enabled"]
            and (module["status"] not in {"completed", "skipped"} or module.get("has_draft"))
        ),
        None,
    )
    if pending:
        return f'/check-ins/{checkin_date}/{pending["module_key"]}?return_to=today'
    return "/#today-state"


def render_checkin_question(
    checkin_date: str,
    module_key: str,
    requested_question: str | None = None,
    *,
    return_to_today: bool = False,
) -> str:
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
    return_hidden = '<input type="hidden" name="return_to" value="today">' if return_to_today else ""
    common = (
        f'<input type="hidden" name="question_id" value="{esc(question["id"])}">'
        f'<input type="hidden" name="expected_version" value="{esc(module["version"])}">'
        f'{return_hidden}'
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
    if previous:
        back_href = f'/check-ins/{checkin_date}/{module_key}?q={previous}'
        if return_to_today:
            back_href += "&return_to=today"
    else:
        back_href = "/#today-state" if return_to_today else f"/check-ins/{checkin_date}"
    severe = '<p class="danger-note" role="note">严重或持续症状需要停止自行加压并寻求医疗判断；这里仅记录信号，不做诊断。</p>' if module_key == "gut" and active.get("severity") == "severe" else ""
    history = (
        '<details><summary>查看之前的回答</summary><p class="muted">以前填写的内容仍然保留，今天的安排只使用你最近确认的回答。</p></details>'
        if module.get("history") else ""
    )
    return (
        f'<div class="quiz-shell"><section class="quiz-card"><div class="quiz-top">'
        f'<p class="quiz-step">{esc(definition["label"])} · {index + 1}/{esc(definition["max_steps"])}</p>'
        f'<form class="skip-form" method="post" action="/check-ins/{esc(checkin_date)}/{esc(module_key)}/skip">'
        f'<input type="hidden" name="expected_version" value="{esc(module["version"])}">'
        f'{return_hidden}'
        f'<button class="skip-link-button" type="submit">跳过本模块</button></form></div>'
        f'<h1 class="question-title">{esc(question["label"])}</h1>{control}{severe}'
        f'<div class="quiz-actions"><a class="back-link" href="{esc(back_href)}">返回</a>'
        + (f'<form method="post" action="/check-ins/{esc(checkin_date)}/{esc(module_key)}/discard-draft">'
           f'<input type="hidden" name="expected_version" value="{esc(module["version"])}">'
           f'{return_hidden}'
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
        '<section class="card"><div class="section-header"><div><h1>今天状态的提问设置</h1>'
        '<p class="muted">选择哪些内容需要每天询问，哪些只在需要时再问。</p></div>'
        f'<a class="button secondary" href="/check-ins/{service.configured_today().isoformat()}">返回今日状态</a></div>'
        f'<form method="post" action="/check-ins/settings"><div class="settings-list">{"".join(rows)}</div>'
        '<div class="form-actions"><button type="submit">保存设置</button></div></form></section>'
    )


def render_ai_settings() -> str:
    status = ai.ai_status()
    provider = status.get("provider") or ""
    model_value = esc(os.environ.get("MEALCIRCUIT_AI_MODEL", ""))
    stage_models = status.get("stage_models") or {}
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
        f'<p><span class="status completed">{state}</span>'
        + (f' · 当前使用 {esc(provider)}' if state == "已启用" else ' · 连接后才会自动准备草案')
        + '</p>'
    )
    disable = (
        '<form method="post" action="/ai/disable">'
        '<div class="form-actions"><button class="secondary" type="submit">关闭本次运行的 API Key 模式</button></div></form>'
        if provider or status["model_configured"] or status["key_configured"] else ""
    )
    return f'''<section class="card"><div class="section-header"><div><h1>智能规划设置</h1><p class="muted">连接信息只在本次运行中使用，不会保存到MealCircuit数据里。</p></div><a class="button secondary" href="/me#advanced">返回设置</a></div>{configured}
<form method="post" action="/ai/configure">
<label for="ai-provider">供应商</label><select id="ai-provider" name="provider">{provider_options}</select>
<label for="ai-model">模型名</label><input id="ai-model" name="model" value="{model_value}" placeholder="例如 deepseek-v4-flash" required>
<label for="ai-key">API Key</label><input id="ai-key" name="api_key" type="password" autocomplete="off" required>
<div class="grid two"><div><label for="ai-timeout">超时秒数</label><input id="ai-timeout" name="timeout_seconds" type="number" min="1" value="{esc(status["timeout_seconds"])}"></div>
<div><label for="ai-max-output">最大输出 token</label><input id="ai-max-output" name="max_output_tokens" type="number" min="1" value="{esc(status["max_output_tokens"])}"></div></div>
<details><summary>高级：为三个阶段指定不同模型</summary><p class="muted">留空就全部使用上面的默认模型；API Key 仍只保存在当前进程。</p>
<div class="grid"><label>个案理解模型<input name="case_model" value="{esc(stage_models.get('case') or '')}" placeholder="默认模型"></label>
<label>计划设计模型<input name="plan_model" value="{esc(stage_models.get('plan') or '')}" placeholder="默认模型"></label>
<label>独立审查模型<input name="review_model" value="{esc(stage_models.get('review') or '')}" placeholder="默认模型"></label></div></details>
<div class="form-actions"><button type="submit">启用本次运行的 API Key 模式</button></div></form>{disable}</section>'''


def render_sync_settings() -> str:
    state = sync.sync_status()
    conflicts = sync.list_conflicts()
    if state["enabled"]:
        try:
            devices = sync.list_sync_devices()
            device_cards = "".join(
                '<article class="panel"><div class="section-header"><div>'
                f'<h3>{esc(item.get("name") or "未命名设备")}</h3>'
                f'<p class="muted">{esc("当前设备" if item.get("current") else "已撤销" if item.get("revoked") else "已授权")}</p>'
                '</div>'
                + (
                    f'<form method="post" action="/sync/devices/{esc(item.get("id"))}/revoke" onsubmit="return confirm(\'立即撤销此设备？\')"><button class="danger" type="submit">撤销</button></form>'
                    if not item.get("current") and not item.get("revoked") else ""
                )
                + '</div></article>'
                for item in devices
            ) or '<p class="muted">没有设备记录。</p>'
        except ValidationError as exc:
            device_cards = f'<p class="muted">同步服务当前不可达；本机功能不受影响。{esc(exc)}</p>'
        conflict_cards = "".join(
            '<article class="panel error">'
            f'<h3>{esc(item["entity_kind"])} · {esc(item["entity_id"])}</h3>'
            f'<p>冲突字段：{esc(", ".join(item["conflicting_paths"]))}</p>'
            f'<details><summary>查看两个保留版本</summary><div class="grid two">'
            f'<div><h4>本机版本</h4><pre>{esc(json.dumps(item["local_revision"]["payload"], ensure_ascii=False, indent=2))}</pre></div>'
            f'<div><h4>远端版本</h4><pre>{esc(json.dumps(item["remote_revision"]["payload"], ensure_ascii=False, indent=2))}</pre></div>'
            '</div></details><div class="actions">'
            f'<form method="post" action="/sync/conflicts/{esc(item["id"])}/resolve"><button name="choice" value="local">保留本机版本</button></form>'
            f'<form method="post" action="/sync/conflicts/{esc(item["id"])}/resolve"><button class="secondary" name="choice" value="remote">保留远端版本</button></form>'
            '</div></article>'
            for item in conflicts
        ) or '<p class="muted">当前没有待解决冲突。</p>'
        warning = (
            '<p class="error">有来自较新版本客户端的数据。内容已完整保留，请升级后再处理。</p>'
            if state["unknown_schema_entities"] else ""
        )
        if state.get("unresolved_assets"):
            warning += (
                f'<p class="error">有 {esc(state["unresolved_assets"])} 个照片资产尚未落到本机；'
                '可能是按需照片，也可能是迁移时缺失的外部路径。请执行照片同步或用 doctor 检查。</p>'
            )
        media_policy = state.get("media_policy") or "all_wifi"
        media_options = "".join(
            f'<option value="{value}" {"selected" if media_policy == value else ""}>{label}</option>'
            for value, label in (("all", "全部网络"), ("all_wifi", "仅非计费网络 / 手动桌面同步"), ("on_demand", "按需下载"))
        )
        on_demand_action = (
            '<form method="post" action="/sync/now"><input type="hidden" name="include_on_demand_media" value="1">'
            '<button class="secondary" type="submit">本次下载全部缺失照片</button></form>'
            if media_policy == "on_demand" else ""
        )
        return f'''<section class="card"><div class="section-header"><div><h1>同步与设备</h1><p class="muted">数据会先在这台设备上加密，同步服务无法看到饮食内容。</p></div>
<a class="button secondary" href="/me#advanced">返回设置</a></div>
<dl class="summary-list"><div><dt>服务</dt><dd>{esc(state["server_url"])}</dd></div><div><dt>账户</dt><dd>{esc(state["account_id"])}</dd></div>
<div><dt>待上传</dt><dd>{esc(state["pending"])}</dd></div><div><dt>游标</dt><dd>{esc(state["cursor"])}</dd></div>
<div><dt>冲突</dt><dd>{esc(state["conflicts"])}</dd></div><div><dt>照片策略</dt><dd>{esc(media_policy)}</dd></div></dl>{warning}
<div class="actions"><form method="post" action="/sync/now"><button type="submit">立即同步</button></form>
{on_demand_action}<form method="post" action="/sync/unlink" onsubmit="return confirm('取消本机同步关联？本地数据会完整保留。')"><button class="secondary" type="submit">取消本机同步</button></form></div>
<form method="post" action="/sync/media-policy"><label for="sync-media-policy">照片同步策略</label>
<select id="sync-media-policy" name="media_policy">{media_options}</select><div class="form-actions"><button class="secondary" type="submit">保存照片策略</button></div></form></section>
<section class="card"><h2>冲突中心</h2><p class="muted">同字段并发值和删除对编辑不会按时间覆盖；两个版本会一直保留到你选择。</p>{conflict_cards}</section>
<section class="card"><h2>设备</h2><p class="muted">撤销会立即使该设备的服务端令牌失效。</p>{device_cards}</section>
<section class="card"><h2>更换恢复密钥</h2><p>会重新保护远端数据并退出其他设备。开始前需要先解决同步冲突和来自较新版本的数据。</p>
<form method="post" action="/sync/rotate/prepare" onsubmit="return confirm('开始安全轮换？确认后其他设备必须重新加入。')"><button class="danger" type="submit">开始安全轮换</button></form></section>
<section class="card error"><h2>删除远端同步账户</h2><p>永久删除服务端账户、密文与附件；本机数据保留并自动转为仅本地模式。</p>
<form method="post" action="/sync/delete-account" onsubmit="return confirm('永久删除远端同步账户？此操作无法撤销。')">
<label for="sync-delete-password">账户密码</label><input id="sync-delete-password" name="password" type="password" autocomplete="current-password" required>
<div class="form-actions"><button class="danger" type="submit">永久删除远端账户</button></div></form></section>'''
    return '''<section class="card"><div class="section-header"><div><h1>同步与设备</h1><p class="muted">不登录也能一直离线使用；需要多设备时再连接自己的同步服务。</p></div><a class="button secondary" href="/me#advanced">返回设置</a></div>
<div class="grid two"><form method="post" action="/sync/configure"><h2>登录已有账户</h2><input type="hidden" name="action" value="login">
<label for="sync-login-url">同步服务 URL</label><input id="sync-login-url" name="server_url" type="url" placeholder="https://sync.example.com" required>
<label for="sync-login-name">登录名</label><input id="sync-login-name" name="login_name" autocomplete="username" required>
<label for="sync-login-device">设备名称</label><input id="sync-login-device" name="device_name" required>
<label for="sync-login-password">账户密码</label><input id="sync-login-password" name="password" type="password" autocomplete="current-password" required>
<label for="sync-recovery">恢复密钥</label><input id="sync-recovery" name="recovery_key" type="password" autocomplete="off" required>
<label><input type="checkbox" name="allow_insecure_localhost" value="1"> 仅本机调试允许 HTTP localhost</label>
<div class="form-actions"><button type="submit">登录并解锁同步</button></div></form>
<form method="post" action="/sync/configure"><h2>创建新账户</h2><input type="hidden" name="action" value="register">
<label for="sync-register-url">同步服务 URL</label><input id="sync-register-url" name="server_url" type="url" placeholder="https://sync.example.com" required>
<label for="sync-register-name">登录名</label><input id="sync-register-name" name="login_name" autocomplete="username" required>
<label for="sync-register-device">设备名称</label><input id="sync-register-device" name="device_name" required>
<label for="sync-register-password">账户密码（至少 12 字符）</label><input id="sync-register-password" name="password" type="password" autocomplete="new-password" minlength="12" required>
<label for="sync-register-confirm">再次输入密码</label><input id="sync-register-confirm" name="password_confirm" type="password" autocomplete="new-password" minlength="12" required>
<label><input type="checkbox" name="allow_insecure_localhost" value="1"> 仅本机调试允许 HTTP localhost</label>
<div class="form-actions"><button type="submit">创建账户并生成恢复密钥</button></div></form></div></section>'''


def render_recovery_confirmation(recovery_key: str, token: str) -> str:
    return f'''<section class="card"><h1>保存恢复密钥</h1>
<p class="error">此密钥只显示一次。丢失全部设备且没有恢复密钥时，服务端无法恢复你的数据。</p>
<pre>{esc(recovery_key)}</pre><form method="post" action="/sync/confirm-recovery">
<input type="hidden" name="token" value="{esc(token)}"><label for="recovery-confirmation">完整重新输入恢复密钥</label>
<input id="recovery-confirmation" name="recovery_key" type="password" autocomplete="off" required>
<div class="form-actions"><button type="submit">我已保存，启用同步</button></div></form></section>'''


def render_rotation_confirmation(recovery_key: str) -> str:
    return f'''<section class="card"><h1>确认新的恢复密钥</h1>
<p class="error">在完整重新输入前，旧密钥仍有效且轮换不会提交。确认后全部远端数据会重新加密，其他设备立即撤销。</p>
<pre>{esc(recovery_key)}</pre><form method="post" action="/sync/rotate/confirm">
<label for="rotation-recovery-confirmation">完整重新输入新的恢复密钥</label>
<input id="rotation-recovery-confirmation" name="recovery_key" type="password" autocomplete="off" required>
<div class="form-actions"><button type="submit">确认保存并完成轮换</button></div></form>
<form method="post" action="/sync/rotate/abort"><button class="secondary" type="submit">中止轮换</button></form></section>'''


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
    home_meals = [meal for meal in menu.get("meals", []) if meal.get("recipe_card")]
    if not home_meals:
        return ""
    recipe_sections = []
    for meal in home_meals:
        recipe = meal["recipe_card"]
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
        meal_key = {"早餐": "BREAKFAST", "午餐": "LUNCH", "晚餐": "DINNER"}.get(meal.get("name"), "MEAL")
        recipe_sections.append(
            f'<section class="menu-section"><span class="subtle-label">{esc(meal_key)}</span>'
            f'<h2>{esc(recipe["title"])}</h2><div class="recipe-meta">'
            f'<span>1 人份</span><span>主动 {esc(recipe["active_minutes"])} 分钟</span>'
            f'<span>总计 {esc(recipe["total_minutes"])} 分钟</span><span>{esc(cookware)}</span></div>'
            '<div class="recipe-columns"><div><h3>食材</h3>' + ingredients
            + '<h3>调味</h3>' + seasonings + '</div><div><h3>按顺序操作</h3><ol class="recipe-steps">'
            + steps + '</ol></div></div><h3>失败补救</h3>' + render_list(recipe["failure_rescue"])
            + f'<p><strong>清洁成本：</strong>{esc(recipe["cleanup"])}</p>'
            + f'<p><strong>肠胃降级：</strong>{esc(recipe["gut_fallback"])}</p></section>'
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
        "".join(recipe_sections)
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
        eat_out = meal.get("eat_out_guidance") or {}
        guidance_html = (
            f'<p class="meal-foods"><strong>外食提醒：</strong>蛋白 {esc(eat_out.get("protein_anchor"))}；'
            f'主食 {esc(eat_out.get("staple"))}；蔬菜 {esc(eat_out.get("vegetables"))}；'
            f'酱汁 {esc(eat_out.get("sauce_rule"))}；备选 {esc(eat_out.get("fallback"))}</p>'
            if eat_out else ""
        )
        meals.append(
            '<li class="meal-item"><span class="meal-node" aria-hidden="true"></span>'
            f'<div class="meal-title"><span>{esc(meal["name"])}</span>{mode_html}</div>'
            f'<p class="meal-foods">{esc("、".join(meal["foods"]))}</p>'
            f'<p class="meal-foods">{esc(meal["portion_guidance"])} · 蛋白 {esc(protein[0])}–{esc(protein[1])}g</p>'
            f'<p class="meal-foods">替换：{esc("、".join(meal["substitutions"]) or "无")}</p>{guidance_html}</li>'
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
    return (
        '<div class="report-grid"><section class="panel">'
        + f'<p class="notice card"><strong>今天整体：</strong>{esc(status_labels[result["system_status"]])}</p>'
        + '<div class="report-section"><h2>事实</h2>' + render_list(result["facts"]) + '</div>'
        + '<div class="report-section"><h2>这可能说明</h2>' + render_list(result["inferences"]) + '</div>'
        + '<div class="report-section"><h2>接下来最重要</h2>' + render_list(result["core_advice"]) + '</div>'
        + '<div class="report-section"><h2>继续保持</h2>' + render_list(result["do_not_adjust"]) + '</div>'
        + '<div class="report-section"><h2>需要留意</h2>' + render_list(result["risk_signals"]) + '</div>'
        + '<div class="report-section"><h2>食材安排</h2>' + priority_html + '</div></section>'
        + f'<aside class="panel report-aside"><p class="subtle-label">{esc(menu["date"])}</p><h2>明天怎么吃</h2>'
        + f'<p class="muted small">{esc(menu["environment"])} · 蛋白目标 {esc(menu["protein_target_g"][0])}–{esc(menu["protein_target_g"][1])}g</p>'
        + '<ol class="meal-timeline">' + ''.join(meals) + '</ol>'
        + '<div class="report-section"><h3>条件加餐</h3>'
        + f'<p>{esc(snack["condition"])}</p>{render_list(snack["options"])}</div>'
        + f'<div class="report-section"><h3>训练日调整</h3><p>{esc(menu["training_adjustment"])}</p></div>'
        + f'<div class="report-section"><h3>肠胃异常调整</h3><p>{esc(menu["gut_adjustment"])}</p></div></aside></div>'
        + render_home_cooking_menu(menu)
        + f'<p class="panel"><strong>一句话复盘：</strong>{esc(result["one_line_review"])}</p>'
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

    def send_json(self, value: object, status: int = 200) -> None:
        payload = json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", 'inline; filename="agent-context.json"')
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

    def read_multipart_values(self, max_bytes: int | None = None) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, bytes]]]]:
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
        fields: dict[str, list[str]] = {}
        files: dict[str, list[tuple[str, bytes]]] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            data = part.get_payload(decode=True) or b""
            if filename and name:
                files.setdefault(name, []).append((filename, data))
            elif name:
                fields.setdefault(name, []).append(data.decode(part.get_content_charset() or "utf-8"))
        return fields, files

    def read_multipart(self, max_bytes: int | None = None) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
        values, files = self.read_multipart_values(max_bytes=max_bytes)
        return (
            {key: items[-1] for key, items in values.items()},
            {key: items[-1] for key, items in files.items()},
        )

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
            priority_tradeoffs = [
                item for item in values.get("priority_tradeoff") or [] if item
            ]
            priority_tradeoffs = list(dict.fromkeys(priority_tradeoffs))
            return {
                "meal_environment": last("meal_environment"), "portion_method": last("portion_method"),
                "meal_modes": {
                    key: last(f"meal_mode_{key}", personalization.LEGACY_DEFAULT_MEAL_MODES[key])
                    for key in personalization.MEAL_KEYS
                },
                "cooking_time_minutes": int(last("cooking_time_minutes", "25")),
                "question_budget": int(last("question_budget", "2")),
                "equipment": _csv(last("equipment")), "food_exclusions": _csv(last("food_exclusions")),
                "preferences": _csv(last("preferences")),
                "non_negotiables": values.get("non_negotiables") or [],
                "priority_tradeoffs": priority_tradeoffs,
                "recording_intensity": last("recording_intensity", "light"),
                "followup_intensity": last("followup_intensity", "only_when_needed"),
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
                status = personalization.onboarding_status()
                if status["status"] == "setup_required":
                    self.send_html("初始化", render_setup_start(status))
                    return
                self.send_html("今天", render_today_workspace(service.configured_today().isoformat()))
            elif path.startswith("/agent/context/"):
                work_date = path.split("/")[3]
                if query.get("format") == ["json"]:
                    self.send_json(agent_workspace.build_agent_context(work_date))
                else:
                    self.send_html("Agent 上下文", render_agent_context_page(work_date))
            elif path.startswith("/agent/state/"):
                work_date = path.split("/")[3]
                state = agent_workspace.get_workspace_state(work_date)
                draft = state.get("draft") or {}
                latest = state.get("latest_run") or {}
                self.send_json({
                    "status": draft.get("status") or latest.get("status") or "collecting",
                    "version": draft.get("version") or 0,
                    "updated_at": draft.get("updated_at") or latest.get("updated_at"),
                    "pending_questions": len([
                        item for item in state.get("questions") or [] if item.get("status") == "pending"
                    ]),
                })
            elif path == "/capture":
                self.redirect("/#record")
            elif path == "/plans":
                self.send_html("计划", render_plans_hub())
            elif path.startswith("/plans/"):
                self.send_html("执行计划", render_plan_page(path.split("/")[2]))
            elif path.startswith("/questions/"):
                self.send_html("今天", render_questions_page(path.split("/")[2]))
            elif path == "/learning":
                self.send_html("MealCircuit了解的你", render_learning_page())
            elif path == "/me":
                self.send_html("我的", render_me_page())
            elif path == "/inventory":
                self.send_html("库存", render_inventory_page())
            elif path == "/profile":
                self.send_html("目标与边界", render_profile_page())
            elif path == "/insights":
                self.redirect("/me#progress")
            elif path == "/data":
                self.send_html("备份与迁移", render_data_page())
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
                self.redirect("/plans")
            elif path == "/history":
                reviews = service.list_daily_reviews()
                body = (
                    '<section class="history-heading"><div><h1>过去的安排</h1>'
                    '<p class="muted">按日期回看当时的复盘和第二天安排。</p></div></section>'
                    + render_review_cards(reviews)
                )
                self.send_html("历史建议", body)
            elif path == "/check-ins":
                self.redirect(f"/check-ins/{service.configured_today().isoformat()}")
            elif path == "/check-ins/settings":
                self.send_html("状态设置", render_checkin_settings())
            elif path.startswith("/check-ins/"):
                parts = path.strip("/").split("/")
                if len(parts) == 2:
                    self.send_html("今日状态", render_checkin_hub(parts[1]))
                elif len(parts) == 3:
                    requested = query.get("q", [None])[0]
                    self.send_html(
                        "状态问答",
                        render_checkin_question(
                            parts[1], parts[2], requested,
                            return_to_today=query.get("return_to") == ["today"],
                        ),
                    )
                else:
                    self.send_html("未找到", '<section class="card"><h1>404</h1><p>页面不存在。</p></section>', 404)
            elif path == "/tasks/photo":
                self.send_html("上传食物照片", '<section class="card"><h1>食物识别任务</h1><p class="muted">照片仅用于候选识别与区间估算。看不见的油、酱汁、重量和品牌必须列为未知项。</p><form method="post" enctype="multipart/form-data" action="/tasks/photo"><label for="task-photo">食物照片 *</label><input id="task-photo" type="file" name="photo" accept="image/jpeg,image/png,image/gif,image/webp" required><label for="task-note">补充说明</label><textarea id="task-note" name="note" placeholder="例如：这是训练后外食；酱汁没有全部吃完"></textarea><div class="form-actions"><button type="submit">创建待处理任务</button></div></form></section>')
            elif path == "/tasks/material":
                self.send_html("原材料分析", '<section class="card"><h1>原材料分析任务</h1><p class="muted">输入已有食材与粗略数量，Agent 会结合总纲、营养库、近 14 天记录和长期记忆分析。</p><form method="post" action="/tasks/material"><label for="task-materials">现有食材及粗略数量 *</label><textarea id="task-materials" name="materials" required placeholder="例如：鸡胸肉约 500g、冷冻西兰花一袋、米、鸡蛋 6 个"></textarea><div class="form-actions"><button type="submit">创建待处理任务</button></div></form></section>')
            elif path == "/tasks":
                self.send_html("任务列表", f'<section class="card"><h1>全部任务</h1>{task_table(service.list_tasks())}</section>')
            elif path == "/ai":
                self.send_html("智能规划设置", render_ai_settings())
            elif path == "/sync":
                self.send_html("同步与设备", render_sync_settings())
            elif path.startswith("/tasks/"):
                task_id = path.split("/")[2]
                task = service.get_task(task_id)
                media = f'<img class="photo" src="/media/{Path(task["image_path"]).name}" alt="上传的食物照片">' if task.get("image_path") else ""
                if task["result_json"]:
                    result = render_provenance_warning(task.get("result_provenance_json")) + render_result(task["type"], task["result_json"])
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
                body = f'''<div class="grid"><section class="card"><h1>新增每日记录</h1><form method="post" action="/records"><label for="record-date">日期</label><input id="record-date" type="date" name="record_date" value="{service.configured_today().isoformat()}" required><label for="record-input">自然语言记录</label><textarea id="record-input" name="raw_input" required></textarea><div class="form-actions"><button>保存</button></div></form></section><section class="card"><h2>新增长期记忆</h2><form method="post" action="/memories"><label for="memory-kind">类型</label><select id="memory-kind" name="kind"><option value="preference">已验证偏好</option><option value="gut_trigger">肠胃触发</option><option value="constraint">约束</option><option value="other">其他</option></select><label for="memory-content">内容</label><textarea id="memory-content" name="content" required></textarea><label for="memory-evidence">证据</label><input id="memory-evidence" name="evidence"><div class="form-actions"><button>保存</button></div></form></section><section class="card"><h2>新增当前有效调整</h2><form method="post" action="/adjustments"><label for="adjustment-content">调整内容</label><textarea id="adjustment-content" name="content" required></textarea><label for="adjustment-reason">原因</label><input id="adjustment-reason" name="reason"><div class="form-actions"><button>保存</button></div></form></section></div><section class="card"><div class="section-header"><div><p class="eyebrow">Advice archive</p><h2>最近建议</h2></div><a class="button secondary" href="/history">查看全部</a></div>{render_review_cards(recent_reviews)}</section><section class="card"><h2>长期记忆</h2><ul>{memories}</ul></section><section class="card"><h2>当前有效调整</h2><ul>{adjustments}</ul></section>'''
                self.send_html("记录与记忆", body)
            elif path.startswith("/reviews/"):
                review_date = path.split("/")[2]
                review = service.get_daily_review(review_date)
                if review["status"] == "completed":
                    result = render_provenance_warning(review.get("result_provenance_json")) + render_daily_review_result(review["result_json"])
                else:
                    result = '<p>这一天的记录已经保存，复盘还没有准备好。</p><a class="button secondary" href="/">回到今天</a>'
                result_shell = result if review["status"] == "completed" else f'<section class="panel"><h2>还在准备</h2>{result}</section>'
                body = (
                    f'<section class="panel"><div class="section-header"><div><h1>{esc(review_date)} 的复盘</h1>'
                    '<p class="muted">回看当时的判断和第二天安排。</p></div><a class="button secondary" href="/history">返回历史</a></div></section>'
                    + render_checkin_callout(review_date)
                    + result_shell
                )
                self.send_html("每日复盘", body)
            elif path.startswith("/media/"):
                filename = Path(path).name
                candidates = [
                    (upload_root() / filename).resolve(),
                    (managed_asset_root() / filename).resolve(),
                ]
                target = next(
                    (
                        item
                        for item in candidates
                        if item.is_file()
                        and item.parent in {upload_root().resolve(), managed_asset_root().resolve()}
                    ),
                    None,
                )
                if target is None:
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
            elif path == "/agent/intake":
                form = self.read_urlencoded()
                agent_workspace.record_intake(form.get("record_date", ""), form.get("text", ""))
                self.redirect("/#record")
            elif path.startswith("/agent/intake/") and path.endswith("/edit"):
                record_id = path.strip("/").split("/")[2]
                form = self.read_urlencoded()
                agent_workspace.update_intake(
                    record_id, form.get("record_date", ""), form.get("text", "")
                )
                self.redirect("/#record")
            elif path.startswith("/agent/drafts/") and path.endswith("/generate"):
                review_date = path.strip("/").split("/")[2]
                agent_workspace.run_agent_draft(review_date, force=True)
                self.redirect("/")
            elif path.startswith("/agent/drafts/") and path.endswith("/revise"):
                review_date = path.strip("/").split("/")[2]
                agent_workspace.revise_draft(review_date, self.read_urlencoded().get("instruction", ""))
                self.redirect("/")
            elif path.startswith("/agent/drafts/") and path.endswith("/accept"):
                review_date = path.strip("/").split("/")[2]
                accepted = agent_workspace.accept_draft(review_date)
                plan_date = ((accepted.get("result_json") or {}).get("tomorrow_menu") or {}).get("date")
                self.redirect(f"/plans/{plan_date}" if plan_date else "/")
            elif path.startswith("/agent/questions/") and path.endswith("/answer"):
                question_id = path.strip("/").split("/")[2]
                form = self.read_urlencoded()
                agent_workspace.answer_clarification(
                    question_id, form.get("answer", ""), int(form.get("version", "-1"))
                )
                self.redirect("/")
            elif path == "/learning/reflect":
                agent_workspace.run_longitudinal_reflection()
                self.redirect("/learning")
            elif path.startswith("/learning/claims/") and path.endswith("/action"):
                claim_id = path.strip("/").split("/")[2]
                form = self.read_urlencoded()
                agent_workspace.update_claim(
                    claim_id, form.get("action", ""), correction=form.get("correction", "")
                )
                self.redirect("/" if form.get("return_to") == "/" else "/learning")
            elif path.startswith("/plans/") and path.endswith("/feedback"):
                parts = path.strip("/").split("/")
                plan_date, plan_item_id = parts[1], parts[2]
                files: dict[str, list[tuple[str, bytes]]] = {}
                if self.headers.get("Content-Type", "").startswith("multipart/form-data"):
                    values, files = self.read_multipart_values(max_bytes=256 * 1024 * 1024)
                else:
                    values = self.read_urlencoded_values()
                expected = int((values.get("expected_version") or ["0"])[-1])
                status_value = (values.get("status") or [""])[-1]
                reason_codes = values.get("reason_codes") or []
                actual_text = (values.get("actual_text") or [""])[-1]
                plan = adaptive.get_plan_for_date(plan_date, include_restricted_history=True)
                if not plan:
                    raise ValidationError("该日期没有已发布计划")
                meal = next(
                    (item for item in plan["menu"]["meals"] if item["plan_item_id"] == plan_item_id), None
                )
                if not meal:
                    raise ValidationError("计划项目不存在或已变更")
                existing = plan["feedback"].get(plan_item_id)
                outcome = dict(existing.get("outcome_json") or {}) if existing else {}
                satiety = (values.get("satiety") or [""])[-1]
                if satiety:
                    outcome["satiety"] = satiety
                else:
                    outcome.pop("satiety", None)
                meal_slot = meal.get("slot") or {
                    "早餐": "breakfast", "午餐": "lunch", "晚餐": "dinner",
                }.get(meal.get("name"), "unknown")
                photo_task_ids = []
                existing_photo_task_ids = outcome.get("photo_task_ids")
                if isinstance(existing_photo_task_ids, list):
                    photo_task_ids.extend(str(item) for item in existing_photo_task_ids if item)
                if outcome.get("photo_task_id"):
                    photo_task_ids.append(str(outcome["photo_task_id"]))
                preserved_photo_task_ids = list(values.get("photo_task_ids") or [])
                legacy_photo_task_id = (values.get("photo_task_id") or [""])[-1]
                if legacy_photo_task_id:
                    preserved_photo_task_ids.append(legacy_photo_task_id)
                for preserved_photo_task_id in dict.fromkeys(preserved_photo_task_ids):
                    preserved_task = service.get_task(preserved_photo_task_id)
                    valid_link = any(
                        link["observed_date"] == plan_date
                        and link["role"] == "consumed"
                        and link["meal_slot"] == meal_slot
                        for link in adaptive.task_evidence_links(preserved_photo_task_id)
                    )
                    if preserved_task.get("type") != "photo" or not valid_link:
                        raise ValidationError("实际照片与这顿记录不匹配")
                    photo_task_ids.append(preserved_photo_task_id)
                for _, data in files.get("photo") or []:
                    if not data:
                        continue
                    task = service.create_photo_task(
                        io.BytesIO(data), f'{plan_date} {meal.get("name") or "餐次"}实际执行照片'
                    )
                    adaptive.link_task_evidence(task["id"], plan_date, "consumed", meal_slot)
                    photo_task_ids.append(task["id"])
                photo_task_ids = list(dict.fromkeys(photo_task_ids))
                if photo_task_ids:
                    outcome["photo_task_ids"] = photo_task_ids
                    outcome["photo_task_id"] = photo_task_ids[-1]
                else:
                    outcome.pop("photo_task_ids", None)
                    outcome.pop("photo_task_id", None)
                draft = {
                    "plan_item_id": plan_item_id,
                    "expected_version": expected,
                    "status": status_value,
                    "satiety": satiety,
                    "reason_codes": reason_codes,
                    "actual_text": actual_text,
                    "photo_task_ids": photo_task_ids,
                }
                try:
                    adaptive.save_plan_feedback(
                        plan_date, plan_item_id, status_value,
                        reason_codes=reason_codes,
                        actual_text=actual_text,
                        outcome=outcome,
                        expected_version=expected or None, actor_source="web",
                    )
                except ValidationError as exc:
                    feedback_error = str(exc)
                    if feedback_error == "实际执行补充 不能超过 2000 字":
                        feedback_error = "“实际怎么吃的”最多填写 2000 字，请精简后再保存。"
                    self.send_html(
                        "记录这一餐",
                        render_plan_page(plan_date, feedback_draft=draft, feedback_error=feedback_error),
                        400,
                    )
                    return
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
                elif schema.get("kind") == "meal_mode_overrides":
                    answer = {
                        key: (values.get(f"{key}_mode") or ["inherit"])[-1]
                        for key in ("breakfast", "lunch", "dinner")
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
                self.redirect("/me#progress")
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
                self.send_html("备份与迁移", render_data_page(f'恢复完成；恢复前备份：{restored.get("pre_restore_backup") or "无"}'))
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
                    form.get("case_model", ""),
                    form.get("plan_model", ""),
                    form.get("review_model", ""),
                )
                self.redirect("/ai")
            elif path == "/ai/disable":
                ai.clear_runtime()
                self.redirect("/ai")
            elif path == "/sync/configure":
                form = self.read_urlencoded()
                allow_insecure = form.get("allow_insecure_localhost") == "1"
                if form.get("action") == "login":
                    sync.login_sync(
                        server_url=form.get("server_url", ""),
                        login_name=form.get("login_name", ""),
                        password=form.get("password", ""),
                        device_name=form.get("device_name", ""),
                        recovery_key=form.get("recovery_key", ""),
                        allow_insecure_localhost=allow_insecure,
                    )
                    self.redirect("/sync")
                elif form.get("action") == "register":
                    if form.get("password") != form.get("password_confirm"):
                        raise ValidationError("两次输入的账户密码不一致")
                    captured: list[str] = []
                    sync.register_sync(
                        server_url=form.get("server_url", ""),
                        login_name=form.get("login_name", ""),
                        password=form.get("password", ""),
                        device_name=form.get("device_name", ""),
                        confirm_recovery_key=lambda value: not captured.append(value),
                        allow_insecure_localhost=allow_insecure,
                    )
                    if len(captured) != 1:
                        raise ValidationError("恢复密钥生成失败")
                    with connect() as connection:
                        connection.execute(
                            "UPDATE sync_configuration SET enabled=0,updated_at=? WHERE singleton=1",
                            (service.now(),),
                        )
                    pending_token = secrets.token_urlsafe(32)
                    with _PENDING_RECOVERY_LOCK:
                        _PENDING_RECOVERY[pending_token] = {
                            "digest": hashlib.sha256(captured[0].encode("utf-8")).hexdigest(),
                            "expires_at": time.monotonic() + 15 * 60,
                        }
                    self.send_html(
                        "同步与设备",
                        render_recovery_confirmation(captured[0], pending_token),
                    )
                else:
                    raise ValidationError("未知同步配置操作")
            elif path == "/sync/confirm-recovery":
                form = self.read_urlencoded()
                token = form.get("token", "")
                with _PENDING_RECOVERY_LOCK:
                    pending = _PENDING_RECOVERY.get(token)
                if not pending or pending["expires_at"] < time.monotonic():
                    raise ValidationError("恢复密钥确认已过期，请重新登录")
                normalized = form.get("recovery_key", "").strip().upper()
                digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                if not secrets.compare_digest(digest, pending["digest"]):
                    raise ValidationError("恢复密钥不匹配；同步仍未启用")
                with connect() as connection:
                    connection.execute(
                        "UPDATE sync_configuration SET enabled=1,updated_at=? WHERE singleton=1",
                        (service.now(),),
                    )
                with _PENDING_RECOVERY_LOCK:
                    _PENDING_RECOVERY.pop(token, None)
                self.redirect("/sync")
            elif path == "/sync/now":
                form = self.read_urlencoded()
                sync.sync_now(include_on_demand_media=form.get("include_on_demand_media") == "1")
                self.redirect("/sync")
            elif path == "/sync/media-policy":
                sync.set_media_policy(self.read_urlencoded().get("media_policy", ""))
                self.redirect("/sync")
            elif path == "/sync/rotate/prepare":
                prepared = sync.prepare_account_key_rotation()
                self.send_html("同步与设备", render_rotation_confirmation(prepared["recovery_key"]))
            elif path == "/sync/rotate/confirm":
                form = self.read_urlencoded()
                sync.confirm_account_key_rotation(form.get("recovery_key", ""))
                self.redirect("/sync")
            elif path == "/sync/rotate/abort":
                sync.abort_account_key_rotation()
                self.redirect("/sync")
            elif path.startswith("/sync/devices/") and path.endswith("/revoke"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise ValidationError("设备撤销路径无效")
                sync.revoke_sync_device(urllib.parse.unquote(parts[2]))
                self.redirect("/sync")
            elif path == "/sync/delete-account":
                sync.delete_sync_account(self.read_urlencoded().get("password", ""))
                self.redirect("/sync")
            elif path == "/sync/unlink":
                sync.unlink_sync()
                self.redirect("/sync")
            elif path.startswith("/sync/conflicts/") and path.endswith("/resolve"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise ValidationError("同步冲突路径无效")
                choice = self.read_urlencoded().get("choice", "")
                sync.resolve_conflict(parts[2], choice)
                self.redirect("/sync")
            elif path.startswith("/check-ins/"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise ValidationError("每日状态路径无效")
                _, checkin_date, module_key, action = parts
                values = self.read_urlencoded_values()
                expected_version = int((values.get("expected_version") or ["0"])[-1])
                return_to_today = (values.get("return_to") or [""])[-1] == "today"
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
                        self.redirect(_next_checkin_step(checkin_date) if return_to_today else f"/check-ins/{checkin_date}")
                    elif return_to_today:
                        self.redirect(
                            f"/check-ins/{checkin_date}/{module_key}"
                            f"?q={question_ids[current_index + 1]}&return_to=today"
                        )
                    else:
                        self.redirect(f"/check-ins/{checkin_date}/{module_key}?q={question_ids[current_index + 1]}")
                elif action == "complete":
                    service.complete_checkin_module(checkin_date, module_key, expected_version)
                    self.redirect(_next_checkin_step(checkin_date) if return_to_today else f"/check-ins/{checkin_date}")
                elif action == "skip":
                    service.skip_checkin_module(checkin_date, module_key, expected_version)
                    self.redirect(_next_checkin_step(checkin_date) if return_to_today else f"/check-ins/{checkin_date}")
                elif action == "discard-draft":
                    service.discard_checkin_draft(checkin_date, module_key, expected_version)
                    self.redirect("/#today-state" if return_to_today else f"/check-ins/{checkin_date}")
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
                agent_workspace.schedule_auto_draft(record_date)
                self.redirect("/#record")
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
