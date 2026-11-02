"""Cross-object KB validation rules (docs/07-knowledge-base.md §3).

Every rule reports with a stable ``code``; tests assert on codes, not messages.
"""

from __future__ import annotations

import math

from delphi.kb.issues import Issue
from delphi.kb.model import GENUINE, UNEXPLAINED, KnowledgeBase, LossTable

PROB_FLOOR = 1e-4
ALPHA_FLOOR = 0.01
SUM_TOLERANCE = 1e-6
ESS_MIN, ESS_MAX = 2.0, 500.0
OPEN_MASS_MAX = 0.9


def validate_kb(kb: KnowledgeBase, classifier_registry: frozenset[str]) -> list[Issue]:
    issues: list[Issue] = []
    _check_orders(kb, issues)
    _check_genuine(kb, issues)
    _check_likelihoods(kb, issues)
    _check_open_mass(kb, issues)
    _check_losses(kb, issues)
    _check_question_bank(kb, issues)
    _check_classifiers(kb, classifier_registry, issues)
    return issues


def _check_orders(kb: KnowledgeBase, issues: list[Issue]) -> None:
    for name, order, keys in (
        ("hypothesis_order", kb.hypothesis_order, set(kb.hypotheses)),
        ("source_order", kb.source_order, set(kb.sources)),
    ):
        if len(set(order)) != len(order):
            issues.append(Issue(f"kb.yaml:{name}", "ORDER_DUPLICATE", "duplicate entries"))
        if set(order) != keys:
            missing = sorted(keys - set(order))
            extra = sorted(set(order) - keys)
            issues.append(
                Issue(
                    f"kb.yaml:{name}",
                    "ORDER_MISMATCH",
                    f"not listed: {missing}; no such file: {extra}",
                )
            )
    if UNEXPLAINED in kb.hypothesis_order:
        issues.append(
            Issue(
                "kb.yaml:hypothesis_order",
                "UNEXPLAINED_DEFINED",
                "UNEXPLAINED is implicit and must not be listed",
            )
        )


def _check_genuine(kb: KnowledgeBase, issues: list[Issue]) -> None:
    genuine = [h for h in kb.hypotheses.values() if h.kind == "GENUINE"]
    if len(genuine) != 1 or genuine[0].key != GENUINE:
        issues.append(
            Issue(
                "hypotheses",
                "GENUINE_DEFINITION",
                "exactly one hypothesis of kind GENUINE, keyed GENUINE, is required",
            )
        )
    for profile in kb.decision_profiles.values():
        g = kb.hypotheses.get(GENUINE)
        if g is not None and profile.context not in g.contexts:
            issues.append(
                Issue(
                    f"decision_profiles/{profile.key}.yaml",
                    "PROFILE_NO_GENUINE",
                    "GENUINE must apply to every profile's context",
                )
            )


def _check_likelihoods(kb: KnowledgeBase, issues: list[Issue]) -> None:
    for h in kb.hypotheses.values():
        for source_key, row in h.likelihoods.items():
            path = f"hypotheses/{h.key}.yaml:likelihoods.{source_key}"
            source = kb.sources.get(source_key)
            if source is None:
                issues.append(Issue(path, "LIKELIHOOD_UNKNOWN_SOURCE", "no such source"))
                continue
            if len(row.probs) != len(source.outcomes):
                issues.append(
                    Issue(
                        path,
                        "LIKELIHOOD_LENGTH",
                        f"{len(row.probs)} probs for {len(source.outcomes)} outcomes",
                    )
                )
                continue
            if any(p < PROB_FLOOR for p in row.probs):
                issues.append(
                    Issue(path, "LIKELIHOOD_FLOOR", f"every prob must be >= {PROB_FLOOR}")
                )
            if not math.isclose(sum(row.probs), 1.0, abs_tol=SUM_TOLERANCE):
                issues.append(Issue(path, "LIKELIHOOD_SUM", f"probs sum to {sum(row.probs)!r}"))
            if not ESS_MIN <= row.ess <= ESS_MAX:
                issues.append(Issue(path, "ESS_RANGE", f"ess must be in [{ESS_MIN}, {ESS_MAX}]"))
            if any(a < ALPHA_FLOOR for a in row.alpha):
                issues.append(Issue(path, "ALPHA_FLOOR", f"every alpha must be >= {ALPHA_FLOOR}"))


def _check_open_mass(kb: KnowledgeBase, issues: list[Issue]) -> None:
    for h in kb.hypotheses.values():
        path = f"hypotheses/{h.key}.yaml:open_mass_share"
        if h.kind == "GENUINE":
            if h.open_mass_share is not None:
                issues.append(Issue(path, "OPEN_MASS_SHARE", "GENUINE has no open_mass_share"))
        elif h.open_mass_share is None or not 0 < h.open_mass_share <= OPEN_MASS_MAX:
            issues.append(
                Issue(
                    path,
                    "OPEN_MASS_SHARE",
                    f"typologies need open_mass_share in (0, {OPEN_MASS_MAX}]",
                )
            )


def _check_decision_cells(
    table: LossTable, row_key: str, decisions: tuple[str, ...], path: str, issues: list[Issue]
) -> None:
    cells = table.get(row_key)
    if cells is None:
        issues.append(Issue(path, "LOSS_MISSING", f"no loss cells for {row_key}"))
    elif set(cells) != set(decisions):
        issues.append(
            Issue(
                path,
                "LOSS_DECISIONS_MISMATCH",
                f"{row_key}: cells {sorted(cells)} != decisions {sorted(decisions)}",
            )
        )


def _check_losses(kb: KnowledgeBase, issues: list[Issue]) -> None:
    for profile in kb.decision_profiles.values():
        ppath = f"decision_profiles/{profile.key}.yaml:loss"
        if set(profile.loss) != {GENUINE, UNEXPLAINED}:
            issues.append(
                Issue(
                    ppath,
                    "PROFILE_LOSS_KEYS",
                    "profile loss must define exactly GENUINE and UNEXPLAINED",
                )
            )
        for row in (GENUINE, UNEXPLAINED):
            _check_decision_cells(profile.loss, row, profile.decisions, ppath, issues)

    for h in kb.hypotheses.values():
        hpath = f"hypotheses/{h.key}.yaml:loss"
        if h.kind == "GENUINE":
            if h.loss:
                issues.append(
                    Issue(hpath, "LOSS_ON_GENUINE", "GENUINE loss cells live in decision_profiles")
                )
            continue
        required = {p.key for p in kb.decision_profiles.values() if p.context in h.contexts}
        for missing in sorted(required - set(h.loss)):
            issues.append(Issue(hpath, "LOSS_PROFILE_MISSING", f"no column for {missing}"))
        for extra in sorted(set(h.loss) - required):
            issues.append(
                Issue(
                    hpath,
                    "LOSS_PROFILE_UNEXPECTED",
                    f"{extra} does not apply to this typology's contexts",
                )
            )
        for profile_key in sorted(required & set(h.loss)):
            decisions = kb.decision_profiles[profile_key].decisions
            _check_decision_cells(h.loss, profile_key, decisions, hpath, issues)


def _check_question_bank(kb: KnowledgeBase, issues: list[Issue]) -> None:
    question_probes = {s.key: s for s in kb.sources.values() if s.question}
    for entry in kb.question_bank.values():
        path = f"question_bank/{entry.probe_key}.yaml"
        source = question_probes.get(entry.probe_key)
        if source is None:
            issues.append(
                Issue(path, "QUESTION_BANK_UNKNOWN_PROBE", "probe_key is not a question probe")
            )
            continue
        ids = [v.variant_id for v in entry.variants]
        if len(set(ids)) != len(ids):
            issues.append(
                Issue(path, "QUESTION_VARIANT_DUPLICATE_ID", "variant ids must be unique")
            )
        expected = set(source.answer_options or ())
        for v in entry.variants:
            keys = [o.key for o in v.options]
            if len(set(keys)) != len(keys) or set(keys) != expected:
                issues.append(
                    Issue(
                        f"{path}:{v.variant_id}",
                        "QUESTION_OPTIONS_MISMATCH",
                        f"option keys {sorted(keys)} != {sorted(expected)}",
                    )
                )
    for key in sorted(question_probes):
        bank = kb.question_bank.get(key)
        approved_en = bank is not None and any(
            v.language == "en" and v.status == "APPROVED" for v in bank.variants
        )
        if not approved_en:  # docs/07 §3 rule 7
            issues.append(
                Issue(
                    f"question_bank/{key}.yaml",
                    "QUESTION_NO_APPROVED_EN",
                    "every question probe needs an APPROVED English variant",
                )
            )


def _check_classifiers(kb: KnowledgeBase, registry: frozenset[str], issues: list[Issue]) -> None:
    for source in kb.sources.values():
        if source.classifier not in registry:  # docs/07 §3 rule 8
            issues.append(
                Issue(
                    f"sources/{source.key}",
                    "CLASSIFIER_UNKNOWN",
                    f"classifier {source.classifier!r} is not registered in code",
                )
            )
