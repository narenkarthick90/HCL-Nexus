"""Render one document content model to PDF, DOCX, HTML or Markdown.

Content model: {doc_id, file, title, version, effective_date, owner, summary, sections:[{level, heading, blocks}]}
blocks: ("p", text) | ("ul", [items]) | ("ol", [items]) | ("table", [[header...], [row...], ...])
"""
from __future__ import annotations

import html
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

META_KEYS = ("doc_id", "title", "version", "effective_date", "owner")


def S(level: int, heading: str, *blocks):
    return {"level": level, "heading": heading, "blocks": [b for b in blocks if b]}


def P(text: str):
    return ("p", text)


def UL(*items: str):
    return ("ul", list(items))


def TABLE(header: list[str], rows: list[list]):
    return ("table", [header] + [[str(c) for c in r] for r in rows])


def render_md(d) -> str:
    out = ["---"] + [f"{k}: {d[k]}" for k in META_KEYS] + ["---", "", f"# {d['title']}", ""]
    for s in d["sections"]:
        out += ["#" * (s["level"] + 1) + " " + s["heading"], ""]
        for kind, val in s["blocks"]:
            if kind == "p":
                out += [val, ""]
            elif kind in ("ul", "ol"):
                out += [(f"{i}. " if kind == "ol" else "- ") + it for i, it in enumerate(val, 1)] + [""]
            elif kind == "table":
                out += ["| " + " | ".join(val[0]) + " |", "|" + "---|" * len(val[0])]
                out += ["| " + " | ".join(r) + " |" for r in val[1:]] + [""]
    return "\n".join(out)


def render_html(d, site: str = "Nimbus Group") -> str:
    e = lambda t: html.escape(str(t))
    body = [f"<h1>{e(d['title'])}</h1>",
            f'<p class="doc-meta">Version {e(d["version"])} &middot; Effective {e(d["effective_date"])}'
            f' &middot; Owner: {e(d["owner"])}</p>']
    for s in d["sections"]:
        body.append(f"<h{s['level'] + 1}>{e(s['heading'])}</h{s['level'] + 1}>")
        for kind, val in s["blocks"]:
            if kind == "p":
                body.append(f"<p>{e(val)}</p>")
            elif kind in ("ul", "ol"):
                body.append(f"<{kind}>" + "".join(f"<li>{e(i)}</li>" for i in val) + f"</{kind}>")
            elif kind == "table":
                head = "".join(f"<th>{e(c)}</th>" for c in val[0])
                rows = "".join("<tr>" + "".join(f"<td>{e(c)}</td>" for c in r) + "</tr>" for r in val[1:])
                body.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
    # nav/footer are boilerplate the ingestion cleaner must strip
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{e(d['title'])}</title>
<meta name="doc-id" content="{e(d['doc_id'])}">
<meta name="doc-version" content="{e(d['version'])}">
<meta name="effective-date" content="{e(d['effective_date'])}">
<meta name="owner" content="{e(d['owner'])}">
<style>body{{font-family:Segoe UI,Arial,sans-serif;max-width:860px;margin:2rem auto}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:4px 8px}}</style>
</head>
<body>
<nav>AI Portfolio Office | Charters | Scorecard | Sign out</nav>
<main>
{chr(10).join(body)}
</main>
<footer>&copy; 2026 {e(site)} (fictional). Internal use only.</footer>
</body>
</html>
"""


def render_pdf(d, path: Path, site: str = "Nimbus Group") -> None:
    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["BodyText"], fontSize=10, leading=14, spaceAfter=6)
    cell = ParagraphStyle("cell", parent=body, fontSize=9, leading=11, spaceAfter=0)
    e = lambda t: html.escape(str(t), quote=False)
    story = [Paragraph(e(d["title"]), ss["Title"]),
             Paragraph(f"Version {e(d['version'])} &middot; Effective {e(d['effective_date'])} &middot; "
                       f"Owner: {e(d['owner'])}", ss["Italic"]), Spacer(1, 6 * mm)]
    for s in d["sections"]:
        story.append(Paragraph(e(s["heading"]), ss["Heading1" if s["level"] == 1 else "Heading2"]))
        for kind, val in s["blocks"]:
            if kind == "p":
                story.append(Paragraph(e(val), body))
            elif kind in ("ul", "ol"):
                story.append(ListFlowable([ListItem(Paragraph(e(i), body)) for i in val],
                                          bulletType="1" if kind == "ol" else "bullet", leftIndent=14))
            elif kind == "table":
                t = Table([[Paragraph(e(c), cell) for c in r] for r in val], repeatRows=1, hAlign="LEFT")
                t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                                       ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dde6f0")),
                                       ("VALIGN", (0, 0), (-1, -1), "TOP")]))
                story += [t, Spacer(1, 3 * mm)]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawString(20 * mm, 10 * mm, f"{d['title']} - v{d['version']} - {site} (fictional)")
        canvas.drawRightString(190 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    SimpleDocTemplate(str(path), pagesize=A4, title=d["title"], author=d["owner"], subject=d["summary"],
                      keywords=f"{d['doc_id']},v{d['version']},{d['effective_date']}",
                      topMargin=18 * mm, bottomMargin=18 * mm).build(story, onFirstPage=footer, onLaterPages=footer)


def render_docx(d, path: Path) -> None:
    from docx import Document

    doc = Document()
    cp = doc.core_properties
    cp.title, cp.author, cp.subject = d["title"], d["owner"], d["summary"]
    cp.keywords = f"{d['doc_id']},v{d['version']},{d['effective_date']}"
    cp.identifier, cp.version = d["doc_id"], str(d["version"])
    doc.add_heading(d["title"], level=0)
    doc.add_paragraph(f"Version {d['version']} · Effective {d['effective_date']} · Owner: {d['owner']}").italic = True
    for s in d["sections"]:
        doc.add_heading(s["heading"], level=s["level"])
        for kind, val in s["blocks"]:
            if kind == "p":
                for para in str(val).splitlines():
                    if para.strip():
                        doc.add_paragraph(para.strip())
            elif kind in ("ul", "ol"):
                for it in val:
                    doc.add_paragraph(it, style="List Bullet" if kind == "ul" else "List Number")
            elif kind == "table":
                t = doc.add_table(rows=len(val), cols=len(val[0]))
                t.style = "Table Grid"
                for r, row in enumerate(val):
                    for c, txt in enumerate(row):
                        t.cell(r, c).text = txt
                        if r == 0:
                            for run in t.cell(r, c).paragraphs[0].runs:
                                run.bold = True
    doc.save(str(path))


def write(d, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix
    if ext == ".pdf":
        render_pdf(d, path)
    elif ext == ".docx":
        render_docx(d, path)
    elif ext == ".html":
        path.write_text(render_html(d), encoding="utf-8")
    elif ext == ".md":
        path.write_text(render_md(d), encoding="utf-8")
    else:
        raise ValueError(ext)