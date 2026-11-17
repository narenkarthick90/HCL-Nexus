# Nimbus Value Intelligence: Enterprise RAG Value Intelligence Assistant

An AI-portfolio assistant for the HCLTech *Future Ready AI Engineer* hackathon. Executives ask three recurring questions:
- Which AI initiatives are being adopted?
- Which are delivering financial value?
- Which need attention?

The assistant answers them in plain English, with sources, from two kinds of input:
- **Tabular data files** (usage, cost, benefit, plan). These are cleaned and turned into one **governed scorecard** by deterministic code.
- **Initiative charters** (PDF, DOCX, Markdown, HTML), retrieved from Azure AI Search.

> Design principle (from the brief): **code for numbers, retrieval for charter evidence, the language model for grounded explanation.**

- **Live demo:** https://nimbus-docs-assistant.blackmeadow-3fb66d6b.koreacentral.azurecontainerapps.io. It scales to zero, so the first request takes about 20–40 s.
- **Definitions, assumptions, client questions and corrections (FR-01):** [docs/data_quality_and_assumptions.md](docs/data_quality_and_assumptions.md)
- **Scorecard:** [outputs/scorecard.md](outputs/scorecard.md) · **Change log:** [outputs/change_log.md](outputs/change_log.md) · **Weekly report:** `outputs/weekly_report_<date>.{md,html,pdf}`

All data is synthetic and fictional ("Nimbus Group").

## Data flow

```
data/raw/ (xlsx + csv, messy)  ── value/clean.py (12 rules, config.yaml) ──►  data/curated/ + outputs/change_log
                                                 └─► value/scorecard.py (brief §4 formulas) ──► outputs/scorecard + value.sqlite (v_* views)
data/charters/ (pdf, docx, md, html) ── ingest/ (structure-aware chunks, Prompt Shields scan) ──► Azure AI Search "charters"

Question ─► nimbus-router (Foundry Agent Service)
            numeric ─► nimbus-sql      reads the v_* views; arithmetic, aggregates and functions are rejected in code
            narrative ─► nimbus-charter  hybrid + semantic search, filtered by initiative; returns passage ids
            mixed ─► both (or SQL + code-attached charter sections, e.g. risks of every Red/Amber initiative)
            pii_refusal / not_in_data ─► fixed refusal / "I do not have that in the data provided."
         ─► nimbus-writer (copies *_display values verbatim, cites [S#]/[C#])
         ─► number guard (code): every figure, status and citation checked against the scorecard
         ─► nimbus-judge (LLM, real time; pass gate in code) ─► revise ≤ 2 ─► final guard removes anything unverifiable
```

| Concern | Choice |
|---|---|
| Agents | Microsoft Agent Framework 1.26, published as 5 versioned prompt agents in **Microsoft Foundry Agent Service** |
| Numbers | pandas cleaning + pure-Python scorecard; read-only SQLite views; sqlglot guard (SELECT / WHERE / ORDER BY / LIMIT only) |
| Charters | Azure AI Search Free (Korea Central): hybrid BM25 + vectors, semantic ranker, synonym map, `initiative_id` / `section` filters |
| Injection defence | Azure AI Content Safety **Prompt Shields** at ingest, untrusted-data wrapping in prompts, statuses and numbers only from code |
| Models | `gpt-5-mini` (all roles), `text-embedding-3-small` |
| Tracing | OpenTelemetry → project App Insights → Foundry **Traces** (no LangSmith: data stays in Azure) |
| UI | FastAPI + static frontend: Ask (chat + live voice), Scorecard (provenance per value), Weekly report, Data quality, Charters |
| Hosting | Azure Container Apps (scale to zero), image in ACR Basic |

## How each requirement is met

| FR | Where |
|---|---|
| 01 Understand and document | `value/assumptions_template.md` → `docs/data_quality_and_assumptions.md` (definitions, A1–A12, 3 client questions, generated corrections) |
| 02 Load and clean | `value/clean.py`: every rule logs before/after and its **effect** (scorecard recomputed without the rule) |
| 03 Compute the scorecard | `value/scorecard.py`, matched exactly against an independent gold implementation (`tests/test_value.py`) |
| 04 Index charters | `ingest/`: source file, initiative id, section, page on every passage |
| 05 Route | `backend/agents/router.py` |
| 06 Grounded answers | `backend/pipeline/writer.py` + `guard.py`: sources shown for every statement |
| 07 Missing evidence | Exact fallback "I do not have that in the data provided." |
| 08 Guardrails | PII refusal (router), Prompt Shields + untrusted wrapping (charters), computation-free SQL + number/status guard |
| 09 Weekly report | `backend/report.py` (`POST /api/report/weekly`, MD/HTML/PDF) |
| 10 Interface | `frontend/` |

## Setup and reproduction

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
copy .env.example .env                               # endpoints and keys

.venv\Scripts\python data\generate_portfolio.py      # raw files, charters, planted issues, gold scorecard
.venv\Scripts\python -m value.build                  # clean -> scorecard -> change log -> value.sqlite (same hash on rerun)
.venv\Scripts\python -m ingest.build_indexes         # charter index + Prompt Shields scan (~$0.0001)
az login
.venv\Scripts\python -m backend.foundry publish      # publish/update the 5 agents in the Foundry project

.venv\Scripts\python -m uvicorn backend.api:app --port 8000     # http://localhost:8000
.venv\Scripts\python -m pytest -q                                # offline tests
.venv\Scripts\python -m backend.pipeline.qa "Which initiatives need attention and why?"
.venv\Scripts\python -m backend.report                          # weekly report
```

`AGENT_RUNTIME=local` runs the same agent definitions in-process instead of through Foundry Agent Service.
`JUDGE_MODE=auto` (the default) skips the LLM judge for numeric answers whose figures were all verified in code.

```
Azure for Students specifics are handled in the script:
- ACR Tasks are not permitted, so the image is built locally.
- The Container Apps environment uses WorkloadProfiles mode.
- The container's managed identity gets the "Foundry User" role.
- Logs go nowhere, so there is no Log Analytics cost.

## Assumptions and trade-offs
- **Synthetic data.** The portfolio is generated from `data/portfolio_spec.yaml` with 16 planted issues (`data/planted_issues.yaml`). The loader and rules are written so supplied files can replace it.
- **Portfolio ROI and adoption** are team-defined aggregates (assumption A8) and are labelled as such.
- **Charter figures are context, never scorecard values.** For example, INIT-01's charter states USD 350,000 while the register says USD 300,000; this is client question 1.
- **Read-only.** The assistant reports and recommends. It never changes source data or approves anything.
- **Speed.** Agent runs through Foundry are slower than in-process: about 20 s for numeric questions and 30–40 s for mixed ones.
