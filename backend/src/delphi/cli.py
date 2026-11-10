"""The ``delphi`` command (docs/04 §4): ``api``, ``worker``, ``migrate``, ``seed``, ``openapi``,
``kb validate``, ``sim freeze-response-models`` and ``sim stats``.

This module is the process entrypoint and the only importer of ``delphi.api``. Configuration
errors exit with status 2 and a message naming the variable (fail fast, docs/24 §1).
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from alembic import command
from sqlalchemy import Engine

from delphi.api.app import create_app
from delphi.api.contract import OPENAPI_FILE, openapi_document
from delphi.kb.canonical import content_sha256
from delphi.kb.issues import KbValidationError
from delphi.kb.loader import load_and_validate
from delphi.platform.accounts import BootstrapError, ensure_bootstrap_admin
from delphi.platform.db import alembic_config, make_engine
from delphi.platform.errors import DelphiError
from delphi.platform.events import EventHub
from delphi.platform.jobs import JobRegistry
from delphi.platform.logging import configure_logging, get_logger
from delphi.platform.metrics import SSE_CLIENTS, metrics_server
from delphi.platform.settings import Settings, SettingsError, libpq_url, load_settings
from delphi.platform.wiring import Platform, build_platform
from delphi.platform.worker import build_registry, install_stop_handlers, run_worker
from delphi.probes.registry import CLASSIFIER_NAMES
from delphi.sim.api import PROFILES, dataset_exists, request_dataset
from delphi.sim.dataset import generate_dataset
from delphi.sim.generator_config import load_generator_config
from delphi.sim.jobs import register_sim_jobs
from delphi.sim.response_models import GENERATED_FILE, dump_yaml, freeze, load_config
from delphi.sim.stats import dataset_stats, format_report

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
    out.write_text(document, encoding="utf-8", newline="\n")
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
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {out}")
    return 0


def _sim_stats(args: argparse.Namespace) -> int:
    """Generate a profile in memory and print its stats report (docs/25 M2)."""
    kb = load_and_validate(Path(args.kb), CLASSIFIER_NAMES)
    config = load_generator_config()
    created_at = datetime.now(UTC) if args.profile == "demo" else None
    dataset = generate_dataset(config, args.profile, kb=kb, created_at=created_at)
    print(format_report(dataset_stats(dataset, config)))
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
    hub = EventHub(platform.sessions, libpq_url(settings.database_url))
    try:
        password = settings.bootstrap_admin_password
        with platform.sessions.begin() as session:
            ensure_bootstrap_admin(
                session,
                platform.clock,
                username=settings.bootstrap_admin_username,
                password=password.get_secret_value() if password else None,
            )
    except BootstrapError as exc:
        platform.close()
        raise SettingsError(str(exc)) from None
    SSE_CLIENTS.set_function(lambda: hub.subscriber_count)
    metrics = metrics_server(settings.metrics_port)
    threading.Thread(target=metrics.serve_forever, name="metrics", daemon=True).start()
    try:
        uvicorn.run(
            create_app(platform, event_hub=hub),
            host=settings.http_host,
            port=settings.http_port,
            log_config=None,  # logging is configured by configure_logging (docs/20 §1)
            access_log=False,
            server_header=False,
        )
    finally:
        metrics.shutdown()
        platform.close()
    return 0


def worker_registry(platform: Platform) -> tuple[JobRegistry, Engine | None]:
    """The worker's handlers: the platform's, plus the simulator's when the worker has its
    `delphi_sim` pool (`DELPHI_SIM_DATABASE_URL`, docs/17 §2). The api never gets that pool.
    Returns the sim engine too, for the caller to dispose of."""
    registry = build_registry(platform)
    settings = platform.settings
    if settings.sim_database_url is None:
        log.warning("worker.sim_jobs_disabled", reason="DELPHI_SIM_DATABASE_URL is not set")
        return registry, None
    sim_engine = make_engine(
        settings.sim_database_url,
        pool_size=settings.db_pool_size,
        connect_timeout_s=settings.db_connect_timeout_s,
        application_name="delphi-worker-sim",
    )
    kb = load_and_validate(_REPO_KB_DIR, CLASSIFIER_NAMES)  # KB v1 until #15 (TD-3)
    register_sim_jobs(
        registry,
        sim_engine=sim_engine,
        clock=platform.clock,
        kb=kb,
        allowed_profiles=settings.allowed_dataset_profiles,
    )
    return registry, sim_engine


def _worker(settings: Settings) -> int:
    _quiet_alembic()
    stop = threading.Event()
    install_stop_handlers(stop)
    platform = build_platform(settings, process="worker")
    registry, sim_engine = worker_registry(platform)
    try:
        run_worker(platform, stop, registry=registry)
    finally:
        if sim_engine is not None:
            sim_engine.dispose()
    return 0


def _seed(settings: Settings, args: argparse.Namespace) -> int:
    """Queue `GENERATE_DATASET` for the worker and exit (docs/23 §4: the PostSync hook runs it on
    every deploy). The key names the profile and seed, so running it again is harmless."""
    seed = (
        args.seed if args.seed is not None else load_generator_config().profiles[args.profile].seed
    )
    params: dict[str, object] = {"profile": args.profile, "seed": seed}
    if args.personas is not None:
        params["personas"] = args.personas
    platform = build_platform(settings, process="seed")
    try:
        with platform.sessions.begin() as session:
            if args.if_empty and dataset_exists(session):
                print("delphi seed: the database already holds a dataset; nothing to do")
                return 0
            queued = request_dataset(
                session,
                platform.clock,
                idempotency_key=f"GENERATE_DATASET:{args.profile}:seed-{seed}",
                params=params,
                allowed_profiles=settings.allowed_dataset_profiles,
            )
    except DelphiError as exc:
        print(f"delphi seed: {exc}", file=sys.stderr)
        return 1
    finally:
        platform.close()
    state = (
        "queued"
        if queued.created
        else "queued again (it had failed)"
        if queued.requeued
        else "already queued"
    )
    print(f"GENERATE_DATASET job {queued.job_id} {state} ({args.profile}, seed {seed})")
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
    seed = sub.add_parser("seed", help="queue dataset generation for the worker (docs/23 §4)")
    seed.add_argument("--profile", default="demo", choices=PROFILES)
    seed.add_argument("--seed", type=int, help="default: the profile's seed")
    seed.add_argument(
        "--personas",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="default: on for the profiles personas.yaml lists (demo)",
    )
    seed.add_argument(
        "--if-empty", action="store_true", help="do nothing if a dataset is already there"
    )
    seed.set_defaults(func=lambda args: _with_settings(lambda s: _seed(s, args))(args))

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
    stats = sim.add_parser("stats", help="generate a profile and print its stats (docs/25 M2)")
    stats.add_argument("--profile", default="test", choices=("test", "demo", "eval"))
    stats.add_argument("--kb", default=str(_REPO_KB_DIR), help="KB v1 directory (default: kb/)")
    stats.set_defaults(func=_sim_stats)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
