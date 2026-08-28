# N12 explicit lark-cli Feishu provider

`business.ai_business_os.lark_cli_provider.LarkCLIProvider` is the production
boundary for Feishu Task, Calendar, and Base access. It invokes the installed
`lark-cli` executable as a subprocess and never imports the Feishu SDK or opens
a WebSocket.

## Reads

```python
from business.ai_business_os import LarkCLIProvider

provider = LarkCLIProvider(
    profile="default",                 # lark-cli auth/config profile
    base_token="<configured-base-token>",
    table_id="<configured-table-id>",
)
tasks = provider.read_tasks()
events = provider.read_calendar()
records = provider.read_base()
```

Each command uses `--as user` and `--json`. The subprocess output must be a
JSON object with `ok: true` and `data`; raw JSON and error envelopes fail closed.
Authentication is left to lark-cli's existing auth/config store. Hermes does
not print subprocess arguments, stdout, stderr, or environment values.

## Writes

A write requires all of the following: an explicit target, human-readable
preview, diff, rollback mapping, idempotency key, and an R2/R3 receipt. The
provider records a redacted request hash and response/readback hashes in the
persistent SQLite ledger before reporting success:

```python
result = provider.write(
    kind="task",
    resource={"summary": "One canary task"},
    target="task:canary",
    preview="Create exactly one canary task",
    diff="+ One canary task",
    rollback={"operation": "delete", "required": True},
    idempotency_key="n12-canary-2026-09-01",
    approval_receipt="R2:operator-receipt",
)
```

The write response must contain its returned `guid`, `event_id`, or
`record_id`. The provider performs an identifier-specific readback and reports
`applied` only when the readback contains that exact identifier. Missing or
invalid receipts, CLI errors, timeouts, and readback mismatches are terminal
for that idempotency key. Replaying the same key never issues another remote
write and returns `duplicate` from the ledger.

`dry_run=True` validates the full approval envelope and returns a preview
without invoking lark-cli. The N12 canary policy remains conservative: do not
call `write` for IM, Calendar, or Base unless the parent task has separately
approved one explicitly scoped test resource.

Default ledger path: `~/.hermes/feishu/write-ledger.sqlite3`. Override it with
`ledger_path` in tests or an isolated deployment.
