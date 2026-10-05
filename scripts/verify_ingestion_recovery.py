"""Live ingestion crash-window verification against the isolated IDP stack."""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from temporalio.client import Client
from shared.idp_common.config import get_settings
from shared.idp_common.storage import upload_bytes
from ingestion_service.app.repository import IngestionRepository, JobCreateInput
from ingestion_service.app.main import app, _reconcile_uploads, _dispatch_claimed
from workflow_orchestrator.app.workflows import DocumentPipelineWorkflow

OUT = Path('/app/artifacts/ingestion-recovery')


async def child(spec, point):
    settings = get_settings()
    repository = IngestionRepository(settings)
    data = Path('/app/samples/documents/sample_invoice_001.png').read_bytes() + spec['job_id'].encode()
    job = JobCreateInput(**spec)
    repository.create_job(job)
    with repository._cursor() as (_, cur):
        cur.execute("UPDATE ingestion_jobs SET created_at = NOW() - INTERVAL '16 minutes' WHERE job_id = %s", (job.job_id,))
    if point == 'registration':
        os._exit(86)
    key = f'tenants/{job.tenant_id}/jobs/{job.job_id}/raw/invoice.png'
    raw = b'corrupt' if point == 'corrupt-upload' else data
    upload_bytes(settings, settings.raw_bucket, key, raw, 'image/png')
    if point in {'upload', 'corrupt-upload'}:
        os._exit(86)
    repository.update_artifact(job.job_id, settings.raw_bucket, key, f's3://{settings.raw_bucket}/{key}')
    if point == 'outbox':
        os._exit(86)
    repository.claim_dispatch(job.job_id)
    client = await Client.connect(settings.temporal_address)
    await client.start_workflow(DocumentPipelineWorkflow.run,
        {'job_id': job.job_id, 'tenant_id': job.tenant_id, 'raw_bucket': settings.raw_bucket,
         'raw_key': key, 'review_wait_timeout_seconds': 60, 'review_poll_interval_seconds': 1},
        id=f'idp-{job.tenant_id}-{job.job_id}', task_queue=settings.temporal_task_queue)
    os._exit(86)


async def verify():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'report.json').write_text('{"passed": false}')
    settings = get_settings()
    repository = IngestionRepository(settings)
    app.state.temporal_client = await Client.connect(settings.temporal_address)
    app.state.temporal_lock = asyncio.Lock()
    checks = []
    for point in ('registration', 'upload', 'corrupt-upload', 'outbox', 'workflow-start'):
        job = 'recovery-' + uuid.uuid4().hex[:12]
        data = Path('/app/samples/documents/sample_invoice_001.png').read_bytes() + job.encode()
        spec = asdict(JobCreateInput(job, 'verification', 'api', job, hashlib.sha256(data).hexdigest(), 'invoice.png', 'image/png', len(data), {}))
        path = OUT / f'{job}.json'
        path.write_text(json.dumps(spec))
        process = subprocess.run([sys.executable, '-m', 'scripts.verify_ingestion_recovery', '--crash-point', point, '--spec', str(path)], timeout=120)
        assert process.returncode == 86, (point, process.returncode)
        await _reconcile_uploads(repository, settings)
        row = repository.get_job_by_id('verification', job)
        if point in {'registration', 'corrupt-upload'}:
            assert row['status'] == 'FAILED'
            assert row['error_code'] == ('RAW_UPLOAD_INCOMPLETE' if point == 'registration' else 'RAW_ARTIFACT_CORRUPT')
            assert repository.claim_dispatch(job) is None
        else:
            assert row['status'] == 'STORED'
            with repository._cursor() as (_, cur):
                cur.execute("UPDATE ingestion_dispatch_outbox SET lease_until = NOW() - INTERVAL '1 second' WHERE job_id = %s", (job,))
            first = repository.claim_dispatch(job)
            assert first
            assert repository.claim_dispatch(job) is None
            with repository._cursor() as (_, cur):
                cur.execute("UPDATE ingestion_dispatch_outbox SET lease_until = NOW() - INTERVAL '1 second' WHERE job_id = %s", (job,))
            second = repository.claim_dispatch(job)
            assert second['lease_token'] != first['lease_token']
            try:
                repository.mark_queued(job, 'stale-workflow', None, first['lease_token'])
                raise AssertionError('Stale acknowledgement accepted')
            except RuntimeError:
                pass
            repository.release_dispatch(job, 'stale-release', first['lease_token'])
            assert await _dispatch_claimed(repository, settings, second)
            assert repository.get_job_by_id('verification', job)['status'] == 'QUEUED'
            assert repository.claim_dispatch(job) is None
            handle = app.state.temporal_client.get_workflow_handle(f'idp-verification-{job}')
            description = await handle.describe()
            assert description.id == f'idp-verification-{job}'
            await handle.cancel()
        assert repository.get_job_by_id('other', job) is None
        audit = repository.get_audit_events('verification', job)
        events = [entry['event_type'] for entry in audit]
        assert events.count('job_registered') == 1, events
        if point in {'registration', 'corrupt-upload'}:
            assert events.count('job_failed') == 1, events
        else:
            assert events.count('artifact_persisted') == 1, events
            assert events.count('workflow_queued') == 1, events
        checks.append(point + ' crash recovered with tenant scope and lease fencing')
    report = {'passed': True, 'real_postgres_storage_temporal': True, 'checks': checks}
    (OUT / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    repository.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--crash-point')
    parser.add_argument('--spec', type=Path)
    args = parser.parse_args()
    asyncio.run(child(json.loads(args.spec.read_text()), args.crash_point) if args.crash_point else verify())
