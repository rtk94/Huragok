---
task_id: task-example
author_agent: architect
written_at: 2026-04-15T14:02:00Z
session_id: 01HXEX0000000000000000000
---

# Add `/healthz` endpoint

## Problem statement

The service has no liveness endpoint. Container orchestrators and uptime monitors have no machine-checkable way to tell whether the process is alive and serving requests. This task adds a minimal `/healthz` endpoint that returns 200 when the process can accept requests.

## Acceptance criteria

- `GET /healthz` returns HTTP 200 with a JSON body `{"status": "ok"}`.
- The endpoint does not require authentication.
- The endpoint is included in the OpenAPI schema with a tag `infra`.
- Response latency under normal load is under 10ms p99.
- The endpoint does not touch the database, cache, or any downstream service. A `/healthz` that depends on the database tells you whether the database is up, not whether the service is alive.

## Scope

**In scope:**
- Add `GET /healthz` to the FastAPI app.
- Tag the endpoint `infra` in OpenAPI.
- No new dependencies.

**Out of scope:**
- `/readyz` (readiness, including downstream checks) — separate task.
- Structured logging for health checks — separate concern.
- Metrics for request counts — separate task.

## Interface / API shape

```
GET /healthz

Response 200:
  Content-Type: application/json
  Body: {"status": "ok"}
```

No request body. No query parameters. No authentication.

## Dependencies

- **Depends on tasks:** none
- **New libraries:** none
- **Environment:** none

## Open questions

*(None.)*
