"""Independent page stages and validated document aggregation."""
from statistics import mean
from fastapi import HTTPException

from temporalio import activity
from temporalio.exceptions import ApplicationError

from shared.idp_common.config import get_settings
from shared.idp_common.page_contract import validate_pages
from shared.idp_common.storage import download_json, upload_json
from shared.idp_common.tenant_scope import artifact_key, require_artifact_scope
from workflow_orchestrator.app.activities import _post


@activity.defn
async def ocr_page_activity(payload: dict) -> dict:
    return await _post("ocr", get_settings().ocr_url + "/ocr", payload)


@activity.defn
async def layout_page_activity(payload: dict) -> dict:
    return await _post("layout", get_settings().layout_url + "/layout", payload)


def aggregate_pages(payload: dict) -> dict:
    settings = get_settings()
    tenant, job = payload['tenant_id'], payload['job_id']
    sources = validate_pages(payload['pages'], tenant_id=tenant, job_id=job, key_field='preprocessed_key')
    records = payload['results']
    if len(records) != len(sources):
        raise ValueError('Incomplete page results')
    ocr_pages, layout_pages, tokens, blocks = [], [], [], []
    durations = {'ocr': 0.0, 'layout': 0.0}
    engines = []
    backends = []
    for number, (source, result) in enumerate(zip(sources, records), 1):
        if result['page_number'] != number:
            raise ValueError('Page results must be ordered and unique')
        loaded = {}
        for stage, bucket in [('ocr', settings.ocr_bucket), ('layout', settings.layout_bucket)]:
            response = result[stage]
            expected_key = artifact_key(tenant, job, f'{stage}/pages/{number:04d}.json')
            if response.get(stage + '_key') != expected_key or response.get(stage + '_bucket') != bucket:
                raise ValueError('Page artifact reference mismatch')
            require_artifact_scope(tenant, job, expected_key)
            document = download_json(settings, bucket, expected_key)
            if (document.get('tenant_id'), document.get('job_id'), document.get('page_slot')) != (tenant, job, number):
                raise ValueError('Page artifact identity mismatch')
            if document.get('schema_version') != '2.0' or len(document.get('pages', [])) != 1:
                raise ValueError('Invalid page artifact schema')
            page = document['pages'][0]
            if page.get('preprocessed_key') != source['preprocessed_key']:
                raise ValueError('Page source mismatch')
            loaded[stage] = document
            durations[stage] += float(response.get('duration_ms', 0))
        page = {**loaded['ocr']['pages'][0], 'page_number': number,
                'width': source.get('width'), 'height': source.get('height'),
                'coordinate_space': 'preprocessed-pixels',
                'native_text_key': source.get('native_text_key')}
        page['tokens'] = [{**token, 'page_number': number} for token in page['tokens']]
        ocr_pages.append(page)
        tokens.extend(page['tokens'])
        engines.append(loaded['ocr'].get('engine_config', {}))
        backends.append(loaded['ocr'].get('engine_backend', 'unknown'))
        layout = {**loaded['layout']['pages'][0], 'page_number': number}
        layout['blocks'] = [{**block, 'id': f'p{number:04d}-b{index:04d}', 'page_number': number,
                             'reading_order': len(blocks) + index} for index, block in enumerate(layout['blocks'])]
        blocks.extend(layout['blocks'])
        layout_pages.append(layout)
    confidence = mean(float(token.get('confidence', 0)) for token in tokens) if tokens else 0.0
    identity = {'schema_version': '2.0', 'tenant_id': tenant, 'job_id': job, 'page_count': len(sources)}
    ocr = {**identity, 'pages': ocr_pages, 'tokens': tokens, 'token_count': len(tokens),
           'full_text': '\n'.join(page['full_text'] for page in ocr_pages), 'mean_confidence': confidence,
           'fallback_used': any(page.get('secondary_engine') for page in ocr_pages), 'page_engines': engines}
    ocr['engine_backend'] = backends[0] if len(set(backends)) == 1 else 'mixed'
    layout = {**identity, 'pages': layout_pages, 'blocks': blocks, 'block_count': len(blocks),
              'text_zone_count': sum(bool(block.get('text')) for block in blocks),
              'full_text': '\n'.join(page['full_text'] for page in layout_pages),
              'image_width': layout_pages[0]['image_width'], 'image_height': layout_pages[0]['image_height']}
    layout['backend'] = layout_pages[0].get('backend', 'unknown') if len({page.get('backend') for page in layout_pages}) == 1 else 'mixed'
    outputs = {}
    for stage, document, bucket in [('ocr', ocr, settings.ocr_bucket), ('layout', layout, settings.layout_bucket)]:
        key = artifact_key(tenant, job, f'{stage}/{stage}.json')
        upload_json(settings, bucket, key, document)
        outputs[stage] = {**document, stage + '_bucket': bucket, stage + '_key': key, 'duration_ms': durations[stage]}
        # Keep token/block payloads in storage, not Temporal history.
        for name in ('tokens', 'blocks', 'pages', 'page_engines'):
            outputs[stage].pop(name, None)
    return outputs


@activity.defn
async def aggregate_pages_activity(payload: dict) -> dict:
    import asyncio
    try:
        return await asyncio.to_thread(aggregate_pages, payload)
    except (ValueError, KeyError, TypeError, HTTPException) as exc:
        raise ApplicationError(str(exc), type='PageContractError', non_retryable=True) from exc
