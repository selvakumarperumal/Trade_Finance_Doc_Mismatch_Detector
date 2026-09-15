# `frontend/` — a page to click through

Enough of a UI to submit a presentation and watch it being analysed. Its real job is to
be the working example of the Socket.IO contract: **every `case` event is the whole
`CaseRecord`, so rendering is `render(latest)` and nothing else.**

```
public/
  index.html    three panels: present, watch, verdict
  app.js        submit, subscribe, render — no framework, no state machine
  socketio.js   a minimal Socket.IO client (see below)
  sample.js     a sample presentation with deliberate discrepancies
  styles.css    light and dark, no build step
  config.js     where the API is; empty means "same origin as this page"
```

No build, no bundler, no `node_modules`. They are plain ES modules, so the browser loads
them directly.

## Running it

The backend serves these files itself, so there is nothing separate to start:

```bash
docker compose up --build      # then open http://localhost:8000
```

That works because `DETECTOR_FRONTEND_DIR=/frontend` is set in `docker-compose.yaml` and
the directory is bind-mounted read-only. Locally, without Docker:

```bash
cd backend
DETECTOR_FRONTEND_DIR=../frontend/public uv run uvicorn main:app --reload
```

**One origin for everything** — the page, the REST routes and the Socket.IO endpoint — so
there is no CORS to configure. To serve the page from somewhere else instead, point
`window.DETECTOR_API` in `config.js` at the backend and set `DETECTOR_CORS_ORIGINS` on
the backend to allow that origin. A `?api=http://host:port` query parameter overrides
`config.js` for a one-off.

## Trying it without any documents

**Load a sample presentation** fills the three boxes with a letter of credit, a commercial
invoice and a bill of lading that disagree on purpose:

| Planted discrepancy | What should catch it |
|---|---|
| invoice drawn for USD 51,000 against a USD 50,000 credit with no tolerance | `check_amount_tolerance`, UCP 600 Art 30(a) |
| shipped 2026-03-04, after the latest shipment date of 2026-03-01 | `check_date_order` |
| beneficiary is `ACME TRADING LIMITED` on the credit, `ACME TRADING LTD` elsewhere | the reconciliation agent |
| the invoice omits the port of discharge the credit requires | the reconciliation agent |

A real run should come back **blocked**. Running against pydantic-ai's `test` model — no
`GEMINI_API_KEY` — everything classifies as unknown and you get `needs_review` instead;
that is the stub talking, not a bug.

## Why a hand-written Socket.IO client

`socketio.js` is about 60 lines of actual code and speaks only the WebSocket transport.
The official `socket.io-client` is what a real frontend should use — it adds
reconnection, HTTP long-polling fallback, acks and multiplexing — but pulling it in means
a package registry and a build step for a page that otherwise needs neither.

The protocol is two layers, and each puts its type in the leading character:

```
Engine.IO   0 open   1 close   2 ping   3 pong   4 message
Socket.IO   (inside a "4")  0 connect   1 disconnect   2 event   4 connect_error
```

So `42["case",{…}]` is: Engine.IO message, Socket.IO event, named `case`. The handshake
is `0{…}` from the server, `40` back to join the default namespace, `40{…}` to confirm.
After that it is events both ways, and a `2` ping that must be answered with `3` or the
server hangs up.

To swap in the real client, replace the `connect` import in `app.js` — the shape of
`connect(url).on(event, fn).emit(event, payload)` is deliberately the same.

## What the page does

```
POST /v1/cases  or  /v1/cases/uploads   ->  202 { case_id }
Socket.IO subscribe { case_id }         ->  'case' × N  ->  'done'
```

`render(record)` redraws the status, the audit trail, the verdict, the findings and the
per-document results from whatever arrived last. There is no accumulation anywhere,
because there is no need for any.
