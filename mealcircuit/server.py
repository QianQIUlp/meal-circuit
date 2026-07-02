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

from . import service
from .db import init_db
from .storage import port_value, upload_root
from .validation import ValidationError, nutrition_number


STYLE = """
:root {
  color-scheme: dark;
  --bg: #0b1210;
  --surface: #111b17;
  --surface-strong: #16231d;
  --surface-raised: #1a2922;
  --border: #2c4037;
  --border-strong: #40594e;
  --text: #f2f6f3;
  --muted: #a7b7ae;
  --accent: #b8f25b;
  --accent-hover: #caf77d;
  --accent-ink: #16210c;
  --amber: #f6c453;
  --blue: #78bff2;
  --danger: #ff9297;
  --success: #82e2ba;
  --focus: #e7ffb9;
  --radius-sm: 6px;
  --radius-md: 10px;
  --shadow: 0 18px 48px rgba(0, 0, 0, .22);
  font-family: "Microsoft YaHei UI", "PingFang SC", system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
}
* { box-sizing: border-box; }
html { scroll-padding-top: 96px; }
body {
  margin: 0;
  min-width: 320px;
  min-height: 100vh;
  background:
    linear-gradient(rgba(184, 242, 91, .025) 1px, transparent 1px),
    linear-gradient(90deg, rgba(184, 242, 91, .025) 1px, transparent 1px),
    var(--bg);
  background-size: 32px 32px;
  font-size: 16px;
  line-height: 1.65;
}
a { color: var(--accent); text-underline-offset: 3px; }
a:hover { color: var(--accent-hover); }
h1, h2, h3, .brand, .metric {
  font-family: Bahnschrift, "Microsoft YaHei UI", "PingFang SC", system-ui, sans-serif;
  letter-spacing: -.02em;
}
h1 { margin: 0 0 12px; font-size: clamp(1.75rem, 4vw, 2.5rem); line-height: 1.15; }
h2 { margin: 0 0 16px; font-size: clamp(1.25rem, 2.2vw, 1.55rem); line-height: 1.3; }
h3 { margin: 24px 0 12px; font-size: 1.05rem; line-height: 1.4; }
p { margin: 0 0 16px; }
code, pre, .metric, td:first-child { font-family: Consolas, "SFMono-Regular", monospace; }
code { color: #d8f7a9; }
.wrap { width: min(100% - 48px, 1184px); margin-inline: auto; }
.skip-link {
  position: fixed; top: 8px; left: 8px; z-index: 1000; padding: 10px 14px;
  background: var(--accent); color: var(--accent-ink); border-radius: var(--radius-sm);
  transform: translateY(-160%); transition: transform 160ms ease-out;
}
.skip-link:focus { transform: translateY(0); }
.nav {
  position: sticky; top: 0; z-index: 40;
  background: rgba(11, 18, 16, .94);
  border-bottom: 1px solid var(--border);
  backdrop-filter: blur(14px);
}
.nav::after {
  content: ""; display: block; height: 2px;
  background: linear-gradient(90deg, var(--accent) 0 42%, var(--amber) 42% 68%, var(--blue) 68% 100%);
  opacity: .72;
}
.nav-shell { display: flex; align-items: center; min-height: 72px; gap: 32px; }
.brand {
  display: inline-flex; align-items: center; gap: 12px; flex: 0 0 auto;
  color: var(--text); font-size: 1.25rem; font-weight: 700; text-decoration: none;
}
.brand:hover { color: var(--text); }
.brand-mark { display: grid; gap: 3px; width: 22px; }
.brand-mark i { display: block; height: 3px; border-radius: 2px; background: var(--accent); }
.brand-mark i:nth-child(2) { width: 72%; background: var(--amber); }
.brand-mark i:nth-child(3) { width: 45%; background: var(--blue); }
.nav-links { display: flex; align-items: center; justify-content: flex-end; gap: 4px; margin-left: auto; }
.nav-links a {
  min-height: 44px; display: inline-flex; align-items: center; padding: 8px 12px;
  color: var(--muted); font-size: .925rem; font-weight: 600; text-decoration: none;
  border-radius: var(--radius-sm); transition: color 160ms ease-out, background 160ms ease-out;
}
.nav-links a:hover { color: var(--text); background: var(--surface-raised); }
.nav-links a[aria-current="page"] { color: var(--text); background: var(--surface-raised); }
main.wrap { padding-block: 32px 56px; }
.hero {
  position: relative; overflow: hidden; padding: clamp(28px, 5vw, 52px);
  background: var(--surface-strong); border: 1px solid var(--border-strong);
  border-radius: var(--radius-md); box-shadow: var(--shadow); margin: 0 0 24px;
}
.hero::before {
  content: ""; position: absolute; inset: 0 auto 0 0; width: 6px;
  background: linear-gradient(var(--accent) 0 42%, var(--amber) 42% 68%, var(--blue) 68% 100%);
}
.hero h1 { max-width: 18ch; }
.hero p { max-width: 68ch; color: var(--muted); margin: 0; font-size: 1.03rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(280px, 100%), 1fr)); gap: 20px; }
.card {
  min-width: 0; padding: clamp(20px, 3vw, 28px); margin-bottom: 20px;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md);
}
.grid > .card { margin-bottom: 0; }
.card > :last-child { margin-bottom: 0; }
.stat-card { display: flex; flex-direction: column; justify-content: center; }
.workflow-card code { display: inline-block; margin: 3px 0; }
.section-heading { margin-right: auto; margin-bottom: 0; }
.actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.button, button {
  appearance: none; min-height: 44px; display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid var(--accent); border-radius: var(--radius-sm); padding: 9px 16px;
  background: var(--accent); color: var(--accent-ink); font: inherit; font-weight: 750;
  line-height: 1.3; text-decoration: none; cursor: pointer; touch-action: manipulation;
  transition: background 160ms ease-out, border-color 160ms ease-out, color 160ms ease-out;
}
.button:hover, button:hover { background: var(--accent-hover); border-color: var(--accent-hover); color: var(--accent-ink); }
.button:active, button:active { background: var(--accent); }
.button.secondary, button.secondary {
  background: transparent; border-color: var(--border-strong); color: var(--text);
}
.button.secondary:hover, button.secondary:hover { background: var(--surface-raised); border-color: #597164; }
.button.danger, button.danger { background: transparent; border-color: #8e4c50; color: var(--danger); }
.button.danger:hover, button.danger:hover { background: #3d2023; border-color: var(--danger); }
button:disabled { cursor: not-allowed; opacity: .46; }
label { display: block; margin: 18px 0 7px; color: var(--text); font-weight: 650; }
input, textarea, select {
  width: 100%; min-height: 46px; padding: 10px 12px; border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm); background: #0d1612; color: var(--text); font: inherit;
  transition: border-color 160ms ease-out, box-shadow 160ms ease-out, background 160ms ease-out;
}
input:hover, textarea:hover, select:hover { border-color: #5a7366; }
input::placeholder, textarea::placeholder { color: #7f9288; }
input[type="file"] { min-height: 52px; padding: 6px; color: var(--muted); }
input[type="file"]::file-selector-button {
  min-height: 38px; margin-right: 12px; padding: 7px 12px; border: 0; border-radius: 4px;
  background: var(--surface-raised); color: var(--text); font: inherit; font-weight: 650; cursor: pointer;
}
textarea { min-height: 140px; resize: vertical; }
select { cursor: pointer; }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 3px; }
input:focus-visible, textarea:focus-visible, select:focus-visible {
  outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(184, 242, 91, .2);
}
.form-actions { margin-top: 24px; }
.search-control { flex: 1 1 280px; max-width: 420px; }
.row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 20px; }
.table-scroll { max-width: 100%; overflow-x: auto; margin: 4px -4px 16px; padding: 0 4px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 13px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--muted); font-size: .78rem; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; white-space: nowrap; }
tbody tr { transition: background 160ms ease-out; }
tbody tr:hover { background: rgba(184, 242, 91, .045); }
td:first-child { font-size: .875rem; }
.nutrition { min-width: 0; }
.nutrition th { width: 38%; }
.nutrition td:first-child { font-family: inherit; font-size: inherit; }
.status {
  display: inline-flex; align-items: center; gap: 7px; min-height: 26px; padding: 3px 9px;
  border: 1px solid currentColor; border-radius: 999px; font: 700 .75rem/1.2 Consolas, monospace;
}
.status::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.pending { background: rgba(246, 196, 83, .1); color: var(--amber); }
.completed { background: rgba(130, 226, 186, .1); color: var(--success); }
.muted { color: var(--muted); }
.small { font-size: .8125rem; }
.error { border-color: #804247; background: #2c191b; color: #ffd4d6; }
.notice { border-color: #42654f; background: #15261d; color: #ccefbf; }
pre {
  max-width: 100%; margin: 12px 0; padding: 16px; overflow: auto; white-space: pre-wrap;
  word-break: break-word; background: #080d0b; border: 1px solid #22332b; border-radius: var(--radius-sm);
  color: #d9e7df; font-size: .875rem; line-height: 1.65;
}
.photo { display: block; max-width: 100%; max-height: 560px; margin: 24px auto; border-radius: var(--radius-sm); object-fit: contain; }
.metric { color: var(--accent); font-size: clamp(2.75rem, 8vw, 4.5rem); font-weight: 720; line-height: .95; }
.structured-list { margin: 8px 0 20px; padding-left: 22px; }
.structured-list li { margin: 7px 0; padding-left: 4px; }
details { margin-top: 24px; border-top: 1px solid var(--border); padding-top: 16px; }
summary { width: fit-content; color: var(--accent); font-weight: 650; cursor: pointer; }
@media (max-width: 760px) {
  .wrap { width: min(100% - 32px, 1184px); }
  .nav-shell { display: block; padding: 12px 0 8px; }
  .brand { min-height: 44px; }
  .nav-links { justify-content: flex-start; margin: 4px -8px 0; overflow-x: auto; scrollbar-width: none; }
  .nav-links::-webkit-scrollbar { display: none; }
  .nav-links a { flex: 0 0 auto; }
  main.wrap { padding-block: 24px 40px; }
  .hero { padding-left: 28px; }
  .row { grid-template-columns: 1fr; }
  .actions > .section-heading { flex-basis: 100%; }
  .table-scroll { margin-inline: -12px; padding-inline: 12px; }
  th, td { padding: 12px 10px; }
}
@media (max-width: 420px) {
  .actions .button, .actions button { flex: 1 1 auto; }
  .search-control { flex-basis: 100%; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; }
}
"""


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def layout(title: str, body: str) -> bytes:
    nav_items = (
        ("/daily", "今日建议", title in {"今日建议与明日菜单", "每日复盘"}),
        ("/tasks/photo", "食物照片", title == "上传食物照片"),
        ("/tasks/material", "原材料分析", title == "原材料分析"),
        ("/foods", "营养库", title in {"食品营养库", "新增食品", "编辑食品"}),
        ("/overview", "记录与记忆", title == "记录与记忆"),
    )
    nav_links = "".join(
        (f'<a href="{href}" aria-current="page">{label}</a>' if current else f'<a href="{href}">{label}</a>')
        for href, label, current in nav_items
    )
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · MealCircuit</title><style>{STYLE}</style></head><body>
    <a class="skip-link" href="#main-content">跳到主要内容</a><nav class="nav" aria-label="主导航"><div class="wrap nav-shell"><a class="brand" href="/"><span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>MealCircuit</a><div class="nav-links">{nav_links}</div></div></nav><main class="wrap" id="main-content" tabindex="-1">{body}</main></body></html>"""
    return page.encode("utf-8")


def task_table(tasks: list[dict]) -> str:
    if not tasks:
        return '<p class="muted">暂无任务。</p>'
    rows = "".join(
        f'<tr><td><a href="/tasks/{esc(t["id"])}">{esc(t["id"])}</a></td><td>{"照片识别" if t["type"] == "photo" else "原材料分析"}</td><td><span class="status {esc(t["status"])}">{esc(t["status"])}</span></td><td>{esc(t["created_at"])}</td></tr>'
        for t in tasks
    )
    return f'<div class="table-scroll" tabindex="0" role="region" aria-label="任务列表"><table><thead><tr><th scope="col">任务</th><th scope="col">类型</th><th scope="col">状态</th><th scope="col">创建时间</th></tr></thead><tbody>{rows}</tbody></table></div>'


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
    <form method="post" action="{action}"><input type="hidden" name="source_key" value="{val('source_key')}"><div class="row"><div><label for="food-name">名称 *</label><input id="food-name" name="name" required value="{val('name')}"></div><div><label for="food-brand">品牌</label><input id="food-brand" name="brand" value="{val('brand')}"></div></div>
    <div class="row"><div><label for="food-basis">营养基准 *</label><select id="food-basis" name="basis"><option value="100g" {selected100}>每 100g</option><option value="serving" {selected_serving}>每份</option></select></div><div><label for="food-serving">份量单位（按份时必填）</label><input id="food-serving" name="serving_unit" placeholder="例如：1 片 / 1 包（35g）" value="{val('serving_unit')}"></div></div>
    <div class="row"><div><label for="food-energy">能量 kcal</label><input id="food-energy" type="number" min="0" step="any" name="energy_kcal" value="{val('energy_kcal')}"></div><div><label for="food-protein">蛋白质 g</label><input id="food-protein" type="number" min="0" step="any" name="protein_g" value="{val('protein_g')}"></div><div><label for="food-carbs">碳水 g</label><input id="food-carbs" type="number" min="0" step="any" name="carbs_g" value="{val('carbs_g')}"></div><div><label for="food-fat">脂肪 g</label><input id="food-fat" type="number" min="0" step="any" name="fat_g" value="{val('fat_g')}"></div><div><label for="food-fiber">膳食纤维 g</label><input id="food-fiber" type="number" min="0" step="any" name="fiber_g" value="{val('fiber_g')}"></div><div><label for="food-sodium">钠 mg</label><input id="food-sodium" type="number" min="0" step="any" name="sodium_mg" value="{val('sodium_mg')}"></div></div>
    <div class="row"><div><label for="food-category">食品类别</label><select id="food-category" name="category">{categories}</select></div><div><label for="food-priority">菜单优先级</label><select id="food-priority" name="menu_priority">{priorities}</select></div></div>
    <label for="food-default-portion">默认份量</label><input id="food-default-portion" name="default_portion" placeholder="例如：50–100g / 1包40g" value="{val('default_portion')}"><label for="food-usage-rule">菜单使用条件</label><textarea id="food-usage-rule" name="usage_rule">{val('usage_rule')}</textarea>
    <label for="food-source">来源链接</label><input id="food-source" type="url" name="source_url" value="{val('source_url')}"><label for="food-photo-path">包装照片路径</label><input id="food-photo-path" name="package_photo_path" placeholder="可记录本机路径" value="{val('package_photo_path')}"><label for="food-notes">备注</label><textarea id="food-notes" name="notes">{val('notes')}</textarea><div class="actions form-actions"><button type="submit">保存</button><a class="button secondary" href="/foods">取消</a></div></form>"""


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


def render_daily_review_result(result: dict) -> str:
    status_labels = {"stable": "稳定", "observe": "观察", "adjust": "需要调整", "risk": "风险上升"}
    menu = result["tomorrow_menu"]
    meals = []
    for meal in menu["meals"]:
        protein = meal["protein_g"]
        meals.append(
            f'<article class="card"><h3>{esc(meal["name"])}</h3>'
            f'{render_list(meal["foods"])}'
            f'<p><strong>大致份量：</strong>{esc(meal["portion_guidance"])}</p>'
            f'<p><strong>蛋白估算：</strong>{esc(protein[0])}–{esc(protein[1])}g</p>'
            f'<h4>替换项</h4>{render_list(meal["substitutions"])}</article>'
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
        f'<p class="notice card"><strong>今日状态：</strong>{esc(status_labels[result["system_status"]])}</p>'
        '<h3>事实</h3>' + render_list(result["facts"])
        + '<h3>系统推断</h3>' + render_list(result["inferences"])
        + '<h3>核心建议</h3>' + render_list(result["core_advice"])
        + '<h3>不需要调整</h3>' + render_list(result["do_not_adjust"])
        + '<h3>风险信号</h3>' + render_list(result["risk_signals"])
        + '<h3>优先食品裁决</h3>' + priority_html
        + f'<section class="card"><h2>{esc(menu["date"])} 食堂菜单</h2>'
        + f'<p><strong>每日蛋白目标：</strong>{esc(menu["protein_target_g"][0])}–{esc(menu["protein_target_g"][1])}g</p></section>'
        + '<div class="grid">' + ''.join(meals) + '</div>'
        + '<section class="card"><h3>条件加餐</h3>'
        + f'<p>{esc(snack["condition"])}</p>{render_list(snack["options"])}'
        + f'<h3>训练日调整</h3><p>{esc(menu["training_adjustment"])}</p>'
        + f'<h3>肠胃异常调整</h3><p>{esc(menu["gut_adjustment"])}</p></section>'
        + f'<p class="card"><strong>一句话复盘：</strong>{esc(result["one_line_review"])}</p>'
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

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_security_headers()
        self.end_headers()

    def send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")

    def validate_origin(self) -> None:
        host_header = self.headers.get("Host", "")
        try:
            host_url = urllib.parse.urlsplit(f"//{host_header}")
            host_name = host_url.hostname
        except ValueError as exc:
            raise ValidationError("Host 请求头无效") from exc
        bound_host = str(self.server.server_address[0])
        allowed = {"127.0.0.1", "localhost", "::1", bound_host}
        allow_remote = bool(getattr(self.server, "allow_remote", False))
        if not host_name or (not allow_remote and host_name.lower() not in {item.lower() for item in allowed}):
            raise ValidationError("Host 请求头不在允许范围")
        origin = self.headers.get("Origin")
        if origin:
            origin_url = urllib.parse.urlsplit(origin)
            origin_port = origin_url.port or (443 if origin_url.scheme == "https" else 80)
            host_port = host_url.port or int(self.server.server_address[1])
            if not origin_url.hostname or origin_url.hostname.lower() != host_name.lower() or origin_port != host_port:
                raise ValidationError("拒绝跨来源写入请求")

    def read_urlencoded(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2 * 1024 * 1024:
            raise ValidationError("表单过大")
        raw = self.rfile.read(length).decode("utf-8")
        return {key: values[-1] for key, values in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

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
        self.send_html("操作失败", f'<section class="card error"><h2>操作失败</h2><p>{esc(error)}</p><a class="button secondary" href="javascript:history.back()">返回</a></section>', status)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path.rstrip("/") or "/", urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/":
                tasks = service.list_tasks()
                pending_reviews = service.list_daily_reviews("pending")
                pending = sum(1 for t in tasks if t["status"] == "pending") + len(pending_reviews)
                review_links = render_list([f'{r["review_date"]}：待生成核心建议与次日菜单' for r in pending_reviews], "暂无待复盘日期")
                daily = service.daily_state()
                if daily["status"] == "completed":
                    first_advice = daily["review"]["result_json"]["core_advice"][0]
                    daily_status = f'<p>{esc(first_advice)}</p><p class="muted">明日菜单：{esc(daily["review"]["result_json"]["tomorrow_menu"]["date"])}</p>'
                elif daily["status"] == "pending":
                    daily_status = '<p>今日记录已保存，等待 Agent 生成核心建议和明日菜单。</p>'
                else:
                    daily_status = '<p>今天尚未记录，进入后可直接提交自然语言饮食记录。</p>'
                body = f'''<section class="hero"><h1>本地饮食反馈工作台</h1><p>记录一餐，校准长期趋势。</p></section><div class="grid"><section class="card"><h2>今日建议</h2>{daily_status}<a class="button" href="/daily">查看建议与菜单</a></section><section class="card"><h2>食物照片</h2><p>上传餐食照片，记录份量区间与营养估算。</p><a class="button" href="/tasks/photo">上传食物照片</a></section><section class="card"><h2>原材料分析</h2><p>输入现有食材，获取低失败率组合与调整。</p><a class="button" href="/tasks/material">分析现有食材</a></section></div><section class="card workflow-card"><h2>待办状态</h2><div class="metric">{pending}</div><p class="muted">待 Agent 处理</p><code>python -m mealcircuit.agent_cli pending</code></section><section class="card"><h2>待生成每日复盘</h2>{review_links}</section><section class="card"><h2>最近任务</h2>{task_table(tasks[:10])}</section>'''
                self.send_html("首页", body)
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
                        f'python -m mealcircuit.agent_cli day-complete {esc(daily["date"])} --file result.json</pre>'
                    )
                else:
                    state = '<p><span class="status pending">尚未记录</span></p>'
                    content = f'''<p>直接记录今天吃了什么和身体状态，保存后系统会创建每日复盘待办。</p><form method="post" action="/records"><input type="hidden" name="record_date" value="{esc(daily["date"])}"><label for="daily-input">今日自然语言记录</label><textarea id="daily-input" name="raw_input" required></textarea><div class="form-actions"><button>保存并创建复盘</button></div></form>'''
                self.send_html("今日建议与明日菜单", f'<section class="card"><h1>今日建议与明日菜单</h1>{state}</section><section class="card">{content}</section>')
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
                original = f'<pre>{esc(task["original_input"])}</pre>' if task["original_input"] else '<p class="muted">无补充说明</p>'
                if task["result_json"]:
                    result = render_result(task["type"], task["result_json"])
                else:
                    result = f'<p>等待 Agent 处理。</p><pre>python -m mealcircuit.agent_cli context {esc(task_id)} --output context.json\npython -m mealcircuit.agent_cli complete {esc(task_id)} --file result.json</pre>'
                corrections = "".join(f'<li>{esc(c["correction_json"]["text"])} <span class="muted small">{esc(c["created_at"])}</span></li>' for c in task["corrections"]) or '<li class="muted">暂无用户校正</li>'
                correction_form = f'<form method="post" action="/tasks/{esc(task_id)}/corrections"><label for="task-correction">新增用户校正（保留原结果，不覆盖）</label><textarea id="task-correction" name="text" required></textarea><div class="form-actions"><button type="submit">保存校正</button></div></form>' if task["status"] == "completed" else ""
                body = f'<section class="card"><h1>{"食物识别" if task["type"]=="photo" else "原材料分析"}</h1><p><span class="status {esc(task["status"])}">{esc(task["status"])}</span> · {esc(task_id)}</p>{media}<h3>用户原始输入</h3>{original}</section><section class="card"><h2>Agent 分析结果</h2>{result}</section><section class="card"><h2>用户校正历史</h2><ul>{corrections}</ul>{correction_form}</section>'
                self.send_html("任务详情", body)
            elif path == "/foods":
                q = query.get("q", [""])[0]
                foods = service.list_foods(q)
                priority_labels = {"high": "高", "normal": "普通", "low": "低", "excluded": "不使用"}
                rows = "".join(f'<tr><td>{esc(f["name"])}</td><td>{esc(f["brand"])}</td><td>{esc(priority_labels.get(f["menu_priority"], f["menu_priority"]))}</td><td>{"每100g" if f["basis"]=="100g" else esc(f["serving_unit"])}</td><td>{esc(f["energy_kcal"])}</td><td>{esc(f["protein_g"])}</td><td><a href="/foods/{esc(f["id"])}">编辑</a></td></tr>' for f in foods)
                body = f'<section class="card"><div class="actions"><h1 class="section-heading">食品营养库</h1><a class="button" href="/foods/new">新增食品</a></div><form method="get"><label for="food-search">检索名称或品牌</label><div class="actions"><input class="search-control" id="food-search" name="q" value="{esc(q)}"><button>检索</button></div></form><div class="table-scroll" tabindex="0" role="region" aria-label="食品营养库"><table><thead><tr><th scope="col">名称</th><th scope="col">品牌</th><th scope="col">菜单优先级</th><th scope="col">基准</th><th scope="col">kcal</th><th scope="col">蛋白质</th><th scope="col"></th></tr></thead><tbody>{rows}</tbody></table></div><p class="muted">高优先级表示同功能下优先选择，不表示每天强制追加。</p></section>'
                self.send_html("食品营养库", body)
            elif path == "/foods/new":
                self.send_html("新增食品", f'<section class="card"><h1>新增食品 / 原料</h1>{food_form()}</section>')
            elif path.startswith("/foods/"):
                food = service.get_food(path.split("/")[2])
                self.send_html("编辑食品", f'<section class="card"><h1>编辑食品 / 原料</h1>{food_form(food)}<form method="post" action="/foods/{esc(food["id"])}/delete" onsubmit="return confirm(\'确认删除？历史仍会保留。\')"><button class="danger">删除</button></form></section>')
            elif path == "/overview":
                info = service.overview()
                reviews_by_date = {r["review_date"]: r for r in info["daily_reviews"]}
                records = "".join(
                    f'<li><strong>{esc(r["record_date"])}</strong> {esc(r["raw_input"])} '
                    f'<a href="/reviews/{esc(r["record_date"])}">'
                    f'{"查看核心建议与菜单" if reviews_by_date.get(r["record_date"], {}).get("status") == "completed" else "等待生成复盘"}</a></li>'
                    for r in info["records"]
                ) or '<li class="muted">暂无记录</li>'
                memories = "".join(f'<li><strong>{esc(m["kind"])}</strong> {esc(m["content"])} <span class="muted">{esc(m["evidence"])}</span></li>' for m in info["memories"]) or '<li class="muted">暂无长期记忆</li>'
                adjustments = "".join(f'<li>{esc(a["content"])} <span class="muted">{esc(a["reason"])}</span></li>' for a in info["adjustments"]) or '<li class="muted">暂无当前调整</li>'
                body = f'''<div class="grid"><section class="card"><h1>新增每日记录</h1><form method="post" action="/records"><label for="record-date">日期</label><input id="record-date" type="date" name="record_date" value="{date.today().isoformat()}" required><label for="record-input">自然语言记录</label><textarea id="record-input" name="raw_input" required></textarea><div class="form-actions"><button>保存</button></div></form></section><section class="card"><h2>新增长期记忆</h2><form method="post" action="/memories"><label for="memory-kind">类型</label><select id="memory-kind" name="kind"><option value="preference">已验证偏好</option><option value="gut_trigger">肠胃触发</option><option value="constraint">约束</option><option value="other">其他</option></select><label for="memory-content">内容</label><textarea id="memory-content" name="content" required></textarea><label for="memory-evidence">证据</label><input id="memory-evidence" name="evidence"><div class="form-actions"><button>保存</button></div></form></section><section class="card"><h2>新增当前有效调整</h2><form method="post" action="/adjustments"><label for="adjustment-content">调整内容</label><textarea id="adjustment-content" name="content" required></textarea><label for="adjustment-reason">原因</label><input id="adjustment-reason" name="reason"><div class="form-actions"><button>保存</button></div></form></section></div><section class="card"><h2>近 30 条每日记录</h2><ul>{records}</ul></section><section class="card"><h2>长期记忆</h2><ul>{memories}</ul></section><section class="card"><h2>当前有效调整</h2><ul>{adjustments}</ul></section>'''
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
                        f'python -m mealcircuit.agent_cli day-complete {esc(review_date)} --file result.json</pre>'
                    )
                body = (
                    f'<section class="card"><h1>{esc(review_date)} 每日复盘</h1>'
                    f'<p><span class="status {esc(review["status"])}">{esc(review["status"])}</span> · '
                    f'版本 {esc(review["result_version"])}</p></section>'
                    f'<section class="card"><h2>核心建议与次日菜单</h2>{result}</section>'
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
            if path == "/tasks/photo":
                fields, files = self.read_multipart()
                if "photo" not in files:
                    raise ValidationError("请选择食物照片")
                _, data = files["photo"]
                task = service.create_photo_task(io.BytesIO(data), fields.get("note", ""))
                self.redirect(f'/tasks/{task["id"]}')
            elif path == "/tasks/material":
                task = service.create_material_task(self.read_urlencoded().get("materials", ""))
                self.redirect(f'/tasks/{task["id"]}')
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
            elif path.startswith("/tasks/") and path.endswith("/corrections"):
                task_id = path.split("/")[2]
                service.add_correction(task_id, {"text": self.read_urlencoded().get("text", "")})
                self.redirect(f"/tasks/{task_id}")
            elif path == "/records":
                form = self.read_urlencoded()
                record_date = form.get("record_date", "")
                service.add_daily_record(record_date, form.get("raw_input", ""))
                self.redirect(f"/reviews/{record_date}")
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
