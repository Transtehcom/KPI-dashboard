# ttkGPT agent

## Approved outcome

The user requested a full working agent using the existing Codex account, preserving server-enforced user and job boundaries and document anonymization. They also requested clipboard paste and drag-and-drop of images and files, alongside existing text paste.

Each chat has a persisted Codex thread and one active turn. The native App Server agent loop calls dynamic tools routed by the dashboard. Tools reuse DataTools, LiveSources and Documents. Every tool call rechecks the bound owner's access signature. Native shell, external MCP connectors, plugins and filesystem tools are disabled. Source-system operations retain their current read-only boundaries. Existing document editing produces downloadable copies.

The selected model, reasoning effort and Fast authorization remain server-validated. Employee semantic preflight continues to reject other people's data and direct questions outside their job. Own calendar and tasks retain their approved exception. Chat rename, grouping, deletion, rollback, source attribution and cancellation remain usable. A changed access signature or interrupted turn invalidates the old thread. Legacy CLI sessions bootstrap into the new agent from the remaining chat history.

## Calculation tool

A separate short-lived Python subprocess evaluates a strictly validated subset of Python over JSON inputs. It supports arithmetic, safe collection operations and comprehensions for analysis. It has no imports, attribute access, filesystem, network, process creation or credentials. Size limits and a hard execution timeout apply. The supported calculation language preserves employee isolation on the existing Windows host.

## Attachments

One attachment ingestion path serves file selection, clipboard files/images and drag-and-drop. Preserve text-only paste. Display thumbnails for ordinary images and filename/type/size for files, with removal before a request and in attachment history.

Supported image formats are PNG, JPEG and WebP. Existing document formats remain supported. Image size is capped at 10 MiB, document size at the existing 3 MiB, total storage at 30 MiB per chat. Validate actual image decoding and dimensions and discard image metadata before model transmission.

The user explicitly selected a per-image switch named «Содержит документ». For these images, local OCR and the existing privacy masker produce anonymized text. The original protected image is never sent to Codex. Protected document-like filenames also force this path. OCR failure blocks model transmission with an actionable message. Ordinary images can be sent to the vision model. File ownership is checked before either path. Document protection cannot be bypassed through the calculation tool or source tools.

## Execution and limits

The main agent turn streams safe progress to the existing message record and supports cancellation. It has a generous wall-clock deadline and bounded tool output, with no artificial ten-step outer loop. Tool errors are returned to the agent for recovery. Unsafe or unknown server requests are denied.

The implementation is applied on the current workload branch, with unrelated untracked sites preserved. Local commits accompany completed changes. GitHub push requires the user's approval.
