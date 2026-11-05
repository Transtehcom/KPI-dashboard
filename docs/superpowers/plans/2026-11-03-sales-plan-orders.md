# Sales plan from customer order amounts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Sales-plan fact equals the sum of the 1C customer-order «Сумма» field for orders in «В закуп» in the selected period.
**Architecture:** Extend the existing conversion_orders dataset with СуммаДокумента→amount and migrate old databases. Reuse the same deduplicated sales-order selection and source-coverage checks for the sales-plan revenue calculation. Keep configured plans, weights, manager attribution and the independent gross-profit calculator.
**Tech Stack:** Existing Python/SQLite/OData/unittest; no dependencies.
**Spec:** User explicitly requested «Выполнение плана продаж так же берётся из "Сумма" в "Заказы клиентов" со статусом "В закуп"». Existing accepted order-date/manager rules apply.

## Global Constraints

- Fact is raw СуммаДокумента, without VAT subtraction or realization/UPD signature requirements.
- Same order date, statusТТК_ВЗакуп and manager-of-order rules as accepted conversion. Sales role only, each order once; supply copies do not multiply amount.
- Existing individual overrides, team-plan split, targets, historical weights and calendar periods remain unchanged. All sales-plan profiles using metric=revenue use this fact.
- Missing amount is unknown, not zero. Migrated old rows with NULL amounts require refreshed data; genuine numeric zero is valid. Missing source coverage remains undefined.
- Actual gross-profit calculations (metric=gross_profit), cash and receivables are unchanged.
- Preserve current workload checkout and accumulated edits; no employee assignment or external writes; Git deferred.
- Apply by reloading the confirmed dashboard and refreshing only conversion_orders through existing OneC.fetch/replace_rows in a transaction; other input coverages are already current today.

## Review Focus

- Sum excludes wrong status/date/manager and supply duplicates; no VAT conversion.
- Legacy schema gains nullable amount and cannot silently pay on unknown values.
- Source selection requests СуммаДокумента; OneC normalization preserves fractional amounts.
- Team/individual plan amounts and gross-profit semantics remain stable.
- No orders after a known load is zero; unavailable or incomplete amounts are undefined.

## Task1: Amount, calculation and application

**Files:** app/common.py, app/onec_conversions.py, app/engine.py, app/demo.py, kpi_rules.json, README.md, tests/test_sales_plan_orders.py (new), affected revenue assertion in tests/test_engine.py.
**Interfaces:** conversion_orders.amount: REAL|NULL; raw source СуммаДокумента maps to amount. Engine shares order-row and coverage helpers; calc_gross_profit(metric=revenue) returns order sum with existing plan structure.

- [x] Add RED tests for raw source amount, migration, fractional totals, source/amount unknowns, role/status/date exclusions and plan preservation.
- [x] Implement amount selection/storage, migration, shared selection helpers and order-plan calculation; keep gross-profit branch and adapt demo.
- [x] Update methodology and the obsolete revenue assertion; run complete unittest discover tests with local HTTP permission.
- [x] One fresh read-only review; regressions for material findings.
- [x] Reload confirmed dashboard, targeted refresh of conversion_orders with successful coverage recorded atomically, verify real sums/KPI and HTTP200.
