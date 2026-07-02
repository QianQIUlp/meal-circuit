from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .storage import ROOT, db_path


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
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL CHECK (type IN ('photo', 'material')),
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed')),
                original_input TEXT NOT NULL DEFAULT '',
                image_path TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                result_json TEXT,
                result_version INTEGER NOT NULL DEFAULT 0
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
            CREATE TABLE IF NOT EXISTS daily_reviews (
                id TEXT PRIMARY KEY,
                review_date TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed')),
                source_record_ids_json TEXT NOT NULL DEFAULT '[]',
                result_json TEXT,
                result_version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS daily_review_history (
                id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL REFERENCES daily_reviews(id),
                version INTEGER NOT NULL,
                source_record_ids_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
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
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_records_date ON daily_records(record_date);
            CREATE INDEX IF NOT EXISTS idx_daily_reviews_status ON daily_reviews(status, review_date);
            CREATE INDEX IF NOT EXISTS idx_daily_review_history ON daily_review_history(review_id, version);
            CREATE INDEX IF NOT EXISTS idx_food_name ON food_items(name, brand);
            """
        )
        existing_food_columns = {row["name"] for row in conn.execute("PRAGMA table_info(food_items)")}
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
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_food_source_key ON food_items(source_key) WHERE source_key IS NOT NULL"
        )


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("result_json", "structured_json", "correction_json", "source_record_ids_json"):
        if key in result and result[key]:
            result[key] = json.loads(result[key])
    return result
