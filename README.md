# AI URL Shortener

A small URL-shortening service that validates submitted HTTP(S) URLs, stores
stable seven-character short codes in PostgreSQL, and redirects visitors to the
original URLs. It uses Python 3.11, FastAPI, psycopg, PostgreSQL, and Jinja.
Development was AI-assisted; the process records are in `docs/`.

## Prerequisites

- Python 3.11.9 on the PATH as python (the py launcher is optional)
- Git.
- PowerShell.
- Docker Desktop with Docker Compose installed and running.

The project was developed on Windows, including an ARM machine. Docker Desktop
normally selects a compatible build of `postgres:16-alpine`; if the image will
not pull or start on another machine, check `docker info` for the OS and CPU
architecture and inspect the image's available platform variants before
choosing any platform override.

## Quick Start

1. Clone the repository and enter its directory:

   ```powershell
   git clone https://github.com/jannisrufina/AI-URL-Shortner.git
   Set-Location AI-URL-Shortner
   ```

   A correct result is a new `AI-URL-Shortner` directory with this README.

2. Create and activate the virtual environment, then install the pinned runtime
   and development requirements. Check `python --version` first: it must print
   3.11.x.

   ```powershell
   python -m venv .venv
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt -r requirements-dev.txt
   ```

   If `python` is not found or is the wrong version, install Python 3.11.9, or
   use the Python launcher (`py -3.11 -m venv .venv`) if it is installed. A
   correct result is an activated `(.venv)` prompt and a successful pip
   install. If activation is blocked, `Set-ExecutionPolicy` above applies to
   this PowerShell window only.

3. Create the local environment file and set a local PostgreSQL password:

   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```

   In `.env`, replace `replace-with-a-local-password` in `POSTGRES_PASSWORD`
   with a local development password. Use the same password in `DATABASE_URL`
   and `TEST_DATABASE_URL`; do not commit `.env`. Only Docker Compose reads
   `.env`; the app reads real environment variables, so step 5 sets them again
   in the shell. A correct result is a local `.env` with matching connection
   URL passwords; `notepad` has no command output to check.

4. Start PostgreSQL and wait for its health check:

   ```powershell
   docker compose up -d --wait postgres
   docker compose ps
   docker compose exec -T postgres pg_isready -U url_shortener -d url_shortener
   ```

   A correct result shows the `postgres` service as `healthy` and `pg_isready`
   reports that it accepts connections.

5. Set the app environment in this PowerShell window. These `$env:` values last
   only for this window. The URL password must match `POSTGRES_PASSWORD` in
   `.env`:

   ```powershell
   $env:DATABASE_URL = "postgresql://url_shortener:replace-with-a-local-password@localhost:5432/url_shortener"
   $env:PUBLIC_BASE_URL = "http://127.0.0.1:8000"
   $env:DB_POOL_MIN_SIZE = "1"
   $env:DB_POOL_MAX_SIZE = "10"
   $env:DB_POOL_WAIT_MS = "250"
   $env:DB_CONNECT_TIMEOUT_SECONDS = "2"
   $env:DB_STATEMENT_TIMEOUT_MS = "1000"
   ```

   Replace the password placeholder with exactly the value in `.env`. The
   local `PUBLIC_BASE_URL` makes returned short links clickable. A deployment
   should set it to the public hostname selected for that deployment. A
   correct result is that these values are set in this window; PowerShell
   prints no output for the assignments.

6. Apply the schema using Alembic, then confirm both tables exist:

   ```powershell
   python -m alembic upgrade head
   docker compose exec -T postgres psql -U url_shortener -d url_shortener -c "\d links"
   docker compose exec -T postgres psql -U url_shortener -d url_shortener -c "\d link_clicks"
   ```

   Alembic may print little or nothing on success. A correct result is the
   `\d links` output showing the columns `code`, `url_digest`, `original_url`,
   `expires_at`, and `created_at`, and the `\d link_clicks` output showing
   `id`, `code`, and `clicked_at`. `alembic upgrade head` applies both
   migrations. App startup does not create or migrate tables.

7. Start one app worker:

   ```powershell
   python -m uvicorn url_shortener.app:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers
   ```

   A correct result is Uvicorn listening at `http://127.0.0.1:8000`. Keep this
   server window open.

8. Try the form and JSON API from another PowerShell window:

   Open `http://127.0.0.1:8000/`, enter `https://example.com/`, and submit.
   The page should display a short link beginning with
   `http://127.0.0.1:8000/`.

   To create through the JSON API and request the short code:

   ```powershell
   $body = @{ url = "https://example.com/" } | ConvertTo-Json
   $response = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/links" -ContentType "application/json" -Body $body
   $response
   curl.exe -i $response.short_url
   ```

   A correct result includes `code`, `short_url`, and `expires_at`; the `curl`
   request to that short URL receives HTTP 302 with a `Location` header.

## Health checks

When the app is running, probe the health endpoints without the create limiter or
analytics behavior:

```powershell
curl.exe -i http://127.0.0.1:8000/livez
curl.exe -i http://127.0.0.1:8000/readyz
```

A correct result is HTTP 200 and JSON `{"status": "ok"}` for both. The
`/readyz` endpoint runs `SELECT 1 FROM links LIMIT 1` through the pool; if the
query cannot run, it returns the standard sanitized 503 response.

## Common Problems

- **`No module named ...`:** the virtual environment is not active. Activate it
  with `.\.venv\Scripts\Activate.ps1`, or invoke the environment explicitly,
  for example `.\.venv\Scripts\python.exe -m pytest`.
- **A settings error names `DATABASE_URL` or `PUBLIC_BASE_URL`:** set those
  `$env:` values in the same PowerShell window used to start the app. They do
  not carry over from another window.
- **`password authentication failed`:** the password in `.env` and the password
  in the connection URL disagree. For a disposable local database, reset it
  with `docker compose down -v`, then repeat the PostgreSQL startup steps. This
  deletes the Compose database volume and all data in it.
- **`database "url_shortener_test" does not exist`:** create the separate test
  database with `docker compose exec -T postgres createdb -U url_shortener
  url_shortener_test`.
- **Docker commands fail or ports are unavailable:** start Docker Desktop and
  wait for its engine. Check that ports 5432 and 8000 are free; if changing the
  PostgreSQL host port, update the port in the connection URLs too.

## Quality Checks and Tests

Activate the environment and install the compiled files as in Quick Start.
When PostgreSQL is available, create the isolated test database and set the
integration-test variables in the same PowerShell window (use the same
password as in `.env`):

```powershell
docker compose exec -T postgres createdb -U url_shortener url_shortener_test
$env:TEST_DATABASE_URL = "postgresql://url_shortener:replace-with-a-local-password@localhost:5432/url_shortener_test"
$env:REQUIRE_DB = "1"
```

Run quality gates before tests, and run them before every push:

```powershell
python scripts/check.py
python -m pytest
```

The quality script runs Ruff lint and format checks, mypy, Bandit, and
pip-audit. `pip-audit` queries a public advisory database, so
`scripts/check.py` needs an internet connection. With a reachable
`TEST_DATABASE_URL`, pytest runs the PostgreSQL integration tests as well; all
tests should pass with none skipped. `REQUIRE_DB=1` makes a missing or
unreachable test database fail instead of silently skipping those tests. In a
local run without a test database, integration tests skip.

Runtime and development dependencies are declared in `requirements.in` and
`requirements-dev.in`. Regenerate the pinned files after changing either:

```powershell
python -m pip install pip-tools==7.6.1
python -m piptools compile --output-file requirements.txt requirements.in
python -m piptools compile --constraint requirements.txt --output-file requirements-dev.txt requirements-dev.in
```

The test scan skips Bandit B101 only for ordinary test assertions; the source
scan has no B101 suppression. To confirm each gate can fail, use temporary files
outside the repository and remove them afterward:

| Gate | Temporary probe | Command |
|---|---|---|
| Ruff lint | `lint.py`: `import os` | `python -m ruff check $env:TEMP\url-shortener-gate-probes\lint.py` |
| Ruff format | `format.py`: `def f( x ):return x` | `python -m ruff format --check $env:TEMP\url-shortener-gate-probes\format.py` |
| mypy | `type_probe.py`: `value: int = "wrong"` | `python -m mypy --strict $env:TEMP\url-shortener-gate-probes\type_probe.py` |
| Bandit | `security_probe.py`: `exec("print('probe')")` | `python -m bandit $env:TEMP\url-shortener-gate-probes\security_probe.py` |
| pip-audit | `vulnerable.txt`: `jinja2==2.11.3` | `python -m pip_audit --no-deps --disable-pip -r $env:TEMP\url-shortener-gate-probes\vulnerable.txt` |

Create the directory with `New-Item -ItemType Directory
$env:TEMP\url-shortener-gate-probes`; remove it with
`Remove-Item -Recurse -Force $env:TEMP\url-shortener-gate-probes`. Do not commit
probe files. The intentionally vulnerable Jinja pin is only for verifying that
pip-audit reports an advisory.

The GitHub Actions workflow installs the compiled requirements, runs the same
quality gates, and attempts pytest even if a preceding step fails.

## Seeding Benchmark Data

Use a dedicated benchmark database. The 10,000-row smoke seed is the default;
1,000,000 rows is the recommended scale-up. The optional 10,000,000-row run
needs several GB of free disk space and substantial time. The seeder refuses
non-`_bench` databases and non-empty tables by default.

```powershell
docker compose exec -T postgres createdb -U url_shortener url_shortener_bench
$env:DATABASE_URL = "postgresql://url_shortener:replace-with-a-local-password@localhost:5432/url_shortener_bench"
$env:BENCH_DATABASE_URL = $env:DATABASE_URL
python -m alembic upgrade head

# Smoke seed (10,000 rows):
python -m scripts.seed_links --count 10000 --seed 1

# Recommended 1,000,000-row scale-up; reset is required to replace existing rows:
python -m scripts.seed_links --count 1000000 --seed 1 --reset --confirm-database url_shortener_bench --sample-codes-count 10000 --sample-codes-file sample-codes.txt

# Optional 10M run; opt in explicitly and reset only the dedicated benchmark DB:
python -m scripts.seed_links --count 10000000 --seed 1 --allow-large --reset --confirm-database url_shortener_bench --sample-codes-count 10000 --sample-codes-file sample-codes-10m.txt
```

The commands leave `DATABASE_URL` pointing at the benchmark database for this
PowerShell window. Use a separate window for benchmarking or reset it before
running the application or tests. The sample-code files are ignored by git.
The seeder's confirmed `--reset` clears `link_clicks` first, then `links`.

## Load Testing

Generate `sample-codes.txt` with the seeder first. In a dedicated server
PowerShell window, point the app to the benchmark database and start one worker
with the default pool settings:

```powershell
$env:DATABASE_URL = "postgresql://url_shortener:replace-with-a-local-password@localhost:5432/url_shortener_bench"
$env:PUBLIC_BASE_URL = "http://127.0.0.1:8000"
$env:DB_POOL_MAX_SIZE = "10"
$env:DB_POOL_WAIT_MS = "250"
python -m uvicorn url_shortener.app:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers
```

In a second PowerShell window with the environment activated, run one profile
per command. Pass the seeded row count; the harness does not need a database
connection. Create and mixed runs use local source addresses; ensure they are
available on the machine. With one address, create load is reduced to its
compliant rate and p99 is statistically weak.

```powershell
python -m scripts.load_test --profile redirect-average --sample-codes-file sample-codes.txt --dataset-rows 1000000 --pool-max 10 --pool-wait-ms 250
python -m scripts.load_test --profile redirect-peak --sample-codes-file sample-codes.txt --dataset-rows 1000000 --pool-max 10 --pool-wait-ms 250
python -m scripts.load_test --profile create --dataset-rows 1000000 --source-addresses 127.0.0.1,127.0.0.2,127.0.0.3,127.0.0.4,127.0.0.5,127.0.0.6,127.0.0.7 --pool-max 10 --pool-wait-ms 250
python -m scripts.load_test --profile mixed --sample-codes-file sample-codes.txt --dataset-rows 1000000 --source-addresses 127.0.0.1,127.0.0.2,127.0.0.3,127.0.0.4,127.0.0.5,127.0.0.6,127.0.0.7 --pool-max 10 --pool-wait-ms 250
```

For the separate overload profile, stop the server and restart it with these
pool settings, then run the overload command. Keep its results separate from
redirect-average; they do not replace the NFR-3 pass/fail run.

```powershell
$env:DB_POOL_MAX_SIZE = "1"
$env:DB_POOL_WAIT_MS = "50"
python -m uvicorn url_shortener.app:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers
```

```powershell
python -m scripts.load_test --profile overload --sample-codes-file sample-codes.txt --dataset-rows 1000000 --pool-max 1 --pool-wait-ms 50
```

The create profile runs at least four minutes. Reports are written to
`bench-results/`, which git ignores. This measures end-to-end latency on
loopback, an upper bound for server-side latency because it includes client
scheduling and loopback overhead. Results depend on the machine. If a target is
missed, report it as not met; peak and overload do not replace redirect-average.

## API Specification

`docs/openapi.json` is the checked-in OpenAPI description of the API as built.
Regenerate it after changing route metadata:

```powershell
python -m scripts.export_openapi
```

Check for drift without rewriting the file:

```powershell
python -m scripts.export_openapi --check
```

The running app also serves the interactive `/docs` UI and `/openapi.json`.

## Limitations

- Click analytics store only a short code and timestamp; writes are best-effort,
  events can be lost on failure, and there is no deduplication, bot filtering,
  or retention policy (L-7).
- See [requirements.md](docs/requirements.md) for L-1 to L-6: the limiter is
  single-process and resets on restart; hostname DNS is not resolved; expired
  rows are retained; repeats without expiry become permanent; alternate IPv4
  spellings are not normalized; and forwarded headers are not trusted behind
  proxies.
- See [architecture.md](docs/architecture.md) for additional known gaps,
  including the missing request-body size limit, limiter memory bounds, IPv6
  address rotation, shorthand/CGNAT/multicast hosts, and the punycode-only
  internationalized-host policy.
- NFR-3 (p99 below 200 ms at 100 redirects/second) is **not met** on the
  development machine (Windows ARM, 8 cores, Docker PostgreSQL, one worker,
  pool 10): the 100/s run failed with p99 2,328 ms (saturated). The service
  handled about 90/s with all 302 responses. Clean runs were at 25 and 50
  requests/second (25/s: p99 51.8 ms). No tuning was attempted. The create
  profile was run once from one source address with 36 requests; its p99 is
  statistically weak.

## Repository Map

| Path | Purpose |
|---|---|
| `url_shortener/` | FastAPI app, validation, expiry, persistence, DB pool/settings, limiter, and Jinja template. |
| `alembic/` | Versioned PostgreSQL schema migrations (`links`, `link_clicks`) and the Alembic environment. |
| `tests/` | Unit, API, and PostgreSQL integration tests. |
| `scripts/` | Quality checks, benchmark seeder, load-test harness, and OpenAPI exporter. |
| `docs/requirements.md` | Functional/non-functional requirements, assumptions, limitations. |
| `docs/design.md` | API contract, data model, validation and flow decisions. |
| `docs/tasks.md` | Ordered implementation tasks and acceptance criteria. |
| `docs/architecture.md` | Built component overview, flows, known gaps, and deviations. |
| `docs/openapi.json` | Generated OpenAPI artifact. |
| `docs/AI_usage_log.md` | AI usage/process record. |
| `.github/workflows/` | GitHub Actions quality and test workflow. |
| `docker-compose.yml`, `.env.example` | PostgreSQL service and local configuration template. |