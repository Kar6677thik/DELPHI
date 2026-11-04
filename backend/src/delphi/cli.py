"""The ``delphi`` command (docs/04 §4): ``api``, ``worker``, ``migrate``, ``openapi``,
``kb validate`` and ``sim freeze-response-models``.

This module is the process entrypoint and the only importer of ``delphi.api``. Configuration
errors exit with status 2 and a message naming the variable (fail fast, docs/24 §1).
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

import uvicorn
from alembic import command

from delphi.api.app import create_app
from delphi.api.contract import OPENAPI_FILE, openapi_document
from delphi.kb.canonical import content_sha256
from delphi.kb.issues import KbValidationError
from delphi.kb.loader import load_and_validate
from delphi.platform.db import alembic_config
from delphi.platform.logging import configure_logging, get_logger
from delphi.platform.settings import Settings, SettingsError, load_settings
from delphi.platform.wiring import build_platform
from delphi.platform.worker import install_stop_handlers, run_worker
from delphi.probes.registry import CLASSIFIER_NAMES
from delphi.sim.response_models import GENERATED_FILE, dump_yaml, freeze, load_config

EXIT_CONFIG_ERROR = 2
# src/delphi/cli.py → the repository's kb/ (KB v1)
_REPO_KB_DIR = Path(__file__).resolve().parents[3] / "kb"
log = get_logger(__name__)


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


def _openapi(args: argparse.Namespace) -> int:
    out = Path(args.out)
    document = openapi_document()
    if args.check:
        if not out.is_file() or out.read_text(encoding="utf-8") != document:
            print(f"{out} is out of date: run `uv run delphi openapi`", file=sys.stderr)
            return 1
        print(f"{out} is up to date")
        return 0
    out.write_text(document, encoding="utf-8")
    print(f"wrote {out}")
    return 0


def _freeze_response_models(args: argparse.Namespace) -> int:
    try:
        kb = load_and_validate(Path(args.kb), CLASSIFIER_NAMES)
    except KbValidationError as exc:
        print(f"KB invalid: {len(exc.issues)} issue(s); run `delphi kb validate`", file=sys.stderr)
        return 1
    text = dump_yaml(freeze(kb, load_config()))
    out = Path(args.out)
    if args.check:
        if not out.is_file() or out.read_text(encoding="utf-8") != text:
            print(
                f"{out} is out of date: run `uv run delphi sim freeze-response-models`",
                file=sys.stderr,
            )
            return 1
        print(f"{out} is up to date")
        return 0
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    return 0


def _with_settings(run: Callable[[Settings], int]) -> Callable[[argparse.Namespace], int]:
    def command(_args: argparse.Namespace) -> int:
        try:
            settings = load_settings()
        except SettingsError as exc:
            print(f"delphi: {exc}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        configure_logging(settings.log_level)
        try:
            return run(settings)
        except SettingsError as exc:
            print(f"delphi: {exc}", file=sys.stderr)
            return EXIT_CONFIG_ERROR

    return command


def _quiet_alembic() -> None:
    # /ready reads the schema revision through Alembic, which logs at INFO on every call.
    logging.getLogger("alembic.runtime.migration").setLevel(logging.WARNING)


def _api(settings: Settings) -> int:
    _quiet_alembic()
    platform = build_platform(settings, process="api")
    try:
        uvicorn.run(
            create_app(platform),
            host=settings.http_host,
            port=settings.http_port,
            log_config=None,  # logging is configured by configure_logging (docs/20 §1)
            access_log=False,
            server_header=False,
        )
    finally:
        platform.close()
    return 0


def _worker(settings: Settings) -> int:
    _quiet_alembic()
    stop = threading.Event()
    install_stop_handlers(stop)
    run_worker(build_platform(settings, process="worker"), stop)
    return 0


def _migrate(settings: Settings) -> int:
    if not settings.migrations_dir.is_dir():
        raise SettingsError(f"DELPHI_MIGRATIONS_DIR: {settings.migrations_dir} is not a directory")
    command.upgrade(alembic_config(settings.migrations_dir, settings.effective_migrate_url), "head")
    # Loading KB v1 when no KB version exists is added with the KB versions table (#15).
    log.info("migrate.done")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="delphi")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("api", help="serve the HTTP API").set_defaults(func=_with_settings(_api))
    sub.add_parser("worker", help="run the job worker").set_defaults(func=_with_settings(_worker))
    sub.add_parser("migrate", help="apply database migrations (alembic upgrade head)").set_defaults(
        func=_with_settings(_migrate)
    )

    openapi = sub.add_parser("openapi", help="write the OpenAPI contract (docs/16)")
    openapi.add_argument("--out", default=str(OPENAPI_FILE), help="default: backend/openapi.json")
    openapi.add_argument(
        "--check", action="store_true", help="fail if the file differs instead of writing it"
    )
    openapi.set_defaults(func=_openapi)

    kb = sub.add_parser("kb", help="knowledge base tools").add_subparsers(
        dest="kb_command", required=True
    )
    validate = kb.add_parser("validate", help="validate a KB YAML tree (docs/07 §3)")
    validate.add_argument("path", nargs="?", default="kb")
    validate.set_defaults(func=_kb_validate)

    sim = sub.add_parser("sim", help="simulator tools").add_subparsers(
        dest="sim_command", required=True
    )
    frz = sim.add_parser(
        "freeze-response-models", help="regenerate the frozen response models (docs/14 §5.2)"
    )
    frz.add_argument("--kb", default=str(_REPO_KB_DIR), help="KB v1 directory (default: kb/)")
    frz.add_argument("--out", default=str(GENERATED_FILE))
    frz.add_argument("--check", action="store_true", help="fail if the file differs")
    frz.set_defaults(func=_freeze_response_models)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
