# Functional requirements:
FR-1: Generate a unique short URL for a URL. Repeated creates of the exact same URL must return the same short URL. Short codes are 7 random Base62 characters.
FR-2: Redirect users from a short URL to its original URL.
FR-3: Accept only well-formed HTTP and HTTPS URLs no longer than 2,048 characters, containing no raw whitespace or control characters.
FR-4: Reject URLs with embedded credentials.
FR-5: Reject a URL as self-referencing when its parsed hostname, case-insensitively, equals the hostname of the configured public base URL (jrb.sh in this deployment) or is a subdomain of it.
FR-6: Reject literal IP addresses that are private, loopback, link-local, or unspecified (including IPv4-mapped IPv6 forms), and obvious local hostnames such as localhost. Do not resolve other hostnames.
FR-7: An optional expiry must be in the future and no more than 365 days after the time of the current create request, including repeated creates.
FR-8: Return HTTP 302 for redirects.
FR-9: Return HTTP 404 for expired, nonexistent, or malformed short codes.
FR-10: Provide a single-page create form at `/`. Escape user-provided and generated values rendered in the page.
FR-11: Use the configured public short-URL base URL, supplied through an environment variable, when constructing short URLs.
FR-12: A successful create returns 200 for both new and reused links.
FR-13: For each redirect to a found, unexpired link, attempt to record one click event containing the short code and a UTC timestamp. Analytics persistence is best-effort and must not change the redirect response.

# Non-functional requirements:
NFR-1: Limit creates to 10 per minute per direct client IP. Enforce the limit in application-process memory, without trusting proxy headers, and return HTTP 429 when exceeded.
NFR-2: Use parameterized database queries, obtain secrets from environment variables, and do not log full URLs.
NFR-3: Meet a server-side p99 redirect latency below 200 ms, measured under the expected average load in A-6 (about 100 requests/second), including database connection pool acquisition. Results under the peak load in A-6 (1,000 requests/second) are measured and reported but are not a pass/fail target for the prototype. Do not use a cache initially; revisit caching if the target is missed.
NFR-4: Meet a server-side p99 create latency below 300 ms, measured under the expected load in A-7.
NFR-5: Support 10 million stored links with fast lookup by short code.
NFR-6: Use PostgreSQL as the only external service. Provide a one-service Docker Compose configuration for PostgreSQL; do not use Redis.
NFR-7: Enforce repeated-URL uniqueness using a unique index on the SHA-256 digest of the exact URL string. Do not perform a separate full-URL comparison.
NFR-8: Click analytics must not regress the engineer-measured redirect-average result beyond the Task 15 baseline threshold recorded in AI_usage_log.md D-57: p99 at or below 77.8 ms, all responses 302, zero errors, and not generator-limited. The engineer runs this measurement; the AI does not.
# Assumptions:
A-1: Repeated URLs are defined by exact string equality; comparison is case-sensitive.
A-2: On a repeated create, if the existing link has no expiry or the new request provides none, the resulting link has no expiry; otherwise the new expiry replaces the existing one.
A-3: Resubmitting an expired link revives the same short code, following A-2.
A-4: The expected stored-link count is 10 million.
A-5: The expected read-to-write ratio is about 100:1.
A-6: Expected redirect traffic is about 100 requests/second average and 1,000 requests/second peak.
A-7: Expected creation traffic is about 1 request/second on average.
A-8: Estimated record size is about 500 bytes, or roughly 5 GB for 10 million records, excluding indexes and other database overhead.
A-9: Any user may shorten any URL and change its expiry.

# Limitations:
L-1: The in-process rate limiter resets on application restart and is limited to a single application process.
L-2: Hostnames that resolve to private or loopback IP addresses may bypass the private-host check because DNS resolution is not performed.
L-3: Expired mappings are retained indefinitely, so database storage grows over time.
L-4: A user can make an expiring link permanent by resubmitting the same URL without an expiry, as specified by A-2.
L-5: Alternate decimal, hexadecimal, and octal IPv4 host spellings (for example 2130706433, 0x7f000001, 0177.0.0.1) are not normalized and may bypass literal-IP classification.
L-6: If the service is deployed behind a proxy, all requests appear to come from the proxy's IP and share a single rate-limit bucket, because forwarded headers are not trusted.
L-7: Click events are best-effort and can be lost when persistence fails; there is no deduplication or bot filtering, and the append-only table grows without a retention policy.

# Out of scope:
- Malicious URL detection, such as Safe Browsing.
- Aggregations, dashboards, bot filtering, and public analytics endpoints remain out of scope; the minimal best-effort click-event write is specified by FR-13.