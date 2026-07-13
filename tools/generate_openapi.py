from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sync_server.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the frozen MealCircuit Sync v1 OpenAPI document")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    destination = Path("protocol/sync-v1.openapi.json")
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
    if args.check:
        if not destination.is_file() or destination.read_text(encoding="utf-8") != generated:
            raise SystemExit("protocol/sync-v1.openapi.json is stale; run tools/generate_openapi.py")
        print("OpenAPI contract is current")
    else:
        destination.write_text(generated, encoding="utf-8")
        print(destination.resolve())


if __name__ == "__main__":
    main()
