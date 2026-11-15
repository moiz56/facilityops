"""Build data_tenant_test: copies of one real run, each altered in one way.

    PYTHONPATH=src python tests/seal/build_data_tenant_test.py

Takes the first run of data_tenant/sis_facility_checkpoint_route and copies
it, record and evidence, into one folder per case. Every case folder has the
data_tenant layout, so its evidence root is <case>/<route>:

    data_tenant_test/
    ├── cases.json                          what each case changed, for the tests
    ├── original/<route>/<run_folder>/      unchanged: this is the one to seal
    ├── edited_value/<route>/<run_folder>/  one temperature changed          TS-02
    ├── swapped_image/...                   one image replaced, same size    TS-03
    ├── deleted_finding/...                 the last finding removed         TS-04
    ├── reordered_array/...                 two checkpoints swapped          TS-05
    ├── reserialised/...                    keys reversed, indent 4          TS-06
    └── missing_image/...                   one image deleted                TS-07

Seal only original/<route>, never data_tenant_test as a whole: every case
folder is a route folder too, and would be sealed as if it were real.

Refuses to run if data_tenant_test already exists. Delete it to rebuild.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from common import PROJECT_ROOT
from common.paths import PATH_PREFIX
from seal.create import evidence_paths

ROUTE = "sis_facility_checkpoint_route"
SOURCE = PROJECT_ROOT / "data_tenant" / ROUTE
TARGET = PROJECT_ROOT / "data_tenant_test"
CASES = ("original", "edited_value", "swapped_image", "deleted_finding",
         "reordered_array", "reserialised", "missing_image")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, record: dict, indent: int | None = 2) -> None:
    path.write_text(json.dumps(record, indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")


def reverse_keys(value):
    """The same JSON with every object's keys in reverse order. Arrays keep their order."""
    if isinstance(value, dict):
        return {key: reverse_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [reverse_keys(item) for item in value]
    return value


def edit_value(record: dict) -> dict:
    """Change the first checkpoint temperature found. Returns what changed."""
    for index, checkpoint in enumerate(record.get("checkpoints", [])):
        environment = (checkpoint.get("sensor") or {}).get("environment") or {}
        sealed = environment.get("temperature_c")
        if isinstance(sealed, float):
            found = round(sealed - 2.76, 2)
            environment["temperature_c"] = found
            return {"path": f"checkpoints[{index}].sensor.environment.temperature_c",
                    "sealed": sealed, "found": found}
    raise SystemExit("no checkpoint has a float sensor.environment.temperature_c to edit")


def swap_image(run_copy: Path, route_copy: Path, record: dict) -> dict:
    """Replace one image with another, padded to the same size. Returns what changed.

    The donor is a smaller image from the same run, so padding keeps the size
    equal without cutting the donor short. TS-03 needs the size unchanged, so
    that only the hash can catch it.
    """
    paths = evidence_paths(record, PATH_PREFIX)
    existing = [p for p in paths if (route_copy / p).is_file()]
    if len(existing) < 2:
        raise SystemExit("need two evidence files to swap one for the other")
    target = existing[1]
    size = (route_copy / target).stat().st_size
    donors = [p for p in existing if p != target and (route_copy / p).stat().st_size < size]
    if not donors:
        raise SystemExit(f"no evidence file smaller than {target} to swap in")
    donor = donors[0]

    data = (route_copy / donor).read_bytes()
    (route_copy / target).write_bytes(data + b"\0" * (size - len(data)))
    return {"path": target, "donor": donor, "bytes": size}


def main() -> int:
    if TARGET.exists():
        print(f"error: {TARGET} already exists; delete it to rebuild", file=sys.stderr)
        return 1
    runs = sorted(p for p in SOURCE.iterdir() if (p / "run.json").is_file())
    if not runs:
        print(f"error: no runs in {SOURCE}", file=sys.stderr)
        return 1
    run = runs[0]

    for case in CASES:
        shutil.copytree(run, TARGET / case / ROUTE / run.name)

    def record_path(case: str) -> Path:
        return TARGET / case / ROUTE / run.name / "run.json"

    original = read(record_path("original"))
    cases: dict = {"route": ROUTE, "run_folder": run.name, "run_id": original["run_id"]}

    record = read(record_path("edited_value"))
    cases["edited_value"] = edit_value(record)
    write(record_path("edited_value"), record)

    record = read(record_path("swapped_image"))
    cases["swapped_image"] = swap_image(
        TARGET / "swapped_image" / ROUTE / run.name, TARGET / "swapped_image" / ROUTE, record,
    )

    record = read(record_path("deleted_finding"))
    findings = record.get("findings") or []
    if not findings:
        raise SystemExit("the run has no finding to delete")
    removed = findings.pop()
    cases["deleted_finding"] = {"sealed": len(findings) + 1, "found": len(findings),
                                "finding_id": removed.get("finding_id")}
    write(record_path("deleted_finding"), record)

    record = read(record_path("reordered_array"))
    checkpoints = record.get("checkpoints") or []
    if len(checkpoints) < 3:
        raise SystemExit("the run needs three checkpoints to swap two")
    checkpoints[1], checkpoints[2] = checkpoints[2], checkpoints[1]
    cases["reordered_array"] = {"path": "checkpoints", "swapped": [1, 2]}
    write(record_path("reordered_array"), record)

    record = read(record_path("reserialised"))
    write(record_path("reserialised"), reverse_keys(record), indent=4)
    cases["reserialised"] = {"change": "keys reversed at every level, indent 4"}

    missing = evidence_paths(read(record_path("missing_image")), PATH_PREFIX)[0]
    (TARGET / "missing_image" / ROUTE / missing).unlink()
    cases["missing_image"] = {"path": missing}

    write(TARGET / "cases.json", cases)
    print(f"built {TARGET} from {run.name}")
    for case in CASES[1:]:
        print(f"  {case:<16} {cases[case]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
