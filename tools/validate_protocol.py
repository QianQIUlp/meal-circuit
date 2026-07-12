from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTOCOL = ROOT / "protocol"
sys.path.insert(0, str(ROOT))

from mealcircuit import service
from mealcircuit.domain import three_way_merge
from mealcircuit.validation import validate_daily_review_result, validate_result


def main() -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:
        raise SystemExit("install jsonschema to validate protocol contracts") from exc

    schema = json.loads((PROTOCOL / "domain-v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    revision = json.loads((PROTOCOL / "fixtures" / "domain-revision.json").read_text(encoding="utf-8"))
    validator.validate(revision)
    with zipfile.ZipFile(PROTOCOL / "fixtures" / "portable-v1.zip") as archive:
        for name in archive.namelist():
            if name.startswith("entities/") and name.endswith(".jsonl"):
                for line in archive.read(name).decode("utf-8").splitlines():
                    if line.strip():
                        validator.validate(json.loads(line))
    checkins = json.loads((PROTOCOL / "checkin-modules-v1.json").read_text(encoding="utf-8"))
    keys = [item["key"] for item in checkins["modules"]]
    if len(keys) != len(set(keys)) or set(keys) != {"weight", "training", "hunger", "sleep", "gut"}:
        raise SystemExit("check-in module contract is incomplete or duplicated")
    machines = json.loads((PROTOCOL / "state-machines-v1.json").read_text(encoding="utf-8"))["machines"]
    for name, machine in machines.items():
        if machine["initial"] not in machine["transitions"]:
            raise SystemExit(f"state machine {name} has no initial transition row")
    contract = json.loads((PROTOCOL / "fixtures" / "contract-v1.json").read_text(encoding="utf-8"))
    if contract["context"]["window_days"] != 14:
        raise SystemExit("context fixture must use the 14-day product window")
    validate_result("photo", contract["photo_result"])
    validate_result("material", contract["material_result"])
    daily = contract["daily"]
    validate_daily_review_result(daily["result"], daily["settings"])
    if {item["food_id"] for item in daily["result"]["priority_food_decisions"]} != set(daily["priority_food_ids"]):
        raise SystemExit("daily result fixture does not cover priority foods")
    service._validate_ingredient_carryover_decisions(daily["result"], daily["carryovers"])
    for case in contract["merge_cases"]:
        merged, conflicts = three_way_merge(case["base"], case["local"], case["remote"])
        if merged != case["expected"] or conflicts != case["conflicts"]:
            raise SystemExit(f"merge fixture failed: {case['name']}")
    print("Domain, Portable, context/result, merge, check-in, and state-machine contracts are valid")


if __name__ == "__main__":
    main()
