"""
AI Financial Statement & Cash-Flow Analysis Dashboard
=====================================================
Ticker -> SEC XBRL -> normalized statements -> ratios -> scenarios -> risk
signals -> AI recommendation. Works on any US 10-K filer.

Design claim: no number shown in this app is produced by a language model.
Every figure traces to an XBRL tag or a formula in this file. The model maps
labels, proposes assumptions, and writes prose - and any figure it cites is
verified against the calculation engine before display.

Run:  streamlit run app.py

┌───────────────────────────────────────────────────────────────────────────┐
│ KEYS ARE HARDCODED IN THE CONFIG BLOCK BELOW.                             │
│                                                                           │
│ That is fine for a private repo and a demo. Two things to remember:       │
│   1. Set SEC_USER_AGENT to your real name and email — the SEC blocks      │
│      requests without it, and live EDGAR mode will fail with a 403.       │
│   2. Blank the keys before making the repo public or submitting the file. │
│      GitHub reports leaked keys to the provider, which revokes them.      │
│                                                                           │
│ Streamlit secrets and environment variables still override these values   │
│ if you ever want them out of the file.                                    │
└───────────────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# ============================================================================ #
# CONFIG — edit these
# ============================================================================ #
# ⚠ CHANGE THIS. The SEC blocks requests that do not identify the caller.
SEC_USER_AGENT = "Nandana"

NVIDIA_API_KEY = "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"
FINNHUB_API_KEY = "d9m6tohr01qpfnk7alrgd9m6tohr01qpfnk7als0"

# Optional alternatives. Leave blank unless you have one.
# A Gemini key starts with "AIza" — an "AQ." or "ya29." string is a short-lived
# OAuth token, not an API key, and will be rejected.
GEMINI_API_KEY = ""
ANTHROPIC_API_KEY = ""
OPENAI_API_KEY = ""

# Which provider to use: auto | nvidia | gemini | anthropic | openai | off
LLM_PROVIDER = "auto"

DEFAULT_KEYS = {"SEC_USER_AGENT": SEC_USER_AGENT, "NVIDIA_API_KEY": NVIDIA_API_KEY,
                "FINNHUB_API_KEY": FINNHUB_API_KEY, "GEMINI_API_KEY": GEMINI_API_KEY,
                "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY, "OPENAI_API_KEY": OPENAI_API_KEY}


def secret(name: str, fallback: str = "") -> str:
    """Streamlit secrets, then environment variable, then the value above."""
    try:
        if name in st.secrets:
            return str(st.secrets[name]).strip()
    except Exception:
        pass
    return (os.getenv(name) or DEFAULT_KEYS.get(name, "") or fallback).strip()


SEC_UA = lambda: secret("SEC_USER_AGENT", "FinDash Academic Project student@example.edu")

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
FILINGS_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik:010d}&type=10-K"

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
# Tried in order; the first that responds is cached for the session. Google renames
# models often, so this degrades instead of hard-failing on one string.
GEMINI_CANDIDATES = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest",
                     "gemini-2.5-pro", "gemini-1.5-flash", "gemini-pro"]

# NVIDIA NIM is OpenAI-compatible, so it is called over the same REST shape as
# OpenAI — no SDK needed, same as Gemini.
NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "nvidia/nvidia-nemotron-nano-9b-v2"
OPENAI_BASE = "https://api.openai.com/v1"

FORECAST_YEARS = 5
FINANCIAL_SIC = (6000, 6799)
QUICK_TICKERS = ["AAPL", "MSFT", "NVDA", "WMT", "XOM", "PG", "CAT", "KO"]

# ============================================================================ #
# CANONICAL FINANCIAL CONCEPT MODEL
# concept -> (statement, label, period, sign, [priority-ordered XBRL tags])
# sign "abs" stores the magnitude: capex is filed as a positive payment and must
# be subtracted downstream, so the convention is normalised once, here.
# ============================================================================ #
IS, BS, CF = "income_statement", "balance_sheet", "cash_flow"
DUR, INST = "duration", "instant"

CONCEPTS: dict[str, tuple] = {
    "revenue": (IS, "Revenue", DUR, "asis", [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet"]),
    "cost_of_revenue": (IS, "Cost of revenue", DUR, "asis", [
        "CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices"]),
    "gross_profit": (IS, "Gross profit", DUR, "asis", ["GrossProfit"]),
    "research_development": (IS, "R&D expense", DUR, "asis", ["ResearchAndDevelopmentExpense"]),
    "sga_expense": (IS, "SG&A expense", DUR, "asis", [
        "SellingGeneralAndAdministrativeExpense", "GeneralAndAdministrativeExpense"]),
    "operating_expenses": (IS, "Operating expenses", DUR, "asis", ["OperatingExpenses"]),
    "operating_income": (IS, "Operating income", DUR, "asis", ["OperatingIncomeLoss"]),
    "interest_expense": (IS, "Interest expense", DUR, "abs", [
        "InterestExpense", "InterestExpenseDebt", "InterestExpenseNonoperating"]),
    "pretax_income": (IS, "Pre-tax income", DUR, "asis", [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"]),
    "income_tax": (IS, "Income tax", DUR, "asis", ["IncomeTaxExpenseBenefit"]),
    "net_income": (IS, "Net income", DUR, "asis", ["NetIncomeLoss", "ProfitLoss"]),
    "eps_diluted": (IS, "Diluted EPS", DUR, "asis", ["EarningsPerShareDiluted"]),

    "cash_and_equivalents": (BS, "Cash & equivalents", INST, "asis", [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]),
    "short_term_investments": (BS, "Short-term investments", INST, "asis", [
        "ShortTermInvestments", "MarketableSecuritiesCurrent"]),
    "accounts_receivable": (BS, "Accounts receivable", INST, "asis", [
        "AccountsReceivableNetCurrent", "ReceivablesNetCurrent"]),
    "inventory": (BS, "Inventory", INST, "asis", ["InventoryNet"]),
    "current_assets": (BS, "Total current assets", INST, "asis", ["AssetsCurrent"]),
    "ppe_net": (BS, "PP&E, net", INST, "asis", ["PropertyPlantAndEquipmentNet"]),
    "goodwill": (BS, "Goodwill", INST, "asis", ["Goodwill"]),
    "intangible_assets": (BS, "Intangible assets", INST, "asis", [
        "IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"]),
    "total_assets": (BS, "Total assets", INST, "asis", ["Assets"]),
    "accounts_payable": (BS, "Accounts payable", INST, "asis", ["AccountsPayableCurrent"]),
    "current_liabilities": (BS, "Total current liabilities", INST, "asis", ["LiabilitiesCurrent"]),
    "short_term_debt": (BS, "Short-term debt", INST, "asis", [
        "LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings"]),
    "long_term_debt": (BS, "Long-term debt", INST, "asis", [
        "LongTermDebtNoncurrent", "LongTermDebt"]),
    "total_liabilities": (BS, "Total liabilities", INST, "asis", ["Liabilities"]),
    "retained_earnings": (BS, "Retained earnings", INST, "asis", [
        "RetainedEarningsAccumulatedDeficit"]),
    "total_equity": (BS, "Total equity", INST, "asis", [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
    "shares_outstanding": (BS, "Shares outstanding", INST, "asis", [
        "CommonStockSharesOutstanding", "CommonStockSharesIssued"]),

    "cfo": (CF, "Operating cash flow", DUR, "asis", [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]),
    "depreciation_amortization": (CF, "Depreciation & amortisation", DUR, "asis", [
        "DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization", "Depreciation"]),
    "stock_compensation": (CF, "Stock-based compensation", DUR, "asis", ["ShareBasedCompensation"]),
    "capex": (CF, "Capital expenditure", DUR, "abs", [
        "PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"]),
    "cfi": (CF, "Investing cash flow", DUR, "asis", [
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"]),
    "cff": (CF, "Financing cash flow", DUR, "asis", [
        "NetCashProvidedByUsedInFinancingActivities",
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"]),
    "dividends_paid": (CF, "Dividends paid", DUR, "abs", [
        "PaymentsOfDividendsCommonStock", "PaymentsOfDividends"]),
    "share_buybacks": (CF, "Share repurchases", DUR, "abs", [
        "PaymentsForRepurchaseOfCommonStock"]),
    "net_change_in_cash": (CF, "Net change in cash", DUR, "asis", [
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
        "CashAndCashEquivalentsPeriodIncreaseDecrease"]),
}

DERIVATIONS = [
    ("gross_profit", ["revenue", "cost_of_revenue"], lambda v: v[0] - v[1], "revenue - cost_of_revenue"),
    ("operating_expenses", ["gross_profit", "operating_income"], lambda v: v[0] - v[1], "gross_profit - operating_income"),
    ("operating_income", ["gross_profit", "operating_expenses"], lambda v: v[0] - v[1], "gross_profit - operating_expenses"),
    ("pretax_income", ["net_income", "income_tax"], lambda v: v[0] + v[1], "net_income + income_tax"),
    ("total_liabilities", ["total_assets", "total_equity"], lambda v: v[0] - v[1], "total_assets - total_equity"),
    ("total_equity", ["total_assets", "total_liabilities"], lambda v: v[0] - v[1], "total_assets - total_liabilities"),
    ("cost_of_revenue", ["revenue", "gross_profit"], lambda v: v[0] - v[1], "revenue - gross_profit"),
]

VALIDATIONS = [
    ("Balance sheet balances", ["total_assets", "total_liabilities", "total_equity"],
     lambda v: abs(v[0] - (v[1] + v[2])), 0.01, "critical"),
    ("Cash flow reconciles", ["net_change_in_cash", "cfo", "cfi", "cff"],
     lambda v: abs(v[0] - (v[1] + v[2] + v[3])), 0.05, "warning"),
    ("Gross profit consistent", ["gross_profit", "revenue", "cost_of_revenue"],
     lambda v: abs(v[0] - (v[1] - v[2])), 0.01, "warning"),
    ("Current assets within total", ["current_assets", "total_assets"],
     lambda v: max(0.0, v[0] - v[1]), 0.001, "critical"),
]

ANNUAL_MIN, ANNUAL_MAX = 300, 400
FORMS = {"10-K", "10-K/A", "10-KT"}


# ============================================================================ #
# DATA MODEL
# ============================================================================ #
@dataclass
class Fact:
    concept: str
    fiscal_year: int
    value: float
    source_tag: str
    accession: str = ""
    period_end: str = ""
    form: str = "10-K"
    derived: bool = False
    formula: str = ""
    restated: bool = False


@dataclass
class Company:
    profile: dict
    facts: list[Fact] = field(default_factory=list)
    unmapped: list[dict] = field(default_factory=list)
    validations: list[dict] = field(default_factory=list)
    quality: float = 0.0

    @property
    def years(self) -> list[int]:
        return sorted({f.fiscal_year for f in self.facts})

    def prov(self, concept: str, year: int) -> Fact | None:
        return next((f for f in self.facts if f.concept == concept and f.fiscal_year == year), None)

    def get(self, concept: str, year: int | None) -> float | None:
        if year is None:
            return None
        f = self.prov(concept, year)
        return f.value if f else None

    def series(self, concept: str) -> list[float | None]:
        return [self.get(concept, y) for y in self.years]


# ============================================================================ #
# INGEST — SEC EDGAR
# ============================================================================ #
def sec_get(url: str) -> dict:
    headers = {"User-Agent": SEC_UA(), "Accept-Encoding": "gzip, deflate"}
    last = ""
    for attempt in range(4):
        try:
            r = requests.get(url, headers=headers, timeout=(5, 30))
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                raise RuntimeError("Not found on EDGAR.")
            if r.status_code in (403, 429):
                raise RuntimeError(
                    "EDGAR refused the request. Set SEC_USER_AGENT at the top of app.py to "
                    "your real name and email — the SEC requires it.")
            if r.status_code >= 500:
                time.sleep(1.5 * (2 ** attempt))
                continue
            r.raise_for_status()
        except RuntimeError:
            raise
        except Exception as exc:
            last = str(exc)[:200]
            time.sleep(1.0 * (2 ** attempt))
    raise RuntimeError(f"EDGAR unreachable: {last}")


@st.cache_data(ttl=7 * 86400, show_spinner=False)
def ticker_map(_ua: str) -> dict:
    raw = sec_get(TICKERS_URL)
    rows = raw.values() if isinstance(raw, dict) else raw
    return {str(r["ticker"]).upper(): {"cik": int(r["cik_str"]), "title": r.get("title", "")}
            for r in rows if r.get("ticker")}


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_submissions(ticker: str, _ua: str) -> dict:
    tmap = ticker_map(_ua)
    t = ticker.strip().upper()
    if t not in tmap:
        raise RuntimeError(f"{t} is not in the SEC registrant list. Check the symbol.")
    cik = tmap[t]["cik"]
    sub = sec_get(SUBMISSIONS_URL.format(cik=cik))
    try:
        sic = int(sub.get("sic") or 0)
    except (TypeError, ValueError):
        sic = 0
    return {"cik": cik, "ticker": t, "name": sub.get("name") or tmap[t]["title"], "sic": sic,
            "sic_description": sub.get("sicDescription", ""),
            "fiscal_year_end": sub.get("fiscalYearEnd", ""),
            "exchange": (sub.get("exchanges") or [""])[0],
            "filings_url": FILINGS_URL.format(cik=cik),
            "is_financial": FINANCIAL_SIC[0] <= sic <= FINANCIAL_SIC[1]}


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_facts(cik: int, _ua: str) -> dict:
    return sec_get(FACTS_URL.format(cik=cik))


# ============================================================================ #
# INGEST — Finnhub (price and company profile)
# ============================================================================ #
@st.cache_data(ttl=900, show_spinner=False)
def finnhub_quote(ticker: str, key: str) -> dict:
    """Live quote. EDGAR carries no price data, so market value needs this."""
    if not key:
        return {"ok": False, "error": "no key"}
    try:
        r = requests.get("https://finnhub.io/api/v1/quote",
                         params={"symbol": ticker, "token": key}, timeout=10)
        if r.status_code == 401:
            return {"ok": False, "error": "Finnhub rejected the key."}
        if r.status_code == 429:
            return {"ok": False, "error": "Finnhub rate limit reached (60/min on the free tier)."}
        j = r.json()
        if not j.get("c"):
            return {"ok": False, "error": f"No quote returned for {ticker}."}
        return {"ok": True, "price": float(j["c"]), "change": float(j.get("d") or 0),
                "change_pct": float(j.get("dp") or 0), "high": j.get("h"), "low": j.get("l"),
                "prev_close": j.get("pc")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:150]}


@st.cache_data(ttl=86400, show_spinner=False)
def finnhub_profile(ticker: str, key: str) -> dict:
    if not key:
        return {}
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/profile2",
                         params={"symbol": ticker, "token": key}, timeout=10)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


# ============================================================================ #
# NORMALIZE — resolve, derive, validate
# ============================================================================ #
def _date(s: str | None):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except ValueError:
        return None


def resolve(facts_json: dict, max_years: int) -> list[Fact]:
    """One XBRL tag per canonical concept per fiscal year.

    Fiscal years are labelled by the calendar year of the period END date —
    unambiguous across filers with different fiscal calendars, though not always
    the label the company itself uses.
    """
    root = facts_json.get("facts", {})
    out: list[Fact] = []

    for concept, (_s, _l, period, sign, tags) in CONCEPTS.items():
        best: dict[int, dict] = {}
        for priority, tag in enumerate(tags):
            for taxonomy in ("us-gaap", "ifrs-full", "srt"):
                node = root.get(taxonomy, {}).get(tag)
                if not node:
                    continue
                units = node.get("units", {})
                for entry in units.get("USD", []) + units.get("shares", []):
                    if entry.get("form") not in FORMS:
                        continue
                    start, end = _date(entry.get("start")), _date(entry.get("end"))
                    if not end:
                        continue
                    if period == DUR and not (start and ANNUAL_MIN <= (end - start).days <= ANNUAL_MAX):
                        continue
                    if period == INST and start:
                        continue
                    fy, cur = end.year, best.get(end.year)
                    # lower priority index wins; on a tie the later filing wins (restatement)
                    if cur is None or priority < cur["p"] or (
                            priority == cur["p"]
                            and str(entry.get("filed", "")) > str(cur["e"].get("filed", ""))):
                        restated = bool(cur and priority == cur["p"] and abs(
                            float(entry.get("val", 0)) - float(cur["e"].get("val", 0))) > 1)
                        best[fy] = {"p": priority, "tag": tag, "e": entry,
                                    "restated": restated or (cur or {}).get("restated", False)}

        for fy, ch in best.items():
            v = float(ch["e"]["val"])
            out.append(Fact(concept, fy, abs(v) if sign == "abs" else v, ch["tag"],
                            ch["e"].get("accn", ""), ch["e"].get("end", ""),
                            ch["e"].get("form", ""), restated=ch["restated"]))

    keep = sorted({f.fiscal_year for f in out}, reverse=True)[:max_years]
    return [f for f in out if f.fiscal_year in keep]


def derive(facts: list[Fact]) -> list[Fact]:
    idx = {(f.concept, f.fiscal_year): f for f in facts}
    for year in sorted({f.fiscal_year for f in facts}):
        for target, deps, fn, formula in DERIVATIONS:
            if (target, year) in idx or not all((d, year) in idx for d in deps):
                continue
            try:
                val = float(fn([idx[(d, year)].value for d in deps]))
            except Exception:
                continue
            t = idx[(deps[0], year)]
            nf = Fact(target, year, val, "(derived)", t.accession, t.period_end, t.form,
                      derived=True, formula=formula)
            facts.append(nf)
            idx[(target, year)] = nf
    return facts


def validate(facts: list[Fact]) -> tuple[list[dict], float]:
    idx = {(f.concept, f.fiscal_year): f.value for f in facts}
    results = []
    for year in sorted({f.fiscal_year for f in facts}):
        env = {c: v for (c, y), v in idx.items() if y == year}
        scale = max(abs(env.get("total_assets", 0)), abs(env.get("revenue", 0)), 1.0)
        for name, needs, fn, tol, sev in VALIDATIONS:
            if not all(n in env for n in needs):
                continue
            delta = fn([env[n] for n in needs])
            results.append({"FY": year, "Check": name, "Severity": sev,
                            "passed": delta <= tol * scale,
                            "Detail": f"gap {delta:,.0f} vs tolerance {tol * scale:,.0f}"})
    if not results:
        return results, 0.0
    w = {"critical": 2.0, "warning": 1.0}
    tot = sum(w[r["Severity"]] for r in results)
    got = sum(w[r["Severity"]] for r in results if r["passed"])
    return results, round(100 * got / tot, 1)


def find_unmapped(facts_json: dict, limit: int = 15) -> list[dict]:
    known = {t for _, _, _, _, tags in CONCEPTS.values() for t in tags}
    out = []
    for taxonomy, tags in facts_json.get("facts", {}).items():
        if taxonomy == "dei":
            continue
        for tag, node in tags.items():
            if tag in known:
                continue
            annual = [e for e in node.get("units", {}).get("USD", []) if e.get("form") in FORMS]
            if not annual:
                continue
            latest = max(annual, key=lambda e: str(e.get("end", "")))
            out.append({"taxonomy": taxonomy, "tag": tag, "label": node.get("label") or tag,
                        "description": (node.get("description") or "")[:200],
                        "latest_value": float(latest.get("val", 0)),
                        "is_custom": taxonomy not in ("us-gaap", "ifrs-full", "srt")})
    return sorted(out, key=lambda r: abs(r["latest_value"]), reverse=True)[:limit]


# ============================================================================ #
# DEMO COMPANY — synthetic, internally consistent, generated in code.
# A receivables / earnings-quality anomaly is planted in the last two years so
# the forensic engine has something true to find.
# ============================================================================ #
@st.cache_data(show_spinner=False)
def demo_company() -> dict:
    years = [2020, 2021, 2022, 2023, 2024, 2025]
    growth = [0, .115, .132, .094, .071, .043]
    gm = [.421, .428, .434, .425, .412, .398]
    opexp = [.248, .245, .243, .248, .252, .259]
    dso = [52, 53, 55, 58, 67, 79]
    dio = [61, 60, 62, 66, 71, 78]
    dpo = [45, 46, 47, 46, 45, 44]

    rev, cash, ppe, gw, intan = 4.2e9, .78e9, 1.12e9, .64e9, .21e9
    ltd, std, retained, prev = 1.45e9, .18e9, 1.24e9, None
    facts: list[Fact] = []

    for i, yr in enumerate(years):
        rev = rev * (1 + growth[i]) if i else rev
        cogs = rev * (1 - gm[i]); gp = rev - cogs
        opex = rev * opexp[i]; rd = opex * .42
        oi = gp - opex; interest = (ltd + std) * .045
        pretax = oi - interest; tax = pretax * .22; ni = pretax - tax
        da = ppe * .135 + intan * .11; capex = rev * .052; sbc = rev * .021
        ar, inv, ap = rev * dso[i] / 365, cogs * dio[i] / 365, cogs * dpo[i] / 365
        dnwc = ((ar - prev[0]) + (inv - prev[1]) - (ap - prev[2])) if prev else 0.0
        cfo = ni + da + sbc - dnwc
        div = ni * .18; bb = max(0.0, cfo - capex - div) * .35
        repay = 60e6 if i else 0.0
        cfi = -capex - (40e6 if i in (2, 4) else 0.0)
        cff = -div - bb - repay
        net = cfo + cfi + cff
        cash = cash + net if i else cash
        ltd = max(0.0, ltd - repay); ppe = ppe + capex - ppe * .135
        intan = max(0.0, intan - intan * .11 + (30e6 if i in (2, 4) else 0))
        gw += 30e6 if i in (2, 4) else 0
        retained += ni - div - bb
        sti = 320e6 + i * 25e6
        ca = cash + sti + ar + inv + rev * .028
        ta = ca + ppe + gw + intan + rev * .061
        cl = ap + std + rev * .047
        tl = cl + ltd + rev * .038
        te = ta - tl                                  # balances by construction
        sh = 412e6 - i * 4.5e6

        for concept, value in {
            "revenue": rev, "cost_of_revenue": cogs, "gross_profit": gp,
            "research_development": rd, "sga_expense": opex - rd, "operating_expenses": opex,
            "operating_income": oi, "interest_expense": interest, "pretax_income": pretax,
            "income_tax": tax, "net_income": ni, "eps_diluted": ni / (sh * 1.012),
            "cash_and_equivalents": cash, "short_term_investments": sti,
            "accounts_receivable": ar, "inventory": inv, "current_assets": ca, "ppe_net": ppe,
            "goodwill": gw, "intangible_assets": intan, "total_assets": ta,
            "accounts_payable": ap, "current_liabilities": cl, "short_term_debt": std,
            "long_term_debt": ltd, "total_liabilities": tl, "retained_earnings": retained,
            "total_equity": te, "shares_outstanding": sh, "cfo": cfo,
            "depreciation_amortization": da, "stock_compensation": sbc, "capex": capex,
            "cfi": cfi, "cff": cff, "dividends_paid": div, "share_buybacks": bb,
            "net_change_in_cash": net,
        }.items():
            facts.append(Fact(concept, yr, value, f"(demo:{concept})",
                              "0000000000-00-000000", f"{yr}-12-31"))
        prev = (ar, inv, ap)

    vals, score = validate(facts)
    return {
        "profile": {"cik": 0, "ticker": "DEMO", "name": "Meridian Systems Inc.",
                    "sic": 3559, "sic_description": "Special Industry Machinery",
                    "fiscal_year_end": "1231", "exchange": "DEMO", "is_financial": False,
                    "is_demo": True,
                    "filings_url": "https://www.sec.gov/edgar/searchedgar/companysearch"},
        "facts": [asdict(f) for f in facts], "validations": vals, "quality": score,
        "unmapped": [
            {"taxonomy": "mrdn", "tag": "PlatformSubscriptionRevenueNet",
             "label": "Platform Subscription Revenue, Net",
             "description": "Recurring revenue from platform subscription contracts.",
             "latest_value": 1.18e9, "is_custom": True},
            {"taxonomy": "mrdn", "tag": "RestructuringAndFacilityConsolidationCosts",
             "label": "Restructuring and Facility Consolidation Costs",
             "description": "Charges for workforce reduction and facility consolidation.",
             "latest_value": 96e6, "is_custom": True},
            {"taxonomy": "us-gaap", "tag": "OperatingLeaseRightOfUseAsset",
             "label": "Operating Lease, Right-of-Use Asset",
             "description": "Lessee right-of-use asset from operating leases.",
             "latest_value": 388e6, "is_custom": False}],
    }


# ============================================================================ #
# ANALYTICS — all deterministic
# ============================================================================ #
def safe(n, d): return None if (n is None or d is None or d == 0) else n / d
def avg(a, b): return b if a is None else a if b is None else (a + b) / 2
def debt(c, y): return (c.get("short_term_debt", y) or 0) + (c.get("long_term_debt", y) or 0)


def ebitda(c, y):
    oi = c.get("operating_income", y)
    return None if oi is None else oi + (c.get("depreciation_amortization", y) or 0)


def fcf(c, y):
    v = c.get("cfo", y)
    return None if v is None else v - (c.get("capex", y) or 0)


RATIO_META: dict[str, tuple] = {}   # key -> (label, unit, group, formula, higher_is_better)


def compute_ratios(c: Company, price: float | None) -> pd.DataFrame:
    years, rows = c.years, {}
    fin, g = c.profile.get("is_financial", False), c.get

    def put(key, label, unit, group, formula, fn, up=True):
        RATIO_META[key] = (label, unit, group, formula, up)
        rows[key] = {}
        for y in years:
            try:
                rows[key][y] = fn(y)
            except Exception:
                rows[key][y] = None

    if not fin:
        put("gross_margin", "Gross margin", "%", "Profitability", "gross_profit / revenue",
            lambda y: safe(g("gross_profit", y), g("revenue", y)))
    put("operating_margin", "Operating margin", "%", "Profitability", "operating_income / revenue",
        lambda y: safe(g("operating_income", y), g("revenue", y)))
    put("net_margin", "Net margin", "%", "Profitability", "net_income / revenue",
        lambda y: safe(g("net_income", y), g("revenue", y)))
    put("ebitda_margin", "EBITDA margin", "%", "Profitability", "(operating_income + D&A) / revenue",
        lambda y: safe(ebitda(c, y), g("revenue", y)))
    put("roe", "Return on equity", "%", "Returns", "net_income / average equity",
        lambda y: safe(g("net_income", y), avg(g("total_equity", y), g("total_equity", y - 1))))
    put("roa", "Return on assets", "%", "Returns", "net_income / average assets",
        lambda y: safe(g("net_income", y), avg(g("total_assets", y), g("total_assets", y - 1))))
    put("roic", "Return on invested capital", "%", "Returns", "NOPAT / (equity + debt - cash)",
        lambda y: safe((g("operating_income", y) or 0)
                       * (1 - (safe(g("income_tax", y), g("pretax_income", y)) or .21)),
                       (g("total_equity", y) or 0) + debt(c, y) - (g("cash_and_equivalents", y) or 0)))
    put("asset_turnover", "Asset turnover", "x", "Returns", "revenue / average assets",
        lambda y: safe(g("revenue", y), avg(g("total_assets", y), g("total_assets", y - 1))))
    put("current_ratio", "Current ratio", "x", "Liquidity",
        "current_assets / current_liabilities",
        lambda y: safe(g("current_assets", y), g("current_liabilities", y)))
    if not fin:
        put("quick_ratio", "Quick ratio", "x", "Liquidity",
            "(current_assets - inventory) / current_liabilities",
            lambda y: safe((g("current_assets", y) or 0) - (g("inventory", y) or 0),
                           g("current_liabilities", y)))
    put("debt_to_equity", "Debt / equity", "x", "Leverage", "total_debt / total_equity",
        lambda y: safe(debt(c, y), g("total_equity", y)), up=False)
    put("net_debt_to_ebitda", "Net debt / EBITDA", "x", "Leverage", "(debt - cash) / EBITDA",
        lambda y: safe(debt(c, y) - (g("cash_and_equivalents", y) or 0), ebitda(c, y)), up=False)
    put("interest_coverage", "Interest coverage", "x", "Leverage",
        "operating_income / interest_expense",
        lambda y: safe(g("operating_income", y), g("interest_expense", y)))
    put("cfo", "Operating cash flow", "$", "Cash flow", "CFO as reported", lambda y: g("cfo", y))
    put("fcf", "Free cash flow", "$", "Cash flow", "CFO - capex", lambda y: fcf(c, y))
    put("fcf_margin", "FCF margin", "%", "Cash flow", "FCF / revenue",
        lambda y: safe(fcf(c, y), g("revenue", y)))
    put("cash_conversion", "CFO / net income", "x", "Cash flow", "CFO / net_income",
        lambda y: safe(g("cfo", y), g("net_income", y)))
    put("accruals_ratio", "Accruals ratio", "%", "Cash flow",
        "(net_income - CFO) / average assets",
        lambda y: safe((g("net_income", y) or 0) - (g("cfo", y) or 0),
                       avg(g("total_assets", y), g("total_assets", y - 1))), up=False)
    put("capex_intensity", "Capex / revenue", "%", "Cash flow", "capex / revenue",
        lambda y: safe(g("capex", y), g("revenue", y)), up=False)
    if not fin:
        put("dso", "Days sales outstanding", "days", "Working capital", "AR / revenue x 365",
            lambda y: (safe(g("accounts_receivable", y), g("revenue", y)) or 0) * 365 or None, up=False)
        put("dio", "Days inventory outstanding", "days", "Working capital", "inventory / COGS x 365",
            lambda y: (safe(g("inventory", y), g("cost_of_revenue", y)) or 0) * 365 or None, up=False)
        put("dpo", "Days payables outstanding", "days", "Working capital", "AP / COGS x 365",
            lambda y: (safe(g("accounts_payable", y), g("cost_of_revenue", y)) or 0) * 365 or None)
    put("book_value", "Book value (equity)", "$", "Valuation", "total_equity",
        lambda y: g("total_equity", y))
    put("book_value_per_share", "Book value per share", "$", "Valuation", "equity / shares",
        lambda y: safe(g("total_equity", y), g("shares_outstanding", y)))
    if price:
        put("market_value", "Market value", "$", "Valuation", "price x shares",
            lambda y: None if g("shares_outstanding", y) is None else price * g("shares_outstanding", y))
        put("price_to_book", "Price / book", "x", "Valuation", "price / book value per share",
            lambda y: safe(price, safe(g("total_equity", y), g("shares_outstanding", y))), up=False)
        put("pe_ratio", "Price / earnings", "x", "Valuation", "price / diluted EPS",
            lambda y: safe(price, g("eps_diluted", y)), up=False)
        put("ev_to_ebitda", "EV / EBITDA", "x", "Valuation",
            "(market cap + debt - cash) / EBITDA",
            lambda y: safe(None if g("shares_outstanding", y) is None else
                           price * g("shares_outstanding", y) + debt(c, y)
                           - (g("cash_and_equivalents", y) or 0), ebitda(c, y)), up=False)

    return pd.DataFrame(rows).T.reindex(columns=years)


def altman_z(c, y, mcap):
    ta, tl = c.get("total_assets", y), c.get("total_liabilities", y)
    if not ta or not tl:
        return None
    wc = (c.get("current_assets", y) or 0) - (c.get("current_liabilities", y) or 0)
    mve = mcap or (c.get("total_equity", y) or 0)
    return round(1.2 * wc / ta + 1.4 * (c.get("retained_earnings", y) or 0) / ta
                 + 3.3 * (c.get("operating_income", y) or 0) / ta + .6 * mve / tl
                 + 1.0 * (c.get("revenue", y) or 0) / ta, 2)


def altman_band(z):
    return "n/a" if z is None else "Safe" if z >= 2.99 else "Grey" if z >= 1.81 else "Distress"


def piotroski(c, y):
    p = y - 1
    if p not in c.years:
        return None, []
    g = c.get
    roa, roa_p = safe(g("net_income", y), g("total_assets", y)), safe(g("net_income", p), g("total_assets", p))
    cr, cr_p = safe(g("current_assets", y), g("current_liabilities", y)), safe(g("current_assets", p), g("current_liabilities", p))
    gm, gm_p = safe(g("gross_profit", y), g("revenue", y)), safe(g("gross_profit", p), g("revenue", p))
    at, at_p = safe(g("revenue", y), g("total_assets", y)), safe(g("revenue", p), g("total_assets", p))
    lv, lv_p = safe(g("long_term_debt", y), g("total_assets", y)), safe(g("long_term_debt", p), g("total_assets", p))
    tests = [("Positive net income", (g("net_income", y) or 0) > 0),
             ("Positive operating cash flow", (g("cfo", y) or 0) > 0),
             ("ROA improving", bool(roa and roa_p and roa > roa_p)),
             ("CFO exceeds net income", bool(g("cfo", y) and g("net_income", y) and g("cfo", y) > g("net_income", y))),
             ("Leverage decreasing", bool(lv is not None and lv_p is not None and lv < lv_p)),
             ("Current ratio improving", bool(cr and cr_p and cr > cr_p)),
             ("No share dilution", bool(g("shares_outstanding", y) and g("shares_outstanding", p)
                                        and g("shares_outstanding", y) <= g("shares_outstanding", p))),
             ("Gross margin improving", bool(gm and gm_p and gm > gm_p)),
             ("Asset turnover improving", bool(at and at_p and at > at_p))]
    return sum(1 for _, ok in tests if ok), tests


def driver_stats(c: Company) -> dict:
    years = c.years
    if len(years) < 2:
        return {}
    b = {k: [] for k in ("revenue_growth", "gross_margin", "opex_pct_revenue",
                         "capex_pct_revenue", "nwc_pct_revenue")}
    for i in range(1, len(years)):
        y, p = years[i], years[i - 1]
        r, rp = c.get("revenue", y), c.get("revenue", p)
        if r and rp:
            b["revenue_growth"].append(r / rp - 1)
        if r:
            if c.get("gross_profit", y) is not None:
                b["gross_margin"].append(c.get("gross_profit", y) / r)
            if c.get("operating_expenses", y) is not None:
                b["opex_pct_revenue"].append(c.get("operating_expenses", y) / r)
            if c.get("capex", y) is not None:
                b["capex_pct_revenue"].append(c.get("capex", y) / r)
            b["nwc_pct_revenue"].append(((c.get("accounts_receivable", y) or 0)
                                         + (c.get("inventory", y) or 0)
                                         - (c.get("accounts_payable", y) or 0)) / r)
    out = {}
    for k, v in b.items():
        if v:
            s = pd.Series(v)
            out[k] = {"p10": float(s.quantile(.1)), "p50": float(s.quantile(.5)),
                      "p90": float(s.quantile(.9)), "mean": float(s.mean()), "last": float(v[-1])}
    return out


# ============================================================================ #
# SCENARIO ENGINE — driver-based DCF, sensitivity, goal-seek. No LLM here.
# ============================================================================ #
DRIVER_LABELS = {"revenue_growth": "Revenue growth", "gross_margin": "Gross margin",
                 "opex_pct_revenue": "Opex % of revenue", "capex_pct_revenue": "Capex % of revenue",
                 "nwc_pct_revenue": "Working capital % of revenue",
                 "da_pct_revenue": "D&A % of revenue", "tax_rate": "Tax rate",
                 "wacc": "WACC (discount rate)", "terminal_growth": "Terminal growth"}
DRIVER_BOUNDS = {"revenue_growth": (-.40, .60), "gross_margin": (.05, .90),
                 "opex_pct_revenue": (.02, .70), "capex_pct_revenue": (0, .35),
                 "nwc_pct_revenue": (-.20, .60), "da_pct_revenue": (0, .30),
                 "tax_rate": (0, .50), "wacc": (.03, .25), "terminal_growth": (0, .05)}


@dataclass
class Drivers:
    revenue_growth: float = .06
    gross_margin: float = .40
    opex_pct_revenue: float = .25
    capex_pct_revenue: float = .05
    nwc_pct_revenue: float = .12
    da_pct_revenue: float = .045
    tax_rate: float = .21
    wacc: float = .09
    terminal_growth: float = .025

    def copy(self): return Drivers(**asdict(self))


def base_drivers(c: Company) -> Drivers:
    s = driver_stats(c)
    y = c.years[-1] if c.years else None
    rev, da = c.get("revenue", y), c.get("depreciation_amortization", y)
    pick = lambda k, d: round(float(s.get(k, {}).get("p50", d)), 4)
    return Drivers(pick("revenue_growth", .06), pick("gross_margin", .40),
                   pick("opex_pct_revenue", .25), pick("capex_pct_revenue", .05),
                   pick("nwc_pct_revenue", .12), round((da / rev) if (da and rev) else .045, 4))


def calibrated(c: Company) -> dict[str, Drivers]:
    """Optimistic and pessimistic anchored to THIS company's driver percentiles,
    not an arbitrary +/-10%."""
    s, base = driver_stats(c), base_drivers(c)
    band = lambda k, key, dl, up: float(s[k]["p90" if up > 0 else "p10"]) if k in s \
        else getattr(base, key) * (1 + up * dl)
    o = base.copy()
    o.revenue_growth = band("revenue_growth", "revenue_growth", .35, 1)
    o.gross_margin = band("gross_margin", "gross_margin", .05, 1)
    o.opex_pct_revenue = band("opex_pct_revenue", "opex_pct_revenue", .05, -1)
    o.wacc = max(.04, base.wacc - .01)
    p = base.copy()
    p.revenue_growth = band("revenue_growth", "revenue_growth", .60, -1)
    p.gross_margin = band("gross_margin", "gross_margin", .06, -1)
    p.opex_pct_revenue = band("opex_pct_revenue", "opex_pct_revenue", .06, 1)
    p.wacc = base.wacc + .015
    return {"Optimistic": o, "Base case": base, "Pessimistic": p}


def project(c: Company, d: Drivers, n: int = FORECAST_YEARS) -> pd.DataFrame:
    y0 = c.years[-1]
    rev = c.get("revenue", y0) or 0.0
    prev_nwc = rev * d.nwc_pct_revenue
    rows = []
    for i in range(1, n + 1):
        rev *= (1 + d.revenue_growth)
        gp, opex = rev * d.gross_margin, rev * d.opex_pct_revenue
        ebit, da = gp - opex, rev * d.da_pct_revenue
        tax = max(0.0, ebit) * d.tax_rate
        capex, nwc = rev * d.capex_pct_revenue, rev * d.nwc_pct_revenue
        dn, prev_nwc = nwc - prev_nwc, nwc
        f = (ebit - tax) + da - capex - dn
        rows.append({"Year": y0 + i, "Revenue": rev, "Gross profit": gp, "Operating expenses": opex,
                     "EBIT": ebit, "EBITDA": ebit + da, "Tax": tax, "NOPAT": ebit - tax, "D&A": da,
                     "Capex": capex, "Change in NWC": dn, "Free cash flow": f,
                     "PV of FCF": f / (1 + d.wacc) ** i})
    return pd.DataFrame(rows).set_index("Year")


@dataclass
class Result:
    name: str
    drivers: Drivers
    projection: pd.DataFrame
    enterprise_value: float
    equity_value: float
    value_per_share: float | None
    decision: str = ""


def value(c: Company, d: Drivers, name="Scenario") -> Result:
    proj = project(c, d)
    y0 = c.years[-1]
    npv = float(proj["PV of FCF"].sum())
    spread = d.wacc - d.terminal_growth
    tv = (float(proj["Free cash flow"].iloc[-1]) * (1 + d.terminal_growth) / spread) if spread > .005 else 0.0
    ev = npv + tv / (1 + d.wacc) ** FORECAST_YEARS
    eq = ev - (debt(c, y0) - (c.get("cash_and_equivalents", y0) or 0))
    sh = c.get("shares_outstanding", y0)
    return Result(name, d, proj, ev, eq, (eq / sh) if sh else None)


def run_scenarios(c: Company, sets: dict[str, Drivers], price: float | None) -> list[Result]:
    out = []
    for name, d in sets.items():
        r = value(c, d, name)
        if price and r.value_per_share:
            up = r.value_per_share / price - 1
            r.decision = ("Undervalued — accept" if up > .15 else
                          "Overvalued — reject" if up < -.15 else "Fairly valued — hold")
        else:
            r.decision = "Positive value" if r.equity_value > 0 else "Negative value"
        out.append(r)
    return out


def tornado(c: Company, base: Drivers, variables: list[str], shift=.20) -> pd.DataFrame:
    b = value(c, base).equity_value
    rows = []
    for v in variables:
        cur = getattr(base, v)
        step = abs(cur) * shift if cur else .01
        lo, hi = base.copy(), base.copy()
        setattr(lo, v, cur - step); setattr(hi, v, cur + step)
        vl, vh = value(c, lo).equity_value, value(c, hi).equity_value
        rows.append({"Variable": DRIVER_LABELS[v], "Value at low": vl, "Value at high": vh,
                     "Swing": abs(vh - vl), "Impact %": abs(vh - vl) / abs(b) if b else 0})
    return pd.DataFrame(rows).sort_values("Swing", ascending=False).reset_index(drop=True)


def two_way(c: Company, base: Drivers, vx: str, vy: str, steps=5, span=.25) -> pd.DataFrame:
    x0, y0 = getattr(base, vx), getattr(base, vy)
    xs = [x0 * (1 + span * (i / (steps - 1) * 2 - 1)) for i in range(steps)]
    ys = [y0 * (1 + span * (i / (steps - 1) * 2 - 1)) for i in range(steps)]
    grid = []
    for yv in ys:
        row = []
        for xv in xs:
            d = base.copy(); setattr(d, vx, xv); setattr(d, vy, yv)
            row.append(value(c, d).value_per_share or 0.0)
        grid.append(row)
    return pd.DataFrame(grid, index=[f"{v:.2%}" for v in ys], columns=[f"{v:.2%}" for v in xs])


def goal_seek(c: Company, base: Drivers, target: float, variable="revenue_growth") -> float | None:
    """Bisection on the deterministic model."""
    lo, hi = -.50, 1.00
    for _ in range(60):
        mid = (lo + hi) / 2
        d = base.copy(); setattr(d, variable, mid)
        vps = value(c, d).value_per_share
        if vps is None:
            return None
        if abs(vps - target) < .01:
            return round(mid, 4)
        lo, hi = (mid, hi) if vps < target else (lo, mid)
    return round((lo + hi) / 2, 4)


# ============================================================================ #
# FORENSICS — deterministic red flags. The AI ranks and explains; never detects.
# ============================================================================ #
@dataclass
class Flag:
    code: str
    title: str
    severity: str
    metric: str
    observed: str
    threshold: str
    category: str


def detect(c: Company, R: pd.DataFrame, mcap: float | None) -> list[Flag]:
    years = c.years
    if not years:
        return []
    y = years[-1]
    p = years[-2] if len(years) > 1 else None
    out: list[Flag] = []

    def r(k, yr):
        try:
            v = R.loc[k, yr]
            return None if pd.isna(v) else float(v)
        except (KeyError, TypeError):
            return None

    def grow(concept):
        a, b = c.get(concept, y), (c.get(concept, p) if p else None)
        return (a / b - 1) if (a and b) else None

    cc = r("cash_conversion", y)
    if cc is not None and cc < 1.0:
        out.append(Flag("EQ_CFO_NI", "Operating cash flow below net income",
                        "high" if cc < .8 else "medium", "CFO / net income", f"{cc:.2f}x",
                        "below 1.00x", "Earnings quality"))
    ac = r("accruals_ratio", y)
    if ac is not None and ac > .05:
        out.append(Flag("EQ_ACCRUALS", "High accruals relative to assets",
                        "high" if ac > .10 else "medium", "Accruals ratio", f"{ac:.1%}",
                        "above 5.0%", "Earnings quality"))
    rg, ag, ig = grow("revenue"), grow("accounts_receivable"), grow("inventory")
    if rg is not None and ag is not None and ag - rg > .08:
        out.append(Flag("WC_AR", "Receivables growing faster than revenue",
                        "high" if ag - rg > .15 else "medium", "AR vs revenue growth",
                        f"{ag:.1%} vs {rg:.1%}", "gap above 8pp", "Working capital"))
    if rg is not None and ig is not None and ig - rg > .10:
        out.append(Flag("WC_INV", "Inventory building faster than revenue", "medium",
                        "Inventory vs revenue growth", f"{ig:.1%} vs {rg:.1%}",
                        "gap above 10pp", "Working capital"))
    dn, dp = r("dso", y), (r("dso", p) if p else None)
    if dn and dp and dn - dp > 7:
        out.append(Flag("WC_DSO", "Collection period lengthening",
                        "high" if dn - dp > 15 else "medium", "Days sales outstanding",
                        f"{dn:.0f} days (from {dp:.0f})", "increase above 7 days", "Working capital"))
    om, op = r("operating_margin", y), (r("operating_margin", p) if p else None)
    if om is not None and op is not None and op - om > .015:
        out.append(Flag("PR_MARGIN", "Operating margin compressing",
                        "high" if op - om > .04 else "medium", "Operating margin",
                        f"{om:.1%} (from {op:.1%})", "decline above 1.5pp", "Profitability"))
    nd = r("net_debt_to_ebitda", y)
    if nd is not None and nd > 3.0:
        out.append(Flag("LV_DEBT", "Elevated net leverage", "high" if nd > 4 else "medium",
                        "Net debt / EBITDA", f"{nd:.2f}x", "above 3.00x", "Leverage"))
    ic = r("interest_coverage", y)
    if ic is not None and ic < 4.0:
        out.append(Flag("LV_COVER", "Thin interest coverage", "high" if ic < 2 else "medium",
                        "Interest coverage", f"{ic:.2f}x", "below 4.00x", "Leverage"))
    cr = r("current_ratio", y)
    if cr is not None and cr < 1.2:
        out.append(Flag("LQ_CURRENT", "Weak short-term liquidity", "high" if cr < 1 else "medium",
                        "Current ratio", f"{cr:.2f}x", "below 1.20x", "Liquidity"))
    fv = fcf(c, y)
    if fv is not None and fv < 0:
        out.append(Flag("CF_NEG", "Negative free cash flow", "high", "Free cash flow",
                        money(fv), "below zero", "Cash flow"))
    ser = [fcf(c, yr) for yr in years][-3:]
    if len(ser) == 3 and all(v is not None for v in ser) and ser[0] > ser[1] > ser[2]:
        out.append(Flag("CF_TREND", "Free cash flow declining three years running", "medium",
                        "FCF trend", " → ".join(money(v) for v in ser),
                        "three consecutive declines", "Cash flow"))
    z = altman_z(c, y, mcap)
    if altman_band(z) in ("Grey", "Distress"):
        out.append(Flag("RK_Z", f"Altman Z-Score in the {altman_band(z).lower()} zone",
                        "high" if altman_band(z) == "Distress" else "medium",
                        "Altman Z-Score", str(z), "below 2.99", "Solvency"))
    if any(not v["passed"] and v["Severity"] == "critical" for v in c.validations):
        out.append(Flag("DQ_FAIL", "Accounting identity check failed", "high", "Data validation",
                        "critical check failed", "all critical checks must pass", "Data quality"))

    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda f: order.get(f.severity, 9))


def risk_score(flags: list[Flag]) -> dict:
    n = {"high": 0, "medium": 0, "low": 0}
    for f in flags:
        n[f.severity] = n.get(f.severity, 0) + 1
    s = max(0, 100 - n["high"] * 18 - n["medium"] * 8 - n["low"] * 3)
    return {"counts": n, "score": s,
            "label": "Low risk" if s >= 80 else "Moderate risk" if s >= 55 else "Elevated risk"}


# ============================================================================ #
# AI LAYER — Gemini / NVIDIA NIM / Anthropic / OpenAI, JSON enforcement, guardrail
# ============================================================================ #
TELEMETRY: list[dict] = []


PROVIDER_ORDER = ("gemini", "nvidia", "anthropic", "openai")


def llm_provider() -> str:
    forced = (os.getenv("LLM_PROVIDER") or LLM_PROVIDER or "auto").lower()
    have = {"gemini": bool(secret("GEMINI_API_KEY")),
            "nvidia": bool(secret("NVIDIA_API_KEY")),
            "anthropic": bool(secret("ANTHROPIC_API_KEY")),
            "openai": bool(secret("OPENAI_API_KEY"))}
    if forced == "off":
        return "off"
    if forced in have:
        return forced if have[forced] else "off"
    for p in PROVIDER_ORDER:                            # auto
        if have[p]:
            return p
    return "off"


@st.cache_data(ttl=3600, show_spinner=False)
def gemini_probe(key: str) -> dict:
    """Ask the API which models this key can actually use.

    Google renames models often, so discovery beats a hardcoded string. Returns
    the real HTTP status and error text rather than an empty list, because the
    two usual failures — the Generative Language API not enabled on the key's
    project, and a referrer/IP restriction blocking server-side calls — are only
    diagnosable from Google's own message.
    """
    if not key:
        return {"ok": False, "models": [], "error": "No Gemini key supplied.", "endpoint": ""}

    last = "no response"
    for version in ("v1beta", "v1"):
        url = f"https://generativelanguage.googleapis.com/{version}/models"
        try:
            models, token, guard = [], None, 0
            while guard < 5:
                guard += 1
                params = {"key": key, "pageSize": 200}
                if token:
                    params["pageToken"] = token
                r = requests.get(url, params=params, timeout=20)
                if r.status_code != 200:
                    try:
                        err = r.json().get("error", {})
                        last = f"HTTP {r.status_code} · {err.get('status','')} · {err.get('message','')[:220]}"
                    except Exception:
                        last = f"HTTP {r.status_code} · {r.text[:200]}"
                    break
                body = r.json()
                models += [m["name"].split("/")[-1] for m in body.get("models", [])
                           if "generateContent" in m.get("supportedGenerationMethods", [])]
                token = body.get("nextPageToken")
                if not token:
                    break
            if models:
                return {"ok": True, "models": sorted(set(models)), "error": "",
                        "endpoint": version}
        except Exception as exc:
            last = f"{type(exc).__name__}: {str(exc)[:200]}"

    return {"ok": False, "models": [], "error": last, "endpoint": ""}


def gemini_models(key: str) -> list[str]:
    return gemini_probe(key).get("models", [])


def gemini_pick(key: str) -> str:
    """Chosen model, discovered model, or a sensible default.

    Discovery is a convenience, not a dependency: some keys can call
    generateContent but not ListModels, so a failed probe must not disable the
    AI layer. _call_gemini falls through the candidate list on a 404.
    """
    chosen = st.session_state.get("gemini_model")
    if chosen:
        return chosen
    available = gemini_probe(key).get("models", [])
    for cand in GEMINI_CANDIDATES:
        if cand in available:
            return cand
    flash = [m for m in available if "flash" in m and "thinking" not in m]
    return (flash or available or GEMINI_CANDIDATES)[0]


def _gemini_once(key: str, model: str, system: str, user: str) -> tuple[str, int, str]:
    r = requests.post(
        f"{GEMINI_BASE}/models/{model}:generateContent",
        params={"key": key}, timeout=90,
        json={"system_instruction": {"parts": [{"text": system}]},
              "contents": [{"role": "user", "parts": [{"text": user}]}],
              "generationConfig": {"responseMimeType": "application/json",
                                   "maxOutputTokens": 2048, "temperature": 0.2}})
    if r.status_code != 200:
        try:
            detail = r.json().get("error", {}).get("message", "")[:220]
        except Exception:
            detail = r.text[:200]
        return "", r.status_code, detail
    cands = r.json().get("candidates", [])
    if not cands:
        return "", 200, "Gemini returned no candidates (the prompt may have been blocked)."
    return "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", [])), 200, ""


def _call_gemini(key: str, model: str, system: str, user: str) -> str:
    """Try the selected model, then fall through the candidate list on 404.

    Model discovery is best-effort — some keys can generate but not list — so a
    stale or renamed model must not take the AI layer down with it.
    """
    tried, last = [], ""
    for name in [model] + [m for m in GEMINI_CANDIDATES if m != model]:
        if name in tried:
            continue
        tried.append(name)
        text, status, detail = _gemini_once(key, name, system, user)
        if status == 200 and text:
            if name != model:
                st.session_state["gemini_model"] = name      # remember what worked
            return text
        last = f"Gemini {status}: {detail}"
        # A 404 means that model name is gone — try the next candidate. A 400 is
        # ambiguous, so only retry when the message is about the model itself.
        # Anything else (401 auth, 429 quota, 5xx) will not improve on retry.
        model_problem = status == 404 or (status == 400 and "model" in detail.lower())
        if not model_problem:
            break
    raise RuntimeError(f"{last} (tried: {', '.join(tried)})")


def _call_openai_compatible(base_url: str, key: str, model: str, system: str, user: str,
                            json_mode: bool = True, extra_body: dict | None = None) -> str:
    """One transport for OpenAI and NVIDIA NIM — NIM speaks the same wire format.

    Reasoning models (nemotron included) return the chain of thought in a separate
    `reasoning_content` field. Only `content` is read, so thinking never reaches
    the parser or the numeric verifier.
    """
    payload = {"model": model, "max_tokens": 3000, "temperature": 0.2,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if extra_body:
        payload.update(extra_body)

    r = requests.post(f"{base_url}/chat/completions", timeout=120,
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"}, json=payload)

    if r.status_code == 400 and json_mode:
        # Not every NIM deployment supports response_format. Retry without it —
        # the prompt already demands JSON and llm_json extracts the object.
        return _call_openai_compatible(base_url, key, model, system, user,
                                       json_mode=False, extra_body=extra_body)
    if r.status_code != 200:
        try:
            err = r.json().get("error", {})
            detail = (err.get("message") if isinstance(err, dict) else str(err)) or r.text[:200]
        except Exception:
            detail = r.text[:200]
        raise RuntimeError(f"{model} HTTP {r.status_code}: {str(detail)[:220]}")

    choices = r.json().get("choices", [])
    if not choices:
        raise RuntimeError(f"{model} returned no choices.")
    msg = choices[0].get("message", {}) or {}
    text = msg.get("content") or ""
    if not text.strip():
        raise RuntimeError(
            f"{model} returned only reasoning and no answer — the thinking budget "
            "consumed the whole token allowance. Try a larger model.")
    return text


@st.cache_data(show_spinner=False)
def _llm_call(provider: str, model: str, system: str, user: str, key: str, _ck: str) -> dict:
    t0 = time.time()
    if provider == "gemini":
        text = _call_gemini(key, model, system, user)
    elif provider == "nvidia":
        # Keep the thinking budget small: this is a classification and drafting
        # task, and every thinking token competes with the answer for max_tokens.
        text = _call_openai_compatible(
            NVIDIA_BASE, key, model, system, user,
            extra_body={"top_p": 0.95,
                        "extra_body": {"min_thinking_tokens": 128,
                                       "max_thinking_tokens": 512}})
    elif provider == "anthropic":
        from anthropic import Anthropic
        msg = Anthropic(api_key=key).messages.create(
            model=model, max_tokens=1600, system=system,
            messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    else:
        text = _call_openai_compatible(OPENAI_BASE, key, model, system, user)
    return {"text": text, "latency_ms": int((time.time() - t0) * 1000)}


def llm_json(feature: str, system: str, user: str) -> tuple[dict | None, str]:
    """Structured output is enforced here, not requested politely."""
    provider = llm_provider()
    if provider == "off":
        return None, "No LLM provider configured."
    if provider == "gemini":
        key = secret("GEMINI_API_KEY")
        model = gemini_pick(key)
    elif provider == "nvidia":
        key = secret("NVIDIA_API_KEY")
        model = os.getenv("NVIDIA_MODEL", NVIDIA_MODEL)
    elif provider == "anthropic":
        key, model = secret("ANTHROPIC_API_KEY"), os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
    else:
        key, model = secret("OPENAI_API_KEY"), os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    system += ("\n\nReturn ONLY one valid JSON object. No markdown fences, no preamble, "
               "no commentary outside the JSON.")
    ck = hashlib.sha256(f"{feature}|{provider}|{model}|{system}|{user}".encode()).hexdigest()[:24]
    try:
        res = _llm_call(provider, model, system, user, key, ck)
    except Exception as exc:
        return None, str(exc)[:400]

    TELEMETRY.append({"feature": feature, "provider": provider, "model": model,
                      "latency_ms": res["latency_ms"]})
    text = re.sub(r"<think>.*?</think>", "", res["text"], flags=re.S).strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = (parts[1] if len(parts) > 1 else text).removeprefix("json").strip()
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        return None, "Model did not return JSON."
    try:
        return json.loads(text[a:b + 1]), ""
    except json.JSONDecodeError as exc:
        return None, f"Malformed JSON: {exc}"


# --- numeric guardrail ------------------------------------------------------
NUM_RE = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?\s*(?:bps|bn|%|[BMKTxbmkt])?(?![A-Za-z0-9])")
SCALE = {"t": 1e12, "b": 1e9, "bn": 1e9, "m": 1e6, "k": 1e3, "": 1., "x": 1., "%": 1., "bps": 1.}


CONTAINERS = (dict, list, tuple, set, pd.DataFrame, pd.Series)


def allowed_values(*sources) -> set[float]:
    """Flatten every computed value the model is permitted to cite.

    Note on style: every statement here is a plain statement, never a bare
    conditional expression. Streamlit's magic rewrites bare expression
    statements into `st.write(<value>)`, so `walk(v) if ... else add(v)` was
    printing a `None` chip into the page on every recursion.
    """
    out: set[float] = set()

    def add(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        if not (math.isnan(f) or math.isinf(f)):
            out.add(abs(f))

    def visit(v):
        if isinstance(v, CONTAINERS):
            walk(v)
        else:
            add(v)

    def walk(s):
        if s is None or isinstance(s, str):
            return
        if isinstance(s, (pd.DataFrame, pd.Series)):
            for v in s.to_numpy().ravel():
                add(v)
        elif isinstance(s, dict):
            for v in s.values():
                visit(v)
        elif isinstance(s, (list, tuple, set)):
            for v in s:
                visit(v)
        else:
            add(s)

    for s in sources:
        walk(s)
    return out


def verify(text: str, allowed: set[float], tol=.01) -> tuple[int, int, list[str]]:
    """Resolve each claimed figure to its true magnitude and check it against what
    this app computed. Percentages are checked ONLY as fractions, because the
    analytics layer always stores ratios that way — allowing the whole-number form
    let a fabricated 47.3% match an unrelated 47-day metric."""
    if not text or not allowed:
        return 0, 0, []
    total = ok = 0
    bad: list[str] = []
    for token in NUM_RE.findall(text):
        raw, suffix = token.strip(), ""
        for s in ("bps", "bn", "%", "T", "B", "M", "K", "x", "t", "b", "m", "k"):
            if raw.endswith(s):
                suffix, raw = s.lower(), raw[:-len(s)].strip()
                break
        try:
            mag = float(raw.replace("$", "").replace(",", "")) * SCALE.get(suffix, 1.)
        except ValueError:
            continue
        if suffix in ("", "x") and mag == int(mag) and abs(mag) <= 2100:
            continue                                       # a year or a count
        total += 1
        cands = [mag / 100] if suffix == "%" else [mag / 10000] if suffix == "bps" else [mag]
        if any(abs(cd - a) <= max(tol * max(abs(a), abs(cd)), 1e-4) for cd in cands for a in allowed):
            ok += 1
        else:
            bad.append(token.strip())
    return total, ok, bad


def annotate(text: str, bad: list[str]) -> str:
    for t in sorted(set(bad), key=len, reverse=True):
        text = text.replace(t, f"{t} ⚠")
    return text


def as_text(v, default: str = "") -> str:
    """Coerce whatever the model put in a field into displayable prose.

    Small models sometimes return a list of sentences, a nested object, or null
    where a string was asked for. Rendering that raw produces junk, so it is
    flattened here rather than at each of the five call sites.
    """
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip() or default
    if isinstance(v, (list, tuple)):
        parts = [as_text(x) for x in v]
        return " ".join(p for p in parts if p) or default
    if isinstance(v, dict):
        parts = [as_text(x) for x in v.values()]
        return " ".join(p for p in parts if p) or default
    return str(v)


def verify_badge(v) -> tuple[str, str]:
    t, ok, bad = v
    if t == 0:
        return "No figures cited", ""
    if not bad:
        return f"All {t} figures verified", "ok"
    return f"{len(bad)} of {t} figures unverified", "warn"


def digest(c: Company, R: pd.DataFrame) -> dict:
    y = c.years[-1]
    val = lambda k: (None if k not in R.index or pd.isna(R.loc[k, y]) else round(float(R.loc[k, y]), 4))
    return {"company": c.profile["name"], "industry": c.profile.get("sic_description"),
            "fiscal_years": c.years, "latest_fiscal_year": y,
            "statements_usd": {k: (round(c.get(k, y), 0) if c.get(k, y) is not None else None)
                               for k in ["revenue", "net_income", "cfo", "capex", "total_assets",
                                         "total_equity", "cash_and_equivalents", "long_term_debt",
                                         "shares_outstanding"]},
            "ratios_latest": {k: val(k) for k in
                              ["gross_margin", "operating_margin", "net_margin", "roe", "roic",
                               "current_ratio", "debt_to_equity", "net_debt_to_ebitda",
                               "interest_coverage", "fcf", "fcf_margin", "cash_conversion",
                               "accruals_ratio", "dso", "book_value_per_share", "price_to_book",
                               "pe_ratio"]},
            "data_quality_score": c.quality}


J = lambda o, n=4000: json.dumps(o, default=str)[:n]
CANONICAL_LIST = list(CONCEPTS.keys()) + ["none"]


# --- FEATURE 1: financial-data extraction ------------------------------------
def ai_extract(c: Company) -> dict:
    tags = c.unmapped[:10]
    if not tags:
        return {"ok": True, "fallback": True, "mappings": [],
                "note": "Every material tag in this filing is already covered by the registry."}
    if llm_provider() == "off":
        return {"ok": True, "fallback": True, "mappings": [],
                "note": f"{len(tags)} tag(s) fall outside the registry. Connect a provider to "
                        "resolve them automatically, or extend CONCEPTS by hand."}
    data, err = llm_json(
        "extraction",
        "You are a financial reporting taxonomy specialist. You classify XBRL tags into a fixed "
        "set of canonical statement concepts. You classify the LABEL and DESCRIPTION only — you "
        "never reason about or estimate the numeric value. Return 'none' if no concept fits.",
        f"Canonical concepts: {', '.join(CANONICAL_LIST)}\n\nTags:\n{J(tags)}\n\n"
        'Return {"mappings":[{"tag":"...","canonical_concept":"...","confidence":0.0,'
        '"reason":"one sentence"}]}')
    if err:
        return {"ok": False, "error": err}
    for m in data.get("mappings", []):
        if m.get("canonical_concept") not in CANONICAL_LIST:
            m["canonical_concept"], m["confidence"] = "none", 0.0
    return {"ok": True, "fallback": False, "mappings": data.get("mappings", []),
            "note": "Accepted mappings should be written back into CONCEPTS so the next run "
                    "resolves them deterministically, with no model call."}


# --- FEATURE 2: scenario generation ------------------------------------------
def ai_scenario(c: Company, base: Drivers, prompt: str) -> dict:
    if llm_provider() == "off":
        d = base.copy(); d.revenue_growth *= .6; d.gross_margin -= .015
        return {"ok": True, "fallback": True, "drivers": asdict(d),
                "rationale": "AI scenario generation is unavailable, so a conservative default was "
                             "applied: growth cut to 60% of base, gross margin lowered 1.5 points."}
    data, err = llm_json(
        "scenario",
        "You are a corporate finance analyst setting assumptions for a discounted cash flow model. "
        "You output ONLY assumption values as decimals (0.08 means 8%). You never compute "
        "valuations or cash flows — a deterministic engine does that. Anchor every assumption to "
        "the company's own historical driver distribution and stay inside the stated bounds.",
        f"Company: {c.profile['name']} ({c.profile.get('sic_description')})\n"
        f"Base drivers: {J(asdict(base))}\nHistorical driver percentiles: {J(driver_stats(c))}\n"
        f"Bounds: {J(DRIVER_BOUNDS)}\n\nScenario: \"{prompt}\"\n\n"
        'Return {"drivers":{...all nine keys...},"rationale":"2-3 sentences",'
        '"key_assumption":"the single most consequential change","confidence":0.0}')
    if err:
        return {"ok": False, "error": err}
    d = base.copy()
    for k, (lo, hi) in DRIVER_BOUNDS.items():
        if k in data.get("drivers", {}):
            try:
                setattr(d, k, max(lo, min(hi, float(data["drivers"][k]))))   # hard clamp
            except (TypeError, ValueError):
                pass
    return {"ok": True, "fallback": False, "drivers": asdict(d),
            "rationale": as_text(data.get("rationale")),
            "key_assumption": as_text(data.get("key_assumption"))}


# --- FEATURE 3: anomaly detection --------------------------------------------
def ai_anomalies(c: Company, R: pd.DataFrame, flags: list[Flag]) -> dict:
    if not flags:
        return {"ok": True, "fallback": True, "ranked": [],
                "headline": "No red flags were triggered by the forensic rule set this period."}
    if llm_provider() == "off":
        s = risk_score(flags)
        return {"ok": True, "fallback": True, "ranked": [],
                "headline": f"{s['counts']['high']} high and {s['counts']['medium']} medium severity "
                            f"signals detected deterministically. Composite risk score "
                            f"{s['score']}/100 ({s['label']}). Connect a provider for ranked "
                            "interpretation."}
    data, err = llm_json(
        "anomaly",
        "You are a forensic accounting analyst. The signals below were ALREADY computed from "
        "audited filings. Rank them by how much they should change a decision and explain the "
        "plausible business cause of each. Do not recompute or invent any figure — reference only "
        "numbers in the input. Give both the concerning and the benign reading of each signal.",
        f"Company: {c.profile['name']}\nSignals: {J([asdict(f) for f in flags])}\n"
        f"Metrics: {J(digest(c, R))}\n\n"
        'Return {"headline":"one sentence","ranked":[{"code":"...","rank":1,'
        '"why_it_matters":"...","concerning_reading":"...","benign_reading":"...",'
        '"what_to_check":"..."}]}')
    if err:
        return {"ok": False, "error": err}
    ranked = [r for r in data.get("ranked", []) if isinstance(r, dict)]
    for r in ranked:
        for k in ("why_it_matters", "concerning_reading", "benign_reading", "what_to_check"):
            r[k] = as_text(r.get(k))
    headline = as_text(data.get("headline"))
    text = headline + " " + " ".join(f"{r['why_it_matters']} {r['concerning_reading']}"
                                     for r in ranked)
    return {"ok": True, "fallback": False, **data, "headline": headline, "ranked": ranked,
            "verify": verify(text, allowed_values(R, digest(c, R)))}


# --- FEATURE 4: recommendation engine ----------------------------------------
def ai_recommend(c, R, flags, results, price, break_even) -> dict:
    scen = [{"scenario": r.name,
             "value_per_share": round(r.value_per_share, 2) if r.value_per_share else None,
             "equity_value_usd": round(r.equity_value), "decision": r.decision,
             "revenue_growth": round(r.drivers.revenue_growth, 4),
             "wacc": round(r.drivers.wacc, 4)} for r in results]
    s = risk_score(flags)
    if llm_provider() == "off":
        base = next((r for r in results if r.name == "Base case"), results[0])
        parts = []
        if base.value_per_share and price:
            parts.append(f"The base-case discounted cash flow gives an intrinsic value of "
                         f"${base.value_per_share:,.2f} per share against a market price of "
                         f"${price:,.2f}, a gap of {base.value_per_share / price - 1:+.1%}.")
        parts.append(f"The forensic rule set raised {s['counts']['high']} high and "
                     f"{s['counts']['medium']} medium severity signals, giving a composite risk "
                     f"score of {s['score']} out of 100 ({s['label']}).")
        if break_even is not None:
            parts.append(f"The recommendation reverses if sustained revenue growth falls below "
                         f"{break_even:.1%}.")
        parts.append("This summary is rule-generated. Connect a provider for the full analysis.")
        return {"ok": True, "fallback": True, "recommendation": " ".join(parts), "decision": ""}
    data, err = llm_json(
        "recommendation",
        "You are a corporate finance advisor writing the recommendation panel of an analyst "
        "dashboard. Every figure you cite must come from the data provided — you may not compute "
        "new ones. State a clear decision, two or three reasons, and the specific condition under "
        "which the decision reverses. Close with the limitation a reader should keep in mind. "
        "Write 130-180 words of plain professional prose. No bullet points.",
        f"Metrics: {J(digest(c, R))}\nScenarios: {J(scen)}\n"
        f"Risk signals: {J([asdict(f) for f in flags])}\nMarket price: {price}\n"
        f"Break-even revenue growth (value equals market price): {break_even}\n\n"
        'Return {"decision":"Accept | Conditional | Reject","headline":"one sentence",'
        '"recommendation":"the 130-180 word narrative","reversal_condition":"one sentence",'
        '"limitation":"one sentence","confidence":0.0}')
    if err:
        return {"ok": False, "error": err}
    narrative = as_text(data.get("recommendation"),
                        "The model returned no narrative. The scenario table and risk panel "
                        "below carry the full computed picture.")
    v = verify(narrative, allowed_values(R, digest(c, R), scen, [price, break_even]))
    return {"ok": True, "fallback": False, **data,
            "decision": as_text(data.get("decision")),
            "headline": as_text(data.get("headline")),
            "reversal_condition": as_text(data.get("reversal_condition")),
            "limitation": as_text(data.get("limitation")),
            "recommendation": annotate(narrative, v[2]), "verify": v}


# --- FEATURE 5: finance assistant --------------------------------------------
SAMPLE_QS = ["Is the company generating more cash than reported profit?",
             "Why did operating margin change in the most recent year?",
             "Which assumption has the greatest effect on the valuation?",
             "Is the share overvalued or undervalued against the base case?",
             "Explain the working capital position to a non-financial manager.",
             "What is the biggest financial risk in these statements?"]


def ai_ask(c, R, flags, results, q) -> dict:
    if llm_provider() == "off":
        return {"ok": True, "fallback": True,
                "answer": "The assistant needs a provider key. Every other panel in this dashboard "
                          "is fully deterministic and works without one."}
    scen = [{"scenario": r.name,
             "value_per_share": round(r.value_per_share, 2) if r.value_per_share else None,
             "decision": r.decision} for r in results]
    data, err = llm_json(
        "assistant",
        "You are a financial analysis assistant embedded in a corporate finance dashboard. Answer "
        "ONLY from the computed data below, which came from SEC filings and this application's own "
        "calculation engine. Every number in your answer must appear in that data. If the data does "
        "not contain the answer, say so and name what would be needed. Under 130 words. Do not give "
        "investment advice or speculate about share price movements.",
        f"Metrics: {J(digest(c, R))}\nRatio history: {J(R.round(4).to_dict(), 5000)}\n"
        f"Risk signals: {J([asdict(f) for f in flags])}\nScenarios: {J(scen)}\n\nQuestion: {q}\n\n"
        'Return {"answer":"...","figures_used":["..."],"confidence":0.0,"data_gap":""}')
    if err:
        return {"ok": False, "error": err}
    answer = as_text(data.get("answer"),
                     "The model returned an empty answer. Try rephrasing the question.")
    v = verify(answer, allowed_values(R, digest(c, R), scen))
    return {"ok": True, "fallback": False, **data,
            "data_gap": as_text(data.get("data_gap")),
            "answer": annotate(answer, v[2]), "verify": v}


# ============================================================================ #
# UI — design system
# ============================================================================ #
P = {"base": "#0A0E14", "panel": "#121924", "panel2": "#18212E", "rule": "#25303F",
     "ink": "#E3EAF4", "dim": "#98A8BC", "muted": "#66768B", "green": "#54C79A",
     "amber": "#E5B33F", "red": "#E2695F", "blue": "#77A8DC", "violet": "#9B8FE0"}
SEV = {"high": P["red"], "medium": P["amber"], "low": P["blue"]}
F = "IBM Plex Mono, ui-monospace, monospace"

CSS = f"""<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');
html,body,.stApp{{font-family:'IBM Plex Sans',system-ui,sans-serif;background:{P['base']};color:{P['ink']}}}
.block-container{{padding-top:1.6rem;padding-bottom:3rem;max-width:1460px}}
footer,#MainMenu,[data-testid="stDecoration"]{{visibility:hidden}}

/* masthead */
.mast{{display:flex;align-items:center;gap:18px;border:1px solid {P['rule']};
 border-left:3px solid {P['green']};background:linear-gradient(180deg,{P['panel']},{P['base']});
 padding:16px 22px;margin-bottom:10px}}
.mast img{{width:44px;height:44px;object-fit:contain;background:#fff;border-radius:4px;padding:3px}}
.mast .n{{font-size:1.38rem;font-weight:600;letter-spacing:-.015em;line-height:1.2}}
.mast .m{{font-family:{F};font-size:.68rem;color:{P['muted']};text-transform:uppercase;
 letter-spacing:.09em;margin-top:6px}}
.mast .px{{margin-left:auto;text-align:right;font-family:{F}}}
.mast .px .v{{font-size:1.5rem;font-variant-numeric:tabular-nums;letter-spacing:-.02em}}
.mast .px .c{{font-size:.78rem;margin-top:3px}}

/* kpi */
.kpi{{border:1px solid {P['rule']};border-top:2px solid var(--a,{P['rule']});background:{P['panel']};
 padding:13px 15px 11px;height:100%;transition:border-color .15s,background .15s}}
.kpi:hover{{background:{P['panel2']};border-color:{P['muted']}}}
.kpi .l{{font-family:{F};font-size:.63rem;text-transform:uppercase;letter-spacing:.11em;
 color:{P['muted']};display:flex;justify-content:space-between;align-items:center}}
.kpi .v{{font-family:{F};font-size:1.48rem;font-weight:500;font-variant-numeric:tabular-nums;
 letter-spacing:-.025em;margin:6px 0 3px;line-height:1.1}}
.kpi .d{{font-family:{F};font-size:.72rem}}
.kpi .spark{{margin:7px 0 2px;opacity:.85}}
.kpi .p{{font-family:{F};font-size:.56rem;color:{P['muted']};margin-top:8px;padding-top:7px;
 border-top:1px dashed {P['rule']};word-break:break-all;line-height:1.4}}

/* structure */
.sec{{font-family:{F};font-size:.69rem;text-transform:uppercase;letter-spacing:.15em;
 color:{P['dim']};border-bottom:1px solid {P['rule']};padding-bottom:7px;margin:22px 0 13px;
 display:flex;justify-content:space-between;align-items:baseline}}
.sec .meta{{font-size:.62rem;color:{P['muted']};letter-spacing:.06em;text-transform:none}}
.pan{{border:1px solid {P['rule']};background:{P['panel']};padding:16px 18px;margin-bottom:10px}}
.pan.a{{border-left:3px solid {P['green']}}}
.pan p{{line-height:1.65;font-size:.92rem;margin:0 0 .7em}}
.pan p:last-child{{margin-bottom:0}}
.empty{{border:1px dashed {P['rule']};background:transparent;padding:22px;text-align:center;
 color:{P['muted']};font-size:.87rem;line-height:1.6}}

/* flags */
.flag{{border:1px solid {P['rule']};border-left:3px solid var(--s);background:{P['panel']};
 padding:11px 14px;margin-bottom:8px;transition:background .15s}}
.flag:hover{{background:{P['panel2']}}}
.flag .t{{font-family:{F};font-size:.57rem;letter-spacing:.13em;color:var(--s);text-transform:uppercase}}
.flag .h{{font-weight:600;font-size:.93rem;margin:4px 0 3px}}
.flag .d{{font-family:{F};font-size:.71rem;color:{P['dim']}}}
.flag .w{{font-size:.85rem;color:{P['dim']};margin-top:8px;line-height:1.55;
 border-top:1px solid {P['rule']};padding-top:8px}}

/* chips & dots */
.chip{{display:inline-block;font-family:{F};font-size:.61rem;letter-spacing:.07em;padding:3px 9px;
 border:1px solid {P['rule']};color:{P['dim']};margin:0 6px 6px 0;text-transform:uppercase}}
.chip.ok{{border-color:{P['green']};color:{P['green']}}}
.chip.warn{{border-color:{P['amber']};color:{P['amber']}}}
.chip.bad{{border-color:{P['red']};color:{P['red']}}}
.dot{{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px;vertical-align:middle}}
.status{{font-family:{F};font-size:.68rem;color:{P['dim']};padding:3px 0;letter-spacing:.03em}}

/* gauge bar */
.gauge{{height:5px;background:{P['rule']};margin-top:9px;overflow:hidden}}
.gauge > span{{display:block;height:100%}}

/* streamlit chrome */
[data-testid="stSidebar"]{{background:{P['panel']};border-right:1px solid {P['rule']}}}
[data-testid="stSidebar"] .stButton button{{border-radius:0}}
.stTabs [data-baseweb="tab-list"]{{gap:2px;border-bottom:1px solid {P['rule']}}}
.stTabs [data-baseweb="tab"]{{font-family:{F};font-size:.7rem;letter-spacing:.09em;
 text-transform:uppercase;background:transparent;border-radius:0;padding:10px 16px;color:{P['muted']}}}
.stTabs [aria-selected="true"]{{background:{P['panel']};border-bottom:2px solid {P['green']};
 color:{P['ink']}}}
[data-testid="stDataFrame"]{{font-family:{F}}}
div[data-testid="stExpander"] details{{border:1px solid {P['rule']};background:{P['panel']};
 border-radius:0}}
.stButton button{{border-radius:0;font-size:.85rem}}
</style>"""


def money(v, dp=2) -> str:
    if v is None:
        return "n/a"
    a, sg = abs(v), "-" if v < 0 else ""
    for cut, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= cut:
            return f"{sg}${a/cut:,.{dp}f}{suf}"
    return f"{sg}${a:,.2f}"


def fmt(v, unit) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "n/a"
    return {"%": f"{v:.1%}", "x": f"{v:.2f}x", "days": f"{v:.0f} d",
            "$": money(v)}.get(unit, f"{v:,.2f}")


def delta(cur, prev, up=True) -> str:
    if cur is None or prev in (None, 0):
        return f"<span class='d' style='color:{P['muted']}'>no prior period</span>"
    ch = (cur - prev) / abs(prev)
    col = P["green"] if (ch >= 0) == up else P["red"]
    return f"<span class='d' style='color:{col}'>{'▲' if ch>=0 else '▼'} {abs(ch):.1%} YoY</span>"


def sparkline(vals, w=118, h=24, color=None) -> str:
    """Inline SVG trend, drawn per KPI card. Information, not decoration —
    it shows the shape behind the single headline number."""
    pts = [v for v in vals if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or abs(hi) or 1
    n = len(pts)
    col = color or (P["green"] if pts[-1] >= pts[0] else P["red"])
    xy = [(i * w / (n - 1), h - 2 - (v - lo) / rng * (h - 4)) for i, v in enumerate(pts)]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    area = f"0,{h} " + path + f" {w},{h}"
    return (f"<svg class='spark' width='{w}' height='{h}' viewBox='0 0 {w} {h}'>"
            f"<polygon points='{area}' fill='{col}' opacity='.10'/>"
            f"<polyline points='{path}' fill='none' stroke='{col}' stroke-width='1.5' "
            f"stroke-linejoin='round'/>"
            f"<circle cx='{xy[-1][0]:.1f}' cy='{xy[-1][1]:.1f}' r='2.2' fill='{col}'/></svg>")


def kpi(label, val, d="", prov="", accent=None, spark="", hint="") -> str:
    tip = f"<span title='{hint}' style='cursor:help;color:{P['muted']}'>?</span>" if hint else ""
    return (f"<div class='kpi' style='--a:{accent or P['rule']}'>"
            f"<div class='l'><span>{label}</span>{tip}</div>"
            f"<div class='v'>{val}</div>{d}{spark}"
            f"{f'<div class=p>{prov}</div>' if prov else ''}</div>")


def sec(title, meta="") -> str:
    return f"<div class='sec'><span>{title}</span><span class='meta'>{meta}</span></div>"


chip = lambda t, k="": f"<span class='chip {k}'>{t}</span>"
pan = lambda h, a=False: f"<div class='pan {'a' if a else ''}'>{h}</div>"
empty = lambda h: f"<div class='empty'>{h}</div>"
dot = lambda c: f"<span class='dot' style='background:{c}'></span>"


def status_row(label, ok, detail) -> str:
    return (f"<div class='status'>{dot(P['green'] if ok else P['red'])}{label} — "
            f"<span style='color:{P['muted']}'>{detail}</span></div>")


def gauge(pct, color) -> str:
    return f"<div class='gauge'><span style='width:{max(0,min(100,pct))}%;background:{color}'></span></div>"


def flagcard(f: Flag, why="") -> str:
    return (f"<div class='flag' style='--s:{SEV.get(f.severity,P['muted'])}'>"
            f"<div class='t'>{f.severity} · {f.category}</div><div class='h'>{f.title}</div>"
            f"<div class='d'>{f.metric}: <b>{f.observed}</b> &nbsp;·&nbsp; threshold {f.threshold}</div>"
            f"{f'<div class=w>{why}</div>' if why else ''}</div>")


# --- charts (one shared template) -------------------------------------------
CYCLE = [P["green"], P["blue"], P["amber"], P["violet"]]


def _style(fig, h=330, yt=""):
    fig.update_layout(height=h, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(family=F, size=11, color=P["dim"]),
                      margin=dict(l=8, r=8, t=30, b=8),
                      hoverlabel=dict(font_family=F, bgcolor=P["panel2"],
                                      bordercolor=P["rule"], font_color=P["ink"]),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0,
                                  font=dict(size=10), bgcolor="rgba(0,0,0,0)"),
                      xaxis=dict(showgrid=False, zeroline=False, linecolor=P["rule"]),
                      yaxis=dict(showgrid=True, gridcolor=P["rule"], gridwidth=.5, zeroline=False,
                                 title=dict(text=yt, font=dict(size=10))))
    return fig


def ch_trend(R, keys, title="", yt=""):
    fig = go.Figure()
    for i, (k, lbl) in enumerate(keys.items()):
        if k in R.index:
            fig.add_trace(go.Scatter(x=[str(c) for c in R.columns], y=R.loc[k].values, name=lbl,
                                     mode="lines+markers", line=dict(color=CYCLE[i % 4], width=2.2),
                                     marker=dict(size=6)))
    fig.update_layout(title=dict(text=title, font=dict(size=12, color=P["dim"])))
    return _style(fig, yt=yt)


def ch_group(x, series, title="", yt=""):
    fig = go.Figure()
    for i, (lbl, vals) in enumerate(series.items()):
        fig.add_trace(go.Bar(x=[str(v) for v in x], y=vals, name=lbl,
                             marker_color=CYCLE[i % 4], marker_line_width=0))
    fig.update_layout(barmode="group", title=dict(text=title, font=dict(size=12, color=P["dim"])))
    return _style(fig, yt=yt)


def ch_bars(x, y, title="", yt=""):
    fig = go.Figure(go.Bar(x=[str(v) for v in x], y=y, marker_line_width=0,
                           marker_color=[P["green"] if v >= 0 else P["red"] for v in y]))
    fig.update_layout(title=dict(text=title, font=dict(size=12, color=P["dim"])))
    return _style(fig, yt=yt)


def ch_water(labels, values, title=""):
    fig = go.Figure(go.Waterfall(x=labels, y=values, measure=["relative"] * len(values),
                                 connector=dict(line=dict(color=P["rule"], width=1)),
                                 increasing=dict(marker=dict(color=P["green"])),
                                 decreasing=dict(marker=dict(color=P["red"])),
                                 textfont=dict(family=F, size=10)))
    fig.update_layout(title=dict(text=title, font=dict(size=12, color=P["dim"])))
    return _style(fig, 350)


def ch_tornado(df, base, title=""):
    fig = go.Figure()
    fig.add_trace(go.Bar(y=df["Variable"], x=df["Value at low"] - base, orientation="h",
                         name="Downside", marker_color=P["red"], marker_line_width=0))
    fig.add_trace(go.Bar(y=df["Variable"], x=df["Value at high"] - base, orientation="h",
                         name="Upside", marker_color=P["green"], marker_line_width=0))
    fig.update_layout(barmode="relative", title=dict(text=title, font=dict(size=12, color=P["dim"])))
    fig = _style(fig, 310, "Change in equity value vs base case")
    fig.update_yaxes(showgrid=False, autorange="reversed")
    return fig


def ch_heat(df, title="", xt="", yt=""):
    fig = go.Figure(go.Heatmap(z=df.values, x=list(df.columns), y=list(df.index),
                               colorscale=[[0, P["red"]], [.5, P["panel2"]], [1, P["green"]]],
                               colorbar=dict(outlinewidth=0, tickfont=dict(family=F, size=9)),
                               hovertemplate="%{y} / %{x}<br>$%{z:,.2f} per share<extra></extra>"))
    fig.update_layout(title=dict(text=title, font=dict(size=12, color=P["dim"])))
    fig = _style(fig, 350)
    fig.update_xaxes(title=dict(text=xt, font=dict(size=10)))
    fig.update_yaxes(showgrid=False, title=dict(text=yt, font=dict(size=10)))
    return fig


def ch_scen(names, vals, price=None, title=""):
    cmap = {"Optimistic": P["green"], "Base case": P["blue"], "Pessimistic": P["red"],
            "AI scenario": P["violet"]}
    fig = go.Figure(go.Bar(x=names, y=vals, marker_line_width=0,
                           marker_color=[cmap.get(n, P["blue"]) for n in names],
                           text=[f"${v:,.2f}" for v in vals], textposition="outside",
                           textfont=dict(family=F, size=11, color=P["ink"])))
    if price:
        fig.add_hline(y=price, line=dict(color=P["dim"], width=1.4, dash="dot"),
                      annotation_text=f"market price ${price:,.2f}",
                      annotation_font=dict(family=F, size=10, color=P["dim"]))
    fig.update_layout(title=dict(text=title, font=dict(size=12, color=P["dim"])))
    return _style(fig, 320, "Intrinsic value per share")


# ============================================================================ #
# APP
# ============================================================================ #
st.set_page_config(page_title="Financial Statement Intelligence", page_icon="📊",
                   layout="wide", initial_sidebar_state="expanded")
st.markdown(CSS, unsafe_allow_html=True)

def _pick_ticker(t: str) -> None:
    """Set from an on_click callback. Callbacks run BEFORE the rerun, which is the
    only legal moment to write to a key that a widget already owns."""
    st.session_state.ticker_in = t


st.session_state.setdefault("ticker_in", "AAPL")

with st.sidebar:
    st.markdown("### Financial Statement Intelligence")
    st.caption("SEC XBRL → normalized statements → analytics → AI layer")

    source = st.radio("Data source", ["Demo company", "SEC EDGAR (live)"],
                      captions=["Offline, no keys needed", "Any US 10-K filer"])
    live = source.startswith("SEC")

    if live:
        st.text_input("Ticker", key="ticker_in", placeholder="AAPL")
        qc = st.columns(4)
        for i, t in enumerate(QUICK_TICKERS):
            qc[i % 4].button(t, key=f"qt{t}", width="stretch",
                             on_click=_pick_ticker, args=(t,))
    ticker = (st.session_state.get("ticker_in") or "AAPL").strip().upper()
    nyears = st.slider("Fiscal years", 3, 10, 6)
    go_btn = st.button("Load company", type="primary", width="stretch")

    st.markdown("---")
    prov_name = llm_provider()
    st.markdown(status_row("EDGAR", True, "ready" if live else "not in use"), unsafe_allow_html=True)
    st.markdown(status_row("Price feed", bool(secret("FINNHUB_API_KEY")),
                           "Finnhub" if secret("FINNHUB_API_KEY") else "no key — manual entry"),
                unsafe_allow_html=True)
    _model_label = {"nvidia": NVIDIA_MODEL.split("/")[-1], "gemini": "gemini",
                    "anthropic": "claude", "openai": "gpt"}.get(prov_name, "")
    st.markdown(status_row("AI layer", prov_name != "off",
                           f"{prov_name} · {_model_label}" if prov_name != "off"
                           else "deterministic only"), unsafe_allow_html=True)
    st.caption(f"{len(CONCEPTS)} concepts · {len(DERIVATIONS)} derivations · "
               f"{len(VALIDATIONS)} validators")

# --- load -------------------------------------------------------------------
if "data" not in st.session_state or go_btn:
    if not live:
        st.session_state.data, st.session_state.demo = demo_company(), True
    else:
        try:
            with st.status(f"Loading {ticker}…", expanded=True) as s:
                st.write("Resolving ticker to CIK…")
                prof = fetch_submissions(ticker, SEC_UA())
                st.write(f"Fetching XBRL company facts for CIK {prof['cik']:010d}…")
                fj = fetch_facts(prof["cik"], SEC_UA())
                st.write("Resolving tags to canonical concepts…")
                facts = derive(resolve(fj, nyears))
                st.write("Validating accounting identities…")
                vals, score = validate(facts)
                st.session_state.data = {"profile": prof, "facts": [asdict(f) for f in facts],
                                         "unmapped": find_unmapped(fj), "validations": vals,
                                         "quality": score}
                st.session_state.demo = False
                s.update(label=f"{prof['name']} loaded — data quality {score}%", state="complete",
                         expanded=False)
        except Exception as exc:
            st.error(f"{exc}\n\nFalling back to the demo company so the dashboard stays usable.")
            st.session_state.data, st.session_state.demo = demo_company(), True

d = st.session_state.data
C = Company(d["profile"], [Fact(**f) for f in d["facts"]], d["unmapped"], d["validations"], d["quality"])
years = C.years
if not years:
    st.error("No annual facts resolved for this filer. Try another ticker or the demo company.")
    st.stop()
Y, PRV = years[-1], (years[-2] if len(years) > 1 else None)

# --- price ------------------------------------------------------------------
IS_DEMO = st.session_state.get("demo", True)
quote, fprofile = {}, {}
if IS_DEMO:
    price, psrc, chg = 41.50, "demo constant", 0.0
else:
    quote = finnhub_quote(C.profile["ticker"], secret("FINNHUB_API_KEY"))
    fprofile = finnhub_profile(C.profile["ticker"], secret("FINNHUB_API_KEY"))
    price = quote.get("price") if quote.get("ok") else None
    psrc = "Finnhub" if quote.get("ok") else (quote.get("error") or "unavailable")
    chg = quote.get("change_pct", 0.0)

manual = st.session_state.get("manual_price", 0.0)
if manual and manual > 0:
    price, psrc, chg = manual, "manual entry", 0.0

# --- computed layers --------------------------------------------------------
RT = compute_ratios(C, price)
mcap = price * C.get("shares_outstanding", Y) if (price and C.get("shares_outstanding", Y)) else None
FLAGS = detect(C, RT, mcap)
RISK = risk_score(FLAGS)
BASE = base_drivers(C)
SCEN_SET = calibrated(C)
if "ai_drivers" in st.session_state:
    SCEN_SET = {**SCEN_SET, "AI scenario": Drivers(**st.session_state.ai_drivers)}
RESULTS = run_scenarios(C, SCEN_SET, price)
BREAK_EVEN = goal_seek(C, BASE, price) if price else None

# --- masthead ---------------------------------------------------------------
p = C.profile
logo = fprofile.get("logo", "")
px_html = ""
if price:
    ccol = P["green"] if chg >= 0 else P["red"]
    px_html = (f"<div class='px'><div class='v'>${price:,.2f}</div>"
               f"<div class='c' style='color:{ccol if chg else P['muted']}'>"
               f"{(f'{chg:+.2f}% today' if chg else psrc)}</div></div>")

st.markdown(f"""<div class='mast'>
{f"<img src='{logo}'/>" if logo else ""}
<div><div class='n'>{p['name']}</div><div class='m'>{p['ticker']}
· CIK {p['cik']:010d} · {p.get('sic_description','')} · FY end {p.get('fiscal_year_end','')}
· {len(years)} years · latest FY{Y}</div></div>{px_html}</div>""", unsafe_allow_html=True)

qcol = P["green"] if C.quality >= 90 else P["amber"] if C.quality >= 70 else P["red"]
rcol = P["green"] if RISK["score"] >= 80 else P["amber"] if RISK["score"] >= 55 else P["red"]
h = st.columns([1, 1, 1, 1])
h[0].markdown(f"<div class='status'>{dot(qcol)}Data quality <b>{C.quality}%</b></div>"
              + gauge(C.quality, qcol), unsafe_allow_html=True)
h[1].markdown(f"<div class='status'>{dot(rcol)}Risk <b>{RISK['score']}/100</b> · {RISK['label']}</div>"
              + gauge(RISK["score"], rcol), unsafe_allow_html=True)
h[2].markdown(f"<div class='status'>{dot(P['green'] if price else P['red'])}Price · {psrc}</div>",
              unsafe_allow_html=True)
st.session_state.setdefault("manual_price", 0.0)
h[3].number_input("Override price", min_value=0.0, step=0.01, key="manual_price",
                  label_visibility="collapsed", placeholder="Override price")

if IS_DEMO:
    st.info("**Demo company.** Figures are synthetic and internally consistent, so every validator, "
            "ratio, scenario and AI fallback runs against the real code path. Switch to SEC EDGAR "
            "in the sidebar for a live filer.")
if p.get("is_financial"):
    st.warning("**Financial-sector filer** (SIC 6000–6799). Inventory, gross margin and "
               "working-capital ratios do not apply to bank or insurer balance sheets and are "
               "suppressed here.")
if not price:
    st.warning(f"**No share price** ({psrc}). Market value, P/B and P/E are hidden. Enter a price "
               "above, or add a Finnhub key in the sidebar.")

T1, T2, T3, T4, T5 = st.tabs(["Overview", "Statements & cash flow", "Scenario lab",
                              "Risk & alerts", "AI insights"])

prov_of = lambda k, y: (
    "" if not C.prov(k, y) else
    (f"derived · {C.prov(k,y).formula} · FY{y}" if C.prov(k, y).derived
     else f"{C.prov(k,y).source_tag}<br>FY{y} · {C.prov(k,y).period_end} · {C.prov(k,y).accession}"))


def rv(key, year):
    try:
        v = RT.loc[key, year]
        return None if pd.isna(v) else float(v)
    except (KeyError, TypeError):
        return None


rseries = lambda key: ([None if pd.isna(v) else float(v) for v in RT.loc[key].values]
                       if key in RT.index else [])

# --- TAB 1: OVERVIEW --------------------------------------------------------
with T1:
    st.markdown(sec(f"Key performance indicators", f"fiscal year {Y}"), unsafe_allow_html=True)
    k = st.columns(4)
    cards = [
        ("Revenue", C.get("revenue", Y), C.get("revenue", PRV), C.series("revenue"),
         prov_of("revenue", Y), P["green"], "Total revenue as filed"),
        ("Operating cash flow", C.get("cfo", Y), C.get("cfo", PRV), C.series("cfo"),
         prov_of("cfo", Y), P["green"], "Cash generated by operations"),
        ("Free cash flow", fcf(C, Y), fcf(C, PRV), [fcf(C, y) for y in years],
         f"derived · CFO − capex · FY{Y}", P["green"], "CFO minus capital expenditure"),
        ("Book value (equity)", C.get("total_equity", Y), C.get("total_equity", PRV),
         C.series("total_equity"), prov_of("total_equity", Y), P["blue"],
         "Shareholders' equity as filed"),
    ]
    for col, (lbl, cur, prv, ser, pr, acc, hint) in zip(k, cards):
        col.markdown(kpi(lbl, money(cur), delta(cur, prv), pr, acc, sparkline(ser), hint),
                     unsafe_allow_html=True)

    k2 = st.columns(4)
    k2[0].markdown(kpi("Market value", money(mcap) if mcap else "n/a",
                       f"<span class='d' style='color:{P['muted']}'>price × shares outstanding</span>",
                       f"price {money(price) if price else '—'} · {psrc}", P["blue"], "",
                       "Market capitalisation"), unsafe_allow_html=True)
    for col, key, lbl in ((k2[1], "price_to_book", "Price / book"),
                          (k2[2], "operating_margin", "Operating margin"),
                          (k2[3], "roic", "Return on invested capital")):
        meta = RATIO_META.get(key)
        if not meta:
            col.markdown(kpi(lbl, "n/a", "", "requires a share price", P["rule"]),
                         unsafe_allow_html=True)
            continue
        col.markdown(kpi(lbl, fmt(rv(key, Y), meta[1]), delta(rv(key, Y), rv(key, PRV), meta[4]),
                         meta[3], P["blue"] if key == "price_to_book" else P["green"],
                         sparkline(rseries(key)), meta[3]), unsafe_allow_html=True)

    st.markdown(sec("Performance trend", f"{years[0]}–{years[-1]}"), unsafe_allow_html=True)
    a, b = st.columns([3, 2])
    a.plotly_chart(ch_group(years, {"Revenue": C.series("revenue"),
                                    "Net income": C.series("net_income"),
                                    "Operating cash flow": C.series("cfo")},
                            "Revenue, earnings and cash generation", "USD"), width="stretch")
    b.plotly_chart(ch_trend(RT, {"operating_margin": "Operating margin", "net_margin": "Net margin",
                                 "fcf_margin": "FCF margin"}, "Margin structure", "% of revenue"),
                   width="stretch")

    st.markdown(sec("Ratio summary", "select groups to expand"), unsafe_allow_html=True)
    grps = sorted({v[2] for v in RATIO_META.values()})
    pick = st.multiselect("Ratio groups", grps, label_visibility="collapsed",
                          default=[g for g in grps if g in ("Profitability", "Returns", "Leverage")])
    keys = [k for k, v in RATIO_META.items() if v[2] in pick and k in RT.index]
    if keys:
        disp = RT.loc[keys].copy()
        disp.index = [f"{RATIO_META[k][0]}  ({RATIO_META[k][1]})" for k in keys]
        st.dataframe(disp.style.format(lambda v: "—" if pd.isna(v) else f"{v:,.3f}"),
                     width="stretch")
        st.download_button("Download ratios (CSV)", disp.to_csv().encode(),
                           f"{p['ticker']}_ratios.csv", "text/csv")
    else:
        st.markdown(empty("Select at least one ratio group above."), unsafe_allow_html=True)

    st.markdown(sec("Data quality", "accounting identities asserted per fiscal year"),
                unsafe_allow_html=True)
    vdf = pd.DataFrame(C.validations)
    if not vdf.empty:
        vdf["Result"] = vdf["passed"].map({True: "Pass", False: "FAIL"})
        st.dataframe(vdf[["FY", "Check", "Severity", "Result", "Detail"]], width="stretch",
                     hide_index=True, height=200)

# --- TAB 2: STATEMENTS & CASH FLOW -----------------------------------------
with T2:
    c1, c2 = st.columns([2, 3])
    which = c1.radio("Statement", ["Income statement", "Balance sheet", "Cash flow statement"],
                     horizontal=True, label_visibility="collapsed")
    mode = c2.radio("View", ["As reported", "Common size", "Year-over-year change"],
                    horizontal=True, label_visibility="collapsed")
    skey = {"Income statement": IS, "Balance sheet": BS, "Cash flow statement": CF}[which]
    present = {f.concept for f in C.facts}
    rows = [c for c, v in CONCEPTS.items() if v[0] == skey and c in present]
    frame = pd.DataFrame({y: {c: C.get(c, y) for c in rows} for y in years}).reindex(rows)
    frame.index = [CONCEPTS[c][1] for c in rows]

    if mode == "Common size":
        den = "revenue" if skey != BS else "total_assets"
        st.caption(f"Each line as a percentage of {CONCEPTS[den][1].lower()}.")
        shown = frame.divide(pd.Series({y: C.get(den, y) for y in years}), axis=1)
        st.dataframe(shown.style.format(lambda v: "—" if pd.isna(v) else f"{v:.1%}"),
                     width="stretch", height=460)
    elif mode == "Year-over-year change":
        st.dataframe(frame.pct_change(axis=1).style.format(
            lambda v: "—" if pd.isna(v) else f"{v:+.1%}"), width="stretch", height=460)
    else:
        st.dataframe(frame.style.format(lambda v: "—" if pd.isna(v) else money(v, 1)),
                     width="stretch", height=460)
    st.download_button("Download statement (CSV)", frame.to_csv().encode(),
                       f"{p['ticker']}_{skey}.csv", "text/csv")

    if skey == IS and C.get("revenue", Y):
        st.markdown(sec("Revenue to net income bridge", f"fiscal year {Y}"), unsafe_allow_html=True)
        steps = [("Revenue", C.get("revenue", Y)),
                 ("Cost of revenue", -(C.get("cost_of_revenue", Y) or 0)),
                 ("Operating expenses", -(C.get("operating_expenses", Y) or 0)),
                 ("Interest", -(C.get("interest_expense", Y) or 0)),
                 ("Tax", -(C.get("income_tax", Y) or 0))]
        steps = [s for s in steps if s[1]]
        st.plotly_chart(ch_water([s[0] for s in steps], [s[1] for s in steps]), width="stretch")

    st.markdown(sec("Cash flow"), unsafe_allow_html=True)
    f1, f2 = st.columns([3, 2])
    f1.plotly_chart(ch_group(years, {"Operating": C.series("cfo"), "Investing": C.series("cfi"),
                                     "Financing": C.series("cff")},
                             "Operating, investing and financing cash flow", "USD"), width="stretch")
    f2.plotly_chart(ch_bars(years, [fcf(C, y) or 0 for y in years], "Free cash flow", "USD"),
                    width="stretch")

    st.markdown(sec("Earnings quality", "cash generated versus profit reported"),
                unsafe_allow_html=True)
    e1, e2 = st.columns(2)
    e1.plotly_chart(ch_group(years, {"Net income": C.series("net_income"),
                                     "Operating cash flow": C.series("cfo")},
                             "Reported profit versus cash generated", "USD"), width="stretch")
    e1.caption("Cash flow persistently below reported profit is the classic earnings-quality "
               "warning. The gap is quantified as the accruals ratio.")
    e2.plotly_chart(ch_trend(RT, {"cash_conversion": "CFO / net income",
                                  "accruals_ratio": "Accruals ratio"},
                             "Conversion and accruals", "ratio"), width="stretch")

    if "dso" in RT.index:
        st.plotly_chart(ch_trend(RT, {"dso": "Days sales outstanding", "dio": "Days inventory",
                                      "dpo": "Days payables"}, "Cash conversion cycle", "days"),
                        width="stretch")

    st.markdown(sec("Provenance", "every figure traced to its filing"), unsafe_allow_html=True)
    lineage = pd.DataFrame([{
        "Concept": CONCEPTS[f.concept][1], "FY": f.fiscal_year, "Value": money(f.value),
        "Source tag": "(derived)" if f.derived else f.source_tag, "Formula": f.formula,
        "Period end": f.period_end, "Accession": f.accession,
        "Restated": "yes" if f.restated else ""}
        for f in sorted(C.facts, key=lambda x: (-x.fiscal_year, x.concept))])
    l1, l2 = st.columns([1, 3])
    fy = l1.selectbox("Fiscal year", ["All"] + [str(y) for y in reversed(years)])
    view = lineage if fy == "All" else lineage[lineage["FY"] == int(fy)]
    st.dataframe(view, width="stretch", hide_index=True, height=320)
    st.markdown(f"<a href='{p.get('filings_url','#')}' target='_blank' style='font-family:{F};"
                f"font-size:.75rem;color:{P['green']}'>→ open this company's filings on sec.gov</a>",
                unsafe_allow_html=True)

# --- TAB 3: SCENARIO LAB ----------------------------------------------------
with T3:
    st.markdown(sec("Scenario comparison", "calibrated to this company's own driver percentiles"),
                unsafe_allow_html=True)
    st.caption("Optimistic and pessimistic use the 90th and 10th percentile of realised historical "
               "drivers, not an arbitrary ±10%.")
    st.dataframe(pd.DataFrame([{
        "Scenario": r.name, "Revenue growth": f"{r.drivers.revenue_growth:.1%}",
        "Gross margin": f"{r.drivers.gross_margin:.1%}", "WACC": f"{r.drivers.wacc:.1%}",
        "Equity value": money(r.equity_value),
        "Value per share": f"${r.value_per_share:,.2f}" if r.value_per_share else "n/a",
        "Decision": r.decision} for r in RESULTS]), width="stretch", hide_index=True)
    st.plotly_chart(ch_scen([r.name for r in RESULTS], [r.value_per_share or 0 for r in RESULTS],
                            price, "Intrinsic value per share by scenario"), width="stretch")

    st.markdown(sec("Assumption controls", "drag to model your own case"), unsafe_allow_html=True)
    s1, s2, s3 = st.columns(3)
    M = BASE.copy()
    M.revenue_growth = s1.slider("Revenue growth", -.30, .50, float(BASE.revenue_growth), .005,
                                 "%.3f", help="Compound annual growth applied to each forecast year")
    M.gross_margin = s1.slider("Gross margin", .05, .90, float(BASE.gross_margin), .005, "%.3f")
    M.opex_pct_revenue = s2.slider("Opex % of revenue", .02, .70, float(BASE.opex_pct_revenue),
                                   .005, "%.3f")
    M.capex_pct_revenue = s2.slider("Capex % of revenue", 0.0, .30, float(BASE.capex_pct_revenue),
                                    .002, "%.3f")
    M.wacc = s3.slider("WACC (discount rate)", .03, .25, float(BASE.wacc), .0025, "%.4f",
                       help="Weighted average cost of capital used to discount forecast cash flows")
    M.terminal_growth = s3.slider("Terminal growth", 0.0, .045, float(BASE.terminal_growth),
                                  .0025, "%.4f")

    LIVE = value(C, M, "Manual")
    v1, v2, v3 = st.columns(3)
    v1.markdown(kpi("Enterprise value", money(LIVE.enterprise_value), "",
                    "PV of forecast FCF + PV of terminal value", P["blue"]), unsafe_allow_html=True)
    v2.markdown(kpi("Equity value", money(LIVE.equity_value), "",
                    "enterprise value − net debt", P["blue"]), unsafe_allow_html=True)
    v3.markdown(kpi("Value per share",
                    f"${LIVE.value_per_share:,.2f}" if LIVE.value_per_share else "n/a",
                    delta(LIVE.value_per_share, price) if price else "",
                    "equity value ÷ shares outstanding", P["green"]), unsafe_allow_html=True)

    with st.expander("Projected free cash flow"):
        st.dataframe(LIVE.projection.style.format(lambda v: f"{v:,.0f}"), width="stretch")

    st.markdown(sec("Sensitivity analysis", "at least two variables required"), unsafe_allow_html=True)
    svars = st.multiselect("Variables to test", list(DRIVER_LABELS), label_visibility="collapsed",
                           default=["revenue_growth", "wacc", "gross_margin", "capex_pct_revenue"],
                           format_func=lambda k: DRIVER_LABELS[k])
    if len(svars) >= 2:
        TD = tornado(C, M, svars)
        st.plotly_chart(ch_tornado(TD, LIVE.equity_value,
                                   "Equity value sensitivity, ±20% on each driver"), width="stretch")
        st.markdown(pan(f"<p><b>{TD.iloc[0]['Variable']}</b> dominates: a ±20% move changes equity "
                        f"value by {money(TD.iloc[0]['Swing'])}, "
                        f"{TD.iloc[0]['Impact %']:.0%} of the base case. "
                        f"<b>{TD.iloc[-1]['Variable']}</b> matters least "
                        f"({TD.iloc[-1]['Impact %']:.0%}).</p>"), unsafe_allow_html=True)
        st.plotly_chart(ch_heat(two_way(C, M, "wacc", "terminal_growth"),
                                "Value per share: WACC versus terminal growth",
                                "WACC", "Terminal growth"), width="stretch")
    else:
        st.markdown(empty("Select at least two variables to run the sensitivity analysis."),
                    unsafe_allow_html=True)

    st.markdown(sec("Goal seek", "bisection on the deterministic model"), unsafe_allow_html=True)
    g1, g2 = st.columns([1, 2])
    tgt = g1.number_input("Target value per share (USD)", value=float(round(price or 50.0, 2)),
                          step=1.0)
    solved = goal_seek(C, M, tgt)
    g2.markdown(pan(f"<p>To reach <b>${tgt:,.2f}</b> per share, sustained annual revenue growth "
                    f"would need to be <b>{solved:.2%}</b>, against a base assumption of "
                    f"{M.revenue_growth:.2%}.</p>" if solved is not None
                    else "<p>No solution inside the search bounds.</p>"), unsafe_allow_html=True)

# --- TAB 4: RISK & ALERTS ---------------------------------------------------
with T4:
    z = altman_z(C, Y, mcap)
    fsc, fsig = piotroski(C, Y)
    st.markdown(sec("Risk and alert panel", "rule-based — no model involved"), unsafe_allow_html=True)
    r = st.columns(4)
    r[0].markdown(kpi("Composite risk score", f"{RISK['score']}/100",
                      f"<span class='d' style='color:{rcol}'>{RISK['label']}</span>",
                      "100 − 18×high − 8×medium − 3×low", rcol), unsafe_allow_html=True)
    r[1].markdown(kpi("Altman Z-Score", str(z) if z else "n/a",
                      f"<span class='d' style='color:{P['dim']}'>zone: {altman_band(z)}</span>",
                      "1.2·WC/TA + 1.4·RE/TA + 3.3·EBIT/TA + 0.6·MVE/TL + Sales/TA", P["blue"]),
                  unsafe_allow_html=True)
    r[2].markdown(kpi("Piotroski F-Score", f"{fsc}/9" if fsc is not None else "n/a", "",
                      "nine fundamental strength signals", P["blue"]), unsafe_allow_html=True)
    r[3].markdown(kpi("Signals triggered", str(len(FLAGS)), "",
                      f"{RISK['counts']['high']} high · {RISK['counts']['medium']} medium",
                      P["amber"]), unsafe_allow_html=True)

    st.markdown(sec("Triggered signals", f"{len(FLAGS)} of 12 rules fired"), unsafe_allow_html=True)
    if not FLAGS:
        st.success("No signals triggered by the forensic rule set for this period.")
    else:
        cats = sorted({f.category for f in FLAGS})
        sel = st.multiselect("Filter by category", cats, default=cats,
                             label_visibility="collapsed")
        why = st.session_state.get("anomaly_why", {})
        for f in [f for f in FLAGS if f.category in sel]:
            st.markdown(flagcard(f, why.get(f.code, "")), unsafe_allow_html=True)

    if fsig:
        with st.expander("Piotroski F-Score breakdown"):
            st.dataframe(pd.DataFrame([{"Signal": s, "Result": "Pass" if ok else "Fail"}
                                       for s, ok in fsig]), width="stretch", hide_index=True)

# --- TAB 5: AI INSIGHTS -----------------------------------------------------
with T5:
    if llm_provider() == "off":
        st.warning("**No LLM provider connected.** Each feature falls back to deterministic "
                   "output, so the dashboard stays fully functional. Set a key in the CONFIG "
                   "block at the top of app.py to enable the model layer.")
    else:
        st.markdown(chip(f"provider: {llm_provider()}", "ok")
                    + chip("responses cached by input hash")
                    + chip("every figure verified against the engine", "ok"),
                    unsafe_allow_html=True)

    A1, A2, A3, A4, A5 = st.tabs(["Recommendation", "Anomaly analysis", "Scenario generator",
                                  "Data extraction", "Finance assistant"])

    def badge(res):
        if "verify" in res:
            txt, cls = verify_badge(res["verify"])
            st.markdown(chip(txt, cls), unsafe_allow_html=True)

    with A1:
        st.markdown(sec("AI recommendation panel", "feature 4 of 5"), unsafe_allow_html=True)
        if st.button("Generate recommendation", type="primary"):
            with st.spinner("Synthesising…"):
                st.session_state.reco = ai_recommend(C, RT, FLAGS, RESULTS, price, BREAK_EVEN)
        res = st.session_state.get("reco")
        if not res:
            st.markdown(empty("Consumes only the outputs of the other layers — ratios, scenarios, "
                              "forensic signals, and the break-even solve.<br>It never sees raw "
                              "filing text, and never performs a calculation."), unsafe_allow_html=True)
        elif res["ok"]:
            st.markdown(pan(
                (f"<div class='sec' style='margin-top:0'>{res['decision']}</div>"
                 if res.get("decision") else "")
                + (f"<p><b>{res['headline']}</b></p>" if res.get("headline") else "")
                + f"<p>{res['recommendation']}</p>"
                + (f"<p style='color:{P['dim']}'><b>Reverses if:</b> {res['reversal_condition']}</p>"
                   if res.get("reversal_condition") else "")
                + (f"<p style='color:{P['muted']};font-size:.85rem'><b>Limitation:</b> "
                   f"{res['limitation']}</p>" if res.get("limitation") else ""), True),
                unsafe_allow_html=True)
            badge(res)
            if res.get("fallback"):
                st.caption("Rule-generated fallback — no model call was made.")
        else:
            st.error(res["error"])

    with A2:
        st.markdown(sec("Anomaly detection and interpretation", "feature 3 of 5"),
                    unsafe_allow_html=True)
        if st.button("Analyse signals"):
            with st.spinner("Ranking…"):
                res = ai_anomalies(C, RT, FLAGS)
                st.session_state.anom = res
                st.session_state.anomaly_why = {r["code"]: r.get("why_it_matters", "")
                                                for r in res.get("ranked", []) if r.get("code")}
        res = st.session_state.get("anom")
        if not res:
            st.markdown(empty(f"{len(FLAGS)} signal(s) were detected deterministically by the "
                              "forensic rule set.<br>The model ranks and explains them — it does "
                              "not detect them. Explanations appear on the Risk &amp; alerts tab too."),
                        unsafe_allow_html=True)
        elif res["ok"]:
            if res.get("headline"):
                st.markdown(pan(f"<p><b>{res['headline']}</b></p>", True), unsafe_allow_html=True)
            for rk in res.get("ranked", []):
                with st.expander(f"{rk.get('rank','·')}. {rk.get('code','')} — "
                                 f"{rk.get('why_it_matters','')[:80]}"):
                    for lbl, key in (("Why it matters", "why_it_matters"),
                                     ("Concerning reading", "concerning_reading"),
                                     ("Benign reading", "benign_reading"),
                                     ("What to check", "what_to_check")):
                        st.markdown(f"**{lbl}** — {rk.get(key,'')}")
            badge(res)
        else:
            st.error(res["error"])

    with A3:
        st.markdown(sec("Natural-language scenario generator", "feature 2 of 5"),
                    unsafe_allow_html=True)
        presets = {"Recession": "A moderate recession compresses demand for two years while input "
                                "costs stay elevated and management defers expansion capex.",
                   "Margin squeeze": "Competitive pricing pressure erodes gross margin while "
                                     "operating costs stay fixed.",
                   "Expansion": "Management wins a large multi-year contract and reinvests "
                                "aggressively in capacity."}
        st.session_state.setdefault("scen_prompt", list(presets.values())[0])
        pc = st.columns(len(presets))
        for i, (lbl, txt) in enumerate(presets.items()):
            pc[i].button(lbl, key=f"pre{i}", width="stretch",
                         on_click=lambda t=txt: st.session_state.update(scen_prompt=t))
        prompt = st.text_area("Describe a scenario", key="scen_prompt", height=80)
        if st.button("Generate assumptions"):
            with st.spinner("Translating to drivers…"):
                res = ai_scenario(C, BASE, prompt)
                st.session_state.scen_ai = res
                if res["ok"]:
                    st.session_state.ai_drivers = res["drivers"]
                    st.rerun()
        res = st.session_state.get("scen_ai")
        if not res:
            st.markdown(empty("The model produces only assumption values, hard-clamped to preset "
                              "bounds.<br>Every projection and valuation figure is computed by the "
                              "deterministic engine."), unsafe_allow_html=True)
        elif res["ok"]:
            st.markdown(pan(f"<p>{res.get('rationale','')}</p>" + (
                f"<p style='color:{P['dim']}'><b>Key assumption:</b> {res['key_assumption']}</p>"
                if res.get("key_assumption") else ""), True), unsafe_allow_html=True)
            dd = Drivers(**res["drivers"])
            cdf = pd.DataFrame({"Base case": {DRIVER_LABELS[k]: v for k, v in asdict(BASE).items()},
                                "AI scenario": {DRIVER_LABELS[k]: v for k, v in asdict(dd).items()}})
            cdf["Change"] = cdf["AI scenario"] - cdf["Base case"]
            st.dataframe(cdf.style.format("{:.4f}"), width="stretch")
            out = value(C, dd, "AI scenario")
            st.markdown(chip(f"value per share ${out.value_per_share:,.2f}"
                             if out.value_per_share else "value per share n/a", "ok"),
                        unsafe_allow_html=True)
            st.success("Applied — the AI scenario now appears in the Scenario lab comparison.")
        else:
            st.error(res["error"])

    with A4:
        st.markdown(sec("Semantic financial-data extraction", "feature 1 of 5"),
                    unsafe_allow_html=True)
        st.caption(f"{len(C.unmapped)} material tag(s) fall outside the concept registry. The model "
                   "classifies the label and description; it never reads the value.")
        if C.unmapped:
            st.dataframe(pd.DataFrame(C.unmapped)[["taxonomy", "tag", "label", "latest_value",
                                                   "is_custom"]], width="stretch", hide_index=True)
        if st.button("Map unregistered tags"):
            with st.spinner("Classifying…"):
                st.session_state.extract = ai_extract(C)
        res = st.session_state.get("extract")
        if res and res["ok"]:
            if res["mappings"]:
                st.dataframe(pd.DataFrame(res["mappings"]), width="stretch", hide_index=True)
            st.markdown(pan(f"<p>{res['note']}</p>"), unsafe_allow_html=True)
        elif res:
            st.error(res["error"])

    with A5:
        st.markdown(sec("Finance assistant", "feature 5 of 5"), unsafe_allow_html=True)
        st.caption("Answers only from computed data. Every figure is checked against the "
                   "calculation engine before display; unverifiable ones are marked ⚠.")
        qc = st.columns(3)
        for i, q in enumerate(SAMPLE_QS):
            qc[i % 3].button(q, key=f"q{i}", width="stretch",
                             on_click=lambda t=q: st.session_state.update(pending_q=t))
        typed = st.chat_input("Ask about this company's financials")
        question = typed or st.session_state.pop("pending_q", None)
        st.session_state.setdefault("chat", [])
        for t in st.session_state.chat:
            with st.chat_message(t["role"]):
                st.markdown(t["content"])
        if question:
            st.session_state.chat.append({"role": "user", "content": question})
            with st.chat_message("user"):
                st.markdown(question)
            with st.chat_message("assistant"):
                with st.spinner("Checking the data…"):
                    res = ai_ask(C, RT, FLAGS, RESULTS, question)
                if res["ok"]:
                    st.markdown(res["answer"])
                    if res.get("data_gap"):
                        st.caption(f"Data gap: {res['data_gap']}")
                    badge(res)
                    st.session_state.chat.append({"role": "assistant", "content": res["answer"]})
                else:
                    st.error(res["error"])

    if TELEMETRY:
        with st.expander("Model telemetry"):
            st.dataframe(pd.DataFrame(TELEMETRY), width="stretch", hide_index=True)
