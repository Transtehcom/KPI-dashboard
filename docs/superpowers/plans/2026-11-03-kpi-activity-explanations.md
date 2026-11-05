# KPI activity explanations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Clicking calls, meetings or KP in an employee KPI reveals every loaded record of the month, per-condition checks, contribution and actionable explanations.
**Architecture:** Add slim audit keys to supported components. Load detailed audits only when expanded through an authenticated read-only API; use existing Engine selection/matching helpers so explanations equal the actual fact. Inline expandable lists include search/filters, accessible condition badges and record-level explanations.
**Tech Stack:** Existing Python/unittest and vanilla HTML/CSS/JavaScript, no dependencies or external writes.
**Spec:** User screenshot and direct request: click «Результативные звонки…» and see all calls with «От2мин», «Не секретарь», «Есть информация об ЛПР», «Зафиксирован следующий шаг»; equivalent detail for meetings/KP, with exact employee guidance for getting records counted.

## Global Constraints

- Preserve all KPI formulas, norms, payments, secretary overrides, deduplication and historical month policies; disclose requirements per actual role/item/month.
- Calls: all loaded calls for employee/month, including short/incoming/excluded/duplicates. Check direction, configured duration, effective secretary mark/source, LPR and next step only where required. Deduplicate using the same accepted rows, so an earlier invalid call cannot block a later valid one.
- Explain dated next-step planning, same responsible and explicit CRM card, inclusive24h creation window and due>=call. For expired windows, never suggest that creating a new task now retroactively qualifies the call or changing timestamps.
- Meetings: all loaded statuses; show completion, protocol, summary and close-time conditions when required. Protocol currently means attached file, summary>=20chars; closure timestamp is LAST_UPDATED proxy, and missing timestamp does not currently exclude a completed meeting. Disclose uncertainty accurately.
- KP: current1C+letters union, dedup by existing company/date matcher and match window; do not require both sources when rule uses OR. Older confirmed/outside-tender/journal/min-count policies remain truthful. Do not invent per-document links for historical aggregate min-count rule; show unresolved individual allocation explicitly.
- Employee may audit only own KPI, manager may audit authorized dashboard employees; same authentication and month validation as existing KPI API. No audio URLs/tokens/private configuration in responses.
- No arbitrary row limit or unannounced truncation; search/all/counted/excluded filters. Empty/error/retry states and keyboard access.
- Current workload checkout/accumulated work preserved, Git packaging deferred. No CRM/1C sync needed: audits use the existing local dataset; no false claim of inspecting unexported source records.

## Review Focus

- Sum of counted audit records equals actual component fact, with explicit exception for unresolved historic aggregate allocation.
- Wrong employee/invalid item/kind/month cannot expose data.
- Same-day duplicates and later valid calls share exact production selection.
- Role-specific optional conditions, meeting closure uncertainty and KP source OR/dedup must not give false remediation.
- Collapsed components do not fetch details; stale responses cannot replace a newly selected employee/month; escaped text and small-screen layout.

## Task1: Backend audit model and API

**Files:** app/kpi_activity_audit.py(new), app/engine.py, app/server.py, tests/test_kpi_activity_audit.py(new), HTTP tests on synthetic in-memory fixture.
**Interfaces:** comp.audit string calls/meetings/kp. Engine.activity_audit(login,month,code,kind) resolves active supported component and returns {kind,title,fact,target,rules,records,summary}; records contain id,when,title,client,source,checks[],counted(bool|None),duplicate,actions[]. Checks include key,label,state(pass/fail/unknown/na),detail,action. Lazy GET /api/kpi-records?login&month&code&kind enforces existing employee access.

- [ ] RED tests: all call outcomes/overrides/dedup and role policies; planned/complete meetings/protocol/summary/late/unknown close; current KP union/matching and legacy policies; bad item/kind; real HTTP self/manager/other/unauthenticated validation.
- [ ] Extract shared row/matching/meeting selectors, add metadata and implement audit/API. Keep component fact unchanged.
- [ ] GREEN focused tests; compare every supported component audit with calculator fixtures.

## Task2: Inline explanation interface and verification

**Files:** web/index.html; README.md; UI smoke fixture if needed in task scratch.
**Interfaces:** supported component audit key consumed by native details loader; lazy endpoint from Task1.

- [ ] Implement clickable labels, inline rules and condition badges, counted/duplicate/unknown outcomes, per-record guidance, search/filters, empty/error/retry and keyboard support. Existing offline preview keeps labels static because no audit data is embedded.
- [ ] Verify JS syntax and interactive loading/filtering using a synthetic local app/session; do not extract real manager sessions.
- [ ] Full unittest discover tests with TEMP/TMP=data/test-tmp and HTTP permission; one fresh read-only final reviewer, regressions for material findings.
- [ ] Restart confirmed dashboard only; verify authenticated behavior through test fixture and real local direct audit/fact parity; no source synchronization required.
