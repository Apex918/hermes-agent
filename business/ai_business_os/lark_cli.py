"""Small ``lark-cli`` contract surface for the explicit Feishu adapter.

Examples::

    lark-cli read tasks --json
    lark-cli preview calendar --json
    lark-cli ima archive --report report.md --json

All commands are local reads or dry-run previews.  There is intentionally no
production ``write`` command in this CLI.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from .feishu_adapter import FeishuAdapterScope, FeishuExplicitAdapter, FakeFeishuProvider
from .feishu_fixtures import load_feishu_event_fixture


_RESOURCE_ALIASES = {
    "task": "tasks",
    "tasks": "tasks",
    "calendar": "calendar",
    "calendars": "calendar",
    "event": "calendar",
    "events": "calendar",
    "base": "base",
    "record": "base",
    "records": "base",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lark-cli", description="Explicit Feishu reads and dry-run previews")
    parser.add_argument("--fixture", type=Path, default=None, help="local JSON fixture (never a URL)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    read = sub.add_parser("read", help="read Task, Calendar, or Base through the provider")
    read.add_argument("resource", choices=tuple(_RESOURCE_ALIASES), help="resource type")
    read.add_argument("--fixture", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    read.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    preview = sub.add_parser("preview", help="create dry-run payloads; never writes")
    preview.add_argument("resource", choices=tuple(_RESOURCE_ALIASES) + ("all",), help="resource type")
    preview.add_argument("--fixture", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    preview.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    # Resource-first spellings are useful for shell scripts and are kept as
    # aliases of preview, not as separate implementation paths.
    for resource in ("task", "calendar", "base"):
        command = sub.add_parser(resource, help=f"preview a {resource} payload")
        command.set_defaults(command="preview", resource=resource)
        command.add_argument("--fixture", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        command.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    ima = sub.add_parser("ima", help="IMA report operations")
    ima_sub = ima.add_subparsers(dest="ima_command", required=True)
    archive = ima_sub.add_parser("archive", help="preview IMA report archival")
    archive.add_argument("--report", required=True, help="report text or local report file")
    archive.add_argument("--report-id", default="ima-report")
    archive.add_argument("--destination", default="ima://archive")
    archive.add_argument("--fixture", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    archive.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return parser


def _load_fixture(path: Path | None) -> dict[str, Any]:
    if path is None:
        return load_feishu_event_fixture()
    if "://" in str(path):
        raise ValueError("fixture must be a local path, not a network URL")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read local fixture: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("fixture root must be an object")
    # Checked-in event fixtures get the stronger scope and network checks.
    if "schema_version" in value:
        return load_feishu_event_fixture(path)
    return value


def _adapter(path: Path | None) -> FeishuExplicitAdapter:
    fixture = _load_fixture(path)
    scope_data = fixture.get("scope")
    scope = None
    if isinstance(scope_data, dict):
        values = (scope_data.get("tenant_id"), scope_data.get("profile"), scope_data.get("workspace_id"))
        if all(isinstance(value, str) and value for value in values):
            scope = FeishuAdapterScope(str(values[0]), str(values[1]), str(values[2]))
    return FeishuExplicitAdapter(FakeFeishuProvider.from_fixture(fixture), scope=scope)


def _read(adapter: FeishuExplicitAdapter, resource: str) -> Any:
    kind = _RESOURCE_ALIASES[resource]
    return {
        "tasks": adapter.read_tasks,
        "calendar": adapter.read_calendar,
        "base": adapter.read_base,
    }[kind]()


def _preview(adapter: FeishuExplicitAdapter, resource: str) -> Any:
    if resource == "all":
        return adapter.preview_all()
    kind = _RESOURCE_ALIASES[resource]
    items = _read(adapter, resource)
    method = {
        "tasks": adapter.preview_task,
        "calendar": adapter.preview_calendar,
        "base": adapter.preview_base,
    }[kind]
    return [method(item) for item in items]


def _report_value(value: str) -> str:
    path = Path(value)
    if path.exists():
        if not path.is_file():
            raise ValueError("IMA report path must be a file")
        return path.read_text(encoding="utf-8")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "ima":
            result = _adapter(args.fixture).archive_ima_report(
                _report_value(args.report), report_id=args.report_id, destination=args.destination
            )
        else:
            adapter = _adapter(args.fixture)
            result = _read(adapter, args.resource) if args.command == "read" else _preview(adapter, args.resource)
    except (OSError, ValueError, KeyError) as exc:
        print(f"lark-cli: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the console entry point
    raise SystemExit(main())


__all__ = ["main"]
