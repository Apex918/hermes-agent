"""H1 tests for the offline three-system coordination adapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from business.ai_business_os.coordination_adapter import (
    CoordinationAdapterError,
    CoordinationPolicy,
    HermesCoordinationAdapter,
    OfflinePolicyError,
)


FIXTURE = {
    "source_kind": "explicit-fixture",
    "mission": {
        "mission_id": "mission-001",
        "objective": "Review the local release readiness",
    },
    "evidence": [
        {
            "evidence_id": "evidence-001",
            "summary": "CI artifact is available locally",
            "source": "fixture",
        }
    ],
    "decision": {
        "decision_id": "decision-001",
        "verdict": "proceed",
        "rationale": "The local evidence is sufficient for coordination.",
    },
    "action_request": {
        "request_id": "action-001",
        "action": "prepare_release_checklist",
        "mode": "read_only",
    },
}


def test_fixture_coordinates_through_existing_hermes_planner_without_effects():
    result = HermesCoordinationAdapter().coordinate(FIXTURE)

    assert result.status == "coordination-only"
    assert result.plan["objective"] == FIXTURE["mission"]["objective"]
    assert result.integration_status == "coordination-only/worktree-only"
    assert result.policy.network_access is False
    assert result.policy.live_trading is False
    assert result.policy.order_submission == "forbidden"
    assert result.policy.external_effects == 0
    assert result.external_effects == 0
    assert result.provenance["source_kind"] == "explicit-fixture"


def test_adapter_is_available_from_business_os_package_boundary():
    from business.ai_business_os import CoordinationAdapter

    assert CoordinationAdapter is HermesCoordinationAdapter


def test_local_command_center_artifact_is_supported_and_provenance_is_preserved(tmp_path: Path):
    path = tmp_path / "command-center.json"
    path.write_text(json.dumps(FIXTURE), encoding="utf-8")

    result = HermesCoordinationAdapter().coordinate(path)

    assert result.provenance["source_kind"] == "local-command-center-artifact"
    assert result.provenance["path"] == str(path)
    assert result.context["mission"]["mission_id"] == "mission-001"


def test_explicit_fixture_accepts_json_text_without_treating_it_as_a_path():
    result = HermesCoordinationAdapter().coordinate_fixture(json.dumps(FIXTURE))

    assert result.provenance == {"source_kind": "explicit-fixture"}
    assert result.context["mission"]["objective"] == FIXTURE["mission"]["objective"]


def test_nested_command_center_context_is_unwrapped_without_following_references(tmp_path: Path):
    path = tmp_path / "command-center.json"
    path.write_text(json.dumps({"context": FIXTURE, "artifact_ref": "https://invalid"}), encoding="utf-8")

    result = HermesCoordinationAdapter().coordinate_artifact(path)

    assert result.context["action_request"]["request_id"] == "action-001"
    assert result.provenance["path"] == str(path)


def test_network_artifact_sources_are_rejected_before_planning():
    adapter = HermesCoordinationAdapter()

    with pytest.raises(CoordinationAdapterError, match="network"):
        adapter.coordinate("https://example.invalid/command-center.json")


def test_live_or_order_action_is_rejected_fail_closed():
    fixture = {**FIXTURE, "action_request": {"request_id": "a", "action": "submit_order"}}

    with pytest.raises(OfflinePolicyError, match="order"):
        HermesCoordinationAdapter().coordinate(fixture)


@pytest.mark.parametrize("action", ["network.request", "websocket.subscribe", "redis.write"])
def test_external_transport_actions_are_rejected_fail_closed(action):
    fixture = {**FIXTURE, "action_request": {"request_id": "a", "action": action}}

    with pytest.raises(OfflinePolicyError):
        HermesCoordinationAdapter().coordinate(fixture)


def test_external_url_in_action_request_is_rejected_fail_closed():
    fixture = {
        **FIXTURE,
        "action_request": {
            "request_id": "a",
            "action": "inspect_artifact",
            "endpoint": "https://example.invalid/artifact.json",
        },
    }

    with pytest.raises(OfflinePolicyError, match="external"):
        HermesCoordinationAdapter().coordinate(fixture)


def test_policy_cannot_enable_network_trading_or_external_effects():
    with pytest.raises(OfflinePolicyError):
        CoordinationPolicy(network_access=True)
    with pytest.raises(OfflinePolicyError):
        CoordinationPolicy(live_trading=True)
    with pytest.raises(OfflinePolicyError):
        CoordinationPolicy(order_submission="allowed")
    with pytest.raises(OfflinePolicyError):
        CoordinationPolicy(external_effects=1)
    with pytest.raises(OfflinePolicyError):
        CoordinationPolicy(external_effects=False)


def test_malformed_context_fails_before_planner_call():
    class Planner:
        def plan(self, *_args, **_kwargs):
            raise AssertionError("planner must not receive malformed context")

    with pytest.raises(CoordinationAdapterError, match="mission"):
        HermesCoordinationAdapter(orchestrator=Planner()).coordinate(
            {"mission": {}, "evidence": [], "decision": {}, "action_request": {}}
        )
