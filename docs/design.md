# URL Shortener Design

This document describes the initial prototype. Requirement references use the IDs in [requirements.md](requirements.md). Implementation tasks are intentionally not included.

## Components and Stack

- **FastAPI application:** serves the JSON API, HTML form, validation, rate limiting, redirect flow, and health endpoints. **[FR-2, FR-3, FR-10, FR-14, NFR-1]**
- **Jinja templates:** render the single-page create form and its result. Keep autoescaping enabled and do not mark user-controlled values as safe. **[FR-10]**
- **PostgreSQL:** source of truth for mappings. A primary-key index supports short-code lookup; a unique digest index provides same-URL reuse and concurrent-create arbitration. **[FR-1, FR-2, NFR-5, NFR-7]**
- **One-service Docker Compose:** starts PostgreSQL only. Run the Python application as a single local process. **[NFR-1, NFR-6, L-1]**
- **No cache initially:** all redirects query PostgreSQL. Benchmark first and revisit caching only if the redirect target is missed. **[NFR-3]**

Use Python, FastAPI, Jinja, PostgreSQL, and a PostgreSQL driver such as psycopg 3. Configure the database connection and public short-URL base through environment variables. Parse the configured public base URL once at startup and use its hostname as the self-reference root; under the current configuration this hostname is `jrb.sh`. Do not trust forwarded client-IP headers. **[FR-5, FR-11, NFR-1, NFR-2, NFR-6]**

## Data Model

Each row represents one exact URL string and its stable short code. The URL is retained for redirect responses; its SHA-256 digest is the URL identity key. By decision, the application does not compare the full URL after a digest match. **[FR-1, FR-2, A-1, NFR-7]**

```sql
CREATE TABLE links (
    code         VARCHAR(7) PRIMARY KEY
                 CHECK (code ~ '^[A-Za-z0-9]{7}$'),
    url_digest   BYTEA NOT NULL,
    original_url TEXT NOT NULL,
    expires_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL,
    CONSTRAINT links_url_digest_length CHECK (octet_length(url_digest) = 32)
);

CREATE UNIQUE INDEX links_url_digest_uq ON links (url_digest);

CREATE TABLE link_clicks (
    id         BIGSERIAL PRIMARY KEY,
    code       VARCHAR(7) NOT NULL,
    clicked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT link_clicks_code_format CHECK (code ~ '^[A-Za-z0-9]{7}$')
);

CREATE INDEX link_clicks_code_clicked_at_idx
    ON link_clicks (code, clicked_at);
```

The primary key creates a unique B-tree index on `code`; this is the redirect lookup index. The unique B-tree index on `url_digest` makes repeated creates conflict on the exact-URL digest. There is no expiry index initially: redirects first look up a code, then check that row's expiry. Expired rows remain in the table. **[FR-1, FR-2, FR-9, NFR-5, NFR-7, A-1, L-3]**

`link_clicks` is append-only and records only the seven-character short code and a `TIMESTAMPTZ` click time. It has no foreign key to `links`: the benchmark seeder's confirmed `--reset` truncates `links`, and a foreign key would block truncating that table alone. The reset explicitly truncates `link_clicks` first. There is no retention policy.

### Schema initialization

Manage schema changes as versioned Alembic migrations. The initial migration creates `links` and its constraints/indexes. A second migration, `20260930_0002`, adds `link_clicks`; `alembic upgrade head` applies both. Start the one-service PostgreSQL Compose configuration, wait for its health check, then run `alembic upgrade head` once before starting the application. Do not run migrations on every request or silently create/alter tables during app startup. Run the same migration before integration tests. **[NFR-6, NFR-7]**

Compute `url_digest` as SHA-256 over the exact UTF-8 bytes of the submitted URL, without URL normalization. Generate a candidate code with a cryptographically secure random source from the Base62 alphabet (`A-Z`, `a-z`, `0-9`). If the `code` primary key collides, generate another code and retry. Seven Base62 characters provide about 3.52 trillion possible codes; the database constraint and retry are still required. **[FR-1, A-1]**

### Expiry on repeated creates

For a new URL, store the requested expiry, or `NULL` if omitted. On a digest conflict, preserve the existing code and original URL and update expiry as follows:

```sql
expires_at = CASE
    WHEN links.expires_at IS NULL OR EXCLUDED.expires_at IS NULL THEN NULL
    ELSE EXCLUDED.expires_at
END
```

This implements the specified rule: if either the existing expiry or the new request has no expiry, the mapping becomes permanent; otherwise the new expiry replaces the old one. A resubmission of an expired URL uses the same code and can revive it. **[A-2, A-3, L-4]**

## API Contract

The paths and payload formats below are design choices because the requirements specify behavior but not routes or schemas. **[FR-2, FR-8, FR-9, FR-10]**

### Create JSON API

`POST /api/links`

Request:

```json
{
  "url": "https://example.com/page",
  "expires_at": "2026-10-01T12:00:00Z"
}
```

`expires_at` is optional; omit it or send `null` for no requested expiry. The endpoint returns HTTP `200` for both a new mapping and a reused mapping. This avoids trying to infer insert-versus-update from PostgreSQL's `INSERT ... ON CONFLICT ... RETURNING`; the returned row is the same either way. **[FR-12]**

Success response:

```json
{
  "code": "aZ19BxQ",
  "short_url": "https://jrb.sh/aZ19BxQ",
  "expires_at": null
}
```

### Create form

`GET /` serves the single-page HTML form. `POST /` accepts the form fields and renders the created/reused short URL on the page. The response is HTML and all displayed values are escaped. **[FR-10, FR-11]**

### Redirect

`GET /{code}` returns HTTP `302` with `Location: <original_url>` for a present, unexpired mapping. Missing and expired codes return HTTP `404`. **[FR-2, FR-8, FR-9]**

### Health checks

`GET /livez` returns HTTP `200` with `{"status": "ok"}` and never touches the database; it is used to confirm the app process is alive.

`GET /readyz` runs `SELECT 1 FROM links LIMIT 1` through the existing PostgreSQL pool and returns HTTP `200` with `{"status": "ok"}` when the query succeeds. If the query fails for any reason (pool wait, connection failure, statement timeout, or a missing `links` table), it returns the standard sanitized `503` error body used elsewhere. Any exception is treated as not ready (fail closed). The readiness path logs only the exception class name (`Readiness check failed (UndefinedTable)`, for example), never a URL, SQL, or connection detail. The endpoints are not rate limited, do not record click analytics, and are registered before the dynamic redirect route so they cannot be mistaken for short codes. **[FR-14, NFR-2, NFR-6]**

### Errors

JSON endpoints use this error shape; HTML form submissions render an escaped error message with the corresponding status.

```json
{
  "error": {
    "code": "invalid_url",
    "message": "URL must be a well-formed HTTP or HTTPS URL."
  }
}
```

Use `422` for invalid request data, `429` when the create limit is exceeded, `404` for missing/expired redirects, and `503` when PostgreSQL is unavailable. Error messages and logs must not include the full submitted URL. **[FR-3, FR-9, NFR-1, NFR-2]**

## Validation Rules and Responsibility

All create validation runs in the FastAPI application before the database write. The database enforces code and digest uniqueness and digest length. **[FR-1, FR-3, FR-4, FR-5, FR-6, FR-7, NFR-7]**

1. **Request and rate limit:** apply the per-IP limiter to each create request using the direct socket peer IP; do not read proxy headers. **[NFR-1]**
2. **URL text and shape:** reject raw whitespace and control characters anywhere in the submitted URL. Require a string no longer than 2,048 characters; parse it as an absolute URL with scheme `http` or `https` and a hostname. Reject malformed ports and embedded username/password credentials. **[FR-3, FR-4]**
3. **Self-reference:** parse the hostname from the configured public base URL at startup. Reject a destination whose parsed hostname equals that root or ends in `.` plus that root, case-insensitively, independent of port and path. This check is local and performs no database lookup. Under the current configuration, the root is `jrb.sh`. **[FR-5, FR-11]**
4. **Private/local hosts:** parse canonical literal IPv4 and IPv6 host values. Reject private, loopback, link-local, and unspecified addresses, including `0.0.0.0`. For IPv4-mapped IPv6 literals, classify the embedded IPv4 address too and reject it if it is private, loopback, link-local, or unspecified. Reject the obvious local hostname `localhost`. Do not resolve other hostnames. Legacy alternate IPv4 spellings (decimal integer, hexadecimal, or octal) are not normalized; their handling is a documented limitation and tested as such. **[FR-6, L-2, L-5]**
5. **Expiry:** if supplied, require a timezone-aware timestamp later than the request's creation timestamp and no more than 365 days after it. Store and compare timestamps as PostgreSQL `TIMESTAMPTZ`; a redirect is expired when `expires_at <= current time`. **[FR-7, FR-9]**
6. **Public URL construction:** form `short_url` from the configured public base URL environment variable and the returned code. **[FR-11]**
7. **HTML output:** rely on Jinja autoescaping for form values and results. Do not render submitted URLs through an unescaped/safe markup path. **[FR-10]**

No network request is made to the destination during validation. Full URLs are never written to application logs, and SQL values are always parameterized. **[NFR-2, FR-6]**

## Create Control Flow

1. Read the direct client IP and prune limiter timestamps older than 60 seconds. If 10 requests remain in the preceding minute, return `429`; otherwise record this create request. **[NFR-1, L-1]**
2. Parse and validate the request and URL as above. Capture one timezone-aware UTC timestamp for creation and expiry-window validation. **[FR-3, FR-4, FR-5, FR-6, FR-7]**
3. Compute SHA-256 over the exact URL bytes and generate a seven-character Base62 candidate code. **[FR-1, A-1]**
4. Execute one parameterized PostgreSQL statement: insert digest, candidate code, original URL, expiry, and creation time; use `ON CONFLICT (url_digest) DO UPDATE` with the expiry rule above; return the row's code, original URL, and expiry. No preliminary lookup or advisory lock is used. **[FR-1, A-2, A-3, NFR-2, NFR-7]**
5. If insertion fails because the candidate code violates its unique constraint, generate a new code and retry. Other database errors are not treated as code collisions. **[FR-1]**
6. Construct the short URL from the configured public base, return HTTP `200`, and render the same result in the form flow. The status does not distinguish insert from reuse. **[FR-10, FR-11, FR-12]**

The unique digest index arbitrates concurrent requests for the same exact URL. PostgreSQL applies the conflict update atomically, so both requests return the same persisted code. **[FR-1, NFR-7]**

## Redirect Control Flow

1. Validate the path code format; malformed or unknown codes produce `404`. **[FR-1, FR-9]**
2. Query PostgreSQL by the parameterized `code` primary key. If no row exists, return `404`. **[FR-2, FR-9, NFR-2, NFR-5]**
3. If `expires_at` is set and is less than or equal to the current UTC time, return `404`. **[FR-9]**
4. After a successful lookup and expiry check, attempt one synchronous insert into `link_clicks` through the database pool, dispatched with `run_in_threadpool`. Record only `code` and the database's UTC `now()` timestamp. Catch any analytics-write exception locally, log a fixed warning containing only the short code, and continue; analytics failure must not change the redirect response. **[FR-2, FR-8, NFR-2, FR-13]**
5. Return HTTP `302` with the stored original URL as `Location`. Do not add a cache initially. **[FR-2, FR-8, NFR-3]**

## Failure Behavior

- **PostgreSQL unavailable or slow:** use a bounded connection pool (initial default: minimum 1, maximum 10 connections), a bounded pool-acquisition wait (initial default: 250 ms), a connection timeout (initial default: 2 seconds), and a PostgreSQL statement timeout (initial default: 1 second). If the pool is exhausted, connection cannot be established before its timeout, or a statement exceeds its timeout, return `503` using the standard error format. Cancel/rollback the failed statement and do not retry it as a code collision. There is no cache fallback in the initial design. **[NFR-3, NFR-4, NFR-6]**
- **Same URL created concurrently:** the unique digest index and `ON CONFLICT` statement serialize the conflicting writes and return the existing code. **[FR-1, NFR-7]**
- **Random code collision:** the unique code constraint rejects the insert; retry with a newly generated code. If a bounded retry policy is exhausted, return `503` and log a sanitized operational error. **[FR-1, NFR-2]**
- **Rate limit reached:** return `429`; no database write is attempted. **[NFR-1]**
- **Expired mapping:** retain its row but return `404`. A valid repeated create updates expiry under the stated rule and revives that same code. **[FR-9, A-2, A-3, L-3]**
- **Click-event write failure:** log a sanitized warning containing the short code only and preserve the `302`; events may be lost. A failing or slow write can delay the `302` by up to the 250 ms pool wait or the 1 s statement timeout before the exception is caught. The lookup's existing typed pool/connection/statement failures still return sanitized `503` before the analytics hook. **[FR-2, FR-8, FR-13, NFR-2, NFR-8, L-7]**

## Rate Limiter

Use an in-process sliding window keyed by direct client IP, storing request timestamps in a deque per IP. Under a lock, remove timestamps at least 60 seconds old, reject when 10 timestamps remain, otherwise append the current timestamp. Prune inactive IP entries during requests so old state does not grow indefinitely. Run a single application process; the limiter resets on restart and does not coordinate across workers. **[NFR-1, L-1]**

## Test Plan

- **URL validation unit tests:** accepted HTTP/HTTPS forms; raw whitespace and control characters; invalid schemes, malformed values, overlength URLs, credentials, configured-base hostname and subdomains with varying case/ports/paths, canonical private/loopback/link-local/unspecified IPs including `0.0.0.0`, IPv4-mapped IPv6 literals, and `localhost`. Add characterization cases for decimal (`2130706433`), hexadecimal (`0x7f000001`), and octal-like (`0177.0.0.1`) IPv4 spellings and document that normalization/rejection is not guaranteed. Confirm no DNS lookup occurs. **[FR-3, FR-4, FR-5, FR-6, FR-11, L-2, L-5]**
- **Expiry unit tests:** missing expiry, future expiry, exact-now expiry, past expiry, 365-day boundary, beyond-boundary, repeated-create combinations, and revival of expired mappings. **[FR-7, FR-9, A-2, A-3, L-4]**
- **PostgreSQL integration tests:** schema constraints; create and repeat the same exact URL; digest equality; same code on reuse; expiry update semantics; concurrent same-URL creates; code-collision retry; parameterized lookup by code; retained expired rows. **[FR-1, FR-2, FR-9, NFR-2, NFR-5, NFR-7, A-1, A-2, A-3, L-3]**
- **API tests:** create success returns `200` for both new and reused mappings; validation errors return `422`; limiter returns `429`; redirects return `302`; expired/missing links return `404`; simulated database outage returns `503`. **[FR-8, FR-9, FR-12, NFR-1]**
- **UI tests:** form is available at `/`, successful result and validation errors render safely, and HTML escaping prevents submitted values from being interpreted as markup. **[FR-10]**
- **Limiter tests:** ten requests within a rolling minute are allowed and the next is rejected; old timestamps and inactive IP state are cleaned up; direct peer IP is used rather than forwarded headers. **[NFR-1, L-1, L-6]**
- **Compose smoke test:** start the one-service PostgreSQL Compose configuration, initialize the schema, run the application, and exercise create and redirect end to end. **[NFR-6]**
- **Database timeout/pool tests:** verify bounded pool configuration, pool-acquisition timeout, connection timeout handling, statement timeout cancellation/rollback, and sanitized `503` responses. **[NFR-2, NFR-3, NFR-4, NFR-6]**
- **Click analytics tests:** active redirects attempt one event; malformed, missing, and expired codes attempt none; typed insert failure and a missing event table still produce the original `302` and `Location`; migration upgrade/downgrade preserves `links`; the confirmed seeder `--reset` truncates `link_clicks` before `links`. **[FR-2, FR-8, FR-9, FR-13, NFR-2, NFR-8, L-7]**
- **Performance test:** with a documented machine, dataset, and traffic profile, measure server-side p99 for redirects and creates. Use average redirect load in A-6 (about 100 requests/second) for the NFR-3 pass/fail result, including pool acquisition; measure peak load in A-6 (about 1,000 requests/second) separately and report it without using it as a pass/fail target. Include the expected 100:1 read-to-write ratio from A-5 in the mixed workload where practical. Measure creates under A-7. Benchmark indexed lookup at the expected data scale; if average-load redirect p99 misses 200 ms, evaluate caching as a later change. **[NFR-3, NFR-4, NFR-5, A-4, A-5, A-6, A-7, A-8]**
- **Health check tests:** `/livez` returns 200 without database access; `/readyz` returns 200 against a healthy database, and a sanitized 503 for injected pool, connection and statement failures and for a missing `links` table, logging only the exception class name; both send `Cache-Control: no-store`, record no click, and are not treated as short codes by the catch-all route. **[FR-14, NFR-2]**

## Limitations

- The in-process limiter resets on restart and is single-process only. **[L-1]**
- DNS names that resolve to private or loopback addresses can bypass host validation because no DNS resolution is performed. **[L-2]**
- Expired mappings are never deleted, so storage grows over time. **[L-3]**
- Resubmitting an expiring URL without expiry makes it permanent under the agreed rule. **[L-4]**
- No cache is present initially; redirect latency depends on PostgreSQL and must be measured. **[NFR-3]**
- Click events are best-effort: failures lose events, retries/deduplication and bot filtering are absent, and storage grows without a retention policy. A failing or slow click write can delay the redirect by up to the pool-acquisition wait (250 ms) or the statement timeout (1 s). **[FR-13, NFR-2, NFR-8, L-7]**
- Malicious URL reputation checks are not implemented. **[Out of scope: malicious URL detection]**
- A SHA-256 collision is not separately checked by design, consistent with the no-full-URL-comparison decision. **[NFR-7]**
- If deployed behind a proxy while forwarded headers remain untrusted, all requests may appear to come from the proxy's IP and share one rate-limit bucket. **[NFR-1, L-6]**
- Alternate decimal, hexadecimal, and octal-like IPv4 host spellings are not normalized and may bypass literal-IP classification. **[FR-6, L-5]**
- `/readyz` checks only connectivity and the `links` table through the shared pool; it does not verify the migration revision or `link_clicks`, and under pool saturation it can report not ready. **[FR-14]**

## Assumptions for Review

- **S-1:** The JSON create route is `POST /api/links`; the browser form posts to `/`; the redirect route is `GET /{code}`. The requirements do not prescribe route names.
- **S-2:** API and form errors use the documented `422`, `429`, `404`, and `503` statuses and error format; the requirements specify only the redirect, missing/expired, and rate-limit statuses.
- **S-3:** API expiry values use RFC 3339 timestamps with an explicit timezone. The form submits the same unambiguous timestamp format.
- **S-4:** The 365-day expiry limit is measured from the current create request time, including repeated creates, rather than the original row's `created_at`.
- **S-5:** `localhost` and names ending in `.localhost` count as obvious local hostnames. A trailing dot is stripped before hostname comparisons.
- **S-6:** Every create request consumes a rate-limit slot, including requests later rejected for invalid input.
- **S-7:** Code-collision retries are bounded; after the retry limit, the API returns `503`. The retry count is an implementation setting.
- **S-8:** A SHA-256 collision is treated as negligible; the application does not compare the stored full URL on digest conflict, as explicitly decided.
- **S-9:** The hostname in `PUBLIC_BASE_URL` is the self-reference root and is configured as `jrb.sh` for this deployment. A different configured root would require revisiting FR-5.
- **S-10:** Initial database defaults are pool min/max 1/10, pool wait 250 ms, connect timeout 2 seconds, and statement timeout 1 second; benchmark results may justify changing them.
- **S-11:** Alternate decimal, hexadecimal, and octal-like IPv4 spellings are characterized in tests but are not normalized or guaranteed to be rejected in the initial build. **[L-5]**