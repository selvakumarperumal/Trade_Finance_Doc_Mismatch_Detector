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

Then open http://localhost:8000/docs.

## The API

Analysing a presentation takes minutes, so **submitting and collecting are separate**.
A submission answers `202` with a case id and the run continues in the background.

| | |
|---|---|
| `POST /v1/cases/uploads` | Submit a presentation as PDFs or images → `202` |
| `POST /v1/cases` | Submit a presentation as already-extracted text → `202` |
| `WS /v1/cases/{id}/stream` | Watch it run |
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

# 2. watch it, or poll it
websocat ws://localhost:8000/v1/cases/case-a1b2c3d4e5f6/stream
curl localhost:8000/v1/cases/case-a1b2c3d4e5f6
```

The websocket sends the **whole record on every change**, not a delta. So a client
renders the latest message and is correct whether it connected before the first document
was read or after the last — no message types to switch on, no events to accumulate, no
cursor to resume from. It stops when `status` is terminal.

Events arrive in completion order rather than document order, because the documents are
read, classified and extracted in parallel. Key your UI on `document_id`.

### Errors

Errors come back as `{"code": ..., "detail": ...}`. Switch on `code`, not on the text. A
case that fails *after* it was accepted records the same codes on its record, so there
is one vocabulary either way.

| `code` | | |
|---|---|---|
| `invalid_case` | 422 | Malformed, or too many documents |
| `unsupported_media_type` | 415 | Not a PDF, PNG, JPEG or TIFF — decided by the bytes, not the filename |
| `upload_too_large` | 413 | Past the per-file or per-case size limit |
| `case_not_found` | 404 | Unknown id, or it aged out of the retention window |
| `case_exists` | 409 | That case id is taken; `DELETE` it to reuse it |
| `model_unavailable` | 502 | The model provider failed (recorded on the case) |
| `server_busy` | 503 | At a concurrency ceiling; carries `Retry-After` |

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

## Tests

```bash
uv run pytest
```

No API key and no AWS account needed: the agents run on pydantic-ai's `test` model and
Textract is stubbed, so the suite exercises the wiring rather than the models' judgement.

## Reading the code

| Doc | What it's for |
|---|---|
| [`Detector/ARCHITECTURE.md`](Detector/ARCHITECTURE.md) | **Start here** — how the app works, upload to result |
| [`WALKTHROUGH.md`](WALKTHROUGH.md) | One real case followed end to end |
| [`mini_detector.ipynb`](mini_detector.ipynb) | A runnable miniature of the whole pipeline, built from scratch |
