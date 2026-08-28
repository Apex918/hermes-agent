# DecisionRecord — N8A PoL replay bundle

- decision: `PARTIAL / BLOCKED`
- effect_class: `R1`
- scope: local scratch only; no network, credentials, configuration edits, or external writes

## Facts (verified)

1. N6 replay consumed `inputs/n6-daily-report.json`; schema is `daily-operations-cockpit/v1`, `read_only=true`, and every replayed fact has `source` plus `source_id`. The observed fact count is 35.
2. N7 replay consumed the versioned local meeting fixture and manifest. It produced 5 deterministic previews: 3 `preview` and 2 `needs_input`; all previews are `production_write=false`, `approval_level=R2`.
3. N8A replay evaluated 4 behavioral golden cases: 1 `passed`, 3 intentional `blocked`, 0 `failed`. `blocked` means the fail-closed outcome was expected and all assertions matched; `failed` means an assertion mismatch. Failures and blocked cases retain a trace ID and Kanban event IDs.
4. `assertion_pass_rate` is computed from behavioral assertions. `format_valid` and `overreach_blocked` are independent boolean metrics, so malformed output and attempted external writes cannot be hidden by an aggregate score.
5. Source hashes and the exact offline commands are recorded in `manifest.json` and `README.md`.

## Inferences

- The local shadow/replay contract is reproducible and supports human review.
- This evidence does not establish real Feishu/IMA connectivity, production authorization, release readiness, remote CI status, or trace retention outside the bundle.

## Recommendation / handoff

Keep the work in shadow-only status. N10R may replay this bundle and independently verify its hashes and report, but must not promote this result to a real Feishu/IMA or release approval. Any external integration requires fresh evidence, explicit R2/R3 approval, and a separate read-only review.

## Verification receipt

```text
python3 -m unittest -v test_replay_bundle.py
# Ran 3 tests ... OK
python3 replay_bundle.py --out replay-report.json
# N6 passed; N7 preview=3, needs_input=2; N8A cases=4, passed=1, blocked=3, failed=0
python3 -m json.tool replay-report.json >/dev/null
```
