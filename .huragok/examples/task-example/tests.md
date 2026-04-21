---
task_id: task-example
author_agent: testwriter
written_at: 2026-04-15T14:28:00Z
session_id: 01HXEX0000000000000000002
---

# Tests: Add `/healthz` endpoint

## Test inventory

- `tests/routes/test_health.py::test_healthz_returns_200` — covers AC-1
- `tests/routes/test_health.py::test_healthz_body_is_ok` — covers AC-1
- `tests/routes/test_health.py::test_healthz_no_auth_required` — covers AC-2
- `tests/routes/test_health.py::test_healthz_in_openapi_with_infra_tag` — covers AC-3
- `tests/routes/test_health.py::test_healthz_under_10ms_p99` — covers AC-4
- `tests/routes/test_health.py::test_healthz_makes_no_db_calls` — covers AC-5

## Acceptance criterion coverage

| Criterion                                        | Covering test(s)                                 |
| ------------------------------------------------ | ------------------------------------------------ |
| AC-1: returns 200 with `{"status": "ok"}`        | test_healthz_returns_200, test_healthz_body_is_ok |
| AC-2: no auth required                           | test_healthz_no_auth_required                    |
| AC-3: in OpenAPI with `infra` tag                | test_healthz_in_openapi_with_infra_tag           |
| AC-4: p99 < 10ms under normal load               | test_healthz_under_10ms_p99                      |
| AC-5: does not touch DB / cache / downstream     | test_healthz_makes_no_db_calls                   |

## Run results

Command: `uv run pytest tests/routes/test_health.py -v`

- **Passed:** 6
- **Failed:** 0
- **Skipped:** 0

## Mutation testing results

Command: `uv run mutmut run --paths-to-mutate=api/routes/health.py`

- **Mutants generated:** 8
- **Killed (good):** 8
- **Survived (bad):** 0
- **Survival rate:** 0.0%

## Surviving mutations of note

*(None.)*

## Coverage gaps

*(None.)*
