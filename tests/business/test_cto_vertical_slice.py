from __future__ import annotations

from datetime import datetime, timezone

from business.ai_business_os import build_cto_agent


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _snapshot(observed_at: str, **payload):
    return {"observed_at": observed_at, **payload}


def test_cto_agent_returns_read_only_decision_with_kanban_handoff():
    agent = build_cto_agent()
    decision = agent.evaluate(
        {
            "repo_status": _snapshot("2026-08-20T11:00:00+00:00", branch="main", dirty=False),
            "ci_pipeline": _snapshot("2026-08-20T11:30:00+00:00", status="passed"),
            "architecture_notes": _snapshot("2026-08-20T10:00:00+00:00", risks=[]),
        },
        now=NOW,
    )

    assert decision.verdict == "ready"
    assert decision.handoff["kanban_handoff"] == "required"
    assert all(item.freshness == "fresh" for item in decision.evidence)
    assert all(item.payload.get("external_write") is not True for item in decision.evidence)


def test_cto_agent_fails_closed_when_ci_evidence_is_missing():
    agent = build_cto_agent()
    decision = agent.evaluate(
        {
            "repo_status": _snapshot("2026-08-20T11:00:00+00:00", branch="main"),
            "ci_pipeline": None,
            "architecture_notes": _snapshot("2026-08-20T10:00:00+00:00", risks=[]),
        },
        now=NOW,
    )

    assert decision.verdict == "blocked"
    assert "ci_pipeline" in decision.handoff["gaps"]


def test_cto_agent_does_not_allow_production_write_or_release():
    agent = build_cto_agent()
    assert agent.manifest["role"] == "CTO"
    assert agent.manifest["max_effect_class"] == "R1"
    assert agent.manifest["allowed_external_writes"] == []
    assert agent.proposal["mode"] == "read_only"
