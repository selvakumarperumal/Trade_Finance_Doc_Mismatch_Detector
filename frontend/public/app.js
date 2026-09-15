/**
 * The page. Submit a presentation, then render whatever the socket pushes back.
 *
 * The rendering is deliberately dumb: every `case` event carries the *whole*
 * `CaseRecord`, so there is nothing to accumulate and no message types to reconcile —
 * `render(record)` is called with the latest one and redraws from scratch. That is the
 * property the API was built around, and it is why this file has no state machine.
 */

import { connect } from './socketio.js';
import { SAMPLE } from './sample.js';

const API = new URLSearchParams(location.search).get('api') ?? window.DETECTOR_API ?? '';
const api = (path) => new URL(path, API || location.origin).toString();

const $ = (id) => document.getElementById(id);
const show = (el, on = true) => el.classList.toggle('is-hidden', !on);

let socket = null;

// --- submitting --------------------------------------------------------------

function textDocuments() {
  return [...document.querySelectorAll('#text-docs textarea')]
    .map((box) => box.value.trim())
    .filter(Boolean)
    .map((text) => ({ text }));
}

async function submit() {
  const button = $('submit');
  const presentedOn = $('presented-on').value || null;
  const asFiles = document.querySelector('.tab.is-active').dataset.mode === 'files';

  button.disabled = true;
  show($('submit-error'), false);
  try {
    const response = asFiles
      ? await submitFiles(presentedOn)
      : await submitText(presentedOn);

    const body = await response.json();
    if (!response.ok) throw new Error(`${body.code ?? response.status}: ${body.detail ?? ''}`);
    watch(body);
  } catch (error) {
    $('submit-error').textContent = String(error.message ?? error);
    show($('submit-error'));
  } finally {
    button.disabled = false;
  }
}

function submitText(presentedOn) {
  const documents = textDocuments();
  if (!documents.length) throw new Error('nothing to analyse — paste at least one document');
  return fetch(api('/v1/cases'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ documents, presented_on: presentedOn }),
  });
}

function submitFiles(presentedOn) {
  const chosen = $('files').files;
  if (!chosen.length) throw new Error('choose at least one file');
  const form = new FormData();
  for (const file of chosen) form.append('files', file);
  if (presentedOn) form.append('presented_on', presentedOn);
  return fetch(api('/v1/cases/uploads'), { method: 'POST', body: form });
}

// --- watching ----------------------------------------------------------------

function watch(record) {
  socket?.close();
  show($('live-idle'), false);
  show($('live'));
  show($('report-panel'), false);
  render(record);

  socket = connect(API || location.origin, {
    onError: (error) => note(`connection: ${error.message}`),
  });
  socket.on('case', render);
  socket.on('done', () => note('stream closed — the case is finished'));
  socket.on('error', (payload) => note(`${payload.code}: ${payload.detail}`));
  socket.emit('subscribe', { case_id: record.case_id });
}

function note(message) {
  const item = document.createElement('li');
  item.className = 'note';
  item.textContent = message;
  $('trail').append(item);
}

// --- rendering ---------------------------------------------------------------

function render(record) {
  $('case-id').textContent = record.case_id;
  $('job-status').textContent = record.status;
  $('job-status').className = `pill pill-${record.status}`;
  $('doc-count').textContent = `${record.document_count} document(s) presented`;

  $('trail').replaceChildren(
    ...record.events.map((event) => {
      const item = document.createElement('li');
      item.innerHTML =
        `<span class="stage">${event.stage}</span>` +
        (event.document_id ? `<span class="doc">${event.document_id}</span>` : '') +
        `<span class="msg"></span>`;
      item.querySelector('.msg').textContent = event.message;
      return item;
    }),
  );

  if (record.error) note(`${record.error_code}: ${record.error}`);
  $('raw').textContent = JSON.stringify(record, null, 2);
  if (record.result) renderReport(record.result);
}

const VERDICT = {
  clean: ['✓', 'Clean — nothing to refuse on'],
  needs_review: ['!', 'Needs review — an examiner should look'],
  blocked: ['✕', 'Blocked — the bank would refuse'],
};

function renderReport(result) {
  show($('report-panel'));
  const [mark, label] = VERDICT[result.status] ?? ['?', result.status];
  $('verdict').className = `verdict verdict-${result.status}`;
  $('verdict').textContent = `${mark}  ${label}`;
  $('summary').textContent = result.report.summary;

  $('mismatches').replaceChildren(
    ...result.report.mismatches.map((mismatch) => {
      const card = document.createElement('article');
      card.className = `finding finding-${mismatch.severity}`;
      card.append(
        el('div', 'finding-head', `${mismatch.severity} · ${mismatch.field}`),
        el('p', '', mismatch.explanation),
      );
      if (mismatch.rule_reference) card.append(el('div', 'rule', mismatch.rule_reference));
      if (mismatch.suggested_action) card.append(el('div', 'action', mismatch.suggested_action));
      if (mismatch.observations?.length) {
        const list = document.createElement('ul');
        list.className = 'observations';
        for (const o of mismatch.observations) {
          list.append(el('li', '', `${o.document_type}: ${o.field} = ${o.value ?? '—'}`));
        }
        card.append(list);
      }
      return card;
    }),
  );
  if (!result.report.mismatches.length) {
    $('mismatches').append(el('p', 'hint', 'No discrepancies were raised.'));
  }

  $('documents').replaceChildren(
    ...result.documents.map((doc) => {
      const row = el('div', `doc-row ${doc.payload ? '' : 'doc-failed'}`, '');
      row.append(
        el('code', '', doc.document_id),
        el('span', 'doc-type', doc.document_type),
        el('span', 'hint', doc.payload
          ? `confidence ${doc.confidence.toFixed(2)}${doc.page_count ? ` · ${doc.page_count} page(s)` : ''}`
          : doc.error ?? 'not extracted'),
      );
      return row;
    }),
  );
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

// --- wiring ------------------------------------------------------------------

function addDocumentBox(value = '') {
  const box = document.createElement('textarea');
  box.rows = 5;
  box.spellcheck = false;
  box.placeholder = 'Paste one document here — the credit, the invoice, the bill of lading…';
  box.value = value;
  $('text-docs').append(box);
  return box;
}

document.querySelectorAll('.tab').forEach((tab) =>
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('is-active', t === tab));
    show($('mode-text'), tab.dataset.mode === 'text');
    show($('mode-files'), tab.dataset.mode === 'files');
  }),
);

$('add-doc').addEventListener('click', () => addDocumentBox());
$('clear-docs').addEventListener('click', () => {
  $('text-docs').replaceChildren();
  [1, 2, 3].forEach(() => addDocumentBox());
});
$('load-sample').addEventListener('click', () => {
  $('text-docs').replaceChildren();
  SAMPLE.documents.forEach((text) => addDocumentBox(text));
  $('presented-on').value = SAMPLE.presented_on;
});
$('submit').addEventListener('click', submit);

[1, 2, 3].forEach(() => addDocumentBox());
$('api-base').textContent = API || location.origin;

// Health doubles as a reachability check, and says whether scans can be read at all.
fetch(api('/health'))
  .then((r) => r.json())
  .then((health) => {
    const pill = $('api-health');
    pill.textContent = health.ocr_available ? 'up · OCR ready' : 'up · text only';
    pill.className = 'pill pill-succeeded';
    if (!health.ocr_available) {
      $('ocr-warning').textContent =
        'OCR is off on this deployment, so uploads will be refused. Use "As text" instead.';
    }
  })
  .catch(() => {
    $('api-health').textContent = 'unreachable';
    $('api-health').className = 'pill pill-failed';
  });
