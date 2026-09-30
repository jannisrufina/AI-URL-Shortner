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

