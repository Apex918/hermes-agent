"""Hermes-owned, source-owned readonly run evidence facade.

The facade is deliberately a local projection boundary.  It does not open a
socket, read SessionDB, resolve credentials, call a delegate, or mutate
Hermes/Kanban state.  It supports deterministic fixture input for contract
tests and an explicitly configured local JSONL source-owned store for isolated
runtime validation; neither mode is a live integration.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote

FACADE_PATH_TEMPLATE = "/api/readonly/{tenant_id}/runs/{run_id}/evidence"
RESOURCE = "run_evidence"
ACL_SCOPE = "read:run"
METHOD = "GET"
API_VERSION = "v1/0.20.6"
INTEGRATION_STATUS = "not_integrated"
NETWORK_ACCESS = False
EXTERNAL_EFFECTS = 0

_REQUIRED_RECORD_FIELDS = frozenset(
    {
        "run_id",
        "tenant_id",
        "project_id",
        "acl",
        "observed_at",
        "source",
        "source_version",
        "content_hash",
        "evidence",
        "provenance",
    }
)
_SAFE_ID = re.compile(r"^[^/?#\\\x00-\x1f\x7f]{1,256}$")
_OPAQUE_REF = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")


class RunEvidenceUnavailable(RuntimeError):
    """Raised when a run evidence projection cannot be served safely."""


@dataclass(frozen=True)
class RunEvidenceScope:
    """The target-authenticated identity and exact resource scope."""

    principal_ref: str
    tenant_id: str
    project_id: str
    acl_scope: str = ACL_SCOPE
    resource: str = RESOURCE

    def __post_init__(self) -> None:
        for field in ("principal_ref", "tenant_id", "project_id", "acl_scope", "resource"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("%s is required" % field)
        if self.acl_scope != ACL_SCOPE:
            raise ValueError("ACL scope must be read:run")
        if self.resource != RESOURCE:
            raise ValueError("resource must be run_evidence")
        for field in ("principal_ref", "tenant_id", "project_id"):
            if _SAFE_ID.fullmatch(getattr(self, field)) is None:
                raise ValueError("%s contains unsafe characters" % field)


class HermesRunEvidenceStore:
    """An immutable-in-use mapping of explicit local projections.

    The constructor accepts an in-memory fixture for offline contract tests;
    ``from_jsonl`` marks a validated local source-owned readonly store.
    """

    def __init__(
        self,
        records: Mapping[tuple[str, str], Mapping[str, Any]],
        *,
        storage_mode: str = "fixture",
    ) -> None:
        if not isinstance(records, Mapping):
            raise TypeError("records must be a mapping")
        if storage_mode not in {"fixture", "source_owned_readonly"}:
            raise ValueError("unsupported storage mode")
        self._records = copy.deepcopy(dict(records))
        self.storage_mode = storage_mode
        self.reads: list[tuple[str, str]] = []

    @classmethod
    def from_jsonl(cls, path: Path) -> "HermesRunEvidenceStore":
        """Load immutable source-owned projections from a local JSONL file."""
        if not isinstance(path, Path):
            path = Path(path)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise RunEvidenceUnavailable("source-owned evidence store is unavailable") from exc

        records: dict[tuple[str, str], Mapping[str, Any]] = {}
        for _line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RunEvidenceUnavailable(
                    "source-owned evidence store contains malformed JSON"
                ) from exc
            if not isinstance(record, Mapping):
                raise RunEvidenceUnavailable("source-owned evidence record is malformed")
            tenant_id = record.get("tenant_id")
            run_id = record.get("run_id")
            if not isinstance(tenant_id, str) or not tenant_id.strip():
                raise RunEvidenceUnavailable("source-owned evidence tenant_id is invalid")
            if not isinstance(run_id, str) or not run_id.strip():
                raise RunEvidenceUnavailable("source-owned evidence run_id is invalid")
            key = (tenant_id, run_id)
            if key in records:
                raise RunEvidenceUnavailable(
                    "source-owned evidence store contains duplicate records"
                )
            records[key] = record

        if not records:
            raise RunEvidenceUnavailable("source-owned evidence store is empty")
        return cls(records, storage_mode="source_owned_readonly")

    def read(self, tenant_id: str, run_id: str) -> Mapping[str, Any]:
        key = (tenant_id, run_id)
        if key not in self._records:
            raise RunEvidenceUnavailable("run evidence is unavailable")
        self.reads.append(key)
        record = self._records[key]
        if not isinstance(record, Mapping):
            raise RunEvidenceUnavailable("run evidence projection is malformed")
        return copy.deepcopy(record)


def render_path(tenant_id: str, run_id: str) -> str:
    """Render the exact local route without allowing query or traversal data."""
    _require_safe_id(tenant_id, "tenant_id")
    _require_safe_id(run_id, "run_id")
    return "/api/readonly/%s/runs/%s/evidence" % (
        quote(tenant_id, safe=""),
        quote(run_id, safe=""),
    )


def _require_safe_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise RunEvidenceUnavailable("%s is invalid" % name)
    return value


def _parse_timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RunEvidenceUnavailable("%s is required" % name)
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RunEvidenceUnavailable("%s is invalid" % name) from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _validate_opaque_credential_ref(value: Any) -> None:
    """Validate only the shape of an opaque reference, never resolve it."""
    if value is None:
        return
    if not isinstance(value, str) or _OPAQUE_REF.fullmatch(value) is None:
        raise RunEvidenceUnavailable("credential_ref must be an opaque reference")


def _projection_errors(projection: Any) -> list[str]:
    if not isinstance(projection, Mapping):
        return ["projection must be a JSON object"]
    errors: list[str] = []
    missing = sorted(_REQUIRED_RECORD_FIELDS - set(projection))
    if missing:
        errors.append("projection is missing: " + ", ".join(missing))
    if projection.get("object") != "hermes.readonly.run_evidence":
        errors.append("object must be hermes.readonly.run_evidence")
    if projection.get("schema") != "hermes.run-evidence.v1":
        errors.append("schema must be hermes.run-evidence.v1")
    if projection.get("system") != "hermes":
        errors.append("system must be hermes")
    if projection.get("api_version") != API_VERSION:
        errors.append("api_version must be v1/0.20.6")
    if projection.get("resource") != RESOURCE:
        errors.append("resource must be run_evidence")
    if projection.get("method") != METHOD:
        errors.append("method must be GET")
    for field in ("run_id", "tenant_id", "project_id", "principal_ref"):
        value = projection.get(field)
        if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
            errors.append("%s is invalid" % field)
    if not isinstance(projection.get("path"), str) or not projection["path"].startswith("/api/readonly/"):
        errors.append("path must be the local readonly route")
    elif isinstance(projection.get("tenant_id"), str) and isinstance(projection.get("run_id"), str):
        try:
            if projection["path"] != render_path(projection["tenant_id"], projection["run_id"]):
                errors.append("path must match tenant_id and run_id exactly")
        except RunEvidenceUnavailable:
            errors.append("path identifiers are invalid")
    if projection.get("source") != "hermes-run-evidence-store":
        errors.append("source must be hermes-run-evidence-store")
    if not isinstance(projection.get("source_version"), str) or not projection["source_version"].strip():
        errors.append("source_version is required")
    if not isinstance(projection.get("content_hash"), str) or not projection["content_hash"].strip():
        errors.append("content_hash is required")
    _check_timestamp(projection.get("observed_at"), "observed_at", errors)
    if projection.get("expires_at") is not None:
        _check_timestamp(projection.get("expires_at"), "expires_at", errors)
    if not isinstance(projection.get("evidence"), (Mapping, list, str, int, float, bool, type(None))):
        errors.append("evidence must be JSON-compatible")
    provenance = projection.get("provenance")
    if not isinstance(provenance, Mapping):
        errors.append("provenance must be an object")
    elif not isinstance(provenance.get("kind"), str) or not provenance["kind"].strip():
        errors.append("provenance.kind is required")
    elif provenance.get("environment") not in {"fixture", "sandbox", "staging"}:
        errors.append("provenance environment must be fixture, sandbox, or staging")
    acl = projection.get("acl")
    if not isinstance(acl, Mapping):
        errors.append("acl must be an object")
    else:
        for key in ("principal_ref", "tenant_id", "project_id"):
            if not isinstance(acl.get(key), str) or not acl[key].strip():
                errors.append("acl.%s is required" % key)
        if acl.get("resource") != ACL_SCOPE:
            errors.append("acl.resource must be read:run")
        if acl.get("principal_ref") != projection.get("principal_ref"):
            errors.append("acl.principal_ref must match principal_ref")
        if acl.get("tenant_id") != projection.get("tenant_id"):
            errors.append("acl.tenant_id must match tenant_id")
        if acl.get("project_id") != projection.get("project_id"):
            errors.append("acl.project_id must match project_id")
    if projection.get("integration_status") != INTEGRATION_STATUS:
        errors.append("integration_status must remain not_integrated")
    if projection.get("network_access") is not False:
        errors.append("network_access must remain false")
    if projection.get("external_effects") != EXTERNAL_EFFECTS:
        errors.append("external_effects must remain zero")
    return errors


def _check_timestamp(value: Any, name: str, errors: list[str]) -> None:
    try:
        _parse_timestamp(value, name)
    except RunEvidenceUnavailable as exc:
        errors.append(str(exc))


class RunEvidenceFacade:
    """Validate scope and return one source-owned local evidence projection."""

    def __init__(self, store: HermesRunEvidenceStore, *, api_version: str = API_VERSION) -> None:
        if type(store) is not HermesRunEvidenceStore:
            raise TypeError("store must be HermesRunEvidenceStore")
        if not isinstance(api_version, str) or not api_version.strip():
            raise ValueError("api_version is required")
        self._store = store
        self.api_version = api_version

    def read(
        self,
        tenant_id: str,
        run_id: str,
        scope: RunEvidenceScope,
        *,
        credential_ref: Optional[str] = None,
    ) -> dict[str, Any]:
        """Read a fixture projection after all identity/scope checks.

        ``credential_ref`` is accepted only as an opaque, shape-checked value;
        it is never logged, returned, dereferenced, or passed to the store.
        """
        if not isinstance(scope, RunEvidenceScope):
            raise RunEvidenceUnavailable("scope is required")
        _require_safe_id(tenant_id, "tenant_id")
        _require_safe_id(run_id, "run_id")
        _validate_opaque_credential_ref(credential_ref)
        if scope.tenant_id != tenant_id:
            raise RunEvidenceUnavailable("tenant scope mismatch")

        record = self._store.read(tenant_id, run_id)
        if not isinstance(record, Mapping):
            raise RunEvidenceUnavailable("run evidence projection is malformed")
        if record.get("run_id") != run_id:
            raise RunEvidenceUnavailable("run scope mismatch")
        if record.get("tenant_id") != tenant_id:
            raise RunEvidenceUnavailable("tenant scope mismatch")
        if record.get("project_id") != scope.project_id:
            raise RunEvidenceUnavailable("project scope mismatch")
        acl = record.get("acl")
        if not isinstance(acl, Mapping):
            raise RunEvidenceUnavailable("ACL is missing")
        if acl.get("principal_ref") != scope.principal_ref:
            raise RunEvidenceUnavailable("principal scope mismatch")
        if acl.get("tenant_id") != tenant_id:
            raise RunEvidenceUnavailable("tenant scope mismatch")
        if acl.get("project_id") != scope.project_id:
            raise RunEvidenceUnavailable("project scope mismatch")
        if acl.get("resource") != ACL_SCOPE:
            raise RunEvidenceUnavailable("ACL scope mismatch")

        _parse_timestamp(record.get("observed_at"), "observed_at")
        expires_at = record.get("expires_at")
        if expires_at is not None and datetime.now(timezone.utc) >= _parse_timestamp(expires_at, "expires_at"):
            raise RunEvidenceUnavailable("run evidence is expired")
        projection = {
            "object": "hermes.readonly.run_evidence",
            "schema": "hermes.run-evidence.v1",
            "system": "hermes",
            "api_version": self.api_version,
            "resource": RESOURCE,
            "method": METHOD,
            "path": render_path(tenant_id, run_id),
            "run_id": run_id,
            "tenant_id": tenant_id,
            "project_id": scope.project_id,
            "principal_ref": scope.principal_ref,
            "acl_scope": ACL_SCOPE,
            "source": record.get("source"),
            "source_version": record.get("source_version"),
            "observed_at": record.get("observed_at"),
            "content_hash": record.get("content_hash"),
            "evidence": copy.deepcopy(record.get("evidence")),
            "provenance": copy.deepcopy(record.get("provenance")),
            "integration_status": INTEGRATION_STATUS,
            "network_access": NETWORK_ACCESS,
            "external_effects": EXTERNAL_EFFECTS,
            "acl": copy.deepcopy(dict(acl)),
        }
        if expires_at is not None:
            projection["expires_at"] = expires_at
        self.validate_projection(projection)
        return projection

    @staticmethod
    def validate_projection(projection: Mapping[str, Any]) -> None:
        errors = _projection_errors(projection)
        if errors:
            raise RunEvidenceUnavailable("; ".join(errors))


__all__ = [
    "ACL_SCOPE",
    "API_VERSION",
    "EXTERNAL_EFFECTS",
    "FACADE_PATH_TEMPLATE",
    "HermesRunEvidenceStore",
    "INTEGRATION_STATUS",
    "METHOD",
    "NETWORK_ACCESS",
    "RESOURCE",
    "RunEvidenceFacade",
    "RunEvidenceScope",
    "RunEvidenceUnavailable",
    "render_path",
]
