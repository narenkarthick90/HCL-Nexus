"""The governed, computation-free SQL guard (FR-03, FR-08)."""
from __future__ import annotations

import pytest

from backend import sqldb
from backend.sqldb import UnsafeSQLError, run_select, validate_select

ALLOWED = [
    "SELECT initiative_id, roi_display, status FROM v_scorecard WHERE status = 'Red'",
    "SELECT initiative_id, name, roi_display FROM v_scorecard ORDER BY roi DESC LIMIT 3",
    "SELECT * FROM v_portfolio_summary;",
    "SELECT initiative_id FROM v_scorecard WHERE roi < -0.1",
    "SELECT name FROM v_scorecard WHERE LOWER(name) LIKE '%claims%'",
    "WITH r AS (SELECT * FROM v_scorecard WHERE status = 'Amber') SELECT initiative_id, status_reason FROM r",
    "SELECT s.initiative_id, i.sponsor_name FROM v_scorecard s JOIN v_initiatives i ON i.initiative_id = s.initiative_id",
]
BLOCKED = [
    "SELECT AVG(roi) FROM v_scorecard", "SELECT SUM(total_cost_usd) FROM v_scorecard", "SELECT COUNT(*) FROM v_scorecard",
    "SELECT roi * 100 FROM v_scorecard", "SELECT realised_benefit_usd - total_cost_usd FROM v_scorecard",
    "SELECT ROUND(roi, 2) FROM v_scorecard", "SELECT CAST(roi AS INTEGER) FROM v_scorecard",
    "SELECT CASE WHEN roi > 0 THEN 1 END FROM v_scorecard", "SELECT roi, RANK() OVER (ORDER BY roi) FROM v_scorecard",
    "SELECT 45 AS roi", "SELECT * FROM scorecard", "SELECT * FROM sqlite_master", "DELETE FROM v_scorecard",
    "SELECT * FROM v_scorecard; DROP TABLE x", "ATTACH DATABASE 'x' AS y", "PRAGMA table_info(v_scorecard)",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_reading_filtering_sorting_is_allowed(sql):
    cols, rows, _ = run_select(sql)
    assert cols and rows


@pytest.mark.parametrize("sql", BLOCKED)
def test_calculations_raw_tables_and_writes_are_blocked(sql):
    with pytest.raises(UnsafeSQLError):
        validate_select(sql)


def test_connection_is_read_only():
    with pytest.raises(Exception):
        sqldb.connection().execute("CREATE TABLE t (x INT)")


def test_row_cap():
    _, rows, truncated = run_select("SELECT * FROM v_monthly_financials")
    assert len(rows) == sqldb.MAX_ROWS and truncated


def test_views_expose_display_strings_and_status_reason():
    rows = sqldb.query_rows("SELECT initiative_id, roi_display, status, status_reason FROM v_scorecard "
                            "WHERE initiative_id = 'INIT-07'")
    assert rows == [{"initiative_id": "INIT-07", "roi_display": "-60.0%", "status": "Red",
                     "status_reason": "Red: ROI -60.0% is below 0"}]


def test_schema_description_lists_only_governed_views():
    d = sqldb.schema_description()
    assert all(f"VIEW {v}" in d for v in sqldb.ALLOWED_VIEWS) and "Never calculate" in d
