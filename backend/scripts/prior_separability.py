"""How separable is fraud from payment features alone? (#148 analysis, #166's check.)

Generates a profile, writes it into a throwaway PostgreSQL database, trains the prior exactly as
`tests/integration/test_sensing_prior.py` does (observed labels by the end of the history, the
matured window, docs/13 §4), then reports, on the test split:

- the prior's macro AUC, per-class AUC and ECE (docs/13 §4's gate: macro AUC ≥ 0.85);
- each feature's single-feature AUC, fraud class against GENUINE (max(AUC, 1 − AUC)): #166's
  target (docs/14 §1) is none above 0.90 on `eval`; the macro AUC is reported, not gated;
- LightGBM gain shares;
- per-scenario medians of the timing and account-age features (ground truth joined here only,
  for analysis: this script is a development tool, not application code).

Usage, from backend/ (needs DELPHI_TEST_DATABASE_URL, a server where databases can be created):

    uv run python -m scripts.prior_separability --profile demo
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from sklearn.metrics import roc_auc_score
from sqlalchemy import create_engine, make_url, select, text
from tests.conftest import KB_DIR
from tests.integration._db import engine_as, upgrade

from delphi.kb.loader import load_and_validate
from delphi.platform.settings import sqlalchemy_url
from delphi.probes.registry import CLASSIFIER_NAMES
from delphi.sensing.config import load_sensing_config
from delphi.sensing.features import prior_feature_names, to_us
from delphi.sensing.offline import OfflineHistories, load_bank_frames
from delphi.sensing.prior import (
    TEST,
    TRAIN,
    VALIDATION,
    LabelObservation,
    Split,
    encode_labels,
    matured_split,
    observed_labels,
    train_prior,
)
from delphi.sim import api as bank
from delphi.sim import truth
from delphi.sim.dataset import generate_dataset
from delphi.sim.generator_config import history_end, load_generator_config
from delphi.sim.writer import write_dataset

DEMO_CREATED_AT = datetime(2026, 11, 1, tzinfo=UTC)
WATCH = ("hour_deviation", "payee_account_age_days", "payee_age_days", "is_new_payee")


def _database() -> tuple[str, Any]:
    admin_url = sqlalchemy_url(os.environ["DELPHI_TEST_DATABASE_URL"])
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    name = f"delphi_sep_{secrets.token_hex(4)}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)

    def drop() -> None:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()

    return url, drop


def analyse(profile: str, leave_out: tuple[str, ...] = ()) -> dict[str, Any]:  # noqa: PLR0915 - a linear report
    started = time.perf_counter()
    kb = load_and_validate(KB_DIR, CLASSIFIER_NAMES)
    generator = load_generator_config()
    settings = load_sensing_config()
    url, drop = _database()
    try:
        upgrade(url)
        sim = engine_as(url, "delphi_sim")
        created = DEMO_CREATED_AT if profile == "demo" else None
        with sim.begin() as conn:
            write_dataset(conn, generate_dataset(generator, profile, kb=kb, created_at=created))
        end = history_end(generator, profile, created_at=created)
        params = generator.profiles[profile]
        with sim.connect() as conn:
            ls = truth.label_schedule
            observations = [
                LabelObservation(
                    str(r.subject_id), r.hypothesis_key, r.label_source, to_us(r.due_at)
                )
                for r in conn.execute(
                    select(
                        ls.c.subject_id, ls.c.hypothesis_key, ls.c.label_source, ls.c.due_at
                    ).where(ls.c.subject_type == "PAYMENT")
                )
            ]
            pt, si = truth.payment_truth, truth.scenario_instances
            scenario_of = {
                str(r.payment_id): r.scenario_type
                for r in conn.execute(
                    select(pt.c.payment_id, si.c.scenario_type).join(
                        si, si.c.id == pt.c.scenario_instance_id
                    )
                )
            }
        sim.dispose()
        labels = observed_labels(
            observations, cutoff_us=to_us(end), precedence=settings.prior.label_precedence
        )
        app = engine_as(url, "delphi_app")
        with app.connect() as conn:
            p, a = bank.payments, bank.accounts
            rows = conn.execute(
                select(p.c.id, p.c.initiated_at)
                .join(a, a.c.id == p.c.payer_account_id)
                .where(a.c.customer_id.is_not(None))
                .order_by(p.c.initiated_at, p.c.id)
            ).all()
            frames = load_bank_frames(conn)
        app.dispose()
    finally:
        drop()

    ids = [str(r.id) for r in rows if str(r.id) in labels]
    at = np.array([to_us(r.initiated_at) for r in rows if str(r.id) in labels], dtype=np.int64)
    split = matured_split(
        at,
        history_start_us=to_us(end - timedelta(days=params.history_days)),
        cutoff_us=to_us(end),
        maturity_days=params.maturity_days,
        parts=settings.prior.split_parts,
    )
    classes = settings.prior.classes
    codes, known = encode_labels([labels[i] for i in ids], classes)
    names = [f for f in prior_feature_names(settings.features) if f not in leave_out]
    offline = OfflineHistories(frames, settings.features)
    parts: dict[int, tuple[Split, list[str], Any]] = {}
    for code in (TRAIN, VALIDATION, TEST):
        mask = (split == code) & known
        chosen = [i for i, m in zip(ids, mask, strict=True) if m]
        frame = offline.features(chosen)
        matrix = frame[names].to_numpy(dtype=np.float64).reshape(len(chosen), len(names))
        parts[code] = (Split(matrix, codes[mask]), chosen, frame)
    model, gate = train_prior(
        parts[TRAIN][0],
        parts[VALIDATION][0],
        parts[TEST][0],
        names,
        settings.prior,
        profile=profile,
    )
    test_split, test_ids, test_frame = parts[TEST]

    single: dict[str, dict[str, float]] = {}
    for c_index, c in enumerate(classes):
        if c == "GENUINE":
            continue
        mask = (test_split.labels == 0) | (test_split.labels == c_index)
        y = test_split.labels[mask] == c_index
        if y.sum() == 0 or y.all():
            continue
        for f_index, f in enumerate(names):
            x = test_split.features[mask, f_index]
            auc = float(roc_auc_score(y, x))
            single.setdefault(f, {})[c] = round(max(auc, 1 - auc), 3)
    worst = sorted(((f, max(v.values())) for f, v in single.items()), key=lambda t: -t[1])[:8]
    per_class = {
        c: sorted(((f, v[c]) for f, v in single.items() if c in v), key=lambda t: -t[1])[:6]
        for c in classes
        if c != "GENUINE"
    }

    gain = model.booster.feature_importance(importance_type="gain")
    gain_share = sorted(
        ((f, round(float(g / gain.sum()), 3)) for f, g in zip(names, gain, strict=True)),
        key=lambda t: -t[1],
    )[:8]

    # What the model combines: for each fraud class, the features whose contribution to that
    # class's raw score most separates its rows from GENUINE payments to new payees (the
    # genuine payments that look most like fraud).
    contrib = model.booster.predict(test_split.features, pred_contrib=True)
    width = len(names) + 1
    new_genuine = (test_split.labels == 0) & (
        test_split.features[:, names.index("is_new_payee")] > 0
    )
    contrib_gap: dict[str, list[tuple[str, float]]] = {}
    for c_index, c in enumerate(classes):
        rows = test_split.labels == c_index
        if c == "GENUINE" or not rows.any() or not new_genuine.any():
            continue
        block = contrib[:, c_index * width : c_index * width + len(names)]
        gap = block[rows].mean(axis=0) - block[new_genuine].mean(axis=0)
        contrib_gap[c] = sorted(
            ((f, round(float(g), 2)) for f, g in zip(names, gap, strict=True)), key=lambda t: -t[1]
        )[:8]

    medians: dict[str, dict[str, Any]] = defaultdict(dict)
    by_scenario: dict[str, list[str]] = defaultdict(list)
    for pid in test_ids:
        by_scenario[scenario_of.get(pid, "?")].append(pid)
    for scenario, pids in sorted(by_scenario.items()):
        sub = test_frame.loc[pids]
        for f in WATCH:
            medians[scenario][f] = round(float(sub[f].median()), 2)
        q = sub["payee_age_days"].quantile([0.1, 0.25, 0.5, 0.75, 0.9])
        medians[scenario]["payee_age_days_q10_90"] = [round(float(v), 3) for v in q]
        medians[scenario]["n"] = len(pids)
        new = sub[sub["is_new_payee"] > 0]["payee_age_days"] * 1440
        medians[scenario]["new_payee_age_minutes_q"] = [
            round(float(v), 1) for v in new.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
        ]

    report = model.report["test"]
    return {
        "profile": profile,
        "macro_auc": round(report["macro_auc"], 4) if report else None,
        "auc": report["auc"] if report else None,
        "ece": round(report["ece"], 4) if report else None,
        "gate": {"passed": gate.passed, "reasons": list(gate.reasons)},
        "n": {"train": len(parts[TRAIN][1]), "test": len(test_ids)},
        "single_feature_auc_top": worst,
        "single_feature_auc_by_class": per_class,
        "gain_share_top": gain_share,
        "contribution_gap_vs_new_payee_genuine": contrib_gap,
        "medians_by_scenario": medians,
        "seconds": round(time.perf_counter() - started, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="demo", choices=["test", "demo", "eval"])
    parser.add_argument("--drop", default="", help="comma-separated features to leave out")
    args = parser.parse_args()
    report = analyse(args.profile, tuple(f for f in args.drop.split(",") if f))
    for key, value in report.items():
        print(f"{key}: {json.dumps(value, default=str)}")


if __name__ == "__main__":
    main()
