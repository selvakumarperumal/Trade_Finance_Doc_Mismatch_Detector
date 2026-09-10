# How the detector works, end to end

From a person selecting a pile of scanned documents and clicking submit, to a report an
examiner can act on.

**The problem.** A beneficiary ships goods and presents documents to a bank to get paid —
the letter of credit itself, the commercial invoice, the bill of lading, a packing list,
an insurance certificate. The bank must check them against each other and against
UCP 600. If the invoice says `ACME TRADING LTD` and the credit says
`ACME TRADING LIMITED`, or the goods shipped a day after the latest shipment date, the
bank refuses and the beneficiary does not get paid. Doing this by hand takes an examiner
twenty minutes a presentation and the mistakes are expensive.

**What this does.** Reads every presented document into typed fields, cross-checks them
the way an examiner would, and returns the discrepancies with a verdict of `clean`,
`needs_review` or `blocked`.

---

## The whole thing in one picture

```mermaid
flowchart TD
    U["Operator selects<br/>credit.pdf, invoice.pdf, bol.pdf<br/>and clicks Submit"]
    U -->|"POST /v1/cases/uploads"| API

    subgraph HTTP["api/ — thin"]
        API["validate: count, size, format<br/>refuse before spending anything"]
        API --> ACC["202 CaseRecord<br/>status queued"]
    end

    ACC -.->|"the browser now watches"| WS["WS /v1/cases/{id}/stream"]
    API --> RUN

    subgraph BG["services/ — background, minutes long"]
        RUN["runner: wait for a slot, then run"]
        RUN --> ING["ingest — validate the presentation"]
        ING --> FAN{{"fan out, one branch per document"}}
        FAN --> B1["ocr → classify → extract"]
        FAN --> B2["ocr → classify → extract"]
        FAN --> B3["ocr → classify → extract"]
        B1 & B2 & B3 --> JOIN{{"join — wait for all of them"}}
        JOIN --> REC["reconcile — cross-check under UCP 600"]
    end

    REC --> STORE["store: status succeeded, result set"]
    STORE -->|"pushes a whole CaseRecord"| WS
    WS --> UI["Report: 2 critical, 1 warning → blocked"]
```

Three ideas carry the whole design:

1. **Submitting and collecting are separate.** A submission answers in milliseconds with
   a case id; the analysis continues in the background.
2. **Documents are processed in parallel.** A twelve-document case costs roughly the
   latency of its slowest single document, not twelve in a row.
3. **One thing failing never loses the rest.** A bad scan becomes a finding, not a 500.

---

## 1. Upload — refusing early

The browser sends one multipart request with N files. Each check runs at the first moment
it becomes knowable, so nothing is spent on a submission that cannot work.

```mermaid
flowchart LR
    F["N files"] --> C1{"count within<br/>max_documents_per_case?"}
    C1 -->|no| X1["422 — before a byte is read"]
    C1 -->|yes| C2["read in 1 MB chunks"]
    C2 --> C3{"within the per-file<br/>and per-case budgets?"}
    C3 -->|no| X2["413 — part-way through"]
    C3 -->|yes| C4{"do the leading bytes match<br/>PDF, PNG, JPEG or TIFF?"}
    C4 -->|no| X3["415"]
    C4 -->|yes| OK["accepted"]
```

The size check happens *while* the bytes arrive, not after:

```python
    ceiling = min(settings.max_document_bytes, max(budget, 0))

    chunks: list[bytes] = []
    size = 0
    while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
        size += len(chunk)
        if size > ceiling:
            raise UploadRejected(...)  # 413 upload_too_large
        chunks.append(chunk)
```

And the format comes from the **file's leading bytes**, not its `Content-Type` header —
that is whatever the operating system guessed from the extension, and is routinely
`application/octet-stream`:

```python
_MAGIC: Final[tuple[tuple[bytes, str], ...]] = (
    (b'%PDF-', PDF_MEDIA_TYPE),
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'II*\x00', 'image/tiff'),
    (b'MM\x00*', 'image/tiff'),
)


def sniff_media_type(content: bytes) -> str | None:
    """Identify a document from its leading bytes, or `None` if it is not one we read."""
    return next((media_type for magic, media_type in _MAGIC if content.startswith(magic)), None)
```

So a correctly-formed PDF the browser mislabelled is accepted, and a renamed executable
is refused before it costs a round trip to AWS.

Nothing is written to disk or to a bucket — `RawDocument.content` is excluded from
serialisation, so the bytes cannot reach a response or a log line:

```python
    content: bytes | None = Field(default=None, repr=False, exclude=True)
```

## 2. Accepted — the 202

```python
    return await runner.submit(
        CaseInput(
            case_id=case_id or new_case_id(),
            presented_on=presented_on,
            documents=documents,
        )
    )
```

`submit` registers the case and returns immediately, before any work happens:

```python
    async def submit(self, case: CaseInput) -> CaseRecord:
        """Accept a case and return its record straight away."""
        if self._closing:
            raise TooBusy('the service is shutting down and is not accepting cases')
        if self._waiting >= self.max_queued:
            raise TooBusy(
                f'{self._waiting} case(s) already waiting, at the limit of {self.max_queued}'
            )

        record = await self.store.create(case.case_id, document_count=len(case.documents))
        self._waiting += 1

        task = asyncio.create_task(self._run(case), name=f'case:{case.case_id}')
        self._tasks.add(task)
        task.add_done_callback(self._retire)
        return record
```

```json
{ "case_id": "case-a1b2c3d4e5f6", "status": "queued", "document_count": 3,
  "events": [], "result": null, "error": null, "error_code": null }
```

That same `CaseRecord` shape is what every other endpoint returns — the polling response
and every websocket message. There is nothing else to learn.

If too many submissions are already waiting, the service answers `503` with a
`Retry-After` instead. Refusing at the door beats handing back a case id nobody will look
at for twenty minutes.

## 3. Fanning out

`ingest` validates and hands the documents to a `map` fork:

```python
builder.add(
    builder.edge_from(builder.start_node).to(ingest),
    builder.edge_from(ingest)
    .label("per document")
    .map(fork_id=FAN_OUT_ID, downstream_join_id=COLLECT_ID)
    .to(ocr),
    builder.edge_from(ocr).to(classify),
    builder.edge_from(classify).to(_routing_decision()),
    builder.edge_from(*EXTRACTION_STEPS.values(), skip_unclassified).to(collect),
    builder.edge_from(collect).label("all documents").to(reconcile),
    builder.edge_from(reconcile).to(builder.end_node),
)
```

Every document now runs its own `ocr → classify → extract` branch concurrently.

## 4. Reading — OCR, one page at a time

Textract's synchronous read takes **one page** per call, but a letter of credit runs to
three or four:

```mermaid
flowchart LR
    PDF["credit.pdf — 4 pages"] --> SPLIT["split into 4 single-page PDFs"]
    SPLIT --> P1[page 1] & P2[page 2] & P3[page 3] & P4[page 4]
    P1 & P2 & P3 & P4 --> SEM{"semaphore — max_parallel_ocr_calls"}
    SEM --> TXT["Textract"]
    TXT --> JOIN["rejoined in page order<br/>one continuous document"]
```

```python
        media_type = sniff_media_type(content)
        if media_type is None:
            raise OcrError(...)  # not a format Textract reads
        if media_type == PDF_MEDIA_TYPE:
            pages = await asyncio.to_thread(split_pdf_pages, content, max_pages=self.max_pages)
            return await self._read_pages(pages)
        return ReadResult(text=await self._read_one(content), page_count=1)
```

Four calls, but roughly one call's latency — and they rejoin positionally, not in
completion order:

```python
        texts = await asyncio.gather(*(self._read_one(page) for page in pages))
        joined = self.page_separator.join(text for text in texts if text.strip())
        return ReadResult(text=joined, page_count=len(pages))
```

If a page fails, the whole document fails. Half a credit read as though it were the whole
thing is worse than none, because the missing half becomes a confident finding about a
field that was never read.

## 5. Classifying — deciding what each document is

The uploader asserts nothing about what a file is; they upload it and the classifier
decides from the text alone. A verdict below the confidence threshold is **forced** to
unknown rather than merely flagged:

```python
    if classification.confidence < threshold:
        ...
        # Forced to unknown rather than merely flagged: reading a document under the
        # wrong schema invents fields, and an invented field becomes a discrepancy.
        return RoutedDocument(
            raw=raw,
            classification=classification.model_copy(
                update={
                    "document_type": DocumentType.UNKNOWN,
                    "reasoning": f"{message}. Original reasoning: {classification.reasoning}",
                }
            ),
            usage=usage,
        )
```

## 6. Extracting — one schema per family

```mermaid
flowchart LR
    CL["classify"] --> R{"route on document_type"}
    R -->|letter_of_credit| E1["LetterOfCredit<br/>lc_number, expiry_date, tolerance_percent…"]
    R -->|commercial_invoice| E2["CommercialInvoice<br/>total_amount, line_items, incoterms…"]
    R -->|bill_of_lading| E3["BillOfLading<br/>shipment_date, vessel, ports…"]
    R -->|"…five more families"| E4["…"]
    R -->|unknown| SK["skip — carried through as a finding"]
```

The routing table and the steps it points at are generated from one map, so they cannot
fall out of step:

```python
EXTRACTION_STEPS: dict[DocumentType, Step[CaseState, DetectorDeps, Any, ExtractedDocument]] = {
    document_type: _extraction_step(document_type)
    for document_type in EXTRACTION_PAYLOAD_TYPES
}

    for document_type, step in EXTRACTION_STEPS.items():
        decision = decision.branch(
            builder.match(RoutedDocument, matches=_is_type(document_type)).to(step)
        )
```

Each agent's `output_type` is that family's payload model, whose field docstrings are
part of the prompt the model sees:

```python
    presentation_period: str | None = None
    """Presentation period as written, e.g. 'within 21 days after shipment date'."""

    presentation_period_days: int | None = None
    """The presentation period reduced to a number of days, when it is expressed that way."""
```

Every field is optional on purpose: extractors are told to leave a field `null` rather
than guess, because a missing field is something reconciliation can reason about while an
invented one becomes a discrepancy that does not exist.

## 7. Reconciling — the actual judgement

All the extractions arrive together as one evidence set. This is the stage that runs on
the stronger model at high reasoning effort — and the one that does not do its own
arithmetic:

```mermaid
flowchart TD
    EV["all extracted documents,<br/>as one XML evidence block"] --> AG["reconciliation agent<br/>Gemini Pro, thinking=high"]
    AG -->|"calls, never computes"| T1["check_amount_tolerance<br/>UCP 600 Art 30 a"]
    AG --> T2["check_insurance_coverage<br/>Art 28 f ii — 110% of CIF"]
    AG --> T3["check_date_order"]
    AG --> T4["check_presentation_period<br/>Art 14 c — 21 days or expiry"]
    T1 & T2 & T3 & T4 -->|"exact figures to quote"| AG
    AG --> REP["ReconciliationReport"]
    REP --> RULE["severity → verdict is a rule, not a judgement"]
    RULE --> V["any critical → blocked<br/>any warning → needs_review<br/>else clean"]
```

```python
    parts.append(
        "\nUse the deterministic tools for every date and amount comparison rather than "
        "computing them yourself, and quote the figures they return in your findings.\n"
    )
    parts.append(
        format_as_xml(evidence, root_tag="extracted_documents", item_tag="document")
    )
```

The tools are pure functions, so the same presentation always gets the same arithmetic:

```python
    period_deadline = shipment_date + timedelta(days=presentation_period_days)
    deadline = min(period_deadline, expiry_date)
    days_late = max((presented_on - deadline).days, 0)
```

And the verdict is derived from the findings, overriding whatever the model wrote:

```python
def _derive_status(mismatches: Sequence[Mismatch]) -> CaseStatus:
    """Map findings to a verdict.

    The prompt asks the model for this too, but the mapping is a rule, not a
    judgement, so the rule wins: a report with a critical finding is blocked
    whatever the model wrote in `status`.
    """
    severities = {mismatch.severity for mismatch in mismatches}
    if Severity.CRITICAL in severities:
        return CaseStatus.BLOCKED
    if Severity.WARNING in severities:
        return CaseStatus.NEEDS_REVIEW
    return CaseStatus.CLEAN
```

An unreadable document also appends a warning, so a presentation with a scan nobody could
read can never come back `clean`.

## 8. Collecting the result

The record reaches `succeeded`, the websocket pushes the whole record and closes, and a
poller sees the same thing on its next `GET`:

```python
        async for record in runner.store.watch(case_id):
            await websocket.send_text(record.model_dump_json())
```

```json
{ "status": "succeeded",
  "result": {
    "status": "blocked",
    "report": {
      "summary": "The invoice exceeds the credit amount and shipment was late…",
      "mismatches": [
        { "code": "late_shipment", "severity": "critical",
          "field": "Shipment date", "rule_reference": "UCP 600 Art 14(c)",
          "explanation": "Shipped 2026-03-04, three days after the latest shipment date.",
          "suggested_action": "Request an amendment, or present under reserve." }
      ] } } }
```

`DELETE /v1/cases/{id}` drops the record when the caller is done. Otherwise it expires on
its own — it holds a whole presentation's extracted contents, so it should not outlive
the caller's interest in it.

---

## What happens when things go wrong

The rule everywhere is **degrade rather than fail**. Each of these was once a bug.

```mermaid
flowchart TD
    F["something fails"] --> Q{"is it case-level?"}
    Q -->|"budget spent<br/>run cancelled"| CASE["the case stops,<br/>recorded as failed with a code"]
    Q -->|anything else| WHERE{"where?"}
    WHERE -->|"a scan won't read"| D1["that document only"]
    WHERE -->|"can't be classified"| D2["that document only"]
    WHERE -->|"extraction errors"| D3["that document only"]
    WHERE -->|"reconciler dies"| D4["the extractions are still returned,<br/>with a reconciliation_failed finding"]
    D1 & D2 & D3 --> R["it reaches the report as a finding,<br/>the other documents are still checked"]
```

The shape is the same at each stage — re-raise what is genuinely case-level, contain
everything else:

```python
        except (UsageLimitExceeded, RunCancelled):
            # Case-level: the budget is spent, or the whole run is going away.
            raise
        except Exception as exc:  # noqa: BLE001 - the extractions are still worth returning
            # Every document has already been read, classified and extracted by this
            # point. Letting the failure escape would throw all of that away and hand
            # the caller nothing, so the extracted fields go back with a report saying
            # the cross-checks did not run — which a human examiner can act on.
            report = _unreconciled_report(documents, exc)
```

A case that fails *after* the `202` has no request left to answer, so the runner records
the same codes the HTTP layer would have returned:

```python
        except AgentRunError as exc:
            await self.store.fail(
                case.case_id, code='model_unavailable', detail=f'the model provider failed: {exc}'
            )
        except Exception as exc:  # noqa: BLE001 - a case must always reach a terminal state
            await self.store.fail(
                case.case_id, code='internal_error', detail=f'{type(exc).__name__}: {exc}'
            )
```

One vocabulary, whichever way the failure arrived.

---

## Where each part lives

```mermaid
flowchart TD
    A["api/ — routes, errors, app factory"] --> S["services/ — agents, graph, OCR, runner, store"]
    S --> P["prompts/ — load and validate prompts.yaml"]
    S --> M["models/ — the shapes everything moves through"]
    P --> M
    M --> C["core/ — settings and observability"]
    S --> C
    A --> C
```

The dependency direction is strictly one way. Nothing in `models/` imports Pydantic AI,
and nothing in `services/` imports FastAPI — which is why the same engine serves the API,
the notebook and the tests:

```python
from Detector.services.pipeline import DetectorPipeline

async with DetectorPipeline.open() as pipeline:
    result = await pipeline.run(case_input)
```

| Folder | What it does |
|---|---|
| [`core/`](core/README.md) | Every tunable, and the Logfire wiring |
| [`models/`](models/README.md) | Documents, extractions, findings, job records |
| [`prompts/`](prompts/README.md) | Loads and validates the ten system prompts |
| [`services/`](services/README.md) | The engine: agents, the graph, OCR, the runner |
| [`api/`](api/README.md) | The HTTP and websocket layer |

---

## Two things worth knowing before you deploy

**Cases are held in memory.** This runs as a single process. `CaseStore` is the seam to
swap in a shared store for a multi-process deployment; nothing above it would change.

**The stages run on different models on purpose.** Splitting them is most of the reason a
twenty-document case is affordable — and moving reconciliation to a cheaper model is the
change most likely to cost someone a refusal:

```python
FAST_MODEL = 'google:gemini-3.7-flash'
"""Classification and extraction are mechanical: locate the field, copy the value out."""

REASONING_MODEL = 'google:gemini-3.1-pro-preview'
"""Reconciliation is the compliance judgement — the one stage worth the stronger model."""
```

Further reading: [`../WALKTHROUGH.md`](../WALKTHROUGH.md) follows one real case end to
end, and [`../mini_detector.ipynb`](../mini_detector.ipynb) rebuilds a miniature of the
whole pipeline from scratch.
