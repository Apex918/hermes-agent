# Business OS integration and nightly reset protection

Date: 2026-08-24

## Recovery provenance

The latest checkout baseline is `a0ca7c192` (`origin/main`). The Business OS
integration was recovered from `hbos/r0-reconcile-current-main` at
`28678ec3f` and replayed in this isolated task worktree as commit
`e3e8febf1`. The source branch `hbos/phase-final-business-os-assembly` remains
available at `2b10ce665`; no shared checkout was overwritten and nothing was
pushed or deleted.

The reset evidence is `/Users/erich/.hermes/logs/update.log`: the update started
at `2026-08-24T16:08:50`, reported `Fast-forward not possible (history
 diverged), resetting to match remote...`, and completed at `origin/main`
`a0ca7c192`. The corresponding implementation is the same-branch divergence
fallback in `hermes_cli/update_cmd.py`.

## Fail-closed contract

The following paths now refuse destructive divergence recovery for `main` and
`master`:

- `hermes update` (`hermes_cli/update_cmd.py`): no fallback `reset --hard` or
  non-fast-forward merge; it exits with an isolated-worktree recovery message.
- POSIX installer (`scripts/install.sh`): the managed-install fallback refuses
  to reset protected branches.
- Windows installer (`scripts/install.ps1`): same protected-branch refusal.

Non-protected managed branches retain their existing reset fallback. This keeps
ordinary release-branch recovery available while preventing a nightly or
non-interactive update from erasing commits on the protected checkout.

The active nightly job (`505e463dffd5`, `hermes-nightly-optimization-loop`) is
already configured with the required external safety contract: the main
checkout is read-only, each change uses an isolated
`/Users/erich/hermes-optimization-worktrees/nightly-hermes-opt-...` worktree,
and reset/merge/cherry-pick/push operations are forbidden. This task does not
write the live Cron database; the repository-level guards cover the destructive
update entry points that produced the observed reset.

## Regression evidence

- Business OS and integration tests: 119 passed.
- Protected `hermes update` divergence test plus non-protected reset-failure
  regression: 2 passed.
- Installer guard tests: 2 passed.
- `bash -n scripts/install.sh`: passed.
- `git diff --check`: passed.

The protected-branch regression asserts that a simulated failed fast-forward on
`main` invokes neither `git reset --hard` nor `git merge --no-edit`, and emits
the isolated-worktree recovery message.
