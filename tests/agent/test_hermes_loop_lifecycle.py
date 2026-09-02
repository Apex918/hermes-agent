"""Offline contract tests for the Hermes Loop lifecycle/evidence ledger."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.hermes_loop_lifecycle import (
    HermesLoopStore,
    LifecycleState,
    LifecycleTransitionError,
)


_MAIN_PATH = [
    LifecycleState.CREATED,
    LifecycleState.PLANNED,
    LifecycleState.DISPATCHED,
    LifecycleState.RUNNING,
    LifecycleState.RESULT_RECEIVED,
    LifecycleState.VERIFIED,
    LifecycleState.EXPERIENCE_CAPTURED,
    LifecycleState.CLOSED,
]


def test_fixture_task_reaches_closed_with_auditable_evidence(tmp_path: Path) -> None:
    store = HermesLoopStore(tmp_path / "loop.db", clock=lambda: 1_700_000_000.0)
    task_id = store.create_task("generate a fixture report", metadata={"source": "fixture"})

    assert store.state(task_id) is LifecycleState.CREATED
    store.plan(task_id, plan={"steps": ["render", "verify"]}, actor="planner")
    store.dispatch(task_id, worker="offline-fixture", actor="dispatcher")
    store.start(task_id, actor="worker")
    store.record_result(
        task_id,
        result={"status": "ok", "artifact": "fixture://report.json"},
        evidence=[{"kind": "fixture", "uri": "fixture://report.json", "sha256": "abc123"}],
        actor="worker",
    )
    store.verify(
        task_id,
        passed=True,
        evidence=[{"kind": "assertion", "name": "schema", "value": True}],
        verifier="offline-test",
    )
    store.capture_experience(
        task_id,
        experience={"lesson": "fixture result is schema-valid", "reusable": True},
        actor="curator",
    )
    store.close(task_id, actor="coordinator")

    audit = store.audit(task_id)
    assert store.state(task_id) is LifecycleState.CLOSED
    assert audit["states"] == [state.value for state in _MAIN_PATH]
    assert audit["evidence_count"] == 2
    assert audit["result"]["artifact"] == "fixture://report.json"
    assert audit["experience"]["reusable"] is True
    assert audit["external_effects"] is False
    assert all(event["actor"] for event in audit["events"])
    assert len(audit["evidence"][0]["content_hash"]) == 64

    store.close_db()
    with HermesLoopStore(tmp_path / "loop.db") as reopened:
        persisted = reopened.audit(task_id)
    assert persisted["state"] == LifecycleState.CLOSED.value
    assert persisted["states"] == audit["states"]
    assert persisted["evidence"] == audit["evidence"]


def test_failed_run_can_replan_and_return_to_main_path(tmp_path: Path) -> None:
    store = HermesLoopStore(tmp_path / "loop.db")
    task_id = store.create_task("retry fixture task")
    store.plan(task_id, plan={"version": 1})
    store.dispatch(task_id, worker="offline-fixture")
    store.start(task_id)
    store.fail(
        task_id,
        reason="worker exceeded fixture deadline",
        kind=LifecycleState.TIMEOUT,
        evidence=[{"kind": "fixture", "deadline_seconds": 1}],
    )
    assert store.state(task_id) is LifecycleState.TIMEOUT

    store.replan(task_id, plan={"version": 2}, reason="bounded retry")
    assert store.state(task_id) is LifecycleState.REPLAN
    store.plan(task_id, plan={"version": 2}, actor="planner")
    assert store.state(task_id) is LifecycleState.PLANNED

    audit = store.audit(task_id)
    assert audit["failure_states"] == [LifecycleState.TIMEOUT.value]
    assert audit["evidence_count"] == 1
    assert audit["states"][-2:] == [LifecycleState.REPLAN.value, LifecycleState.PLANNED.value]


def test_verification_failure_is_evidence_bearing_and_replannable(tmp_path: Path) -> None:
    store = HermesLoopStore(tmp_path / "loop.db")
    task_id = store.create_task("invalid fixture task")
    store.plan(task_id, plan={"steps": ["render"]})
    store.dispatch(task_id, worker="offline-fixture")
    store.start(task_id)
    store.record_result(task_id, result={"status": "ok"}, evidence=[])
    store.verify(
        task_id,
        passed=False,
        evidence=[{"kind": "assertion", "name": "schema", "value": False}],
        verifier="offline-test",
        reason="schema assertion failed",
    )
    assert store.state(task_id) is LifecycleState.VERIFICATION_FAILED
    store.replan(task_id, plan={"fix": "add required field"}, reason="verification failed")

    audit = store.audit(task_id)
    assert audit["failure_states"] == [LifecycleState.VERIFICATION_FAILED.value]
    assert audit["evidence_count"] == 1
    assert audit["events"][-1]["to_state"] == LifecycleState.REPLAN.value


def test_illegal_transition_and_unstructured_payload_fail_closed(tmp_path: Path) -> None:
    store = HermesLoopStore(tmp_path / "loop.db")
    task_id = store.create_task("guarded fixture task")

    with pytest.raises(LifecycleTransitionError, match="CREATED -> RUNNING"):
        store.transition(task_id, LifecycleState.RUNNING)

    with pytest.raises(TypeError, match="JSON object"):
        store.plan(task_id, plan="not-json")

    # The durable representation is JSON, not a Python repr or an external URI.
    store.plan(task_id, plan={"ok": True})
    raw = (tmp_path / "loop.db").read_bytes()
    assert b"not-json" not in raw
    json.dumps(store.audit(task_id))
