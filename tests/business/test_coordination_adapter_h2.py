"""H2 fresh-process regression tests for the public Hermes boundary."""

from __future__ import annotations

import subprocess
import sys


def test_public_adapter_imports_in_a_fresh_process():
    code = (
        "from business.ai_business_os import "
        "CoordinationAdapter, HermesCoordinationAdapter; "
        "assert CoordinationAdapter is HermesCoordinationAdapter"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_public_adapter_produces_only_coordination_plan():
    from business.ai_business_os import CoordinationAdapter

    fixture = {
        "mission": {"mission_id": "m-h2", "objective": "inspect local evidence"},
        "evidence": [{"evidence_id": "e-h2", "source": "fixture"}],
        "decision": {"decision_id": "d-h2", "verdict": "proceed"},
        "action_request": {
            "request_id": "a-h2",
            "action": "prepare_review",
            "mode": "read_only",
        },
    }
    result = CoordinationAdapter().coordinate(fixture)

    assert result.status == "coordination-only"
    assert result.policy.network_access is False
    assert result.policy.live_trading is False
    assert result.policy.order_submission == "forbidden"
    assert result.external_effects == 0
    assert result.integration_status == "coordination-only/worktree-only"


def test_public_adapter_rejects_external_source_before_planning():
    from business.ai_business_os import CoordinationAdapter, CoordinationAdapterError

    try:
        CoordinationAdapter().coordinate("https://invalid.example/fixture.json")
    except CoordinationAdapterError as exc:
        assert "network" in str(exc)
    else:
        raise AssertionError("external source must fail closed")
