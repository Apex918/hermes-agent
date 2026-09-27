---
name: hqb-index-operations
description: Run offline HQB evidence and readonly reports.
version: 0.1.0
author: Nous Research, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [HQB, RuoYi, Evidence, Readonly, Reports, Cron]
    category: productivity
    related_skills: [hermes-agent, computer-use]
---

# HQB Index Operations Skill

Use this skill to turn a local, already-observed HQB fixture into a reviewable
Hermes evidence packet, read a RuoYi readonly summary, read back ingestion
batches, and render a daily report. The workflow is deliberately offline:
Hermes produces evidence and proposals only; it does not write RuoYi facts,
resolve credentials, or fetch pages that require login or CAPTCHA workarounds.

## When to Use

- A Kanban task asks for an HQB evidence packet or daily operations report.
- An operator supplies a local JSON fixture containing HQB observations and a
  RuoYi readonly projection.
- A scheduled Cron run should repeat the same local report generation.
- Don't use for live RuoYi mutations, purchasing, trading, credential setup, or
  scraping an authenticated or CAPTCHA-protected page.

## Prerequisites

- Python 3.11+ and the checked-in script under this skill's `scripts/`.
- A local JSON fixture following `fixtures/hqb-index-operations.v1.json`.
- The fixture must declare `integration_status: not_integrated`,
  `network_access: false`, and `external_effects: 0`.
- No API key, cookie, token, browser login, or RuoYi URL is needed.

## How to Run

Invoke the script through the `terminal` tool. The default fixture is the
checked-in offline fixture; use `--fixture` for another local file.

```
python scripts/hqb_index_operations.py packet
python scripts/hqb_index_operations.py summary
python scripts/hqb_index_operations.py readback batch-001
python scripts/hqb_index_operations.py report --output /tmp/hqb-report.json
python scripts/hqb_index_operations.py all --output /tmp/hqb-run.json
python scripts/hqb_index_operations.py ima-archive --report /tmp/hqb-report.json
```

The output is JSON. `--output` writes only to a local path; it never submits the
packet or report.

## Quick Reference

```
packet       Build a hashed Hermes evidence packet; do not submit it.
summary      Read the fixture's GET-only RuoYi summary.
readback ID  Read one exact batch ID from the local readback list.
report       Combine packet metadata, summary, and batch readbacks.
all          Emit all three artifacts plus the combined report.
ima-archive  Produce the existing IMA archive dry-run envelope.
```

## Procedure

### 1. Establish a safe local source

Use `read_file` to inspect the fixture before running the script. If it is a
URL, contains credential-like keys, or lacks the three safety flags, stop.
Done when the source is a local JSON object with the required safety flags.

### 2. Produce the evidence packet

Use the `packet` command through `terminal`. It hashes each candidate payload
and the canonical packet JSON, and emits `submission: not_submitted`. Never
turn this output into a RuoYi write from this workflow. Done when the packet
contains at least one `OFFER` or `QUOTE_OBSERVATION` candidate and two SHA-256
hex hashes.

### 3. Read the RuoYi projection

Use the `summary` command. Accept only a fixture projection marked
`mode: readonly`, `method: GET`, and scoped to the same tenant. A summary is a
readback of RuoYi's state, not proof that Hermes may mutate it. Done when the
returned object contains only the validated readonly summary.

### 4. Read back batches

Use `readback <batch-id>` for an exact identifier, or `readback_batches` through
the `all` command for every fixture batch. Check candidate counts and the
`verified` flag; a missing or mismatched batch is an error, not a successful
empty result. Done when every reported batch has an explicit status and exact
candidate count.

### 5. Render and archive the report preview

Use `report` or `all` to create the daily JSON report. If IMA archival is
requested, use `ima-archive`; it follows the existing IMA adapter rules and
returns a dry-run envelope only. Done when the report carries
`integration_status: not_integrated`, `network_access: false`, and
`external_effects: 0`, and any IMA preview has `production_write: false`.

### 6. Schedule or hand off without widening scope

For recurring work, use the `cronjob` tool with this script as the local
pre-run/report step, a fixed `workdir`, and local-only output. For a dispatched
Kanban task, return the JSON report as the handoff; do not create a mutation
card from inside this skill. Done when the schedule or handoff names the
fixture, output artifact, and safety flags.

## Browser and Computer Use Boundary

`browser_navigate` and `computer_use` may be used only to inspect a public page
when an operator explicitly asks for a fixture refresh. Save the resulting
observation locally, then run the offline script. Stop immediately on a login
wall, MFA prompt, CAPTCHA, bot-check, or request for a secret; do not try to
bypass it. The report must not claim a page was observed when only a blocked
page was reached.

## Pitfalls

- A successful packet build is not ingestion. This skill intentionally has no
  RuoYi POST, PUT, PATCH, DELETE, materialize, or acknowledgement path.
- `readonly_summary` is tenant-scoped. Never combine a summary or batch from a
  different tenant into the packet report.
- Do not add credentials to a fixture to make a provider call work. This flow
  does not read credentials at all.
- The default IMA destination is a label for the existing archive rule; the
  preview is not a remote archive and has no external effect.
- A missing batch is not equivalent to zero candidates. Keep the failure
  visible so an operator can investigate the source-owned readback.
- `integration_status` stays `not_integrated` until a separately reviewed
  integration is explicitly implemented and verified outside this skill.

## Verification

Run the skill's focused tests through the repository test runner:

```
scripts/run_tests.sh tests/skills/test_hqb_index_operations_skill.py -q
```

For a direct fixture smoke check, run `all` and verify the JSON contains the
packet hash, `ruoyi_readonly_summary`, `batch_readback`, and all three safety
flags. Tests are offline and must not require a network, credential file, or
browser session.
