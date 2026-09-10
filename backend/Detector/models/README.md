# `models/` — the shapes the pipeline moves through

Pure Pydantic and dataclasses. Nothing here imports Pydantic AI, Pydantic Graph or
FastAPI, which is what lets the same types serve the agents, the graph and the HTTP
responses without any of them leaking into the others.

These are not passive data holders. The extraction payloads **are** the agents'
`output_type`, so every field name and docstring in `extractions.py` is part of the
prompt the model sees.

## The journey of one document

```mermaid
flowchart LR
    RD["RawDocument<br/>bytes or text"] -->|ocr| RD2["RawDocument<br/>text + page_count"]
    RD2 -->|classify| RO["RoutedDocument<br/>+ Classification"]
    RO -->|extract| ED["ExtractedDocument<br/>+ ExtractionPayload"]
    ED --> CR["CaseResult<br/>documents + report"]
    RR["ReconciliationReport<br/>mismatches, verdict"] --> CR
```

And of one case:

```mermaid
flowchart LR
    CI["CaseInput<br/>case_id, documents, presented_on"] --> REC["CaseRecord<br/>status, events, result"]
    REC -->|"once succeeded"| CR["CaseResult"]
```

---

## `enums.py` — the four vocabularies

```mermaid
flowchart TD
    subgraph DT["DocumentType — the eight families, plus unknown"]
        direction LR
        D1[letter_of_credit] ~~~ D2[commercial_invoice] ~~~ D3[bill_of_lading]
        D4[packing_list] ~~~ D5[certificate_of_origin] ~~~ D6[insurance_certificate]
        D7[bill_of_exchange] ~~~ D8[inspection_certificate] ~~~ D9[unknown]
    end
    subgraph SEV["Severity — how badly it hurts"]
        direction LR
        S1["critical<br/>the bank would refuse"] ~~~ S2["warning<br/>a human should look"] ~~~ S3["info<br/>cosmetic"]
    end
    subgraph CS["CaseStatus — the verdict"]
        direction LR
        C1[clean] ~~~ C2[needs_review] ~~~ C3[blocked]
    end
    subgraph JS["JobStatus — where the run got to"]
        direction LR
        J1[queued] --> J2[running] --> J3[succeeded]
        J2 --> J4[failed]
        J2 --> J5[cancelled]
    end
```

`CaseStatus` and `JobStatus` are deliberately separate. A case can be `succeeded` **and**
`blocked`: the analysis ran to completion, and its answer was that the bank would refuse.

---

## `extractions.py` — one typed payload per family

Eight models, each the `output_type` of its family's agent, all sharing
`extra='forbid'` and `use_attribute_docstrings=True`.

```mermaid
flowchart TD
    EB["ExtractionBase<br/>extra=forbid, docstrings become schema"]
    EB --> LC[LetterOfCredit]
    EB --> INV[CommercialInvoice]
    EB --> BOL[BillOfLading]
    EB --> PL[PackingList]
    EB --> COO[CertificateOfOrigin]
    EB --> INS[InsuranceCertificate]
    EB --> BOE[BillOfExchange]
    EB --> INSP[InspectionCertificate]
    INV -.->|has many| LI[LineItem]
    INV -.->|has one| SD[ShippingDetails]
    PL -.->|has many| PB[PackageBreakdownEntry]
    LC & INV & BOL & PL & COO & INS & BOE & INSP --> U["ExtractionPayload<br/>discriminated on 'kind'"]
```

**Every field is optional on purpose.** The extractors are told to leave a field `null`
rather than guess, because a missing field is itself something the reconciliation stage
can reason about — while an invented one becomes a discrepancy that does not exist.

Each payload carries a literal `kind` field, which is the discriminator on the
`ExtractionPayload` union. That is what lets a `CaseResult` round-trip through JSON and
come back as the right type.

`EXTRACTION_PAYLOAD_TYPES` maps `DocumentType → payload class`, and it is the single
table the rest of the system is generated from: the extraction agents, the graph's
extraction steps, and its routing branches all iterate it. **Adding a document family
means adding one entry here and one prompt.**

---

## `documents.py` — documents in flight

| Type | What it is |
|---|---|
| `RawDocument` | one uploaded document: `content` bytes *or* `text`, plus `filename`, `page_count`, `ocr_error` |
| `CaseInput` | the unit of work — every document presented under one credit |
| `Classification` | what the classifier decided: type, confidence, reasoning |
| `RoutedDocument` | a classified document on its way to an extractor |
| `ExtractedDocument` | the output of one document's whole branch |
| `TokenUsage` | a serialisable snapshot of what a stage cost |

Two details that matter:

**`RawDocument.content` is excluded from serialisation.** A presentation is megabytes of
scanned paper, and none of it belongs in an API response, a log line or a stored case
record. The bytes are read once by the OCR step and dropped.

**`CaseInput` rejects duplicate `document_id`s.** Every later stage keys on it — the
audit trail, the per-document result, the observations a finding cites — so two documents
sharing an id would make a finding point at the wrong piece of paper.

```mermaid
flowchart LR
    subgraph "usage accumulates down the branch"
        A["classify<br/>TokenUsage"] -->|carried in RoutedDocument| B["extract<br/>+ TokenUsage"]
        B -->|"__add__"| C["ExtractedDocument.usage<br/>the whole branch"]
    end
```

---

## `reconciliation.py` — findings and the verdict

```mermaid
flowchart TD
    CR["CaseResult<br/>what one run produced"]
    CR --> RR[ReconciliationReport]
    CR --> ED["documents: ExtractedDocument[]"]
    CR --> TU["usage: TokenUsage"]
    RR --> ST["status: CaseStatus"]
    RR --> SUM["summary — two or three sentences"]
    RR --> MM["mismatches: Mismatch[]"]
    RR --> MF["matched_fields — what agreed"]
    RR --> MD["missing_documents"]
    MM --> SEVR["severity, code, field, explanation"]
    MM --> RULE["rule_reference<br/>e.g. UCP 600 Art 14 c"]
    MM --> ACT["suggested_action — how to cure it"]
    MM --> OBS["observations: FieldObservation[]"]
    OBS --> OV["document_type, field, value, document_id<br/>the conflicting values, one per document"]
```

A `Mismatch` is written to be actionable by a bank ops examiner: `explanation` says what
disagrees in plain language, `observations` shows the conflicting values side by side,
`rule_reference` cites the article relied on, and `suggested_action` says how to cure it.

---

## `jobs.py` — a submitted case as a record

`CaseRecord` is the **only** shape the API hands back: it is the `202` body, the polling
response, and every websocket message.

```mermaid
stateDiagram-v2
    [*] --> queued: submitted
    queued --> running: a slot came free
    running --> succeeded: result is set
    running --> failed: error + error_code are set
    running --> cancelled: deleted, or shutdown
    queued --> cancelled
    succeeded --> [*]
    failed --> [*]
    cancelled --> [*]
```

`events` is a list of `CaseEvent` — the audit trail, appended as the run produces it.
Because a record is a complete snapshot rather than a delta, a websocket client that
connects late or reconnects simply renders the newest one and is correct.
