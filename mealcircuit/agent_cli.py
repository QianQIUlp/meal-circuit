from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import adaptive, personalization, portability, service
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


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def load_json_value(path: str):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"无法读取 JSON 文件：{exc}") from exc
    return value


def load_json(path: str) -> dict:
    value = load_json_value(path)
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

    setup = sub.add_parser("setup", help="管理可恢复的目标与安全初始化")
    setup_sub = setup.add_subparsers(dest="setup_command", required=True)
    setup_sub.add_parser("status")
    setup_sub.add_parser("start")
    setup_show = setup_sub.add_parser("show")
    setup_show.add_argument("session_id")
    setup_save = setup_sub.add_parser("save")
    setup_save.add_argument("session_id")
    setup_save.add_argument("step", choices=personalization.ONBOARDING_STEPS)
    setup_save.add_argument("--file", "-f", required=True)
    setup_save.add_argument("--version", type=int, required=True)
    setup_preview = setup_sub.add_parser("preview")
    setup_preview.add_argument("session_id")
    setup_complete = setup_sub.add_parser("complete")
    setup_complete.add_argument("session_id")
    setup_complete.add_argument("--file", "-f", required=True, help="目标契约确认 JSON")
    setup_complete.add_argument("--version", type=int, required=True)

    plan = sub.add_parser("plan", help="查看正式发布的日期计划与回执")
    plan.add_argument("date")
    feedback = sub.add_parser("feedback", help="记录或查看计划执行回执")
    feedback_sub = feedback.add_subparsers(dest="feedback_command", required=True)
    feedback_add = feedback_sub.add_parser("add")
    feedback_add.add_argument("date")
    feedback_add.add_argument("plan_item_id")
    feedback_add.add_argument("status", choices=sorted(adaptive.FEEDBACK_STATUSES))
    feedback_add.add_argument("--reason", action="append", default=[])
    feedback_add.add_argument("--actual", default="")
    feedback_add.add_argument("--outcome-file")
    feedback_add.add_argument("--expected-version", type=int)
    feedback_history = feedback_sub.add_parser("history")
    feedback_history.add_argument("feedback_id")

    questions = sub.add_parser("questions", help="查看和回答当天自适应问题")
    question_sub = questions.add_subparsers(dest="question_command", required=True)
    question_list = question_sub.add_parser("list")
    question_list.add_argument("date")
    question_answer = question_sub.add_parser("answer")
    question_answer.add_argument("question_id")
    question_answer.add_argument("--file", "-f", required=True)
    question_answer.add_argument("--version", type=int, required=True)
    question_skip = question_sub.add_parser("skip")
    question_skip.add_argument("question_id")
    question_skip.add_argument("--version", type=int, required=True)

    learning = sub.add_parser("learning", help="管理学习候选、确认规则和实验")
    learning_sub = learning.add_subparsers(dest="learning_command", required=True)
    learning_list = learning_sub.add_parser("list")
    learning_list.add_argument("--status", choices=["pending", "accepted", "rejected", "snoozed", "expired"])
    learning_decide = learning_sub.add_parser("decide")
    learning_decide.add_argument("candidate_id")
    learning_decide.add_argument("decision", choices=["accept", "reject", "snooze"])
    learning_decide.add_argument("--statement")
    learning_sub.add_parser("rules")
    rule_status = learning_sub.add_parser("rule-status")
    rule_status.add_argument("rule_id")
    rule_status.add_argument("status", choices=["active", "inactive"])
    learning_sub.add_parser("experiments")
    experiment_propose = learning_sub.add_parser("experiment-propose")
    experiment_propose.add_argument("variable_key")
    experiment_propose.add_argument("--file", "-f", required=True)
    experiment_start = learning_sub.add_parser("experiment-start")
    experiment_start.add_argument("experiment_id")
    experiment_start.add_argument("starts_on")
    experiment_start.add_argument("--days", type=int, required=True)
    experiment_finish = learning_sub.add_parser("experiment-finish")
    experiment_finish.add_argument("experiment_id")
    experiment_finish.add_argument("--file", "-f", required=True)
    experiment_finish.add_argument("--cancel", action="store_true")

    inventory = sub.add_parser("inventory", help="管理库存及临期状态")
    inventory_sub = inventory.add_subparsers(dest="inventory_command", required=True)
    inventory_list = inventory_sub.add_parser("list")
    inventory_list.add_argument("--all", action="store_true")
    inventory_add = inventory_sub.add_parser("add")
    inventory_add.add_argument("name")
    inventory_add.add_argument("--amount", default="")
    inventory_add.add_argument("--expires-on")
    inventory_update = inventory_sub.add_parser("update")
    inventory_update.add_argument("inventory_id")
    inventory_update.add_argument("status", choices=sorted(adaptive.INVENTORY_STATUSES))
    inventory_update.add_argument("--version", type=int, required=True)
    inventory_update.add_argument("--amount")

    evidence = sub.add_parser("evidence-link", help="将照片或原材料任务关联到真实日期与餐次")
    evidence.add_argument("task_id")
    evidence.add_argument("date")
    evidence.add_argument("role", choices=sorted(adaptive.EVIDENCE_ROLES))
    evidence.add_argument("--meal", default="unknown", choices=sorted(adaptive.MEAL_SLOTS))

    rescue = sub.add_parser("rescue", help="处理正式计划执行中的临时故障")
    rescue_sub = rescue.add_subparsers(dest="rescue_command", required=True)
    rescue_start = rescue_sub.add_parser("start")
    rescue_start.add_argument("date")
    rescue_start.add_argument("plan_item_id")
    rescue_start.add_argument("issue", choices=sorted(adaptive.RESCUE_ISSUES))
    rescue_start.add_argument("--text", default="")
    rescue_context = rescue_sub.add_parser("context")
    rescue_context.add_argument("rescue_id")
    rescue_context.add_argument("--output", "-o")
    rescue_complete = rescue_sub.add_parser("complete")
    rescue_complete.add_argument("rescue_id")
    rescue_complete.add_argument("--file", "-f", required=True)
    rescue_generate = rescue_sub.add_parser("generate")
    rescue_generate.add_argument("rescue_id")

    metric = sub.add_parser("metric", help="记录和查看可校准指标")
    metric_sub = metric.add_subparsers(dest="metric_command", required=True)
    metric_add = metric_sub.add_parser("add")
    metric_add.add_argument("key")
    metric_add.add_argument("date")
    metric_add.add_argument("--file", "-f", required=True)
    metric_list = metric_sub.add_parser("list")
    metric_list.add_argument("--key")
    metric_list.add_argument("--limit", type=int, default=100)
    calibration = sub.add_parser("calibration", help="查看当前周期校准资格与证据覆盖")
    calibration.add_argument("--date")

    export = sub.add_parser("export-bundle", help="导出带哈希清单的完整本地数据包")
    export.add_argument("--output", "-o")
    import_bundle = sub.add_parser("import-bundle", help="预览或恢复完整本地数据包")
    import_bundle.add_argument("bundle")
    import_bundle.add_argument("--apply", action="store_true")
    return parser


def main() -> None:
    configure_utf8_stdio()
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
            emit(service.submit_task_result(args.task_id, load_json(args.file)), args.output)
        elif args.command == "generate":
            emit(service.generate_task_result(args.task_id), args.output)
        elif args.command == "correct":
            emit(service.add_correction(args.task_id, {"text": args.text}))
        elif args.command == "schema":
            if args.type == "daily":
                emit(service.daily_review_schema())
            else:
                policy = personalization.generation_policy(args.type)
                emit(service.result_schema(args.type, fact_only=policy["fact_only"]))
        elif args.command == "day-list":
            emit(service.list_daily_reviews(args.status))
        elif args.command == "day-context":
            emit(service.daily_review_context(args.date, args.days), args.output)
        elif args.command == "day-complete":
            emit(service.submit_daily_review(args.date, load_json(args.file)), args.output)
        elif args.command == "day-generate":
            emit(service.generate_daily_review(args.date), args.output)
        elif args.command == "setup":
            if args.setup_command == "status":
                emit(personalization.onboarding_status())
            elif args.setup_command == "start":
                emit(personalization.start_onboarding())
            elif args.setup_command == "show":
                emit(personalization.get_onboarding(args.session_id))
            elif args.setup_command == "save":
                emit(personalization.save_onboarding_step(
                    args.session_id, args.step, load_json(args.file), args.version
                ))
            elif args.setup_command == "preview":
                emit(personalization.onboarding_preview(args.session_id))
            elif args.setup_command == "complete":
                emit(personalization.complete_onboarding(
                    args.session_id, args.version, load_json(args.file)
                ))
        elif args.command == "plan":
            emit(adaptive.get_plan_for_date(args.date))
        elif args.command == "feedback":
            if args.feedback_command == "add":
                outcome = load_json(args.outcome_file) if args.outcome_file else {}
                emit(adaptive.save_plan_feedback(
                    args.date, args.plan_item_id, args.status, reason_codes=args.reason,
                    actual_text=args.actual, outcome=outcome, expected_version=args.expected_version,
                    actor_source="cli",
                ))
            else:
                emit(adaptive.plan_feedback_history(args.feedback_id))
        elif args.command == "questions":
            if args.question_command == "list":
                emit(adaptive.schedule_questions(args.date))
            elif args.question_command == "answer":
                emit(adaptive.answer_question(
                    args.question_id, load_json_value(args.file), args.version
                ))
            else:
                emit(adaptive.answer_question(args.question_id, None, args.version, skip=True))
        elif args.command == "learning":
            if args.learning_command == "list":
                emit(adaptive.list_candidates(args.status))
            elif args.learning_command == "decide":
                emit(adaptive.decide_candidate(
                    args.candidate_id, args.decision, statement=args.statement
                ))
            elif args.learning_command == "rules":
                emit(adaptive.list_rules(active_only=False))
            elif args.learning_command == "rule-status":
                emit(adaptive.set_rule_status(args.rule_id, args.status))
            elif args.learning_command == "experiments":
                emit(adaptive.list_experiments(current_scope_only=False))
            elif args.learning_command == "experiment-propose":
                emit(adaptive.propose_experiment(args.variable_key, load_json(args.file)))
            elif args.learning_command == "experiment-start":
                emit(adaptive.activate_experiment(args.experiment_id, args.starts_on, args.days))
            else:
                emit(adaptive.finish_experiment(
                    args.experiment_id, load_json(args.file), cancel=args.cancel
                ))
        elif args.command == "inventory":
            if args.inventory_command == "list":
                emit(adaptive.list_inventory(active_only=not args.all))
            elif args.inventory_command == "add":
                emit(adaptive.create_inventory_item(
                    args.name, args.amount, expires_on=args.expires_on
                ))
            else:
                emit(adaptive.update_inventory_status(
                    args.inventory_id, args.status, args.version, args.amount
                ))
        elif args.command == "evidence-link":
            emit(adaptive.link_task_evidence(args.task_id, args.date, args.role, args.meal))
        elif args.command == "rescue":
            if args.rescue_command == "start":
                emit(adaptive.create_rescue_session(
                    args.date, args.plan_item_id, args.issue, args.text
                ))
            elif args.rescue_command == "context":
                emit(service.rescue_context(args.rescue_id), args.output)
            elif args.rescue_command == "complete":
                emit(service.submit_rescue_result(args.rescue_id, load_json(args.file)))
            else:
                emit(service.generate_rescue(args.rescue_id))
        elif args.command == "metric":
            if args.metric_command == "add":
                emit(personalization.record_metric(args.key, args.date, load_json_value(args.file)))
            else:
                emit(personalization.list_metrics(args.key, args.limit))
        elif args.command == "calibration":
            emit(adaptive.calibration_snapshot(args.date))
        elif args.command == "export-bundle":
            emit(portability.export_bundle(args.output))
        elif args.command == "import-bundle":
            emit(
                portability.restore_bundle(args.bundle, confirm=True)
                if args.apply else portability.preview_import(args.bundle)
            )
    except KeyError as exc:
        print(f"错误：记录不存在：{exc.args[0]}", file=sys.stderr)
        raise SystemExit(2) from exc
    except ValidationError as exc:
        print(f"校验失败：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
