"""Functional fixture drill for exception resolution and durable delivery recovery."""
import argparse
import asyncio
from datetime import UTC, datetime
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time
import uuid

import httpx
import psycopg2
from temporalio.client import Client
from temporalio.worker import Replayer

from delivery_service.app import main as delivery
from delivery_service.app.models import DeliveryContext, DeliveryRequest
from delivery_service.app.providers import build_provider
from delivery_service.app.repository import DeliveryRepository
from ingestion_service.app.repository import IngestionRepository
from ingestion_service.app.reprocessing import request_fingerprint
from shared.idp_common.config import get_settings
from shared.idp_common.operator_auth import Operator
from shared.idp_common.storage import download_json, download_verified_bytes, upload_bytes
from scripts.verify_live_milestone import FIELDS, annotate, review_snapshot
from workflow_orchestrator.app.workflows import DocumentPipelineWorkflow

OUT = Path('artifacts/recovery-delivery')
INTERNAL = {'X-IDP-Internal-Key': 'local-verification-internal'}
OPERATOR = {'X-IDP-Operator-Key': 'local-verification-operator', 'X-Tenant-Id': 'verification'}
INTAKE = {'X-API-Key': 'local-verification-intake', 'X-Tenant-Id': 'verification'}
MODES = {}
TARGET = 'http://ingestion-service:18999/results'


def connection():
    settings = get_settings()
    return psycopg2.connect(host=settings.postgres_host, port=settings.postgres_port, user=settings.postgres_user,
                            password=settings.postgres_password, dbname=settings.postgres_db)


class Receiver(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        signature = 'sha256=' + hmac.new(b'local-verification-webhook-secret', body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(self.headers.get('X-IDP-Signature-256', ''), signature):
            self.send_error(401)
            return
        envelope = json.loads(body)
        case = envelope.get('metadata', {}).get('fixture_case', '')
        if MODES.get(case) == 'offline':
            self.send_error(503)
            return
        conn = connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    digest = hashlib.sha256(body).hexdigest()
                    cur.execute("""INSERT INTO verification_webhook_effects(tenant_id,event_id,body_hash)
                                   VALUES (%s,%s,%s) ON CONFLICT(tenant_id,event_id)
                                   DO UPDATE SET posts=verification_webhook_effects.posts+1 RETURNING body_hash""",
                                (envelope['tenant_id'], self.headers['Idempotency-Key'], digest))
                    assert cur.fetchone()[0] == digest, 'Retry changed the receiver envelope'
        finally:
            conn.close()
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b'acknowledged')


def register(ledger, case, providers=None):
    job = 'delivery-fixture-' + uuid.uuid4().hex[:12]
    request = DeliveryRequest(job_id=job, tenant_id='verification', payload={'approved_value': 42},
        approval_status='human_approved', destinations=providers or ['webhook'], webhook_url=TARGET,
        idempotency_key=uuid.uuid4().hex, metadata={'fixture_case': case})
    operation = delivery._delivery_id(request, request.destinations)
    envelope = {'schema_version': '1.0', 'delivery_id': operation, 'job_id': job, 'tenant_id': 'verification',
                'payload': request.payload, 'approval_status': request.approval_status, 'metadata': request.metadata,
                'redacted_fields': [], 'prepared_at': datetime.now(UTC).isoformat()}
    fingerprint = hashlib.sha256(delivery._stable_json({'request': request.model_dump(mode='json'),
                    'providers': request.destinations, 'target_url': TARGET}).encode()).hexdigest()
    ledger.register('verification', operation, job, 'delivery', fingerprint, request.model_dump(mode='json'), envelope, request.destinations, TARGET)
    return job, operation, request


async def child(args):
    settings = get_settings()
    spec = json.loads(Path(args.spec).read_text())
    if args.phase == 'reprocess-crash':
        repo = IngestionRepository(settings)
        parent = repo.get_job_by_id('verification', spec['parent'])
        download_verified_bytes(settings, parent['artifact_bucket'], parent['artifact_key'], parent['sha256'], parent['size_bytes'])
        repo.create_reprocess_job('verification', spec['parent'], spec['child'], spec['key'], spec['fingerprint'], spec['actor'], spec['reason'])
        os._exit(86)
    ledger = DeliveryRepository(settings)
    work = ledger.claim(120, 'verification', spec['operation'])
    assert work
    Path(args.spec + '.lease').write_text(json.dumps(work, default=str))
    if args.phase == 'after-send':
        row = ledger.get('verification', spec['operation'])
        await build_provider(work['provider']).deliver(DeliveryContext(settings, DeliveryRequest(**row['request']), row['operation_id'], row['envelope'], row['target_url']))
    elif args.phase == 'after-receipt':
        assert work['provider'] == '__receipt__'
        delivery._project_receipt(ledger.get('verification', spec['operation']))
    os._exit(86)


async def main(phase):
    OUT.mkdir(parents=True, exist_ok=True)
    report_path = OUT / 'report.json'
    report = json.loads(report_path.read_text()) if report_path.exists() else {'passed': False, 'checks': []}
    report['passed'] = False
    report_path.write_text(json.dumps(report))
    settings = get_settings()
    repo = IngestionRepository(settings)
    temporal = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    async with httpx.AsyncClient(timeout=90) as http:
        for attempt in range(60):
            try:
                if (await http.get('http://ingestion-service:8000/health')).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        else:
            raise RuntimeError('Intake failed to become ready')
        if phase == 'prepare':
            report = {'passed': False, 'checks': [], 'parents': []}
            report_path.write_text(json.dumps(report))
            for _ in range(3):
                suffix = uuid.uuid4().hex
                data = Path('samples/documents/sample_invoice_001.png').read_bytes() + suffix.encode()
                response = await http.post('http://ingestion-service:8000/documents', headers={**INTAKE, 'Idempotency-Key': suffix},
                                           files={'file': ('invoice.png', data, 'image/png')})
                response.raise_for_status()
                job = response.json()['job_id']
                handle = temporal.get_workflow_handle('idp-verification-' + job)
                result = await asyncio.wait_for(handle.result(), 120)
                assert result['status'] == 'ocr_exception', result
                report['parents'].append({'job': job, 'suffix': suffix})
            report['checks'].append('real OCR outage through public intake created three durable exceptions')
        elif phase == 'reprocess':
            headers = {**OPERATOR, 'Idempotency-Key': uuid.uuid4().hex}
            first = report['parents'][0]['job']
            base = 'http://ingestion-service:8000/exceptions/'
            assert (await http.post(base + first + '/reprocess', headers=INTAKE, json={'reason': 'OCR engine restored'})).status_code == 401
            other = {'X-IDP-Operator-Key': 'local-verification-operator-other', 'X-Tenant-Id': 'other', 'Idempotency-Key': uuid.uuid4().hex}
            assert (await http.post(base + first + '/reprocess', headers=other, json={'reason': 'OCR engine restored'})).status_code == 404
            response = await http.post(base + first + '/reprocess', headers=headers, json={'reason': 'OCR engine restored'})
            assert response.status_code == 202, response.text
            child_id = response.json()['child_job_id']
            replay = await http.post(base + first + '/reprocess', headers=headers, json={'reason': 'OCR engine restored'})
            assert replay.json()['child_job_id'] == child_id and replay.json()['idempotent_replay']
            assert (await http.post(base + first + '/reprocess', headers=headers, json={'reason': 'Different resolution reason'})).status_code == 409
            children = [(first, child_id)]
            parent = report['parents'][1]['job']
            operator = Operator('verification', 'operator:' + hashlib.sha256(b'local-verification-operator').hexdigest()[:16])
            reason = 'Recover a committed resolution after process termination'
            spec = {'parent': parent, 'child': str(uuid.uuid4()), 'key': uuid.uuid4().hex,
                    'fingerprint': request_fingerprint(reason, operator), 'actor': operator.subject, 'reason': reason}
            path = OUT / 'reprocess-crash.json'
            path.write_text(json.dumps(spec))
            process = subprocess.run([sys.executable, '-m', 'scripts.verify_recovery_delivery', 'reprocess-crash', '--spec', str(path)], timeout=60)
            assert process.returncode == 86
            with repo._cursor() as (_, cur):
                cur.execute("UPDATE ingestion_jobs SET created_at=NOW()-INTERVAL '3 minutes' WHERE job_id=%s", (spec['child'],))
            replay = await http.post(base + parent + '/reprocess', headers={**OPERATOR, 'Idempotency-Key': spec['key']}, json={'reason': reason})
            assert replay.status_code == 202 and replay.json()['child_job_id'] == spec['child'], replay.text
            children.append((parent, spec['child']))
            for parent_id, child_job in children:
                handle = temporal.get_workflow_handle('idp-verification-' + child_job)
                for _ in range(30):
                    try:
                        await handle.describe()
                        break
                    except Exception:
                        await asyncio.sleep(1)
                snapshot = await review_snapshot(handle)
                await annotate(http, snapshot, 'Approve', FIELDS)
                result = await asyncio.wait_for(handle.result(), 180)
                assert result['status'] == 'delivered', result
                receipt = await asyncio.to_thread(download_json, settings, result['delivery']['delivery_bucket'], result['delivery']['delivery_receipt_key'])
                stored = next(item for item in receipt['destination_results'] if item['provider'] == 'object_storage')
                envelope = await asyncio.to_thread(download_json, settings, stored['bucket'], stored['key'])
                assert envelope['metadata']['reprocess_parent_job_id'] == parent_id
                for _ in range(20):
                    resolution = repo.get_reprocess_request('verification', parent_id)
                    if resolution['status'] == 'resolved':
                        break
                    await asyncio.sleep(1)
                assert resolution['status'] == 'resolved', resolution
                assert (await temporal.get_workflow_handle('idp-verification-' + parent_id).result())['status'] == 'ocr_exception'
                history = await handle.fetch_history()
                (OUT / (child_job + '.history.json')).write_text(history.to_json())
                await Replayer(workflows=[DocumentPipelineWorkflow]).replay_workflow(history)
            bad = repo.get_job_by_id('verification', report['parents'][2]['job'])
            await asyncio.to_thread(upload_bytes, settings, settings.raw_bucket, bad['artifact_key'], b'corrupt source', 'image/png')
            denied = await http.post(base + bad['job_id'] + '/reprocess', headers={**OPERATOR, 'Idempotency-Key': uuid.uuid4().hex}, json={'reason': 'Attempt with corrupted source'})
            assert denied.status_code == 409 and repo.get_reprocess_request('verification', bad['job_id']) is None, denied.text
            original = Path('samples/documents/sample_invoice_001.png').read_bytes() + report['parents'][2]['suffix'].encode()
            await asyncio.to_thread(upload_bytes, settings, settings.raw_bucket, bad['artifact_key'], original, 'image/png')
            report['checks'] += ['tenant-scoped operator authorization and immutable resolution replay/conflict',
                                'real OCR -> Label Studio approval -> revalidation -> durable delivery for a new generation',
                                'crash after resolution commit recovered source copy and one new Temporal execution',
                                'corrupt source stayed unresolved; original exception history preserved',
                                'both new delivery workflow histories replayed']
        elif phase == 'delivery':
            ledger = DeliveryRepository(settings)
            delivery.app.state.repository = ledger
            conn = connection()
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("""CREATE TABLE IF NOT EXISTS verification_webhook_effects (
                        tenant_id TEXT, event_id TEXT, body_hash TEXT, posts INT NOT NULL DEFAULT 1,
                        effects INT NOT NULL DEFAULT 1, PRIMARY KEY(tenant_id,event_id))""")
            finally:
                conn.close()
            server = ThreadingHTTPServer(('0.0.0.0', 18999), Receiver)
            Thread(target=server.serve_forever, daemon=True).start()
            try:
                for point in ('before-send', 'after-send', 'after-receipt'):
                    job, operation, request = register(ledger, point)
                    if point == 'after-receipt':
                        await delivery._process_due(ledger, 'verification', operation)
                    path = OUT / (operation + '.json')
                    path.write_text(json.dumps({'operation': operation}))
                    process = subprocess.run([sys.executable, '-m', 'scripts.verify_recovery_delivery', point, '--spec', str(path)], timeout=60)
                    assert process.returncode == 86, (point, process.returncode)
                    abandoned = json.loads(Path(str(path) + '.lease').read_text())
                    with ledger.cursor() as cur:
                        cur.execute("UPDATE delivery_work SET lease_until=NOW()-INTERVAL '1 second' WHERE tenant_id=%s AND operation_id=%s AND state='leased'", ('verification', operation))
                    assert not ledger.acknowledge(abandoned, {'status': 'success'}), 'Expired lease was accepted'
                    while await delivery._process_due(ledger, 'verification', operation):
                        pass
                    assert ledger.get('verification', operation)['state'] == 'success'
                    with ledger.cursor() as cur:
                        cur.execute('SELECT posts,effects FROM verification_webhook_effects WHERE tenant_id=%s AND event_id=%s', ('verification', operation))
                        received = cur.fetchone()
                        assert received['effects'] == 1
                        assert received['posts'] == (2 if point == 'after-send' else 1), (point, received)
                        cur.execute("SELECT outcome FROM delivery_attempts WHERE lease_token=%s", (abandoned['lease_token'],))
                        assert cur.fetchone()['outcome'] == 'unknown'
                    report['checks'].append(point + ' crash reconciled with frozen envelope and receiver deduplication')
                transport = httpx.ASGITransport(app=delivery.app)
                async with httpx.AsyncClient(transport=transport, base_url='http://delivery.local', timeout=60) as api:
                    _, operation, request = register(ledger, 'partial', ['object_storage', 'webhook'])
                    MODES['partial'] = 'offline'
                    responses = await asyncio.gather(*[api.post('/deliver', headers=INTERNAL, json=request.model_dump(mode='json')) for _ in range(4)])
                    assert all(response.status_code in (200, 202) for response in responses), [r.text for r in responses]
                    for _ in range(4):
                        with ledger.cursor() as cur:
                            cur.execute("UPDATE delivery_work SET next_attempt_at=NOW() WHERE tenant_id=%s AND operation_id=%s AND state='pending'", ('verification', operation))
                        await delivery._process_due(ledger, 'verification', operation)
                    row = ledger.get('verification', operation)
                    assert row['state'] == 'dead_letter', row
                    storage = next(work for work in row['work'] if work['provider'] == 'object_storage')
                    assert storage['state'] == 'acknowledged' and storage['attempt_count'] == 1
                    other = {**INTERNAL, 'X-IDP-Operator-Key': 'local-verification-operator-other'}
                    assert (await api.post('/delivery-queue/' + operation + '/redrive', headers=other, json={'reason': 'Destination repaired'})).status_code == 404
                    MODES['partial'] = 'online'
                    response = await api.post('/delivery-queue/' + operation + '/redrive', headers={**INTERNAL, **OPERATOR}, json={'reason': 'Destination repaired'})
                    assert response.status_code == 202, response.text
                    while await delivery._process_due(ledger, 'verification', operation):
                        pass
                    assert ledger.get('verification', operation)['state'] == 'success'
                    assert next(work for work in ledger.get('verification', operation)['work'] if work['provider'] == 'object_storage')['attempt_count'] == 1
                    conflict = await api.post('/deliver', headers=INTERNAL, json={**request.model_dump(mode='json'), 'payload': {'changed': True}})
                    assert conflict.status_code == 409
                report['checks'] += ['partial delivery failure exhausts bounded attempts and exposes tenant-scoped dead letter',
                                    'operator redrive retries only failed destination, preserves successful provider, and rejects changed envelope']
            finally:
                server.shutdown()
                server.server_close()
                ledger.close()
        elif phase == 'prepare-reconcile':
            ledger = DeliveryRepository(settings)
            _, operation, _ = register(ledger, 'restart-no-caller', ['object_storage'])
            report['restart_operation'] = operation
            assert ledger.get('verification', operation)['state'] == 'pending'
            ledger.close()
        elif phase == 'finish':
            ledger = DeliveryRepository(settings)
            for _ in range(60):
                row = ledger.get('verification', report['restart_operation'])
                if row['state'] == 'success':
                    break
                await asyncio.sleep(1)
            assert row['state'] == 'success', row
            metrics = await http.get('http://delivery-service:8017/metrics')
            assert metrics.status_code == 200 and 'idp_delivery_oldest_unfinished_age_seconds' in metrics.text
            report['checks'].append('delivery service restart drained committed work and projected receipt without caller retry; metrics exported')
            assert len(set(report['checks'])) == 12, 'Run every verification phase before marking the gate passed'
            report['passed'] = True
            ledger.close()
    repo.close()
    report_path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['prepare','reprocess','delivery','prepare-reconcile','finish','reprocess-crash','before-send','after-send','after-receipt'])
    parser.add_argument('--spec')
    args = parser.parse_args()
    asyncio.run(child(args) if args.spec else main(args.phase))
