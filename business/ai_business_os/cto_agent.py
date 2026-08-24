from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .adapters import ReadOnlyAdapter
from .role_packs import RolePack, _pack


@dataclass(frozen=True)
class CTOAgent:
    """Deployable CTO read-only vertical slice backed by the CTO role pack."""

    role_pack: RolePack

    @property
    def manifest(self) -> dict[str, Any]:
        return self.role_pack.manifest

    @property
    def proposal(self) -> dict[str, Any]:
        return self.role_pack.proposal

    def evaluate(
        self,
        snapshots: Mapping[str, Mapping[str, Any] | None],
        *,
        now: datetime | None = None,
    ):
        return self.role_pack.evaluate(snapshots, now=now)


def build_cto_agent() -> CTOAgent:
    """Build a CTO agent with no external writes and R1 maximum effect."""
    role_pack = _pack(
        "CTO",
        [
            ("repo_status", "repository status", 24, ("observed_at",)),
            ("ci_pipeline", "CI pipeline", 24, ("observed_at",)),
            ("architecture_notes", "architecture notes", 24, ("observed_at",)),
        ],
    )
    manifest = dict(role_pack.manifest)
    manifest.update(
        {
            "max_effect_class": "R1",
            "allowed_external_writes": [],
            "agent_type": "business_role",
        }
    )
    proposal = dict(role_pack.proposal)
    proposal.update({"mode": "read_only", "external_writes": []})
    return CTOAgent(
        RolePack(
            role=role_pack.role,
            manifest=manifest,
            proposal=proposal,
            decision=role_pack.decision,
            adapters=tuple(
                ReadOnlyAdapter(
                    adapter_id=adapter.adapter_id,
                    source=adapter.source,
                    freshness_limit_hours=adapter.freshness_limit_hours,
                    required_fields=adapter.required_fields,
                    read_only=True,
                )
                for adapter in role_pack.adapters
            ),
        )
    )
