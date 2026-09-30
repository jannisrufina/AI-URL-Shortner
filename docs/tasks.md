# Implementation Tasks

Tasks are ordered by dependency. Each task includes acceptance criteria, focused tests where it implements behavior, and requirement IDs. Every task is done only when all four quality-gate categories pass: Ruff lint and formatting (`ruff check`, `ruff format --check`), mypy type checking, Bandit code security scan, and `pip-audit` dependency vulnerability audit. **Must-have** work is required for the prototype; **Can shrink/defer** work may be reduced or scheduled later if time is short. **High-impact** marks work that requires explicit sign-off before proceeding or relying on its result.

## 1. Establish quality gates

**Priority:** Must-have.

**Work:** Add reproducible project commands/configuration for Ruff lint and formatter, mypy, Bandit, and pip-audit. Make the same commands runnable locally and in CI.

**Dependencies:** None.

**Acceptance criteria:**
- All four gates run from documented commands and return actionable failures.
- CI runs all four gates on changes.
- All four quality gates pass.

**Requirements:** N/A (quality-process task; it does not directly implement a product requirement).

## 2. Produce architecture overview

**Priority:** Must-have.

**Work:** Create `docs/architecture.md` with a compact component overview and create/redirect flow diagram, consistent with `docs/design.md`.

**Dependencies:** Task 1.

**Acceptance criteria:**
- The overview identifies FastAPI, Jinja, PostgreSQL, and the one-service Compose setup.
- Create and redirect flows show the database boundary, validation, rate limit, expiry check, and click-analytics hook.
- The overview introduces no behavior beyond approved design/requirements.
- All four quality gates pass.

**Requirements:** FR-1, FR-2, FR-10, NFR-6.

## 3. Bootstrap app, schema, and bounded database access

**Priority:** Must-have.

**Work:** Set up Python/FastAPI/Jinja, PostgreSQL driver, Alembic, tests, and one-service Compose. Add the initial `links` migration and configure the bounded connection pool and timeouts from `design.md`.

**Dependencies:** Tasks 1 and 2.

**Acceptance criteria:**
- Compose starts PostgreSQL only and reports healthy before migrations.
- App reads database settings and `PUBLIC_BASE_URL` from environment variables; secrets are not committed; app runs as one process.
- `alembic upgrade head` initializes the schema and is repeatable; app startup and requests do not silently migrate schema.
- Pool maximum and pool/connect/statement timeouts are bounded and configurable; database timeout errors are distinguishable from code collisions.
- Schema setup creates no automatic expiry cleanup; mappings remain stored after expiry.
- Passing foundation tests verify migration, schema constraints/indexes, health ordering, and bounded database configuration.
- All four quality gates pass.

**Requirements:** FR-1, FR-2, FR-11, NFR-1, NFR-2, NFR-5, NFR-6, NFR-7, A-1, A-4, L-1, L-3.

**Latency reconciliation:** Retain the design's 250 ms pool-acquisition maximum as an overload/failure bound, not an expected redirect wait. The 200 ms redirect p99 remains an end-to-end acceptance target under documented normal operating load, including normal pool acquisition. Measure request latency with pool wait included; test pool exhaustion separately as a bounded `503` failure path. If normal-load p99 exceeds 200 ms, report the miss and seek approval for a design/configuration change; do not waive the target. **High-impact:** sign-off is required on this interpretation because an actual 250 ms pool wait can exceed the redirect target.

## 4. Implement URL validation and public-host policy

**Priority:** Must-have.

**Work:** Implement HTTP(S) syntax and length checks, raw whitespace/control-character rejection, credential rejection, self-reference checks derived from parsed `PUBLIC_BASE_URL`, and literal IP/local-host checks. Do not perform DNS lookups.

**Dependencies:** Tasks 1 and 3.

**Acceptance criteria:**
- Passing focused unit tests cover invalid schemes, malformed URLs/ports, length over 2,048 characters, whitespace/control characters, and credentials; rejection occurs before database access.
- Tests cover the configured-base hostname and dot-delimited subdomains case-insensitively, for arbitrary paths and ports.
- Tests cover private, loopback, link-local, unspecified (`0.0.0.0`), IPv4-mapped IPv6, and `localhost` rejection.
- No DNS resolution occurs.
- Characterization tests cover decimal (`2130706433`), hexadecimal (`0x7f000001`), and octal-like (`0177.0.0.1`) IPv4 forms and document that they are not normalized.
- All four quality gates pass.

**Requirements:** FR-3, FR-4, FR-5, FR-6, FR-11, L-2, L-5.

**High-impact:** Review the alternate-IP-notation limitation before production use; these forms are not normalized and may bypass literal-IP classification.

## 5. Implement the in-process create limiter

**Priority:** Must-have.

**Work:** Add a thread-safe per-direct-client-IP sliding-window limiter and cleanup of expired timestamps and inactive IP buckets.

**Dependencies:** Tasks 1 and 3.

**Acceptance criteria:**
- Passing focused limiter tests show at most 10 admitted create requests per IP per rolling 60-second window and `429` for the next request.
- Tests verify direct peer IP use, ignored forwarded headers, timestamp cleanup, and inactive-bucket cleanup.
- Behavior is documented as single-process and reset-on-restart.
- All four quality gates pass.

**Requirements:** NFR-1, L-1, L-6.

**High-impact:** Sign off before deployment behind a proxy without trusted client-IP handling; users may share the proxy's rate-limit bucket.

## 6. Implement create persistence and JSON API

**Priority:** Must-have.

**Work:** Add `POST /api/links`. Use exact submitted URL bytes for SHA-256, seven-character random Base62 codes, and one parameterized `INSERT ... ON CONFLICT (url_digest) DO UPDATE ... RETURNING` statement. Preserve the existing code, apply expiry rules, retry only on code uniqueness collisions, and return `200` for both new and reused mappings.

**Dependencies:** Tasks 3, 4, and 5.

**Acceptance criteria:**
- Passing focused persistence tests cover stable codes for exact repeats, differing digests for different strings, concurrent same-URL creates, and code-collision retries.
- Passing expiry tests cover the 365-day bound, both repeat-create rules, and revival of expired mappings.
- Passing API tests verify new/reused creates return `200`, invalid input returns `422`, and database failures return `503`.
- Short URLs use `PUBLIC_BASE_URL`; SQL is parameterized and full URLs are absent from logs.
- All four quality gates pass.

**Requirements:** FR-1, FR-3, FR-4, FR-5, FR-6, FR-7, FR-11, FR-12, NFR-2, NFR-4, NFR-7, A-1, A-2, A-3, L-4.

## 7. Implement redirects and failure handling

**Priority:** Must-have.

**Work:** Add `GET /{code}` with indexed primary-key lookup, expiry enforcement, and the future click-analytics hook. Map database unavailability, pool exhaustion, and timeouts to sanitized errors.

**Dependencies:** Tasks 3 and 4.

**Acceptance criteria:**
- Passing focused redirect tests verify `302` and exact `Location` for active mappings; missing, malformed, and expired codes return `404`.
- Tests verify expired rows remain stored and the analytics hook is after lookup/expiry validation but before response creation; analytics is not implemented.
- Failure tests verify pool exhaustion, connection timeout, statement timeout, and database unavailability return `503`, without being misclassified as code collisions.
- No cache is used.
- All four quality gates pass.

**Requirements:** FR-2, FR-8, FR-9, NFR-2, NFR-3, NFR-5, NFR-6, L-3.

## 8. Implement the single-page create form

**Priority:** Must-have.

**Work:** Add `GET /` and `POST /` using the create service from Task 6.

**Dependencies:** Tasks 3, 4, 5, and 6.

**Acceptance criteria:**
- Passing focused UI tests verify the form loads, submits a URL and optional expiry, and displays the resulting short URL.
- User-provided and generated values are escaped; validation errors render safely.
- Successful create uses the same persistence, rate-limit, expiry, and URL-validation behavior as the JSON API.
- All four quality gates pass.

**Requirements:** FR-3, FR-4, FR-5, FR-6, FR-7, FR-10, FR-11, FR-12, NFR-1, NFR-2, A-2, L-4.

## 9. Add cross-cutting integration and end-to-end tests

**Priority:** Must-have.

**Work:** Add only cross-component integration and end-to-end coverage across migrations, JSON API, form, limiter, and redirects. Focused tests remain in Tasks 3–8.

**Dependencies:** Tasks 3 through 8.

**Acceptance criteria:**
- Passing integration tests cover create then redirect, repeat-create expiry changes, expired-link revival, PostgreSQL failures, and migrations against PostgreSQL.
- End-to-end create checks verify HTTP `200` for both newly created and reused links.
- HTTP-path checks cover URL validation failures, redirect `302` and missing/expired `404` behavior, and retained expired rows.
- Passing end-to-end tests exercise the browser form, escaped output, and short URLs built from the configured public base URL.
- Tests verify limiter behavior through the HTTP path, including that differing forwarded-IP headers do not split requests sharing the same direct peer IP into separate buckets.
- One documented command runs the suite against an isolated test database.
- All four quality gates pass.

**Requirements:** FR-1 through FR-12, NFR-1, NFR-7, A-1, A-2, A-3, L-3, L-4, L-6.

## 10. Add a performance data seeder

**Priority:** Must-have utility; full 10-million-row scale can shrink.

**Work:** Add a configurable deterministic utility that seeds synthetic links into a dedicated benchmark database, without using the rate-limited endpoint and without deleting data by default.

**Dependencies:** Task 3.

**Acceptance criteria:**
- Passing seeder tests cover a small smoke dataset, generated code/URL uniqueness, schema validity, and safe defaults.
- Utility supports configurable counts; the default/smoke case is quick, and 10-million-row population is an optional scale run.
- It uses efficient bulk loading and reports count/time without printing URLs; destructive operations require an explicit option.
- For a full-scale seed, compare measured storage use with the approximate 5 GB record-only estimate and report index/database overhead separately.
- All four quality gates pass.

**Requirements:** NFR-2, NFR-5, NFR-7, A-4, A-8.

**High-impact:** Obtain sign-off before seeding 10 million rows against a shared or valuable database; it can consume substantial disk and I/O.

## 11. Add and run performance/load tests

**Priority:** Must-have measurement; full-scale/peak runs can shrink.

**Work:** Add repeatable redirect/create load profiles and report dataset, machine, offered load, pool wait, status distribution, and server-side p99. Run without a cache.

**Dependencies:** Tasks 9 and 10.

**Acceptance criteria:**
- Passing smoke load tests validate the harness and report format.
- Benchmark includes database pool acquisition in complete redirect latency; normal-load redirect p99 must be below 200 ms and create p99 below 300 ms.
- Where practical, include a mixed workload at the expected 100:1 read-to-write ratio in A-5.
- The NFR-3 pass/fail measurement uses the average redirect load in A-6 (about 100 requests/second); run the A-6 peak load (about 1,000 requests/second) separately and report its results without using them as the NFR-3 pass/fail condition.
- Measure create p99 at the expected average create load in A-7.
- Pool exhaustion/timeout failures are measured separately and do not replace normal-load p99 results.
- Create load respects the per-IP limiter; use distinct direct client IPs or rate-compliant offered load, never spoof forwarded headers.
- If redirect p99 misses its target, report the miss and propose caching separately; do not implement an unapproved cache change.
- All four quality gates pass.

**Requirements:** NFR-1, NFR-3, NFR-4, NFR-5, A-4, A-5, A-6, A-7.

**High-impact:** Obtain sign-off before peak-rate or full-scale tests outside an isolated benchmark environment. A single direct client IP can create only 10 links per minute.

## 12. Reconcile architecture overview with built code

**Priority:** Must-have.

**Work:** After implementation and performance testing, update `docs/architecture.md` so its component boundaries and create/redirect flows describe the code as built. Record material deviations from `docs/design.md` for review; do not silently change the design or requirements.

**Dependencies:** Tasks 2 through 11.

**Acceptance criteria:**
- The overview matches the implemented app, persistence, validation, limiter, and redirect paths.
- Any discrepancy with `docs/design.md` is clearly identified for review; changes to design decisions or requirements remain approval-gated.
- All four quality gates pass.

**Requirements:** FR-1, FR-2, FR-10, NFR-1, NFR-6.

## 13. Export the OpenAPI specification

**Priority:** Can shrink to documenting the generated endpoint if time is short.

**Work:** Export FastAPI's OpenAPI document as a reproducible JSON artifact and document the regeneration command.

**Dependencies:** Tasks 6 through 8.

**Acceptance criteria:**
- Passing export validation confirms the artifact parses and matches routes, payloads, status codes (including `200` for both new and reused creates), and error schemas.
- Regeneration produces no unexplained diff.
- All four quality gates pass.

**Requirements:** FR-1, FR-2, FR-7, FR-8, FR-9, FR-10, FR-11, FR-12.

## 14. Complete setup instructions

**Priority:** Must-have.

**Work:** Document prerequisites; required environment variables; PostgreSQL Compose startup; schema migrations; running the app; running quality gates and tests; and using the performance seeder and load tests.

**Dependencies:** Tasks 1, 3, and 6–13.

**Acceptance criteria:**
- A clean-checkout walkthrough reaches a working create form and redirect.
- Instructions include prerequisite versions, environment-variable names/examples without secrets, Compose commands, migration commands, app run command, and test commands.
- Seeding and load-test instructions distinguish the quick smoke path from optional large/peak runs.
- Pool/timeouts, single-process limiter, proxy caveat, and URL-validation limitations are documented.
- Another person can follow the guide without undocumented local state; all four quality gates pass.

**Requirements:** FR-10, FR-11, NFR-1, NFR-2, NFR-6, L-1, L-2, L-3, L-4, L-5, L-6.

## 15. Brownfield scenario: click analytics

**Priority:** Must-have scenario; use the smallest viable implementation and retain the explicit approval gate.

**Work:** Analyze impact, then implement the smallest viable click-analytics slice at the existing post-lookup/post-expiry hook (one recorded event per successful, unexpired redirect; no analytics dashboard), and run a redirect-latency regression check.

**Dependencies:** Tasks 7, 9, 11, 12, and 14; explicit approval to begin this out-of-scope enhancement.

**Acceptance criteria:**
- Impact analysis covers storage/write-path changes, privacy/logging, failure behavior, and expected redirect overhead before implementation.
- Implementation proceeds only after impact and scope are approved; analytics apply only to found, unexpired links.
- Passing focused tests verify hook placement and behavior without changing redirect semantics.
- Before/after load tests measure redirect p99 against the 200 ms target; any regression beyond target blocks acceptance pending review.
- All four quality gates pass.

**Requirements:** FR-2, FR-8 (redirect behavior that analytics must preserve); click analytics themselves are out of scope for the initial build and have no current requirement ID.

**High-impact:** Requires explicit approval because this later feature affects privacy, storage, and latency.

## 16. Ambiguous-requirement scenario

**Priority:** Must-have scenario; use the smallest viable slice and retain the explicit approval gate.

**Work:** Start with the vague proposal “Make the service more reliable.” The engineer supplies a concrete clarification and explicit assumptions; no external stakeholder is needed to provide them. After the clarification and assumptions are approved, implement only the smallest viable slice within that scope.

**Dependencies:** Tasks 6–9 and 12–14; engineer-provided clarification and assumptions plus approval before implementation.

**Acceptance criteria:**
- The vague requirement, engineer-provided clarification, assumptions, approval, and agreed minimal scope are documented.
- No new requirement is added to `requirements.md` without explicit approval.
- The approved slice is minimal, has focused passing tests, and does not expand beyond the approved clarification.
- All four quality gates pass.

**Requirements:** N/A until the clarified slice is approved and assigned requirement IDs.

**High-impact:** Requires approval of the engineer's clarification and assumptions before implementation because this scenario is outside current requirements.

## 17. Prepare the final engineering summary

**Priority:** Must-have.

**Work:** Produce a concise completion report with implemented scope, requirement traceability, quality/test results, performance measurements, setup guidance, limitations, deviations, and deferred high-impact decisions.

**Dependencies:** Tasks 1–16.

**Acceptance criteria:**
- Summary distinguishes verified outcomes from targets not met or not measured.
- Each requirement ID is marked implemented, tested, deferred, or not applicable with rationale.
- High-impact approvals and remaining risks are recorded in the summary.
- All four quality gates pass.

**Requirements:** All applicable FR, NFR, A, and L IDs in `requirements.md`.
