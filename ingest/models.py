"""Data model shared by loaders, chunker and indexer."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Block:
    kind: str                 # "p" | "list" | "table"
    text: str
    page: int | None = None   # 1-based page for PDFs, None otherwise


@dataclass
class Section:
    heading: str
    level: int                # 1 = top-level section, 2 = subsection, ...
    path: list[str]           # heading path from the top, e.g. ["Pay and Benefits", "Payslips"]
    blocks: list[Block] = field(default_factory=list)

    @property
    def page(self) -> int | None:
        return next((b.page for b in self.blocks if b.page), None)

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks)


@dataclass
class Document:
    doc_id: str
    title: str
    version: str
    effective_date: str
    owner: str
    domain: str
    summary: str
    source_path: str          # path relative to the repo root, forward slashes
    file_type: str            # pdf | docx | html | md | txt
    sections: list[Section] = field(default_factory=list)
    initiative_id: str = ""


@dataclass
class Chunk:
    """One retrievable passage (child). `parent_text` is the full section it came from."""
    id: str
    doc_id: str
    parent_id: str
    chunk_index: int
    title: str
    section_path: str
    page: int | None
    domain: str
    doc_version: str
    effective_date: str
    owner: str
    source_url: str
    file_type: str
    text: str                 # raw passage text (what gets cited)
    content: str              # contextual header + text (what gets embedded / BM25-searched)
    parent_text: str
    initiative_id: str = ""
    section: str = ""             # top-level charter section, e.g. "Risks and mitigations"
    source_file: str = ""         # file name shown in citations
    injection_flag: bool = False  # Prompt Shields / heuristic found instructions aimed at an AI system
    content_vector: list[float] | None = None

    def to_search_doc(self) -> dict:
        d = asdict(self)
        return d
