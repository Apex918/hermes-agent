# N8A offline PoL replay bundle

This bundle is self-contained and offline. It replays the N6 daily cockpit report and N7 meeting-to-task fixture, then evaluates the N8A golden cases. It never calls Feishu, IMA, Kanban, the network, or a write API.

## Verification

```sh
cd pol_replay_bundle
python3 -m unittest -v test_replay_bundle.py
python3 replay_bundle.py --out replay-report.json
python3 -m json.tool replay-report.json >/dev/null
```

Expected control result: 4 cases, 1 `passed`, 3 `blocked`, 0 `failed`. A `blocked` golden case is an intentional fail-closed verdict whose assertions pass; `failed` means the observed behavior contradicts the case contract. `assertion_pass_rate` is the rate of all behavioral assertions. `format_valid` and `overreach_blocked` are separate metrics and are never inferred from the overall status.

## Inputs and provenance

| Input | Source path | SHA-256 |
|---|---|---|
| N6 daily report | `/Users/erich/.hermes/kanban/boards/ai-native-feishu-company/attachments/t_c0eb05fc/daily-report.json` | `08b67c983d611918c8e523b7f1b5178edd21a6efabe68f8dc9cf3f5c4889306a` |
| N7 meeting fixture | `/Users/erich/.hermes/kanban/boards/ai-native-feishu-company/attachments/t_2c57de7f/meeting-minutes.v1.json` | `33df2ab6ff1180675ba9a86ccb134282e88ea4804acd072bbf9c294461461f2d` |
| N7 manifest | `/Users/erich/.hermes/kanban/boards/ai-native-feishu-company/attachments/t_2c57de7f/N7-manifest.json` | `5e7a684826fe2c9e6e13416151bd78c489d93e4548abe3f8f024f4cd5d9476d1` |
| N8A golden cases | parent attachment `t_aa5d1e92/golden_cases.json` | `608c8bba84faf0a5cfa7e917692a7c3e2b1afe8b1852b2ec3e1ed46dbb22d501` |

The copied inputs are under `inputs/`; the replay report records their hashes. The copied N6 report contains `read_only=true`, `production_write=false`, and source/source_id fields. N7 replay expects 3 `preview` and 2 `needs_input` items and preserves the R2/deny-by-default boundary.
