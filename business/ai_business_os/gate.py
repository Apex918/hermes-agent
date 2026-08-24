"""Shadow-gated release primitives for the fake AI Business OS connector."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, asdict
from typing import Any, Iterable, Iterator, Mapping
from uuid import uuid4


_MISSING = object()


class ConnectorNotAllowedError(RuntimeError):
    """Raised when a connector is not present on the allowlist."""


class ApprovalError(RuntimeError):
    """Raised when an approval receipt is missing or invalid."""


class OperationNotFoundError(RuntimeError):
    """Raised when an operation id does not exist in the gate ledger."""


@dataclass(slots=True)
class ApprovalReceipt:
    """Evidence that a reversible write may proceed."""

    operation_id: str
    connector_name: str
    approved_by: str
    receipt_id: str | None = None
    approved_at: str | None = None

    def is_for(self, operation_id: str, connector_name: str) -> bool:
        return self.operation_id == operation_id and self.connector_name == connector_name


@dataclass(slots=True)
class AuditEntry:
    """Single ledger event for shadow, commit, rollback, or replay."""

    operation_id: str
    connector_name: str
    key: str
    status: str
    mode: str
    applied: bool
    before: Any
    after: Any
    receipt: ApprovalReceipt | None = None
    source_operation_id: str | None = None
    shadow_operation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.before is _MISSING:
            payload["before"] = None
            payload["before_missing"] = True
        else:
            payload["before_missing"] = False
        if self.after is _MISSING:
            payload["after"] = None
            payload["after_missing"] = True
        else:
            payload["after_missing"] = False
        return payload


@dataclass(slots=True)
class ShadowOnlyResult:
    """Return object for shadow-only proposal and downstream actions."""

    operation_id: str
    connector_name: str
    key: str
    value: Any
    status: str
    mode: str
    applied: bool
    before: Any
    after: Any
    receipt: ApprovalReceipt | None = None
    source_operation_id: str | None = None
    shadow_operation_id: str | None = None

    def to_audit_entry(self) -> AuditEntry:
        return AuditEntry(
            operation_id=self.operation_id,
            connector_name=self.connector_name,
            key=self.key,
            status=self.status,
            mode=self.mode,
            applied=self.applied,
            before=self.before,
            after=self.after,
            receipt=self.receipt,
            source_operation_id=self.source_operation_id,
            shadow_operation_id=self.shadow_operation_id,
        )


class ConnectorAllowlist:
    """A tiny, testable allowlist for connector names."""

    def __init__(self, allowed: Iterable[str] | None = None) -> None:
        self._allowed = {str(name).strip() for name in (allowed or ()) if str(name).strip()}

    def __contains__(self, name: str) -> bool:
        return str(name).strip() in self._allowed

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._allowed))

    def add(self, name: str) -> None:
        name = str(name).strip()
        if name:
            self._allowed.add(name)

    def discard(self, name: str) -> None:
        self._allowed.discard(str(name).strip())

    def require(self, name: str) -> None:
        if name not in self:
            raise ConnectorNotAllowedError(f"connector {name!r} is not allowlisted")


class FakeConnector:
    """In-memory connector used for offline tests and replayable writes."""

    def __init__(self, name: str, initial_state: Mapping[str, Any] | None = None) -> None:
        self.name = str(name)
        self._state: dict[str, Any] = deepcopy(dict(initial_state or {}))
        self.calls: list[tuple[str, str, Any]] = []

    def read(self, key: str, default: Any = _MISSING) -> Any:
        if key in self._state:
            return deepcopy(self._state[key])
        if default is _MISSING:
            raise KeyError(key)
        return deepcopy(default)

    def write(self, key: str, value: Any) -> None:
        self.calls.append(("write", key, deepcopy(value)))
        self._state[str(key)] = deepcopy(value)

    def delete(self, key: str) -> None:
        self.calls.append(("delete", key, None))
        self._state.pop(str(key), None)

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self._state)


class ReleaseShadowGate:
    """Shadow-first reversible write gate with audit replay and rollback."""

    def __init__(
        self,
        connectors: Mapping[str, FakeConnector],
        allowlist: ConnectorAllowlist,
    ) -> None:
        self._connectors = {str(name): connector for name, connector in connectors.items()}
        self.allowlist = allowlist
        self.audit_log: list[AuditEntry] = []
        self._shadow_by_id: dict[str, ShadowOnlyResult] = {}
        self._committed_by_id: dict[str, AuditEntry] = {}

    def _get_connector(self, connector_name: str) -> FakeConnector:
        self.allowlist.require(connector_name)
        try:
            connector = self._connectors[str(connector_name)]
        except KeyError as exc:  # pragma: no cover - defensive
            raise OperationNotFoundError(f"unknown connector {connector_name!r}") from exc
        return connector

    @staticmethod
    def _new_operation_id() -> str:
        return f"op-{uuid4().hex}"

    def propose_reversible_write(self, connector_name: str, key: str, value: Any) -> ShadowOnlyResult:
        connector = self._get_connector(connector_name)
        op_id = self._new_operation_id()
        before = deepcopy(connector.read(key, default=_MISSING))
        proposal = ShadowOnlyResult(
            operation_id=op_id,
            connector_name=str(connector_name),
            key=str(key),
            value=deepcopy(value),
            status="shadowed",
            mode="shadow",
            applied=False,
            before=before,
            after=deepcopy(value),
            shadow_operation_id=op_id,
        )
        self._shadow_by_id[op_id] = proposal
        self.audit_log.append(proposal.to_audit_entry())
        return proposal

    def write_reversible(self, connector_name: str, key: str, value: Any) -> ShadowOnlyResult:
        return self.propose_reversible_write(connector_name, key, value)

    def commit_shadow_write(
        self,
        operation_id: str,
        *,
        receipt: ApprovalReceipt | None = None,
    ) -> ShadowOnlyResult:
        proposal = self._shadow_by_id.get(operation_id)
        if proposal is None:
            raise OperationNotFoundError(f"unknown shadow operation {operation_id!r}")
        if receipt is None:
            raise ApprovalError("approval receipt required before committing a reversible write")
        if not receipt.is_for(proposal.operation_id, proposal.connector_name):
            raise ApprovalError("approval receipt does not match the shadow operation")

        connector = self._get_connector(proposal.connector_name)
        connector.write(proposal.key, proposal.after)
        committed = ShadowOnlyResult(
            operation_id=proposal.operation_id,
            connector_name=proposal.connector_name,
            key=proposal.key,
            value=deepcopy(proposal.after),
            status="committed",
            mode="apply",
            applied=True,
            before=deepcopy(proposal.before),
            after=deepcopy(proposal.after),
            receipt=receipt,
            shadow_operation_id=proposal.operation_id,
        )
        committed_entry = committed.to_audit_entry()
        self._committed_by_id[operation_id] = committed_entry
        self.audit_log.append(committed_entry)
        return committed

    def rollback(self, operation_id: str) -> ShadowOnlyResult:
        committed = self._committed_by_id.get(operation_id)
        if committed is None:
            raise OperationNotFoundError(f"unknown committed operation {operation_id!r}")
        connector = self._get_connector(committed.connector_name)
        before_current = deepcopy(connector.read(committed.key, default=_MISSING))
        if committed.before is _MISSING:
            connector.delete(committed.key)
        else:
            connector.write(committed.key, committed.before)
        rollback = ShadowOnlyResult(
            operation_id=self._new_operation_id(),
            connector_name=committed.connector_name,
            key=committed.key,
            value=deepcopy(committed.before),
            status="rolled_back",
            mode="rollback",
            applied=True,
            before=before_current,
            after=deepcopy(committed.before),
            receipt=committed.receipt,
            source_operation_id=operation_id,
        )
        self.audit_log.append(rollback.to_audit_entry())
        return rollback

    def replay(self, operation_id: str) -> ShadowOnlyResult:
        committed = self._committed_by_id.get(operation_id)
        if committed is None:
            raise OperationNotFoundError(f"unknown committed operation {operation_id!r}")
        connector = self._get_connector(committed.connector_name)
        before_current = deepcopy(connector.read(committed.key, default=_MISSING))
        connector.write(committed.key, committed.after)
        replay = ShadowOnlyResult(
            operation_id=self._new_operation_id(),
            connector_name=committed.connector_name,
            key=committed.key,
            value=deepcopy(committed.after),
            status="replayed",
            mode="replay",
            applied=True,
            before=before_current,
            after=deepcopy(committed.after),
            receipt=committed.receipt,
            source_operation_id=operation_id,
        )
        self.audit_log.append(replay.to_audit_entry())
        return replay
