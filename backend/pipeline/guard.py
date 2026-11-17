"""Deterministic answer guard (FR-08): no number, status or citation reaches the user unverified.

Checks, all in code:
  unverified_number  every numeric token must equal a value in a cited scorecard result (S label), a number in the
                     user's question, or a status-rule threshold (60%, 30%, 0). A figure that only a charter states is
                     allowed when the same sentence cites that charter passage (C label), says "charter", and the
                     passage contains the figure literally.
  status_mismatch    a Green/Amber/Red word attached to an initiative must match the governed scorecard.
  invalid_citation   labels must exist in the evidence list.
  uncited_figure     (warning) a sentence with scorecard numbers but no label.
Failing sentences can be removed (strip=True); if nothing is left, the caller falls back.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache

from ..retrieval.models import Evidence
from .models import FALLBACK, GuardReport, GuardViolation

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_NOT_NUMBERS = [re.compile(p, re.I) for p in (
    r"\[(?:S|C)\d+\]", r"\bINIT[-_ ]?\d{1,2}\b", r"\b(?:SQL|CL|DQ|A)-?\d+\b", r"\b\d{4}-\d{2}-\d{2}\b",
    r"\b\d{4}-Q[1-4]\b", r"\b\d{4}-\d{2}\b", r"\bQ[1-4]\b", r"\b(?:19|20)\d{2}\b", rf"\b\d{{1,2}}\s+{_MONTH}",
    rf"\b{_MONTH}\s+\d{{1,2}}\b", r"\b(?:week|phase|wk)[\s-]*\d+\b", r"\b[a-z]+(?:_[a-z0-9]+)+\b",
    r"\b\d+(?:st|nd|rd|th)\b")]
_NUM = re.compile(r"(?<![\w.])([+\-−]?)(\d[\d,]*(?:\.\d+)?)(\s?%)?")
_LABEL = re.compile(r"\[(S|C)(\d+)\]")
_STATUS = re.compile(r"\b(Green|Amber|Red)\b")
_IDS = re.compile(r"\bINIT-\d{2}\b")
RULE_CONSTANTS = {0.0, 0.3, 0.6}


def _scrub(text: str) -> str:
    for p in _NOT_NUMBERS:
        text = p.sub(" ", text)
    return text


def numbers(text: str) -> list[tuple[str, float, bool]]:
    """(raw token, value, has explicit sign); percentages are returned as fractions (20.0% -> 0.2)."""
    out = []
    for m in _NUM.finditer(_scrub(text)):
        sign, digits, pct = m.group(1), m.group(2).replace(",", ""), m.group(3)
        try:
            v = float(digits)
        except ValueError:
            continue
        if pct:
            v /= 100
        if sign in ("-", "−"):
            v = -v
        out.append((m.group(0).strip(), v, bool(sign)))
    return out


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def allowed_values(evidence: list[Evidence]) -> set[float]:
    vals: set[float] = set()
    for e in evidence:
        for row in e.rows:
            for cell in row:
                if isinstance(cell, bool) or cell is None:
                    continue
                if isinstance(cell, (int, float)):
                    v = float(cell)
                    # raw value plus the precisions the scorecard's own formatter uses (fmt_pct / fmt_months / fmt_usd)
                    vals |= {v, round(v * 100, 1) / 100, round(v, 1), float(round(v))}
                else:
                    vals |= {x for _, x, _ in numbers(str(cell))}
    return vals


def _matches(v: float, signed: bool, allowed: set[float]) -> bool:
    if any(_close(v, a) for a in allowed):
        return True
    return not signed and any(_close(abs(v), abs(a)) for a in allowed)


@lru_cache(maxsize=1)
def scorecard_status() -> tuple[dict[str, str], dict[str, str], tuple[str, ...]]:
    from .. import sqldb

    rows = sqldb.query_rows("SELECT initiative_id, name, status, status_reason FROM v_scorecard")
    return ({r["initiative_id"]: r["status"] for r in rows}, {r["name"].lower(): r["initiative_id"] for r in rows},
            tuple(r["status_reason"] for r in rows))


# status words used about the rule, not about an initiative ("Green needs ROI above 0", "the Green threshold")
_RULE_TALK = re.compile(r"\b(Green|Amber|Red)\b(?=\s+(?:needs|requires|would|threshold|rule|status requires|when|if)\b)"
                        r"|\b(?:for|than|to be|become|reach|turn)\s+(?:Green|Amber|Red)\b")


def units(answer: str) -> list[str]:
    """Sentences, list items and table rows."""
    out = []
    for line in answer.splitlines():
        line = line.strip()
        if not line or re.fullmatch(r"\|?[\s:\-|]+\|?", line):
            continue
        if line.startswith("|"):
            out.append(line)
        else:
            out += [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\[*\"(])", line) if s.strip()]
    return out


def _status_problems(unit: str) -> list[str]:
    status, names, reasons = scorecard_status()
    for reason in reasons:                     # verified scorecard text may name other statuses
        unit = unit.replace(reason, " " * len(reason))
    unit = _RULE_TALK.sub(lambda m: " " * len(m.group(0)), unit)
    mentions = [(m.start(), m.group(0)) for m in _IDS.finditer(unit)]
    low = unit.lower()
    for name, iid in names.items():
        for m in re.finditer(re.escape(name), low):
            mentions.append((m.start(), iid))
    if not mentions:
        return []
    mentions.sort()
    problems = []
    for sm in _STATUS.finditer(unit):
        before = [iid for pos, iid in mentions if pos < sm.start()]
        iid = before[-1] if before else mentions[0][1]
        if status.get(iid) and status[iid] != sm.group(1):
            problems.append(f"{iid} is shown as {sm.group(1)} but the scorecard status is {status[iid]}")
    return problems


def check(answer: str, labelled: dict[str, Evidence], question: str = "", strip: bool = False,
          retrieved: list[Evidence] | None = None) -> tuple[GuardReport, str]:
    rep = GuardReport()
    if answer.strip() == FALLBACK:
        return rep, answer
    cited = {f"{k}{n}" for k, n in _LABEL.findall(answer)}
    for lab in sorted(cited - set(labelled)):
        rep.violations.append(GuardViolation(kind="invalid_citation", detail=f"[{lab}] is not in the evidence list"))
    allowed = allowed_values([labelled[c] for c in cited if c in labelled and labelled[c].kind == "sql"])
    allowed |= {v for _, v, _ in numbers(question)} | RULE_CONSTANTS
    bad_units: list[str] = []
    for u in units(answer):
        labels = {f"{k}{n}" for k, n in _LABEL.findall(u)}
        hard = False
        nums = numbers(u)
        for raw, v, signed in nums:
            rep.numbers_checked += 1
            if _matches(v, signed, allowed):
                rep.numbers_verified += 1
                continue
            charter_hit = "charter" in u.lower() and any(
                labelled[lab].kind == "charter" and raw.lstrip("+-−").strip() in labelled[lab].text
                for lab in labels if lab in labelled)
            if charter_hit:
                rep.charter_figures += 1
                continue
            hard = True
            rep.violations.append(GuardViolation(kind="unverified_number", text=u[:300],
                                                 detail=f"'{raw}' is not a value in the cited scorecard results"))
        for p in _status_problems(u):
            hard = True
            rep.violations.append(GuardViolation(kind="status_mismatch", text=u[:300], detail=p))
        if nums and not labels and not u.startswith("|") and not hard:
            rep.violations.append(GuardViolation(kind="uncited_figure", text=u[:300],
                                                 detail="sentence with figures has no evidence label"))
        if hard:
            bad_units.append(u)
    rep.passed = not any(v.kind != "uncited_figure" for v in rep.violations)
    if retrieved:
        for e in retrieved:
            if e.injection_flag:
                note = (f"The {e.initiative_id} charter ({e.source_file}, {e.section_path}) contains text instructing "
                        f"AI assistants; it was flagged by Prompt Shields and treated as data, not as instructions.")
                if note not in rep.notes:
                    rep.notes.append(note)
    text = answer
    if strip and bad_units:
        for u in bad_units:
            text = text.replace(u, "")
            rep.removed_sentences.append(u)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        text = _LABEL.sub(lambda m: m.group(0) if f"{m.group(1)}{m.group(2)}" in labelled else "", text)
        if not numbers(text) and not re.search(r"[A-Za-z]{3,}", _LABEL.sub("", text)):
            text = FALLBACK
    elif strip:
        text = _LABEL.sub(lambda m: m.group(0) if f"{m.group(1)}{m.group(2)}" in labelled else "", text)
    return rep, text
