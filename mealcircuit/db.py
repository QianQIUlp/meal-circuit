from __future__ import annotations

import json
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .db_migrations import (
    CURRENT_SCHEMA_VERSION as DOMAIN_SCHEMA_VERSION,
    create_migration_backup,
    detected_schema_version,
    migrate,
)
from .storage import ROOT, backups_root, db_path


MIGRATIONS = {
    1: "adaptive closed-loop domain tables and review provenance",
    2: "safety policy, target provenance, scoped learning, and append-only feedback",
    3: "published plan projections and constrained rescue provenance",
    4: "versioned rule and experiment provenance",
}
CURRENT_SCHEMA_VERSION = max(MIGRATIONS)


def _migration_checksum(version: int) -> str:
    description = MIGRATIONS[version]
    return hashlib.sha256(f"{version}:{description}".encode("utf-8")).hexdigest()


def _schema_version(path: Path) -> int:
    if not path.is_file() or path.stat().st_size == 0:
        return 0
    conn = sqlite3.connect(path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if not exists:
            return 0
        row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        return int(row[0])
    finally:
        conn.close()


def _backup_before_schema_upgrade(path: Path) -> Path | None:
    if not path.is_file() or path.stat().st_size == 0 or _schema_version(path) >= CURRENT_SCHEMA_VERSION:
        return None
    target_root = backups_root()
    target_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = target_root / f"pre-schema-v{CURRENT_SCHEMA_VERSION}-{stamp}.db"
    suffix = 1
    while target.exists():
        target = target_root / f"pre-schema-v{CURRENT_SCHEMA_VERSION}-{stamp}-{suffix}.db"
        suffix += 1
    source_conn = sqlite3.connect(path)
    backup_conn = sqlite3.connect(target)
    try:
        source_conn.backup(backup_conn)
        result = backup_conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise sqlite3.DatabaseError("迁移前备份完整性检查失败")
    finally:
        backup_conn.close()
        source_conn.close()
    return target
@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    target = (path or db_path()).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    if path is None:
        from .portable import recover_interrupted_import

        recover_interrupted_import()
    target = (path or db_path()).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    existed_before = target.is_file() and target.stat().st_size > 0
    existing_version = _schema_version(target)
    if existing_version > CURRENT_SCHEMA_VERSION:
        raise sqlite3.DatabaseError(
            f"数据库版本 {existing_version} 高于当前程序支持的 {CURRENT_SCHEMA_VERSION}；请升级 MealCircuit"
        )
    _backup_before_schema_upgrade(target)
    with connect(target) as conn:
        domain_version = detected_schema_version(conn)
        domain_backup = (
            create_migration_backup(conn, target, domain_version)
            if existed_before and domain_version < DOMAIN_SCHEMA_VERSION
            else None
        )
        conn.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                checksum TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL CHECK (type IN ('photo', 'material')),
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed')),
                original_input TEXT NOT NULL DEFAULT '',
                image_path TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                result_json TEXT,
                result_provenance_json TEXT,
                result_version INTEGER NOT NULL DEFAULT 0,
                input_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS task_input_history (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                version INTEGER NOT NULL,
                input_text TEXT NOT NULL,
                archived_at TEXT NOT NULL,
                UNIQUE(task_id, version)
            );
            CREATE TABLE IF NOT EXISTS task_corrections (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                correction_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS food_items (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                brand TEXT NOT NULL DEFAULT '',
                basis TEXT NOT NULL CHECK (basis IN ('100g', 'serving')),
                energy_kcal REAL CHECK (energy_kcal IS NULL OR energy_kcal >= 0),
                protein_g REAL CHECK (protein_g IS NULL OR protein_g >= 0),
                carbs_g REAL CHECK (carbs_g IS NULL OR carbs_g >= 0),
                fat_g REAL CHECK (fat_g IS NULL OR fat_g >= 0),
                fiber_g REAL CHECK (fiber_g IS NULL OR fiber_g >= 0),
                sodium_mg REAL CHECK (sodium_mg IS NULL OR sodium_mg >= 0),
                serving_unit TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'other',
                menu_priority TEXT NOT NULL DEFAULT 'normal',
                default_portion TEXT NOT NULL DEFAULT '',
                usage_rule TEXT NOT NULL DEFAULT '',
                source_key TEXT,
                source_url TEXT NOT NULL DEFAULT '',
                package_photo_path TEXT,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS food_item_history (
                id TEXT PRIMARY KEY,
                food_id TEXT NOT NULL,
                event TEXT NOT NULL CHECK (event IN ('create', 'update', 'delete')),
                before_json TEXT,
                after_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_records (
                id TEXT PRIMARY KEY,
                record_date TEXT NOT NULL,
                raw_input TEXT NOT NULL,
                structured_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_checkins (
                id TEXT PRIMARY KEY,
                checkin_date TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_checkin_modules (
                id TEXT PRIMARY KEY,
                checkin_id TEXT NOT NULL REFERENCES daily_checkins(id),
                module_key TEXT NOT NULL CHECK (module_key IN ('weight','training','hunger','sleep','gut')),
                status TEXT NOT NULL DEFAULT 'not_started' CHECK (status IN ('not_started','in_progress','completed','skipped')),
                answers_json TEXT,
                draft_json TEXT,
                schema_version INTEGER NOT NULL DEFAULT 1,
                version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(checkin_id,module_key)
            );
            CREATE TABLE IF NOT EXISTS daily_checkin_module_history (
                id TEXT PRIMARY KEY,
                module_id TEXT NOT NULL REFERENCES daily_checkin_modules(id),
                version INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('completed','skipped')),
                answers_json TEXT,
                archived_at TEXT NOT NULL,
                archive_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS checkin_module_settings (
                module_key TEXT PRIMARY KEY CHECK (module_key IN ('weight','training','hunger','sleep','gut')),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
                sort_order INTEGER NOT NULL,
                frequency TEXT NOT NULL DEFAULT 'daily' CHECK (frequency IN ('daily','optional')),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_reviews (
                id TEXT PRIMARY KEY,
                review_date TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed')),
                source_record_ids_json TEXT NOT NULL DEFAULT '[]',
                source_checkin_versions_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                result_provenance_json TEXT,
                result_version INTEGER NOT NULL DEFAULT 0,
                schema_version INTEGER NOT NULL DEFAULT 1,
                policy_version TEXT NOT NULL DEFAULT '',
                validator_version TEXT NOT NULL DEFAULT '',
                source_manifest_json TEXT NOT NULL DEFAULT '{}',
                context_hash TEXT NOT NULL DEFAULT '',
                agent_run_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS daily_review_history (
                id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL REFERENCES daily_reviews(id),
                version INTEGER NOT NULL,
                source_record_ids_json TEXT NOT NULL,
                source_checkin_versions_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL,
                result_provenance_json TEXT,
                completed_at TEXT,
                archived_at TEXT NOT NULL,
                archive_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('preference', 'gut_trigger', 'constraint', 'other')),
                content TEXT NOT NULL,
                evidence TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS adjustments (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS onboarding_sessions (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'in_progress'
                    CHECK (status IN ('in_progress','completed','abandoned')),
                current_step TEXT NOT NULL DEFAULT 'welcome',
                answers_json TEXT NOT NULL DEFAULT '{}',
                version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS profile_versions (
                id TEXT PRIMARY KEY,
                version INTEGER NOT NULL UNIQUE,
                profile_json TEXT NOT NULL,
                safety_mode TEXT NOT NULL
                    CHECK (safety_mode IN ('setup_required','standard','observation')),
                safety_policy_mode TEXT NOT NULL DEFAULT 'standard',
                source TEXT NOT NULL DEFAULT 'onboarding',
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS goal_versions (
                id TEXT PRIMARY KEY,
                goal_key TEXT NOT NULL,
                version INTEGER NOT NULL,
                profile_version_id TEXT NOT NULL REFERENCES profile_versions(id),
                goal_json TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                created_at TEXT NOT NULL,
                UNIQUE(goal_key, version)
            );
            CREATE TABLE IF NOT EXISTS strategy_versions (
                id TEXT PRIMARY KEY,
                version INTEGER NOT NULL UNIQUE,
                profile_version_id TEXT NOT NULL REFERENCES profile_versions(id),
                goal_version_ids_json TEXT NOT NULL DEFAULT '[]',
                mode TEXT NOT NULL CHECK (mode IN ('portion_guided','numeric_assisted','observation')),
                strategy_json TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                created_at TEXT NOT NULL,
                confirmed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS nutrition_target_versions (
                id TEXT PRIMARY KEY,
                target_key TEXT NOT NULL,
                version INTEGER NOT NULL,
                profile_version_id TEXT NOT NULL REFERENCES profile_versions(id),
                goal_version_ids_json TEXT NOT NULL DEFAULT '[]',
                value_json TEXT NOT NULL,
                unit TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_detail_json TEXT NOT NULL DEFAULT '{}',
                method TEXT NOT NULL,
                applicability_json TEXT NOT NULL DEFAULT '{}',
                safety_mode TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                confirmed_at TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_until TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(target_key, version)
            );
            CREATE TABLE IF NOT EXISTS metric_observations (
                id TEXT PRIMARY KEY,
                metric_key TEXT NOT NULL,
                observed_date TEXT NOT NULL,
                value_json TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_evidence_links (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                observed_date TEXT NOT NULL,
                meal_slot TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (meal_slot IN ('breakfast','lunch','dinner','snack','unknown')),
                role TEXT NOT NULL
                    CHECK (role IN ('consumed','planned','inventory','reference')),
                created_at TEXT NOT NULL,
                UNIQUE(task_id, observed_date, meal_slot, role)
            );
            CREATE TABLE IF NOT EXISTS plan_execution_feedback (
                id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL REFERENCES daily_reviews(id),
                result_version INTEGER NOT NULL,
                plan_date TEXT NOT NULL,
                plan_item_id TEXT NOT NULL,
                meal_name TEXT NOT NULL,
                strategy_key TEXT NOT NULL DEFAULT '',
                planned_snapshot_json TEXT NOT NULL DEFAULT '{}',
                goal_version_ids_json TEXT NOT NULL DEFAULT '[]',
                profile_version_id TEXT,
                strategy_version_id TEXT,
                safety_mode TEXT NOT NULL DEFAULT 'setup_required',
                scope_policy_version TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL
                    CHECK (status IN ('followed','modified','skipped','not_applicable')),
                reason_codes_json TEXT NOT NULL DEFAULT '[]',
                actual_text TEXT NOT NULL DEFAULT '',
                outcome_json TEXT NOT NULL DEFAULT '{}',
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(review_id, result_version, plan_item_id)
            );
            CREATE TABLE IF NOT EXISTS plan_versions (
                id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL REFERENCES daily_reviews(id),
                result_version INTEGER NOT NULL,
                plan_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'published' CHECK (status IN ('published','superseded')),
                schema_version INTEGER NOT NULL DEFAULT 2,
                menu_json TEXT NOT NULL,
                source_manifest_json TEXT NOT NULL DEFAULT '{}',
                context_hash TEXT NOT NULL DEFAULT '',
                policy_version TEXT NOT NULL DEFAULT '',
                validator_version TEXT NOT NULL DEFAULT '',
                agent_run_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(review_id, result_version)
            );
            CREATE TABLE IF NOT EXISTS plan_items (
                id TEXT PRIMARY KEY,
                plan_version_id TEXT NOT NULL REFERENCES plan_versions(id),
                meal_slot TEXT NOT NULL CHECK (meal_slot IN ('breakfast','lunch','dinner','snack','other')),
                name TEXT NOT NULL,
                strategy_key TEXT NOT NULL DEFAULT '',
                item_json TEXT NOT NULL,
                sort_order INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plan_execution_feedback_events (
                id TEXT PRIMARY KEY,
                feedback_id TEXT NOT NULL REFERENCES plan_execution_feedback(id),
                event_version INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                actor_source TEXT NOT NULL DEFAULT 'user',
                occurred_at TEXT NOT NULL,
                UNIQUE(feedback_id, event_version)
            );
            CREATE TABLE IF NOT EXISTS question_events (
                id TEXT PRIMARY KEY,
                question_date TEXT NOT NULL,
                question_key TEXT NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','answered','skipped','expired')),
                answer_json TEXT,
                priority INTEGER NOT NULL DEFAULT 0,
                prompt TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                expected_impact TEXT NOT NULL DEFAULT '',
                question_schema_json TEXT NOT NULL DEFAULT '{}',
                subject_json TEXT NOT NULL DEFAULT '{}',
                cooldown_key TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(question_date, question_key)
            );
            CREATE TABLE IF NOT EXISTS inventory_items (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'available'
                    CHECK (status IN ('available','used','not_bought','discarded','unknown')),
                amount_text TEXT NOT NULL DEFAULT '',
                expires_on TEXT,
                source_kind TEXT NOT NULL DEFAULT 'user',
                source_id TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inventory_events (
                id TEXT PRIMARY KEY,
                inventory_id TEXT NOT NULL REFERENCES inventory_items(id),
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                occurred_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS adaptation_candidates (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('friction','preference','constraint','strategy','association')),
                statement TEXT NOT NULL,
                scope_json TEXT NOT NULL DEFAULT '{}',
                evidence_summary_json TEXT NOT NULL DEFAULT '{}',
                confidence TEXT NOT NULL CHECK (confidence IN ('weak','emerging','strong')),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','accepted','rejected','snoozed','expired')),
                proposed_effect_json TEXT NOT NULL DEFAULT '{}',
                goal_version_ids_json TEXT NOT NULL DEFAULT '[]',
                profile_version_id TEXT,
                strategy_version_id TEXT,
                safety_mode TEXT NOT NULL DEFAULT 'setup_required',
                scope_policy_version TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                decided_at TEXT
            );
            CREATE TABLE IF NOT EXISTS adaptation_evidence (
                id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL REFERENCES adaptation_candidates(id),
                evidence_type TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
                stance TEXT NOT NULL CHECK (stance IN ('support','counterexample')),
                created_at TEXT NOT NULL,
                UNIQUE(candidate_id, evidence_type, evidence_id, stance)
            );
            CREATE TABLE IF NOT EXISTS adaptive_rules (
                id TEXT PRIMARY KEY,
                origin TEXT NOT NULL CHECK (origin IN ('user_declared','candidate','imported')),
                kind TEXT NOT NULL,
                statement TEXT NOT NULL,
                scope_json TEXT NOT NULL DEFAULT '{}',
                effect_json TEXT NOT NULL DEFAULT '{}',
                goal_version_ids_json TEXT NOT NULL DEFAULT '[]',
                profile_version_id TEXT,
                strategy_version_id TEXT,
                safety_mode TEXT NOT NULL DEFAULT 'setup_required',
                scope_policy_version TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive','expired')),
                version INTEGER NOT NULL DEFAULT 1,
                expires_on TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS adaptive_experiments (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed','active','completed','cancelled')),
                variable_key TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                starts_on TEXT,
                ends_on TEXT,
                result_json TEXT,
                goal_version_ids_json TEXT NOT NULL DEFAULT '[]',
                profile_version_id TEXT,
                strategy_version_id TEXT,
                safety_mode TEXT NOT NULL DEFAULT 'setup_required',
                scope_policy_version TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rescue_sessions (
                id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL REFERENCES daily_reviews(id),
                result_version INTEGER NOT NULL,
                plan_date TEXT NOT NULL DEFAULT '',
                plan_item_id TEXT NOT NULL,
                issue_code TEXT NOT NULL,
                input_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','completed')),
                result_json TEXT,
                source_manifest_json TEXT NOT NULL DEFAULT '{}',
                context_hash TEXT NOT NULL DEFAULT '',
                agent_run_id TEXT,
                policy_version TEXT NOT NULL DEFAULT '',
                validator_version TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS agent_runs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                context_hash TEXT NOT NULL,
                context_schema_version INTEGER NOT NULL DEFAULT 1,
                result_schema_version INTEGER NOT NULL DEFAULT 1,
                policy_version TEXT NOT NULL DEFAULT '',
                validator_version TEXT NOT NULL DEFAULT '',
                source_manifest_json TEXT NOT NULL DEFAULT '{}',
                validation_attempts_json TEXT NOT NULL DEFAULT '[]',
                result_hash TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
                error_summary TEXT NOT NULL DEFAULT '',
                usage_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_task_input_history ON task_input_history(task_id, version);
            CREATE INDEX IF NOT EXISTS idx_records_date ON daily_records(record_date);
            CREATE INDEX IF NOT EXISTS idx_checkins_date ON daily_checkins(checkin_date);
            CREATE INDEX IF NOT EXISTS idx_checkin_modules ON daily_checkin_modules(checkin_id,module_key);
            CREATE INDEX IF NOT EXISTS idx_checkin_history ON daily_checkin_module_history(module_id,version);
            CREATE INDEX IF NOT EXISTS idx_daily_reviews_status ON daily_reviews(status, review_date);
            CREATE INDEX IF NOT EXISTS idx_daily_review_history ON daily_review_history(review_id, version);
            CREATE INDEX IF NOT EXISTS idx_food_name ON food_items(name, brand);
            CREATE INDEX IF NOT EXISTS idx_profile_active ON profile_versions(active, version);
            CREATE INDEX IF NOT EXISTS idx_goal_active ON goal_versions(active, goal_key, version);
            CREATE INDEX IF NOT EXISTS idx_strategy_active ON strategy_versions(active, version);
            CREATE INDEX IF NOT EXISTS idx_target_active ON nutrition_target_versions(active, target_key, version);
            CREATE INDEX IF NOT EXISTS idx_metric_date ON metric_observations(metric_key, observed_date);
            CREATE INDEX IF NOT EXISTS idx_task_evidence_date ON task_evidence_links(observed_date, role);
            CREATE INDEX IF NOT EXISTS idx_feedback_plan_date ON plan_execution_feedback(plan_date, status);
            CREATE INDEX IF NOT EXISTS idx_plan_date ON plan_versions(plan_date, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_plan_items ON plan_items(plan_version_id, sort_order);
            CREATE INDEX IF NOT EXISTS idx_feedback_events ON plan_execution_feedback_events(feedback_id, event_version);
            CREATE INDEX IF NOT EXISTS idx_question_date ON question_events(question_date, status, priority);
            CREATE INDEX IF NOT EXISTS idx_inventory_status ON inventory_items(status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_candidates_status ON adaptation_candidates(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_rules_status ON adaptive_rules(status, updated_at);
            """
        )
        migration_columns = {row["name"] for row in conn.execute("PRAGMA table_info(schema_migrations)")}
        if "checksum" not in migration_columns:
            conn.execute("ALTER TABLE schema_migrations ADD COLUMN checksum TEXT NOT NULL DEFAULT ''")
        existing_food_columns = {row["name"] for row in conn.execute("PRAGMA table_info(food_items)")}
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "input_version" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN input_version INTEGER NOT NULL DEFAULT 1")
        task_provenance = {
            "schema_version": "INTEGER NOT NULL DEFAULT 1",
            "policy_version": "TEXT NOT NULL DEFAULT ''",
            "validator_version": "TEXT NOT NULL DEFAULT ''",
            "source_manifest_json": "TEXT NOT NULL DEFAULT '{}'",
            "context_hash": "TEXT NOT NULL DEFAULT ''",
            "agent_run_id": "TEXT",
        }
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        for name, definition in task_provenance.items():
            if name not in task_columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        food_migrations = {
            "fiber_g": "REAL",
            "sodium_mg": "REAL",
            "category": "TEXT NOT NULL DEFAULT 'other'",
            "menu_priority": "TEXT NOT NULL DEFAULT 'normal'",
            "default_portion": "TEXT NOT NULL DEFAULT ''",
            "usage_rule": "TEXT NOT NULL DEFAULT ''",
            "source_key": "TEXT",
        }
        for name, definition in food_migrations.items():
            if name not in existing_food_columns:
                conn.execute(f"ALTER TABLE food_items ADD COLUMN {name} {definition}")
        history_columns = {row["name"] for row in conn.execute("PRAGMA table_info(daily_review_history)")}
        if "archive_reason" not in history_columns:
            conn.execute("ALTER TABLE daily_review_history ADD COLUMN archive_reason TEXT NOT NULL DEFAULT ''")
        review_columns = {row["name"] for row in conn.execute("PRAGMA table_info(daily_reviews)")}
        if "source_checkin_versions_json" not in review_columns:
            conn.execute("ALTER TABLE daily_reviews ADD COLUMN source_checkin_versions_json TEXT NOT NULL DEFAULT '{}'")
        history_columns = {row["name"] for row in conn.execute("PRAGMA table_info(daily_review_history)")}
        if "source_checkin_versions_json" not in history_columns:
            conn.execute("ALTER TABLE daily_review_history ADD COLUMN source_checkin_versions_json TEXT NOT NULL DEFAULT '{}'")
        review_columns = {row["name"] for row in conn.execute("PRAGMA table_info(daily_reviews)")}
        review_migrations = {
            "schema_version": "INTEGER NOT NULL DEFAULT 1",
            "review_mode": "TEXT NOT NULL DEFAULT 'standard'",
            "source_manifest_json": "TEXT NOT NULL DEFAULT '{}'",
            "context_hash": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in review_migrations.items():
            if name not in review_columns:
                conn.execute(f"ALTER TABLE daily_reviews ADD COLUMN {name} {definition}")
        history_columns = {row["name"] for row in conn.execute("PRAGMA table_info(daily_review_history)")}
        for name, definition in review_migrations.items():
            if name not in history_columns:
                conn.execute(f"ALTER TABLE daily_review_history ADD COLUMN {name} {definition}")
        feedback_columns = {row["name"] for row in conn.execute("PRAGMA table_info(plan_execution_feedback)")}
        if "strategy_key" not in feedback_columns:
            conn.execute("ALTER TABLE plan_execution_feedback ADD COLUMN strategy_key TEXT NOT NULL DEFAULT ''")
        if "planned_snapshot_json" not in feedback_columns:
            conn.execute("ALTER TABLE plan_execution_feedback ADD COLUMN planned_snapshot_json TEXT NOT NULL DEFAULT '{}'")
        feedback_scope = {
            "goal_version_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "profile_version_id": "TEXT",
            "strategy_version_id": "TEXT",
            "safety_mode": "TEXT NOT NULL DEFAULT 'setup_required'",
            "scope_policy_version": "TEXT NOT NULL DEFAULT ''",
        }
        feedback_columns = {row["name"] for row in conn.execute("PRAGMA table_info(plan_execution_feedback)")}
        for name, definition in feedback_scope.items():
            if name not in feedback_columns:
                conn.execute(f"ALTER TABLE plan_execution_feedback ADD COLUMN {name} {definition}")
        profile_columns = {row["name"] for row in conn.execute("PRAGMA table_info(profile_versions)")}
        if "safety_policy_mode" not in profile_columns:
            conn.execute("ALTER TABLE profile_versions ADD COLUMN safety_policy_mode TEXT NOT NULL DEFAULT 'standard'")
            conn.execute(
                "UPDATE profile_versions SET safety_policy_mode=CASE WHEN safety_mode='standard' THEN 'standard' ELSE 'observation' END"
            )
        scoped_tables = {
            "adaptation_candidates": {
                "profile_version_id": "TEXT",
                "strategy_version_id": "TEXT",
                "safety_mode": "TEXT NOT NULL DEFAULT 'setup_required'",
                "scope_policy_version": "TEXT NOT NULL DEFAULT ''",
            },
            "adaptive_rules": {
                "profile_version_id": "TEXT",
                "strategy_version_id": "TEXT",
                "safety_mode": "TEXT NOT NULL DEFAULT 'setup_required'",
                "scope_policy_version": "TEXT NOT NULL DEFAULT ''",
                "version": "INTEGER NOT NULL DEFAULT 1",
            },
            "adaptive_experiments": {
                "goal_version_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "profile_version_id": "TEXT",
                "strategy_version_id": "TEXT",
                "safety_mode": "TEXT NOT NULL DEFAULT 'setup_required'",
                "scope_policy_version": "TEXT NOT NULL DEFAULT ''",
                "version": "INTEGER NOT NULL DEFAULT 1",
            },
            "agent_runs": {
                "policy_version": "TEXT NOT NULL DEFAULT ''",
                "validator_version": "TEXT NOT NULL DEFAULT ''",
                "source_manifest_json": "TEXT NOT NULL DEFAULT '{}'",
                "validation_attempts_json": "TEXT NOT NULL DEFAULT '[]'",
                "result_hash": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, columns in scoped_tables.items():
            existing_columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing_columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        review_columns = {row["name"] for row in conn.execute("PRAGMA table_info(daily_reviews)")}
        review_provenance = {
            "agent_run_id": "TEXT",
            "policy_version": "TEXT NOT NULL DEFAULT ''",
            "validator_version": "TEXT NOT NULL DEFAULT ''",
            "plan_version_id": "TEXT",
        }
        for name, definition in review_provenance.items():
            if name not in review_columns:
                conn.execute(f"ALTER TABLE daily_reviews ADD COLUMN {name} {definition}")
        history_columns = {row["name"] for row in conn.execute("PRAGMA table_info(daily_review_history)")}
        for name, definition in review_provenance.items():
            if name not in history_columns:
                conn.execute(f"ALTER TABLE daily_review_history ADD COLUMN {name} {definition}")
        rescue_columns = {row["name"] for row in conn.execute("PRAGMA table_info(rescue_sessions)")}
        rescue_provenance = {
            "plan_date": "TEXT NOT NULL DEFAULT ''",
            "source_manifest_json": "TEXT NOT NULL DEFAULT '{}'",
            "context_hash": "TEXT NOT NULL DEFAULT ''",
            "agent_run_id": "TEXT",
            "policy_version": "TEXT NOT NULL DEFAULT ''",
            "validator_version": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in rescue_provenance.items():
            if name not in rescue_columns:
                conn.execute(f"ALTER TABLE rescue_sessions ADD COLUMN {name} {definition}")
        question_columns = {row["name"] for row in conn.execute("PRAGMA table_info(question_events)")}
        question_metadata = {
            "prompt": "TEXT NOT NULL DEFAULT ''",
            "question_schema_json": "TEXT NOT NULL DEFAULT '{}'",
            "subject_json": "TEXT NOT NULL DEFAULT '{}'",
            "cooldown_key": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in question_metadata.items():
            if name not in question_columns:
                conn.execute(f"ALTER TABLE question_events ADD COLUMN {name} {definition}")
        timestamp = "1970-01-01T00:00:00+00:00"
        for sort_order, module_key in enumerate(("weight", "training", "hunger", "sleep", "gut")):
            conn.execute(
                "INSERT OR IGNORE INTO checkin_module_settings(module_key,enabled,sort_order,frequency,updated_at) VALUES(?,1,?,'daily',?)",
                (module_key, sort_order, timestamp),
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_food_source_key ON food_items(source_key) WHERE source_key IS NOT NULL"
        )
        timestamp_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for version, description in MIGRATIONS.items():
            expected_checksum = _migration_checksum(version)
            existing = conn.execute(
                "SELECT description,checksum FROM schema_migrations WHERE version=?", (version,)
            ).fetchone()
            if existing:
                checksum = existing["checksum"] or expected_checksum
                if checksum != expected_checksum:
                    raise sqlite3.DatabaseError(f"数据库迁移 {version} 校验和不匹配")
                conn.execute(
                    "UPDATE schema_migrations SET description=?,checksum=? WHERE version=?",
                    (description, expected_checksum, version),
                )
            else:
                conn.execute(
                    "INSERT INTO schema_migrations(version,description,applied_at,checksum) VALUES(?,?,?,?)",
                    (version, description, timestamp_now, expected_checksum),
                )
        migrate(
            conn,
            target,
            existed_before=existed_before,
            initial_backup=(domain_version, domain_backup) if domain_backup is not None else None,
        )
        backfilled = conn.execute(
            "SELECT value FROM app_metadata WHERE key='domain_backfill_version'"
        ).fetchone()
        backfill_version = int(backfilled[0]) if backfilled else 0
        if backfill_version < 2:
            from .domain_store import seed_current_entities

            seed_current_entities(conn)
            conn.execute(
                """INSERT INTO app_metadata(key,value) VALUES('domain_backfill_version','2')
                   ON CONFLICT(key) DO UPDATE SET value='2'"""
            )
        else:
            from .domain_store import refresh_configuration_entities

            refresh_configuration_entities(conn)


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    for key in (
        "result_json", "structured_json", "correction_json", "source_record_ids_json",
        "result_provenance_json", "source_checkin_versions_json", "answers_json", "draft_json", "profile_json",
        "goal_json", "goal_version_ids_json", "strategy_json", "value_json",
        "source_detail_json", "applicability_json",
        "reason_codes_json", "outcome_json", "answer_json", "payload_json", "question_schema_json", "subject_json",
        "planned_snapshot_json",
        "scope_json", "evidence_summary_json", "proposed_effect_json", "effect_json",
        "plan_json", "menu_json", "item_json", "usage_json", "source_manifest_json", "validation_attempts_json",
    ):
        if key in result and result[key]:
            result[key] = json.loads(result[key])
    return result
