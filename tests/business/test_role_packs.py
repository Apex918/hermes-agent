from datetime import datetime, timedelta, timezone

from business.ai_business_os.role_packs import (
    build_all_role_packs,
    build_p0_role_packs,
    build_p1_sensitive_role_packs,
    build_sensitive_role_packs,
)


BASELINE_ROLES = ["CEO", "CFO", "COO", "CMO", "CRO", "CPO", "CTO", "CISO"]
SENSITIVE_ROLES = ["CHRO", "GC", "CCO", "CDO"]


def _fresh_snapshot(label: str, *, now: datetime, age_hours: int = 1):
    return {
        "observed_at": (now - timedelta(hours=age_hours)).isoformat(),
        "label": label,
        "status": "green",
    }


def test_p0_role_packs_cover_the_eight_baseline_roles():
    packs = build_p0_role_packs()
    assert [pack.role for pack in packs] == BASELINE_ROLES
    assert len(packs) == 8


def test_p1_sensitive_role_packs_cover_the_four_sensitive_roles():
    packs = build_p1_sensitive_role_packs()
    assert [pack.role for pack in packs] == SENSITIVE_ROLES
    assert len(packs) == 4


def test_build_all_role_packs_combines_baseline_and_sensitive_packs():
    packs = build_all_role_packs()
    assert [pack.role for pack in packs] == BASELINE_ROLES + SENSITIVE_ROLES
    assert len(packs) == 12
    assert build_sensitive_role_packs() == build_p1_sensitive_role_packs()


def test_each_pack_exposes_manifest_proposal_decision_and_read_only_adapters():
    packs = build_all_role_packs()

    for pack in packs:
        assert pack.manifest["role"] == pack.role
        assert pack.manifest["tier"] in {"P0", "P1"}
        assert pack.manifest["evidence_coverage"] == 100
        assert pack.manifest["adapter_ids"]

        assert pack.proposal["read_only_adapters"]
        assert pack.proposal["kanban_handoff"] == "required"

        assert pack.decision["fail_closed_on"] == ["missing", "expired"]
        assert pack.decision["verdict"] in {"pending_evidence", "pending_approval"}

        assert pack.adapters
        assert all(adapter.read_only for adapter in pack.adapters)


def test_baseline_role_pack_handoff_blocks_on_missing_data_and_passes_with_fresh_snapshots():
    packs = build_p0_role_packs()
    cfo = next(pack for pack in packs if pack.role == "CFO")
    now = datetime.now(timezone.utc)

    blocked = cfo.evaluate({})
    assert blocked.verdict == "blocked"
    assert "missing" in blocked.reason.lower()
    assert blocked.handoff["gaps"]

    fresh_snapshots = {
        adapter.adapter_id: _fresh_snapshot(adapter.adapter_id, now=now)
        for adapter in cfo.adapters
    }
    passed = cfo.evaluate(fresh_snapshots, now=now)
    assert passed.verdict == "ready"
    assert passed.reason.endswith("Kanban handoff ready")
    assert passed.handoff["summary"].startswith("CFO")
    assert passed.handoff["gaps"] == []
    assert len(passed.evidence) == len(cfo.adapters)


def test_sensitive_role_packs_fail_closed_and_stay_pending_approval_when_fresh():
    packs = build_p1_sensitive_role_packs()
    chro = next(pack for pack in packs if pack.role == "CHRO")
    now = datetime.now(timezone.utc)

    expired_snapshots = {
        adapter.adapter_id: _fresh_snapshot(adapter.adapter_id, now=now, age_hours=72)
        for adapter in chro.adapters
    }
    expired = chro.evaluate(expired_snapshots, now=now)
    assert expired.verdict == "blocked"
    assert "expired" in expired.reason.lower()
    assert all(envelope.freshness == "expired" for envelope in expired.evidence)

    fresh_snapshots = {
        adapter.adapter_id: _fresh_snapshot(adapter.adapter_id, now=now)
        for adapter in chro.adapters
    }
    pending = chro.evaluate(fresh_snapshots, now=now)
    assert pending.verdict == "pending_approval"
    assert pending.reason.endswith("pending approval for high-risk output")
    assert pending.handoff["summary"].startswith("CHRO")
    assert "approval" in pending.handoff["summary"].lower()
    assert pending.handoff["gaps"] == []
    assert len(pending.evidence) == len(chro.adapters)
