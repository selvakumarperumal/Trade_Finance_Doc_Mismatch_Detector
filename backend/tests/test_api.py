"""The API as a caller meets it: upload, follow, collect, and every way to be refused."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import PNG, StubTextract, make_pdf

TERMINAL = {'succeeded', 'failed', 'cancelled'}


def upload(client: TestClient, *files, **data):
    """POST a multipart submission, one part per file."""
    return client.post('/v1/cases/uploads', files=[('files', f) for f in files], data=data)


def drain(client: TestClient, case_id: str) -> dict:
    """Follow a case to its end over the websocket, returning the final record."""
    with client.websocket_connect(f'/v1/cases/{case_id}/stream') as ws:
        while (record := ws.receive_json()).get('status') not in TERMINAL:
            pass
        return record


# --- the flow a person clicking submit goes through --------------------------


def test_uploading_several_files_starts_one_case(client: TestClient) -> None:
    response = upload(
        client,
        ('credit.pdf', make_pdf(3), 'application/pdf'),
        ('invoice.pdf', make_pdf(1), 'application/pdf'),
        ('bol.png', PNG, 'image/png'),
        presented_on='2026-03-02',
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body['status'] == 'queued'
    assert body['document_count'] == 3

    final = drain(client, body['case_id'])
    assert final['status'] == 'succeeded'
    assert {e['stage'] for e in final['events']} >= {'ingest', 'ocr', 'classify'}
    assert len(final['result']['documents']) == 3


def test_every_page_of_a_multipage_pdf_is_read(
    client: TestClient, stub_textract: StubTextract
) -> None:
    """Textract's synchronous read is single-page, so a 4-page credit is four calls."""
    case_id = upload(client, ('credit.pdf', make_pdf(4), 'application/pdf')).json()['case_id']
    final = drain(client, case_id)

    assert stub_textract.calls == 4
    assert final['result']['documents'][0]['page_count'] == 4


def test_polling_returns_the_same_record_the_socket_sends(client: TestClient) -> None:
    case_id = upload(client, ('invoice.pdf', make_pdf(1), 'application/pdf')).json()['case_id']
    streamed = drain(client, case_id)
    polled = client.get(f'/v1/cases/{case_id}').json()

    assert polled == streamed
    assert polled['status'] == 'succeeded'
    assert polled['result'] is not None
    assert polled['events']


def test_connecting_late_replays_the_whole_run(client: TestClient) -> None:
    """The socket opens after the POST returns, so nothing may depend on the timing."""
    case_id = upload(client, ('invoice.pdf', make_pdf(2), 'application/pdf')).json()['case_id']
    client.get(f'/v1/cases/{case_id}')  # let the run finish first

    final = drain(client, case_id)
    assert final['events'], 'a finished case must still replay its trail'
    assert final['result'] is not None


def test_deleting_a_case_drops_what_it_held(client: TestClient) -> None:
    case_id = upload(client, ('invoice.pdf', make_pdf(1), 'application/pdf')).json()['case_id']
    drain(client, case_id)

    assert client.delete(f'/v1/cases/{case_id}').status_code == 204
    assert client.get(f'/v1/cases/{case_id}').status_code == 404


def test_text_submissions_take_the_same_path(client: TestClient) -> None:
    response = client.post(
        '/v1/cases',
        json={'documents': [{'text': 'COMMERCIAL INVOICE 1'}, {'text': 'BILL OF LADING 2'}]},
    )
    assert response.status_code == 202
    assert drain(client, response.json()['case_id'])['status'] == 'succeeded'


# --- being refused -----------------------------------------------------------


def test_too_many_files_is_refused_before_they_are_read(client: TestClient) -> None:
    pdf = make_pdf(1)
    response = upload(client, *[(f'f{i}.pdf', pdf, 'application/pdf') for i in range(4)])
    assert response.status_code == 422
    assert response.json()['code'] == 'invalid_case'


@pytest.mark.parametrize(
    ('name', 'content', 'status_code', 'code'),
    [
        ('empty.pdf', b'', 422, 'invalid_case'),
        ('renamed.pdf', b'MZ\x90\x00' + b'x' * 100, 415, 'unsupported_media_type'),
        ('huge.pdf', make_pdf(1, pad=5_000), 413, 'upload_too_large'),
    ],
)
def test_unusable_uploads_say_what_is_wrong(
    client: TestClient, name: str, content: bytes, status_code: int, code: str
) -> None:
    response = upload(client, (name, content, 'application/pdf'))
    assert response.status_code == status_code
    assert response.json()['code'] == code


def test_the_case_wide_size_limit_is_enforced_across_files(client: TestClient) -> None:
    """Each file is under the per-file limit; together they are over the per-case one."""
    big = make_pdf(1, pad=3_000)
    response = upload(client, *[(f'b{i}.pdf', big, 'application/pdf') for i in range(3)])
    assert response.status_code == 413


def test_a_browser_mislabelling_a_pdf_is_still_accepted(client: TestClient) -> None:
    assert upload(client, ('credit.pdf', make_pdf(2), 'application/octet-stream')).status_code == 202


def test_reusing_a_case_id_is_a_conflict(client: TestClient) -> None:
    body = {'case_id': 'case-fixed', 'documents': [{'text': 'INVOICE'}]}
    assert client.post('/v1/cases', json=body).status_code == 202

    clash = client.post('/v1/cases', json=body)
    assert clash.status_code == 409
    assert clash.json()['code'] == 'case_exists'

    client.delete('/v1/cases/case-fixed')
    assert client.post('/v1/cases', json=body).status_code == 202


def test_two_documents_may_not_share_an_id(client: TestClient) -> None:
    response = client.post(
        '/v1/cases',
        json={'documents': [{'document_id': 'a', 'text': 'x'}, {'document_id': 'a', 'text': 'y'}]},
    )
    assert response.status_code == 422


def test_an_unknown_case_is_a_404(client: TestClient) -> None:
    response = client.get('/v1/cases/ghost')
    assert response.status_code == 404
    assert response.json()['code'] == 'case_not_found'


def test_the_stream_reports_an_unknown_case_rather_than_hanging(client: TestClient) -> None:
    with client.websocket_connect('/v1/cases/ghost/stream') as ws:
        assert ws.receive_json() == {
            'code': 'case_not_found',
            'detail': "no case 'ghost'; it may have expired",
        }


def test_health_reports_whether_scans_can_be_read(client: TestClient) -> None:
    body = client.get('/health').json()
    assert body['status'] == 'ok'
    assert body['ocr_available'] is True
