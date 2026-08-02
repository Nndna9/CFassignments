# Implementation Plan

---

## Shipped ✅

| Layer | State |
|---|---|
| Concept registry | ~38 canonical concepts, 7 derivation rules, 4 accounting-identity validators |
| EDGAR ingest | ticker → CIK → submissions → companyfacts, backoff on 429/5xx, cached, graceful fallback to demo |
| Normalization | Tag resolver, restatement handling, sign normalization, derivation DAG, validators, quality score |
| Analytics | 28 ratios, Altman Z, Piotroski F, driver percentile statistics |
| Scenario engine | Driver-based DCF, calibrated 3-scenario set, tornado sensitivity, WACC × terminal-growth grid, bisection goal-seek |
| Forensics | 12 red-flag rules, severity ranking, composite risk score |
| AI layer | Gemini, NVIDIA NIM, Anthropic and OpenAI behind one interface; model auto-discovery, JSON enforcement with no-json-mode fallback, reasoning-content separation, response caching, numeric verifier, 5 features each with a deterministic fallback |
| UI | 5 tabs, design system, sparkline KPI cards, provenance chips, status rows, staged loading, CSV export |
| Offline mode | Synthetic internally-consistent demo company; runs with no network and no keys |

**Every mandatory dashboard component in the brief is covered.** What follows raises
quality, not compliance.

---

## Phase 0 — Before you touch anything (2 minutes)

- [ ] Set `SEC_USER_AGENT` at the top of `app.py` to your real name and email — live EDGAR
      returns 403 without it
- [ ] Keep the GitHub repo **private** while the keys sit in the file
- [ ] Streamlit Cloud → app → Settings → Sharing → restrict viewers, or anyone with the URL
      burns your NVIDIA quota
- [ ] Blank the keys in the CONFIG block before submitting the file to anyone

---

## Phase 1 — Validate against live EDGAR (2–3 days) 🔴 then this

The engine has only seen synthetic data. Real filings will break assumptions.

- [ ] Confirm `SEC_USER_AGENT` is set to your real name and email
- [ ] Run `AAPL MSFT NVDA WMT XOM PG KO CAT UNH HD` (quick-pick buttons cover most)
- [ ] Confirm the sidebar status rows show a green dot for Price feed and AI layer
- [ ] Record the data quality score for each; investigate anything below 90
- [ ] Note every unmapped tag surfaced in the Data extraction tab; add the material ones to `CONCEPTS`
- [ ] Check fiscal-year labelling on off-calendar filers (Apple Sep, Microsoft Jun, Nvidia Jan)
- [ ] Spot-check three figures per company against the actual 10-K on sec.gov
- [ ] Pick the company for the report and presentation

**Exit criterion:** five tickers score ≥ 95% data quality and three spot-checks match the filing.

---

## Phase 2 — Demo hardening (1 day)

- [ ] Run the chosen company once so it's cached, then verify it loads with the network off
- [ ] Generate and screenshot every AI output ahead of time (they're cached — no API spend on stage)
- [ ] Screenshot pack for the report's dashboard section
- [ ] Record a 90-second screen capture as presentation backup

---

## Phase 3 — Close the gaps the README admits (2–3 days)

- [ ] **Persist AI concept mappings.** Accepted mappings should append to `CONCEPTS` (or a
      small JSON sidecar) so the system gets deterministic over time. This is currently the
      extraction feature's headline claim and is not implemented — close it or soften the claim.
- [ ] **Confidence threshold + review UI** for low-confidence mappings
- [ ] **Capture five Q&A pairs** from a live assistant session for the brief's Section 6
- [ ] **Basic tests** — a `test_app.py` asserting the balance sheet balances, capex is positive,
      scenarios are ordered, and the verifier rejects a fabricated figure

---

## Phase 4 — Optional differentiation, in value order

| # | Item | Effort | Why |
|---|---|---|---|
| 1 | Peer benchmarking by SIC + size, ratio z-scores | 2 d | Turns single-company analysis into relative analysis |
| 2 | CSV upload path | 1 d | Private, hypothetical and non-US companies; universal offline escape hatch |
| 3 | PDF / PPTX memo export | 1.5 d | Presentation-worthy deliverable |
| 4 | 10-Q quarterly ingestion | 2 d | ~40 observations makes forecasting statistically defensible |
| 5 | Beneish M-Score | 1 d | Completes the forensic triad alongside Altman and Piotroski |
| 6 | Financial-sector template | 2 d | Removes the SIC 6000–6799 exclusion |
| 7 | Split into a package + FastAPI layer | 2 d | The "API-first platform" interview story |

Item 7 is deliberately last. The single file is the right call for a prototype; splitting it
is a mechanical refactor once the logic is proven, and the section banners in `app.py` are
already the module boundaries.

---

## Rubric checklist

Track this, not the backlog. The backlog is ambition; this is marks.

**Individual report (1,300–1,650 words, 10 sections — ~140 words each)**
- [ ] Executive summary
- [ ] Financial problem
- [ ] Corporate finance concepts — DCF, WACC, terminal value, FCF, book vs market value
- [ ] Data and assumptions — classify historical / current / forecast / user-entered / AI-generated
- [ ] Financial calculations — minimum three, formulas shown (DCF, ratio set, Altman Z)
- [ ] AI features — five, each with does / uses / produces / helps / limitation *(the table in README.md is written to this exact format — adapt it, don't start over)*
- [ ] Dashboard explanation
- [ ] Scenario and sensitivity analysis — which variable matters most, when the decision flips
- [ ] AI limitations and ethical risks — verifier, quality score, human accountability
- [ ] Final recommendation

This is a summary document at that word count. Resist expanding it.

**Dashboard** — all six mandatory components are shipped. Re-check against the brief before submitting.

**Presentation (7–10 min, 8–15 slides)**

| Slide | Content |
|---|---|
| 1 | Problem: statements are comparable in principle, incomparable in practice |
| 2 | Pipeline diagram: XBRL → normalize → validate → analyse → recommend |
| 3 | Tag heterogeneity and the concept registry |
| 4 | Data quality score and the identity checks behind it |
| 5–6 | Live demo: KPI cards → cash flow → earnings quality |
| 7 | Scenario lab: three calibrated cases |
| 8 | Sensitivity: which variable actually matters |
| 9 | Risk panel and the forensic signals |
| 10 | The five AI features and the Python-versus-LLM split |
| 11 | The numeric verifier catching a fabricated figure |
| 12 | Provenance: any number → XBRL tag → sec.gov |
| 13 | Recommendation and the condition that reverses it |
| 14 | Limitations and ethics |
| 15 | Roadmap |

Slide 11 is the one people remember. Prepare a deliberately fabricated figure to demonstrate.

---

## Work split (group of 4)

| Owner | Scope |
|---|---|
| A | `CONCEPTS` extension, EDGAR validation, spot-checks against filings |
| B | Analytics, scenario engine, sensitivity, forensics |
| C | AI layer, prompts, verifier, Q&A capture |
| D | UI, charts, screenshots, presentation, demo hardening |

Everyone writes their own report. Shared: the repo, the chosen company, the cached data.

---

## Demo protocol

1. Launch on the demo company. Do **not** ingest a new ticker as the opening move.
2. Walk the frozen company end to end.
3. Take an audience ticker as the finale, with demo mode visible as the safety net.
   If it fails, that's an SEC rate limit, not a bug — say so and switch back.
4. Keep the backup video ready.

---

## Open decisions

- [ ] Target company for the report and presentation
- [ ] LLM provider and budget ceiling
- [ ] Submission deadline → determines how much of Phase 4 is reachable
