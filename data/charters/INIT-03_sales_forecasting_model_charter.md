---
doc_id: INIT-03
title: Sales Forecasting Model: Initiative Charter
version: 1.0
effective_date: 2026-01-15
owner: Tom Achterberg
---

# Sales Forecasting Model: Initiative Charter

## Initiative overview

| Field | Value |
|---|---|
| Initiative ID | INIT-03 |
| Initiative name | Sales Forecasting Model |
| Type | Predictive model |
| Business unit | Sales |
| Go-live date | 2026-01-15 |

Replace spreadsheet-based quarterly forecasts with a weekly machine-learning forecast per region and product line, used by account executives and sales managers.

## Scope

### In scope

- Weekly forecast refresh in the CRM dashboard
- Forecast explanations per opportunity
- Sales managers and account executives in EMEA and APAC

### Out of scope

- Commission calculations
- Pricing recommendations

## Benefit hypothesis

Better forecast accuracy reduces excess inventory and expedited shipping costs; benefit is measured by the finance team in EUR each month.

## KPIs and measurement

- Forecast accuracy (MAPE) versus the spreadsheet baseline
- Weekly active users among licensed sales staff
- Inventory write-downs

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Sales staff keep using personal spreadsheets | Managers review the model forecast in weekly pipeline calls |
| Model drift after the new product launch | Monthly retraining and drift monitoring |
| Low trust because forecasts are hard to explain | Per-opportunity explanations in the dashboard |

## Governance

| Role | Name | Title |
|---|---|---|
| Executive sponsor | Elena Petrova | Chief Revenue Officer |
| Product owner | Tom Achterberg | Sales Analytics Manager |

## Dependencies

- CRM data quality programme
- Data platform migration

## Status notes

Adoption in APAC lags EMEA; a targeted enablement session is planned.
