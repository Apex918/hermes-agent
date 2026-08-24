"""AI Business OS fixtures: offline, tenant-scoped, role-pack aware.

This package-local conftest keeps the new business tests hermetic without
changing the rest of the suite. It provides:
- a no-network guard that records raw socket attempts,
- a fixture catalog with three contract payloads per P0 role, and
- a tenant-scoped reader that fails closed across tenant boundaries.
"""

from __future__ import annotations

from copy import deepcopy
import socket
from typing import Any

import pytest

from business.ai_business_os import validator

P0_ROLES = (
    "ceo",
    "cfo",
    "coo",
    "cto",
    "ciso",
    "cmo",
    "cro",
    "gc",
)

_RIGHTS = {
    "scope": "enterprise governance",
    "asset_policy": "internal-only",
    "publish": False,
}
_FRESHNESS = {"reviewed_at": "2026-08-20", "max_age_days": 30}
_LICENSE = "MIT"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "expect_network_attempts: test asserts on blocked network attempts itself",
    )


@pytest.fixture

def network_attempts():
    """Record blocked connection targets for explicit assertions."""
    return []


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch, network_attempts):
    """Fail any business test that attempts a real socket connection."""

    def _blocked_connect(self, address, *args, **kwargs):
        network_attempts.append(address)
        raise RuntimeError(f"network disabled in business tests: {address!r}")

    def _blocked_create_connection(address, *args, **kwargs):
        network_attempts.append(address)
        raise RuntimeError(f"network disabled in business tests: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    monkeypatch.setattr(socket, "create_connection", _blocked_create_connection)
    yield
    if request.node.get_closest_marker("expect_network_attempts"):
        return
    assert not network_attempts, f"business test attempted network: {network_attempts!r}"


@pytest.fixture(scope="module")
def role_pack_triplets():
    """Three contract payloads per P0 role: manifest, proposal, decision."""

    packs: dict[str, dict[str, dict[str, Any]]] = {}
    for role in P0_ROLES:
        manifest_path = validator.ROLE_MANIFEST_DIR / f"{role}.v1.json"
        manifest = validator.load_manifest(manifest_path)
        rights = deepcopy(manifest["rights"])
        freshness = deepcopy(manifest["freshness"])
        title = manifest["title"]
        purpose = manifest.get("purpose", "")

        proposal = {
            "schema_version": "hermes.ai_business_os.action_request.v1",
            "manifest_version": 1,
            "request_id": f"{role}-proposal",
            "subject": f"{role}-read-only-pack",
            "action": f"generate-{role}-role-pack",
            "constraints": ["no external network", "tenant-isolated", "read-only"],
            "rights": deepcopy(rights),
            "freshness": deepcopy(freshness),
            "license": _LICENSE,
        }
        decision = {
            "schema_version": "hermes.ai_business_os.decision_record.v1",
            "manifest_version": 1,
            "decision_id": f"{role}-decision",
            "subject": f"{role}-role-pack",
            "decision": "approved",
            "rationale": f"{title}: {purpose}".strip(),
            "rights": deepcopy(rights),
            "freshness": deepcopy(freshness),
            "license": _LICENSE,
        }

        packs[role] = {
            "manifest": manifest,
            "proposal": proposal,
            "decision": decision,
        }
    return packs


@pytest.fixture(scope="module")
def tenant_role_pack_catalog(role_pack_triplets):
    """Tenant-isolated copies of the same P0 role-pack catalog."""

    return {
        "tenant-a": deepcopy(role_pack_triplets),
        "tenant-b": deepcopy(role_pack_triplets),
    }


@pytest.fixture()
def read_role_pack():
    """Fail closed when a requester tries to read another tenant's catalog."""

    def _read(
        catalog: dict[str, dict[str, dict[str, Any]]],
        *,
        owner_tenant: str,
        requester_tenant: str,
        role: str,
    ) -> dict[str, Any]:
        if owner_tenant != requester_tenant:
            raise PermissionError(
                f"{requester_tenant} cannot read {owner_tenant} role pack {role!r}"
            )
        return catalog[owner_tenant][role]

    return _read
