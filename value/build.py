"""Build everything the assistant relies on, reproducibly:  python -m value.build

  data/curated/*.csv            cleaned, standardised tables
  outputs/change_log.{csv,md}   every correction, why, and its effect on the scorecard
  outputs/scorecard.{csv,md}    the governed value scorecard (brief section 4)
  outputs/value.sqlite          read-only store with the governed v_* views for the SQL agent
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pandas as pd

from . import clean, scorecard, store

ROOT = Path(__file__).resolve().parent.parent
CURATED = ROOT / "data" / "curated"
OUT = ROOT / "outputs"
DB_PATH = OUT / "value.sqlite"
DOCS = ROOT / "docs"

EFFECT_FIELDS = [("adoption_display", "adoption"), ("wau_change_display", "WAU change"), ("roi_display", "ROI"),
                 ("total_cost_display", "total cost"), ("realised_benefit_display", "realised benefit"),
                 ("avg_monthly_benefit_display", "avg monthly benefit"), ("payback_display", "payback"),
                 ("run_rate_vs_plan_display", "run-rate vs plan"), ("status", "status")]


def _score(curated, as_of):
    return scorecard.compute(curated["register"], curated["usage"], curated["financials"], as_of).set_index("initiative_id")


def effects(raw, final_sc: pd.DataFrame, as_of) -> dict[str, dict[str, str]]:
    """For each rule: {initiative: 'ROI +149,900.0% -> +50.0%; ...'} comparing the scorecard WITHOUT that rule
    (before) to the final scorecard (after)."""
    out = {}
    for name in clean.RULE_NAMES:
        try:
            cur, _ = clean.run(skip={name}, raw=raw)
            alt = _score(cur, as_of)
        except Exception as e:  # noqa: BLE001
            out[name] = {"*": f"the pipeline cannot run without this correction ({type(e).__name__})"}
            continue
        per = {}
        for iid in final_sc.index:
            diffs = []
            for col, label in EFFECT_FIELDS:
                a = alt.loc[iid, col] if iid in alt.index else None
                b = final_sc.loc[iid, col]
                a, b = ("n/a" if pd.isna(x) else x for x in (a, b))
                if a != b:
                    diffs.append(f"{label} {a} -> {b}")
            if diffs:
                per[iid] = "; ".join(diffs)
        out[name] = per
    return out


def effect_text(entry, eff: dict[str, str]) -> str:
    if "*" in eff:
        return eff["*"]
    ids = [i.strip() for i in entry.initiative.split(",")]
    parts = [f"{i}: {eff[i]}" for i in ids if i in eff]
    return " | ".join(parts) if parts else "No scorecard value changed."


def md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in df.itertuples(index=False):
        lines.append("| " + " | ".join("" if pd.isna(v) else str(v).replace("|", "/") for v in r) + " |")
    return "\n".join(lines)


def build(verbose: bool = True, db_path: Path = DB_PATH) -> dict:
    raw = clean.load_raw()
    curated, log = clean.run(raw=raw)
    as_of = clean.as_of_date(curated["usage"])
    sc = scorecard.compute(curated["register"], curated["usage"], curated["financials"], as_of)
    eff = effects(raw, sc.set_index("initiative_id"), as_of)
    for e in log:
        e.effect = effect_text(e, eff.get(e.rule, {}))
    cl = pd.DataFrame(clean.change_log_dict(log))
    cl.insert(0, "id", [f"CL-{i:02d}" for i in range(1, len(cl) + 1)])
    prov = scorecard.provenance(sc)
    summary = scorecard.portfolio_summary(sc)

    CURATED.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    reg = curated["register"].assign(go_live=lambda d: d.go_live.map(lambda x: x.isoformat() if x else None))
    reg.to_csv(CURATED / "initiatives.csv", index=False)
    curated["usage"].to_csv(CURATED / "weekly_usage.csv", index=False)
    curated["financials"].to_csv(CURATED / "monthly_financials.csv", index=False)
    sc.to_csv(OUT / "scorecard.csv", index=False)
    cl.to_csv(OUT / "change_log.csv", index=False)
    store.write(db_path, curated, sc, prov, summary, cl)

    view = sc[["initiative_id", "name", "adoption_display", "wau_change_display", "roi_display",
               "avg_monthly_benefit_display", "payback_display", "run_rate_vs_plan_display", "status"]]
    view.columns = ["Initiative", "Name", "Adoption", "WAU change", "ROI", "Avg monthly benefit", "Payback",
                    "Run-rate vs plan", "Status"]
    s = summary.iloc[0]
    (OUT / "scorecard.md").write_text(
        f"# Nimbus Group AI value scorecard (as of {as_of})\n\n"
        f"Calculated by `value/scorecard.py` ({scorecard.CALC_VERSION}) from the cleaned data. "
        f"Green {s.green} · Amber {s.amber} · Red {s.red}.\n\n" + md_table(view.fillna("not available")) + "\n\n"
        "Status reasons:\n\n" + "\n".join(f"- **{r.initiative_id}** {r.status_reason}" for r in sc.itertuples()) + "\n",
        encoding="utf-8")
    (OUT / "change_log.md").write_text(
        "# Data-quality change log\n\nGenerated by `python -m value.build`. *Effect* compares the scorecard computed "
        "without the correction (before) with the final scorecard (after).\n\n"
        + md_table(cl[["id", "file", "initiative", "kind", "field", "before", "after", "rows", "reason", "effect"]]) + "\n",
        encoding="utf-8")

    corr = cl[["id", "file", "initiative", "kind", "before", "after", "reason", "effect"]].copy()
    corr["kind"] = corr.kind.str.replace("_", " ")
    tmpl = (Path(__file__).with_name("assumptions_template.md")).read_text("utf-8")
    DOCS.mkdir(parents=True, exist_ok=True)
    doc = tmpl.format(as_of=as_of, calc_version=scorecard.CALC_VERSION, corrections=md_table(corr))
    for target in (DOCS / "data_quality_and_assumptions.md", OUT / "data_quality_and_assumptions.md"):
        target.write_text(doc, encoding="utf-8")         # docs/ for the repo, outputs/ for the UI

    digest = hashlib.sha256(sc.to_csv(index=False).encode() + cl.to_csv(index=False).encode()).hexdigest()[:16]
    if verbose:
        print(f"as of {as_of} | {len(cl)} change-log entries | scorecard hash {digest}")
        print(view.to_string(index=False))
    return {"as_of": as_of, "scorecard": sc, "change_log": cl, "hash": digest}


if __name__ == "__main__":
    build(verbose="-q" not in sys.argv)
