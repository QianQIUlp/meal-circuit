from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from mealcircuit import service
from mealcircuit.configuration import initialize_private_home
from mealcircuit.db import connect
from mealcircuit.sync import login_sync, register_sync, sync_now, unlink_sync


PASSWORD = "synthetic-cross-client-password"
LOGIN = "cross-client"
API_KEY_CANARY = "SYNTHETIC-CROSS-CLIENT-API-KEY"


def configure_home(path: Path) -> None:
    os.environ["MEALCIRCUIT_HOME"] = str(path.resolve())
    os.environ.pop("MEALCIRCUIT_DB", None)
    initialize_private_home()
    (path / "settings.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "timezone": "UTC",
                "meal_environment": "cross-client-test",
                "protein_target_g": [90, 120],
                "portion_method": "synthetic",
                "missing_training_default": "unknown",
                "compensation_boundary": "standard",
                "home_cooking": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    (path / "profile.md").write_text("# Cross-client synthetic profile\n", encoding="utf-8")
    (path / "doctrine.private.md").write_text("# Cross-client synthetic doctrine\n", encoding="utf-8")


def prepare(home: Path, state_path: Path, server_url: str) -> None:
    configure_home(home)
    os.environ["MEALCIRCUIT_OPENAI_API_KEY"] = API_KEY_CANARY
    captured: list[str] = []
    register_sync(
        server_url=server_url,
        login_name=LOGIN,
        password=PASSWORD,
        device_name="python-desktop",
        confirm_recovery_key=lambda value: not captured.append(value),
        allow_insecure_localhost=True,
    )
    task = service.create_material_task("python-offline-canary")
    result = sync_now()
    if result["accepted"] <= 0:
        raise RuntimeError("Python client did not upload its offline revisions")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "server_url": server_url,
                "login": LOGIN,
                "password": PASSWORD,
                "recovery_key": captured[0],
                "python_task_id": task["id"],
            }
        ),
        encoding="utf-8",
    )
    try:
        state_path.chmod(0o600)
    except OSError:
        pass


def _audit_server_copies(paths: list[Path], forbidden: list[bytes]) -> None:
    for root in paths:
        candidates = [root] if root.is_file() else list(root.rglob("*")) if root.is_dir() else []
        for candidate in candidates:
            if not candidate.is_file():
                continue
            value = candidate.read_bytes()
            for marker in forbidden:
                if marker and marker in value:
                    raise RuntimeError(f"server-side plaintext marker found in {candidate}: {marker!r}")


def verify(
    home: Path,
    state_path: Path,
    *,
    server_database: Path | None = None,
    server_blob_root: Path | None = None,
    server_log: Path | None = None,
    backup_output: Path | None = None,
) -> None:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    configure_home(home)
    login_sync(
        server_url=state["server_url"],
        login_name=state["login"],
        password=state["password"],
        device_name="python-verifier",
        recovery_key=state["recovery_key"],
        allow_insecure_localhost=True,
    )
    result = sync_now()
    with connect() as connection:
        android = connection.execute(
            "SELECT id FROM daily_records WHERE raw_input='android-offline-canary'"
        ).fetchone()
    if android is None or result["applied"] <= 0:
        raise RuntimeError("Python client did not receive the Android offline revision")
    unlink_sync()
    audit_paths = [path for path in (server_blob_root, server_log) if path]
    if server_database:
        audit_paths.extend(server_database.parent.glob(f"{server_database.name}*"))
    if server_database and backup_output:
        backup_output.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(server_database)) as source, closing(sqlite3.connect(backup_output)) as destination:
            source.backup(destination)
        audit_paths.append(backup_output)
    _audit_server_copies(
        audit_paths,
        [
            b"python-offline-canary",
            b"android-offline-canary",
            PASSWORD.encode(),
            state["recovery_key"].encode(),
            API_KEY_CANARY.encode(),
        ],
    )
    state_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Android/Python E2EE synchronization acceptance harness")
    parser.add_argument("phase", choices=["prepare", "verify"])
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--server-url", default="http://127.0.0.1:18080")
    parser.add_argument("--server-database", type=Path)
    parser.add_argument("--server-blob-root", type=Path)
    parser.add_argument("--server-log", type=Path)
    parser.add_argument("--backup-output", type=Path)
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare(args.home, args.state, args.server_url)
    else:
        verify(
            args.home,
            args.state,
            server_database=args.server_database,
            server_blob_root=args.server_blob_root,
            server_log=args.server_log,
            backup_output=args.backup_output,
        )


if __name__ == "__main__":
    main()
