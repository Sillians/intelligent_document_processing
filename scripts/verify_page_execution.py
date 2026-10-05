"""Real two-page pipeline with isolated page retry across worker restart."""
import asyncio
from collections import Counter
from io import BytesIO
import json
from pathlib import Path
import uuid

import httpx
from PIL import Image
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from shared.idp_common.config import get_settings
from shared.idp_common.storage import download_json, upload_bytes
from shared.idp_common.tenant_scope import artifact_key
from workflow_orchestrator.app import activities
from workflow_orchestrator.app.page_execution import ocr_page_activity, layout_page_activity, aggregate_pages_activity
from workflow_orchestrator.app.workflows import DocumentPipelineWorkflow
from scripts.verify_live_milestone import FIELDS, annotate, initialize, review_snapshot


async def main():
    output = Path('artifacts/page-execution')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'report.json').write_text(json.dumps({'passed':False}))
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as http:
        for _ in range(90):
            try:
                await initialize(http)
                break
            except httpx.HTTPError:
                await asyncio.sleep(2)
        else:
            raise RuntimeError('Verification dependencies did not become ready')
    counts = Counter()
    failed = asyncio.Event()

    @activity.defn(name='ocr_page_activity')
    async def retry_once(payload):
        counts[payload['page_slot']] += 1
        if payload['page_slot'] == 2 and counts[2] == 1:
            failed.set()
            raise ApplicationError('Controlled page-two transient failure', type='OCR_PAGE_TEST', non_retryable=False)
        return await ocr_page_activity(payload)

    registered = [getattr(activities, name) for name in (
        'preprocess_activity', 'ocr_activity', 'layout_activity', 'classify_activity', 'extract_activity',
        'validate_activity', 'create_review_task_activity', 'poll_review_activity', 'record_ocr_exception_activity',
        'deliver_activity', 'poll_delivery_activity', 'evaluate_activity', 'notify_webhook_activity')]
    registered += [retry_once, layout_page_activity, aggregate_pages_activity]
    job = 'pages-' + uuid.uuid4().hex
    queue = 'page-verification-' + job
    with Image.open('samples/documents/sample_invoice_001.png') as original:
        image = original.convert('RGB')
        data = BytesIO()
        image.save(data, format='TIFF', save_all=True, append_images=[image.copy()])
    key = artifact_key('verification', job, 'raw/two-pages.tiff')
    await asyncio.to_thread(upload_bytes, settings, settings.raw_bucket, key, data.getvalue(), 'image/tiff')
    client = await Client.connect(settings.temporal_address)
    async with Worker(client, task_queue=queue, workflows=[DocumentPipelineWorkflow], activities=registered):
        handle = await client.start_workflow(DocumentPipelineWorkflow.run,
            {'tenant_id':'verification', 'job_id':job, 'raw_bucket':settings.raw_bucket, 'raw_key':key,
             'review_poll_interval_seconds':1, 'review_wait_timeout_seconds':300, 'delivery_poll_interval_seconds':1},
            id=job, task_queue=queue)
        await asyncio.wait_for(failed.wait(), 180)
    # A new worker must reuse successful page-one activity history.
    async with Worker(client, task_queue=queue, workflows=[DocumentPipelineWorkflow], activities=registered):
        snapshot = await asyncio.wait_for(review_snapshot(handle), 180)
        async with httpx.AsyncClient(timeout=30) as http:
            await annotate(http, snapshot, 'Approve', FIELDS)
        result = await asyncio.wait_for(handle.result(), 180)
    assert result['status'] == 'delivered', result
    assert counts == {1:1, 2:2}, counts
    for stage in ('ocr', 'layout'):
        document = await asyncio.to_thread(download_json, settings, result[stage][stage + '_bucket'], result[stage][stage + '_key'])
        assert document['page_count'] == 2
        assert [page['page_number'] for page in document['pages']] == [1,2]
        (output / (stage + '.json')).write_text(json.dumps(document, indent=2))
    history = await handle.fetch_history()
    (output / (job + '.history.json')).write_text(history.to_json())
    report = {'passed':True, 'job_id':job, 'ocr_attempts':dict(counts),
        'checks':['page-two transient retry after worker restart does not rerun page one',
                  'real OCR/layout aggregation preserves two ordered pages',
                  'real Label Studio correction revalidates before acknowledged delivery'],
        'evidence_scope':'Functional synthetic fixture; not representative accuracy or process-SIGKILL evidence'}
    (output / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
