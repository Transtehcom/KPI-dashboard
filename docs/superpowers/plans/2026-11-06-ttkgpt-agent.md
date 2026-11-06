# ttkGPT agent implementation plan

The user approved the architecture described in chat and requested implementation.

## Goal

Provide native Codex agent turns, scoped business tools, constrained Python calculations, and clipboard/drop attachment ingestion with protected image OCR.

## Architecture

Reuse the dashboard's access, source, privacy, document and session components. Add a bounded stdio JSON-RPC App Server transport and a server-owned tool router. Source credentials remain within source adapters. Preserve the legacy CLI path only as an explicit recovery setting and regression-test fixture.

## Specification

See docs/superpowers/specs/2026-11-06-ttkgpt-agent.md.

## Constraints and review focus

- Bind thread, owner and role signature on every tool call, including after permission changes.
- Deny inherited tools, connectors and arbitrary filesystem execution.
- Protect document images before model transmission and preserve ordinary image vision support.
- Keep message cancellation, rollback and attachment removal coherent during concurrency.
- Preserve model/Fast permissions and existing source attribution and document editing.

## Tasks

- [x] Add app/gpt_appserver.py and tests/test_gpt_appserver.py. Verify installed protocol, initialization, thread start/resume, dynamic tool requests, streamed completion, cancellation, timeout and denial of other requests. Probe a real isolated thread.
- [x] Add app/gpt_calculate.py and tests/test_gpt_calculate.py. Validate safe Python AST and JSON sizes, execute with timeout in a separate process and reject escaping through imports, attributes, unknown calls and unbounded output.
- [x] Extend app/gpt_documents.py, app/server.py and web/index.html with image/file ingestion, clipboard/drop, previews and protected-image mode. Add local OCR support and tests for ownership, data limits and protection failures.
- [x] Integrate app/gpt_agent.py with GPTAssistant and Sessions. Keep semantic preflight, native tool callbacks, scoped tools, image reading, source links and access checks. Make the native backend the production default and explicitly select the legacy backend in existing legacy-path tests.
- [x] Run security, UI and full regression suites. Verify real admin counts, employee denial, ordinary image reading and protected-image masking. Review and resolve findings, commit locally and apply while no active requests are running.

## Verified decisions

- App Server requires the installed account model cache. Public capability metadata is copied per process with native file tools removed. Account identity and credentials stay in Codex.
- Native code-mode orchestration is enabled for dashboard dynamic tools. Inherited MCP, apps, plugins, shell and file modification capabilities are disabled, with effective configuration verified before thread creation.
- The native turn deadline is 15 minutes. Calculation workers have a five-second deadline and 256 MiB memory cap.
- New image inputs are attached once to the native turn after staff preflight. Protected images use whole-OCR masking and text-only input.
- Permission changes and cancellation close the document mutation guard before a delayed edit can save.
- Real-provider checks passed for 1C author percentage, ordinary image reading, protected image amount reading and staff job-boundary refusal.

Final verification completed with 542 passing tests. The dashboard restarted with HTTP 200 after checking there were no active processing jobs.
