# Logistics KPI draft Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add the approved draft logistics KPI profile, available for later employee assignment without premium calculation.
**Architecture:** Keep the six source items and weights in kpi_rules.json; mark the profile as draft. Engine bypasses metric calculation and payout for draft profiles. Existing UI displays draft rules and setup issues instead of a failed performance score or a fabricated salary/bonus.
**Tech Stack:** Existing Python/unittest and HTML/JavaScript; no dependencies or schema changes.
**Spec:** User request plus explicit approval «Добавить черновик с пометками». Source: C:/Users/BartovKK/Downloads/KPI Специалиста отдела логистики.docx.

## Global Constraints

- Six weights remain 20%, 20%, 10%, 15%, 10%, 10% (85%); do not normalize or invent a seventh item.
- Preserve source formulas, exceptions and requirements. Flag conflicts for budget and damage metrics, missing timing/claim/discipline parameters and compensation.
- No employee assignment or changes to existing employees, compensation or KPI profiles.
- Salary and bonus are unconfigured, not guessed. Even with pay_when_no_data enabled, a draft cannot pay or count as a failure in team totals.
- Use the current workload checkout/running dashboard, preserving accumulated prior changes; Git packaging remains deferred as in the existing session.
- Monthly preview is provisional pending clarification; no live logistics calculation or new sync is needed.

## Review Focus

- Assigning a draft in the future must not crash calculations or create a payment.
- Global pay_when_no_data and any synthetic positive bonus cannot bypass the draft lock.
- Profile rules and issues must reach both methodology and employee selection through existing APIs.
- Team financial/performance totals exclude draft profiles.
- The original 85% and contradictory formulas stay visible, without silent repairs.

## Task 1: Profile, draft calculation lock and presentation

**Files:** kpi_rules.json; app/engine.py; app/server.py; app/employees.py; web/index.html; tests/test_logistics_kpi.py; README.md.
**Interfaces:** positions.logistics has draft=true, setup_issues, nullable salary/kpi_bonus, six standard item dictionaries. Engine.compute preserves numeric result totals and adds draft flag; draft item status is draft, pct=None, payout=0. Bootstrap and employee position listing expose draft metadata.

- [x] Add tests for source weights/formulas, selectable unassigned profile, pending compensation, draft payout lock, exclusion from team totals, and metadata propagation.
- [x] Run python -m unittest tests.test_logistics_kpi; expect missing profile/draft handling failures.
- [x] Add the draft profile and computation guard; expose status and issues through existing APIs and UI.
- [x] Repeat focused tests; then python -m unittest discover tests with TEMP/TMP=data/test-tmp and HTTP-test permission. Expect all green.
- [x] One fresh read-only review; fix material findings with a regression test.
- [x] Restart only confirmed dashboard process; verify HTTP 200 and draft listing/bootstrap on real configuration without assigning anyone. No data sync needed for this configuration-only addition.
