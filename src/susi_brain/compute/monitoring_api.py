"""Read-only API schemas and routes for Compute's HTTP request monitor."""

from typing import Literal
from datetime import datetime

from fastapi import Query
from fastapi.responses import JSONResponse

from .monitoring import MAX_BUCKETS
from susi_brain.shared.http.errors import ServiceError

PREFIX = '/api/v1/compute/monitoring'
COUNT = {'type': 'integer', 'minimum': 0}
NULL_STRING = {'type': ['string', 'null']}
RECORD = {'type': 'object', 'required': ['id', 'instance_id', 'completion_id', 'request_id', 'method', 'path', 'started_at',
    'finished_at', 'duration_seconds', 'status_code', 'phase', 'state', 'traffic', 'request_bytes',
    'response_bytes', 'model_group', 'model', 'endpoint_id'], 'properties': {
    'id': {'type': 'integer', 'minimum': 1}, 'instance_id': {'type': 'string'},
    'completion_id': {'type': ['integer', 'null'], 'minimum': 1},
    'request_id': NULL_STRING, 'method': {'type': 'string'}, 'path': {'type': 'string'},
    'started_at': {'type': 'string'}, 'finished_at': NULL_STRING,
    'duration_seconds': {'type': 'number', 'minimum': 0},
    'status_code': {'type': ['integer', 'null'], 'minimum': 100, 'maximum': 599},
    'phase': {'type': 'string', 'enum': ['processing', 'responding', 'streaming', 'finished']},
    'state': {'type': 'string', 'enum': ['active', 'completed', 'failed', 'cancelled', 'disconnected']},
    'traffic': {'type': 'string', 'enum': ['api', 'interface', 'monitoring']},
    'request_bytes': COUNT, 'response_bytes': COUNT,
    'model_group': {'type': ['string', 'null'], 'enum': ['smart', 'small', 'speech', None]},
    'model': NULL_STRING, 'endpoint_id': NULL_STRING,
}}
META = {'instance_id': {'type': 'string'}, 'storage': {'type': 'string', 'enum': ['daily-jsonl', 'process-local']},
        'process_started_at': {'type': 'string'}, 'cached_requests': COUNT,
        'log_status': {'type': 'object', 'required': ['buffered_entries', 'flush_interval_seconds', 'last_flush_at', 'error', 'ignored_lines'],
                       'properties': {'buffered_entries': COUNT, 'flush_interval_seconds': {'type': 'number', 'exclusiveMinimum': 0},
                           'last_flush_at': NULL_STRING, 'error': {'type': ['string', 'null'], 'enum': ['append_failed', None]},
                           'ignored_lines': COUNT}},
        'observed_since': {'type': 'string'}, 'generated_at': {'type': 'string'}, 'log_capacity': COUNT,
        'retained_requests': COUNT, 'evicted_requests': COUNT, 'exclude_monitoring': {'type': 'boolean'}}
TOTALS = {'type': 'object', 'properties': {name: COUNT for name in ('requests', 'errors', 'cancelled')},
          'required': ['requests', 'errors', 'cancelled']}
ACTIVE = {'type': 'object', 'required': [*META, 'totals', 'active_count', 'truncated', 'requests'], 'properties': {
    **META, 'active_count': COUNT, 'truncated': {'type': 'boolean'},
    'totals': {'type': 'object', 'required': ['received', 'finished', 'active', 'errors', 'cancelled'],
               'properties': {name: COUNT for name in ('received', 'finished', 'active', 'errors', 'cancelled')}},
    'requests': {'type': 'array', 'items': RECORD},
}}
LOG = {'type': 'object', 'required': [*META, 'requests', 'next_before'], 'properties': {
    **META, 'requests': {'type': 'array', 'items': RECORD},
    'next_before': {'type': ['integer', 'null'], 'minimum': 1},
}}
HISTOGRAM = {'type': 'object', 'required': [*META, 'interval', 'bucket_seconds', 'retention_buckets', 'totals', 'buckets'],
    'properties': {**META, 'interval': {'type': 'string', 'enum': ['minute', 'ten_minutes', 'hour', 'day']},
    'bucket_seconds': {'type': 'integer', 'enum': [60, 600, 3600, 86400]},
    'retention_buckets': {'type': 'integer', 'const': MAX_BUCKETS}, 'totals': TOTALS,
    'buckets': {'type': 'array', 'items': {'type': 'object',
        'required': ['start', 'end', 'requests', 'errors', 'cancelled', 'observed', 'partial'],
        'properties': {'start': {'type': 'string'}, 'end': {'type': 'string'},
            **TOTALS['properties'], 'observed': {'type': 'boolean'}, 'partial': {'type': 'boolean'}}}},
}}


def response(schema):
    return {200: {'description': 'Request monitoring snapshot with durable daily-log metadata',
                  'content': {'application/json': {'schema': schema}}}}


def snapshot(value):
    return JSONResponse(value, headers={'Cache-Control': 'no-store'})


def install_monitoring_routes(app, monitor):
    @app.get(PREFIX + '/active', operation_id='compute__active_requests', tags=['Monitoring'], responses=response(ACTIVE))
    def active(
        limit: int = Query(default=100, ge=1, le=1000, description='Maximum active rows, oldest started first.'),
        exclude_monitoring: bool = Query(default=False, description='Hide requests to these monitoring APIs from the rows and active_count.'),
    ):
        """Read ongoing server-side HTTP API requests, including waiting-for-headers and full SSE/WAV/cleanup lifetimes. Covers /api and /v1 namespaces, unknown API routes, documentation/assets, validation errors and CORS preflights; non-API pages and pre-ASGI failures are excluded. Arrival id is scoped by each row's instance_id; request_id is the possibly reused correlation header, nullable for early CORS. Paths are redacted route templates. No body, query string, credentials, client address or actual document path is stored. Chat and TTS rows include selected group/model/endpoint ID; speech uses model_group speech. Input text and generated audio are never stored. Active rows and totals reset on restart; process_started_at identifies this process's beginning. The request log and reconstructed histogram survive restarts with the same log directory. log_status exposes unflushed rows, 60-second flush interval, last successful nonempty flush, append_failed or null, and invalid archive rows skipped at startup. totals includes all traffic even when rows are filtered. This GET itself is active unless excluded. active_count may exceed limit; truncated reports omissions. No inference or probes are triggered."""
        return snapshot(monitor.activity(limit, exclude_monitoring))

    @app.get(PREFIX + '/requests', operation_id='compute__request_log', tags=['Monitoring'], responses=response(LOG))
    def requests(
        limit: int = Query(default=100, ge=1, le=500, description='Maximum completed log rows.'),
        before: int | None = Query(default=None, ge=1, description='Exclusive completion_id cursor from next_before; persisted IDs remain valid after restart in the same log directory.'),
        exclude_monitoring: bool = Query(default=False, description='Hide monitoring API requests; recording still includes them.'),
        method: str | None = Query(default=None, min_length=1, max_length=16, pattern='^[A-Z]+$', description='Exact uppercase HTTP method filter.'),
        status_code: int | None = Query(default=None, ge=100, le=599, description='Exact HTTP status filter.'),
        path: str | None = Query(default=None, min_length=1, max_length=512, description='Exact API route template filter, not a concrete document path.'),
        q: str | None = Query(default=None, max_length=256, description='Case-insensitive literal substring of JSON request metadata; no regex or shell.'),
        since: datetime | None = Query(default=None, description='Inclusive log timestamp, ISO 8601 with timezone, for example 2026-10-25T00:00:00Z.'),
        until: datetime | None = Query(default=None, description='Exclusive log timestamp, ISO 8601 with timezone; must not precede since.'),
    ):
        """Search completed requests, newest completion_id first, across daily JSONL files and not-yet-flushed RAM rows. Each call to the shared BufferedDailyLog.log queues one row; its worker appends every 60 seconds using O_APPEND, without rewriting existing content. Only requests-YYYY-MM-DD.jsonl for the UTC day at flush time is appended; a batch crossing midnight belongs to the new day while event timestamps remain unchanged. Default request_log_directory is logs/compute, outside the public document directory. Shutdown drains the remainder; a crash can lose unflushed RAM rows. There is no automatic archive deletion. request_log_capacity (default 10000, 1..100000) bounds only the recent cache; retained_requests includes disk and buffered records, cached_requests reports the cache and evicted_requests is zero for durable storage. log_status reports flush health. One writer may own each directory; another writer fails startup. Completion IDs resume after the highest valid archived ID and remain stable for pagination across restarts; arrival id is scoped by the row's original instance_id. Search uses AND-combined exact filters, literal q and timezone-aware since inclusive/until exclusive; an invalid time range returns 422. next_before=null means no older matching rows. Disk searches run in a worker thread. Corrupt, oversized or incompatible rows are skipped at startup/search. History contains safe metadata only; no scopes, bodies, queries, credentials or raw exception text. Failed/cancelled/disconnected outcomes remain distinct. This GET is logged after its response completes; no provider calls or automatic generation retries occur."""
        if (since is not None and since.tzinfo is None or until is not None and until.tzinfo is None
                or since is not None and until is not None and since > until):
            raise ServiceError(422, 'invalid_time_range', 'Use timezone-aware timestamps with since no later than until.')
        return snapshot(monitor.log(limit, before, exclude_monitoring, method, status_code, path, q, since, until))

    @app.get(PREFIX + '/histogram', operation_id='compute__request_histogram', tags=['Monitoring'], responses=response(HISTOGRAM))
    def histogram(
        interval: Literal['minute', 'ten_minutes', 'hour', 'day'] = Query(default='minute', description='UTC-aligned buckets of 60, 600, 3600 or 86400 seconds.'),
        buckets: int | None = Query(default=None, ge=1, le=MAX_BUCKETS, description='Bucket count; omitted defaults to 60 minutes, 144 ten-minute buckets, 168 hours or 30 days.'),
        exclude_monitoring: bool = Query(default=False, description='Subtract monitoring API traffic from each bucket and totals.'),
    ):
        """Read zero-filled UTC completion buckets, oldest first, including the current partial bucket. requests counts all finished outcomes; errors counts failed HTTP/application/transport requests; cancelled counts cancellation/disconnection. Errors and cancelled are disjoint subsets. Startup reconstructs aggregates from valid daily-log records before serving requests, including older entries outside the RAM cache. Each resolution keeps at most 1440 buckets: one day of minutes, ten days of ten-minute buckets, sixty days of hours and 1440 days of days. Rebuilding streams archive files without retaining their entire contents. observed_since is the earliest restored request start, or this process's start without history; observed=false and partial=true identify time before this boundary/current bucket, not continuous server uptime. Active counters reset on restart; historical aggregates persist through the shared archive and no workers share active state. Requests still active enter a histogram only on completion. This GET is counted after returning; exclude_monitoring filters the view without disabling recording."""
        return snapshot(monitor.histogram(interval, buckets, exclude_monitoring))
