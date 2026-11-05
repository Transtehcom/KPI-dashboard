# Objective feedback rechecks

Goal: Admin can reprocess a rated call with frozen employee comment/selected criteria and see model feedback without automatically accepting the complaint.

Architecture: Two calls: blind fresh analysis (no rating, prior result or employee comment), then evidence-based review against numbered masked transcript. Review may change only selected fields (or overall comment) and only with a verified verbatim quote and source sentence. Transcript accuracy and acoustic tone cannot be established from text; those complaints are insufficient_data and cannot change results. Local backend supplies timestamps and rejects invented quotes. No automatic resolution of employee feedback.

Files: app/call_recheck.py (schema, prompts, evidence gates, history, worker); app/call_jobs.py (queue linkage); app/call_analysis.py (optional output schema); app/server.py (manager-only endpoint, revision validation, per-author history); web/index.html (admin button, evidence display); tests/test_call_recheck.py and existing API/frontend tests.

- [x] Add failing tests: blind prompt isolation, unsupported complaint rejection, verified correction, forged quote rejection, preserved history, pending feedback, stale revisions and employee restrictions.
- [x] Implement persistent snapshots at enqueue and atomic final result after both passes. Failures retain previous result. Link history to processing job; survive queued restart and reflect cancellation/interruption.
- [x] Add admin resubmit button using current model/effort and optional one-time schedule. Show model verdict/explanation/verified quotes with timestamps to permitted viewers.
- [x] Verify targeted and full tests, review security/concurrency, deploy without interrupting jobs, local commit and offer push.

Constraints: source systems read-only; never send plaintext personal names or ratings as model authority; do not resolve employee rating automatically; no live AI calls in tests. Work in existing user-authorized checkout, exclude sites/.

Validation: 382 tests passed. Inline JavaScript syntax checked. Independent review retention finding fixed with completed/in-flight purge regressions. Service restarted successfully (HTTP 200, new controls/history schema present). No live AI calls during verification.
