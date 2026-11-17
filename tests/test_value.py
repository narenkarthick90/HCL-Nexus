"""Scorecard correctness and data-quality handling (FR-02, FR-03)."""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd
import pytest
import yaml

from value import build, clean, scorecard
from value.scorecard import Inputs, compute_one, months_live, status_of

ROOT = build.ROOT


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return build.build(verbose=False, db_path=tmp_path_factory.mktemp("db") / "value.sqlite")


def test_scorecard_matches_independent_gold(built):
    sc = built["scorecard"].set_index("initiative_id")
    gold = pd.read_csv(ROOT / "data" / "gold" / "scorecard_gold.csv").set_index("initiative_id")
    cols = {"adoption": "adoption", "wau_change": "wau_change", "roi": "roi", "payback_months": "payback_months",
            "avg_monthly_benefit": "avg_monthly_benefit_usd", "run_rate_vs_plan": "run_rate_vs_plan",
            "total_cost_usd": "total_cost_usd", "realised_benefit_usd": "realised_benefit_usd", "status": "status"}
    assert set(gold.index) == set(sc.index)
    for g, s in cols.items():
        for iid in gold.index:
            a, b = gold.loc[iid, g], sc.loc[iid, s]
            if isinstance(a, str):
                assert a == b, (iid, g)
            elif pd.isna(a):
                assert pd.isna(b), (iid, g)
            else:
                assert math.isclose(a, b, rel_tol=1e-9), (iid, g, a, b)


def test_every_planted_issue_is_logged(built):
    planted = yaml.safe_load((ROOT / "data" / "planted_issues.yaml").read_text("utf-8"))
    cl = built["change_log"]
    for p in planted:
        ids = {i.strip() for i in p["initiative"].split(",")}
        hit = cl[(cl.file == p["file"]) & (cl.kind == p["kind"])]
        if p["initiative"] != "all":
            hit = hit[hit.initiative.map(lambda s: bool(ids & {x.strip() for x in s.split(",")}))]
        assert len(hit), f"{p['id']} ({p['kind']}) not detected"


def test_change_log_records_effects(built):
    cl = built["change_log"].set_index("kind")
    assert "+149,900.0% -> +50.0%" in cl.loc["unit", "effect"]          # kUSD costs
    assert "adoption 725.0% -> 72.5%" in cl.loc["outlier_typo", "effect"]
    assert cl.effect.map(bool).all()


def test_build_is_reproducible(built, tmp_path):
    assert build.build(verbose=False, db_path=tmp_path / "value.sqlite")["hash"] == built["hash"]


def test_status_rule_boundaries():
    assert status_of(0.60, 0.01)[0] == "Green"
    assert status_of(0.80, 0.0)[0] == "Amber"          # ROI exactly 0
    assert status_of(0.30, 0.5)[0] == "Amber"          # adoption exactly 30% is not Red
    assert status_of(0.29, 0.5)[0] == "Red"
    assert status_of(0.90, -0.01)[0] == "Red"
    assert status_of(None, 0.5)[0] == "Amber"


def test_formula_edge_cases():
    base = dict(initiative_id="X", first_week="w1", latest_week="w3", notes=[])
    r = compute_one(Inputs(licensed_users=0, wau_first=10, wau_latest=12, total_cost=100.0, realised_benefit=0.0,
                           months=4, planned_annual_benefit=None, **base))
    assert r["adoption"] is None and r["payback_months"] is None and r["run_rate_vs_plan"] is None
    assert r["roi"] == -1.0 and r["status"] == "Red"
    assert "payback not reached" in r["data_notes"]


def test_months_live():
    assert months_live(dt.date(2026, 1, 15), dt.date(2026, 11, 15)) == 10
    assert months_live(dt.date(2026, 1, 16), dt.date(2026, 11, 15)) == 9
    assert months_live(dt.date(2025, 9, 1), dt.date(2026, 11, 15)) == 14


def test_display_strings():
    assert scorecard.fmt_pct(0.7) == "70.0%" and scorecard.fmt_pct(0.0711, signed=True) == "+7.1%"
    assert scorecard.fmt_pct(0.0, signed=True) == "0.0%"
    assert scorecard.fmt_usd(102000) == "USD 102,000"


def test_skipping_a_rule_changes_the_scorecard():
    curated, _ = clean.run(skip={"cost_units"})
    sc = scorecard.compute(curated["register"], curated["usage"], curated["financials"], dt.date(2026, 11, 15))
    assert sc.set_index("initiative_id").loc["INIT-04", "total_cost_usd"] == 102
