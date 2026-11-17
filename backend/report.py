"""Weekly executive portfolio report (FR-09).

Everything quantitative is assembled in code from the governed scorecard (no LLM): headline, Green/Amber/Red table,
attention list (Red first, then Amber, each with the status rule that fired), usage movers and the data-quality
corrections that changed a status. The writer agent adds only a 2-3 sentence "why it needs attention" note per
initiative from that initiative's scorecard row and charter passages (Risks, Benefit hypothesis, Status notes);
each note passes the number/status guard, and anything unverifiable is removed.

  python -m backend.report            -> outputs/weekly_report_<as_of>.{md,html,pdf,json}
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import re
from pathlib import Path

from . import sqldb
from .pipeline import guard
from .pipeline.models import FALLBACK
from .pipeline.writer import write_answer
from .retrieval.models import Evidence
from .retrieval.search import section_lookup
from .settings import ROOT

OUT = ROOT / "outputs"
NOTE_SECTIONS = ["Risks and mitigations", "Benefit hypothesis", "Status notes"]
ROW_SQL = ("SELECT initiative_id, name, status, status_reason, adoption_display, wau_change_display, roi_display, "
           "avg_monthly_benefit_display, payback_display, run_rate_vs_plan_display, data_notes "
           "FROM v_scorecard WHERE initiative_id = '{iid}'")
NOTE_TASK = ("Write 1-2 sentences for the weekly executive report on why {name} ({iid}) needs attention. Its status "
             "and status rule are printed just above your note, so do not repeat them; give the most relevant charter "
             "risk, status note or data gap that explains the position. Do not recommend decisions. Cite every "
             "sentence.")


def _sql_evidence(sql: str, eid: str) -> Evidence:
    cols, rows, _ = sqldb.run_select(sql)
    return Evidence(id=eid, kind="sql", title="Governed value scorecard", section_path="query result",
                    text=sqldb.rows_to_markdown(cols, rows), sql=sql, columns=cols, rows=[list(r) for r in rows],
                    source_file="outputs/value.sqlite")


async def attention_note(row: dict) -> dict:
    iid = row["initiative_id"]
    s1 = _sql_evidence(ROW_SQL.format(iid=iid), f"SQL-{iid}")
    passages = [e for sec in NOTE_SECTIONS for e in await section_lookup(iid, sec)]
    labelled = {"S1": s1, **{f"C{i}": e for i, e in enumerate(passages, 1)}}
    q = f"Why does {row['name']} ({iid}) need attention?"
    draft = await write_answer(q, labelled, task=NOTE_TASK.format(name=row["name"], iid=iid))
    rep, text = guard.check(draft["answer_markdown"], labelled, q, strip=True, retrieved=passages)
    if not rep.passed:                                       # one revision with the guard's findings
        critique = "; ".join(f"{v.kind}: {v.detail}" for v in rep.violations)
        draft = await write_answer(q, labelled, task=NOTE_TASK.format(name=row["name"], iid=iid), previous=draft,
                                   critique=critique)
        rep, text = guard.check(draft["answer_markdown"], labelled, q, strip=True, retrieved=passages)
    if text == FALLBACK:                                     # never leave an initiative without the code facts
        text = f"{row['status_reason']} [S1]."
    used = {lab for lab in re.findall(r"\[((?:S|C)\d+)\]", text)}
    sources = [{"label": lab, "kind": e.kind, "citation": e.citation if e.kind == "sql" else e.citation,
                "query": e.sql, "excerpt": e.excerpt if e.kind == "charter" else ""}
               for lab, e in labelled.items() if lab in used]
    return {"initiative_id": iid, "name": row["name"], "status": row["status"], "status_reason": row["status_reason"],
            "note": text, "sources": sources, "guard": rep.model_dump(),
            "injection_notes": rep.notes}


def _attention_order(r: dict) -> tuple:
    reds = r["status_reason"].count(" below ")
    return (0 if r["status"] == "Red" else 1, -reds, r["roi"] if r["roi"] is not None else 0)


async def build_report() -> dict:
    rows = sqldb.query_rows("SELECT * FROM v_scorecard ORDER BY initiative_id")
    summary = sqldb.query_rows("SELECT * FROM v_portfolio_summary")[0]
    changes = sqldb.query_rows("SELECT id, file, initiative, kind, before, after, effect FROM v_change_log")
    attention = sorted([r for r in rows if r["status"] != "Green"], key=_attention_order)
    notes = await asyncio.gather(*(attention_note(r) for r in attention))
    movers = sorted([r for r in rows if r["wau_change"] is not None], key=lambda r: r["wau_change"])
    status_changes = [c for c in changes if "status " in c["effect"]]
    as_of = summary["as_of"]
    return {
        "title": f"Nimbus Group AI portfolio: weekly report (week ending {as_of})",
        "as_of": as_of, "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "headline": (f"As of {as_of}, {summary['green']} of {summary['initiatives']} initiatives are Green, "
                     f"{summary['amber']} Amber and {summary['red']} Red. Realised benefit to date is "
                     f"{summary['total_realised_benefit_display']} against {summary['total_cost_display']} total cost "
                     f"(portfolio ROI {summary['portfolio_roi_display']}, a team-defined aggregate). Latest-week "
                     f"adoption across all licences is {summary['portfolio_adoption_display']}."),
        "summary_source": "SELECT * FROM v_portfolio_summary",
        "scorecard": [{k: r[k] for k in ("initiative_id", "name", "status", "adoption_display", "wau_change_display",
                                         "roi_display", "avg_monthly_benefit_display", "payback_display",
                                         "run_rate_vs_plan_display")} for r in rows],
        "scorecard_source": "SELECT initiative_id, name, status, *_display FROM v_scorecard",
        "attention": notes,
        "movers": {"declining": [{"initiative_id": r["initiative_id"], "name": r["name"],
                                  "wau_change_display": r["wau_change_display"]} for r in movers if r["wau_change"] < 0],
                   "growing": [{"initiative_id": r["initiative_id"], "name": r["name"],
                                "wau_change_display": r["wau_change_display"]} for r in reversed(movers)
                               if r["wau_change"] > 0][:3]},
        "data_quality": {"corrections": len(changes), "status_changing": status_changes},
        "guardrail_notes": sorted({n for a in notes for n in a["injection_notes"]}),
    }


# ---------------------------------------------------------------- rendering
def to_markdown(r: dict) -> str:
    out = [f"# {r['title']}", "", f"*Generated {r['generated']} from the governed scorecard "
                                  f"(`value/scorecard.py`) and the initiative charters.*", "",
           "## Headline", "", r["headline"] + " [Source: v_portfolio_summary]", "",
           "## Initiatives needing attention", ""]
    for a in r["attention"]:
        out += [f"### {a['status']} · {a['initiative_id']} {a['name']}", "", f"**Status rule:** {a['status_reason']}",
                "", a["note"], "", "Sources: " + "; ".join(
                    f"[{s['label']}] {s['citation']}" for s in a["sources"]), ""]
    out += ["## Scorecard", "", "| Initiative | Name | Status | Adoption | WAU change | ROI | Avg monthly benefit | "
                                "Payback | Run-rate vs plan |", "|---|---|---|---|---|---|---|---|---|"]
    for s in r["scorecard"]:
        out.append("| " + " | ".join(str(s[k] or "not available") for k in (
            "initiative_id", "name", "status", "adoption_display", "wau_change_display", "roi_display",
            "avg_monthly_benefit_display", "payback_display", "run_rate_vs_plan_display")) + " |")
    out += ["", "Source: v_scorecard (all values calculated in code from the cleaned data).", "",
            "## Usage movers (weekly active users, first vs latest supplied week)", ""]
    out += [f"- Declining: {m['initiative_id']} {m['name']} {m['wau_change_display']}" for m in r["movers"]["declining"]]
    out += [f"- Growing: {m['initiative_id']} {m['name']} {m['wau_change_display']}" for m in r["movers"]["growing"]]
    dq = r["data_quality"]
    out += ["", "## Data quality", "", f"{dq['corrections']} corrections were applied before calculating "
                                       f"(full log: outputs/change_log.md). Corrections that changed a status:", ""]
    out += [f"- {c['id']} {c['file']} ({c['kind'].replace('_', ' ')}): {c['effect']}" for c in dq["status_changing"]]
    if r["guardrail_notes"]:
        out += ["", "## Guardrail notes", ""] + [f"- {n}" for n in r["guardrail_notes"]]
    return "\n".join(out) + "\n"


def to_html(md: str) -> str:
    body, in_table, in_list = [], False, False
    for line in md.splitlines():
        e = html.escape(line)
        e = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", e)
        e = re.sub(r"\*(.+?)\*", r"<em>\1</em>", e)
        e = re.sub(r"`(.+?)`", r"<code>\1</code>", e)
        if line.startswith("|"):
            if re.fullmatch(r"\|[-| ]+\|", line):
                continue
            cells = [c.strip() for c in e.strip("|").split("|")]
            tag = "th" if not in_table else "td"
            body.append(("<table>" if not in_table else "") + "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>")
            in_table = True
            continue
        if in_table:
            body.append("</table>"); in_table = False
        if line.startswith("- "):
            body.append(("<ul>" if not in_list else "") + f"<li>{e[2:]}</li>"); in_list = True
            continue
        if in_list:
            body.append("</ul>"); in_list = False
        m = re.match(r"(#{1,3}) (.*)", e)
        body.append(f"<h{len(m.group(1))}>{m.group(2)}</h{len(m.group(1))}>" if m else (f"<p>{e}</p>" if e else ""))
    if in_table:
        body.append("</table>")
    if in_list:
        body.append("</ul>")
    return "\n".join(body)


def to_pdf(r: dict, path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=9.5, leading=13)
    small = ParagraphStyle("s", parent=body, fontSize=8, leading=10, textColor=colors.HexColor("#555555"))
    cell = ParagraphStyle("c", parent=body, fontSize=8, leading=10)
    rag = {"Green": "#1f7a3a", "Amber": "#a86400", "Red": "#b3261e"}
    e = lambda t: html.escape(str(t if t is not None else "not available"))
    story = [Paragraph(e(r["title"]), ss["Title"]), Paragraph(e(r["headline"]) + " [v_portfolio_summary]", body),
             Spacer(1, 4 * mm), Paragraph("Initiatives needing attention", ss["Heading2"])]
    for a in r["attention"]:
        story += [Paragraph(f'<font color="{rag[a["status"]]}"><b>{a["status"]}</b></font> · '
                            f'<b>{e(a["initiative_id"])} {e(a["name"])}</b>', body),
                  Paragraph(e(a["note"]), body),
                  Paragraph("Sources: " + e("; ".join(f"[{s['label']}] {s['citation']}" for s in a["sources"])), small),
                  Spacer(1, 2 * mm)]
    story.append(Paragraph("Scorecard", ss["Heading2"]))
    head = ["Initiative", "Name", "Status", "Adoption", "WAU change", "ROI", "Avg monthly benefit", "Payback", "Run-rate"]
    data = [[Paragraph(h, cell) for h in head]] + [[Paragraph(e(s[k]), cell) for k in (
        "initiative_id", "name", "status", "adoption_display", "wau_change_display", "roi_display",
        "avg_monthly_benefit_display", "payback_display", "run_rate_vs_plan_display")] for s in r["scorecard"]]
    t = Table(data, repeatRows=1, colWidths=[18 * mm, 42 * mm, 14 * mm, 17 * mm, 18 * mm, 16 * mm, 26 * mm, 20 * mm, 17 * mm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f6")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [t, Paragraph("Source: v_scorecard (calculated in code from the cleaned data).", small),
              Paragraph("Data quality", ss["Heading2"]),
              Paragraph(f"{r['data_quality']['corrections']} corrections applied before calculating; corrections that "
                        f"changed a status:", body)]
    story += [Paragraph(e(f"{c['id']} {c['file']}: {c['effect']}"), small) for c in r["data_quality"]["status_changing"]]
    if r["guardrail_notes"]:
        story += [Paragraph("Guardrail notes", ss["Heading2"])] + [Paragraph(e(n), small) for n in r["guardrail_notes"]]
    SimpleDocTemplate(str(path), pagesize=A4, title=r["title"], leftMargin=14 * mm, rightMargin=14 * mm,
                      topMargin=14 * mm, bottomMargin=14 * mm).build(story)


async def generate(out_dir: Path = OUT) -> dict:
    r = await build_report()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / f"weekly_report_{r['as_of']}"
    md = to_markdown(r)
    stem.with_suffix(".md").write_text(md, encoding="utf-8")
    stem.with_suffix(".json").write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
    stem.with_suffix(".html").write_text(f"<!doctype html><meta charset='utf-8'><title>{html.escape(r['title'])}</title>"
                                         f"<body style='font-family:Segoe UI,Arial;max-width:900px;margin:2rem auto'>"
                                         f"{to_html(md)}</body>", encoding="utf-8")
    to_pdf(r, stem.with_suffix(".pdf"))
    area = "outputs" if out_dir.resolve() == OUT.resolve() else "reports"
    r["files"] = {ext: f"/files/{area}/{stem.name}.{ext}" for ext in ("md", "html", "pdf", "json")}
    r["markdown"], r["html"] = md, to_html(md)
    return r


async def _cli() -> None:
    from .foundry import close
    from .retrieval.search import close_clients

    r = await generate()
    await close_clients()
    await close()
    print(r["markdown"])
    print("files:", r["files"])


if __name__ == "__main__":
    asyncio.run(_cli())
