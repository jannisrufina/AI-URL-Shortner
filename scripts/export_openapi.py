"""Generate or check the committed OpenAPI artifact without starting the app."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from url_shortener.app import create_app

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SPEC_PATH = _PROJECT_ROOT / "docs" / "openapi.json"
_STALE_MESSAGE = "OpenAPI artifact is stale; run python -m scripts.export_openapi."


def generate_spec() -> dict[str, Any]:
    application = create_app()
    return application.openapi()


def render_spec(spec: dict[str, Any] | None = None) -> bytes:
    document = generate_spec() if spec is None else spec
    text = json.dumps(
        document,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return (text + "\n").encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    generated = render_spec()
    if args.check:
        try:
            committed = _SPEC_PATH.read_bytes()
        except OSError:
            print(_STALE_MESSAGE, file=sys.stderr)
            return 1
        if committed != generated:
            print(_STALE_MESSAGE, file=sys.stderr)
            return 1
        return 0

    _SPEC_PATH.write_bytes(generated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
