# Call processing jobs implementation plan

Goal: Allow administrators to create independent concurrent call-processing jobs, cancel them and optionally select a one-time local launch date/time.

Architecture: Persist jobs in SQLite. A bounded thread dispatcher starts due jobs (default 3). Existing call parsing stays in call_analysis; app/call_jobs.py owns scheduling, progress, per-call exclusion and cancellation. Transcription remains serialized through call_records._running. No model runs under the database lock.

Constraints: Employee permissions remain unchanged. Jobs snapshot selected call IDs and model/effort at creation. Cancellation is cooperative: current call can finish, subsequent calls do not start. Queued jobs survive restarts; interrupted running jobs are marked interrupted rather than silently replayed. No recurring schedules. Browser datetime-local uses server local time, explicitly labeled. No external AI calls in tests.

- [x] Write failing tests for parallel workers, bounded concurrency, cancellation, future dispatch, restart recovery, duplicate-call exclusion and schedule validation.
- [x] Add schema and implement dispatcher and worker. Preserve existing transcript/result rules; do not skip untranscribed calls because another job is transcribing.
- [x] Add manager-only status/create/cancel endpoints and start dispatcher after HTTP bind.
- [x] Add optional launch-time control, persistent task list, progress and cancel buttons to Calls page; polling does not replace selection or editable scheduling fields.
- [x] Verify queue, existing parsing, permissions and frontend syntax, then run full suite. Restart service only when no active processing would be interrupted. Commit locally, offer GitHub push.

Review focus: cancellation between calls; same call in two jobs; background transcription lock; overdue queued jobs after restart; invalid dates and employee access; job failure must release worker capacity.

Validation: 362 tests passed; inline JavaScript syntax and config JSON checked. Independent review finding (active tasks disappearing beyond history limit) fixed and covered by regression. Working tree scope excludes sites/.
