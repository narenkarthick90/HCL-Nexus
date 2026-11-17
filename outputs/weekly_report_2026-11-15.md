# Nimbus Group AI portfolio: weekly report (week ending 2026-11-15)

*Generated 2026-11-17T14:44:52 from the governed scorecard (`value/scorecard.py`) and the initiative charters.*

## Headline

As of 2026-11-15, 3 of 10 initiatives are Green, 5 Amber and 2 Red. Realised benefit to date is USD 1,476,800 against USD 1,308,000 total cost (portfolio ROI +12.9%, a team-defined aggregate). Latest-week adoption across all licences is 57.8%. [Source: v_portfolio_summary]

## Initiatives needing attention

### Red · INIT-06 Claims Triage Bot

**Status rule:** Red: adoption 22.0% is below 30% and ROI -14.8% is below 0

Handlers distrust the triage recommendations and often re-triage manually, which the charter identifies as a key risk to adoption and benefits [C1]. Handler feedback says the console adds clicks for complex claims, undermining ease of use and aligning with the charter's noted risk of complex claims being forced through a rigid flow [C3][C1].

Sources: [C1] INIT-06_claims_triage_bot_charter.docx > Risks and mitigations; [C3] INIT-06_claims_triage_bot_charter.docx > Status notes

### Red · INIT-07 Churn Prediction Model

**Status rule:** Red: ROI -60.0% is below 0

ROI is -60.0% [S1]. The charter flags the risk that retention offers cost more than the margin they save, with mitigation to offer budget caps per segment [C1].

Sources: [S1] Governed scorecard (v_scorecard); [C1] INIT-07_churn_prediction_model_charter.md > Risks and mitigations

### Amber · INIT-08 Contract Review Copilot

**Status rule:** Amber: ROI 0.0% is exactly 0, not above 0

ROI is 0.0% which triggers the attention flag [S1]. The charter also lists the risk of "Missed non-standard clauses" with the mitigation that a lawyer signs off every review, which is material to safe deployment and benefit realisation [C1][C2][C3].

Sources: [S1] Governed scorecard (v_scorecard); [C1] INIT-08_contract_review_copilot_charter.html > Risks and mitigations; [C2] INIT-08_contract_review_copilot_charter.html > Benefit hypothesis; [C3] INIT-08_contract_review_copilot_charter.html > Status notes

### Amber · INIT-09 IT Helpdesk Voice Bot

**Status rule:** Amber: adoption 35.0% is between 30% and 60%

A week-2 usage export failed and was not re-run, creating a data gap and resulting in "no usage reported for week(s) 2026-11-02" on the scorecard [C3][S1].

Sources: [S1] Governed scorecard (v_scorecard); [C3] INIT-09_it_helpdesk_voice_bot_charter.pdf > Status notes, p. 2

### Amber · INIT-03 Sales Forecasting Model

**Status rule:** Amber: adoption 45.0% is between 30% and 60%

Low trust because forecasts are hard to explain is listed as a charter risk, with mitigation via per-opportunity explanations in the dashboard [C1]. Adoption in APAC lags EMEA and a targeted enablement session is planned, which is noted in the charter status notes [C3].

Sources: [C1] INIT-03_sales_forecasting_model_charter.md > Risks and mitigations; [C3] INIT-03_sales_forecasting_model_charter.md > Status notes

### Amber · INIT-10 Marketing Content Generator

**Status rule:** Amber: adoption 43.0% is between 30% and 60%

Run-rate versus plan is not available because the planned annual benefit was not supplied in the register [S1][C2]. The charter states the benefit is agency spend avoided for first drafts and translation, which is therefore unquantified in the scorecard [C1][C2].

Sources: [S1] Governed scorecard (v_scorecard); [C1] INIT-10_marketing_content_generator_charter.md > Benefit hypothesis; [C2] INIT-10_marketing_content_generator_charter.md > Status notes

### Amber · INIT-04 Invoice Document AI

**Status rule:** Amber: adoption 55.0% is between 30% and 60%

Usage dipped in the latest week because of a month-end close peak [C3]. The charter flags that clerks can bypass the queue during month-end peaks, a risk with a defined mitigation (month-end staffing plan) that helps explain the attention required [C1].

Sources: [C1] INIT-04_invoice_document_ai_charter.html > Risks and mitigations; [C3] INIT-04_invoice_document_ai_charter.html > Status notes

## Scorecard

| Initiative | Name | Status | Adoption | WAU change | ROI | Avg monthly benefit | Payback | Run-rate vs plan |
|---|---|---|---|---|---|---|---|---|
| INIT-01 | HR Policy Copilot | Green | 70.0% | +7.1% | +20.0% | USD 24,000 | 6.7 months | 96.0% |
| INIT-02 | Customer Service Chatbot | Green | 80.0% | +8.5% | +29.6% | USD 35,000 | 9.3 months | 100.0% |
| INIT-03 | Sales Forecasting Model | Amber | 45.0% | +7.1% | +23.4% | USD 17,280 | 8.1 months | 103.7% |
| INIT-04 | Invoice Document AI | Amber | 55.0% | -5.7% | +50.0% | USD 17,000 | 6.0 months | 97.1% |
| INIT-05 | Developer Code Assistant | Green | 72.5% | +14.5% | +27.2% | USD 19,500 | 4.7 months | 93.6% |
| INIT-06 | Claims Triage Bot | Red | 22.0% | -26.7% | -14.8% | USD 14,000 | 8.2 months | 70.0% |
| INIT-07 | Churn Prediction Model | Red | 66.7% | +5.3% | -60.0% | USD 12,000 | 12.5 months | 48.0% |
| INIT-08 | Contract Review Copilot | Amber | 70.0% | +9.4% | 0.0% | USD 15,000 | 4.0 months | 90.0% |
| INIT-09 | IT Helpdesk Voice Bot | Amber | 35.0% | +40.0% | +9.6% | USD 19,000 | 2.7 months | 87.7% |
| INIT-10 | Marketing Content Generator | Amber | 43.0% | -14.0% | +30.1% | USD 10,500 | 10.8 months | not available |

Source: v_scorecard (all values calculated in code from the cleaned data).

## Usage movers (weekly active users, first vs latest supplied week)

- Declining: INIT-06 Claims Triage Bot -26.7%
- Declining: INIT-10 Marketing Content Generator -14.0%
- Declining: INIT-04 Invoice Document AI -5.7%
- Growing: INIT-09 IT Helpdesk Voice Bot +40.0%
- Growing: INIT-05 Developer Code Assistant +14.5%
- Growing: INIT-08 Contract Review Copilot +9.4%

## Data quality

22 corrections were applied before calculating (full log: outputs/change_log.md). Corrections that changed a status:

- CL-04 usage_weekly.csv (date format): INIT-05: adoption n/a -> 72.5%; WAU change n/a -> +14.5%; status Amber -> Green
- CL-13 usage_weekly.csv (metric names): INIT-01: adoption n/a -> 70.0%; WAU change n/a -> +7.1%; status Amber -> Green | INIT-02: adoption n/a -> 80.0%; WAU change n/a -> +8.5%; status Amber -> Green | INIT-03: adoption n/a -> 45.0%; WAU change n/a -> +7.1%

## Guardrail notes

- The INIT-07 charter (INIT-07_churn_prediction_model_charter.md, Status notes) contains text instructing AI assistants; it was flagged by Prompt Shields and treated as data, not as instructions.
