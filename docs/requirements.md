# Functional requirements:
FR-1: Generate a unique short URL for a URL. Repeated creates of the exact same URL must return the same short URL. Short codes are 7 random Base62 characters.
FR-2: Redirect users from a short URL to its original URL.
FR-3: Accept only well-formed HTTP and HTTPS URLs no longer than 2,048 characters.
FR-4: Reject URLs with embedded credentials.
FR-5: Reject a URL as self-referencing when its parsed hostname, case-insensitively, is `jrb.sh` or a subdomain of `jrb.sh`. The check applies regardless of port or path and does not use a database lookup.
FR-6: Reject private or loopback literal IP addresses and obvious local hostnames such as `localhost`. Do not resolve other hostnames; DNS-based bypass is a known limitation.
FR-7: An optional expiry must be in the future and no more than 365 days from creation time.
FR-8: Return HTTP 302 for redirects.
FR-9: Return HTTP 404 for expired or nonexistent short URLs.
FR-10: Provide a single-page create form at `/`. Escape user-provided and generated values rendered in the page.
FR-11: Use the configured public short-URL base URL, supplied through an environment variable, when constructing short URLs.

# Non-functional requirements:
NFR-1: Limit creates to 10 per minute per direct client IP. Enforce the limit in application-process memory, without trusting proxy headers, and return HTTP 429 when exceeded.
NFR-2: Use parameterized database queries, obtain secrets from environment variables, and do not log full URLs.
NFR-3: Meet a server-side p99 redirect latency below 200 ms. Do not use a cache initially; revisit caching if this target is missed.
NFR-4: Meet a server-side p99 create latency below 300 ms.
NFR-5: Support 10 million stored links with fast lookup by short code.
NFR-6: Use PostgreSQL as the only external service. Provide a one-service Docker Compose configuration for PostgreSQL; do not use Redis.
NFR-7: Enforce repeated-URL uniqueness using a unique index on the SHA-256 digest of the exact URL string. Do not perform a separate full-URL comparison.
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

# Out of scope:
- Malicious URL detection, such as Safe Browsing.
- Click analytics are excluded from the initial build and will be added later as a brownfield enhancement. Keep a hook point after short-code lookup and expiry validation, before returning the redirect response.