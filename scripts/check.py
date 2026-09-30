import subprocess
import sys

COMMANDS: tuple[tuple[str, ...], ...] = (
    ("ruff", "check", "."),
    ("ruff", "format", "--check", "."),
    ("mypy",),
    ("bandit", "-r", "url_shortener", "tests"),
    ("pip_audit", "-r", "requirements-dev.txt"),
)


def main() -> int:
    for command in COMMANDS:
        print(f"+ {sys.executable} -m {' '.join(command)}", flush=True)
        result = subprocess.run([sys.executable, "-m", *command], check=False)
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
