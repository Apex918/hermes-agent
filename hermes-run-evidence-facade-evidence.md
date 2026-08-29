# Hermes run-evidence read facade

Status: `implemented_offline_fixture_only`

This worktree adds the Hermes-owned read projection at the exact route:

```text
GET /api/readonly/{tenant_id}/runs/{run_id}/evidence
```

The route is registered by the existing Hermes API server, but it serves data
only when an explicitly injected `HermesRunEvidenceStore` fixture facade is
present. Normal API-server startup does not inject one, so this change does
not claim a live integration or create a new data source.

## Source API evidence

The pre-existing Hermes API source confirms:

- `GET /v1/runs/{run_id}` and `GET /v1/runs/{run_id}/events` are documented in
  `gateway/platforms/api_server.py:17-19` and registered at
  `gateway/platforms/api_server.py:2267-2269`.
- The source API version is `0.20.6` from `hermes_cli/__init__.py:17`, carried
  by the facade as `v1/0.20.6`.
- These existing routes are evidence for the native run surface only; they are
  not treated as proof that the new tenant-scoped facade already exists.

## Security and scope boundary

API-server authentication remains target-owned (`API_SERVER_KEY`, with mTLS
termination outside this adapter). The facade requires explicit
`X-Principal-Ref`, `X-Tenant-Id`, `X-Project-Id`, and exact `X-ACL-Scope:
read:run` values. The path tenant and header tenant must match the fixture's
ACL record. `X-Credential-Ref`, if supplied, is shape-checked as an opaque
reference and is never resolved, returned, logged, or passed to the store.

Only the `read` operation exists on the fixture store. The route rejects query
parameters and request bodies; no POST/PUT/PATCH/DELETE route is added. The
projection carries `integration_status=not_integrated`, `network_access=false`,
and `external_effects=0` and is constrained by
`schemas/hermes-run-evidence.schema.json`.

No live network probe, credential read, database/Redis/WebSocket access,
event write, delegate, Kanban mutation, deployment, or production integration
was performed.
