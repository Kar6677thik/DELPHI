# DELPHI

**Dynamic Evidence-Led Probing of fraud Hypotheses & Interventions**. A proof of concept for
Virtusa Jatayu Season 6, Use Case 2 (FDE — Behavioral Fraud Intelligence).

> Every fraud system asks *"how risky is this?"*. DELPHI asks *"what is the one question, check
> or action that would most quickly reveal the truth, at the lowest cost to an innocent
> customer?"*

DELPHI keeps a Bayesian belief over competing fraud hypotheses (genuine, account takeover,
coerced scam victim, mule, unexplained, …). It chooses the next probe, evidence request or
freeze by **value of information net of cost**. The same engine runs three loops:

| Loop | When | Decides |
|---|---|---|
| **Guardian** | Payment time (seconds) | Which probe to run before releasing, holding or declining a payment |
| **Navigator** | Investigation time (minutes) | Which evidence the investigator should fetch next |
| **Jatayu (Chase)** | After money has moved | Which downstream accounts to lien first, and for how much |

The **Typology Scout** clusters cases that no known hypothesis explains, and FDE engineers turn
those clusters into new, versioned typologies.

## Status

**M1 (knowledge base + decision engine) implemented.** M0's deploy half waits on the Pre-M0
checklist. The spec starts at [`docs/README.md`](docs/README.md); the milestones are in
[`docs/25-milestones.md`](docs/25-milestones.md).

## Developing

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/). [just](https://just.systems) is
optional; every recipe is a plain command.

```sh
git config core.hooksPath .githooks   # enforce the commit-authorship rule
cd backend
uv sync --frozen
uv run delphi kb validate ../kb       # validate the knowledge base
uv run pytest                         # unit, property and golden tests
```

## Source material

- [`docs/reference/research-behavioral-fraud-intelligence.md`](docs/reference/research-behavioral-fraud-intelligence.md): research, novelty check and the original DELPHI concept.
- [`docs/reference/engineering-field-guide.md`](docs/reference/engineering-field-guide.md): the engineering principles this project follows.
