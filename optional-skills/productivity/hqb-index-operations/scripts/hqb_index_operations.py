#!/usr/bin/env python3
"""Offline HQB evidence, readonly-summary, readback, and report workflow.

This helper is intentionally a local projection tool.  It never opens a
network connection, reads credentials, calls RuoYi, or exposes a mutation
command.  Its output can be reviewed or handed to an operator for a separate,
explicitly approved ingestion step.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

SKILL_NAME = "hqb-index-operations"
FIXTURE_SCHEMA = "hqb.index.operations.fixture.v1"
PACKET_SCHEMA = "hermes-evidence/v1"
REPORT_SCHEMA = "hqb.index.operations.report.v1"
IMA_SCHEMA = "hermes.ai_business_os.feishu_explicit.v1"
INTEGRATION_STATUS = "not_integrated"
NETWORK_ACCESS = False
EXTERNAL_EFFECTS = 0
DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "hqb-index-operations.v1.json"
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_SENSITIVE_KEY_PARTS = (
    "access_token",
    "api_key",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "encrypt_key",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
)
_ALLOWED_CANDIDATE_TYPES = {"OFFER", "QUOTE_OBSERVATION"}


class HqbOperationsError(ValueError):
    """Raised when an offline HQB operation cannot be safely completed."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _assert_safe(value: Any, *, path: str = "payload") -> None:
    """Reject credential-like fields and network sources before consuming data."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_sensitive_key(key):
                raise HqbOperationsError(f"{path}.{key}: credential-like fields are not accepted")
            _assert_safe(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_safe(item, path=f"{path}[{index}]")
    elif isinstance(value, str) and "://" in value:
        raise HqbOperationsError(f"{path}: network sources are not permitted")


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HqbOperationsError(f"{name} is required")
    return value.strip()


def _load_json(path: str | Path) -> dict[str, Any]:
    if isinstance(path, str) and "://" in path:
        raise HqbOperationsError("fixture must be a local path, not a network URL")
    local_path = Path(path).expanduser()
    if not local_path.is_file():
        raise HqbOperationsError(f"local fixture is unavailable: {local_path}")
    try:
        value = json.loads(local_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HqbOperationsError(f"cannot read local fixture: {local_path}") from exc
    if not isinstance(value, dict):
        raise HqbOperationsError("fixture root must be a JSON object")
    _assert_safe(value)
    return value


def validate_fixture(fixture: Mapping[str, Any]) -> None:
    """Validate the offline contract and all integration safety invariants."""
    if not isinstance(fixture, Mapping):
        raise HqbOperationsError("fixture must be a JSON object")
    _assert_safe(fixture)
    if fixture.get("schema_version") != FIXTURE_SCHEMA:
        raise HqbOperationsError(f"schema_version must be {FIXTURE_SCHEMA}")
    if fixture.get("integration_status") != INTEGRATION_STATUS:
        raise HqbOperationsError("integration_status must remain not_integrated")
    if fixture.get("network_access") is not False:
        raise HqbOperationsError("network_access must remain false")
    if fixture.get("external_effects") != EXTERNAL_EFFECTS:
        raise HqbOperationsError("external_effects must remain zero")
    _require_text(fixture.get("tenant_id"), "tenant_id")
    summary = fixture.get("readonly_summary", fixture.get("ruoyi_readonly_summary"))
    if not isinstance(summary, Mapping):
        raise HqbOperationsError("readonly_summary is required")
    _validate_summary(summary, tenant_id=str(fixture["tenant_id"]))
    batches = fixture.get("readback_batches", fixture.get("batches", []))
    if not isinstance(batches, list):
        raise HqbOperationsError("readback_batches must be a list")
    for index, batch in enumerate(batches):
        _validate_batch(batch, index=index)


def load_fixture(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate a local fixture; no other source is accepted."""
    fixture = _load_json(DEFAULT_FIXTURE if path is None else path)
    validate_fixture(fixture)
    return fixture


def _validate_summary(summary: Mapping[str, Any], *, tenant_id: str) -> None:
    if summary.get("mode") != "readonly":
        raise HqbOperationsError("RuoYi summary mode must be readonly")
    if summary.get("method") != "GET":
        raise HqbOperationsError("RuoYi summary method must be GET")
    endpoint = _require_text(summary.get("endpoint"), "readonly_summary.endpoint")
    if not (endpoint.startswith("/hqb/") or endpoint.startswith("/api/readonly/")):
        raise HqbOperationsError("readonly_summary.endpoint must be a local readonly path")
    if summary.get("tenant_id") != tenant_id:
        raise HqbOperationsError("readonly summary crosses tenant scope")
    data = summary.get("data", summary.get("payload"))
    if not isinstance(data, Mapping):
        raise HqbOperationsError("readonly_summary.data must be an object")


def read_ruoyi_readonly_summary(source: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Return the fixture's RuoYi GET projection, never a write response."""
    fixture = dict(source) if isinstance(source, Mapping) else load_fixture(source)
    validate_fixture(fixture)
    summary = fixture.get("readonly_summary", fixture.get("ruoyi_readonly_summary"))
    assert isinstance(summary, Mapping)  # guarded by validate_fixture
    return deepcopy(dict(summary))


def _validate_batch(batch: Any, *, index: int = 0) -> None:
    if not isinstance(batch, Mapping):
        raise HqbOperationsError(f"readback_batches[{index}] must be an object")
    _require_text(batch.get("batch_id", batch.get("id")), f"readback_batches[{index}].batch_id")
    if "status" not in batch:
        raise HqbOperationsError(f"readback_batches[{index}].status is required")
    count = batch.get("candidate_count", batch.get("candidateCount"))
    if not isinstance(count, int) or count < 0:
        raise HqbOperationsError(f"readback_batches[{index}].candidate_count must be non-negative")
    ids = batch.get("candidate_ids", batch.get("candidateIds", []))
    if not isinstance(ids, list) or any(not isinstance(item, (str, int)) for item in ids):
        raise HqbOperationsError(f"readback_batches[{index}].candidate_ids must be a list")
    if ids and len(ids) != count:
        raise HqbOperationsError(f"readback_batches[{index}] candidate count does not match ids")


def readback_batch(source: Mapping[str, Any] | str | Path, batch_id: str | int) -> dict[str, Any]:
    """Read and normalize one local RuoYi batch response by exact identifier."""
    fixture = dict(source) if isinstance(source, Mapping) else load_fixture(source)
    validate_fixture(fixture)
    wanted = str(batch_id)
    batches = fixture.get("readback_batches", fixture.get("batches", []))
    assert isinstance(batches, list)
    for raw in batches:
        assert isinstance(raw, Mapping)
        current = str(raw.get("batch_id", raw.get("id")))
        if current != wanted:
            continue
        candidate_ids = raw.get("candidate_ids", raw.get("candidateIds", []))
        count = raw.get("candidate_count", raw.get("candidateCount"))
        result = {
            "batch_id": current,
            "status": raw.get("status"),
            "candidate_count": count,
            "candidate_ids": deepcopy(candidate_ids),
            "verified": bool(raw.get("verified", True)),
            "source": "ruoyi-readonly-fixture",
            "integration_status": INTEGRATION_STATUS,
            "network_access": NETWORK_ACCESS,
            "external_effects": EXTERNAL_EFFECTS,
        }
        return result
    raise HqbOperationsError(f"readback batch is unavailable: {wanted}")


def readback_batches(source: Mapping[str, Any] | str | Path) -> list[dict[str, Any]]:
    """Read every fixture batch with exact-id normalization and no mutation."""
    fixture = dict(source) if isinstance(source, Mapping) else load_fixture(source)
    validate_fixture(fixture)
    batches = fixture.get("readback_batches", fixture.get("batches", []))
    assert isinstance(batches, list)
    return [readback_batch(fixture, str(item.get("batch_id", item.get("id")))) for item in batches]


def _candidate_values(candidate: Mapping[str, Any]) -> tuple[str, Any, str | None]:
    candidate_type = str(candidate.get("candidate_type", candidate.get("candidateType", ""))).upper()
    if candidate_type not in _ALLOWED_CANDIDATE_TYPES:
        raise HqbOperationsError("candidate_type must be OFFER or QUOTE_OBSERVATION")
    payload = candidate.get("payload", candidate.get("evidence"))
    if not isinstance(payload, Mapping):
        raise HqbOperationsError("candidate payload must be an object")
    supplied_hash = candidate.get("candidate_hash", candidate.get("candidateHash"))
    if supplied_hash is not None and (not isinstance(supplied_hash, str) or _SHA256.fullmatch(supplied_hash) is None):
        raise HqbOperationsError("candidate_hash must be a SHA-256 hex value")
    return candidate_type, payload, supplied_hash


def build_evidence_packet(source: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Build a RuoYi-compatible evidence packet without submitting it."""
    fixture = dict(source) if isinstance(source, Mapping) else load_fixture(source)
    validate_fixture(fixture)
    raw_candidates = fixture.get("candidates", fixture.get("evidence_candidates", []))
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise HqbOperationsError("at least one evidence candidate is required")
    candidates: list[dict[str, Any]] = []
    for raw in raw_candidates:
        if not isinstance(raw, Mapping):
            raise HqbOperationsError("evidence candidate must be an object")
        candidate_type, payload, supplied_hash = _candidate_values(raw)
        payload_json = _canonical(deepcopy(dict(payload)))
        candidate_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if supplied_hash is not None and supplied_hash.lower() != candidate_hash:
            raise HqbOperationsError("candidate_hash does not match its payload")
        candidates.append({
            "candidateHash": candidate_hash,
            "candidateType": candidate_type,
            "payload": payload_json,
        })
    tenant_id = str(fixture["tenant_id"])
    provenance = str(fixture.get("provenance", "hermes:offline-fixture"))
    packet_body = {
        "schema_version": PACKET_SCHEMA,
        "packet_id": str(fixture.get("packet_id", "hqb-offline-packet")),
        "tenant_id": tenant_id,
        "producer": "hermes",
        "provenance": provenance,
        "observed_at": fixture.get("observed_at"),
        "evidence": deepcopy(fixture.get("evidence", [])),
        "integration_status": INTEGRATION_STATUS,
        "network_access": NETWORK_ACCESS,
        "external_effects": EXTERNAL_EFFECTS,
    }
    packet_json = _canonical(packet_body)
    packet_hash = hashlib.sha256(packet_json.encode("utf-8")).hexdigest()
    return {
        "object": "hqb.hermes.evidence_packet",
        "schema_version": PACKET_SCHEMA,
        "tenant_id": tenant_id,
        "packet_hash": packet_hash,
        "provenance": provenance,
        "packet": packet_json,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "integration_status": INTEGRATION_STATUS,
        "network_access": NETWORK_ACCESS,
        "external_effects": EXTERNAL_EFFECTS,
        "submission": "not_submitted",
    }


def build_daily_report(source: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Combine packet metadata, readonly summary, and batch readbacks."""
    fixture = dict(source) if isinstance(source, Mapping) else load_fixture(source)
    validate_fixture(fixture)
    packet = build_evidence_packet(fixture)
    summary = read_ruoyi_readonly_summary(fixture)
    batches = readback_batches(fixture)
    verified = sum(1 for batch in batches if batch["verified"])
    return {
        "object": "hqb.index.operations.daily_report",
        "schema_version": REPORT_SCHEMA,
        "tenant_id": str(fixture["tenant_id"]),
        "as_of": fixture.get("as_of", fixture.get("observed_at")),
        "integration_status": INTEGRATION_STATUS,
        "network_access": NETWORK_ACCESS,
        "external_effects": EXTERNAL_EFFECTS,
        "packet": {
            "packet_hash": packet["packet_hash"],
            "candidate_count": packet["candidate_count"],
            "submission": packet["submission"],
        },
        "ruoyi_readonly_summary": summary,
        "batch_readback": batches,
        "counts": {
            "candidate_count": packet["candidate_count"],
            "batch_count": len(batches),
            "verified_batch_count": verified,
        },
        "next_action": "human_review_then_explicit_ruoyi_ingestion",
    }


def ima_archive_preview(report: Mapping[str, Any] | str, *, report_id: str = "hqb-daily-report", destination: str = "ima://archive") -> dict[str, Any]:
    """Create the existing IMA archive dry-run envelope; never archive remotely."""
    if not isinstance(report, (str, Mapping)):
        raise HqbOperationsError("IMA report must be text or an object")
    _assert_safe(report, path="ima_report")
    report_value: Any = report if isinstance(report, str) else deepcopy(dict(report))
    payload = {"report_id": report_id, "destination": destination, "report": report_value}
    audit_ref = f"{SKILL_NAME}:ima:{report_id}"
    key = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return {
        "schema_version": IMA_SCHEMA,
        "adapter": "feishu-explicit-api",
        "kind": "ima_report",
        "operation": "archive",
        "mode": "dry_run",
        "production_write": False,
        "idempotency_key": key,
        "audit_ref": audit_ref,
        "payload": payload,
        "integration_status": INTEGRATION_STATUS,
        "network_access": NETWORK_ACCESS,
        "external_effects": EXTERNAL_EFFECTS,
    }


def _read_report(value: str) -> str:
    if "://" in value:
        raise HqbOperationsError("IMA report must be local text or a local file")
    path = Path(value).expanduser()
    if path.exists():
        if not path.is_file():
            raise HqbOperationsError("IMA report path must be a file")
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise HqbOperationsError("cannot read local IMA report") from exc
    return value


def _add_local_options(command_parser: argparse.ArgumentParser) -> None:
    """Accept fixture/output options both before and after the subcommand.

    Shell users naturally write ``report --output report.json`` while the
    top-level parser convention is ``--output report.json report``.  Keep both
    spellings working without letting a subparser's defaults overwrite values
    already parsed by the parent parser.
    """
    command_parser.add_argument(
        "--fixture", type=Path, default=argparse.SUPPRESS, help="local JSON fixture"
    )
    command_parser.add_argument(
        "--output", type=Path, default=argparse.SUPPRESS, help="optional local JSON output path"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=SKILL_NAME, description="Offline HQB evidence and reporting")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE, help="local JSON fixture")
    parser.add_argument("--output", type=Path, default=None, help="optional local JSON output path")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("packet", "summary", "readback", "report", "all"):
        command_parser = sub.add_parser(command)
        _add_local_options(command_parser)
        if command == "readback":
            command_parser.add_argument("batch_id")
    ima = sub.add_parser("ima-archive", help="preview existing IMA archive rules")
    _add_local_options(ima)
    ima.add_argument("--report", required=True, help="local report text or local file")
    ima.add_argument("--report-id", default="hqb-daily-report")
    ima.add_argument("--destination", default="ima://archive")
    return parser


def _command(args: argparse.Namespace) -> Any:
    if args.command == "ima-archive":
        return ima_archive_preview(_read_report(args.report), report_id=args.report_id, destination=args.destination)
    fixture = load_fixture(args.fixture)
    if args.command == "packet":
        return build_evidence_packet(fixture)
    if args.command == "summary":
        return read_ruoyi_readonly_summary(fixture)
    if args.command == "readback":
        return readback_batch(fixture, args.batch_id)
    if args.command == "report":
        return build_daily_report(fixture)
    return {
        "packet": build_evidence_packet(fixture),
        "ruoyi_readonly_summary": read_ruoyi_readonly_summary(fixture),
        "batch_readback": readback_batches(fixture),
        "report": build_daily_report(fixture),
        "integration_status": INTEGRATION_STATUS,
        "network_access": NETWORK_ACCESS,
        "external_effects": EXTERNAL_EFFECTS,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _command(args)
        rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        if args.output is not None:
            if "://" in str(args.output):
                raise HqbOperationsError("output must be a local path")
            output = args.output.expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0
    except (HqbOperationsError, OSError) as exc:
        print(f"{SKILL_NAME}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_FIXTURE",
    "EXTERNAL_EFFECTS",
    "FIXTURE_SCHEMA",
    "HqbOperationsError",
    "INTEGRATION_STATUS",
    "NETWORK_ACCESS",
    "build_daily_report",
    "build_evidence_packet",
    "ima_archive_preview",
    "load_fixture",
    "main",
    "read_ruoyi_readonly_summary",
    "readback_batch",
    "readback_batches",
    "validate_fixture",
]
