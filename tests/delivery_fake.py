"""Small in-memory ledger double; real SQL and lease tests use the live drill."""
from copy import deepcopy
from datetime import UTC, datetime
import time
import uuid


class MemoryLedger:
    def __init__(self):
        self.operations = {}
        self.sent_count = 0

    def register(self, tenant, operation, job, kind, fingerprint, request, envelope, providers, target_url):
        key = (tenant, operation)
        if key in self.operations:
            row = self.operations[key]
            if row['fingerprint'] != fingerprint or row['job_id'] != job:
                raise ValueError('Idempotency conflict')
            return row, True
        row = dict(tenant_id=tenant, operation_id=operation, job_id=job, kind=kind, fingerprint=fingerprint,
                   request=request, envelope=envelope, providers=providers, target_url=target_url,
                   created_at=datetime.now(UTC), work=[])
        for provider in [*providers, '__receipt__']:
            row['work'].append(dict(tenant_id=tenant, operation_id=operation, provider=provider,
                                    state='pending', attempt_count=0, budget_used=0, due=0, last_error=None, result=None))
        self.operations[key] = row
        return row, False

    def get(self, tenant, operation, job=None):
        row = self.operations.get((tenant, operation))
        if not row or (job is not None and row['job_id'] != job):
            return None
        row = deepcopy(row)
        row['state'] = ('dead_letter' if any(w['state'] == 'dead_letter' for w in row['work']) else
                        'success' if all(w['state'] == 'acknowledged' for w in row['work']) else 'pending')
        return row

    def claim(self, lease, tenant=None, operation=None):
        for row in self.operations.values():
            if tenant is not None and row['tenant_id'] != tenant or operation is not None and row['operation_id'] != operation:
                continue
            for work in row['work']:
                if work['state'] != 'pending' or work['due'] > time.monotonic():
                    continue
                if work['provider'] == '__receipt__' and any(w['state'] != 'acknowledged' for w in row['work'] if w['provider'] != '__receipt__'):
                    continue
                work.update(state='leased', lease_token=uuid.uuid4().hex, attempt_count=work['attempt_count'] + 1, budget_used=work['budget_used'] + 1)
                return deepcopy(work)

    def acknowledge(self, claim, result):
        work = next(w for w in self.operations[(claim['tenant_id'], claim['operation_id'])]['work'] if w['provider'] == claim['provider'])
        if work['state'] != 'leased' or work['lease_token'] != claim['lease_token']:
            return False
        work.update(state='acknowledged', result=result)
        self.sent_count += 1
        return True

    def fail(self, claim, error, maximum, delay, permanent=False):
        work = next(w for w in self.operations[(claim['tenant_id'], claim['operation_id'])]['work'] if w['provider'] == claim['provider'])
        work.update(state='dead_letter' if permanent or work['budget_used'] >= maximum else 'pending',
                    due=time.monotonic() + delay, last_error=error)
