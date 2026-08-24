"""Contract tests for the AI Business OS package.

The acceptance for N1 is intentionally narrow: versioned JSON Schema contracts,
12 enterprise role manifests, and fail-closed validation when rights,
freshness, or license metadata is missing.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from business.ai_business_os import validator

REPO = Path(__file__).resolve().parents[2]
CONTRACT_DIR = REPO / "business" / "ai_business_os" / "contracts"
ROLE_DIR = CONTRACT_DIR / "role-manifests"

CONTRACT_SPECS = [
    ("evidence-envelope.v1.schema.json", "hermes.ai_business_os.evidence_envelope.v1"),
    ("decision-record.v1.schema.json", "hermes.ai_business_os.decision_record.v1"),
    ("action-request.v1.schema.json", "hermes.ai_business_os.action_request.v1"),
    ("role-manifest.v1.schema.json", "hermes.ai_business_os.role_manifest.v1"),
]

EXPECTED_ROLES = [
    "ceo",
    "cfo",
    "coo",
    "cmo",
    "cro",
    "cpo",
    "cto",
    "ciso",
    "chro",
    "gc",
    "cco",
    "cdo",
]


@pytest.fixture(scope="module")
def schema_payloads():
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(CONTRACT_DIR.glob("*.schema.json"))
    }


@pytest.fixture(scope="module")
def role_manifests():
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(ROLE_DIR.glob("*.json"))
    }


@pytest.mark.parametrize("filename,schema_version", CONTRACT_SPECS)
def test_versioned_schema_files_exist(filename, schema_version, schema_payloads):
    assert filename in schema_payloads, f"missing schema file: {filename}"
    schema = schema_payloads[filename]
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["schema_version"]["const"] == schema_version
    assert schema["properties"]["manifest_version"]["const"] == 1


def test_role_manifest_catalog_has_exactly_12_entries(role_manifests):
    assert len(role_manifests) == 12, f"expected 12 role manifests, found {len(role_manifests)}"
    assert {manifest["role"] for manifest in role_manifests.values()} == set(EXPECTED_ROLES)


def test_catalog_validation_passes():
    assert validator.validate_catalog() == []


@pytest.mark.parametrize("role", EXPECTED_ROLES)
def test_each_role_manifest_validates(role, role_manifests):
    manifest = next(m for m in role_manifests.values() if m["role"] == role)
    errors = validator.validate_named_contract("role-manifest", manifest)
    assert errors == [], f"{role} manifest should validate, got: {errors}"


@pytest.mark.parametrize(
    "contract_name,payload_factory",
    [
        (
            "evidence-envelope",
            lambda: {
                "schema_version": "hermes.ai_business_os.evidence_envelope.v1",
                "manifest_version": 1,
                "evidence_id": "evidence-001",
                "subject": "quarterly-plan",
                "evidence": [
                    {"kind": "note", "summary": "approval memo", "uri": "file:///tmp/approval.md"}
                ],
                "rights": {
                    "scope": "enterprise-ops",
                    "asset_policy": "internal-only",
                    "publish": False,
                },
                "freshness": {"reviewed_at": "2026-08-20", "max_age_days": 30},
                "license": "MIT",
            },
        ),
        (
            "decision-record",
            lambda: {
                "schema_version": "hermes.ai_business_os.decision_record.v1",
                "manifest_version": 1,
                "decision_id": "decision-001",
                "subject": "budget-reallocation",
                "decision": "approved",
                "rationale": "fits the quarter plan",
                "rights": {
                    "scope": "enterprise-ops",
                    "asset_policy": "internal-only",
                    "publish": False,
                },
                "freshness": {"reviewed_at": "2026-08-20", "max_age_days": 30},
                "license": "MIT",
            },
        ),
        (
            "action-request",
            lambda: {
                "schema_version": "hermes.ai_business_os.action_request.v1",
                "manifest_version": 1,
                "request_id": "action-001",
                "subject": "launch-readiness",
                "action": "publish-release-notes",
                "constraints": ["no external network", "preserve audit trail"],
                "rights": {
                    "scope": "enterprise-ops",
                    "asset_policy": "internal-only",
                    "publish": False,
                },
                "freshness": {"reviewed_at": "2026-08-20", "max_age_days": 30},
                "license": "MIT",
            },
        ),
    ],
)
def test_named_contracts_validate(contract_name, payload_factory):
    errors = validator.validate_named_contract(contract_name, payload_factory())
    assert errors == []


@pytest.mark.parametrize("missing_field", ["rights", "freshness", "license"])
def test_missing_contract_metadata_fails_closed(missing_field, role_manifests):
    manifest = deepcopy(next(iter(role_manifests.values())))
    manifest.pop(missing_field)
    errors = validator.validate_named_contract("role-manifest", manifest)
    assert errors, f"missing {missing_field} should fail closed"
    assert any(missing_field in error for error in errors)
