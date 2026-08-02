# Financial Statement Intelligence

An AI-enabled corporate finance dashboard. Enter a ticker; it pulls the company's SEC XBRL
filings, normalizes them into comparable statements, computes ratios, runs three calibrated
scenarios, flags forensic risk signals, and produces a decision recommendation.

Works on **any US 10-K filer** — no company-specific code.

Built for the *AI-Enabled Corporate Finance Decision Dashboard* group project
(Topic 1: AI Financial Statement and Cash-Flow Analysis Dashboard).

---

## The design claim

> **No number displayed in this app is produced by a language model.**
> Every figure traces to an XBRL tag in an SEC filing or to a formula in `app.py`.
> The model maps labels, proposes assumptions, and writes prose — and any figure it
> cites is verified against the calculation engine before it reaches the screen.

That constraint drives the whole design, and it is the honest answer to the hallucination
and accountability questions the brief asks about in its ethics section.

---

## Run it

```bash
pip install -r requirements.txt
streamlit run app.py
```

Opens on a **synthetic demo company** — no network, no setup. Every validator, ratio,
scenario and AI fallback runs against the real code path.

### One thing to edit

Open `app.py` and change the first line of the CONFIG block:

```python
SEC_USER_AGENT = "Your Name your.email@university.edu"
```

The SEC blocks requests that do not identify the caller, so live EDGAR mode returns 403
without this. Everything else is already filled in.

### Configuration

Everything lives in one block at the top of `app.py`:

```python
SEC_USER_AGENT  = "..."   # ⚠ change to your name and email
NVIDIA_API_KEY  = "..."   # powers the five AI features
FINNHUB_API_KEY = "..."   # share price → market value, P/B, P/E
GEMINI_API_KEY  = ""      # optional alternative
LLM_PROVIDER    = "auto"  # auto | nvidia | gemini | anthropic | openai | off
```

Streamlit secrets and environment variables override these if you ever want the keys out of
the file, but nothing is required for it to run.

Provider order under `auto` is **Gemini → NVIDIA → Anthropic → OpenAI**, first key found.
With none set, every AI feature falls back to deterministic output and nothing breaks.

> **Before making the repo public or submitting the file**, blank the keys. GitHub reports
> leaked keys to the provider, which revokes them automatically.

### NVIDIA NIM

NIM is OpenAI-compatible, so it is called over the same REST shape — no SDK required. Two
things are handled for you:

- **Reasoning models split their output.** Nemotron returns the chain of thought in
  `reasoning_content` and the answer in `content`. Only `content` is read, so thinking never
  reaches the JSON parser or the numeric verifier.
- **`response_format` is not universal.** If a deployment rejects JSON mode with a 400, the
  call retries without it — the prompt already demands JSON and the parser extracts the object.

The thinking budget is deliberately small (128–512 tokens against a 3,000-token ceiling).
Every thinking token competes with the answer, and these are classification and short-drafting
tasks.

**Quality note:** `nemotron-nano-9b-v2` is a 9B model. It is reliable for the scenario-driver
and tag-classification features, which are constrained JSON with clamped outputs. The
recommendation and assistant narratives read thinner than a frontier model, and the numeric
verifier matters more, not less. Change `NVIDIA_MODEL` if your key reaches a larger one.

### Deploy to Streamlit Community Cloud

1. Push the four files to GitHub — **keep the repo private** while the keys are in the file.
2. share.streamlit.io → **New app** → pick the repo → main file `app.py`.
3. Deploy. First boot takes about a minute.
4. App → Settings → Sharing → restrict viewers, or anyone with the URL burns your quota.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| EDGAR returns 403 | `SEC_USER_AGENT` is still the placeholder | Set your real name and email |
| `HTTP 401` from NVIDIA | Key revoked or mistyped | New key at [build.nvidia.com](https://build.nvidia.com) |
| "only reasoning and no answer" | The thinking budget consumed the token allowance | Use a larger `NVIDIA_MODEL` |
| `HTTP 429` | Free-tier rate limit | Wait a minute |
| Finnhub rate limit | 60 calls/min on the free tier | Wait, or use the header price override |
| A column of `None` chips in the page | A bare expression statement somewhere in the file. Streamlit's magic rewrites `foo() if x else bar()` used as a *statement* into `st.write(<result>)`, and a function returning `None` then prints a `None` chip — once per call | Never use a conditional expression as a statement. Use `if/else`. Run the AST check in the Testing section |
| Gemini `API key not valid` | Key revoked, or the value is an `AQ.`/`ya29.` OAuth token rather than an `AIza` API key | New key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |

---

## Files

```
app.py                  Everything — 4 layers, clearly sectioned
requirements.txt        4 required packages, 2 optional
README.md               This file
IMPLEMENTATION_PLAN.md  What's built, what's next, rubric checklist
```

`app.py` is organised top to bottom as a pipeline. Jump to a section by its banner comment:

| Section | What it does |
|---|---|
| `CANONICAL FINANCIAL CONCEPT MODEL` | ~38 canonical concepts → priority-ordered XBRL tags, 7 derivation rules, 4 accounting identities |
| `INGEST` | EDGAR client (ticker → CIK → companyfacts) and Finnhub quote + company profile |
| `NORMALIZE` | Resolve → derive → validate → quality score |
| `DEMO COMPANY` | Synthetic internally-consistent filer, generated in code |
| `ANALYTICS` | 28 ratios, Altman Z, Piotroski F, driver statistics |
| `SCENARIO ENGINE` | Driver-based DCF, calibrated scenarios, tornado, two-way grid, bisection goal-seek |
| `FORENSICS` | 12 red-flag rules, severity ranking, composite risk score |
| `AI LAYER` | Gemini / Anthropic / OpenAI client with model discovery, numeric guardrail, five features |
| `UI` | Design tokens, sparkline KPI cards, provenance chips, status rows, shared Plotly template |
| `APP` | Five tabs |

---

## How normalization works

Two companies reporting identical economics tag them differently. One uses
`RevenueFromContractWithCustomerExcludingAssessedTax`, an older filer uses
`SalesRevenueNet`, a third defines a custom extension tag. Hardcoding one company's tags is
what makes a dashboard a single-company toy.

`CONCEPTS` maps each canonical concept to a priority-ordered list of XBRL tags. Resolution
is deterministic:

1. **Filter** — matching unit, correct period type (annual duration of 300–400 days, or a
   point-in-time instant), form `10-K`.
2. **Select** — highest-priority tag wins. Where two filings cover the same period, the
   later-filed one wins and the fact is marked restated.
3. **Normalize signs** — capex is filed as a positive payment, stored as a positive
   magnitude, and subtracted downstream. Applied once, at the resolver.
4. **Derive** — missing subtotals filled by a dependency-ordered rule set
   (`gross_profit := revenue − cost_of_revenue`), with the formula recorded on the fact.
5. **Validate** — accounting identities asserted per fiscal year:
   `assets = liabilities + equity`, `Δcash = CFO + CFI + CFF`,
   `gross_profit = revenue − COGS`. Failures produce the **data quality score** in the header.

Fiscal years are labelled by the **calendar year of the period end**, with the exact period
end shown everywhere. Unambiguous across filers with different fiscal calendars, though not
always the label the company itself uses.

Every fact carries its source tag, accession number and period end — which is what makes
the provenance table possible. Auditability is the product here, not a nice-to-have.

---

## The five AI features

Documented in the format Section 5 of the brief requires.

| # | Feature | Does | Uses | Produces | Helps | Limitation |
|---|---|---|---|---|---|---|
| 1 | **Financial-data extraction** | Classifies XBRL tags outside the registry (including company-specific extension tags) into canonical concepts | Tag name, label, description. **Never the value** | `{tag → concept, confidence, reason}` | Makes multi-company support real; rule-based mapping covers ~85% of tags, this closes the gap | A wrong mapping silently corrupts every downstream ratio. Mitigated by confidence scores and the identity validators, which catch most mapping errors |
| 2 | **Scenario generation** | Converts a plain-English scenario into a structured set of DCF driver values | Base drivers, percentiles of the company's own realised drivers, hard bounds | Driver JSON + rationale + key assumption | Replaces arbitrary ±10% with scenarios anchored to how much this company's economics actually vary | Assumption plausibility can't be verified and models anchor optimistically. Every value is hard-clamped; the projection is fully deterministic |
| 3 | **Anomaly detection** | Python computes forensic signals (accruals, CFO-vs-NI, receivables and inventory outrunning revenue, DSO drift, margin compression, leverage, coverage, FCF trend, Altman zone); the model ranks and explains them | Computed signals and metrics only | Ranked signals with a concerning reading, a benign reading, and what to check | Separates a signal that changes a decision from an artefact of an acquisition | Thresholds are industry-agnostic and misfire around M&A and accounting changes. Detection is deterministic — no key costs the explanation, never the signal |
| 4 | **Recommendation engine** | Writes the decision panel: verdict, reasons, reversal condition, limitation | Only structured outputs of other layers — ratios, scenarios, flags, break-even solve | `{decision, headline, narrative, reversal condition, limitation, confidence}` | Covers dashboard component 6 and the final-recommendation section; forces the analysis to state what would change its mind | Fluent prose reads as more certain than the analysis. Figures pass the verifier, unverified ones are marked inline, the decision stays the analyst's |
| 5 | **Finance assistant** | Answers questions about the company's financials in conversation | Metric digest, full ratio history, flags, scenario results | Grounded answer + figures used + confidence + explicit data gap | Lets a non-financial reader interrogate the dashboard without knowing ratio names | Highest hallucination exposure of any feature. Constrained to supplied data, instructed to declare gaps, every answer passes the verifier |

### The numeric guardrail

`verify()` extracts every figure from model output, resolves it to a true magnitude using
its own suffix (`$324M` → 324,000,000), and checks it against the values the app actually
computed. Unverified figures are marked `[unverified]` inline with a badge under the text.

Percentages are checked **only** as fractions, because the analytics layer always stores
ratios that way — allowing the whole-number form let a fabricated `47.3%` match an
unrelated 47-day metric.

Honest limitation: it reliably catches magnitude-class fabrications. A figure that happens
to land within 1% of some other computed value can still pass. It's a screening control,
not a proof — which is exactly why the brief's requirement that a human owns the final
decision is the right one.

---

## Interface

Direction: an analyst's instrument, not a marketing page. The visual language borrows from
the artifact the app reads — a printed financial statement: hairline rules, right-aligned
tabular figures, monospace for anything numeric.

**Signature element: the provenance chip.** Every KPI carries the XBRL tag, fiscal year,
period end and accession number that produced it, in micro monospace under the value.
Auditability is the product, so it is also the ornament.

What each piece is doing:

- **Sparklines in every KPI card** — the shape behind the headline number, drawn as inline
  SVG. Information, not decoration: a flat 8% margin and an 8% margin that just fell off a
  cliff read identically without one.
- **Status rows and gauges** in the header — data quality and risk are scores, so they get
  a bar, not just a number. EDGAR / price feed / AI layer each show a live green or red dot.
- **Staged loading** — `st.status` narrates the pipeline (resolve ticker → fetch facts →
  map concepts → validate) instead of an opaque spinner.
- **Empty states that teach** — before you click an AI feature, the panel explains what it
  does and what it is not allowed to do.
- **Quick-pick tickers, scenario presets, category filters** — fewer keystrokes to a result.
- **CSV download** on the ratio table and each statement, for the report appendix.
- **Unverified figures marked ⚠ inline** — the guardrail is visible, not buried in a log.

---

## Requirement coverage

| Brief requirement | Where |
|---|---|
| ≥ 4 KPI cards | Overview — 8 cards, each with a provenance chip |
| Trend / cash-flow chart | Overview and Statements & cash flow |
| Scenario comparison (optimistic / base / pessimistic) | Scenario lab — calibrated to the company's own driver percentiles |
| Sensitivity analysis (≥ 2 variables) | Scenario lab — tornado over four drivers + WACC × terminal-growth grid |
| Risk and alert panel | Risk & alerts — forensic signals, Altman Z, Piotroski F, composite score |
| AI recommendation panel | AI insights → Recommendation |
| ≥ 5 AI features | AI insights — five tabs, table above |
| ≥ 3 major calculations | DCF valuation, 28 ratios, Altman Z / Piotroski F, bisection goal-seek |
| Data classification | Provenance table separates reported, derived and restated; scenario lab separates user assumptions from AI-generated ones |
| Ethical AI discussion | Deterministic/model split, numeric verifier, data quality score, `[unverified]` marking |

---

## A note on Streamlit magic

Streamlit rewrites **bare expression statements** into `st.write(<value>)`. That includes
conditional expressions used as statements:

```python
walk(v) if isinstance(v, CONTAINERS) else add(v)     # ← prints a `None` chip, every call
st.success("ok") if models else st.error("bad")      # ← same trap
```

Both forms bit this project. Everything is now written as plain `if/else`, and this check
should stay green:

```bash
python -c "import ast;t=ast.parse(open('app.py').read());\
print([n.lineno for n in ast.walk(t) if isinstance(n,ast.Expr) and \
isinstance(n.value,(ast.IfExp,ast.BoolOp,ast.Name,ast.Attribute,ast.Subscript))] or 'clean')"
```

---

## Known limitations

- **Financial-sector filers** (SIC 6000–6799) have no inventory and no meaningful gross
  margin; those ratios are suppressed and a banner appears.
- **Annual data only.** Five to ten observations is too thin for statistical forecasting,
  which is why projection is driver-based rather than a time-series model.
- **US 10-K filers, USD only.** Foreign private issuers file 20-F with a different taxonomy.
- **AI concept mappings are not persisted** — accepted mappings should be written back into
  `CONCEPTS` so the system becomes deterministic over time. Currently manual.
- **Narrative text features are not built.** No Item 1A / MD&A pipeline in this prototype.
