from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = PROJECT_ROOT / "protocol/sync-v1.openapi.json"
SYNC_ENV_PREFIX = "MEALCIRCUIT_SYNC_"

sys.path.insert(0, str(PROJECT_ROOT))


@contextmanager
def canonical_sync_environment():
    saved = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(SYNC_ENV_PREFIX)
    }
    try:
        for key in saved:
            os.environ.pop(key, None)
        yield
    finally:
        for key in list(os.environ):
            if key.startswith(SYNC_ENV_PREFIX):
                os.environ.pop(key, None)
        os.environ.update(saved)


def generate_openapi() -> str:
    with canonical_sync_environment():
        from sync_server.app import create_app

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = create_app(
                f"sqlite:///{(root / 'openapi.db').as_posix()}",
                root / "blobs",
                registration_mode="closed",
                create_schema=True,
            )
            generated = json.dumps(app.openapi(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            app.state.engine.dispose()
    return generated


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the frozen MealCircuit Sync v1 OpenAPI document")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    destination = args.output or DEFAULT_DESTINATION
    generated = generate_openapi()
    if args.check:
        if not destination.is_file() or destination.read_text(encoding="utf-8") != generated:
            raise SystemExit("protocol/sync-v1.openapi.json is stale; run tools/generate_openapi.py")
        print("OpenAPI contract is current")
    else:
        destination.write_text(generated, encoding="utf-8")
        print(destination.resolve())


if __name__ == "__main__":
    main()
