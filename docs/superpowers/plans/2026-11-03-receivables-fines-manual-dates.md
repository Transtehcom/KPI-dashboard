# Receivables, fines and manual payment deadlines Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove security-return KPIs, merge fines into receivables KPI5 and allow a manager to edit payment deadlines directly in its table.
**Architecture:** EffectiveNovember2026, tender/lead KPI6 ends and KPI5 weight becomes20%; PM KPI5 remains20% and adopts the same fines gate. Reuse manual penalty storage and checked-no-fines workflow. Payment dates are separate manual overrides over immutable1C due dates, applied only in this KPI; existing authenticated manual API saves them. Per-item penalty zero must not zero other KPI payouts.
**Tech Stack:** Existing Python/SQLite/unittest, HTML/JavaScript; no dependencies or external writes.
**Spec:** User screenshots/direct request: remove average contract-security return and application-security return, move penalties to receivables; any one fine fully zeroes KPI5; make «Срок оплаты» manually editable. User explicitly approved moving KPI6's10% into KPI5 →20%/2000 rubles at current10000 bonus.

## Global Constraints

- Apply the policy change fromNovember2026 as stated in commentary; preserve earlier monthly rules/weights and source records. Tender/lead have five current items with total100%; PM current five items remains total100%.
- All sales/tender receivables KPI5 use fines gate. Any penalty record in selected month (1C or manual, even amount0) zeroes this item only. No globalzero_all. Keep current overdue-days scale when no fines are confirmed.
- Move the existing manager fine form and «штрафов нет» check to KPI5. Unconfirmed absence of fines remains pending without payment; employees cannot write fines/dates.
- Manual due_date applies per receivable payment-stage row, preserves source1C due_date, survives sync and can be cleared to immediately restore the latest source date. Do not change cash/receivables source tables through the override.
- Recompute working-day overdue/payout immediately; keep future, unpaid carry-over and as-of payment history rules unchanged. Only payment deadline is manually editable in this table.
- Use existing manual_fields and /api/manual authorization/validation; no new permissions or employee reassignment. Current workload/previous work preserved, Git deferred.
- Preserve and finish the preceding activity-explanation feature; it is already applied and verified249tests.

## Review Focus

- A single fine must zero KPI5 even when days input is missing or its scale would otherwise pay; other items remain payable.
- Unknown fines must not silently pay despite generic mixed-known-component behavior.
- Editing/clearing a date must refer to the correct sorted payment-stage row and preserve source/history and sync survival.
- Employee/anonymous cannot save; only due_date allowed; invalid dates/unknown refs rejected atomically.
- November weight transfer and removed sixth item must not alter historical month calculations.

## Task1: Rules and backend

**Files:** kpi_rules.json, app/engine.py, app/manual.py, app/server.py, tests/test_receivables_fines.py(new), affected old weight expectations only if necessary.
**Interfaces:** params penalties_zero:true on new current receivables items; penalty_zero reason and payout_pending result flags handled per-item by Engine.compute. details.date_edit={table,field,column,rows[{ref,manual,source_value}]} for1C rows. manual_fields receivables.due_date is overlay-only; apply() skips source mutation.

- [x] RED tests for weight/history, any fine/zero amount/source/month, known-no-fines scale, pending gate, unaffected items, manual save/reset/sync/history/row sorting and authorization.
- [x] Implement shared penalty retrieval/gate, November rules, overlay due date and details metadata. Move penalty form binding in App.employee.
- [x] Focused GREEN and full suite.

## Task2: Inline date editor and verification

**Files:** web/index.html, README.md, synthetic browser fixture.
- [x] Render manager date input in existing deadline cell; show manual mark/source date/reset hint, employee read-only. Reuse /api/manual and preserve open table after recalculation.
- [x] Show penalty gate/form in KPI5 and clear item-zero/pending result; no contract/application return components in current months.
- [x] Synthetic HTTP/browser test saving and reset; full unittest/JS syntax; one fresh read-only review and necessary regressions.
- [x] Restart confirmed dashboard only, direct real-data verification with no real business penalties/date edits; no source sync needed.
