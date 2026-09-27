# Three-System Hermes Adapter — Release Decision

- Date: 2026-08-29
- Kanban: `three-system-command-center`
- H1: `t_163858b3`
- H2: `t_0cb5640e`
- H3: `t_e9235287`
- Worktree: `/Users/erich/Documents/three-system-hermes-adapter-wt`
- Branch: `feature/three-system-hermes-adapter-v1`

## Decision

**CONDITIONAL — coordination-only/worktree-only.**

The Hermes adapter is accepted as an isolated, read-only coordination boundary. It
must not be described as merged into Hermes main or as an integration with RuoYi or
Mario production.

## Verified scope

- Reuses Hermes' existing `agent.orchestrator.Orchestrator` planning boundary.
- Exposes `CoordinationAdapter` through `business.ai_business_os`.
- Accepts only explicit fixtures or local command-center JSON artifacts.
- Validates Mission/Evidence/Decision/ActionRequest structure before planning.
- Rejects network URLs and dangerous order, trading, credential, database, Redis,
  WebSocket, production, and write actions fail-closed.
- Enforces `network_access=false`, `live_trading=false`,
  `order_submission=forbidden`, and `external_effects=0`.
- Preserves source provenance and `integration_status=coordination-only/worktree-only`.

## Verification evidence

- `tests/business`: **140 passed**
- Fresh-process public import test: passed
- `py_compile`: passed
- `git diff --check`: passed
- No RuoYi, Mario, exchange, wallet, Redis, database, WebSocket, authentication, or
  production-write call was introduced.
- RuoYi and Mario existing dirty WIP was not modified by this worktree.

## Explicit non-claims

- No Hermes main merge has occurred.
- No plugin/tool registration into the production Hermes runtime has occurred.
- No RuoYi API or Mario API/Redis integration has occurred.
- No live, paper, shadow, order, balance, or wallet side effect has occurred.

## Next gate

Before any real adapter is introduced, a separate Kanban card must define and review
an allow-listed read-only API contract for each system, including authentication
ownership, tenant/ACL propagation, timeout/retry behavior, evidence freshness, and
read-back verification. Until that card is independently approved, the adapter stays
fixture/local-only.
