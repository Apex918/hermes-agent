"""Offline Feishu event fixtures and isolation helpers.

The fixture loader is intentionally local-only.  It gives business tests one
stable, redaction-safe event corpus without importing the Feishu SDK or making
an outbound request.  ``FeishuFixtureScope`` and ``OfflineFeishuEventStore``
model the boundaries that an adapter must enforce before handing an event to a
business workflow.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


DEFAULT_FEISHU_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "feishu-events.v1.json"
EVENT_KINDS = ("im", "document_comment", "card", "task", "calendar", "base")
_REDACTED = "<redacted>"
_SENSITIVE_KEY_PARTS = (
    "access_token",
    "app_secret",
    "authorization",
    "encrypt_key",
    "password",
    "secret",
    "token",
)


class FeishuFixtureValidationError(ValueError):
    """Raised when an offline fixture is malformed or not local."""


def _read_local_json(source: str | Path | None) -> dict[str, Any]:
    path = DEFAULT_FEISHU_FIXTURE_PATH if source is None else source
    if isinstance(path, str) and "://" in path:
        raise FeishuFixtureValidationError("network fixture sources are not permitted")
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeishuFixtureValidationError(f"cannot read local Feishu fixture: {path}") from exc
    if not isinstance(value, dict):
        raise FeishuFixtureValidationError("Feishu fixture root must be an object")
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact_sensitive_fields(value: Any) -> Any:
    """Return a deep redacted copy without changing the source payload.

    Matching is deliberately key-based, so ordinary user text is preserved;
    values under token/secret/password/authorization keys are never exposed in
    reports or audit artifacts.
    """

    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED if _is_sensitive_key(str(key)) else redact_sensitive_fields(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_fields(item) for item in value)
    return deepcopy(value)


def _validate_relative_path(path: Any, workspace_path: str) -> bool:
    if not isinstance(path, str) or not path or "://" in path:
        return False
    candidate = PurePosixPath(path)
    root = PurePosixPath(workspace_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return False
    return candidate == root or root in candidate.parents


def _contains_network_source(value: Any) -> bool:
    if isinstance(value, str):
        return "://" in value
    if isinstance(value, Mapping):
        return any(_contains_network_source(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_network_source(item) for item in value)
    return False


def _validate_fixture(value: Mapping[str, Any]) -> None:
    required = {"schema_version", "manifest_version", "network_policy", "scope", "events"}
    missing = sorted(required - set(value))
    if missing:
        raise FeishuFixtureValidationError(f"fixture missing required keys: {', '.join(missing)}")
    if value["schema_version"] != "hermes.ai_business_os.feishu_events.v1":
        raise FeishuFixtureValidationError("unsupported Feishu fixture schema version")
    if value["manifest_version"] != 1:
        raise FeishuFixtureValidationError("unsupported Feishu fixture manifest version")
    policy = value["network_policy"]
    scope = value["scope"]
    events = value["events"]
    if not isinstance(policy, Mapping) or policy.get("network_allowed") is not False or policy.get("external_side_effects") is not False:
        raise FeishuFixtureValidationError("fixture must explicitly disable network and external side effects")
    if not isinstance(scope, Mapping):
        raise FeishuFixtureValidationError("fixture scope must be an object")
    for key in ("tenant_id", "profile", "workspace_id", "workspace_path"):
        if not isinstance(scope.get(key), str) or not scope[key]:
            raise FeishuFixtureValidationError(f"fixture scope missing {key}")
    if not isinstance(events, list):
        raise FeishuFixtureValidationError("fixture events must be a list")
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    kinds: set[str] = set()
    for event in events:
        if not isinstance(event, Mapping):
            raise FeishuFixtureValidationError("fixture event must be an object")
        for key in ("kind", "event_type", "event_id", "idempotency_key", "tenant_id", "profile", "workspace_id", "workspace_path", "artifact_path", "payload"):
            if key not in event:
                raise FeishuFixtureValidationError(f"fixture event missing {key}")
        event_id = str(event["event_id"])
        idempotency_key = str(event["idempotency_key"])
        if event["kind"] not in EVENT_KINDS:
            raise FeishuFixtureValidationError(f"unknown Feishu event kind: {event['kind']!r}")
        if not event_id or event_id in seen_ids:
            raise FeishuFixtureValidationError("event ids must be non-empty and unique")
        if not idempotency_key or idempotency_key in seen_keys:
            raise FeishuFixtureValidationError("idempotency keys must be non-empty and unique")
        if event["tenant_id"] != scope["tenant_id"] or event["profile"] != scope["profile"] or event["workspace_id"] != scope["workspace_id"]:
            raise FeishuFixtureValidationError("fixture event crosses its declared scope")
        if event["workspace_path"] != scope["workspace_path"] or not _validate_relative_path(event["artifact_path"], scope["workspace_path"]):
            raise FeishuFixtureValidationError("fixture event path escapes its workspace")
        if _contains_network_source(event["payload"]):
            raise FeishuFixtureValidationError("fixture payload contains a network source")
        seen_ids.add(event_id)
        seen_keys.add(idempotency_key)
        kinds.add(str(event["kind"]))
    missing_kinds = set(EVENT_KINDS) - kinds
    if missing_kinds:
        raise FeishuFixtureValidationError(f"fixture missing event kinds: {', '.join(sorted(missing_kinds))}")


def load_feishu_event_fixture(source: str | Path | None = None) -> dict[str, Any]:
    """Load and validate the checked-in, network-closed Feishu event corpus."""

    value = _read_local_json(source)
    _validate_fixture(value)
    return value


@dataclass(frozen=True)
class FeishuFixtureScope:
    """Tenant/profile/workspace scope used to authorize fixture events."""

    tenant_id: str
    profile: str
    workspace_id: str
    workspace_path: str

    @classmethod
    def from_fixture(cls, fixture: Mapping[str, Any]) -> "FeishuFixtureScope":
        scope = fixture.get("scope")
        if not isinstance(scope, Mapping):
            raise FeishuFixtureValidationError("fixture scope must be an object")
        return cls(
            tenant_id=str(scope["tenant_id"]),
            profile=str(scope["profile"]),
            workspace_id=str(scope["workspace_id"]),
            workspace_path=str(scope["workspace_path"]),
        )

    def authorize(self, event: Mapping[str, Any]) -> None:
        """Fail closed on tenant, profile, workspace, or path mismatch."""

        for key, expected in (
            ("tenant_id", self.tenant_id),
            ("profile", self.profile),
            ("workspace_id", self.workspace_id),
            ("workspace_path", self.workspace_path),
        ):
            if event.get(key) != expected:
                raise PermissionError(f"event {key} is outside fixture scope")
        if not _validate_relative_path(event.get("artifact_path"), self.workspace_path):
            raise PermissionError("event artifact path is outside fixture workspace")


class OfflineFeishuEventStore:
    """A deterministic, no-side-effect sink with idempotent event admission."""

    def __init__(self, scope: FeishuFixtureScope) -> None:
        self.scope = scope
        self._seen_keys: set[str] = set()
        self.accepted_events: list[dict[str, Any]] = []
        self.external_side_effects: list[dict[str, Any]] = []

    def ingest(self, event: Mapping[str, Any]) -> dict[str, Any]:
        self.scope.authorize(event)
        key = event.get("idempotency_key")
        if not isinstance(key, str) or not key:
            raise FeishuFixtureValidationError("event idempotency_key is required")
        if key in self._seen_keys:
            return {"status": "duplicate", "applied": False, "idempotency_key": key}
        self._seen_keys.add(key)
        self.accepted_events.append(redact_sensitive_fields(dict(event)))
        return {"status": "accepted", "applied": False, "idempotency_key": key}


__all__ = [
    "DEFAULT_FEISHU_FIXTURE_PATH",
    "EVENT_KINDS",
    "FeishuFixtureScope",
    "FeishuFixtureValidationError",
    "OfflineFeishuEventStore",
    "load_feishu_event_fixture",
    "redact_sensitive_fields",
]
