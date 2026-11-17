"""Load the raw portfolio files, correct data-quality issues and standardise metric conventions (FR-02).

Every rule is a small function that fixes one kind of problem and records ChangeLogEntry rows.
After all rules run, `finalize` applies STRICT typing: values no rule fixed are dropped (ISO dates and plain
numbers only, one canonical metric, USD amounts assumed). That makes the effect of each rule measurable:
`run(skip={rule})` recomputes the scorecard as if that correction had not been made.
"""
from __future__ import annotations

import copy
import datetime as dt
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
CONFIG = yaml.safe_load((Path(__file__).with_name("config.yaml")).read_text("utf-8"))


@dataclass
class ChangeLogEntry:
    rule: str
    kind: str
    file: str
    initiative: str
    field: str
    before: str
    after: str
    rows: int
    reason: str
    effect: str = ""


@dataclass
class Ctx:
    log: list[ChangeLogEntry] = field(default_factory=list)

    def add(self, rule, kind, file, initiative, fld, before, after, rows, reason):
        self.log.append(ChangeLogEntry(rule, kind, file, initiative, fld, str(before), str(after), int(rows), reason))


# ---------------------------------------------------------------- load
def load_raw(raw_dir: Path = RAW_DIR) -> dict[str, pd.DataFrame]:
    from openpyxl import load_workbook

    f = CONFIG["raw_files"]
    ws = load_workbook(raw_dir / f["register"], data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    reg = pd.DataFrame(rows[1:], columns=["initiative_id", "name", "type", "business_unit", "sponsor", "owner",
                                          "go_live", "licensed_users", "planned_annual_benefit_usd"], dtype=object)
    read = lambda name: pd.read_csv(raw_dir / f[name], dtype=str, keep_default_na=False)
    return {"register": reg, "usage": read("usage"), "costs": read("costs"), "benefits": read("benefits"),
            "fx": read("fx")}


FILES = {"register": "initiatives.xlsx", "usage": "usage_weekly.csv", "costs": "costs.csv", "benefits": "benefits.csv"}


def _iso(s: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s).strip())
    except ValueError:
        return None


def _num(s) -> float | None:
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return None if pd.isna(s) else float(s)
    s = str(s).strip()
    return float(s) if re.fullmatch(r"-?\d+(\.\d+)?", s) else None


def _supplied_weeks(T) -> list[dt.date]:
    """Reporting weeks = the ISO-formatted week_start values in the usage feed (the majority format)."""
    return sorted({d for d in (_iso(v) for v in T["usage"].week_start) if d})


# ---------------------------------------------------------------- rules
def r_normalise_ids(T, ctx: Ctx):
    pat, fmt = re.compile(CONFIG["initiative_id"]["pattern"], re.I), CONFIG["initiative_id"]["format"]
    for name, df in T.items():
        if name == "fx":
            continue
        new = df.initiative_id.map(lambda v: fmt.format(int(m.group(1))) if (m := pat.match(str(v))) else v)
        for raw, norm in sorted({(a, b) for a, b in zip(df.initiative_id, new) if a != b}):
            ctx.add("normalise_ids", "id_format", FILES[name], norm, "initiative_id", repr(raw), norm,
                    (df.initiative_id == raw).sum(), "Initiative ids normalised to the INIT-NN register format.")
        df["initiative_id"] = new


def _parse_day_first(s: str) -> dt.date | None:
    for f in CONFIG["dates"]["formats"]:
        try:
            return dt.datetime.strptime(s.strip(), f).date()
        except ValueError:
            pass
    return None


def r_parse_dates(T, ctx: Ctx):
    weeks = set(_supplied_weeks(T))
    u = T["usage"]
    groups = {}
    for idx, v in u.week_start.items():
        if _iso(v):
            continue
        d = _parse_day_first(v)
        if d and d not in weeks:          # day-first does not land on a reporting week -> try month-first
            try:
                alt = dt.datetime.strptime(v.strip(), "%m/%d/%Y").date()
                d = alt if alt in weeks else None
            except ValueError:
                d = None
        if d:
            u.at[idx, "week_start"] = d.isoformat()
            groups.setdefault(u.at[idx, "initiative_id"], []).append((v, d.isoformat()))
    for iid, pairs in sorted(groups.items()):
        ctx.add("parse_dates", "date_format", FILES["usage"], iid, "week_start", pairs[0][0], pairs[0][1], len(pairs),
                "Non-ISO week dates parsed day-first and checked against the supplied reporting weeks.")
    reg = T["register"]
    for idx, v in reg.go_live.items():
        if isinstance(v, (dt.date, dt.datetime)) or _iso(v):
            continue
        d = _parse_day_first(str(v))
        if d:
            reg.at[idx, "go_live"] = d
            ctx.add("parse_dates", "date_format", FILES["register"], reg.at[idx, "initiative_id"], "go_live", v,
                    d.isoformat(), 1, "Text go-live date parsed (day-first, assumption A3).")


def r_numbers_as_text(T, ctx: Ctx):
    reg = T["register"]
    for col in ("licensed_users", "planned_annual_benefit_usd"):
        for idx, v in reg[col].items():
            if isinstance(v, str) and (m := re.fullmatch(r"\s*([\d,]+(?:\.\d+)?)\s*[A-Za-z ]*", v)):
                val = float(m.group(1).replace(",", ""))
                reg.at[idx, col] = val
                ctx.add("numbers_as_text", "number_as_text", FILES["register"], reg.at[idx, "initiative_id"], col,
                        repr(v), f"{val:g}", 1, "Number stored as text with separators/units; numeric value extracted.")


def r_standardise_metrics(T, ctx: Ctx):
    u = T["usage"]
    canon = {s.lower().strip(): c for c, syns in CONFIG["metric_synonyms"].items() for s in syns}
    excluded = {k.lower(): v for k, v in CONFIG["excluded_metrics"].items()}
    drop = []
    for raw in sorted(u.metric.unique()):
        key = raw.lower().strip()
        mask = u.metric == raw
        ids = ", ".join(sorted(u[mask].initiative_id.unique()))
        if key in excluded:
            drop.extend(u[mask].index)
            ctx.add("standardise_metrics", "wrong_metric", FILES["usage"], ids, "metric", raw, "(row excluded)",
                    mask.sum(), f"'{raw}' is {excluded[key]}.")
        elif key in canon and raw != canon[key]:
            u.loc[mask, "metric"] = canon[key]
            ctx.add("standardise_metrics", "metric_names", FILES["usage"], ids, "metric", raw, canon[key], mask.sum(),
                    "Synonym mapped to the canonical metric name (config.yaml metric_synonyms).")
    T["usage"] = u.drop(index=drop)


def r_drop_duplicates(T, ctx: Ctx):
    for name in ("usage", "costs", "benefits"):
        df = T[name]
        dup = df.duplicated(keep="first")
        for iid, n in df[dup].initiative_id.value_counts().sort_index().items():
            ctx.add("drop_duplicates", "duplicate_row", FILES[name], iid, "(whole row)",
                    " | ".join(df[dup & (df.initiative_id == iid)].iloc[0].astype(str)), "(duplicate removed)", n,
                    "Exact duplicate row; keeping it would double-count the value.")
        T[name] = df[~dup]


def r_fix_wau_outliers(T, ctx: Ctx):
    u, reg = T["usage"], T["register"]
    lic = {r.initiative_id: _num(r.licensed_users) for r in reg.itertuples()}
    div, tol = CONFIG["outliers"]["wau_typo_divisor"], CONFIG["outliers"]["max_neighbour_change"]
    for iid, g in u[u.metric == "weekly_active_users"].groupby("initiative_id"):
        g = g.assign(v=g.value.map(_num)).sort_values("week_start")
        cap = lic.get(iid)
        if not cap:
            continue
        for pos, (idx, row) in enumerate(g.iterrows()):
            if row.v is None or row.v <= cap:
                continue
            neighbours = [x for x in (g.v.iloc[pos - 1] if pos else None,
                                      g.v.iloc[pos + 1] if pos + 1 < len(g) else None) if x]
            cand = row.v / div
            ok = cand <= cap and neighbours and min(abs(cand - n) / n for n in neighbours) <= tol
            u.at[idx, "value"] = f"{cand:g}" if ok else ""
            ctx.add("fix_wau_outliers", "outlier_typo", FILES["usage"], iid, f"WAU {row.week_start}", f"{row.v:g}",
                    f"{cand:g}" if ok else "(set missing)", 1,
                    f"WAU exceeds licensed users ({cap:g}); " + (f"dividing by {div} restores a value consistent with "
                                                                 f"the neighbouring week." if ok else "no safe correction."))


def r_report_missing(T, ctx: Ctx):
    weeks = [w.isoformat() for w in _supplied_weeks(T)]
    u = T["usage"]
    for iid in sorted(T["register"].initiative_id):
        have = set(u[(u.initiative_id == iid) & (u.metric == "weekly_active_users")].week_start)
        for w in weeks:
            if w not in have:
                ctx.add("report_missing", "missing_value", FILES["usage"], iid, f"WAU {w}", "(no row)",
                        "(left missing)", 0, "Not imputed: adoption and WAU change use only the first and latest week.")
    for r in T["register"].itertuples():
        if r.planned_annual_benefit_usd is None or (isinstance(r.planned_annual_benefit_usd, float)
                                                    and pd.isna(r.planned_annual_benefit_usd)):
            ctx.add("report_missing", "missing_value", FILES["register"], r.initiative_id, "planned_annual_benefit_usd",
                    "(blank)", "(left missing)", 0,
                    "Not imputed: run-rate versus plan is reported as not available; status does not depend on it.")


def r_cost_units(T, ctx: Ctx):
    c = T["costs"]
    mult = CONFIG["currency"]["unit_multipliers"]
    for unit, k in mult.items():
        if k == 1:
            continue
        mask = c.unit == unit
        for iid in sorted(c[mask].initiative_id.unique()):
            m = mask & (c.initiative_id == iid)
            ex = c[m].iloc[0].amount
            c.loc[m, "amount"] = c[m].amount.map(lambda v: f"{float(v) * k:g}")
            c.loc[m, "unit"] = "USD"
            ctx.add("cost_units", "unit", FILES["costs"], iid, "amount", f"{ex} {unit}", f"{float(ex) * k:g} USD", m.sum(),
                    f"Amounts in {unit} converted to USD (x{k:,}).")


def r_drop_subtotals(T, ctx: Ctx):
    c = T["costs"]
    sub = (c.period_type != "month") | (c.category.str.lower() == "subtotal")
    for iid in sorted(c[sub].initiative_id.unique()):
        rows = c[sub & (c.initiative_id == iid)]
        ctx.add("drop_subtotals", "subtotal_rows", FILES["costs"], iid, "period",
                ", ".join(f"{p}={a}" for p, a in zip(rows.period, rows.amount)), "(rows removed)", len(rows),
                "Quarterly subtotal rows repeat the monthly rows they summarise; keeping them double-counts cost.")
    T["costs"] = c[~sub]


def r_negative_costs(T, ctx: Ctx):
    c = T["costs"]
    neg = c.amount.map(lambda v: (_num(v) or 0) < 0)
    for idx in c[neg].index:
        v = c.at[idx, "amount"]
        c.at[idx, "amount"] = v.lstrip("-")
        ctx.add("negative_costs", "sign_error", FILES["costs"], c.at[idx, "initiative_id"], f"amount {c.at[idx, 'period']}",
                v, v.lstrip("-"), 1, "Negative cost with no credit-note category: treated as a sign error (assumption A6).")


def r_cumulative_benefit(T, ctx: Ctx):
    b = T["benefits"]
    for iid in sorted(b[b.reporting_basis == "cumulative"].initiative_id.unique()):
        m = (b.initiative_id == iid) & (b.reporting_basis == "cumulative")
        g = b[m].sort_values("month")
        vals = g.realised_benefit.map(float)
        if not vals.is_monotonic_increasing:
            continue
        inc = vals.diff().fillna(vals)
        b.loc[g.index, "realised_benefit"] = inc.map(lambda v: f"{v:g}").values
        b.loc[g.index, "reporting_basis"] = "monthly"
        ctx.add("cumulative_benefit", "cumulative_basis", FILES["benefits"], iid, "realised_benefit",
                f"{g.month.iloc[-1]}: {vals.iloc[-1]:g} (cumulative)", f"{g.month.iloc[-1]}: {inc.iloc[-1]:g} (monthly)",
                len(g), "Cumulative-to-date values converted to monthly increments; summing them would count earlier "
                        "months many times.")


def r_currency(T, ctx: Ctx):
    b = T["benefits"]
    rates = {r.currency: float(r.rate_to_usd) for r in T["fx"].itertuples()}
    for cur in sorted(set(b.currency) - {CONFIG["currency"]["base"]}):
        m = b.currency == cur
        for iid in sorted(b[m].initiative_id.unique()):
            mm = m & (b.initiative_id == iid)
            ex = b[mm].iloc[0].realised_benefit
            b.loc[mm, "realised_benefit"] = b[mm].realised_benefit.map(lambda v: f"{round(float(v) * rates[cur], 2):g}")
            b.loc[mm, "currency"] = "USD"
            ctx.add("currency", "currency", FILES["benefits"], iid, "realised_benefit", f"{ex} {cur}",
                    f"{round(float(ex) * rates[cur], 2):g} USD", mm.sum(),
                    f"Converted with the Group Finance rate {cur} 1 = USD {rates[cur]} (fx_rates.csv).")


RULES = [r_normalise_ids, r_parse_dates, r_numbers_as_text, r_standardise_metrics, r_drop_duplicates,
         r_fix_wau_outliers, r_report_missing, r_cost_units, r_drop_subtotals, r_negative_costs,
         r_cumulative_benefit, r_currency]
RULE_NAMES = [r.__name__[2:] for r in RULES]


# ---------------------------------------------------------------- strict typing
_PARTY = re.compile(r"^\s*(.*?)\s*\((.*)\)\s*$")


def finalize(T) -> dict[str, pd.DataFrame]:
    reg = T["register"].copy()

    def go(v):
        if isinstance(v, dt.datetime):
            return v.date()
        if isinstance(v, dt.date):
            return v
        return _iso(v) if isinstance(v, str) else None

    reg["go_live"] = reg.go_live.map(go)
    for col in ("licensed_users", "planned_annual_benefit_usd"):
        reg[col] = reg[col].map(_num)
    for who in ("sponsor", "owner"):
        parts = reg[who].map(lambda s: _PARTY.match(str(s)).groups() if _PARTY.match(str(s)) else (str(s), ""))
        reg[f"{who}_name"], reg[f"{who}_role"] = parts.map(lambda p: p[0]), parts.map(lambda p: p[1])
    reg = reg.drop(columns=["sponsor", "owner"])

    u = T["usage"]
    u = u[u.metric == "weekly_active_users"].assign(week_start=u.week_start.map(lambda v: _iso(v)),
                                                    weekly_active_users=u.value.map(_num))
    u = u.dropna(subset=["week_start"])
    u = (u.groupby(["initiative_id", "week_start"], as_index=False).weekly_active_users.sum(min_count=1))
    u["week_start"] = u.week_start.map(lambda d: d.isoformat())

    c = T["costs"].assign(amount=lambda d: d.amount.map(_num))
    b = T["benefits"].assign(realised_benefit=lambda d: d.realised_benefit.map(_num))
    cm = c.groupby(["initiative_id", "period"], as_index=False).amount.sum().rename(columns={"period": "month", "amount": "cost_usd"})
    bm = b.groupby(["initiative_id", "month"], as_index=False).realised_benefit.sum().rename(columns={"realised_benefit": "benefit_usd"})
    fin = cm.merge(bm, on=["initiative_id", "month"], how="outer").sort_values(["initiative_id", "month"])
    known = set(reg.initiative_id)
    return {"register": reg, "usage": u[u.initiative_id.isin(known)].reset_index(drop=True),
            "financials": fin[fin.initiative_id.isin(known)].reset_index(drop=True)}


def as_of_date(usage: pd.DataFrame) -> dt.date:
    """Reporting date = end (Sunday) of the latest supplied week (assumption A2)."""
    return dt.date.fromisoformat(max(usage.week_start)) + dt.timedelta(days=6)


def run(skip: set[str] = frozenset(), raw: dict | None = None):
    T = copy.deepcopy(raw if raw is not None else load_raw())
    ctx = Ctx()
    for rule, name in zip(RULES, RULE_NAMES):
        if name not in skip:
            rule(T, ctx)
    return finalize(T), ctx.log


def change_log_dict(log: list[ChangeLogEntry]) -> list[dict]:
    return [asdict(e) for e in log]
