"""Read-only access to the governed scorecard store (outputs/value.sqlite) and the SQL guard (FR-03, FR-08).

The SQL agent writes SQL, but it may only READ numbers the scorecard code produced:
  1. sqlglot parse: exactly one SELECT (CTEs and UNION allowed); no DML/DDL/PRAGMA/ATTACH
  2. only the governed v_* views may be referenced
  3. NO computation: arithmetic operators, aggregate and window functions, CASE, CAST and any other function
     (except LOWER/UPPER/TRIM/COALESCE for matching) are rejected, and so are numeric literals in the SELECT list.
     Filtering (WHERE), sorting (ORDER BY) and LIMIT are allowed: they choose rows, they do not create numbers.
  4. connection opened with mode=ro + PRAGMA query_only, an authorizer that only allows reads,
     a row cap and a time limit.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import sqlglot
from sqlglot import exp

from .settings import ROOT

DB_PATH = ROOT / "outputs" / "value.sqlite"
MAX_ROWS = 50
TIMEOUT_S = 3.0
ALLOWED_VIEWS = {"v_scorecard", "v_portfolio_summary", "v_metric_provenance", "v_initiatives", "v_weekly_usage",
                 "v_monthly_financials", "v_change_log"}

_FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Pragma,
              exp.Attach, exp.Detach, exp.Merge, exp.TruncateTable, exp.Transaction, exp.Commit, exp.Rollback)
_COMPUTE = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv, exp.Pow, exp.AggFunc, exp.Window, exp.Case,
            exp.If, exp.Cast, exp.TryCast, exp.BitwiseAnd, exp.BitwiseOr, exp.BitwiseXor, exp.DPipe)
_ALLOWED_FUNCS = (exp.Lower, exp.Upper, exp.Trim, exp.Coalesce)
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


class UnsafeSQLError(ValueError):
    pass


def validate_select(sql: str) -> str:
    """Return the single, computation-free SELECT or raise UnsafeSQLError with a reason the agent can act on."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise UnsafeSQLError("empty query")
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise UnsafeSQLError(f"could not parse SQL: {str(e).splitlines()[0]}") from None
    if len(statements) != 1:
        raise UnsafeSQLError("exactly one statement is allowed")
    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise UnsafeSQLError(f"only SELECT queries are allowed (got {type(stmt).__name__})")
    ctes = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
    for node in stmt.walk():
        if isinstance(node, _FORBIDDEN):
            raise UnsafeSQLError(f"forbidden operation: {type(node).__name__}")
        if isinstance(node, _COMPUTE):
            raise UnsafeSQLError(f"calculations are not allowed ({type(node).__name__}); every number must come "
                                 "pre-computed from the scorecard views (use the *_display columns, v_portfolio_summary "
                                 "for totals and counts, ORDER BY + LIMIT for rankings)")
        if isinstance(node, exp.Func) and not isinstance(node, _ALLOWED_FUNCS):
            raise UnsafeSQLError(f"function {node.sql()[:40]} is not allowed; only LOWER, UPPER, TRIM and COALESCE "
                                 "may be used for matching")
        if isinstance(node, exp.Table):
            name = node.name.lower()
            if name not in ALLOWED_VIEWS and name not in ctes:
                raise UnsafeSQLError(f"{node.name} is not available; query only these views: "
                                     f"{', '.join(sorted(ALLOWED_VIEWS))}")
    for select in stmt.find_all(exp.Select):
        for proj in select.expressions:
            if any(lit.is_number for lit in proj.find_all(exp.Literal)):
                raise UnsafeSQLError("numeric literals cannot be selected as output values")
    return sql


# ---------------------------------------------------------------- execution
_local = threading.local()


def _authorizer(action, *_):
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def ensure_db() -> None:
    if not DB_PATH.exists():
        from value.build import build

        build(verbose=False)


def connection() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    if con is None:
        ensure_db()
        con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        con.execute("PRAGMA query_only = ON")
        con.set_authorizer(_authorizer)
        _local.con = con
    return con


def run_select(sql: str) -> tuple[list[str], list[tuple], bool]:
    """Execute a validated SELECT. Returns (columns, rows, truncated)."""
    sql = validate_select(sql)
    con = connection()
    deadline = time.monotonic() + TIMEOUT_S
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cur = con.execute(f"SELECT * FROM ({sql}) LIMIT {MAX_ROWS + 1}")
        rows = cur.fetchall()
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise UnsafeSQLError(f"query exceeded {TIMEOUT_S}s") from None
        raise
    finally:
        con.set_progress_handler(None, 0)
    cols = [d[0] for d in cur.description]
    return cols, rows[:MAX_ROWS], len(rows) > MAX_ROWS


def query_rows(sql: str) -> list[dict]:
    """Trusted internal read (UI, weekly report): same read-only connection, no LLM involved."""
    cols, rows, _ = run_select(sql)
    return [dict(zip(cols, r)) for r in rows]


def fmt_cell(v) -> str:
    if v is None:
        return "not available"
    if isinstance(v, float):
        return repr(v)                # full precision; the agent is told to show *_display columns
    return str(v)


def rows_to_markdown(cols: list[str], rows: list[tuple], max_rows: int = 50) -> str:
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(fmt_cell(v).replace("|", "/") for v in r) + " |" for r in rows[:max_rows]]
    if len(rows) > max_rows:
        out.append(f"... {len(rows) - max_rows} more rows")
    return "\n".join(out)


def schema_description(sample_rows: int = 2) -> str:
    from value.store import NOTES

    con = connection()
    parts = []
    for name in sorted(ALLOWED_VIEWS):
        cur = con.execute(f'SELECT * FROM "{name}" LIMIT {sample_rows}')
        cols = [d[0] for d in cur.description]
        parts.append(f"VIEW {name} ({', '.join(cols)})\nSample rows:\n{rows_to_markdown(cols, cur.fetchall())}")
    return "\n\n".join(parts) + "\n\n" + NOTES
