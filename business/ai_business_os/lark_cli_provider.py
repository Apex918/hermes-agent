"""Explicit production Feishu access through the installed ``lark-cli``.

The provider intentionally delegates authentication to lark-cli's existing
profile/auth configuration.  Hermes never reads or prints credentials.  Reads
require the CLI's JSON ``ok`` envelope; writes are receipt-gated, apply-once,
and verified by an identifier-specific readback before they are reported as
successful.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence

from hermes_constants import get_hermes_home

from .feishu_fixtures import redact_sensitive_fields


class LarkCLIError(RuntimeError):
    """Base error for a failed or unavailable lark-cli operation."""


class LarkCLIProtocolError(LarkCLIError):
    """Raised when lark-cli does not return the required ok envelope."""


class ApprovalReceiptInvalid(LarkCLIError):
    """Raised when a write lacks a valid R2/R3 approval receipt."""


class FeishuReadbackMismatch(LarkCLIError):
    """Raised when a write response cannot be verified by exact identifier."""


@dataclass(frozen=True)
class LarkCLIResponse:
    """Safe response metadata returned by the subprocess boundary."""

    data: Any
    returncode: int = 0


def parse_ok_envelope(stdout: str | bytes) -> Any:
    """Parse only a successful lark-cli ``{"ok": true, "data": ...}`` envelope.

    Error payloads and raw JSON are deliberately rejected.  Error details are
    not copied into the exception because CLI error streams can contain tokens
    or provider-specific sensitive fields.
    """

    if isinstance(stdout, bytes):
        try:
            stdout = stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LarkCLIProtocolError("lark-cli returned non-UTF-8 output") from exc
    try:
        payload = json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LarkCLIProtocolError("lark-cli returned invalid JSON") from exc
    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        raise LarkCLIProtocolError("lark-cli returned a non-ok envelope")
    if "data" not in payload:
        raise LarkCLIProtocolError("lark-cli ok envelope is missing data")
    return payload["data"]


class PersistentWriteLedger:
    """Small SQLite ledger for write requests and their verified readbacks."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS feishu_writes (
                    idempotency_key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    target TEXT NOT NULL,
                    audit_ref TEXT NOT NULL,
                    receipt TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_hash TEXT NOT NULL,
                    readback_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    returned_id TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def get(self, idempotency_key: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT idempotency_key, kind, target, audit_ref, receipt, request_hash, "
                "response_hash, readback_json, status, returned_id, error, created_at "
                "FROM feishu_writes WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        keys = (
            "idempotency_key", "kind", "target", "audit_ref", "receipt",
            "request_hash", "response_hash", "readback_json", "status",
            "returned_id", "error", "created_at",
        )
        result: dict[str, Any] = dict(zip(keys, row))
        result["readback"] = json.loads(result.pop("readback_json"))
        return result

    def reserve(
        self,
        *,
        idempotency_key: str,
        kind: str,
        target: str,
        audit_ref: str,
        receipt: str,
        request_hash: str,
    ) -> bool:
        """Reserve a key before the remote call to close the race window."""
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO feishu_writes
                (idempotency_key, kind, target, audit_ref, receipt, request_hash,
                 response_hash, readback_json, status, returned_id, error)
                VALUES (?, ?, ?, ?, ?, ?, '', '{}', 'pending', '', '')
                """,
                (idempotency_key, kind, target, audit_ref, receipt, request_hash),
            )
        return cursor.rowcount == 1

    def record(
        self,
        *,
        idempotency_key: str,
        kind: str,
        target: str,
        audit_ref: str,
        receipt: str,
        request_hash: str,
        response_hash: str,
        readback: Mapping[str, Any] | None,
        status: str,
        returned_id: str = "",
        error: str = "",
    ) -> None:
        safe_readback = redact_sensitive_fields(dict(readback or {}))
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO feishu_writes
                (idempotency_key, kind, target, audit_ref, receipt, request_hash,
                 response_hash, readback_json, status, returned_id, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    kind = excluded.kind,
                    target = excluded.target,
                    audit_ref = excluded.audit_ref,
                    receipt = excluded.receipt,
                    request_hash = excluded.request_hash,
                    response_hash = excluded.response_hash,
                    readback_json = excluded.readback_json,
                    status = excluded.status,
                    returned_id = excluded.returned_id,
                    error = excluded.error
                """,
                (
                    idempotency_key, kind, target, audit_ref, receipt, request_hash,
                    response_hash, json.dumps(safe_readback, ensure_ascii=False, sort_keys=True),
                    status, returned_id, error,
                ),
            )


def _default_ledger_path() -> Path:
    return get_hermes_home() / "feishu" / "write-ledger.sqlite3"


class LarkCLIProvider:
    """Real Task/Calendar/Base provider backed by lark-cli subprocess calls."""

    def __init__(
        self,
        *,
        executable: str = "lark-cli",
        profile: str | None = None,
        actor: str = "user",
        timeout: float = 30.0,
        base_token: str | None = None,
        table_id: str | None = None,
        ledger_path: str | Path | None = None,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        # ``uv run`` installs this repository's compatibility console script
        # under ``.venv/bin/lark-cli``.  Production must not recurse into that
        # fixture-only command when a real lark-cli is also on PATH.
        self.executable = _resolve_external_lark_cli(executable) if runner is None else executable
        self.profile = profile
        if actor not in {"user", "bot"}:
            raise ValueError("lark-cli actor must be user or bot")
        self.actor = actor
        self.timeout = timeout
        self.base_token = base_token
        self.table_id = table_id
        self._runner = runner or subprocess.run
        self.ledger = PersistentWriteLedger(ledger_path or _default_ledger_path())

    @property
    def name(self) -> str:
        return "feishu-lark-cli"

    def read_tasks(self) -> list[dict[str, Any]]:
        data = self._invoke("task", "+get-my-tasks", "--as", self.actor, "--json", "--page-all")
        return _items(data, "tasks")

    def read_calendar(self) -> list[dict[str, Any]]:
        data = self._invoke("calendar", "+agenda", "--as", self.actor, "--json")
        return _items(data, "events")

    def read_base(self) -> list[dict[str, Any]]:
        if not self.base_token or not self.table_id:
            raise ValueError("Base reads require base_token and table_id")
        data = self._invoke(
            "base", "+record-list", "--as", self.actor, "--base-token", self.base_token,
            "--table-id", self.table_id, "--json",
        )
        return _items(data, "records")

    def write(
        self,
        *,
        kind: str,
        resource: Mapping[str, Any],
        target: str,
        preview: str,
        diff: str,
        rollback: Mapping[str, Any],
        idempotency_key: str,
        approval_receipt: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Apply one approved write, or return its explicit dry-run envelope.

        Every write carries its review metadata at this boundary.  The ledger is
        checked before the subprocess call, so retries never issue another remote
        write, including after a timeout or readback mismatch.
        """

        normalized_kind = _normalize_kind(kind)
        _validate_write_metadata(
            normalized_kind, resource, target, preview, diff, rollback,
            idempotency_key, approval_receipt,
        )
        safe_resource = redact_sensitive_fields(dict(resource))
        request_hash = _hash_json(
            {
                "kind": normalized_kind,
                "target": target,
                "resource": safe_resource,
                "preview": preview,
                "diff": diff,
                "rollback": redact_sensitive_fields(dict(rollback)),
            }
        )
        audit_ref = "audit:" + request_hash[:20]
        prior = self.ledger.get(idempotency_key)
        if prior is not None:
            if prior["request_hash"] != request_hash:
                raise LarkCLIError("idempotency key is already bound to a different request")
            if prior["status"] == "pending":
                raise LarkCLIError("idempotency key is reserved for reconciliation")
            return _duplicate_result(prior, normalized_kind)
        if dry_run:
            return {
                "success": False,
                "status": "dry_run",
                "production_write": False,
                "kind": normalized_kind,
                "target": target,
                "preview": preview,
                "diff": diff,
                "rollback": deepcopy(dict(rollback)),
                "audit_ref": audit_ref,
                "idempotency_key": idempotency_key,
            }
        if not self.ledger.reserve(
            idempotency_key=idempotency_key,
            kind=normalized_kind,
            target=target,
            audit_ref=audit_ref,
            receipt=approval_receipt,
            request_hash=request_hash,
        ):
            raise LarkCLIError("idempotency key is reserved for reconciliation")

        response: Mapping[str, Any] = {}
        readback: Mapping[str, Any] = {}
        response_hash = ""
        try:
            response_data = self._invoke_write(normalized_kind, safe_resource, idempotency_key)
            response = _as_mapping(response_data)
            response_hash = _hash_json(redact_sensitive_fields(response))
            returned_id = _identifier(normalized_kind, response)
            if not returned_id:
                raise LarkCLIProtocolError("lark-cli write response has no resource identifier")
            readback = _as_mapping(self._invoke_readback(normalized_kind, returned_id, resource))
            verified_id = _identifier(normalized_kind, readback)
            if verified_id != returned_id:
                raise FeishuReadbackMismatch("Feishu write readback identifier mismatch")
            self.ledger.record(
                idempotency_key=idempotency_key, kind=normalized_kind, target=target,
                audit_ref=audit_ref, receipt=approval_receipt, request_hash=request_hash,
                response_hash=response_hash, readback=readback, status="applied",
                returned_id=returned_id,
            )
            result = {
                "success": True,
                "status": "applied",
                "idempotent": False,
                "production_write": True,
                "kind": normalized_kind,
                "target": target,
                "audit_ref": audit_ref,
                "idempotency_key": idempotency_key,
                "response": deepcopy(dict(redact_sensitive_fields(response))),
                "readback": deepcopy(dict(redact_sensitive_fields(readback))),
            }
            result[_identifier_key(normalized_kind)] = returned_id
            return result
        except Exception as exc:
            # A failure is terminal for this idempotency key.  Error text is
            # intentionally generic so CLI stderr can never become a log leak.
            self.ledger.record(
                idempotency_key=idempotency_key, kind=normalized_kind, target=target,
                audit_ref=audit_ref, receipt=approval_receipt, request_hash=request_hash,
                response_hash=response_hash, readback=readback, status="failed",
                error=_safe_error(exc),
            )
            raise

    def create_task(self, task: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.write(kind="task", resource=task, **kwargs)

    def create_calendar_event(self, event: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.write(kind="calendar", resource=event, **kwargs)

    def create_base_record(self, record: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.write(kind="base", resource=record, **kwargs)

    def _invoke(self, *args: str) -> Any:
        argv = [self.executable]
        if self.profile:
            argv.extend(["--profile", self.profile])
        argv.extend(args)
        try:
            completed = self._runner(
                argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self.timeout, check=False, stdin=subprocess.DEVNULL,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired as exc:
            raise LarkCLIError("lark-cli timed out") from exc
        except OSError as exc:
            raise LarkCLIError("lark-cli could not be started") from exc
        except Exception as exc:
            raise LarkCLIError("lark-cli invocation failed") from exc
        if getattr(completed, "returncode", 1) != 0:
            raise LarkCLIError("lark-cli returned a process error")
        return parse_ok_envelope(getattr(completed, "stdout", ""))

    def _invoke_write(self, kind: str, resource: Mapping[str, Any], idempotency_key: str) -> Any:
        if kind == "task":
            args = ["task", "+create", "--as", self.actor, "--json", "--data", _json_arg(resource), "--idempotency-key", idempotency_key]
        elif kind == "calendar":
            calendar_id = str(resource.get("calendar_id") or "primary")
            data = {key: value for key, value in resource.items() if key != "calendar_id"}
            args = [
                "calendar", "events", "create", "--as", self.actor, "--json",
                "--calendar-id", calendar_id,
                "--idempotency-key", idempotency_key,
                "--data", _json_arg(data),
            ]
        else:
            raise LarkCLIError("Base writes unavailable: installed lark-cli has no base service")
        return self._invoke(*args)

    def _invoke_readback(self, kind: str, returned_id: str, resource: Mapping[str, Any]) -> Any:
        if kind == "task":
            return self._invoke("task", "tasks", "get", "--as", self.actor, "--task-guid", returned_id, "--json")
        if kind == "calendar":
            return self._invoke(
                "calendar", "+get", "--as", self.actor, "--calendar-id",
                str(resource.get("calendar_id") or "primary"), "--event-id", returned_id, "--json",
            )
        if not self.base_token or not self.table_id:
            raise ValueError("Base readback requires base_token and table_id")
        return self._invoke(
            "base", "+record-get", "--as", self.actor, "--base-token", self.base_token,
            "--table-id", self.table_id, "--record-id", returned_id, "--json",
        )


# Explicit aliases make the production boundary discoverable to integrations
# that prefer a provider-oriented name over the transport-oriented one.
RealFeishuProvider = LarkCLIProvider
SubprocessLarkCLIProvider = LarkCLIProvider


def _normalize_kind(kind: str) -> str:
    normalized = str(kind or "").strip().lower()
    return {"tasks": "task", "events": "calendar", "record": "base", "records": "base"}.get(normalized, normalized)


def _resolve_external_lark_cli(executable: str) -> str:
    if executable != "lark-cli":
        return executable
    candidates: list[str] = []
    path_value = os.environ.get("PATH", "")
    for directory in path_value.split(os.pathsep):
        if not directory:
            continue
        candidate = shutil.which(executable, path=directory)
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        return executable
    prefix_bin = Path(sys.prefix).resolve() / "bin"
    for candidate in candidates:
        try:
            if Path(candidate).resolve().parent != prefix_bin:
                return candidate
        except OSError:
            return candidate
    return candidates[0]


def _validate_write_metadata(
    kind: str,
    resource: Mapping[str, Any],
    target: str,
    preview: str,
    diff: str,
    rollback: Mapping[str, Any],
    idempotency_key: str,
    approval_receipt: str,
) -> None:
    if kind not in {"task", "calendar", "base"}:
        raise ValueError("unsupported Feishu write kind")
    if not isinstance(resource, Mapping) or not resource:
        raise ValueError("write resource must be a non-empty object")
    for name, value in (("target", target), ("preview", preview), ("diff", diff), ("idempotency_key", idempotency_key)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"write {name} is required")
    if not isinstance(rollback, Mapping) or not rollback:
        raise ValueError("write rollback is required")
    if not isinstance(approval_receipt, str) or not re.search(
        r"\bR[23]\b\s*[:/_-]\s*\S+", approval_receipt, flags=re.IGNORECASE
    ):
        raise ApprovalReceiptInvalid("R2/R3 approval receipt is required")


def _items(data: Any, label: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        values = data
    elif isinstance(data, Mapping):
        values = None
        for key in ("items", label, "data", "records", "events", "tasks"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                values = candidate
                break
        if values is None:
            raise LarkCLIProtocolError("lark-cli response data is not a resource list")
    else:
        raise LarkCLIProtocolError("lark-cli response data is not an object or list")
    if not all(isinstance(item, Mapping) for item in values):
        raise LarkCLIProtocolError("lark-cli resource list contains invalid entries")
    return [deepcopy(dict(redact_sensitive_fields(item))) for item in values]


def _as_mapping(data: Any) -> Mapping[str, Any]:
    if isinstance(data, Mapping):
        return data
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], Mapping):
        return data[0]
    raise LarkCLIProtocolError("lark-cli response data is not an object")


def _identifier(kind: str, data: Mapping[str, Any]) -> str:
    keys = {
        "task": ("guid", "task_guid", "task_id", "id"),
        "calendar": ("event_id", "guid", "id"),
        "base": ("record_id", "id"),
    }[kind]
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    for nested_key in ("task", "event", "record", "item", "data"):
        nested = data.get(nested_key)
        if isinstance(nested, Mapping):
            found = _identifier(kind, nested)
            if found:
                return found
    return ""


def _identifier_key(kind: str) -> str:
    return {"task": "guid", "calendar": "event_id", "base": "record_id"}[kind]


def _duplicate_result(prior: Mapping[str, Any], kind: str) -> dict[str, Any]:
    result = {
        "success": prior.get("status") == "applied",
        "status": "duplicate",
        "idempotent": True,
        "production_write": prior.get("status") == "applied",
        "kind": kind,
        "target": prior.get("target", ""),
        "audit_ref": prior.get("audit_ref", ""),
        "idempotency_key": prior.get("idempotency_key", ""),
        "readback": deepcopy(dict(prior.get("readback") or {})),
        "error": prior.get("error", ""),
    }
    returned_id = prior.get("returned_id", "")
    if returned_id:
        result[_identifier_key(kind)] = returned_id
    return result


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, FeishuReadbackMismatch):
        return "Feishu write readback mismatch"
    if isinstance(exc, ApprovalReceiptInvalid):
        return "R2/R3 approval receipt is required"
    if isinstance(exc, LarkCLIProtocolError):
        return "lark-cli protocol error"
    if isinstance(exc, LarkCLIError):
        return str(exc)
    return "Feishu write failed"


def _hash_json(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _json_arg(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _append_optional(args: list[str], resource: Mapping[str, Any], key: str, *, flag: str | None = None) -> None:
    value = resource.get(key)
    if value not in (None, ""):
        args.extend([flag or "--" + key.replace("_", "-"), str(value)])


__all__ = [
    "ApprovalReceiptInvalid",
    "FeishuReadbackMismatch",
    "LarkCLIError",
    "LarkCLIProtocolError",
    "LarkCLIProvider",
    "LarkCLIResponse",
    "PersistentWriteLedger",
    "RealFeishuProvider",
    "SubprocessLarkCLIProvider",
    "parse_ok_envelope",
]
