# Final Engineering Summary

## 1. What was built

A URL shortener: FastAPI, PostgreSQL 16, Alembic migrations, psycopg 3 with a connection pool, and a single-page create form. It runs as one Uvicorn worker, with one-service Docker Compose for PostgreSQL.

Routes: `POST /api/links`, `GET /` and `POST /` (form), `GET /livez`, `GET /readyz`, and the redirect `GET /{code}`, which stays last in route order.

Beyond the original brief, the work included two scenarios:
- **Brownfield (Task 15):** best-effort click analytics (`link_clicks` table, one insert per successful redirect).
- **Ambiguous requirement (Task 16):** "Make the service more reliable" narrowed to liveness and readiness endpoints.

## 2. Requirement status

| Requirement | Status | Evidence |
|---|---|---|
| FR-1 to FR-12 (create, redirect, validation, expiry, form, 302/404, public base URL) | Met | Unit, integration, and end-to-end tests against PostgreSQL |
| FR-13 (best-effort click recording) | Met | Integration tests for success, 404, expired, and write-failure paths; redirect stays 302 |
| FR-14 (`/livez`, `/readyz`) | Met | Tests for no-DB liveness, healthy readiness, and missing-table and failure 503 |
| NFR-1 (10 creates/min/IP, in-process, 429) | Met | Tests; limitation L-1 applies |
| NFR-2 (parameterized queries, env secrets, no full URLs in logs) | Met | Code review, tests asserting log contents, Bandit |
| NFR-3 (redirect p99 < 200 ms at about 100 req/s) | **Not met on this machine** | See section 3 |
| NFR-4 (create p99 < 300 ms at about 1 req/s) | Reported, weak evidence | The create profile is capped by NFR-1 at about 36 requests per run, so its p99 is statistically weak |
| NFR-5 (10M links, fast lookup by code) | Partly verified | Measured at 1M rows. The 10M figure is an extrapolation from index and overhead projections, not a measurement |
| NFR-6 (PostgreSQL only, one-service Compose, no Redis) | Met | Compose file, architecture doc |
| NFR-7 (uniqueness by SHA-256 digest index) | Met | Schema and integration tests |
| NFR-8 (analytics must not push redirect p99 above 77.8 ms) | Met | After-run p99 58.2 ms, 750/750 returned 302, 0 errors, not generator-limited |

## 3. Performance: what was measured

Everything below was measured by me, not by the AI, on one Windows-on-ARM development machine with 1 worker, pool 10 (250 ms wait), and a 1M-row benchmark database.

- **Baseline (25 req/s for 30 s):** p99 51.8 ms. This is the official baseline for D-56 and D-57.
- **After click analytics (same profile):** p50 +6.8 ms, p99 +6.4 ms (p99 58.2 ms). That meets the D-57 threshold of 77.8 ms.
- **Saturation:** at 99 to 100 req/s p99 rose to 1,495 ms and 2,328 ms, so capacity on this machine is about 90 req/s.
- **NFR-3 conclusion:** the 100 req/s target is not met here. I did not tune it, because the brief said to revisit caching only if the target is missed and to avoid it initially. The next steps would be more workers, a larger pool, and a cache, measured in that order.
- **Not measured:** behavior at 90 to 100 req/s with click analytics enabled, the 1,000 req/s peak, and a real 10M-row dataset.

## 4. Security and robustness

- Input validation: HTTP and HTTPS only, 2,048-character limit, no whitespace or control characters, no embedded credentials, self-reference rejection, and rejection of literal private, loopback, link-local, and unspecified IPs (including IPv4-mapped IPv6).
- Parameterized SQL everywhere. Secrets come from environment variables.
- Errors are sanitized: a database failure returns a standard 503 and never exposes details. Logs carry short codes and exception class names, never full URLs.
- The create limiter ignores proxy headers (`--no-proxy-headers`), so clients cannot spoof their address.
- Quality gates: Ruff lint and format, mypy strict, Bandit, and pip-audit, plus an OpenAPI drift test.
- Final test run: **253 passed, 0 skipped**, with `TEST_DATABASE_URL` set and `REQUIRE_DB=1`.

## 5. Known limitations

- L-1: the rate limiter lives in process memory, so it resets on restart and covers one process.
- L-2: hostnames are not resolved, so a hostname that points at a private address can bypass the check.
- L-3: expired links are kept indefinitely.
- L-4: resubmitting an expiring URL with no expiry makes it permanent (per A-2).
- L-5: alternate IPv4 spellings (decimal, hex, octal) are not normalized.
- L-6: behind a proxy, all clients share one rate-limit bucket.
- L-7: click events are best-effort. They can be lost, have no deduplication or bot filtering, and the table grows without a retention policy. A slow or failing click write can delay the 302 by up to the pool wait (250 ms) or statement timeout (1 s).
- `/readyz` proves only that `links` is readable. It does not check `link_clicks` or the migration version.
- `link_clicks` has no foreign key, because the seeder truncates `links`.

## 6. AI-assisted process

The AI (Copilot) drafted code, tests, and docs. I reviewed every output, decided scope and trade-offs myself, and ran every performance measurement myself. The full record is `docs/AI_usage_log.md`: decisions D-1 to D-65, errors E-1 to E-60, stage entries, and sign-offs.

What the review caught, with examples:
- **Unverified claims:** the AI repeatedly could not run database tests, so its reports showed skips. I ran them against PostgreSQL every time (E-12, E-26, E-28, E-39).
- **Task 15 errors:** a concatenated migration file, a duplicate function, and a weak missing-table test. The after-run was invalid until I migrated the bench database, because every click write had failed. I reran it, and that run is the one reported.
- **Task 16 errors:** a vague slice in the AI's memo, a wrong `# pragma: no cover`, and an unreachable 503 documented on `/livez` (a flaw in my own prompt). All were fixed before sign-off.

Where I overruled or narrowed the AI: the choice of `/livez` and `/readyz` over `/healthz` (a 7-letter path is a valid short code), the decision to keep click writes synchronous and best-effort, and the decision not to tune around the NFR-3 miss.

## 7. What I would do next

1. Re-measure NFR-3 on production-like hardware and with multiple workers.
2. Seed 10M rows and measure NFR-5 directly.
3. Run a longer create-load profile with several source addresses, so the create p99 means something.
4. Add retention and deduplication for click events, or move recording off the request path.
5. Extend `/readyz` to check the migration version and `link_clicks`.
6. Add an OpenAPI test for the health-endpoint responses.