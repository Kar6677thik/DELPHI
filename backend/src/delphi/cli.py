"""The ``delphi`` command (docs/04 §4). M1 provides the ``kb`` subcommands."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from delphi.kb.canonical import content_sha256
from delphi.kb.issues import KbValidationError
from delphi.kb.loader import load_and_validate
from delphi.probes.registry import CLASSIFIER_NAMES


def _kb_validate(args: argparse.Namespace) -> int:
    try:
        kb = load_and_validate(Path(args.path), CLASSIFIER_NAMES)
    except KbValidationError as exc:
        for issue in exc.issues:
            print(issue, file=sys.stderr)
        print(f"KB invalid: {len(exc.issues)} issue(s)", file=sys.stderr)
        return 1
    print(
        f"KB valid: {kb.name} - {len(kb.hypotheses)} hypotheses, {len(kb.sources)} sources, "
        f"sha256 {content_sha256(kb)}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="delphi")
    sub = parser.add_subparsers(dest="command", required=True)
    kb = sub.add_parser("kb", help="knowledge base tools").add_subparsers(
        dest="kb_command", required=True
    )
    validate = kb.add_parser("validate", help="validate a KB YAML tree (docs/07 §3)")
    validate.add_argument("path", nargs="?", default="kb")
    validate.set_defaults(func=_kb_validate)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
