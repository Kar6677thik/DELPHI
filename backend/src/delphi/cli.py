"""The ``delphi`` command (docs/04 §4): ``api``, ``worker``, ``migrate``, ``seed``, ``openapi``,
``kb validate``, ``sim freeze-response-models`` and ``sim stats``.

This module is the process entrypoint and the only importer of ``delphi.api``. Configuration
errors exit with status 2 and a message naming the variable (fail fast, docs/24 §1).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import uvicorn
from alembic import command
from pydantic import ValidationError
from sqlalchemy import Engine, text

from delphi.api.app import create_app
from delphi.api.contract import OPENAPI_FILE, openapi_document
from delphi.guardian.api import (
    LiveGuardian,
    WorkerGuardian,
    case_trail,
    live_guardian,
    worker_guardian,
)
from delphi.guardian.service import GuardianService
from delphi.kb.canonical import content_sha256
from delphi.kb.issues import KbValidationError
from delphi.kb.loader import load_and_validate
from delphi.kb.model import KnowledgeBase
from delphi.kb.store import active_version, ensure_kb_v1
from delphi.llm.evaluation import CASES_FILE, load_cases, render_report, run_eval
from delphi.llm.service import LlmService, primary_provider
from delphi.navigator.api import OUTREACH as OUTREACH_SOURCE
from delphi.navigator.api import request_evidence
from delphi.navigator.campaign_store import Limits as CampaignLimits
from delphi.navigator.jobs import register_navigator_jobs
from delphi.navigator.reviews import SweepLimits
from delphi.platform.accounts import BootstrapError, ensure_bootstrap_admin
from delphi.platform.audit import verify as audit_verify
from delphi.platform.clock import SystemClock
from delphi.platform.db import (
    alembic_config,
    current_revision,
    make_engine,
    make_session_factory,
    migration_head,
)
from delphi.platform.errors import DelphiError
from delphi.platform.events import EventHub
from delphi.platform.jobs import JobRegistry
from delphi.platform.logging import configure_logging, get_logger
from delphi.platform.metrics import SSE_CLIENTS, metrics_server
from delphi.platform.settings import Settings, SettingsError, libpq_url, load_settings
from delphi.platform.wiring import Platform, build_platform
from delphi.platform.worker import build_registry, install_stop_handlers, run_worker
from delphi.probes.api import (
    SimBeneficiaryIntel,
    SimPhoneRisk,
    SimSessionTelemetry,
    passive_executor,
    passive_fetchers,
)
from delphi.probes.registry import CLASSIFIER_NAMES
from delphi.sensing.api import SensingInference
from delphi.sensing.config import load_sensing_config
from delphi.sensing.jobs import register_sensing_jobs
from delphi.sim.api import PROFILES, dataset_exists, request_dataset
from delphi.sim.customer import LiveCustomer
from delphi.sim.dataset import generate_dataset
from delphi.sim.generator_config import load_generator_config
from delphi.sim.jobs import register_sim_jobs
from delphi.sim.reset import register_reset_job
from delphi.sim.response_models import (
    GENERATED_FILE,
    dump_yaml,
    freeze,
    load_config,
    load_response_models,
)
from delphi.sim.stats import dataset_stats, format_report
from delphi.sim.stories import register_story_job

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
    live, free_pool = live_guardian_for(platform)
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
            create_app(platform, event_hub=hub, guardian_runner=live),
            host=settings.http_host,
            port=settings.http_port,
            log_config=None,  # logging is configured by configure_logging (docs/20 §1)
            access_log=False,
            server_header=False,
        )
    finally:
        metrics.shutdown()
        free_pool()
        platform.close()
    return 0


def _active_kb(platform: Platform) -> KnowledgeBase:
    """The ACTIVE KB version from the database (TD-3 repaid by #15)."""
    with platform.sessions() as session:
        active = active_version(session)
    if active is None:
        raise SettingsError("no ACTIVE KB version: run `delphi migrate` first (docs/07 §1)")
    return active.kb


def _guardian_service(
    platform: Platform, kb: KnowledgeBase
) -> tuple[GuardianService, Callable[[], None]]:
    """The Guardian service both runners use (#95, docs/08 §2): the sensing inference as the
    prior (#92) and the simulated passive adapters over the bank (docs/08 §5). Returns it with a
    function that frees its thread pool.

    Time runs REAL (`demo_time=False`): `DELPHI_TIME_MODE` (docs/07) is not a setting yet."""
    settings = platform.settings
    timeout_ms = (
        kb.config.guardrails.passive_adapter_timeout_ms + settings.passive_statement_margin_ms
    )
    inference = SensingInference(platform.engine, platform.clock)
    telemetry = SimSessionTelemetry(platform.engine, timeout_ms)
    fetchers = passive_fetchers(
        SimBeneficiaryIntel(platform.engine, inference, timeout_ms),
        SimPhoneRisk(platform.engine, timeout_ms),
        telemetry,
        telemetry,
    )
    executor = passive_executor(settings.passive_probe_threads)
    service = GuardianService(
        platform.sessions,
        platform.clock,
        inference,
        fetchers,
        executor,
        otp_key=settings.otp_key,
    )
    return service, executor.shutdown


def live_guardian_for(
    platform: Platform, *, kb_source: Callable[[Platform], KnowledgeBase] = _active_kb
) -> tuple[LiveGuardian, Callable[[], None]]:
    """The api's Guardian runner slot (#95, docs/08 §2): an API runner for the mock phone's
    LIVE sessions. Returns the slot and a function that frees its thread pool."""
    service, free_pool = _guardian_service(platform, kb_source(platform))
    return live_guardian(service, platform.sessions, platform.clock), free_pool


@dataclass
class WorkerParts:
    """What the worker's composition root builds: the job handlers, and with the simulator's
    pool that pool and the WORKER Guardian runner (#95, #101)."""

    registry: JobRegistry
    sim_engine: Engine | None = None
    guardian: WorkerGuardian | None = None
    free_pool: Callable[[], None] | None = None

    def close(self) -> None:
        if self.guardian is not None:
            self.guardian.stop()
        if self.free_pool is not None:
            self.free_pool()
        if self.sim_engine is not None:
            self.sim_engine.dispose()


def worker_registry(
    platform: Platform, *, kb_source: Callable[[Platform], KnowledgeBase] = _active_kb
) -> WorkerParts:
    """The worker's handlers: the platform's and sensing's, plus the simulator's when it has its
    `delphi_sim` pool (`DELPHI_SIM_DATABASE_URL`, docs/17 §2). The api never gets that pool.
    The simulator's jobs use the ACTIVE KB (``kb_source``).

    With the pool comes the worker's Guardian runner (runner WORKER, docs/08 §1): simulated
    customers answer its AUTOPLAY sessions, which read ground truth through `delphi_sim`, and
    `DEMO_STORY` submits the story payments to it. The caller starts it (``guardian.start()``
    resumes its unfinished sessions, FR-GDN-14) and closes the parts."""
    registry = build_registry(platform)
    register_sensing_jobs(
        registry, engine=platform.engine, sessions=platform.sessions, clock=platform.clock
    )
    settings = platform.settings
    llm = LlmService(
        platform.sessions,
        platform.clock,
        primary_provider(settings),
        settings.llm_daily_token_budget,
    )
    register_navigator_jobs(
        registry,
        sessions=platform.sessions,
        llm=llm,
        trail=case_trail,
        clock=platform.clock,
        campaign_limits=CampaignLimits.of(settings),
        p_mule_fallback=load_sensing_config().features.mule_score_fallback,
        review_limits=SweepLimits.of(settings),
    )
    if settings.simulation_enabled and settings.sim_database_url is None:
        # The api accepts simulated calls on this flag; a worker without the simulator's pool
        # would leave them queued for ever (docs/09 §3).
        raise SettingsError(
            "DELPHI_SIMULATION_ENABLED is set but DELPHI_SIM_DATABASE_URL is not: the worker"
            " could not run the simulator's jobs."
        )
    if settings.sim_database_url is None:
        log.warning("worker.sim_jobs_disabled", reason="DELPHI_SIM_DATABASE_URL is not set")
        return WorkerParts(registry)
    kb = kb_source(platform)
    sim_engine = make_engine(
        settings.sim_database_url,
        pool_size=settings.db_pool_size,
        connect_timeout_s=settings.db_connect_timeout_s,
        application_name="delphi-worker-sim",
    )
    register_sim_jobs(
        registry,
        sim_engine=sim_engine,
        clock=platform.clock,
        kb=kb,
        allowed_profiles=settings.allowed_dataset_profiles,
        record_outreach=_outreach_recorder(platform),
    )
    service, free_pool = _guardian_service(platform, kb)
    customer = LiveCustomer(sim_engine, load_response_models(), kb.config.classifiers)
    guardian = worker_guardian(service, customer, platform.sessions, platform.clock)
    register_story_job(
        registry,
        sim_engine=sim_engine,
        clock=platform.clock,
        customer=customer,
        submit=guardian.submit,
    )
    register_reset_job(
        registry,
        sim_engine=sim_engine,
        clock=platform.clock,
        timeout_s=settings.demo_reset_timeout_s,
        drop_sessions=guardian.abandon,
    )
    return WorkerParts(registry, sim_engine, guardian, free_pool)


def _outreach_recorder(platform: Platform) -> Callable[[UUID, str], None]:
    """A simulated outreach answer recorded as the case's evidence, as the app role (docs/09
    §3): the same evidence fetch an investigator's recorded result goes through."""

    def record_outreach(case_id: UUID, outcome: str) -> None:
        with platform.sessions.begin() as session:
            request_evidence(
                session,
                platform.clock,
                case_id,
                OUTREACH_SOURCE,
                requested_by=None,
                recorded_outcome=outcome,
                note="Simulated outreach call.",
            )

    return record_outreach


def _worker(settings: Settings) -> int:
    _quiet_alembic()
    stop = threading.Event()
    install_stop_handlers(stop)
    platform = build_platform(settings, process="worker")
    parts = worker_registry(platform)
    try:
        if parts.guardian is not None:
            parts.guardian.start()
            # Stop the session tasks when asked to stop, before the platform's pool closes.
            threading.Thread(
                target=_on_stop, args=(stop, parts.guardian.stop), name="guardian-stop", daemon=True
            ).start()
        run_worker(platform, stop, registry=parts.registry)
    finally:
        parts.close()
    return 0


def _on_stop(stop: threading.Event, then: Callable[[], None]) -> None:
    stop.wait()
    then()


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
    # Then KB v1, if no KB version exists yet (docs/07 §1, docs/23 §4): idempotent, so the
    # migrate Job can run on every deploy.
    try:
        kb = load_and_validate(_REPO_KB_DIR, CLASSIFIER_NAMES)
    except KbValidationError as exc:
        for issue in exc.issues:
            print(f"delphi migrate: KB v1: {issue}", file=sys.stderr)
        return 1
    engine = make_engine(settings.effective_migrate_url, application_name="delphi-migrate")
    try:
        with make_session_factory(engine).begin() as session:
            created = ensure_kb_v1(session, SystemClock(), kb)
    finally:
        engine.dispose()
    if created is not None:
        log.info("migrate.kb_v1_loaded", kb_version_id=created, content_sha256=content_sha256(kb))
    log.info("migrate.done")
    return 0


LLM_EVAL_REPORTS = _REPO_KB_DIR.parent / "docs" / "reports"


def _llm_eval(args: argparse.Namespace) -> int:
    """docs/15 §8: run the fixed cases through the real model and write the report. Needs
    DELPHI_LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY; it uses no database."""
    try:
        settings = Settings(  # the eval reads no database; any other DELPHI_* comes from env
            database_url=os.environ.get("DELPHI_DATABASE_URL", "postgresql://unused/llm-eval")
        )
    except ValidationError as exc:
        print(f"delphi: invalid configuration: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    provider = primary_provider(settings)
    if provider is None:
        print(
            "delphi llm-eval: set DELPHI_LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY"
            " (the run calls the real model and costs money)",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR
    report = run_eval(provider, load_cases(Path(args.cases)))
    today = datetime.now(UTC).date()
    out = Path(args.out) if args.out else LLM_EVAL_REPORTS / f"llm-eval-{today.isoformat()}.md"
    out.write_text(render_report(report, on=today, git_sha=settings.version), encoding="utf-8")
    print(f"llm-eval: {'PASS' if report.passed else 'FAIL'}; report written to {out}")
    return 0 if report.passed else 1


# The schemas whose tables the restore check counts (docs/17).
RESTORE_SCHEMAS = ("delphi", "bank")


def verify_restore_report(settings: Settings) -> dict[str, object]:
    """docs/23 §8 restore drill: is the database at DELPHI_DATABASE_URL a usable restore? It is
    if its migrations are at this build's head, exactly one KB version is ACTIVE, the audit chain
    verifies end to end, and it has users. Also counts every table's rows, for the drill report."""
    engine = make_engine(settings.database_url, application_name="delphi-verify-restore")
    try:
        with make_session_factory(engine)() as session:
            conn = session.connection()
            names = conn.execute(
                text(
                    "SELECT table_schema || '.' || table_name FROM information_schema.tables"
                    " WHERE table_schema = ANY(:schemas) AND table_type = 'BASE TABLE'"
                    " ORDER BY 1"
                ),
                {"schemas": list(RESTORE_SCHEMAS)},
            ).scalars()
            # The names come from the catalog, not from input; quoted as identifiers anyway.
            counts = {
                name: int(
                    conn.execute(
                        text(
                            "SELECT count(*) FROM {}.{}".format(  # nosec B608
                                *(f'"{p}"' for p in name.split("."))
                            )
                        )
                    ).scalar_one()
                )
                for name in names
            }
            revision = current_revision(conn)
            active = conn.execute(
                text("SELECT count(*) FROM delphi.kb_versions WHERE status = 'ACTIVE'")
            ).scalar_one()
            chain = audit_verify(session)
    finally:
        engine.dispose()
    head = migration_head(settings.migrations_dir)
    checks = {
        "migrations_at_head": revision is not None and revision == head,
        "one_active_kb": active == 1,
        "audit_chain_verifies": chain.ok,
        "has_users": counts.get("delphi.users", 0) > 0,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "revision": revision,
        "head": head,
        "audit_entries_checked": chain.checked,
        "audit_first_bad_id": chain.first_bad_id,
        "row_counts": counts,
    }


def _verify_restore(settings: Settings) -> int:
    report = verify_restore_report(settings)
    # One JSON line, like the log lines around it on stdout.
    print(json.dumps({"event": "verify_restore.report", **report}, sort_keys=True))
    return 0 if report["ok"] else 1


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

    llm_eval = sub.add_parser(
        "llm-eval", help="evaluate the LLM path against the real model (docs/15 §8; costs money)"
    )
    llm_eval.add_argument("--cases", default=str(CASES_FILE), help="the eval cases (JSONL)")
    llm_eval.add_argument("--out", help="default: docs/reports/llm-eval-<date>.md")
    llm_eval.set_defaults(func=_llm_eval)

    sub.add_parser(
        "verify-restore",
        help="check that the database at DELPHI_DATABASE_URL is a usable restore (docs/23 §8)",
    ).set_defaults(func=_with_settings(_verify_restore))
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
