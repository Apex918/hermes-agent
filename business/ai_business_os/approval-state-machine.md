# AI Business OS approval state machine

Business writes use an explicit, receipt-gated lifecycle:

```text
dry_run -> pending_approval -> approved/rejected -> applying -> applied/failed
```

- `dry_run` builds and returns the preview, diff, rollback payload, audit reference,
  and idempotency key without persisting or invoking a side effect.
- `pending_approval` is the durable queue state. It is the only state shown as
  pending to an approver.
- `approved` requires a non-empty business approval receipt for R2 and R3.
  `--yolo`, `yolo`, and `approvals.mode=off` are explicitly rejected as receipts;
  runtime bypass configuration is not business approval.
- `rejected` is terminal and records the reason. Missing/invalid R2/R3 receipts
  reject before the effect callback is invoked.
- `applying` is written to the durable approval ledger immediately before the
  effect callback runs. The callback is then executed exactly once for an
  idempotency key.
- `applied` records the result. `failed` records callback exceptions or explicit
  `{success: false}` results and does not silently become approved.

Each record retains `state_history`, `approval_receipt`, `audit_ref`, preview,
rollback, result/error, and `idempotency_key`. The approval ledger under
`$HERMES_HOME/approval/` makes terminal results readable after the pending queue
item is removed and prevents retries after process restart from re-running the
side effect. `read_approval()` accepts the record id, audit reference, or
idempotency key.

This mechanism governs simulated/business writes only; it does not reinterpret
Hermes command approval settings as a business approver decision.
