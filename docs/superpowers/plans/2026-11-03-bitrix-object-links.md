# Bitrix object links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, inline. One final read-only review.

**Goal:** Every displayed object imported from Bitrix24 opens its original object from its name/title.

**Architecture:** Share validated portal/entity URL builders; preserve explicit client references during normalization and in the current database. Add link metadata to existing API payloads and detail matrices, and render anchors through the existing escaped link helper. Keep dashboard drilldowns and editors working.

**Tech Stack:** Python, SQLite migrations, unittest, existing HTML/JavaScript.

**Spec:** User request: «на любой объект, который взят из Битрикса, можно было перейти в Битрикс, сделать кликабельными».

## Global Constraints

- Current workload checkout and accumulated edits preserved; Git packaging remains deferred.
- CRM read-only. No production session extraction, business edits, or new external permissions.
- Explicit source IDs only; no links inferred from names or 1C UUIDs. Missing identities stay plain text.
- URLs use configured CRM origin without webhook path/credentials; http(s), positive integer IDs, known entity types only.
- Open external links in a new tab with noopener noreferrer; escape labels/attributes; preserve internal navigation, editors, authorization, KPI calculations.
- Cover KPI details/audits, current/fact funnels, call records/listen log, workload tasks/profiles, sales register, calendar events/tasks/activities.

## Review Focus

- Duplicate labels and overlapping IDs across entity types must open the correct card.
- Current and historical/fact records retain identities after sorting, filtering, sync and snapshots.
- Missing/malformed IDs and unconfigured CRM must not generate broken/unsafe links or leak webhook credentials.
- Links must not trigger surrounding internal row actions; keyboard access and existing controls must continue to work.
- Permission-filtered APIs remain filtered; 1C/journal rows without explicit Bitrix references remain unlinked.

## Task 1: Shared identity and API metadata

- [x] RED tests for entity URL validation, records/fact rows, KPI details, audit client/title URLs, calls/log and source normalization.
- [x] Add app/crm_links.py; optional client_ref columns and normalization; metadata for exact object/client identities.
- [x] Use shared helpers in engine, funnel, audit, calls/calendar/sales; preserve source IDs and existing rules.
- [x] Focused tests GREEN.

## Task 2: Rendering and deployment

- [x] Render external links in all covered views without replacing dashboard drilldowns/editors.
- [x] Tests, JS syntax, whitespace, synthetic browser checks and one fresh read-only review; fix material findings.
- [x] Backfill missing reference metadata by read-only CRM queries if required; restart only verified dashboard listener.
- [x] Verify live links and unchanged KPI amounts through direct read-only checks; close synthetic resources.
