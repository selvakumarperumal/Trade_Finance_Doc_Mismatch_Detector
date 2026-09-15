# Trade Finance Document Mismatch Detector — backend

Cross-checks the documents presented under a letter of credit — the credit, the
invoice, the bill of lading, the rest — and reports the discrepancies a bank ops
examiner would raise under UCP 600, with a verdict of `clean`, `needs_review` or
`blocked`.

Documents arrive either as text you extracted upstream, or as scans that Amazon
Textract reads inside the pipeline. Multi-page PDFs are split a page at a time and
read concurrently, because Textract's synchronous API takes one page per call.

## Run it

```bash
uv sync
export GEMINI_API_KEY=...              # the models
export AWS_REGION=ap-south-1           # OCR; omit to run text-only
uv run uvicorn main:app --reload
```

Or from the repository root, which is the same thing in a container:

```bash
docker compose up --build
```

Then open http://localhost:8000/docs.

## The API

Analysing a presentation takes minutes, so **submitting and collecting are separate**.
A submission answers `202` with a case id and the run continues in the background.

| | |
|---|---|
| `POST /v1/cases/uploads` | Submit a presentation as PDFs or images → `202` |
| `POST /v1/cases` | Submit a presentation as already-extracted text → `202` |
| `Socket.IO /socket.io` | Watch it run — emit `subscribe` with the case id |
| `GET /v1/cases/{id}` | Poll it instead |
| `DELETE /v1/cases/{id}` | Abandon a case and drop what it held |
| `GET /health` | Liveness, and whether OCR is available |

**Every one of them answers with the same `CaseRecord`**, so there is one shape to
handle:

```jsonc
{
  "case_id": "case-a1b2c3d4e5f6",
  "status": "queued",      // -> running -> succeeded | failed | cancelled
  "document_count": 3,
  "events": [...],         // the audit trail so far
  "result": null,          // the report, once status is "succeeded"
  "error": null,           // why, once status is "failed"
  "error_code": null
}
```

```bash
# 1. submit the files
curl -X POST localhost:8000/v1/cases/uploads \
  -F files=@credit.pdf -F files=@invoice.pdf -F files=@bol.pdf \
  -F presented_on=2026-03-02
# -> 202 {"case_id":"case-a1b2c3d4e5f6","status":"queued", ...}

# 2. poll it
curl localhost:8000/v1/cases/case-a1b2c3d4e5f6
```

Or watch it with a **Socket.IO** client — note this is Socket.IO, not a bare WebSocket,
so `websocat` will not do:

```js
import { io } from 'socket.io-client';

const socket = io('http://localhost:8000');           // path defaults to /socket.io
socket.emit('subscribe', { case_id: 'case-a1b2c3d4e5f6' });

socket.on('case',  (record) => render(record));       // the whole CaseRecord
socket.on('done',  ()       => socket.disconnect());  // terminal status reached
socket.on('error', (e)      => console.error(e.code, e.detail));
```

| Event | Direction | Payload |
|---|---|---|
| `subscribe` | client → server | `{ case_id }` — start streaming that case |
| `unsubscribe` | client → server | `{ case_id }` — stop; disconnecting also stops it |
| `case` | server → client | the whole `CaseRecord`, on subscribe and after every change |
| `done` | server → client | `{ case_id }` — terminal status, no more `case` events |
| `error` | server → client | `{ code, detail }` — the same codes the HTTP layer uses |

Every `case` event carries the **whole record**, not a delta. So a client renders the
latest one and is correct whether it subscribed before the first document was read or
after the last — no message types to switch on, no events to accumulate, no cursor to
resume from.

Events arrive in completion order rather than document order, because the documents are
read, classified and extracted in parallel. Key your UI on `document_id`.

### Errors

Errors come back as `{"code": ..., "detail": ...}`. Switch on `code`, not on the text.

While a request is still open, the code arrives with an HTTP status:

| `code` | | |
|---|---|---|
| `invalid_case` | 422 | Malformed, or too many documents |
| `unsupported_media_type` | 415 | Not a PDF, PNG, JPEG or TIFF — decided by the bytes, not the filename |
| `upload_too_large` | 413 | Past the per-file or per-case size limit |
| `case_not_found` | 404 | Unknown id, or it aged out of the retention window |
| `case_exists` | 409 | That case id is taken; `DELETE` it to reuse it |
| `server_busy` | 503 | At a concurrency ceiling; carries `Retry-After` |

A case that fails *after* it was accepted has no request left to answer, so the same
vocabulary is written to `error_code` on the record and read back from
`GET /v1/cases/{id}`:

| `error_code` | |
|---|---|
| `invalid_case` | The presentation was malformed or too big |
| `model_unavailable` | The model provider failed |
| `usage_limit_exceeded` | The case ran past its configured model budget |
| `server_busy` | The model concurrency limit was hit |
| `cancelled` | `DELETE`d, or still running when the process shut down |
| `internal_error` | Anything else — a case always reaches a terminal state |

## Configuration

Everything in [`Detector/core/config.py`](Detector/core/config.py) is overridable with a
`DETECTOR_` prefix — `DETECTOR_RECONCILIATION_MODEL`, `DETECTOR_MAX_CONCURRENT_CASES`,
`DETECTOR_MAX_PARALLEL_OCR_CALLS`, `DETECTOR_OCR_ENABLED=false`, and the rest. The system
prompts live in [`Config/prompts.yaml`](Config/prompts.yaml) so they can be edited
without a deploy.

The stages run on different models on purpose: classification and extraction are
mechanical, so they run on Gemini Flash, while reconciliation is the actual compliance
judgement and runs on Pro.

Without AWS configured the service still starts; `/health` reports `ocr_available:
false` and uploads are refused, while presentations that carry their own text are
analysed as normal.

Cases are held in memory, so this runs as a single process. `CaseStore` in
[`Detector/services/store.py`](Detector/services/store.py) is the seam to swap in a
shared store for a multi-process deployment.

There is no telemetry layer. The service logs through the standard library, and only
says two things: Textract being unavailable at startup, and a case task failing
unexpectedly. Both land on stderr alongside uvicorn's own output. Anything that wants
traces can instrument the ASGI app from outside.

## Tests

```bash
uv run pytest
```

No API key and no AWS account needed: the agents run on pydantic-ai's `test` model and
Textract is stubbed, so the suite exercises the wiring rather than the models' judgement.
One test in [`tests/test_events.py`](tests/test_events.py) is the exception — it starts a
real uvicorn server and connects a real Socket.IO client, because the mount path and the
handler wiring are what a stub cannot check.

## Reading the code

| Doc | What it's for |
|---|---|
| [`Detector/ARCHITECTURE.md`](Detector/ARCHITECTURE.md) | **Start here** — how the app works, upload to result |
| [`WALKTHROUGH.md`](WALKTHROUGH.md) | One real case followed end to end |
| [`mini_detector.ipynb`](mini_detector.ipynb) | A runnable miniature of the whole pipeline, built from scratch |

Each package has its own README for the layer it covers:

| | |
|---|---|
| [`Detector/core/`](Detector/core/README.md) | Every tunable, in one `Settings` class |
| [`Detector/models/`](Detector/models/README.md) | Documents, extractions, findings, job records |
| [`Detector/prompts/`](Detector/prompts/README.md) | Loads and validates the ten system prompts |
| [`Detector/services/`](Detector/services/README.md) | The engine: agents, the graph, OCR, the runner, the store |
| [`Detector/api/`](Detector/api/README.md) | The HTTP and Socket.IO layer |
