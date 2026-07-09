from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import service
from .configuration import configuration_status, initialize_private_home
from .db import init_db
from .migration import apply_migration, migration_preview
from .validation import ValidationError


def emit(value: object, output: str | None = None) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    if output:
        Path(output).write_text(payload + "\n", encoding="utf-8")
        print(f"已写入 {Path(output).resolve()}")
    else:
        print(payload)


def load_json(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"无法读取 JSON 文件：{exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError("JSON 顶层必须是对象")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mealcircuit.agent_cli",
        description="MealCircuit（食回路）Agent-in-the-loop CLI；可选用用户自己的 API Key 手动生成结果",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="创建仓库外私人数据目录和配置模板（不覆盖现有文件）")
    sub.add_parser("doctor", help="检查私人路径、配置和规则加载状态")
    migration = sub.add_parser("migrate-data", help="从旧 DietOS 工程安全迁移私人数据")
    migration.add_argument("--from-repo", required=True)
    migration.add_argument("--apply", action="store_true", help="实际复制；默认只预览")
    listing = sub.add_parser("list", help="列出任务")
    listing.add_argument("--status", choices=["pending", "completed"])
    sub.add_parser("pending", help="统一列出照片、原材料和每日复盘待办")
    context = sub.add_parser("context", help="导出任务、总纲、近 14 天记录和长期上下文")
    context.add_argument("task_id")
    context.add_argument("--output", "-o")
    context.add_argument("--days", type=int, default=14, choices=range(1, 31), metavar="1-30")
    complete = sub.add_parser("complete", help="校验并完成任务；已完成结果不可覆盖")
    complete.add_argument("task_id")
    complete.add_argument("--file", "-f", required=True)
    complete.add_argument("--output", "-o")
    generate = sub.add_parser("generate", help="使用用户环境变量中的模型 API Key 生成并完成任务")
    generate.add_argument("task_id")
    generate.add_argument("--output", "-o")
    correct = sub.add_parser("correct", help="追加用户校正历史")
    correct.add_argument("task_id")
    correct.add_argument("--text", required=True)
    schema = sub.add_parser("schema", help="输出合法结果示例结构")
    schema.add_argument("type", choices=["photo", "material", "daily"])
    day_list = sub.add_parser("day-list", help="列出每日复盘")
    day_list.add_argument("--status", choices=["pending", "completed"])
    day_context = sub.add_parser("day-context", help="导出指定日期、近14天、总纲和菜单设置")
    day_context.add_argument("date")
    day_context.add_argument("--output", "-o")
    day_context.add_argument("--days", type=int, default=14, choices=range(1, 31), metavar="1-30")
    day_complete = sub.add_parser("day-complete", help="校验并提交每日复盘和次日菜单")
    day_complete.add_argument("date")
    day_complete.add_argument("--file", "-f", required=True)
    day_complete.add_argument("--output", "-o")
    day_generate = sub.add_parser("day-generate", help="使用用户环境变量中的模型 API Key 生成并提交每日复盘")
    day_generate.add_argument("date")
    day_generate.add_argument("--output", "-o")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            emit(initialize_private_home())
            return
        if args.command == "doctor":
            emit(configuration_status())
            return
        if args.command == "migrate-data":
            emit(apply_migration(args.from_repo) if args.apply else migration_preview(args.from_repo))
            return
        init_db()
        if args.command == "list":
            emit(service.list_tasks(args.status))
        elif args.command == "pending":
            emit(service.pending_work())
        elif args.command == "context":
            emit(service.task_context(args.task_id, args.days), args.output)
        elif args.command == "complete":
            emit(service.complete_task(args.task_id, load_json(args.file)), args.output)
        elif args.command == "generate":
            emit(service.generate_task_result(args.task_id), args.output)
        elif args.command == "correct":
            emit(service.add_correction(args.task_id, {"text": args.text}))
        elif args.command == "schema":
            emit(service.daily_review_schema() if args.type == "daily" else service.result_schema(args.type))
        elif args.command == "day-list":
            emit(service.list_daily_reviews(args.status))
        elif args.command == "day-context":
            emit(service.daily_review_context(args.date, args.days), args.output)
        elif args.command == "day-complete":
            emit(service.complete_daily_review(args.date, load_json(args.file)), args.output)
        elif args.command == "day-generate":
            emit(service.generate_daily_review(args.date), args.output)
    except KeyError as exc:
        print(f"错误：记录不存在：{exc.args[0]}", file=sys.stderr)
        raise SystemExit(2) from exc
    except ValidationError as exc:
        print(f"校验失败：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
