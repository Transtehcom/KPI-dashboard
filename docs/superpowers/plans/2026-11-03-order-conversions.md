# Order conversions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Count customer orders in «В закуп» by order date for sales/tender and supply conversions.
**Architecture:** Add a dedicated synchronized order dataset with one sales-manager link and deduplicated supply-owner links through explicit shared request basis. Compute conversion against monthly KP+tenders or all supply requests. Track successful dataset coverage so absent loading cannot become a false zero.
**Tech Stack:** Existing Python/SQLite/OData/unittest; no dependencies.
**Spec:** User screenshot and clarification: first formula applies to tender specialists and sales manager; user explicitly approved also correcting supply. Sales conversion = customer orders in «В закуп» in selected period / (KP+tenders) of employee. Supply = same orders / all employee's requests in ОС. Example00ЦБ-000160 counts for Anna and Yuri.

## Global Constraints

- Select order period by its Date; status must be «ТТК_ВЗакуп». Other statuses and deletion-marked orders excluded.
- Sales owner is the order's Менеджер, not a tender-root manager or a Bitrix deal owner. KP by its manager, tenders by leading manager; each document counted once.
- Supply owner comes from explicit matching ДокументОснования of nondeleted supply requests, including requests older than the order period. Repeated links for one employee count one order; one order may count once for Anna and once for Yuri.
- Denominator periods follow each KP/tender/request document date. Supply includes every request status already loaded, not only completed requests.
- Correct formula for selected months; retain existing weights and period-specific targets, including sales baseline growth rules before October2026. No Win-rate/report-column double counting in new conversion.
- Preserve sales registry, cash, receivables and other ownership rules; introduce independent conversion data.
- No external writes; read1C and update local dashboard only. Current workload checkout and prior changes preserved; Git packaging deferred.
- Include successful loading range metadata; zero numerator after successful load=0, unavailable data or zero denominator=undefined with explanation, no fictitious success.

## Review Focus

- ExampleOctober order with August request must count in October for sales and supply independently.
- Same order must not multiply when duplicate requests share a basis.
- A manager different from the tender owner must receive the sales numerator.
- Unknown or failed loading must preserve data and avoid false zero; real empty orders load must allow zero.
- Both roles, status exclusions, boundary dates and unchanged target/weight rules must be covered.

## Task1: Data and corrected calculation

**Files:** app/common.py, app/onec.py, app/onec_conversions.py (new), app/sync.py, app/engine.py, kpi_rules.json, config.example.json, live config dataset entry only, tests/test_conversions.py (new), affected old conversion tests in tests/test_engine.py, README.md.
**Interfaces:** conversion_orders(id,emp,dt,doc,client,status,role,order_key,request_docs); role sales or supply. dataset_state(dataset,date_from,date_to,updated_at) records successful conversion_orders coverage within the same transaction. fetch_conversion_orders(onec,ds,date_from,date_to) returns OneC raw normalized rows with manager.

- [x] Write failing source/calculation tests for both formulas, multiple tenders, all request statuses, Aug→Oct example, duplicate links, wrong status/manager/date, loaded empty/unknown sources and preserved targets.
- [x] Run focused unittest, confirm expected missing dataset/calculator behavior.
- [x] Implement source, schema, dataset dispatch, successful sync metadata and shared calculation. Replace old conflicting conversion tests with new rules; keep historical target tests.
- [x] Expose numerator and denominators in breakdowns; update methodology/config documentation.
- [x] Run full unittest discover tests with TEMP/TMP=data/test-tmp and local HTTP permission; fresh read-only reviewer; regressions for material findings.
- [x] Apply scoped configuration, restart confirmed dashboard only, sync and verify real October/November values and example00ЦБ-000160 under Anna/Yuri.
