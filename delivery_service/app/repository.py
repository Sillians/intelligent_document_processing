"""Authoritative delivery ledger with fenced work leases and immutable attempts."""
from contextlib import contextmanager
import uuid

from psycopg2 import pool
from psycopg2.extras import Json, RealDictCursor


class DeliveryRepository:
    def __init__(self, settings):
        self.max_attempts = settings.delivery_durable_max_attempts
        self._pool = pool.ThreadedConnectionPool(1, max(4, settings.delivery_max_inflight_requests + 2),
            host=settings.postgres_host, port=settings.postgres_port, user=settings.postgres_user,
            password=settings.postgres_password, dbname=settings.postgres_db)
        try:
            with self.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS delivery_operations (
                    tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL, job_id TEXT NOT NULL,
                    kind TEXT NOT NULL, fingerprint TEXT NOT NULL, request JSONB NOT NULL,
                    envelope JSONB NOT NULL, providers JSONB NOT NULL, target_url TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(tenant_id, operation_id))""")
                cur.execute("""CREATE TABLE IF NOT EXISTS delivery_work (
                    tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL, provider TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempt_count INT NOT NULL DEFAULT 0,
                    budget_used INT NOT NULL DEFAULT 0, next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    lease_token TEXT, lease_until TIMESTAMPTZ, result JSONB, last_error TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(tenant_id, operation_id, provider),
                    FOREIGN KEY(tenant_id, operation_id) REFERENCES delivery_operations ON DELETE CASCADE)""")
                cur.execute("""CREATE INDEX IF NOT EXISTS delivery_work_due_idx ON delivery_work(next_attempt_at)
                               WHERE state IN ('pending', 'leased')""")
                cur.execute("""CREATE TABLE IF NOT EXISTS delivery_attempts (
                    id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL, provider TEXT NOT NULL,
                    lease_token TEXT NOT NULL UNIQUE, attempt_number INT NOT NULL, outcome TEXT NOT NULL DEFAULT 'started',
                    result JSONB, error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), finished_at TIMESTAMPTZ,
                    FOREIGN KEY(tenant_id, operation_id, provider) REFERENCES delivery_work ON DELETE CASCADE)""")
                cur.execute("""CREATE TABLE IF NOT EXISTS delivery_operator_audit (
                    id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL, reason TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        except Exception:
            self._pool.closeall()
            raise

    @contextmanager
    def cursor(self):
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                yield cur
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def register(self, tenant, operation, job, kind, fingerprint, request, envelope, providers, target_url):
        with self.cursor() as cur:
            cur.execute("""INSERT INTO delivery_operations(tenant_id, operation_id, job_id, kind, fingerprint,
                           request, envelope, providers, target_url) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT DO NOTHING RETURNING operation_id""",
                        (tenant, operation, job, kind, fingerprint, Json(request), Json(envelope), Json(providers), target_url))
            created = cur.fetchone() is not None
            cur.execute("SELECT * FROM delivery_operations WHERE tenant_id = %s AND operation_id = %s FOR UPDATE", (tenant, operation))
            row = dict(cur.fetchone())
            if row['fingerprint'] != fingerprint or row['kind'] != kind or row['job_id'] != job:
                raise ValueError("Idempotency key is already bound to a different delivery envelope")
            if created:
                for provider in [*providers, '__receipt__']:
                    cur.execute("INSERT INTO delivery_work(tenant_id, operation_id, provider) VALUES (%s,%s,%s)", (tenant, operation, provider))
        return row, not created

    def get(self, tenant, operation, job=None):
        with self.cursor() as cur:
            cur.execute("""SELECT * FROM delivery_operations WHERE tenant_id = %s AND operation_id = %s
                           AND (%s IS NULL OR job_id = %s)""", (tenant, operation, job, job))
            row = cur.fetchone()
            if not row:
                return None
            row = dict(row)
            cur.execute("SELECT * FROM delivery_work WHERE tenant_id = %s AND operation_id = %s ORDER BY provider", (tenant, operation))
            row['work'] = [dict(item) for item in cur.fetchall()]
            row['state'] = ('dead_letter' if any(item['state'] == 'dead_letter' for item in row['work']) else
                            'success' if all(item['state'] == 'acknowledged' for item in row['work']) else 'pending')
            return row

    def claim(self, lease_seconds, tenant=None, operation=None):
        with self.cursor() as cur:
            cur.execute("""SELECT w.* FROM delivery_work w
                WHERE w.state IN ('pending','leased') AND w.next_attempt_at <= NOW()
                AND (w.lease_until IS NULL OR w.lease_until < NOW())
                AND (%s IS NULL OR w.tenant_id = %s) AND (%s IS NULL OR w.operation_id = %s)
                AND (w.provider != '__receipt__' OR NOT EXISTS (
                    SELECT 1 FROM delivery_work d WHERE d.tenant_id=w.tenant_id AND d.operation_id=w.operation_id
                    AND d.provider != '__receipt__' AND d.state != 'acknowledged'))
                ORDER BY w.next_attempt_at, w.provider LIMIT 1 FOR UPDATE OF w SKIP LOCKED""",
                        (tenant, tenant, operation, operation))
            row = cur.fetchone()
            if not row:
                return None
            row = dict(row)
            args = (row['tenant_id'], row['operation_id'], row['provider'])
            if row['lease_token']:
                cur.execute("UPDATE delivery_attempts SET outcome='unknown', finished_at=NOW() WHERE lease_token=%s AND outcome='started'", (row['lease_token'],))
            if row['budget_used'] >= self.max_attempts:
                cur.execute("""UPDATE delivery_work SET state='dead_letter', lease_token=NULL, lease_until=NULL,
                               last_error='Attempt budget exhausted after unknown outcome', updated_at=NOW()
                               WHERE tenant_id=%s AND operation_id=%s AND provider=%s""", args)
                return None
            token = uuid.uuid4().hex
            cur.execute("""UPDATE delivery_work SET state='leased', lease_token=%s, lease_until=NOW()+make_interval(secs=>%s),
                           attempt_count=attempt_count+1, budget_used=budget_used+1, updated_at=NOW()
                           WHERE tenant_id=%s AND operation_id=%s AND provider=%s RETURNING *""", (token, lease_seconds, *args))
            work = dict(cur.fetchone())
            cur.execute("""INSERT INTO delivery_attempts(tenant_id,operation_id,provider,lease_token,attempt_number)
                           VALUES (%s,%s,%s,%s,%s)""", (*args, token, work['attempt_count']))
            return work

    def acknowledge(self, work, result):
        with self.cursor() as cur:
            cur.execute("""UPDATE delivery_work SET state='acknowledged', result=%s, lease_token=NULL, lease_until=NULL,
                           last_error=NULL, updated_at=NOW() WHERE tenant_id=%s AND operation_id=%s AND provider=%s
                           AND state='leased' AND lease_token=%s AND lease_until > NOW() RETURNING provider""",
                        (Json(result), work['tenant_id'], work['operation_id'], work['provider'], work['lease_token']))
            if not cur.fetchone():
                return False
            cur.execute("UPDATE delivery_attempts SET outcome='acknowledged', result=%s, finished_at=NOW() WHERE lease_token=%s",
                        (Json(result), work['lease_token']))
            return True

    def fail(self, work, error, max_attempts, delay, permanent=False):
        with self.cursor() as cur:
            cur.execute("""UPDATE delivery_work SET state=CASE WHEN %s OR budget_used >= %s THEN 'dead_letter' ELSE 'pending' END,
                           lease_token=NULL, lease_until=NULL,
                           next_attempt_at=NOW()+make_interval(secs=>%s), last_error=%s, updated_at=NOW()
                           WHERE tenant_id=%s AND operation_id=%s AND provider=%s AND state='leased'
                           AND lease_token=%s AND lease_until > NOW() RETURNING state""",
                        (permanent, max_attempts, delay, error[:500], work['tenant_id'], work['operation_id'], work['provider'], work['lease_token']))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE delivery_attempts SET outcome=%s, error=%s, finished_at=NOW() WHERE lease_token=%s",
                            (row['state'], error[:500], work['lease_token']))

    def redrive(self, tenant, operation, actor, reason):
        with self.cursor() as cur:
            cur.execute("SELECT operation_id FROM delivery_operations WHERE tenant_id=%s AND operation_id=%s FOR UPDATE", (tenant, operation))
            if not cur.fetchone():
                return False
            cur.execute("""UPDATE delivery_work SET state='pending', budget_used=0, next_attempt_at=NOW(), updated_at=NOW()
                           WHERE tenant_id=%s AND operation_id=%s AND state='dead_letter' RETURNING provider""", (tenant, operation))
            if not cur.fetchall():
                raise ValueError("Only dead-letter destinations can be redriven")
            cur.execute("INSERT INTO delivery_operator_audit(tenant_id,operation_id,actor_id,reason) VALUES (%s,%s,%s,%s)", (tenant, operation, actor, reason))
            return True

    def queue(self, tenant, limit, offset):
        with self.cursor() as cur:
            cur.execute("""SELECT o.operation_id,o.job_id,o.kind,o.created_at,w.provider,w.state,w.attempt_count,w.last_error
                           FROM delivery_operations o JOIN delivery_work w USING(tenant_id,operation_id)
                           WHERE o.tenant_id=%s AND w.state != 'acknowledged' ORDER BY o.created_at
                           LIMIT %s OFFSET %s""", (tenant, limit, offset))
            return [dict(row) for row in cur.fetchall()]

    def backlog(self):
        with self.cursor() as cur:
            cur.execute("""SELECT COUNT(*) FILTER(WHERE state IN ('pending','leased')) AS pending,
                           COUNT(*) FILTER(WHERE state='dead_letter') AS dead_letter,
                           COALESCE(MAX(EXTRACT(EPOCH FROM (NOW()-o.created_at))) FILTER(WHERE state != 'acknowledged'),0) AS oldest
                           FROM delivery_work w JOIN delivery_operations o USING(tenant_id,operation_id)""")
            return dict(cur.fetchone())

    def close(self):
        self._pool.closeall()
