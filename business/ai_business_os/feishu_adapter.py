"""Explicit, network-free Feishu API adapter boundary.

This module deliberately does not import the Feishu SDK or the gateway/WebSocket
transport.  It gives callers a small provider interface for explicit Task,
Calendar, and Base reads and produces reviewable dry-run payloads.  The default
path never calls a provider write method; production writes are rejected even
when a caller explicitly asks for them.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Mapping, Protocol, Sequence

from .feishu_fixtures import redact_sensitive_fields


class FeishuAdapterError(ValueError):
    """Base error for malformed adapter input or unsupported operations."""


class ProductionWriteDisabled(FeishuAdapterError):
    """Raised because this vertical slice cannot perform production writes."""


class WebhookIngressDisabled(FeishuAdapterError):
    """Raised when the optional webhook ingress path was not enabled."""


@dataclass(frozen=True)
class FeishuAdapterScope:
    """Stable provenance attached to every explicit API result."""

    tenant_id: str = "local"
    profile: str = "feishu-explicit"
    workspace_id: str = "local-workspace"


class FeishuProvider(Protocol):
    """Read-only provider contract implemented by the fake and real bridges."""

    def read_tasks(self) -> Sequence[Mapping[str, Any]]: ...

    def read_calendar(self) -> Sequence[Mapping[str, Any]]: ...

    def read_base(self) -> Sequence[Mapping[str, Any]]: ...


class FakeFeishuProvider:
    """Deterministic provider for contract tests and local CLI previews.

    The provider intentionally has no write methods.  ``calls`` makes it easy
    for tests to prove the adapter used explicit reads rather than a WebSocket
    event consumer.
    """

    def __init__(
        self,
        *,
        tasks: Sequence[Mapping[str, Any]] = (),
        calendar: Sequence[Mapping[str, Any]] = (),
        base: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self._resources = {
            "tasks": [dict(item) for item in tasks],
            "calendar": [dict(item) for item in calendar],
            "base": [dict(item) for item in base],
        }
        self.calls: list[str] = []

    def _read(self, kind: str) -> list[dict[str, Any]]:
        self.calls.append(f"read_{kind}")
        return deepcopy(self._resources[kind])

    def read_tasks(self) -> list[dict[str, Any]]:
        return self._read("tasks")

    def read_calendar(self) -> list[dict[str, Any]]:
        return self._read("calendar")

    def read_base(self) -> list[dict[str, Any]]:
        return self._read("base")

    # Names used by common explicit REST wrappers.  They still remain reads.
    list_tasks = read_tasks
    list_calendar_events = read_calendar
    list_base_records = read_base

    @classmethod
    def from_fixture(cls, fixture: Mapping[str, Any]) -> "FakeFeishuProvider":
        resources: dict[str, list[Mapping[str, Any]]] = {"tasks": [], "calendar": [], "base": []}
        for event in fixture.get("events", []):
            if not isinstance(event, Mapping):
                continue
            kind = str(event.get("kind") or "")
            resource = {"task": "tasks", "calendar": "calendar", "base": "base"}.get(kind)
            payload = event.get("payload")
            if resource and isinstance(payload, Mapping):
                resources[resource].append(dict(payload))
        # A hand-authored local provider fixture may use direct resource keys.
        for kind in resources:
            direct = fixture.get(kind)
            if direct is None:
                direct = fixture.get({"tasks": "task", "calendar": "events", "base": "records"}[kind])
            if isinstance(direct, list):
                resources[kind] = [dict(item) for item in direct if isinstance(item, Mapping)]
        return cls(**resources)


@dataclass(frozen=True)
class DryRunPayload:
    """A serialisable proposal; it is not an instruction to perform a write."""

    kind: str
    operation: str
    payload: Mapping[str, Any]
    idempotency_key: str
    audit_ref: str
    mode: str = "dry_run"
    production_write: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "hermes.ai_business_os.feishu_explicit.v1",
            "adapter": "feishu-explicit-api",
            "kind": self.kind,
            "operation": self.operation,
            "mode": self.mode,
            "production_write": self.production_write,
            "idempotency_key": self.idempotency_key,
            "audit_ref": self.audit_ref,
            "payload": deepcopy(dict(self.payload)),
        }


class FeishuExplicitAdapter:
    """Read Feishu resources through an explicit provider and plan only writes."""

    def __init__(
        self,
        provider: FeishuProvider,
        *,
        scope: FeishuAdapterScope | None = None,
        webhook_enabled: bool = False,
    ) -> None:
        self.provider = provider
        self.scope = scope or FeishuAdapterScope()
        self.webhook_enabled = webhook_enabled

    def read_tasks(self) -> list[dict[str, Any]]:
        return self._read("tasks", self.provider.read_tasks)

    def read_calendar(self) -> list[dict[str, Any]]:
        return self._read("calendar", self.provider.read_calendar)

    def read_base(self) -> list[dict[str, Any]]:
        return self._read("base", self.provider.read_base)

    # Explicit API naming aliases.
    list_tasks = read_tasks
    list_calendar_events = read_calendar
    list_base_records = read_base

    def preview_task(self, task: Mapping[str, Any], *, operation: str = "create") -> dict[str, Any]:
        return self._preview("task", task, operation).to_dict()

    def preview_calendar(self, event: Mapping[str, Any], *, operation: str = "create") -> dict[str, Any]:
        return self._preview("calendar", event, operation).to_dict()

    def preview_base(self, record: Mapping[str, Any], *, operation: str = "create") -> dict[str, Any]:
        return self._preview("base", record, operation).to_dict()

    # Verbose names make the write-safe intent clear to API callers.
    build_task_payload = preview_task
    build_calendar_payload = preview_calendar
    build_base_payload = preview_base

    def generate_dry_run_payload(
        self, kind: str, resource: Mapping[str, Any], *, operation: str = "create"
    ) -> dict[str, Any]:
        """Build one dry-run payload using a resource kind name."""

        normalized = {"tasks": "task", "calendar": "calendar", "base": "base"}.get(kind, kind)
        return self._preview(normalized, resource, operation).to_dict()

    def preview_all(self) -> dict[str, list[dict[str, Any]]]:
        """Read all three resource types and return dry-run proposals."""

        return {
            "tasks": [self.preview_task(item) for item in self.read_tasks()],
            "calendar": [self.preview_calendar(item) for item in self.read_calendar()],
            "base": [self.preview_base(item) for item in self.read_base()],
        }

    def archive_ima_report(
        self,
        report: Mapping[str, Any] | str,
        *,
        report_id: str = "ima-report",
        destination: str = "ima://archive",
    ) -> dict[str, Any]:
        """Return an IMA archive proposal without calling an archive service."""

        if not isinstance(report, (str, Mapping)):
            raise FeishuAdapterError("IMA report must be text or an object")
        resource = {"report_id": report_id, "destination": destination, "report": report}
        return self._preview("ima_report", resource, "archive").to_dict()

    # A shorter spelling is convenient for CLI integrations.
    archive_report = archive_ima_report

    def ingest_webhook_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Optionally turn an already-received event into a dry-run proposal.

        This is an ingress seam only.  It does not open a listener and never
        writes back to Feishu.  Explicit reads remain the primary path.
        """

        if not self.webhook_enabled:
            raise WebhookIngressDisabled("webhook ingress is optional and disabled")
        if not isinstance(event, Mapping):
            raise FeishuAdapterError("webhook event must be an object")
        kind = str(event.get("kind") or event.get("resource") or "").lower()
        if kind not in {"task", "calendar", "base"}:
            raise FeishuAdapterError("webhook event kind must be task, calendar, or base")
        payload = event.get("payload", event)
        if not isinstance(payload, Mapping):
            raise FeishuAdapterError("webhook payload must be an object")
        return self._preview(kind, payload, "ingest").to_dict()

    def create_task(self, task: Mapping[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        return self._write_guard("task", task, "create", dry_run=dry_run)

    def create_calendar_event(self, event: Mapping[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        return self._write_guard("calendar", event, "create", dry_run=dry_run)

    def create_base_record(self, record: Mapping[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        return self._write_guard("base", record, "create", dry_run=dry_run)

    def _read(self, kind: str, reader: Any) -> list[dict[str, Any]]:
        try:
            value = reader()
        except Exception as exc:
            raise FeishuAdapterError(f"explicit {kind} read failed") from exc
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise FeishuAdapterError(f"explicit {kind} read must return a list")
        return [dict(item) for item in value if isinstance(item, Mapping)]

    def _preview(self, kind: str, resource: Mapping[str, Any], operation: str) -> DryRunPayload:
        if kind not in {"task", "calendar", "base", "ima_report"}:
            raise FeishuAdapterError(f"unsupported resource kind: {kind}")
        if not isinstance(resource, Mapping):
            raise FeishuAdapterError(f"{kind} payload must be an object")
        safe_resource = redact_sensitive_fields(dict(resource))
        identity = _identity(kind, safe_resource)
        key = f"feishu-explicit:{self.scope.tenant_id}:{kind}:{operation}:{identity}"
        audit_ref = "audit:" + sha256(key.encode("utf-8")).hexdigest()[:20]
        payload = {
            "scope": {
                "tenant_id": self.scope.tenant_id,
                "profile": self.scope.profile,
                "workspace_id": self.scope.workspace_id,
            },
            "resource": safe_resource,
        }
        return DryRunPayload(kind, operation, payload, key, audit_ref)

    def _write_guard(self, kind: str, resource: Mapping[str, Any], operation: str, *, dry_run: bool) -> dict[str, Any]:
        proposal = self._preview(kind, resource, operation).to_dict()
        if not dry_run:
            raise ProductionWriteDisabled(
                "production Feishu writes are disabled; use the returned preview payload"
            )
        return proposal


def _identity(kind: str, payload: Mapping[str, Any]) -> str:
    candidates = {
        "task": ("task_id", "id"),
        "calendar": ("event_id", "id"),
        "base": ("record_id", "id"),
        "ima_report": ("report_id", "id"),
    }[kind]
    for key in candidates:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()[:20]


# Names suitable for small integrations that do not want the longer class name.
FeishuAdapter = FeishuExplicitAdapter
FakeProvider = FakeFeishuProvider

__all__ = [
    "DryRunPayload",
    "FakeFeishuProvider",
    "FakeProvider",
    "FeishuAdapter",
    "FeishuAdapterError",
    "FeishuAdapterScope",
    "FeishuExplicitAdapter",
    "FeishuProvider",
    "ProductionWriteDisabled",
    "WebhookIngressDisabled",
]
