# Operations panels implementation plan

> For agentic workers use superpowers subagent-driven-development for bounded implementation tasks and independent final review.

## Goal

Deploy the approved sales day and supply request panels with prospective history, explicit identity links and traceable price guidance.

## Architecture

New operational snapshot tables coexist with existing KPI tables. A read-only 1C importer returns normalized entities and events, historical capture stores changed payloads, report builders calculate bounded JSON views, existing Metrics screens render them after deployment bootstrap enables the feature. Existing KPI formulas remain owned by their current modules.

## Stack

Python standard library, SQLite, existing OData and Bitrix clients, existing vanilla JavaScript dashboard.

## Spec

docs/superpowers/specs/2026-11-09-operations-panels.md

## Global constraints

- Source reads only, local source credentials remain private.
- Canonical entity ID is source plus document kind plus source GUID. Display numbers never merge documents.
- History begins at deployment, current source freshness limit is 90 minutes, historical price validity is 30 calendar days.
- Zero time means end of day in Asia/Yekaterinburg, retain seconds and raw source values.
- Commit only task files, preserve unrelated sites directory, offer GitHub upload after local commits.
- Run regression tests before deployment, back up live SQLite before schema migration and initial capture.

## Interfaces

app/onec_operations.py exports fetch(onec, config, since=None), returning entities and events plus source errors. Entity keys include id, ref, kind, number, date, title, client, owner, owner_name, sales_owner, status, due, closed_at, amount, vat_included, currency, links, url, items and events. Kinds are tender, kp, order, supply, transport. Links contain canonical IDs. Items retain source row identity and offers. Events contain id, entity_id, at, author, author_name, kind, status, description, url and source.

app/operations.py owns ensure_schema(con), capture(con, source, entities, events, observed_at=None), snapshot(con, source, cutoff=None), freshness(con, cutoff=None), baseline_started(con), local_datetime(value), deadline(value), capture_bitrix(con, config, observed_at=None). Snapshot returns entity payloads effective by observation time. Sources are onec and bitrix. Calls and tasks are normalized into explicit entities and events with stored author identity. Capture is transactional under the caller's existing db_lock and does not commit independently.

app/operations_views.py exports build(con, config, kind, date_from, date_to, emp='Все', channel='Все', now=None), price_history(con, item, cutoff, quantity=None), set_item_scope(con, request_id, item_id, included, reason, author), approve_price(con, request_id, item_id, supplier_id, author). build returns kind, date_from, date_to, cutoff, history_start, coverage, employees, tiles, records, queue, events, results, people and warnings. All visible documents contain explicit links. Tiles contain key, title, value and record_ids or event_ids. A sales record represents a connected opportunity. Supply records represent unique supply documents. Embedded documents retain source identity, timestamps, amounts, items and selected supplier. Reports always expose source freshness and history limits.

## Review focus

- An incomplete source fetch preserves the previous successful snapshot and freshness.
- Snapshot changes, repeated imports and source events remain idempotent, current fields cannot leak into past reports.
- Staff tools cannot use the new report to read another employee or job direction. Source URL schemes and IDs are validated.
- Quote baselines retain unknown dates, conflicting units, zero prices and stale conditions as explicit excluded reference evidence.
- Reopened requests and shared transport links preserve event history while unique request/opportunity totals avoid duplicates.

## Task 1 — history and source capture

- [ ] Add focused failing tests for midnight deadlines, versioned snapshots, source failure and idempotence.
- [ ] Implement app/operations.py and app/onec_operations.py with read-only source mapping tested against available OData metadata.
- [ ] Capture document references, source event authors, full timestamps, supplier item offers and separate transport deadlines.
- [ ] Run source/history tests and commit.

## Task 2 — computed reports and price guidance

- [ ] Add failing tests for explicit connected documents, historical cutoff, closing/reopening, known-denominator rates and supplier median.
- [ ] Implement app/operations_views.py, audit scope exclusions and quantity comparability approvals.
- [ ] Verify sales event attribution, request-only supply counts and sample date arithmetic.
- [ ] Run report tests and commit.

## Task 3 — API, UI and ttkGPT

- [ ] Add manager-authorized operations GET and admin-only audited POST endpoints with date and identity validation.
- [ ] Add feature-gated sales day and supply views, preserving existing registry editors and navigation.
- [ ] Render filters, source freshness, linked drilldowns, request items, price history, scope editing and daily event feeds.
- [ ] Expose computed reports to ttkGPT through existing scoped data tools, using access checks before retrieval.
- [ ] Run server, UI and access tests and commit.

## Task 4 — integration and production deployment

- [ ] Integrate independent source loading and capture into hourly sync, preserving atomic snapshot writes and existing KPI data.
- [ ] Run the full existing suite and new regression tests, independent code review and UI syntax checks.
- [ ] Back up live SQLite, seed source snapshots, verify source totals and control examples, then restart only the verified supervised dashboard.
- [ ] Verify HTTP readiness, live API behavior and history boundary. Commit fixes and offer GitHub upload.
