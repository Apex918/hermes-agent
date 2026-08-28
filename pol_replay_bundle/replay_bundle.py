"""Run the N6/N7 offline replay and feed its evidence into N8A."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

from control_tower import run as run_control_tower


ROOT = Path(__file__).parent


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replay_n6(path: Path) -> Dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report.get("schema") == "daily-operations-cockpit/v1"
    assert report.get("read_only") is True
    facts = [item for section in report.get("sections", {}).values() for item in section]
    assert facts and all(item.get("source") and "source_id" in item for item in facts)
    preview = report.get("write_preview", {})
    assert preview.get("production_write") is False
    assert preview.get("approval_required") is True
    return {"schema": report["schema"], "fact_count": len(facts), "sha256": sha256(path), "status": "passed"}


def replay_n7(path: Path, manifest_path: Path) -> Dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert fixture["network_policy"] == {"network_allowed": False, "external_side_effects": False, "source": "checked-in-local-fixture"}
    previews: List[Dict[str, Any]] = []
    for meeting in fixture["meetings"]:
        for entry in meeting.get("work_items", []):
            work = entry["work_item"]
            owner = entry.get("owner", work.get("owner"))
            due = entry.get("due_date")
            status = "preview" if owner and due else "needs_input"
            previews.append({"meeting_id": meeting["meeting_id"], "work_item_id": work["work_item_id"], "status": status, "production_write": False, "approval_level": "R2"})
    expected = manifest["preview_status_counts"]
    actual = {status: sum(item["status"] == status for item in previews) for status in ("preview", "needs_input")}
    assert actual == expected
    assert manifest["safety"]["network_allowed"] is False
    assert manifest["safety"]["external_side_effects"] is False
    assert manifest["safety"]["production_write"] is False
    return {"schema": fixture["schema_version"], "preview_count": len(previews), "status_counts": actual, "sha256": sha256(path), "status": "passed", "previews": previews}


def replay(out: Path) -> Dict[str, Any]:
    n6 = replay_n6(ROOT / "inputs/n6-daily-report.json")
    n7 = replay_n7(ROOT / "inputs/n7-meeting-minutes.v1.json", ROOT / "inputs/n7-manifest.json")
    evidence = [
        {"source": "n6.daily-report", "source_id": n6["sha256"]},
        {"source": "n7.meeting-minutes", "source_id": n7["sha256"]},
    ]
    cases = json.loads((ROOT / "golden_cases.json").read_text(encoding="utf-8"))
    control_path = out.with_name("control-report.json")
    control = run_control_tower(str(ROOT / "golden_cases.json"), str(control_path))
    result = {"schema": "ai-native-pol-replay-bundle/v1", "read_only": True, "network_allowed": False, "external_side_effects": False, "evidence": evidence, "n6": n6, "n7": n7, "control_tower": control}
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(replay(args.out), ensure_ascii=False, indent=2))
