from __future__ import annotations

import socket

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


def test_p0_role_pack_catalog_has_three_fixtures_per_role(role_pack_triplets):
    assert set(role_pack_triplets) == set(P0_ROLES)

    for role, pack in role_pack_triplets.items():
        assert set(pack) == {"manifest", "proposal", "decision"}

        manifest = pack["manifest"]
        proposal = pack["proposal"]
        decision = pack["decision"]

        assert manifest["role"] == role
        assert validator.validate_named_contract("role-manifest", manifest) == []
        assert validator.validate_named_contract("action-request", proposal) == []
        assert validator.validate_named_contract("decision-record", decision) == []


def test_tenant_a_cannot_read_tenant_b(tenant_role_pack_catalog, read_role_pack):
    with pytest.raises(PermissionError, match="tenant-a.*tenant-b"):
        read_role_pack(
            tenant_role_pack_catalog,
            owner_tenant="tenant-b",
            requester_tenant="tenant-a",
            role="ceo",
        )


@pytest.mark.expect_network_attempts
def test_network_off_contract_blocks_raw_socket(network_attempts):
    with pytest.raises(RuntimeError, match="network disabled"):
        socket.create_connection(("127.0.0.1", 9), timeout=0.1)

    assert network_attempts == [("127.0.0.1", 9)]
