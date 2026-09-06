# Following one case, end to end

Three documents go in. A refusal comes out. This document walks every step in between.

Every value shown here is **real output** captured from a run, not illustration. The only thing
simulated is the model's replies (there was no API key on the machine that produced this) — the
prompts, the schemas, the arithmetic and the verdict logic are all genuine.

- **Companion docs:** [`mini_detector.ipynb`](mini_detector.ipynb) builds the same system from
  scratch, bottom-up, and runs offline. [`Detector/ARCHITECTURE.md`](Detector/ARCHITECTURE.md) is the
  per-construct reference.
- **Read this one** if you want to know *what happens to the data*.

---

## Contents

| | Stage | What it does |
|---|---|---|
| [0](#0-the-papers) | — | The three documents |
| [1](#1-what-you-hand-in) | — | `CaseInput` |
| [2](#2-ingest) | `ingest` | Validate, or refuse to start |
| [3](#3-the-fan-out) | fork | Split into one branch per document |
| [4](#4-classify) | `classify` | What kind of document is this? |
| [5](#5-route) | decision | Pick the extractor |
| [6](#6-extract) | `extract_*` | Text → typed fields |
| [7](#7-the-join) | join | Wait for all branches |
| [8](#8-reconcile) | `reconcile` | Cross-check everything |
| [9](#9-the-status-rule) | — | Findings → verdict |
| [10](#10-the-output) | — | What the caller gets |
| [11](#11-where-everything-lives) | — | File map |

---

## 0. The papers

A seller in India shipped t-shirts to a buyer in Singapore under a letter of credit. To get paid,
the seller presents three documents to the bank.

**The credit** — the bank's promise. This defines what a compliant presentation looks like:

```
IRREVOCABLE DOCUMENTARY CREDIT
LC Number: LC-2026-88431
Issuing Bank: Meridian Commercial Bank, Singapore
Applicant: Harborline Trading Pte Ltd, Singapore
Beneficiary: Anand Textiles Pvt Ltd, Tirupur, India
Amount: USD 250,000.00
Tolerance: +/- 5 PCT
Expiry Date: 2026-03-15 at counters of issuing bank
Latest Shipment Date: 2026-02-20
Port of Loading: Chennai, India
Port of Discharge: Singapore
Description of Goods: 40,000 pcs 100% cotton knitted t-shirts, CIF Singapore
```

**The invoice** — what the seller is charging:

```
COMMERCIAL INVOICE
Invoice No: INV-4471          Date: 2026-02-18
L/C Ref: LC-2026-88431
Seller: Anand Textiles Pvt Ltd, Tirupur, India
Buyer: Harborline Trading Pte Ltd, Singapore
Description: 40,000 pcs 100% cotton knitted t-shirts, CIF Singapore
Total Amount: USD 268,400.00
```

**The bill of lading** — proof the goods actually shipped:

```
BILL OF LADING
B/L No: MSCU-772311
Shipper: Anand Textiles Pvt Ltd, Tirupur, India
Consignee: To order of Meridian Commercial Bank
Vessel: MV NORTHERN STAR       Voyage: 118W
Port of Loading: Chennai, India
Port of Discharge: Port Klang, Malaysia
Shipped on board: 2026-02-24
Description: 40,000 pcs cotton knitted t-shirts
```

They were presented to the bank on **2026-03-02**.

> **The one rule that explains everything below:** banks deal in *documents, not goods*. It does not
> matter that the shipment really happened. If the paperwork disagrees with the credit, the bank
> refuses to pay. The rulebook is **UCP 600**.

Take a moment with those three documents before reading on. There are three real problems and one
thing that looks like a problem but isn't.

---

## 1. What you hand in

The package does no OCR and no file handling. You turn PDFs into text; it takes it from there.

```python
sample_case = CaseInput(
    case_id='case-001',
    presented_on=date(2026, 3, 2),
    documents=[
        RawDocument(document_id='doc-lc',  text=LC_TEXT,      filename='credit.pdf'),
        RawDocument(document_id='doc-inv', text=INVOICE_TEXT, filename='invoice.pdf'),
        RawDocument(document_id='doc-bol', text=BOL_TEXT,     filename='bol.pdf'),
    ],
)

result = await get_pipeline().run(sample_case)
```

That's the entire public surface. Two things worth noting:

- **`document_id` is yours.** Events and findings key on it, and documents come back in completion
  order, not the order you sent them.
- **`presented_on` is not optional in spirit.** Without it the presentation-period check can't run,
  and the prompt explicitly tells the model *not* to guess a date. Leave it out and you lose a check.

There is also `declared_type`, for when the uploader claimed a document was an invoice. It is passed
to the classifier as a *hint* and never trusted, because uploaders mislabel constantly.

---

## 2. `ingest`

The first graph step. It validates the whole case and then hands the documents to the fan-out.

```python
@builder.step
async def ingest(ctx: StepContext[CaseState, DetectorDeps, CaseInput]) -> list[RawDocument]:
    case, settings = ctx.inputs, ctx.deps.settings

    if len(case.documents) > settings.max_documents_per_case:
        raise ValueError(...)

    for document in case.documents:
        if len(document.text) > settings.max_document_chars:
            raise ValueError(...)          # reject — never truncate
        if not document.text.strip():
            raise ValueError(...)

    ctx.state.record('ingest', f'accepted {len(case.documents)} document(s)')
    return case.documents
```

**Output:**

```
[ingest] accepted 3 document(s)
```

**The one thing worth understanding here is what it refuses to do.** An oversized document raises
instead of being trimmed to fit. A silently truncated letter of credit still extracts — it just
extracts *confidently and wrongly*, missing whichever terms fell off the end. A loud failure at the
door is the cheapest possible outcome.

Returning `list[RawDocument]` is what sets up the next step.

---

## 3. The fan-out

```python
builder.edge_from(ingest)
       .label('per document')
       .map(fork_id=FAN_OUT_ID, downstream_join_id=COLLECT_ID)
       .to(classify)
```

`.map()` takes the list `ingest` returned and **sends each element down its own concurrent branch**.
So `classify` receives a single `RawDocument`, not the list.

From here until the join, there are three independent pipelines running at once:

```
                  ┌── doc-lc  ── classify → route → extract ──┐
ingest ── fan out ├── doc-inv ── classify → route → extract ──┤ join → reconcile
                  └── doc-bol ── classify → route → extract ──┘
```

Each branch is fully independent. **doc-lc starts extracting while doc-bol is still classifying** —
there's no phase barrier, only the join at the end. Measured with 300ms model calls, 3 documents:
0.93s, against 2.10s if it ran sequentially.

`downstream_join_id` handles one edge case: mapping an *empty* list. Without it a case with zero
documents would fan out to nothing and the join would wait forever. With it, the fork jumps straight
to the join, which yields its empty initial value.

---

## 4. `classify`

Runs three times, in parallel. Here's doc-lc.

### What the model is sent

Instructions (the system prompt) plus this:

```
Document id: doc-lc
Filename: credit.pdf

<document_text>

IRREVOCABLE DOCUMENTARY CREDIT
LC Number: LC-2026-88431
Issuing Bank: Meridian Commercial Bank, Singapore
...
</document_text>
```

The `<document_text>` tags matter. They mark where the untrusted OCR text starts and stops, so a
scanned document containing the words "ignore your instructions" reads as content, not as a command.

### The shape it must reply in

The agent declares `output_type=Classification`, and Pydantic AI turns that class into a JSON schema
the provider is made to conform to. This is the real generated schema:

```json
{
  "document_type": {
    "$ref": "#/$defs/DocumentType",
    "description": "The family this document belongs to, or `unknown` if it doesn't clearly match one."
  },
  "confidence": {
    "description": "How sure the classifier is, from 0 to 1.",
    "minimum": 0.0, "maximum": 1.0, "type": "number"
  },
  "reasoning": {
    "description": "The markers in the text that drove the decision, in one or two sentences.",
    "type": "string"
  }
}
```

Those `description` strings are not comments. **They are part of the prompt** — the model reads them
at inference time. They come from the field docstrings on the `Classification` model, so the type
definition and the instructions cannot drift apart.

### What comes back

```python
Classification(
    document_type=DocumentType.LETTER_OF_CREDIT,
    confidence=0.97,
    reasoning='Names an issuing bank, an applicant and a beneficiary.',
)
```

Not a string to parse. A validated object — `confidence` is guaranteed to be between 0 and 1 because
the schema said so and Pydantic checked.

**Output of all three branches:**

```
[classify] doc-lc:  letter_of_credit   at 0.97
[classify] doc-inv: commercial_invoice at 0.96
[classify] doc-bol: bill_of_lading     at 0.95
```

### Two ways this step protects you

**A low-confidence result is treated as unknown:**

```python
if classification.confidence < deps.settings.min_classification_confidence:   # 0.5
    return UnclassifiedDoc(raw=raw, classification=classification)
```

A document called a packing list at 0.2 confidence is better treated as unreadable than run through
the packing-list extractor, which would produce a plausible-looking extraction of the wrong fields.

**A failed call degrades one document, not the case:**

```python
except (UsageLimitExceeded, RunCancelled):
    raise                                  # budget breach / cancellation → stop everything
except AgentRunError as exc:
    return UnclassifiedDoc(...)            # anything else → this document only
```

The ordering is deliberate: those two are *subclasses* of `AgentRunError`, so they must be caught
first or they'd be swallowed. A blown budget should stop the case. A flaky HTTP 500 shouldn't.

---

## 5. Route

`classify` doesn't return a generic object with a type field. It returns one of four **empty
subclasses**:

```python
class LetterOfCreditDoc(RoutedDocument): ...
class CommercialInvoiceDoc(RoutedDocument): ...
class BillOfLadingDoc(RoutedDocument): ...
class UnclassifiedDoc(RoutedDocument): ...
```

`...` is the whole class body. They add no data. Their only job is to be four *different types*:

```python
builder.decision(node_id='route_by_document_type')
    .branch(builder.match(LetterOfCreditDoc).to(extract_letter_of_credit))
    .branch(builder.match(CommercialInvoiceDoc).to(extract_commercial_invoice))
    .branch(builder.match(BillOfLadingDoc).to(extract_bill_of_lading))
    .branch(builder.match(UnclassifiedDoc).to(skip_unclassified))
```

**Why bother, when an `if` on `document_type` would work?**

```python
# the version that isn't used:
if doc.classification.document_type == DocumentType.LETTER_OF_CREDIT:
    ...
# add a 9th document family, forget a branch → falls through silently, at runtime, in production
```

The `Decision` type accumulates the types it has handled. Declaring the return as
`Decision[..., RoutedDocuments]` asserts the branch table covers the entire union — so forgetting a
family is **a type error your checker catches**, not a surprise at 2am. That guarantee is what the
four otherwise-pointless classes buy.

The envelope is picked by table lookup, so there's no branching here either:

```python
envelope = ROUTED_DOCUMENT_TYPES[classification.document_type]
return envelope(raw=raw, classification=classification)
```

---

## 6. `extract`

Also three times, in parallel. Each family has its **own agent, own prompt, and own output type** —
so the LC extractor can only ever return an LC shape.

The prompt is short, because the schema is doing the work:

```
You extract structured information from a Letter of Credit (LC) document.
Only use information explicitly found in the text provided. If a field is not present,
leave it null rather than guessing. Normalise dates to ISO format (YYYY-MM-DD).
```

It doesn't list the fields to extract. It doesn't have to — `output_type=LetterOfCredit` already
told the model every field name, type and description. The prompt only sets the *policy*.

**That policy line is the important one.** Every field on every payload is optional
(`str | None = None`) so that "not present" is always a legal answer. A missing field is evidence in
its own right; an invented one is a compliance failure.

### What comes back for doc-lc

```json
{
  "kind": "letter_of_credit",
  "lc_number": "LC-2026-88431",
  "issuing_bank": "Meridian Commercial Bank, Singapore",
  "applicant": "Harborline Trading Pte Ltd, Singapore",
  "beneficiary": "Anand Textiles Pvt Ltd, Tirupur, India",
  "credit_amount": "250000.00",
  "currency": "USD",
  "expiry_date": "2026-03-15",
  "latest_shipment_date": "2026-02-20",
  "port_of_loading": "Chennai, India",
  "port_of_discharge": "Singapore",
  "description_of_goods": "40,000 pcs 100% cotton knitted t-shirts, CIF Singapore",
  "tolerance_percent": "5"
}
```

In Python those are **real types**, not strings that look like values:

```python
>>> type(payload.credit_amount)      # Decimal('250000.00')
<class 'decimal.Decimal'>
>>> type(payload.expiry_date)        # datetime.date(2026, 3, 15)
<class 'datetime.date'>
```

`Decimal`, never `float`. Tolerance arithmetic on a float is how you refuse a compliant presentation
for being $0.000001 over.

Notice too that `Tolerance: +/- 5 PCT` in the raw text became `tolerance_percent: 5`. That
normalisation is why the amount check downstream is a single subtraction.

### If extraction fails

```python
except AgentRunError as exc:
    return ExtractedDocument(**base, payload=None, error=f'extraction failed: {exc}')
```

`payload` and `error` are complementary — exactly one is set. The failed document **still travels on
to reconciliation**, so the failure becomes a warning in the report rather than a stack trace in your
logs. Section 8 shows what that warning looks like.

**Output:**

```
[extract] doc-lc:  extracted letter_of_credit
[extract] doc-inv: extracted commercial_invoice
[extract] doc-bol: extracted bill_of_lading
```

---

## 7. The join

```python
collect = builder.join(reduce_list_append, initial_factory=list[ExtractedDocument])
```

Waits for all three branches and folds them into one list. This is the only barrier in the graph, and
it has to be — reconciliation needs every document before it can compare anything.

Results arrive in **completion order**, so `reconcile` sorts by `document_id` first to make runs
reproducible.

---

## 8. `reconcile`

One model call, over everything. This is where discrepancies are actually found.

### What the model is sent

The extracted documents, rendered as XML — **not the original text**. The reconciler never sees raw
OCR output:

```xml
Case: case-001
Documents presented: 3
Date of presentation: 2026-03-02

Use the deterministic tools for every date and amount comparison rather than
computing them yourself, and quote the figures they return in your findings.

<extracted_documents>
  <document>
    <document_id>doc-bol</document_id>
    <document_type>bill_of_lading</document_type>
    <classification_confidence>0.95</classification_confidence>
    <fields>
      <kind>bill_of_lading</kind>
      <bl_number>MSCU-772311</bl_number>
      <shipper>Anand Textiles Pvt Ltd, Tirupur, India</shipper>
      <consignee>To order of Meridian Commercial Bank</consignee>
      <vessel_name>MV NORTHERN STAR</vessel_name>
      <port_of_loading>Chennai, India</port_of_loading>
      <port_of_discharge>Port Klang, Malaysia</port_of_discharge>
      <shipment_date>2026-02-24</shipment_date>
      <description_of_goods>40,000 pcs cotton knitted t-shirts</description_of_goods>
    </fields>
  </document>
  ...
</extracted_documents>
```

**This is the payoff of the three-stage split.** The hard reasoning step reads a tidy, typed table.
If you handed it the raw text instead, it would be doing extraction and judgement at the same time,
and doing both slightly worse.

Note the presentation-date handling. When `presented_on` is missing, the prompt says so explicitly:

> `Date of presentation: not supplied. Do not raise a presentation-period finding on a date you had
> to assume.`

Without that line, a model asked to check timeliness will invent a date and find a discrepancy.

### The arithmetic is not the model's job

The reconciler gets three plain Python functions as tools. Date and money maths is exactly where
language models are subtly and confidently wrong, and exactly where being wrong costs the beneficiary
a refusal.

**These are the real verdicts, computed by pure functions on the extracted values — no model
involved:**

```python
check_amount_tolerance(Decimal('250000.00'), Decimal('268400.00'), Decimal('5'))
# passed=False
# 'presented 268400.00 against credit 250000.00 with 5% tolerance
#  (ceiling 262500.00): over by 5900.00'

check_date_order('shipment date', date(2026,2,24), 'latest shipment date', date(2026,2,20))
# passed=False
# 'shipment date 2026-02-24 vs latest shipment date 2026-02-20: late by 4 day(s)'

check_presentation_period(date(2026,2,24), date(2026,3,15), date(2026,3,2))
# passed=True
# 'presented 2026-03-02 against deadline 2026-03-15
#  (set by the expiry date): in time'
```

Two things to notice.

**The tools return the numbers, not just a yes/no.** That `detail` string is written to be quoted
straight into a finding, which is why the report can say "over by USD 5,900.00" and be exactly right.

**The third check passes, and says which rule bound it.** UCP 600 Art 14(c) gives you 21 days from
shipment *and* not later than expiry — the deadline is the earlier of the two. Here 02-24 + 21 days
is 03-17, but the credit expires 03-15, so expiry binds. They presented on 03-02, comfortably in
time. That rule lives in code, not in a prompt, so it can't drift.

### What comes back

A `ReconciliationReport`: a summary, a list of `Mismatch` objects, and the fields that agreed.

---

## 9. The status rule

The prompt asks the model for a `status`, and the report has the field. **The code throws it away:**

```python
def _derive_status(mismatches):
    severities = {m.severity for m in mismatches}
    if Severity.CRITICAL in severities:
        return CaseStatus.BLOCKED
    if Severity.WARNING in severities:
        return CaseStatus.NEEDS_REVIEW
    return CaseStatus.CLEAN

return report.model_copy(update={'mismatches': mismatches, 'status': _derive_status(mismatches)})
```

Mapping findings to a verdict is **a rule, not a judgement**, so the rule wins. A report that lists a
critical finding and claims `clean` comes back `blocked`, every time.

This is a small function and it is the most important one in the project. It's the line between a
system a bank can use and a demo.

The same function also appends a warning for any document that failed to extract, so an unreadable
scan can't quietly shrink the evidence base without the examiner being told:

```
[warning] 1 presented document(s) could not be read into structured fields and took no
          part in the cross-checks: doc-bol (extraction failed: ...)
```

---

## 10. The output

```
[reconcile] blocked: 3 critical, 0 warning
```

### Finding 1 — the invoice is drawn over the credit · CRITICAL

| Document | Field | Value |
|---|---|---|
| letter_of_credit | `credit_amount` | USD 250,000.00 ± 5% |
| commercial_invoice | `total_amount` | USD 268,400.00 |

The tolerance gives a ceiling of USD 262,500.00. The invoice is over by **USD 5,900.00**.

*UCP 600 Art 18(b), Art 30(a)* — a tolerance applies only when the credit states one. This one did
("+/- 5 PCT"), and the invoice still breaks it.
**Cure:** amend the credit, or present an invoice within the ceiling.

### Finding 2 — late shipment · CRITICAL

| Document | Field | Value |
|---|---|---|
| letter_of_credit | `latest_shipment_date` | 2026-02-20 |
| bill_of_lading | `shipment_date` | 2026-02-24 |

Four days late. The goods went on board after the last date the credit permits.

*UCP 600 Art 20* — the on-board date is the date of shipment.
**Cure:** request an amendment extending the latest shipment date.

### Finding 3 — wrong port of discharge · CRITICAL

| Document | Field | Value |
|---|---|---|
| letter_of_credit | `port_of_discharge` | Singapore |
| bill_of_lading | `port_of_discharge` | Port Klang, Malaysia |

The transport document doesn't cover the carriage the credit requires. The goods are going to the
wrong country.

*UCP 600 Art 20(a)(ii)*
**Cure:** obtain a corrected bill of lading, or amend the credit.

### Finding 4 — the red herring · INFO

The bill of lading says "cotton knitted t-shirts" where the credit says "**100%** cotton knitted
t-shirts". This looks like a discrepancy and **is not one.**

*UCP 600 Art 14(e)* — on documents other than the invoice, a general description of the goods is
fine as long as it doesn't conflict with the credit. Only the **invoice** must correspond strictly
(Art 18(c)) — and it does, word for word.

Getting this right matters as much as catching the other three. A checker that flags every textual
difference produces so much noise that people stop reading it.

### And what agreed

```
beneficiary / seller / shipper · applicant / buyer · LC number quoted on the invoice
· port of loading · currency
```

Worth reporting. It tells the examiner what was actually checked, so silence on a field means "we
looked" rather than "we forgot".

### The verdict

```python
CaseResult(
    case_id='case-001',
    status=CaseStatus.BLOCKED,       # derived, not the model's opinion
    report=ReconciliationReport(...),
    documents=[...],                 # all three, with payloads
)
```

**BLOCKED.** The bank refuses. The seller has three things to fix before re-presenting, and two of
them need the buyer's agreement to amend the credit.

---

## 11. Where everything lives

| Stage | File |
|---|---|
| `CaseInput`, `RawDocument`, `ExtractedDocument` | [`Detector/models/documents.py`](Detector/models/documents.py) |
| The 8 payload schemas | [`Detector/models/extractions.py`](Detector/models/extractions.py) |
| `Mismatch`, `ReconciliationReport`, `CaseResult` | [`Detector/models/reconciliation.py`](Detector/models/reconciliation.py) |
| Every prompt | [`Config/prompts.yaml`](Config/prompts.yaml) |
| The 10 agents | [`Detector/services/agents.py`](Detector/services/agents.py) |
| The deterministic tools | [`Detector/services/tools.py`](Detector/services/tools.py) |
| Steps, routing, fan-out, join, the status rule | [`Detector/services/graph.py`](Detector/services/graph.py) |
| `run()` / `run_with_progress()` | [`Detector/services/pipeline.py`](Detector/services/pipeline.py) |

The real package handles **eight** document families, not three — packing list, certificate of
origin, insurance certificate, bill of exchange and inspection certificate as well. The shape of the
run is identical; there are just more branches on the routing decision and a fourth tool
(`check_insurance_coverage`, UCP 600 Art 28(f)(ii)).

### The three ideas worth stealing

1. **Put the schema where the model reads it.** Field descriptions ride into the JSON schema, so
   your types *are* your prompt and the two can't disagree.
2. **Take the arithmetic away from the model.** Anything where "confidently slightly wrong" is
   expensive belongs in a pure function — and it should return the numbers so the model can quote
   them.
3. **Let rules override judgement at the boundary.** The model proposes findings; code derives the
   verdict.
