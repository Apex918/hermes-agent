"""Offline, fail-closed control-tower evaluation harness (N8A)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List

REQUIRED_METRICS = (
    "evidence_coverage",
    "assertion_pass_rate",
    "format_valid",
    "overreach_blocked",
    "approval_wait",
    "task_success_rate",
    "cost",
    "blocked",
)


@dataclass
class EvaluationResult:
    case_id: str
    status: str
    assertions: List[Dict[str, Any]]
    metrics: Dict[str, Any]
    trace_id: str
    kanban_event_ids: List[str]
    failure_reasons: List[str]


def _stable_id(prefix: str, case_id: str, suffix: str = "") -> str:
    digest = hashlib.sha256(f"{case_id}:{suffix}".encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _event(case_id: str, status: str, trace_id: str, reason: str = "") -> Dict[str, Any]:
    return {
        "event_id": _stable_id("evt", case_id, status + ":" + reason),
        "case_id": case_id,
        "type": "evaluation." + status,
        "trace_id": trace_id,
        "reason": reason,
        "external_write": False,
    }


def evaluate(case: Dict[str, Any]) -> EvaluationResult:
    case_id = str(case.get("case_id", "missing"))
    trace_id = _stable_id("trace", case_id)
    assertions: List[Dict[str, Any]] = []
    failures: List[str] = []
    inp, exp = case.get("input", {}), case.get("expected", {})

    def check(name: str, actual: Any, expected: Any) -> None:
        ok = actual == expected
        assertions.append({"name": name, "passed": ok, "actual": actual, "expected": expected})
        if not ok:
            failures.append(name)

    evidence_present = bool(inp.get("evidence", []))
    format_valid = isinstance(inp.get("output"), dict) and bool(inp["output"].get("schema"))
    approval_gate = bool(inp.get("approval_required", True))
    task_success = bool(inp.get("task_success", False))
    overreach_blocked = not bool(inp.get("external_write", False))
    check("evidence_present", evidence_present, exp.get("evidence_present", True))
    check("format_valid", format_valid, exp.get("format_valid", True))
    check("approval_gate", approval_gate, exp.get("approval_gate", True))
    check("task_success", task_success, exp.get("task_success", False))
    check("overreach_blocked", overreach_blocked, exp.get("overreach_blocked", True))

    expected_status = exp.get("result_status", "passed")
    status = str(expected_status) if not failures else "failed"
    reason = ";".join(failures)
    events = [_event(case_id, status, trace_id, reason)]
    if failures or status == "blocked":
        events.append(_event(case_id, "trace_retained", trace_id, reason or "expected_blocked"))
    metrics = {
        "evidence_coverage": 1.0 if evidence_present else 0.0,
        "assertion_pass_rate": sum(a["passed"] for a in assertions) / len(assertions),
        "format_valid": format_valid,
        "overreach_blocked": overreach_blocked,
        "approval_wait": inp.get("approval_wait_seconds"),
        "task_success_rate": 1.0 if task_success else 0.0,
        "cost": inp.get("cost", {"currency": "USD", "amount": None}),
        "blocked": status == "blocked",
    }
    return EvaluationResult(case_id, status, assertions, metrics, trace_id,
                            [e["event_id"] for e in events], failures)


def run(path: str, out: str) -> Dict[str, Any]:
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    results = [evaluate(case) for case in data["cases"]]
    report = {
        "schema": "ai-native-control-tower/v2",
        "read_only": True,
        "source_fixture": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "results": [asdict(r) for r in results],
        "summary": {
            "cases": len(results),
            "passed": sum(r.status == "passed" for r in results),
            "failed": sum(r.status == "failed" for r in results),
            "blocked": sum(r.status == "blocked" for r in results),
            "failures_retain_trace": all(
                r.trace_id and r.kanban_event_ids
                for r in results if r.status in ("failed", "blocked")
            ),
        },
    }
    Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.fixture, args.out), ensure_ascii=False, indent=2))
