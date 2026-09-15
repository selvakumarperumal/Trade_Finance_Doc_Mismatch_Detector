# `samples/` — PDFs to test with

One presentation under letter of credit `LC-2026-88421`, as five PDFs. Drag them into the
**As scans** tab at http://localhost:8000, or:

```bash
curl -X POST localhost:8000/v1/cases/uploads \
  -F files=@samples/01-letter-of-credit.pdf \
  -F files=@samples/02-commercial-invoice.pdf \
  -F files=@samples/03-bill-of-lading.pdf \
  -F files=@samples/04-packing-list.pdf \
  -F presented_on=2026-03-20
```

| File | Pages | What it is |
|---|---|---|
| `01-letter-of-credit.pdf` | **2** | MT700-style credit for USD 50,000, no tolerance |
| `02-commercial-invoice.pdf` | 1 | Drawn for USD 51,000 |
| `03-bill-of-lading.pdf` | 1 | Clean on board 2026-03-04 |
| `04-packing-list.pdf` | 1 | Agrees with the bill of lading throughout |
| `05-unreadable-scan.pdf` | 1 | Blank on purpose — see below |

The credit is two pages deliberately: Textract's synchronous read takes **one page per
call**, so that file alone proves the page-splitting works. Four documents plus the blank
one is six Textract calls, not five.

## What is planted

They disagree the way real presentations do. A run with a real model should come back
**`blocked`**:

| Discrepancy | Should be caught by |
|---|---|
| Invoice is USD 51,000 against a USD 50,000 credit, tolerance `NIL` | `check_amount_tolerance` — UCP 600 Art 30(a) |
| Shipped 2026-03-04, after the latest shipment date of 2026-03-01 | `check_date_order` |
| Beneficiary is `ACME TRADING LIMITED` on the credit, `ACME TRADING LTD` on the rest | the reconciliation agent |
| Credit requires an insurance certificate for 110% of CIF — none is presented | `missing_documents` |
| The invoice omits the port of discharge the credit names | the reconciliation agent |

Presented on 2026-03-20: within both the 21-day window from shipment and the 2026-03-31
expiry, so the Art 14(c) check should **pass**. Change `presented_on` to something after
2026-03-25 to make it fail too.

## The blank one

`05-unreadable-scan.pdf` has no extractable text. Include it to watch the degradation
path: the OCR step reports `no text found`, `classify` sends it to `unknown` without
spending a model call, and it reaches the report as a finding — **the other four are
still cross-checked and the case still succeeds.** One bad scan never fails a
presentation.

## Regenerating them

`sources/` holds the HTML each PDF was printed from. Edit one and print it to PDF from
any browser (A4, background graphics on) to change the planted discrepancies — to build a
`clean` presentation, for instance, set the invoice total to 50,000.00 and the on-board
date to 2026-03-01 or earlier.

## A note on cost

Uploading these makes **real Textract calls** against whatever AWS account the service is
configured with — six of them for the full set. Use the **As text** tab, or the frontend's
*Load a sample presentation* button, to exercise the same case for free.
