# Ambiguity: “Make the service more reliable”

This memo is for the Task 16 ambiguity scenario in [docs/tasks.md](tasks.md). The requirement is intentionally vague, and the codebase already contains a set of reliability behaviors that are explicit in [docs/requirements.md](requirements.md), [docs/design.md](design.md), [docs/architecture.md](architecture.md), [url_shortener/app.py](../url_shortener/app.py), [url_shortener/database.py](../url_shortener/database.py), and [url_shortener/settings.py](../url_shortener/settings.py). The purpose here is to separate “already built” from “still ambiguous” before any implementation slice is chosen.

## 1) Why this requirement is ambiguous

“Reliable” can mean several different things for this service, each with different behavior, operating assumptions, and implementation size.

1. Availability / uptime
   - Meaning: keep the service running and answering requests despite partial failures.
   - What it would change here: it could require startup health checks, retry loops, degraded-mode routes, or graceful handling of PostgreSQL startup issues; it could also drive a different deployment model.
   - Size: medium. This is usually more than a small backend patch because it affects app lifecycle, readiness, and operational assumptions.

2. Failure handling correctness
   - Meaning: when a dependency fails, the app should return the right status and avoid misleading success or false 404s.
   - What it would change here: it could expand the error taxonomy beyond the current sanitized `503` handling, add retries only for safe cases, or make retry policy and logging policy explicit.
   - Size: small to medium. The code already distinguishes database access failures from code collisions in [url_shortener/app.py](../url_shortener/app.py) and [url_shortener/database.py](../url_shortener/database.py), so this is a scope choice rather than a blank slate.

3. Data durability / loss prevention
   - Meaning: no lost URL mappings, no lost click-events, or at least predictable loss behavior.
   - What it would change here: it could require stronger write durability guarantees, transaction boundaries, or event buffering. The current design calls out best-effort click writes and a no-cache redirect path, which are not durability guarantees.
   - Size: medium to large. This usually changes storage semantics, write paths, and operational expectations, not just route logic.

4. Graceful degradation under bad dependencies
   - Meaning: continue serving safe traffic even when a non-critical dependency slows down or fails.
   - What it would change here: it could mean preserving redirect responses while a click-write path fails, or keeping create validation working even when analytics or background tasks are unavailable.
   - Size: small to medium. This is feasible, but it requires deciding which failures are “non-critical” versus user-visible.

5. Observability and operational diagnosis
   - Meaning: the team can tell why requests failed and how often the service is unhealthy.
   - What it would change here: it could require structured logs, metrics, traces, request IDs, or explicit health endpoints. The current requirements call for sanitized logs and no full-URL logging in [docs/requirements.md](requirements.md), which constrains but does not resolve the instrumentation design.
   - Size: small to medium. It is often a cross-cutting change that touches the app boundary and deployment assumptions, but not necessarily the database schema.

6. Performance under load
   - Meaning: maintain latency and throughput under average or peak traffic, especially for redirects.
   - What it would change here: it could require altering the database pool, statement timeouts, batching, caching, or request path design. The code already has bounded pool settings and explicit load targets in [docs/requirements.md](requirements.md) and [docs/design.md](design.md).
   - Size: medium. This can become a benchmarking and tuning project rather than a simple reliability patch.

7. Deploy safety / rollout safety
   - Meaning: updates should not cause outages or silent schema drift during deployment.
   - What it would change here: it could require stronger migration discipline, compatibility gates, or a rollout plan. The existing design explicitly says not to run migrations on every request or silently create/alter tables during startup in [docs/design.md](design.md).
   - Size: small to medium. It changes operational workflow more than application logic.

8. Security and abuse resilience
   - Meaning: the service should resist bad inputs, abuse, or hostile traffic without breaking legitimate use.
   - What it would change here: it could mean stronger validation, quotas beyond the current per-IP create limiter, bot filtering, or request-body protections. The current requirement deliberately excludes malicious URL detection and bot analytics in [docs/requirements.md](requirements.md), so this interpretation is not the same as “normal reliability.”
   - Size: medium. It often expands validation and operational policy beyond the existing scope.

In short: the phrase “more reliable” is broad enough to cover uptime, correctness under failure, durability, handling slow dependencies, performance, observability, rollout safety, and abuse resistance. The right interpretation depends on what is considered the loss event and what the service is allowed to sacrifice to preserve a different property.

## 2) What the service already does for reliability today

This section is intentionally limited to behaviors already specified or implemented in the current code and requirements, so the ambiguity memo does not re-propose built work.

- Bounded database access and timeout controls are already in place.
  - The settings default to pool minimum 1, maximum 10, 250 ms pool wait, 2 second connect timeout, and 1 second statement timeout in [url_shortener/settings.py](../url_shortener/settings.py).
  - The database wrapper translates pool wait, connection failure, and statement-cancel conditions into typed failures in [url_shortener/database.py](../url_shortener/database.py).
  - The requirements explicitly call for bounded database access, sanitised `503` responses, and no cache in the initial design in [docs/requirements.md](requirements.md).

- Sanitized failure behavior is already built into the application boundary.
  - The app maps request-validation, URL-validation, expiry-validation, database-access, and code-generation exhaustion cases to fixed-status error responses in [url_shortener/app.py](../url_shortener/app.py).
  - The API does not echo full submitted URLs to users or logs; the requirements state “do not log full URLs” and “do not trust forwarded client-IP headers” in [docs/requirements.md](requirements.md).

- Redirects already fail safely under a missing or expired link.
  - The requirement set says missing, malformed, and expired codes return `404`; the redirect flow validates the code and expiry before proceeding in [docs/design.md](design.md) and [url_shortener/app.py](../url_shortener/app.py).
  - This preserves a predictable redirect contract even when the database is otherwise available.

- Best-effort click analytics are already intentionally non-blocking.
  - FR-13 in [docs/requirements.md](requirements.md) says each successful, unexpired redirect should attempt one click event, with analytics persistence best-effort and not changing the redirect response.
  - The redirect path in [url_shortener/app.py](../url_shortener/app.py) attempts the write and catches any failure locally so the `302` still returns.

- The create path already handles common failure modes without broad retry on arbitrary DB failures.
  - The design requires one parameterized `INSERT ... ON CONFLICT` path with retry only on code collisions, not a broad retry loop for all database issues, in [docs/design.md](design.md).
  - This prevents misclassifying a real database failure as a harmless code collision.

- The in-process create limiter already constrains the most obvious request-abuse path.
  - The requirements say limit creates to 10 per direct client IP per 60 seconds and do not trust proxy headers in [docs/requirements.md](requirements.md).
  - The design names the limiter as a single-process sliding window with inactive-bucket cleanup in [docs/design.md](design.md).

- The service is already intentionally simple and non-cache-first.
  - [docs/design.md](design.md) and [docs/requirements.md](requirements.md) explicitly say no cache initially and no Redis, with PostgreSQL as the only external service.
  - This limits reliability decisions to the existing service boundary rather than introducing a new persistence layer or staging store.

- Validation already catches many common client-side and self-reference issues before persistence.
  - The URL validation rules in [docs/design.md](design.md) cover HTTP/HTTPS syntax, credentials, self-reference detection against the configured public base, and private/local host rejection.
  - This is a reliability guardrail for bad input, but it is not a broad service-availability strategy.

- There is already an explicit “fail fast” stance around startup and connection access.
  - The app lifecycle opens the database pool at startup and closes it at shutdown in [url_shortener/app.py](../url_shortener/app.py).
  - This matches the design goal: the service reads its settings, opens the pool, and fails startup when the pool cannot be created, while request-time database problems map to sanitized `503` responses.

The point is not that reliability is already “done.” It is that the codebase already enforces a narrow, explicit reliability baseline: bounded DB access, fixed errors for dependency failure, rate limiting, failure-safe redirect analytics, and no cache. The ambiguity is therefore not “what is reliability?” but “which reliability problem do we want to optimize next?”

## 3) Ranked clarifying questions

The questions below are ordered by how much the answer changes the work. Each question includes the options and the trade-off.

### 1. What is the primary failure you want the service to survive?

Options:
- Database outage or slow database responses.
- Bad client input / validation abuse.
- Burst traffic and latency spikes.
- Partial outages while the app remains available.
- Data loss or state inconsistency.

Trade-off:
- Choosing database failures as the main target makes the work mostly a dependency-hardening and error-handling effort.
- Choosing traffic spikes makes it a performance and capacity design question.
- Choosing data loss makes it a durability and write-path question.

Default if unanswered:
- Assume “database slowness/outage and request-path failure handling” are the main target, because that matches the explicit requirement and built-in 503 patterns.

Risk of default being wrong:
- Medium. If the real concern is traffic spikes or abuse, this default will underweight performance and capacity work.

### 2. Is the requirement about user-facing availability or internal operational resilience?

Options:
- Users must still get usable responses during a minor dependency problem.
- The service must be diagnosable and operator-friendly even if not perfectly available.
- The goal is to avoid silent data loss above all else.
- The goal is to keep the app running under traffic without changing the public API.

Trade-off:
- Availability-oriented work often changes routing, startup behavior, and operational policies.
- Observability-oriented work can be done without changing public behavior but often needs logs, metrics, or health checks.
- Durability-oriented work changes data semantics and write safety.

Default if unanswered:
- Assume the requirement is user-facing, not just operator-facing: the app should keep serving valid requests and fail in a controlled way when dependencies break.

Risk of default being wrong:
- Medium. If the real intent is purely operational instrumentation, the default could lead to an unnecessarily broad runtime-change proposal.

### 3. Are we optimizing for correctness under failure, or for throughput/latency under load?

Options:
- Keep the current semantics and make failures explicit and safe.
- Keep the response path fast even when a non-critical dependency is slow.
- Accept some risk of reduced latency to gain stronger durability or consistency.

Trade-off:
- Correctness-first changes can add retries, extra validation, or stricter write semantics.
- Throughput-first changes often involve caching, pooling, batching, or reducing work on the request path.

Default if unanswered:
- Assume correctness-under-failure is the first priority, with throughput still constrained by the existing NFR targets.

Risk of default being wrong:
- High. If the real business concern is peak-load stability rather than failure correctness, this default could choose the wrong success metric.

### 4. Do you want the “reliable” slice to be entirely inside this single-process app, or is deployment/topology part of scope?

Options:
- Keep the scope inside the current one-service app and PostgreSQL config.
- Include deployment choices such as health checks, multi-instance operation, or orchestrator behavior.
- Include proxy/load-balancer concerns and request-shaping.

Trade-off:
- In-process work is faster and safer for a Phase A ambiguity exercise.
- Deployment-level work is broader and depends on how the service is run in production; this was explicitly avoided in the requirement text.

Default if unanswered:
- Assume the work stays inside the current app and DB boundary; do not assume multi-instance scaling, orchestrator features, or a production deployment model.

Risk of default being wrong:
- High. If the real environment is multi-instance or behind a managed platform, a single-process default under-specifies the needed work.

### 5. How much observability is needed to call the service “reliable”?

Options:
- Minimal: explicit statuses and sanitized operational logs only.
- Standard: request metrics and error counters.
- Full: request traces, health endpoints, and deployment-level monitoring.

Trade-off:
- More observability improves diagnosis but broadens the scope and may require external tooling or new interfaces.
- Minimal observability is cheaper but less useful when the release is under stress.

Default if unanswered:
- Assume minimal observability is enough for this ambiguity phase: explicit handler errors and logs, without any new monitoring stack or external metrics system.

Risk of default being wrong:
- Medium. If the real requirement is a production monitoring standard, the default will miss the intended effort.

### 6. Should “reliable” include protecting against bad traffic and abuse as well as unstable dependencies?

Options:
- No; focus on dependency and request-path failure handling.
- Yes; include additional abuse controls and attack-surface hardening.
- Only the current rate limiter is in scope; no new abuse mechanisms.

Trade-off:
- Abuse protection expands the work into validation, throttling, and operational policy.
- Leaving it out keeps the scope small and aligned with the current requirements.

Default if unanswered:
- Assume no new abuse-protection features beyond the current direct-IP rate limiter and validation rules.

Risk of default being wrong:
- Medium. If the real concern is hostile traffic or bot abuse, this default underestimates the work.

### 7. Is a “reliable” slice allowed to reduce feature scope while preserving current API behavior?

Options:
- Yes; the team can defer broader features to choose a smaller reliability slice.
- No; the goal is to improve reliability without reducing product behavior.
- Only minor operational changes are allowed; no feature removal.

Trade-off:
- Defer-and-slice approaches are safer for Phase A and match the Task 16 instructions.
- “No reduction” pushes the work toward major redesign or broader deployment changes.

Default if unanswered:
- Assume a slice is allowed, and the work should stay small and explicit instead of redefining the product.

Risk of default being wrong:
- Low to medium. This is mostly a project-governance choice; the wrong default would broaden scope unnecessarily.

### 8. Is the desired reliability improvement targeted at the create path, the redirect path, or both?

Options:
- Create path only.
- Redirect path only.
- Both, with a common reliability policy.

Trade-off:
- Redirect-only work usually targets latency and click-write safety.
- Create-only work usually targets validation, database write safety, and rate-limit behavior.
- Both-path work is broader and likely needs integration-level validation.

Default if unanswered:
- Assume both paths matter, but the highest-risk path is the redirect flow because it is the hot path and includes the best-effort analytics write.

Risk of default being wrong:
- Medium. If the main issue is a create workload or a burst of create traffic, a redirect-focused default would misprioritize the work.

## 4) Proposed smallest useful slice (recommendation)

I recommend the following slice as the smallest useful interpretation, but it is not a decision yet; it is a recommendation to accept, change, or reject.

Recommended slice:
- Focus on the request-path reliability contract, not deployment topology.
- Define reliability as: “the service fails in a controlled, explainable way when dependencies are slow or unavailable, without exposing unsafe or misleading behavior to users.”
- Scope this to the existing single-process app, PostgreSQL boundary, and current public API contract.
- Keep the current validation and rate-limit design as-is.
- Improve only the failure-handling and observability boundary around database exhaustion, timeouts, and slow dependency behavior.
- Make the work explicit and measured by the existing API statuses and remaining requirement targets without adding new infrastructure assumptions.

This slice is intentionally small because it fits the current app and code patterns in [url_shortener/app.py](../url_shortener/app.py), [url_shortener/database.py](../url_shortener/database.py), and [url_shortener/settings.py](../url_shortener/settings.py). It does not redefine the system as multi-instance, adds no external monitoring stack, and does not change the design or requirements without approval.

Why this is a useful slice:
- It clearly answers the most common interpretation of “reliable” for a service that already has bounded DB timeouts and sanitized `503`s.
- It does not duplicate what is already built.
- It can be implemented and tested in a small, reviewable unit without assuming anything about orchestrator or deployment strategy.

If you prefer a different interpretation, the main alternatives are:
- latency and throughput under load,
- data durability and stronger write guarantees,
- full deployment-level resilience and observability,
- abuse protection and hostile-traffic hardening.

## 5) What is explicitly out of scope for that recommended slice

For the recommended slice, the following are out of scope unless you specifically choose a broader interpretation:

- Any new orchestrator, load-balancer, or instance-scaling design.
- Any assumption about multi-process or multi-instance operation.
- Any new monitoring stack, metrics backend, tracing system, or external alerting framework.
- Any change to the database schema or migration strategy.
- Any caching layer, Redis usage, or background queueing.
- Any analytics dashboard or public reporting endpoint.
- Any requirement change in [docs/requirements.md](requirements.md) without explicit approval.
- Any change to the public API contract beyond explicitly agreed failure handling and operational logging.
- Any load-test or benchmark database work, per the Task 16 constraints.

This keeps the ambiguity exercise in the “clarify the meaning and the minimal acceptable slice” phase, rather than turning it into a broad deployment or platform hardening project.

## 6) Summary

The requirement is ambiguous because “reliable” could mean availability, correctness under failure, durability, degradation behavior, observability, performance, deploy safety, or abuse resilience. The current service already enforces a narrow baseline: bounded PostgreSQL pool behavior, typed database failures, sanitized `503` responses, rate limiting, and best-effort click processing. The right next step is not to pick a single meaning by default, but to answer the questions above and then choose the smallest slice that matches the intended definition.

## 7) Engineer clarification (supplied by me)

I am not accepting the AI's recommended slice. It restates reliability behavior the service already has (bounded pool, timeouts, sanitized 503s) and defines no testable deliverable.

**My answers**
- Q1: the failure to survive is a PostgreSQL outage or missing schema, and an operator or orchestrator being unable to tell the app is unhealthy.
- Q2: operational resilience: a way to tell "the process is up" from "the service can serve requests".
- Q3: correctness under failure first. Latency targets stay as in the existing NFRs.
- Q4: inside the current single-process app and PostgreSQL boundary. No orchestrator or multi-instance assumptions.
- Q5: minimal: two health endpoints, no metrics or tracing stack.
- Q6: no new abuse controls.
- Q7: yes, a small slice is acceptable.
- Q8: neither create nor redirect; this is a new operational endpoint pair.

**Chosen slice: liveness and readiness endpoints**
- `GET /livez`: returns 200 `{"status":"ok"}` with no database access.
- `GET /readyz`: runs `SELECT 1 FROM links LIMIT 1` through the existing pool, with the existing pool wait and statement timeout. Returns 200 `{"status":"ok"}` on success. On a database failure it returns the existing standard 503 body (`service_unavailable`). A missing `links` table (migration not applied) also counts as not ready.
- Both are registered before the catch-all `GET /{code:path}`, are not rate limited, record no click, and expose no connection details or error text.
- Paths are `/livez` and `/readyz`, not `/healthz`: `healthz` is exactly seven letters and would be a valid short code.

**Acceptance criteria**
- `/livez` returns 200 even when the database is unreachable.
- `/readyz` returns 200 with a healthy database, 503 with the standard error body on injected pool, connection and statement failures, and 503 when `links` is missing.
- Neither route is shadowed by the catch-all, and a 7-character code still redirects as before.
- `docs/openapi.json` is regenerated and the drift test passes.
- Docs updated (new FR-14, design, architecture, README); no existing test is weakened.

**Out of scope**
Retries, circuit breakers, metrics, alerting, multi-instance behavior, startup behavior when PostgreSQL is down, the retry-exhaustion log gap, and any change to existing routes. The last two are documented known gaps, not part of this slice.
