from __future__ import annotations

import argparse
import html
import io
import ipaddress
import json
import mimetypes
import sys
import urllib.parse
from datetime import date
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import checkins, service
from .db import init_db
from .storage import port_value, upload_root
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


def layout(title: str, body: str) -> bytes:
    today = date.today()
    checkin_path = f"/check-ins/{today.isoformat()}"
    nav_groups = (
        ("日常", (
            ("/", "今日总览", "dashboard", title == "首页"),
            ("/daily", "今日建议", "advice", title in {"今日建议与明日菜单", "每日复盘"}),
            (checkin_path, "今日状态", "checkin", title in {"今日状态", "状态问答", "状态设置"}),
        )),
        ("处理", (
            ("/tasks/photo", "照片任务", "photo", title in {"上传食物照片", "任务详情"}),
            ("/tasks/material", "原材料", "material", title == "原材料分析"),
            ("/tasks", "全部任务", "tasks", title == "任务列表"),
        )),
        ("资料", (
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
        "首页": "今日总览", "今日建议与明日菜单": "今日建议", "每日复盘": "每日复盘",
        "今日状态": "今日状态", "状态问答": "状态问答", "状态设置": "状态设置",
        "上传食物照片": "照片任务", "原材料分析": "原材料分析", "任务列表": "全部任务",
        "任务详情": "任务详情", "食品营养库": "食品营养库", "新增食品": "新增食品",
        "编辑食品": "编辑食品", "历史建议": "历史建议", "记录与记忆": "记录与记忆",
        "操作失败": "操作失败", "未找到": "未找到",
    }
    top_action = "" if title in {"今日状态", "状态问答", "状态设置"} else (
        f'<a class="button" href="{checkin_path}" aria-label="记录状态" title="记录状态">{icon("checkin")}记录状态</a>'
    )
    date_label = f"{today.month}月{today.day}日 周{'一二三四五六日'[today.weekday()]}"
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · MealCircuit</title><link rel="icon" href="/assets/ui/favicon.svg" type="image/svg+xml"><script src="/assets/ui/theme-init.js?v=20260708b"></script><link rel="stylesheet" href="/assets/ui/app.css?v=20260708b"><script src="/assets/ui/app.js?v=20260708b" defer></script></head><body>
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

    def read_multipart(self) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise ValidationError("上传必须使用 multipart/form-data")
        length = int(self.headers.get("Content-Length", "0"))
        if length > service.MAX_UPLOAD_BYTES + 1024 * 1024:
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

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path.rstrip("/") or "/", urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/assets/ui/"):
                self.send_static(path.removeprefix("/assets/ui/"))
            elif path == "/":
                self.send_html("首页", render_dashboard(service.dashboard_snapshot()))
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
            if path == "/check-ins/settings":
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
