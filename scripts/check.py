import subprocess
import sys

COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Ruff lint", ("ruff", "check", ".")),
    ("Ruff format", ("ruff", "format", "--check", ".")),
    ("mypy", ("mypy",)),
    ("Bandit source", ("bandit", "-r", "url_shortener")),
    # B101 flags ordinary asserts used in tests; skip it only for test files.
    ("Bandit tests", ("bandit", "-r", "tests", "-s", "B101")),
    (
        "pip-audit",
        (
            "pip_audit",
            "--no-deps",
            "--disable-pip",
            "-r",
            "requirements-dev.txt",
        ),
    ),
)


def main() -> int:
    failures: list[str] = []
    for name, command in COMMANDS:
        print(f"+ python -m {' '.join(command)}", flush=True)
        result = subprocess.run([sys.executable, "-m", *command], check=False)
        if result.returncode != 0:
            failures.append(f"{name} (exit {result.returncode})")

    if failures:
        print("\nQuality gate failures:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print("All quality gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
