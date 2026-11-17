"""Write the curated tables, scorecard and change log to a SQLite file with governed views.

The SQL agent can only see the v_* views (enforced in backend/sqldb.py). Every number in a view was produced
by value.clean (cleaned source values) or value.scorecard (calculated metrics).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

VIEWS = {
    "v_scorecard": """SELECT initiative_id, name, type, business_unit, go_live, months_live, licensed_users,
        first_week, wau_first, latest_week, wau_latest, total_cost_usd, realised_benefit_usd, planned_annual_benefit_usd,
        adoption, wau_change, roi, avg_monthly_benefit_usd, payback_months, run_rate_vs_plan, status, status_reason,
        data_notes, licensed_users_display, wau_first_display, wau_latest_display, months_live_display,
        total_cost_display, realised_benefit_display, planned_annual_benefit_display, adoption_display,
        wau_change_display, roi_display, avg_monthly_benefit_display, payback_display, run_rate_vs_plan_display,
        as_of, calc_version FROM scorecard""",
    "v_portfolio_summary": "SELECT * FROM portfolio_summary",
    "v_metric_provenance": "SELECT initiative_id, metric, formula, inputs, value_display FROM scorecard_inputs",
    "v_initiatives": """SELECT initiative_id, name, type, business_unit, go_live, sponsor_name, sponsor_role,
        owner_name, owner_role FROM initiatives""",
    "v_weekly_usage": "SELECT initiative_id, week_start, weekly_active_users FROM weekly_usage",
    "v_monthly_financials": "SELECT initiative_id, month, cost_usd, benefit_usd FROM monthly_financials",
    "v_change_log": "SELECT id, rule, kind, file, initiative, field, before, after, rows, reason, effect FROM change_log",
}

NOTES = """Business notes for the scorecard views:
- Every metric is pre-computed by the governed scorecard code. Never calculate: no arithmetic, no SUM/AVG/COUNT.
- Show numbers using the *_display columns exactly as stored (e.g. roi_display '+20.0%').
- status is Green / Amber / Red; status_reason states which rule fired.
- Portfolio-level counts and totals are in v_portfolio_summary (portfolio_roi and portfolio_adoption are team-defined
  aggregates, see definition_note).
- How each value was calculated: v_metric_provenance (formula + inputs).
- Data corrections and their effects: v_change_log.
- Personal data available: sponsor/owner names and roles only (v_initiatives).
"""


def write(db_path: Path, curated: dict, scorecard: pd.DataFrame, provenance: pd.DataFrame,
          summary: pd.DataFrame, change_log: pd.DataFrame) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    reg = curated["register"].copy()
    reg["go_live"] = reg.go_live.map(lambda d: d.isoformat() if d else None)
    tables = {"initiatives": reg, "weekly_usage": curated["usage"], "monthly_financials": curated["financials"],
              "scorecard": scorecard, "scorecard_inputs": provenance, "portfolio_summary": summary,
              "change_log": change_log}
    for name, df in tables.items():
        df.to_sql(name, con, index=False)
    for name, sql in VIEWS.items():
        con.execute(f"CREATE VIEW {name} AS {sql}")
    con.commit()
    con.close()
    tmp.replace(db_path)
    return db_path
