"""Offline contract and API tests for Hermes run-evidence read facade."""

import asyncio
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from jsonschema import Draft202012Validator

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run_evidence_facade import (
    ACL_SCOPE,
    FACADE_PATH_TEMPLATE,
    INTEGRATION_STATUS,
    NETWORK_ACCESS,
    HermesRunEvidenceStore,
    RunEvidenceFacade,
    RunEvidenceScope,
    RunEvidenceUnavailable,
)

ROOT = Path(__file__).resolve().parents[2]


def _record():
    return {
        "run_id": "run_fixture_1",
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "acl": {
            "principal_ref": "principal-a",
            "tenant_id": "tenant-a",
            "project_id": "project-a",
            "resource": ACL_SCOPE,
        },
        "observed_at": "2026-08-29T08:00:00+00:00",
        "source": "hermes-run-evidence-store",
        "source_version": "v1/0.20.6",
        "content_hash": "sha256:fixture-run-1",
        "evidence": {"events": [{"type": "run.completed", "status": "completed"}]},
        "provenance": {"kind": "fixture", "environment": "sandbox"},
    }


def _facade():
    store = HermesRunEvidenceStore({("tenant-a", "run_fixture_1"): _record()})
    scope = RunEvidenceScope("principal-a", "tenant-a", "project-a", ACL_SCOPE)
    return RunEvidenceFacade(store), scope, store


def test_fixture_facade_returns_source_owned_projection_with_invariants():
    facade, scope, store = _facade()

    result = facade.read("tenant-a", "run_fixture_1", scope)

    assert result["object"] == "hermes.readonly.run_evidence"
    assert result["path"] == "/api/readonly/tenant-a/runs/run_fixture_1/evidence"
    assert result["source"] == "hermes-run-evidence-store"
    assert result["source_version"] == "v1/0.20.6"
    assert result["tenant_id"] == "tenant-a"
    assert result["project_id"] == "project-a"
    assert result["principal_ref"] == "principal-a"
    assert result["acl_scope"] == ACL_SCOPE
    assert result["integration_status"] == INTEGRATION_STATUS
    assert result["network_access"] is False
    assert result["external_effects"] == 0
    assert store.reads == [("tenant-a", "run_fixture_1")]
    assert "credential_ref" not in result


def test_scope_is_explicit_and_every_mismatch_fails_closed():
    facade, scope, _ = _facade()

    with pytest.raises(RunEvidenceUnavailable, match="principal"):
        facade.read("tenant-a", "run_fixture_1", RunEvidenceScope("other", "tenant-a", "project-a", ACL_SCOPE))
    with pytest.raises(RunEvidenceUnavailable, match="tenant"):
        facade.read("tenant-b", "run_fixture_1", scope)
    with pytest.raises(RunEvidenceUnavailable, match="project"):
        facade.read("tenant-a", "run_fixture_1", RunEvidenceScope("principal-a", "tenant-a", "other", ACL_SCOPE))
    with pytest.raises(ValueError, match="ACL"):
        facade.read("tenant-a", "run_fixture_1", RunEvidenceScope("principal-a", "tenant-a", "project-a", "read:other"))


def test_store_is_read_only_and_opaque_credential_ref_is_never_resolved():
    facade, scope, store = _facade()

    result = facade.read("tenant-a", "run_fixture_1", scope, credential_ref="opaque:credential-ref-1")

    assert result["integration_status"] == "not_integrated"
    assert not hasattr(store, "write")
    assert all("credential" not in name.lower() for name in store.__dict__)
    with pytest.raises(AttributeError):
        store.write("tenant-a", "run_fixture_1", {})


def test_missing_or_malformed_store_data_fails_closed():
    store = HermesRunEvidenceStore({("tenant-a", "run_fixture_1"): {"run_id": "different"}})
    facade = RunEvidenceFacade(store)
    scope = RunEvidenceScope("principal-a", "tenant-a", "project-a", ACL_SCOPE)
    with pytest.raises(RunEvidenceUnavailable):
        facade.read("tenant-a", "run_fixture_1", scope)


def test_schema_accepts_facade_projection_and_matches_python_contract():
    facade, scope, _ = _facade()
    payload = facade.read("tenant-a", "run_fixture_1", scope)
    schema = json.loads((ROOT / "schemas" / "hermes-run-evidence.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(payload)

    invalid = copy.deepcopy(payload)
    invalid["integration_status"] = "integrated"
    with pytest.raises(Exception):
        Draft202012Validator(schema).validate(invalid)
    with pytest.raises(RunEvidenceUnavailable, match="integration_status"):
        facade.validate_projection(invalid)


def test_schema_rejects_mutation_and_network_metadata():
    schema = json.loads((ROOT / "schemas" / "hermes-run-evidence.schema.json").read_text())
    validator = Draft202012Validator(schema)
    facade, scope, _ = _facade()
    payload = facade.read("tenant-a", "run_fixture_1", scope)
    for key, value in (("method", "POST"), ("path", "https://example.invalid/evidence"), ("network_access", True)):
        invalid = dict(payload)
        invalid[key] = value
        with pytest.raises(Exception):
            validator.validate(invalid)


def test_api_route_is_exactly_get_only_and_uses_existing_target_auth():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-hermes-test"}))
    facade, _, _ = _facade()
    adapter._inject_run_evidence_facade(facade)
    app = web.Application()
    app.router.add_get(FACADE_PATH_TEMPLATE, adapter._handle_run_evidence)

    async def exercise():
        async with TestClient(TestServer(app)) as client:
            headers = {
                "Authorization": "Bearer sk-hermes-test",
                "X-Principal-Ref": "principal-a",
                "X-Tenant-Id": "tenant-a",
                "X-Project-Id": "project-a",
                "X-ACL-Scope": ACL_SCOPE,
            }
            response = await client.get("/api/readonly/tenant-a/runs/run_fixture_1/evidence", headers=headers)
            assert response.status == 200
            body = await response.json()
            assert body["run_id"] == "run_fixture_1"
            assert body["integration_status"] == "not_integrated"

            missing_auth = await client.get("/api/readonly/tenant-a/runs/run_fixture_1/evidence", headers={k: v for k, v in headers.items() if k != "Authorization"})
            assert missing_auth.status == 401

            query = await client.get("/api/readonly/tenant-a/runs/run_fixture_1/evidence?debug=true", headers=headers)
            assert query.status == 400

            body = await client.request("GET", "/api/readonly/tenant-a/runs/run_fixture_1/evidence", data="forbidden", headers=headers)
            assert body.status == 400

            mutation = await client.post("/api/readonly/tenant-a/runs/run_fixture_1/evidence", headers=headers)
            assert mutation.status == 405

    asyncio.run(exercise())


def test_route_table_contains_facade_and_no_mutation_route():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-hermes-test"}))
    rows = adapter._http_route_table()
    assert ("GET", FACADE_PATH_TEMPLATE) in [(method, path) for method, path, _ in rows]
    assert not any(path == FACADE_PATH_TEMPLATE and method != "GET" for method, path, _ in rows)


def test_fresh_process_import_has_no_network_or_credential_side_effects():
    code = "from gateway.run_evidence_facade import FACADE_PATH_TEMPLATE, NETWORK_ACCESS; assert NETWORK_ACCESS is False; print(FACADE_PATH_TEMPLATE)"
    completed = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), check=True, capture_output=True, text=True)
    assert FACADE_PATH_TEMPLATE in completed.stdout


def test_public_contract_constants_are_stable():
    assert FACADE_PATH_TEMPLATE == "/api/readonly/{tenant_id}/runs/{run_id}/evidence"
    assert NETWORK_ACCESS is False
    assert INTEGRATION_STATUS == "not_integrated"
    assert ACL_SCOPE == "read:run"
