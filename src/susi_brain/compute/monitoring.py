"""Live HTTP activity and histograms backed by a buffered daily request log."""

import asyncio
import math
import time
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

from starlette.requests import ClientDisconnect
from susi_brain.shared.observability.daily_log import BufferedDailyLog

INTERVALS = {'minute': (60, 60), 'ten_minutes': (600, 144), 'hour': (3600, 168), 'day': (86400, 30)}
MAX_BUCKETS = 1440
RECORD_FIELDS = {'id', 'instance_id', 'completion_id', 'request_id', 'method', 'path', 'started_at',
                 'finished_at', 'duration_seconds', 'status_code', 'phase', 'state', 'traffic',
                 'request_bytes', 'response_bytes', 'model_group', 'model', 'endpoint_id'}


def valid_record(value):
    """Ignore corrupt/incompatible archive rows without exposing arbitrary fields."""
    if not isinstance(value, dict) or set(value) != RECORD_FIELDS:
        return False
    try:
        return (all(type(value[key]) is int and value[key] > 0 for key in ('id', 'completion_id'))
                and all(type(value[key]) is int and value[key] >= 0 for key in ('request_bytes', 'response_bytes'))
                and isinstance(value['instance_id'], str) and isinstance(value['method'], str)
                and isinstance(value['path'], str)
                and value['phase'] == 'finished' and value['state'] in ('completed', 'failed', 'cancelled', 'disconnected')
                and value['traffic'] in ('api', 'interface', 'monitoring')
                and value['model_group'] in ('smart', 'small', 'speech', None)
                and (value['status_code'] is None or type(value['status_code']) is int and 100 <= value['status_code'] <= 599)
                and all(value[key] is None or isinstance(value[key], str) for key in ('request_id', 'model', 'endpoint_id'))
                and type(value['duration_seconds']) in (int, float) and math.isfinite(value['duration_seconds'])
                and value['duration_seconds'] >= 0
                and all(datetime.fromisoformat(value[key]).tzinfo is not None for key in ('started_at', 'finished_at')))
    except (ValueError, TypeError):
        return False


def timestamp(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


def relative_path(scope):
    path, root = scope.get('path', ''), scope.get('root_path', '')
    return path[len(root):] if root and path.startswith(root + '/') else path


class RequestMonitor:
    def __init__(self, capacity=10000, *, clock=None, wall_clock=None, log_directory=None):
        self.clock, self.wall_clock = clock or time.monotonic, wall_clock or time.time
        self.instance_id, self.started_at = str(uuid4()), self.wall_clock()
        self.capacity, self.lock = capacity, RLock()
        self.active, self.observed_since = {}, self.started_at
        self.received = self.finished = self.errors = self.cancelled = 0
        self.buckets = {name: {} for name in INTERVALS}
        self.log_store = BufferedDailyLog(log_directory, prefix='requests', capacity=capacity,
            clock=self.wall_clock, sequence_field='completion_id', validate=valid_record, on_restore=self._restore)

    def start(self):
        self.log_store.start()

    def close(self):
        self.log_store.close()

    def _restore(self, record):
        with self.lock:
            self.observed_since = min(self.observed_since, datetime.fromisoformat(record['started_at']).timestamp())
            self._aggregate(record, restoring=True)

    def _aggregate(self, record, restoring=False):
        finished_at = datetime.fromisoformat(record['finished_at']).timestamp()
        failed, cancelled = record['state'] == 'failed', record['state'] in ('cancelled', 'disconnected')
        for name, (width, _) in INTERVALS.items():
            start = int(finished_at // width) * width
            if restoring and start < (int(self.wall_clock() // width) - MAX_BUCKETS + 1) * width:
                continue
            buckets = self.buckets[name]
            if start not in buckets:
                for old in [key for key in buckets if key < start - (MAX_BUCKETS - 1) * width]:
                    del buckets[old]
                buckets[start] = dict.fromkeys(
                    ('requests', 'errors', 'cancelled', 'monitoring_requests', 'monitoring_errors', 'monitoring_cancelled'), 0)
            bucket = buckets[start]
            if len(buckets) > MAX_BUCKETS:
                del buckets[min(buckets)]
            for key, value in (('requests', 1), ('errors', int(failed)), ('cancelled', int(cancelled))):
                bucket[key] += value
                if record['traffic'] == 'monitoring':
                    bucket['monitoring_' + key] += value

    def begin(self, scope):
        with self.lock:
            self.received += 1
            path = relative_path(scope)
            traffic = ('monitoring' if path == '/api/v1/compute/monitoring' or path.startswith('/api/v1/compute/monitoring/')
                       else 'interface' if path.startswith(('/api/v1/compute/ui', '/api/v1/compute/docs'))
                       or path in ('/api/v1/compute/openapi.json', '/api/v1/compute/chat.js',
                                   '/api/v1/compute/audio.js', '/api/v1/compute/tts.js') else 'api')
            record = {'id': self.received, 'method': scope['method'], 'started_at': timestamp(self.wall_clock()),
                      'status_code': None, 'phase': 'processing', 'request_bytes': 0, 'response_bytes': 0,
                      'traffic': traffic, '_scope': scope, '_clock': self.clock(),
                      '_namespace': '/v1' if path == '/v1' or path.startswith('/v1/') else '/api',
                      '_documentation_asset': path.startswith('/api/v1/compute/docs-assets/'),
                      '_disconnected': False, '_body_finished': False}
            self.active[record['id']] = record
            return record

    def received_body(self, record, message):
        with self.lock:
            if message['type'] == 'http.request':
                record['request_bytes'] += len(message.get('body', b''))
            elif message['type'] == 'http.disconnect':
                record['_disconnected'] = True

    def response_started(self, record, message):
        with self.lock:
            record['status_code'] = message['status']
            headers = dict(message.get('headers', []))
            record['phase'] = 'streaming' if headers.get(b'content-type', b'').lower().startswith((b'text/event-stream', b'audio/')) else 'responding'

    def sent_body(self, record, message):
        with self.lock:
            record['response_bytes'] += len(message.get('body', b''))
            record['_body_finished'] = not message.get('more_body', False)

    def _public(self, record):
        scope = record['_scope']
        route = getattr(scope.get('route'), 'path', None)
        path = route or (('/api/v1/compute/docs-assets/{asset}' if record['_documentation_asset']
                        else record['_namespace'] + '/{unmatched}'))
        state = scope.get('state', {})
        target = state.get('compute_target', {})
        return {key: record[key] for key in ('id', 'method', 'started_at', 'status_code', 'phase',
                                            'request_bytes', 'response_bytes', 'traffic')} | {
            'instance_id': self.instance_id, 'completion_id': None, 'request_id': state.get('correlation_id'), 'path': path,
            'finished_at': None, 'duration_seconds': round(max(0, self.clock() - record['_clock']), 6),
            'state': 'active', 'model_group': target.get('group'), 'model': target.get('model'),
            'endpoint_id': target.get('endpoint_id'),
        }

    def finish(self, record, error=None):
        with self.lock:
            if record['id'] not in self.active:
                return
            finished_at = self.wall_clock()
            if record['_disconnected'] and not record['_body_finished'] or isinstance(error, (OSError, ClientDisconnect)):
                outcome = 'disconnected'
            elif isinstance(error, asyncio.CancelledError):
                outcome = 'cancelled'
            elif error is not None or not record['_body_finished'] or (record['status_code'] or 0) >= 400:
                outcome = 'failed'
            else:
                outcome = 'completed'
            self.finished += 1
            failed, cancelled = outcome == 'failed', outcome in ('cancelled', 'disconnected')
            self.errors += failed
            self.cancelled += cancelled
            public = self._public(record)
            public.update(state=outcome, phase='finished', finished_at=timestamp(finished_at))
            self.log_store.log(public, at=finished_at)
            del self.active[record['id']]
            self._aggregate(public)

    def _metadata(self):
        stored = self.log_store.metadata()
        return {'instance_id': self.instance_id, 'storage': stored['storage'], 'observed_since': timestamp(self.observed_since),
                'process_started_at': timestamp(self.started_at),
                'generated_at': timestamp(self.wall_clock()), 'log_capacity': self.capacity,
                'retained_requests': stored['retained_entries'], 'evicted_requests': stored['evicted_entries'],
                'cached_requests': stored['cache_entries'], 'log_status': {key: stored[key] for key in
                    ('buffered_entries', 'flush_interval_seconds', 'last_flush_at', 'error', 'ignored_lines')}}

    def activity(self, limit=100, exclude_monitoring=False):
        with self.lock:
            records = [record for record in self.active.values() if not exclude_monitoring or record['traffic'] != 'monitoring']
            return self._metadata() | {
                'totals': {'received': self.received, 'finished': self.finished, 'active': len(self.active),
                           'errors': self.errors, 'cancelled': self.cancelled},
                'active_count': len(records), 'truncated': len(records) > limit,
                'requests': [self._public(record) for record in records[:limit]],
                'exclude_monitoring': exclude_monitoring,
            }

    def log(self, limit=100, before=None, exclude_monitoring=False, method=None, status_code=None, path=None,
            q=None, since=None, until=None):
        result = self.log_store.search(limit=limit, before=before, query=q,
            filters={key: value for key, value in {'method': method, 'status_code': status_code, 'path': path}.items()
                     if value is not None}, exclude={'traffic': 'monitoring'} if exclude_monitoring else None,
            since=since, until=until)
        with self.lock:
            return self._metadata() | {'requests': [entry['data'] for entry in result['entries']],
                'next_before': result['next_before'],
                'exclude_monitoring': exclude_monitoring}

    def histogram(self, interval='minute', buckets=None, exclude_monitoring=False):
        with self.lock:
            width, default = INTERVALS[interval]
            count, now = buckets or default, self.wall_clock()
            last = int(now // width) * width
            start = last - (count - 1) * width
            rows = []
            for position in range(count):
                lower = start + position * width
                stored = self.buckets[interval].get(lower, {})
                values = {key: stored.get(key, 0) - (stored.get('monitoring_' + key, 0) if exclude_monitoring else 0)
                          for key in ('requests', 'errors', 'cancelled')}
                rows.append({'start': timestamp(lower), 'end': timestamp(lower + width), **values,
                             'observed': lower + width > self.observed_since,
                             'partial': lower < self.observed_since or lower + width > now})
            return self._metadata() | {'interval': interval, 'bucket_seconds': width,
                'retention_buckets': MAX_BUCKETS, 'exclude_monitoring': exclude_monitoring,
                'totals': {key: sum(row[key] for row in rows) for key in ('requests', 'errors', 'cancelled')},
                'buckets': rows}


class RequestMonitoringMiddleware:
    def __init__(self, app, monitor):
        self.app, self.monitor = app, monitor

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        path = relative_path(scope)
        if not (path in ('/api', '/v1') or path.startswith(('/api/', '/v1/'))):
            return await self.app(scope, receive, send)
        record, error = self.monitor.begin(scope), None

        async def observed_receive():
            message = await receive()
            self.monitor.received_body(record, message)
            return message

        async def observed_send(message):
            if message['type'] == 'http.response.start':
                self.monitor.response_started(record, message)
            try:
                await send(message)
            except OSError:
                record['_disconnected'] = True
                raise
            if message['type'] == 'http.response.body':
                self.monitor.sent_body(record, message)

        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException as exc:
            error = exc
            raise
        finally:
            self.monitor.finish(record, error)
