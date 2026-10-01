# AI-URL-Shortner

## Quality checks

Use Python 3.11.9. From PowerShell, create and activate a virtual environment,
install the pinned development requirements, and run the shared quality command:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt -r requirements-dev.txt
python scripts/check.py
python -m pytest
```

Runtime and development dependencies are declared in `requirements.in` and
`requirements-dev.in`. Regenerate the fully pinned files after changing either
input:

```powershell
python -m pip install pip-tools==7.6.1
python -m piptools compile --output-file requirements.txt requirements.in
python -m piptools compile --constraint requirements.txt --output-file requirements-dev.txt requirements-dev.in
```

`scripts/check.py` runs Ruff lint, Ruff formatting validation, mypy, Bandit on
application code, Bandit on tests with only B101 skipped, and pip-audit against
the fully pinned development file without dependency resolution. The GitHub
Actions workflow installs the compiled development file, runs these same gates
on every push and pull request, and always attempts pytest afterward.

To verify that each gate can fail, use temporary files outside the repository.
Create a temporary directory with `New-Item -ItemType Directory
$env:TEMP\url-shortener-gate-probes`, write each probe there, and run the
corresponding command below; the command should exit nonzero. Remove the
directory afterward with `Remove-Item -Recurse -Force
$env:TEMP\url-shortener-gate-probes`.

| Gate | Temporary probe | Command (run from the repository root) |
| --- | --- | --- |
| Ruff lint | `lint.py` containing `import os` | `python -m ruff check $env:TEMP\url-shortener-gate-probes\lint.py` |
| Ruff format | `format.py` containing `def f( x ):return x` | `python -m ruff format --check $env:TEMP\url-shortener-gate-probes\format.py` |
| mypy | `type_probe.py` containing `value: int = "wrong"` | `python -m mypy --strict $env:TEMP\url-shortener-gate-probes\type_probe.py` |
| Bandit | `security_probe.py` containing `exec("print('probe')")` | `python -m bandit $env:TEMP\url-shortener-gate-probes\security_probe.py` |
| pip-audit | `vulnerable.txt` containing `jinja2==2.11.3` | `python -m pip_audit --no-deps --disable-pip -r $env:TEMP\url-shortener-gate-probes\vulnerable.txt` |

Do not commit these probe files. The pinned project dependency audit should pass;
the deliberately vulnerable Jinja pin is only for confirming that pip-audit
reports an advisory.

The test scan skips B101 only because plain assertions are idiomatic in tests;
the application-code scan does not skip it.

## PostgreSQL bootstrap

Copy `.env.example` to `.env`, then start the single PostgreSQL service and wait
for its Compose health check before migrating:

```powershell
Copy-Item .env.example .env
docker compose up -d --wait postgres
docker compose ps
docker compose exec -T postgres pg_isready -U url_shortener -d url_shortener
docker compose exec -T postgres createdb -U url_shortener url_shortener_test
```

Set the application environment, apply the schema only through Alembic, and
start the single Uvicorn worker:

```powershell
$env:DATABASE_URL = "postgresql://url_shortener:replace-with-a-local-password@localhost:5432/url_shortener"
$env:PUBLIC_BASE_URL = "https://jrb.sh"
$env:DB_POOL_MIN_SIZE = "1"
$env:DB_POOL_MAX_SIZE = "10"
$env:DB_POOL_WAIT_MS = "250"
$env:DB_CONNECT_TIMEOUT_SECONDS = "2"
$env:DB_STATEMENT_TIMEOUT_MS = "1000"
python -m alembic upgrade head
python -m uvicorn url_shortener.app:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers
```

The 250 ms pool-acquisition wait is an overload/failure bound, not an expected
redirect wait. The in-process limiter is single-process and resets on restart
(L-1). Uvicorn runs with `--no-proxy-headers`: forwarded headers are not trusted,
so when deployed behind a proxy, requests share the proxy's rate-limit bucket
(L-6).

For the full non-performance suite, point at the separate disposable test
database created above and require the database fixture to fail rather than
skip if it cannot connect:

```powershell
$env:TEST_DATABASE_URL = "postgresql://url_shortener:replace-with-a-local-password@localhost:5432/url_shortener_test"
$env:REQUIRE_DB = "1"
python -m pytest
```

CI starts a PostgreSQL service container and sets REQUIRE_DB=1, so a missing or unreachable test database fails the run. Local runs without TEST_DATABASE_URL skip the integration tests.

## Seeding benchmark data

Use a dedicated benchmark database. The 10,000-row smoke seed is the default;
the 1,000,000-row run is the recommended scale-up. A 10,000,000-row run is
optional and requires several GB of free disk space plus substantial load time.
The seeder refuses non-`_bench` database names and non-empty tables by default.

```powershell
docker compose exec -T postgres createdb -U url_shortener url_shortener_bench
$env:DATABASE_URL = "postgresql://url_shortener:replace-with-a-local-password@localhost:5432/url_shortener_bench"
$env:BENCH_DATABASE_URL = $env:DATABASE_URL
python -m alembic upgrade head

# Smoke seed (10,000 rows) to confirm the tool works:
python -m scripts.seed_links --count 10000 --seed 1

# Recommended scale-up (1,000,000 rows). It replaces the smoke data, so it
# needs an explicit reset:
python -m scripts.seed_links --count 1000000 --seed 1 --reset --confirm-database url_shortener_bench --sample-codes-count 10000 --sample-codes-file sample-codes.txt

# Optional 10M scale seed; same explicit opt-in, and only if needed:
python -m scripts.seed_links --count 10000000 --seed 1 --allow-large --reset --confirm-database url_shortener_bench --sample-codes-count 10000 --sample-codes-file sample-codes-10m.txt

The commands above leave `DATABASE_URL` pointing at the benchmark database for
the rest of that PowerShell window. Use a separate window for benchmarking, or
set `DATABASE_URL` back to the main database before running the app or the
tests. The generated `sample-codes*.txt` files are ignored by git.

## Load testing

Generate `sample-codes.txt` with the seeder first. In a dedicated server
PowerShell window, point the app at the benchmark database, use the public base
URL expected by the sample codes, retain the default pool settings, and start
one worker without proxy-header handling:

```powershell
$env:DATABASE_URL = "postgresql://url_shortener:replace-with-a-local-password@localhost:5432/url_shortener_bench"
$env:PUBLIC_BASE_URL = "http://127.0.0.1:8000"
$env:DB_POOL_MAX_SIZE = "10"
$env:DB_POOL_WAIT_MS = "250"
python -m uvicorn url_shortener.app:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers
```

In a second PowerShell window, run one profile per command. The harness reads
the row count using the supplied benchmark connection, but never prints the
connection string. Create and mixed runs bind the listed loopback addresses;
ensure they are available locally. With only one address, create load is
reduced to its compliant rate and p99 is marked statistically weak.

```powershell
$env:BENCH_DATABASE_URL = "postgresql://url_shortener:replace-with-a-local-password@localhost:5432/url_shortener_bench"
python -m scripts.load_test --profile redirect-average --sample-codes-file sample-codes.txt --database-url $env:BENCH_DATABASE_URL --pool-max 10 --pool-wait-ms 250
python -m scripts.load_test --profile redirect-peak --sample-codes-file sample-codes.txt --database-url $env:BENCH_DATABASE_URL --pool-max 10 --pool-wait-ms 250
python -m scripts.load_test --profile create --database-url $env:BENCH_DATABASE_URL --source-addresses 127.0.0.1,127.0.0.2,127.0.0.3,127.0.0.4,127.0.0.5,127.0.0.6,127.0.0.7 --pool-max 10 --pool-wait-ms 250
python -m scripts.load_test --profile mixed --sample-codes-file sample-codes.txt --database-url $env:BENCH_DATABASE_URL --source-addresses 127.0.0.1,127.0.0.2,127.0.0.3,127.0.0.4,127.0.0.5,127.0.0.6,127.0.0.7 --pool-max 10 --pool-wait-ms 250
```

For the overload profile, stop the server, restart it with pool maximum 1 and
pool wait 50 ms, then run the overload command. Keep its results separate; they
do not replace redirect-average for NFR-3.

```powershell
$env:DB_POOL_MAX_SIZE = "1"
$env:DB_POOL_WAIT_MS = "50"
python -m uvicorn url_shortener.app:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers
```

```powershell
python -m scripts.load_test --profile overload --sample-codes-file sample-codes.txt --database-url $env:BENCH_DATABASE_URL --pool-max 1 --pool-wait-ms 50
```

Use `--dataset-rows N` instead of `--database-url` to omit the harness's
database count query. Reports are written as JSON and Markdown in
`bench-results/`. This measures end-to-end latency on loopback, which is an
upper bound for server-side latency (so it includes client scheduling and
loopback overhead). Results depend on the machine. If a target is missed, report
it as not met; peak and overload results never replace the average-load NFR-3
run.
