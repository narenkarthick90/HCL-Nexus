"""Governed scorecard: the brief's section-4 formulas, in deterministic code.

This is the ONLY place where portfolio numbers are calculated. The assistant reads these values
(through governed SQL views) and may explain them, but never computes new ones.

Input: curated tables from value.clean (register, usage, financials). Output: one row per initiative with
raw values, display strings (so the language model never formats or rounds numbers), the status rule that
fired, and per-metric provenance.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import pandas as pd

CALC_VERSION = "scorecard-1.0"
GREEN_ADOPTION, RED_ADOPTION = 0.60, 0.30

FORMULAS = {
    "adoption": "weekly active users in the latest week / licensed users",
    "wau_change": "(latest-week WAU - first-week WAU) / first-week WAU, over the supplied three weeks",
    "roi": "(realised benefit to date - total cost to date) / total cost to date",
    "avg_monthly_benefit": "realised benefit to date / months live",
    "payback_months": "total cost to date / average monthly benefit",
    "run_rate_vs_plan": "average monthly benefit x 12 / planned annual benefit",
    "status": "Green if adoption >= 60% and ROI > 0; Red if adoption < 30% or ROI < 0; otherwise Amber",
}


# ---------------------------------------------------------------- formatting (display strings)
def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def fmt_pct(x, signed: bool = False) -> str | None:
    if not _ok(x):
        return None
    s = f"{x * 100:+,.1f}%" if signed else f"{x * 100:,.1f}%"
    return s.replace("+0.0%", "0.0%").replace("-0.0%", "0.0%")


def fmt_usd(x) -> str | None:
    return f"USD {x:,.0f}" if _ok(x) else None


def fmt_months(x) -> str | None:
    return f"{x:,.1f} months" if _ok(x) else None


def fmt_int(x) -> str | None:
    return f"{int(x):,}" if _ok(x) else None


def months_live(go_live: dt.date, as_of: dt.date) -> int:
    """Whole calendar months from go-live to the reporting date (assumption A1)."""
    return (as_of.year - go_live.year) * 12 + as_of.month - go_live.month - (1 if as_of.day < go_live.day else 0)


@dataclass
class Inputs:
    initiative_id: str
    licensed_users: float | None
    wau_first: float | None
    wau_latest: float | None
    first_week: str | None
    latest_week: str | None
    total_cost: float | None
    realised_benefit: float | None
    months: int | None
    planned_annual_benefit: float | None
    notes: list[str] = field(default_factory=list)


def _div(a, b):
    return a / b if _ok(a) and _ok(b) and b != 0 else None


def compute_one(i: Inputs) -> dict:
    adoption = _div(i.wau_latest, i.licensed_users)
    wau_change = _div(i.wau_latest - i.wau_first, i.wau_first) if _ok(i.wau_latest) and _ok(i.wau_first) else None
    roi = _div(i.realised_benefit - i.total_cost, i.total_cost) if _ok(i.realised_benefit) and _ok(i.total_cost) else None
    amb = _div(i.realised_benefit, i.months)
    payback = _div(i.total_cost, amb) if _ok(amb) and amb > 0 else None
    run_rate = _div(amb * 12, i.planned_annual_benefit) if _ok(amb) else None

    notes = list(i.notes)
    if adoption is None:
        notes.append("adoption not available (missing licensed users or latest-week WAU)")
    if roi is None:
        notes.append("ROI not available (missing cost or benefit)")
    if payback is None and _ok(amb):
        notes.append("payback not reached: average monthly benefit is not positive")
    if run_rate is None:
        notes.append("run-rate versus plan not available: planned annual benefit not supplied")

    status, reason = status_of(adoption, roi)
    return dict(adoption=adoption, wau_change=wau_change, roi=roi, avg_monthly_benefit_usd=amb,
                payback_months=payback, run_rate_vs_plan=run_rate, status=status, status_reason=reason,
                data_notes="; ".join(notes))


def status_of(adoption, roi) -> tuple[str, str]:
    a, r = fmt_pct(adoption), fmt_pct(roi, signed=True)
    reds = []
    if _ok(adoption) and adoption < RED_ADOPTION:
        reds.append(f"adoption {a} is below 30%")
    if _ok(roi) and roi < 0:
        reds.append(f"ROI {r} is below 0")
    if reds:
        return "Red", "Red: " + " and ".join(reds)
    if _ok(adoption) and _ok(roi) and adoption >= GREEN_ADOPTION and roi > 0:
        return "Green", f"Green: adoption {a} is at least 60% and ROI {r} is above 0"
    why = []
    if not _ok(adoption) or not _ok(roi):
        why.append("adoption or ROI is not available")
    else:
        if adoption < GREEN_ADOPTION:
            why.append(f"adoption {a} is between 30% and 60%")
        if roi == 0:
            why.append(f"ROI {r} is exactly 0, not above 0")
    return "Amber", "Amber: " + " and ".join(why)


# ---------------------------------------------------------------- portfolio
def inputs_from_curated(register: pd.DataFrame, usage: pd.DataFrame, fin: pd.DataFrame,
                        as_of: dt.date) -> list[Inputs]:
    out = []
    for r in register.itertuples(index=False):
        u = usage[usage.initiative_id == r.initiative_id].dropna(subset=["weekly_active_users"]).sort_values("week_start")
        weeks = sorted(usage.week_start.dropna().unique())
        first = u[u.week_start == weeks[0]].weekly_active_users.sum() if weeks and (u.week_start == weeks[0]).any() else None
        latest = u[u.week_start == weeks[-1]].weekly_active_users.sum() if weeks and (u.week_start == weeks[-1]).any() else None
        f = fin[fin.initiative_id == r.initiative_id]
        cost = float(f.cost_usd.sum()) if len(f) and f.cost_usd.notna().any() else None
        ben = float(f.benefit_usd.sum()) if len(f) and f.benefit_usd.notna().any() else None
        months = months_live(r.go_live, as_of) if isinstance(r.go_live, dt.date) else None
        notes = []
        missing = [w for w in weeks if not (u.week_start == w).any()]
        if missing:
            notes.append("no usage reported for week(s) " + ", ".join(missing))
        out.append(Inputs(r.initiative_id, r.licensed_users if _ok(r.licensed_users) else None,
                          float(first) if first is not None else None, float(latest) if latest is not None else None,
                          weeks[0] if weeks else None, weeks[-1] if weeks else None, cost, ben, months,
                          r.planned_annual_benefit_usd if _ok(r.planned_annual_benefit_usd) else None, notes))
    return out


def compute(register: pd.DataFrame, usage: pd.DataFrame, fin: pd.DataFrame, as_of: dt.date) -> pd.DataFrame:
    rows = []
    reg = register.set_index("initiative_id")
    for i in inputs_from_curated(register, usage, fin, as_of):
        m = compute_one(i)
        meta = reg.loc[i.initiative_id]
        rows.append({
            "initiative_id": i.initiative_id, "name": meta["name"], "type": meta["type"],
            "business_unit": meta["business_unit"], "go_live": meta["go_live"].isoformat() if isinstance(meta["go_live"], dt.date) else None,
            "months_live": i.months, "licensed_users": i.licensed_users,
            "first_week": i.first_week, "wau_first": i.wau_first, "latest_week": i.latest_week, "wau_latest": i.wau_latest,
            "total_cost_usd": i.total_cost, "realised_benefit_usd": i.realised_benefit,
            "planned_annual_benefit_usd": i.planned_annual_benefit, **m,
            # display strings: the only form in which the assistant shows numbers
            "licensed_users_display": fmt_int(i.licensed_users), "wau_first_display": fmt_int(i.wau_first),
            "wau_latest_display": fmt_int(i.wau_latest), "months_live_display": fmt_int(i.months),
            "total_cost_display": fmt_usd(i.total_cost), "realised_benefit_display": fmt_usd(i.realised_benefit),
            "planned_annual_benefit_display": fmt_usd(i.planned_annual_benefit),
            "adoption_display": fmt_pct(m["adoption"]), "wau_change_display": fmt_pct(m["wau_change"], signed=True),
            "roi_display": fmt_pct(m["roi"], signed=True), "avg_monthly_benefit_display": fmt_usd(m["avg_monthly_benefit_usd"]),
            "payback_display": fmt_months(m["payback_months"]), "run_rate_vs_plan_display": fmt_pct(m["run_rate_vs_plan"]),
            "as_of": as_of.isoformat(), "calc_version": CALC_VERSION,
        })
    return pd.DataFrame(rows)


def provenance(sc: pd.DataFrame) -> pd.DataFrame:
    """One row per initiative x metric: formula, the input values used, and the result."""
    rows = []
    for r in sc.itertuples(index=False):
        spec = {
            "adoption": (f"WAU week {r.latest_week} = {r.wau_latest_display}; licensed users = {r.licensed_users_display}", r.adoption_display),
            "wau_change": (f"WAU week {r.first_week} = {r.wau_first_display}; WAU week {r.latest_week} = {r.wau_latest_display}", r.wau_change_display),
            "roi": (f"realised benefit to date = {r.realised_benefit_display}; total cost to date = {r.total_cost_display}", r.roi_display),
            "avg_monthly_benefit": (f"realised benefit to date = {r.realised_benefit_display}; months live = {r.months_live_display} (go-live {r.go_live}, as of {r.as_of})", r.avg_monthly_benefit_display),
            "payback_months": (f"total cost to date = {r.total_cost_display}; average monthly benefit = {r.avg_monthly_benefit_display}", r.payback_display),
            "run_rate_vs_plan": (f"average monthly benefit = {r.avg_monthly_benefit_display}; planned annual benefit = {r.planned_annual_benefit_display}", r.run_rate_vs_plan_display),
            "status": (f"adoption = {r.adoption_display}; ROI = {r.roi_display}", r.status),
        }
        for metric, (inputs, value) in spec.items():
            rows.append(dict(initiative_id=r.initiative_id, metric=metric, formula=FORMULAS[metric], inputs=inputs,
                             value_display=value if value is not None else "not available"))
    return pd.DataFrame(rows)


def portfolio_summary(sc: pd.DataFrame) -> pd.DataFrame:
    """Portfolio-level figures, computed here so the assistant never aggregates.
    portfolio_roi and portfolio_adoption are NOT brief-defined metrics; they are aggregate ratios (assumption A8)."""
    cost, ben = sc.total_cost_usd.sum(), sc.realised_benefit_usd.sum()
    lic = sc.licensed_users.sum()
    wau = sc.wau_latest.sum()
    counts = sc.status.value_counts()
    roi = (ben - cost) / cost if cost else None
    adoption = wau / lic if lic else None
    return pd.DataFrame([dict(
        as_of=sc.as_of.iloc[0], initiatives=len(sc), green=int(counts.get("Green", 0)), amber=int(counts.get("Amber", 0)),
        red=int(counts.get("Red", 0)),
        green_initiatives=", ".join(sc[sc.status == "Green"].initiative_id), amber_initiatives=", ".join(sc[sc.status == "Amber"].initiative_id),
        red_initiatives=", ".join(sc[sc.status == "Red"].initiative_id),
        total_cost_usd=cost, total_realised_benefit_usd=ben, portfolio_roi=roi, total_licensed_users=lic,
        total_wau_latest=wau, portfolio_adoption=adoption,
        total_cost_display=fmt_usd(cost), total_realised_benefit_display=fmt_usd(ben),
        portfolio_roi_display=fmt_pct(roi, signed=True), total_licensed_users_display=fmt_int(lic),
        total_wau_latest_display=fmt_int(wau), portfolio_adoption_display=fmt_pct(adoption),
        definition_note="portfolio_roi = (sum of realised benefit - sum of cost) / sum of cost; portfolio_adoption = "
                        "sum of latest-week WAU / sum of licensed users. Aggregates defined by the team (assumption A8), "
                        "not brief metrics.",
        calc_version=CALC_VERSION)])
