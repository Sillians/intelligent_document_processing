"""Exercise real Label Studio, Temporal, object storage and OCR in the verification stack."""
import argparse
import asyncio
import json
from pathlib import Path
import time
import uuid
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

import httpx
from temporalio.client import Client
from temporalio.worker import Replayer

from shared.idp_common.config import get_settings
from shared.idp_common.storage import ensure_bucket, upload_bytes, download_json, download_bytes
from workflow_orchestrator.app.workflows import DocumentPipelineWorkflow
from workflow_orchestrator.app.pipeline import build_review_payload, build_delivery_payload

OUT = Path('/app/artifacts/live-milestone')
HEADERS = {'X-IDP-Internal-Key': 'local-verification-internal'}
LS_HEADERS = {'Authorization': 'Token local-verification-labelstudio-token'}
REVIEW_HEADERS = {'X-IDP-Review-Key': 'local-verification-review'}
FIELDS = {'invoice_number': 'INV-REVIEWED-42', 'invoice_date': '2026-09-01',
          'total_amount': '42.00', 'currency': 'USD', 'vendor_name': 'Verified Supplier'}


async def ready(http):
    for url in ['http://label-studio:8080/health', 'http://ingestion-service:8000/health',
                'http://human-review-console:8016/health', 'http://ocr-service:8011/health']:
        deadline = time.monotonic() + 240
        while True:
            try:
                r = await http.get(url)
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f'Service not ready: {url}')
            await asyncio.sleep(2)


async def initialize(http):
    await ready(http)
    projects = await http.get('http://label-studio:8080/api/projects/', headers=LS_HEADERS)
    projects.raise_for_status()
    body = projects.json()
    entries = body if isinstance(body, list) else body.get('results', [])
    if not any(p['id'] == 1 for p in entries):
        xml = '<View><Text name="document" value="$job_id"/><Choices name="decision" toName="document" choice="single"><Choice value="Approve"/><Choice value="Reject"/></Choices><TextArea name="corrected_fields_json" toName="document"/></View>'
        response = await http.post('http://label-studio:8080/api/projects/', headers=LS_HEADERS,
                                   json={'title': 'IDP Live Verification', 'label_config': xml})
        response.raise_for_status()
        assert response.json()['id'] == 1
    settings = get_settings()
    for bucket in [settings.raw_bucket, settings.preprocessed_bucket, settings.ocr_bucket,
                   settings.layout_bucket, settings.extraction_bucket, settings.validation_bucket,
                   settings.review_bucket, settings.delivery_bucket]:
        await asyncio.to_thread(ensure_bucket, settings, bucket)


async def start(client, *, timeout=180, invalid=False):
    job = 'live-' + uuid.uuid4().hex[:16]
    settings = get_settings()
    key = f'tenants/verification/jobs/{job}/raw/invoice.png'
    data = b'invalid image' if invalid else Path('/app/samples/documents/sample_invoice_001.png').read_bytes()
    await asyncio.to_thread(upload_bytes, settings, settings.raw_bucket, key, data, 'image/png')
    handle = await client.start_workflow(DocumentPipelineWorkflow.run,
        {'job_id': job, 'tenant_id': 'verification', 'raw_bucket': settings.raw_bucket,
         'raw_key': key, 'review_poll_interval_seconds': 1, 'review_wait_timeout_seconds': timeout},
        id=f'idp-verification-{job}', task_queue=settings.temporal_task_queue)
    return handle


async def review_snapshot(handle):
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        try:
            snapshot = await handle.query(DocumentPipelineWorkflow.review_snapshot)
            if snapshot:
                return snapshot
        except Exception:
            pass
        await asyncio.sleep(1)
    raise RuntimeError('Workflow did not reach review: ' + handle.id)


async def annotate(http, snapshot, choice, fields):
    task = snapshot['review_task']
    response = await http.post(f"http://label-studio:8080/api/tasks/{task['review_ref']}/annotations/",
        headers=LS_HEADERS, json={'result': [
            {'from_name': 'decision', 'to_name': 'document', 'type': 'choices', 'value': {'choices': [choice]}},
            {'from_name': 'corrected_fields_json', 'to_name': 'document', 'type': 'textarea',
             'value': {'text': [json.dumps(fields)]}},
        ]})
    response.raise_for_status()
    event_url = f"http://human-review-console:8016/review/tasks/{snapshot['job_id']}/{task['review_task_id']}/label-studio-event?tenant_id=verification"
    denied = await http.post(event_url)
    assert denied.status_code == 401
    response = await http.post(event_url, headers=REVIEW_HEADERS)
    response.raise_for_status()
    decision = response.json()
    assert decision['status'] == 'decided' and decision['reviewer_id']
    replay = await http.post(event_url, headers=REVIEW_HEADERS)
    replay.raise_for_status()
    assert replay.json() == decision
    return decision


async def evidence(handle, report):
    history = await handle.fetch_history()
    (OUT / f'{handle.id}.history.json').write_text(history.to_json())
    await Replayer(workflows=[DocumentPipelineWorkflow]).replay_workflow(history)
    report.setdefault('replayed_workflows', []).append(handle.id)


async def main(phase):
    OUT.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=45) as http:
        client = await Client.connect(get_settings().temporal_address)
        if phase == 'prepare':
            (OUT / 'report.json').write_text(json.dumps({'passed': False, 'phase': 'preparing'}))
            await initialize(http)
            handle = await start(client)
            snapshot = await review_snapshot(handle)
            (OUT / 'pending.json').write_text(json.dumps({'workflow_id': handle.id, 'snapshot': snapshot}, indent=2))
            print(json.dumps({'phase': 'awaiting_worker_crash', 'workflow_id': handle.id}))
            return
        pending = json.loads((OUT / 'pending.json').read_text())
        if phase == 'secondary-recheck':
            previous = json.loads((OUT / 'secondary-ocr.json').read_text())
            response = await http.post('http://ocr-secondary:8011/ocr', headers=HEADERS,
                                      json={'job_id': previous['job_id'], 'tenant_id': 'verification',
                                            'preprocessed_key': previous['pages'][0]['preprocessed_key']})
            response.raise_for_status()
            assert response.json()['engine_backend'] == 'tesseract'
            settings = get_settings()
            artifact = await asyncio.to_thread(download_json, settings, response.json()['ocr_bucket'], response.json()['ocr_key'])
            assert artifact['engine_config']['secondary']['version']
            assert artifact['pages'][0]['primary_failure'] == 'OCR_UNAVAILABLE'
            (OUT / 'secondary-ocr.json').write_text(json.dumps(artifact, indent=2))
            print(json.dumps({'passed': True, 'check': 'real secondary engine version and primary failure provenance'}))
            return
        if phase == 'outage':
            handle = await start(client)
            result = await asyncio.wait_for(handle.result(), 180)
            assert result['status'] == 'ocr_exception', result
            job = result['job_id']
            response = await http.get(f'http://human-review-console:8016/review/ocr-exceptions/{job}?tenant_id=verification', headers=REVIEW_HEADERS)
            response.raise_for_status()
            assert response.json()['failure_code'] == 'OCR_UNAVAILABLE'
            denied = await http.get(f'http://human-review-console:8016/review/ocr-exceptions/{job}?tenant_id=other', headers=REVIEW_HEADERS)
            assert denied.status_code == 401
            report = json.loads((OUT / 'report.json').read_text())
            report['checks'].append('real OCR connection outage -> exhausted retries -> durable tenant exception')
            history = await handle.fetch_history()
            attempts = [event.activity_task_started_event_attributes.attempt for event in history.events
                        if event.HasField('activity_task_started_event_attributes')]
            assert 3 in attempts, 'Expected a live activity to exhaust three attempts'
            await evidence(handle, report)
            report['passed'] = True
            (OUT / 'report.json').write_text(json.dumps(report, indent=2))
            print(json.dumps(report, indent=2))
            return
        report = {'real_services': True, 'passed': False, 'checks': []}
        before = json.loads((OUT / 'worker-before-crash.json').read_text())
        after = json.loads((OUT / 'worker-after-crash.json').read_text())
        assert before['StartedAt'] != after['StartedAt'], 'Worker restart evidence is missing'
        handle = client.get_workflow_handle(pending['workflow_id'])
        snapshot = pending['snapshot']
        decision = await annotate(http, snapshot, 'Approve', FIELDS)
        result = await asyncio.wait_for(handle.result(), 180)
        assert result['status'] == 'delivered', result
        assert result['extraction']['fields']['invoice_number'] == FIELDS['invoice_number']
        delivery = result['delivery']
        settings = get_settings()
        receipt = await asyncio.to_thread(download_json, settings, delivery['delivery_bucket'], delivery['delivery_receipt_key'])
        for provider in receipt['destination_results']:
            if provider.get('provider') == 'object_storage':
                envelope = await asyncio.to_thread(download_json, settings, provider['bucket'], provider['key'])
                assert envelope['payload']['invoice_number'] == FIELDS['invoice_number']
                assert envelope['metadata']['reviewer_id'] == decision['reviewer_id']
        (OUT / 'approval-result.json').write_text(json.dumps(result, indent=2))
        (OUT / 'delivery-receipt.json').write_text(json.dumps(receipt, indent=2))
        assert decision['reviewer_id'] == str(result['review_decision']['reviewer_id'])
        report['checks'].append('worker crash recovery -> Label Studio approval -> revalidation -> delivery receipt')
        review_request = build_review_payload(snapshot['job_id'], snapshot['extraction'], snapshot['validation'], 'verification')
        replay_task = await http.post('http://human-review-console:8016/review/tasks', headers=HEADERS, json=review_request)
        replay_task.raise_for_status()
        assert replay_task.json()['review_ref'] == snapshot['review_task']['review_ref']
        delivery_request = {**build_delivery_payload(result['job_id'], result['extraction'], result['validation']), 'tenant_id': 'verification'}
        delivery_request['metadata'] = {**delivery_request.get('metadata', {}),
            'review_task_id': snapshot['review_task']['review_task_id'],
            'reviewer_id': decision['reviewer_id'], 'review_decision_key': decision['decision_key']}
        replay_delivery = await http.post('http://delivery-service:8017/deliver', headers=HEADERS, json=delivery_request)
        replay_delivery.raise_for_status()
        assert replay_delivery.json()['idempotent_replay']
        assert replay_delivery.json()['delivery_id'] == delivery['delivery_id']
        report['checks'].append('review task and delivery request replay preserve committed identities')
        await evidence(handle, report)
        for choice, fields, expected in [('Reject', {}, 'rejected'), ('Approve', {**FIELDS, 'total_amount': 'invalid'}, 'review_validation_failed')]:
            other = await start(client)
            snapshot = await review_snapshot(other)
            await annotate(http, snapshot, choice, fields)
            result = await asyncio.wait_for(other.result(), 180)
            assert result['status'] == expected, result
            assert 'delivery' not in result
            report['checks'].append(expected + ' without delivery')
            await evidence(other, report)
        timed = await start(client, timeout=60)
        await review_snapshot(timed)
        result = await asyncio.wait_for(timed.result(), 100)
        assert result['status'] == 'review_timed_out' and 'delivery' not in result
        report['checks'].append('review timeout without delivery')
        await evidence(timed, report)
        (OUT / 'report.json').write_text(json.dumps(report, indent=2))
        preprocessed = json.loads((OUT / 'approval-result.json').read_text())['preprocess']
        job = 'secondary-' + uuid.uuid4().hex[:16]
        source = await asyncio.to_thread(download_bytes, settings, settings.preprocessed_bucket, preprocessed['preprocessed_key'])
        key = f'tenants/verification/jobs/{job}/preprocess/page.png'
        await asyncio.to_thread(upload_bytes, settings, settings.preprocessed_bucket, key, source, 'image/png')
        response = await http.post('http://ocr-secondary:8011/ocr', headers=HEADERS,
                                  json={'job_id': job, 'tenant_id': 'verification',
                                        'preprocessed_key': key})
        assert response.status_code == 422 and response.json()['detail']['code'] == 'OCR_SECONDARY_REJECTED', response.text
        report['checks'].append('real secondary low-confidence output rejected')
        image = Image.new('RGB', (1200, 500), 'white')
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=40)
        for number, line in enumerate(['Invoice Number: INV-SECONDARY-42', 'Supplier: Verified Supplier', 'Total Amount: 42.00', 'Currency: USD']):
            draw.text((50, 40 + number * 80), line, font=font, fill='black')
        buffer = BytesIO()
        image.save(buffer, format='PNG')
        await asyncio.to_thread(upload_bytes, settings, settings.preprocessed_bucket, key, buffer.getvalue(), 'image/png')
        response = await http.post('http://ocr-secondary:8011/ocr', headers=HEADERS,
                                  json={'job_id': job, 'tenant_id': 'verification', 'preprocessed_key': key})
        response.raise_for_status()
        secondary = response.json()
        assert secondary['fallback_used'] and secondary['token_count'] > 0
        artifact = await asyncio.to_thread(download_json, settings, secondary['ocr_bucket'], secondary['ocr_key'])
        assert artifact['pages'][0]['secondary_engine']['backend'] == 'tesseract'
        (OUT / 'secondary-ocr.json').write_text(json.dumps(artifact, indent=2))
        report['checks'].append('real Tesseract secondary after unavailable Paddle, validated output persisted')
        (OUT / 'report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['prepare', 'finish', 'outage', 'secondary-recheck'])
    asyncio.run(main(parser.parse_args().phase))
