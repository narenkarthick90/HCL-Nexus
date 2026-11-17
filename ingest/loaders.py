"""Format-specific loaders (FR-01 / FR-02).

Each loader turns a file into a `Document` whose sections keep their heading path, page
numbers (PDF) and block types (paragraph / list / table). Boilerplate such as HTML nav and
footer, PDF running footers and TXT hard-wrapping is removed here.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pymupdf
from bs4 import BeautifulSoup, Tag

from .config import DOCS_DIR, ROOT
from .models import Block, Document, Section

_MANIFEST: dict[str, dict] | None = None


def manifest_entry(path: Path) -> dict:
    """Optional enrichment (initiative id etc.) from data/charters/manifest.yaml, keyed by file name."""
    global _MANIFEST
    if _MANIFEST is None:
        import yaml

        mf = DOCS_DIR / "manifest.yaml"
        _MANIFEST = {m["file"]: m for m in (yaml.safe_load(mf.read_text("utf-8")) or [])} if mf.exists() else {}
    return _MANIFEST.get(path.name, {})


_INIT = re.compile(r"\b(INIT-\d{2})\b", re.I)


def initiative_of(path: Path, doc_id: str = "") -> str:
    if m := manifest_entry(path).get("initiative_id"):
        return m
    for s in (doc_id, path.name):
        if mm := _INIT.search(s or ""):
            return mm.group(1).upper()
    return ""


# ---------------------------------------------------------------- section builder
class SectionBuilder:
    """Receives heading/block events in reading order and builds sections with heading paths."""

    def __init__(self) -> None:
        self.sections: list[Section] = []
        self._stack: list[str] = []
        self._current: Section | None = None

    def heading(self, level: int, text: str) -> None:
        text = text.strip()
        self._stack = self._stack[: level - 1] + [text]
        self._current = Section(heading=text, level=level, path=list(self._stack))
        self.sections.append(self._current)

    def block(self, kind: str, text: str, page: int | None = None) -> None:
        text = text.strip()
        if not text:
            return
        if self._current is None:                       # content before the first heading
            self._current = Section(heading="Introduction", level=1, path=["Introduction"])
            self.sections.append(self._current)
        blocks = self._current.blocks
        if kind == "table" and blocks and blocks[-1].kind == "table":
            prev_rows, new_rows = blocks[-1].text.split("\n"), text.split("\n")
            if prev_rows[0] == new_rows[0]:             # same header: table continued on the next page
                blocks[-1].text += "\n" + "\n".join(new_rows[2:])
                return
        blocks.append(Block(kind, text, page))

    def result(self) -> list[Section]:
        return [s for s in self.sections if s.blocks]    # drop empty container headings


def _table_to_text(rows: list[list[str]]) -> str:
    rows = [[re.sub(r"\s+", " ", (c or "")).strip() for c in r] for r in rows if any(c for c in r)]
    if not rows:
        return ""
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _finish(path: Path, domain: str, meta: dict, sections: list[Section]) -> Document:
    m = manifest_entry(path)
    doc_id = meta.get("doc_id") or m.get("initiative_id") or path.stem.upper()
    return Document(
        doc_id=doc_id,
        title=meta.get("title") or m.get("title") or path.stem,
        version=str(meta.get("version") or m.get("version") or ""),
        effective_date=str(meta.get("effective_date") or m.get("effective_date") or ""),
        owner=meta.get("owner") or m.get("owner") or "",
        domain=domain,
        summary=m.get("summary") or meta.get("summary") or "",
        source_path=path.resolve().relative_to(ROOT).as_posix(),
        file_type=path.suffix.lstrip(".").lower(),
        sections=sections,
        initiative_id=initiative_of(path, doc_id),
    )


# ---------------------------------------------------------------- Markdown
_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def load_markdown(path: Path, domain: str) -> Document:
    raw = path.read_text("utf-8")
    meta: dict = {}
    if m := _FM.match(raw):
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        raw = raw[m.end():]
    sb, para, lst, tbl = SectionBuilder(), [], [], []

    def flush() -> None:
        nonlocal para, lst, tbl
        if para:
            sb.block("p", " ".join(para)); para = []
        if lst:
            sb.block("list", "\n".join(lst)); lst = []
        if tbl:
            rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in tbl
                    if not re.match(r"^\|?\s*:?-{3,}", r.strip())]
            sb.block("table", _table_to_text(rows)); tbl = []

    for line in raw.splitlines():
        s = line.rstrip()
        if hm := re.match(r"^(#{1,6})\s+(.*)$", s):
            flush()
            level = len(hm.group(1)) - 1            # '# Title' is the document title, '##' = level 1
            if level == 0:
                meta.setdefault("title", hm.group(2).strip())
            else:
                sb.heading(level, hm.group(2))
        elif s.startswith("|"):
            if para or lst:
                flush()
            tbl.append(s)
        elif re.match(r"^(\s*[-*]\s+|\s*\d+\.\s+)", s):
            if para or tbl:
                flush()
            lst.append(s.strip())
        elif not s.strip():
            flush()
        else:
            if lst or tbl:
                flush()
            para.append(s.strip())
    flush()
    return _finish(path, domain, meta, sb.result())


# ---------------------------------------------------------------- HTML
def load_html(path: Path, domain: str) -> Document:
    soup = BeautifulSoup(path.read_text("utf-8"), "html.parser")
    meta = {"title": soup.title.get_text(strip=True) if soup.title else ""}
    for tag in soup.find_all("meta"):
        name = (tag.get("name") or "").replace("-", "_")
        if name in ("doc_id", "doc_version", "effective_date", "owner"):
            meta[name.replace("doc_version", "version")] = tag.get("content", "")
    for junk in soup.find_all(["nav", "footer", "header", "script", "style", "aside"]):
        junk.decompose()
    root: Tag = soup.find("main") or soup.body or soup
    if h1 := root.find("h1"):
        meta["title"] = h1.get_text(" ", strip=True)
    sb = SectionBuilder()
    for el in root.find_all(["h2", "h3", "h4", "h5", "p", "ul", "ol", "table"]):
        if el.find_parent(["li", "td", "th", "table"]) and el.name != "table":
            continue                                   # nested element already covered by its parent
        if el.name[0] == "h":
            sb.heading(int(el.name[1]) - 1, el.get_text(" ", strip=True))
        elif el.name == "p":
            if "doc-meta" in (el.get("class") or []):
                continue
            sb.block("p", el.get_text(" ", strip=True))
        elif el.name in ("ul", "ol"):
            items = [li.get_text(" ", strip=True) for li in el.find_all("li", recursive=False)]
            mark = (lambda i: f"{i}. ") if el.name == "ol" else (lambda i: "- ")
            sb.block("list", "\n".join(mark(i) + t for i, t in enumerate(items, 1)))
        else:
            rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])] for tr in el.find_all("tr")]
            sb.block("table", _table_to_text(rows))
    return _finish(path, domain, meta, sb.result())


# ---------------------------------------------------------------- TXT
_TXT_KEYS = {"TITLE": "title", "DOCUMENT ID": "doc_id", "VERSION": "version",
             "EFFECTIVE DATE": "effective_date", "OWNER": "owner"}


def load_txt(path: Path, domain: str) -> Document:
    lines = path.read_text("utf-8").splitlines()
    meta, i = {}, 0
    while i < len(lines) and not re.match(r"^={5,}$", lines[i]):
        if ":" in lines[i]:
            k, v = lines[i].split(":", 1)
            if k.strip() in _TXT_KEYS:
                meta[_TXT_KEYS[k.strip()]] = v.strip()
        i += 1
    sb, para, lst, tbl = SectionBuilder(), [], [], []

    def flush() -> None:
        nonlocal para, lst, tbl
        if para:
            sb.block("p", " ".join(para)); para = []
        if lst:
            sb.block("list", "\n".join(lst)); lst = []
        if tbl:
            rows = [[c.strip() for c in r.split("|")] for r in tbl if not re.match(r"^\s*-[-+ ]*-\s*$", r)]
            sb.block("table", _table_to_text(rows)); tbl = []

    for s in lines[i + 1:]:
        if m := re.match(r"^(\d+)\.\s+([A-Z][A-Z0-9 ,&/'\-]+)$", s):
            flush(); sb.heading(1, m.group(2).title().replace("'S", "'s"))
        elif m := re.match(r"^(\d+)\.(\d+)\s+(.+)$", s):
            flush(); sb.heading(2, m.group(3))
        elif m := re.match(r"^\s{2,}(?:-\s+|\d+\)\s+)(.*)$", s):
            if para or tbl:
                flush()
            n = len(lst) + 1
            lst.append(("- " if s.lstrip().startswith("-") else f"{n}. ") + m.group(1).strip())
        elif s.startswith("  ") and ("|" in s or re.match(r"^\s*-[-+ ]*-\s*$", s)):
            if para or lst:
                flush()
            tbl.append(s.strip())
        elif not s.strip():
            flush()
        else:
            if lst or tbl:
                flush()
            para.append(s.strip())
    flush()
    return _finish(path, domain, meta, sb.result())


# ---------------------------------------------------------------- PDF
_FOOTER_PT = 42.0            # 15 mm from the bottom edge
_META_LINE = re.compile(r"^Version\s+\S+.*Effective\s+\d{4}-\d{2}-\d{2}", re.I)


def _rects_overlap(a, b) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def _pdf_table_rows(page, table) -> list[list[str]]:
    """Read each cell through its bounding box; Table.extract() drops spaces between spans."""
    return [[page.get_textbox(c) if c else "" for c in row.cells] for row in table.rows]


def load_pdf(path: Path, domain: str) -> Document:
    pdf = pymupdf.open(path)
    md = pdf.metadata or {}
    meta = {"title": md.get("title", ""), "owner": md.get("author", ""), "summary": md.get("subject", "")}
    kw = [k.strip() for k in (md.get("keywords") or "").split(",")]
    if len(kw) == 3:
        meta.update(doc_id=kw[0], version=kw[1].lstrip("v"), effective_date=kw[2])

    # pass 1: collect every line on every page with its typography
    lines: list[dict] = []
    for pno, page in enumerate(pdf, 1):
        h = page.rect.height
        tables = []
        for t in page.find_tables():
            text = _table_to_text(_pdf_table_rows(page, t))
            if text:
                tables.append((t.bbox, text))
                lines.append(dict(page=pno, y=t.bbox[1], x=t.bbox[0], h=t.bbox[3] - t.bbox[1],
                                  text=text, size=0.0, bold=False, kind="table"))
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for ln in b["lines"]:
                spans = [s for s in ln["spans"] if s["text"].strip()]
                if not spans:
                    continue
                x0, y0, x1, y1 = ln["bbox"]
                if y1 > h - _FOOTER_PT or any(_rects_overlap(ln["bbox"], tb) for tb, _ in tables):
                    continue
                lines.append(dict(page=pno, y=y0, x=x0, h=y1 - y0,
                                  text=" ".join(s["text"] for s in spans).strip(),
                                  size=round(max(s["size"] for s in spans), 1),
                                  bold=any("bold" in s["font"].lower() or s["flags"] & 16 for s in spans),
                                  kind="text"))
    lines.sort(key=lambda l: (l["page"], round(l["y"]), l["x"]))

    # merge fragments on the same baseline (bullet glyph + item text, split spans)
    merged: list[dict] = []
    for ln in lines:
        prev = merged[-1] if merged else None
        if prev and prev["kind"] == ln["kind"] == "text" and prev["page"] == ln["page"] and abs(prev["y"] - ln["y"]) < 2:
            prev["text"] = (prev["text"] + " " + ln["text"]).strip()
            prev["bold"] = prev["bold"] and ln["bold"]
            prev["size"] = max(prev["size"], ln["size"])
        else:
            merged.append(dict(ln))

    text_lines = [l for l in merged if l["kind"] == "text"]
    weights: Counter[float] = Counter()                 # body size = size carrying most characters
    for l in text_lines:
        if not l["bold"]:
            weights[l["size"]] += len(l["text"])
    body = weights.most_common(1)[0][0] if weights else 10.0
    heading_sizes = sorted({l["size"] for l in text_lines if l["bold"] and l["size"] >= body + 2.5}, reverse=True)
    level_of = {s: i + 1 for i, s in enumerate(heading_sizes)}
    pitches = [b["y"] - a["y"] for a, b in zip(text_lines, text_lines[1:])
               if a["page"] == b["page"] and 0 < b["y"] - a["y"] < 30 and a["size"] == b["size"] == body]
    pitch = min(pitches) if pitches else body * 1.4

    # pass 2: emit headings / paragraphs / lists / tables
    sb = SectionBuilder()
    para: list[str] = []
    lst: list[str] = []
    para_page = lst_page = None
    prev = None
    title_seen = False

    def flush() -> None:
        nonlocal para, lst
        if para:
            sb.block("p", " ".join(para), para_page); para = []
        if lst:
            sb.block("list", "\n".join(lst), lst_page); lst = []

    for ln in merged:
        if ln["kind"] == "table":
            flush(); sb.block("table", ln["text"], ln["page"]); prev = None
            continue
        t = ln["text"]
        if ln["size"] in level_of:
            flush()
            if not title_seen and t.strip().lower() == meta["title"].strip().lower():
                title_seen = True                      # document title rendered as a heading
            else:
                sb.heading(level_of[ln["size"]], t)
            prev = None
            continue
        if _META_LINE.match(t):
            prev = None
            continue
        bullet = re.match(r"^(?:[•\-•]|\d{1,2}\.)\s+(.*)$", t)
        new_page = prev is not None and prev["page"] != ln["page"]
        gap = prev is not None and not new_page and (ln["y"] - prev["y"]) > pitch * 1.3
        continues = new_page and para and not re.search(r"[.!?:]$", para[-1]) and t[:1].islower()
        if bullet:
            if para:
                flush()
            if not lst:
                lst_page = ln["page"]
            n = len(lst) + 1
            lst.append(("- " if not t[0].isdigit() else f"{n}. ") + bullet.group(1))
        else:
            if lst and not gap and not new_page:         # wrapped continuation of the last list item
                lst[-1] += " " + t
                prev = ln
                continue
            if lst or (gap and para) or (new_page and not continues):
                flush()
            if not para:
                para_page = ln["page"]
            para.append(t)
        prev = ln
    flush()
    return _finish(path, domain, meta, sb.result())



# ---------------------------------------------------------------- DOCX
def load_docx(path: Path, domain: str) -> Document:
    from docx import Document as Docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = Docx(str(path))
    cp = d.core_properties
    meta = {"title": cp.title or "", "owner": cp.author or "", "summary": cp.subject or "",
            "doc_id": cp.identifier or "", "version": cp.version or ""}
    if cp.keywords and (m := re.search(r"(\d{4}-\d{2}-\d{2})", cp.keywords)):
        meta["effective_date"] = m.group(1)
    sb, lst = SectionBuilder(), []

    def flush_list():
        nonlocal lst
        if lst:
            sb.block("list", "\n".join(lst)); lst = []

    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "tbl":
            flush_list()
            t = Table(child, d)
            sb.block("table", _table_to_text([[c.text for c in r.cells] for r in t.rows]))
        elif tag == "p":
            para = Paragraph(child, d)
            style = (para.style.name if para.style is not None else "") or ""
            text = para.text.strip()
            if not text:
                continue
            if style == "Title":
                meta["title"] = meta["title"] or text
            elif style.startswith("Heading"):
                flush_list()
                lvl = int(style.split()[-1]) if style.split()[-1].isdigit() else 1
                sb.heading(lvl, text)
            elif style.startswith("List"):
                lst.append(("1. " if "Number" in style else "- ") + text)
            elif text.startswith("Version ") and "Effective" in text and not sb.sections:
                continue                                  # the metadata line under the title
            else:
                flush_list()
                sb.block("p", text)
    flush_list()
    return _finish(path, domain, meta, sb.result())

# ---------------------------------------------------------------- dispatch
LOADERS = {".docx": load_docx, ".md": load_markdown, ".html": load_html, ".htm": load_html, ".txt": load_txt, ".pdf": load_pdf}


def load_document(path: Path, domain: str | None = None) -> Document:
    path = Path(path)
    domain = domain or path.parent.name
    try:
        loader = LOADERS[path.suffix.lower()]
    except KeyError:
        raise ValueError(f"Unsupported format: {path.suffix} ({path})") from None
    return loader(path, domain)


def load_all(docs_dir: Path = DOCS_DIR, domains: list[str] | None = None) -> list[Document]:
    """Every supported file directly in docs_dir (the charters folder); domain = 'charters'."""
    return [load_document(f, "charters") for f in sorted(docs_dir.iterdir())
            if f.is_file() and f.suffix.lower() in LOADERS]
