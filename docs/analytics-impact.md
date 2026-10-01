# Click Analytics Impact Analysis

**Phase A only:** analysis and proposed decisions. No code, test, schema, or requirement has been changed. Task 15 requires explicit engineer approval before implementation.

## Current State

The redirect handler is `redirect_link()` in `url_shortener/app.py`. It first checks the path value against `^[A-Za-z0-9]{7}$`; malformed codes return the fixed JSON 404 without touching PostgreSQL. It captures one UTC `now`, calls `get_link_by_code()` from `url_shortener/links.py` in `run_in_threadpool`, and returns 404 for a missing row or when `expires_at <= now`. A `DatabaseAccessError` from lookup is handled as sanitized 503, not 404. For a found, unexpired row, it calls `record_click(link)` immediately before constructing `RedirectResponse(..., status_code=302)`. `record_click()` is currently a no-op in `url_shortener/app.py`.

`tests/test_redirects.py::test_click_hook_runs_only_after_active_lookup` pins one hook call for an active code and zero for missing, expired, and malformed codes. `test_expiry_boundary_uses_one_request_timestamp` pins expiration at `expires_at == now`. `test_database_failures_return_sanitized_503` pins lookup failures to 503. PostgreSQL-backed create/redirect, expiry, and retention coverage is in `tests/test_database_integration.py`; there is not yet a test of a persisted analytics event.

There is no click analytics requirement in `requirements.md`; its Out of scope section says analytics are excluded and the no-op hook is retained for later. Task 15 is explicitly high-impact and approval-gated. This phase does not alter that status.

## Impact Map

| Surface | Likely impact after approval | Not expected to change |
|---|---|---|
| `url_shortener/app.py` | Replace the no-op hook call with one best-effort event write after successful lookup/expiry validation and before building the 302. Catch only the typed analytics-write failure locally so redirect behavior remains unchanged. | Route paths, request/response schemas, code validation, lookup, expiry semantics, create flow, and handler registrations. |
| `url_shortener/links.py` | Add a parameterized insert operation for one click event, using the existing `Database.connection()` transaction boundary. | Existing lookup and create/upsert functions. |
| `alembic/versions/` and schema | Add a new migration that creates the event table and its key/indexes; downgrade drops only that new table/indexes. Existing `links` table and indexes stay unchanged. | Initial migration and short-link schema. |
| `url_shortener/database.py` | Expected to remain unchanged: its existing pool and typed `DatabaseAccessError` classes can support the write. Confirm during implementation if additional failure classification is needed. | Pool defaults and settings. |
| `tests/test_redirects.py` | Extend hook tests for exactly one event-write attempt on active links, zero on all 404 paths, and 302 when an injected analytics write raises a typed database error. | Existing redirect semantics assertions. |
| `tests/test_database_integration.py` | Verify event row fields/count and migration upgrade/downgrade on the disposable PostgreSQL fixture. | Existing tests except any shared schema assumptions requiring adjustment. |
| `tests/test_database_unit.py` | Optional typed-write failure test if the insert helper needs isolated DB adapter coverage. | Existing pool/connection tests otherwise. |
| `README.md` | Describe event contents, best-effort failure semantics, retention decision, migration, and the before/after latency procedure. | Existing setup commands unless schema workflow needs clarification. |
| `docs/architecture.md` | Replace the “no-op click-analytics hook” description with the approved persistence boundary and failure semantics. | Other component/flow descriptions. |
| `docs/design.md`, `docs/requirements.md` | Engineer-approved changes only: add the proposed requirement text below and update the Out of scope entry. | No silent edits; these documents are currently consistent that analytics are out of scope. |
| `docs/openapi.json`, `tests/test_openapi.py` | No expected change: no new public path or response is proposed. | OpenAPI contract. |
| `scripts/seed_links.py` | No expected code change. The new event table is empty after seeding; the first redirects populate it. | Seeded links and sample-code generation. |
| `scripts/load_test.py` | No expected code change; use its existing redirect-average profile for regression measurement. | Harness scheduling, statuses, and reports. |
| `docker-compose.yml`, `.github/workflows/quality.yml`, `url_shortener/settings.py`, `url_shortener/expiry.py`, `url_shortener/validation.py`, `url_shortener/rate_limiter.py` | No expected change. | PostgreSQL service, CI, settings, URL/expiry validation, and limiter behavior. |

## Options

Traffic reference: 100 redirects/second is 8,640,000 events/day and 315,360,000 events/year. Storage and latency below are **planning estimates, not measurements**. Table estimates assume ordinary PostgreSQL tuple/page overhead and a primary-key index; WAL, replicas, backups, free space, vacuum bloat, and retention overhead are additional.

| Option | Schema/migration on 1M–10M `links` | Writes, hot-code behavior, pool | Slow/failing write and crash durability | Approximate DB growth at 100 redirects/s | Testability |
|---|---|---|---|---|---|
| **a. `click_count` on `links`, synchronous update** | `ALTER TABLE links ADD COLUMN click_count BIGINT NOT NULL DEFAULT 0`. PostgreSQL 16 can add a constant default without rewriting every tuple, but the DDL still needs a table lock and may wait behind long transactions. Backfill is not required. | One `UPDATE` and WAL record per redirect. A popular code serializes on the same row lock, producing a hot tuple, WAL pressure, and MVCC dead tuples. One extra pool checkout per redirect; default pool is max 10 with 250 ms wait. | Catch typed analytics errors and continue to 302; a slow update still delays response. A committed count is durable, but no timestamp/event exists; a process crash before commit loses that increment. | Fixed 8-byte value per link: roughly 8 MB at 1M or 80 MB at 10M, plus tuple alignment and update bloat; no per-click row growth. | Easy count assertion and failure injection; cannot prove event timestamps or exactly one event record. |
| **b. Append-only `link_clicks`, synchronous insert** | Create a separate table with exactly `code` (FK to `links(code)`) and UTC `clicked_at`; use a non-unique lookup index if required. No rewrite/backfill of the large `links` table. New-table/index/FK DDL still takes short locks and migration must be coordinated. | One insert/commit per eligible redirect, plus index maintenance and FK check. Popular-code inserts do not update one `links` row, though indexes/heap pages still contend. One extra pool checkout and transaction per redirect. | Catch typed insert errors and preserve the 302; a slow insert delays it. A committed event survives process crash. If DB is unavailable or insert fails, that redirect has no event. | Roughly 64–100 bytes/event including heap and lookup index: about 20–32 GB/year at 100/s, before WAL/backups/bloat. | Best fit for exact event rows; integration can assert one row with code/time and simulate a write failure. |
| **c. Append-only table written as a background task** | Same table/migration and long-term growth as option b. | Same insert/index work, but scheduled after response transmission. It still uses the same worker and DB pool; 100 background inserts/second can consume pool slots and compete with lookup traffic. A popular code does not cause a parent-row update. | The response can already be 302 before the task fails, but slow tasks can occupy worker resources. Crash before commit loses queued/in-flight events; process-local background work is not a durable queue. | Same rough 20–32 GB/year as option b. | Can test response-before-failure with a task hook, but background execution order/failure tests are more complex; it still cannot guarantee delivery on crash. |
| **d. In-process counter flushed periodically** | Needs a `click_count` column or aggregate table, plus flush code; a count column has option-a migration risks. | One in-memory increment per redirect; hot code needs a lock/atomic update in the process. Periodic batch writes reduce DB operations, but flushes contend on popular rows and need retry/dirty-state logic. It uses pool slots in bursts rather than once per request. | Redirect latency is lowest until flush. A failed flush must retain pending deltas; a process crash loses unflushed counts. Multiple workers would diverge, though the current design runs one process. | Fixed count storage like option a, roughly 8–80 MB for 1M–10M links plus bloat; no event history. In-memory state may approach hundreds of MB or more if most of 10M codes become active; estimate depends on Python object overhead. | Requires deterministic clock/flush tests, concurrent update tests, and crash/flush failure reasoning; cannot verify timestamped events. |

For a two-column append table, 64–100 bytes/row is only a rough sizing range. At 315.36M events/year, that is approximately 20–32 GB/year; measure actual relation/index growth before setting retention or partitioning policy.

## Privacy and Logging

Proposed event fields are the short code and a UTC `clicked_at` timestamp only. Do not store client IP, user agent, referrer, query string, or full URL. The short code is a lookup key and can be resolved to a URL by someone with database access, so treat the event table as sensitive operational data despite the minimal fields. Do not log the code, URL, request headers, connection string, password, or event payload. A write-failure log may contain only a fixed message and exception class/counter, never exception text or request data. NFR-2’s existing no-full-URL-in-logs rule continues to apply.

No retention period is currently specified. An append-only table therefore grows indefinitely unless a retention/partition-drop policy is approved and implemented. At the estimate above, this is a multi-tens-of-GB/year decision, not a negligible side table.

## Failure Behavior

Preserve the existing response contract: a found, unexpired code remains HTTP 302 even if analytics persistence is slow or fails; malformed, missing, and expired codes remain 404 and do not attempt an analytics write; a lookup database outage remains the existing sanitized 503. Analytics failures must be caught after lookup and must never be translated into 404 or replace the redirect with 503.

- **Option a:** catch update failure and send 302; a successful commit persists an aggregate increment. A failed update loses that click.
- **Option b:** catch insert failure and send 302; a successful commit stores one row. A failed insert stores no event.
- **Option c:** send 302 before the background insert completes; task failure/crash loses the event and must not affect the emitted response.
- **Option d:** send 302 after an in-memory increment; a later failed flush keeps the delta for retry, while a process crash loses pending deltas.

There is an unavoidable semantic tension: “exactly one recorded event for every successful redirect” cannot be guaranteed through a database outage while also promising that analytics failures never change the response. Proposed interpretation: make exactly one write attempt per eligible request, commit exactly one event when that write succeeds, never retry an ambiguous commit, and preserve the 302 if it fails. Engineer approval is needed for this best-effort boundary.

## Expected Redirect Overhead

Estimates only; none has been measured. Option a adds one synchronous update round trip and row-lock/WAL work; its likely tail cost is dominated by database latency and hot-row wait. Option b adds one synchronous insert transaction, primary-key maintenance, and FK check; expect one extra DB round trip, typically sub-ms to several-ms on a local lightly loaded database, with contention and pool wait dominating under load. Option c adds no wait to response completion by design, but background work still competes for the same worker/pool and can increase later request latency. Option d adds an in-process increment (normally microseconds or less) and amortizes DB cost across flushes, at the cost of loss risk and bursty flush contention. These are directional estimates only; use the regression plan below to measure.

## Recommendation

After explicit approval, use **option b**, a minimal append-only `link_clicks` table with only `code` and `clicked_at TIMESTAMPTZ`, and one synchronous insert attempt in the current `record_click` hook after lookup and expiry validation. Do not add IP, user agent, referrer, a public endpoint, or a dashboard. Make one insert attempt per eligible redirect and locally catch the typed analytics write error so the existing 302 is preserved. There is no retry after an ambiguous commit. Add only the index needed for an approved event lookup/retention policy; any extra index or partitioning needs measured evidence and a retention decision.

This is the smallest option that records individual click timestamps and avoids serializing all clicks for one popular code on an update to the same `links` row. It costs one extra synchronous database write and has substantial retention growth. If latency misses the agreed regression bound, cut the optional code/time index, extra analytics reporting, and any dashboard first; do not silently move writes to a background task or add a cache. Exact delivery during DB failure remains impossible under the response-preservation rule and needs approval.

## Proposed Requirement Text

These are proposals only; they have not been added to `requirements.md`.

- **Proposed FR-13:** For each redirect lookup that finds an unexpired short link, attempt to persist one click event containing only its short code and a UTC timestamp. Do not attempt an event for malformed, nonexistent, or expired codes. Add no public analytics endpoint or dashboard.
- **Proposed NFR-8:** Analytics persistence is best-effort and must not change redirect status: a successful, unexpired lookup still returns HTTP 302 if the analytics write fails. Analytics failures are reported without logging URLs, codes, personal data, or connection details.
- **Proposed Out of scope replacement:** “Click-event persistence is included after engineer approval. Aggregations, dashboards, client identifiers, referrers, and public analytics endpoints remain out of scope.”
- **Design change needed:** update the Redirect Control Flow hook from a no-op to a synchronous best-effort insert; describe the new table, timestamp source, typed failure handling, and retention/rollback. Update `architecture.md` after implementation to show the event-store boundary and failure behavior.

## Test Plan

- Unit test: active link invokes the event writer exactly once after lookup and expiry check; missing, expired, and malformed links invoke it zero times.
- Unit/failure-injection test: event writer raises each typed database error; route still returns 302 with the original `Location`, and the safe error log contains no URL, code, or exception text.
- PostgreSQL integration test: one eligible redirect produces exactly one row with matching short code and timezone-aware UTC timestamp; a second redirect produces one additional event. Verify no rows for 404/expired/malformed paths and no client IP, user agent, referrer, or URL columns in the migration.
- Migration test: upgrade from the current head, verify table/index/FK, then downgrade to the current revision and verify only the analytics schema is removed. Run this only against the disposable integration database.
- Keep existing `tests/test_redirects.py` assertions for hook placement and response behavior. Run the complete suite and `python scripts/check.py`.

## Latency Regression Plan

Use the same machine, running benchmark DB, 1,000,000-row dataset, sample-code file, pool settings, and command for baseline and after. The engineer runs these commands; the AI cannot run the load test. Do not change code or cache if a result misses—the miss blocks acceptance and requires investigation/review.

In the server window for both runs, set the benchmark `DATABASE_URL`, `PUBLIC_BASE_URL=http://127.0.0.1:8000`, `DB_POOL_MAX_SIZE=10`, and `DB_POOL_WAIT_MS=250`; start one Uvicorn worker with `--no-proxy-headers`. First, on the current code, use a second window:

```powershell
python -m scripts.load_test --profile redirect-average --sample-codes-file sample-codes.txt --dataset-rows 1000000 --pool-max 10 --pool-wait-ms 250
```

Keep that JSON/Markdown result as the baseline. Apply the approved analytics implementation and migration without reseeding or changing the dataset, restart the server with identical settings, then run:

```powershell
python -m scripts.load_test --profile redirect-average --sample-codes-file sample-codes.txt --dataset-rows 1000000 --pool-max 10 --pool-wait-ms 250
```

**Pass condition:** after-run p99 is below 200 ms, every response is 302, the generator is not limited, and after p99 is no more than 10% above baseline p99. If baseline is zero/invalid, or any status, generator, target, or relative condition fails, report it as not met; rerun only to investigate reproducibility, then block acceptance pending engineer review. The redirect-average profile is the NFR-3 pass/fail profile; do not substitute peak or overload results.

## Risks, Rollback, and Engineer Decisions

- **Storage/retention:** the event table can grow by roughly 20–32 GB/year at 100 redirects/second under the stated estimate. Choose retention, partitioning, archival, and deletion authority before production use; without a policy, expect unbounded growth.
- **Latency/pool:** one sync write per successful redirect consumes another connection checkout from a default pool of 10 with a 250 ms acquisition wait. Popular codes avoid a hot `links` row with append-only writes, but indexes, table pages, pool slots, and WAL can still contend.
- **Semantics:** decide whether “exactly one” means one insert attempt per eligible request or one durable row despite DB failure. The latter conflicts with preserving a 302 during write failure without a durable queue/outbox mechanism.
- **Schema rollback:** stop the new writer first, then run `python -m alembic downgrade 20260929_0001` to downgrade the proposed analytics migration to the current links-schema revision. This drops the analytics table and permanently deletes collected events; take/export any required data before downgrade. Do not downgrade the existing links migration.
- **Privacy:** approve the short-code-plus-timestamp data classification and retention period; ensure operational logs omit codes and exception text.
- **Scope approval:** approve synchronous best-effort writes, the event schema/indexes, failure semantics, and the proposed FR/NFR text before implementation. The Task 15 high-impact gate remains open until the engineer approves these decisions and the before/after latency check passes.

Decisions needed from the engineer:

1. Approve option b and best-effort semantics, including losing an event when persistence fails.
2. Set the retention period and decide whether an index or partitioning is justified initially.
3. Approve the proposed FR-13/NFR-8 text and replacement Out of scope wording.
4. Approve the proposed regression threshold (p99 < 200 ms, all 302, generator not limited, and at most 10% above baseline).