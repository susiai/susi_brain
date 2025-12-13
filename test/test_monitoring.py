"""Verify server-wide request lifetimes, retention, pagination and UTC histograms."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from susi_brain.compute.config import ComputeSettings
from susi_brain.compute.main import create_compute_app
from susi_brain.compute.monitoring import INTERVALS, MAX_BUCKETS, RequestMonitor, RequestMonitoringMiddleware
from susi_brain.compute.monitoring_api import ACTIVE, HISTOGRAM, LOG

PREFIX = '/api/v1/compute'
MONITOR = PREFIX + '/monitoring'


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in os.environ:
        if name.startswith(('SUSI_COMPUTE_', 'SUSI_SERVER_')) or name in {'OPENAI_API_KEY', 'SUSI_API_KEY'}:
            monkeypatch.delenv(name)


class Clock:
    def __init__(self):
        self.wall, self.mono = 86400 * 20000 + 59, 10.0
    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


def monitor_for(capacity=10000):
    clock = Clock()
    return RequestMonitor(capacity, clock=lambda: clock.mono, wall_clock=lambda: clock.wall), clock


def begin(monitor, path=PREFIX + '/health/live'):
    return monitor.begin({'method': 'GET', 'path': path, 'state': {'correlation_id': 'same-correlation'},
                          'route': SimpleNamespace(path=path)})


def finish(monitor, record, *, status=200, error=None):
    monitor.response_started(record, {'status': status})
    monitor.sent_body(record, {'body': b'ok'})
    monitor.finish(record, error)


@pytest.mark.parametrize('root_path', ['', '/proxy'])
def test_all_api_outcomes_are_logged_without_sensitive_paths_queries_or_payloads(root_path):
    app = create_compute_app(ComputeSettings())

    @app.post(PREFIX + '/echo/{document}')
    async def echo(document: str, request: Request):
        await request.body()
        return Response('ok')

    @app.get(PREFIX + '/failure')
    async def failure():
        raise RuntimeError('private-error-text')

    with TestClient(app, root_path=root_path, raise_server_exceptions=False) as client:
        assert client.post(root_path + PREFIX + '/echo/private-document?api_key=private-query',
                           content=b'private-body', headers={'Authorization': 'Bearer private-header',
                           'X-Request-ID': 'known-request'}).status_code == 200
        assert client.post(PREFIX + '/calibration', content='invalid-json', headers={'Content-Type': 'application/json'}).status_code == 422
        assert client.get(PREFIX + '/not-real/private-segment').status_code == 404
        assert client.get(PREFIX + '/failure').status_code == 500
        assert client.get(PREFIX + '/ui').status_code == 200
        records = app.state.request_monitor.log()['requests']
        assert {record['status_code'] for record in records} == {200, 404, 422, 500}
        echoed = next(record for record in records if record['request_id'] == 'known-request')
        assert echoed['path'] == PREFIX + '/echo/{document}'
        assert echoed['request_bytes'] == len(b'private-body') and echoed['response_bytes'] == 2
        assert next(record for record in records if record['status_code'] == 404)['path'] == '/api/{unmatched}'
        exported = json.dumps(records)
        for private in ('private-query', 'private-header', 'private-body', 'private-document', 'private-segment', 'private-error-text', '_scope'):
            assert private not in exported
        assert client.get('/outside-api').status_code == 404
        assert len(app.state.request_monitor.log()['requests']) == len(records)


def test_monitoring_apis_track_their_own_reads_and_match_swagger_schemas():
    app = create_compute_app(ComputeSettings())
    with TestClient(app) as client:
        response = client.get(MONITOR + '/active')
        active = response.json()
        assert response.headers['cache-control'] == 'no-store'
        Draft202012Validator(ACTIVE).validate(active)
        assert active['active_count'] == 1 and active['totals'] == {'received': 1, 'finished': 0, 'active': 1, 'errors': 0, 'cancelled': 0}
        assert active['requests'][0]['path'] == MONITOR + '/active'
        filtered = client.get(MONITOR + '/active?exclude_monitoring=true').json()
        assert filtered['active_count'] == 0 and filtered['totals']['active'] == 1
        logs = client.get(MONITOR + '/requests').json()
        Draft202012Validator(LOG).validate(logs)
        assert len(logs['requests']) == 2
        histogram = client.get(MONITOR + '/histogram').json()
        Draft202012Validator(HISTOGRAM).validate(histogram)
        assert histogram['totals']['requests'] == 3
        assert client.get(MONITOR + '/histogram?exclude_monitoring=true').json()['totals']['requests'] == 0
        schema = client.get(PREFIX + '/openapi.json').json()
        for suffix in ('/active', '/requests', '/histogram'):
            operation = schema['paths'][MONITOR + suffix]['get']
            assert 'application/json' in operation['responses']['200']['content']
            assert 'application/problem+json' in operation['responses']['422']['content']


def test_cors_preflight_is_counted_even_before_correlation_or_routing():
    app = create_compute_app(ComputeSettings())
    with TestClient(app) as client:
        assert client.options(PREFIX + '/health/live', headers={
            'Origin': 'https://example.org', 'Access-Control-Request-Method': 'GET'}).status_code == 200
    row = app.state.request_monitor.log()['requests'][0]
    assert row['method'] == 'OPTIONS' and row['status_code'] == 200 and row['request_id'] is None


def test_long_stream_and_regular_request_overlap_and_finish_in_completion_order():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        app = create_compute_app(ComputeSettings())
        @app.get(PREFIX + '/slow-stream')
        async def slow_stream():
            async def chunks():
                yield b'data: first\n\n'
                entered.set()
                await release.wait()
                yield b'data: [DONE]\n\n'
            return StreamingResponse(chunks(), media_type='text/event-stream')
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url='http://compute') as client:
            task = asyncio.create_task(client.get(PREFIX + '/slow-stream'))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await client.get(PREFIX + '/health/live')
                active = (await client.get(MONITOR + '/active?exclude_monitoring=true')).json()
                assert active['active_count'] == 1
                row = active['requests'][0]
                assert row['path'] == PREFIX + '/slow-stream' and row['phase'] == 'streaming'
                assert row['status_code'] == 200 and row['response_bytes'] == len(b'data: first\n\n')
                assert row['finished_at'] is None and row['duration_seconds'] >= 0
                before = app.state.request_monitor.histogram(exclude_monitoring=True)['totals']['requests']
                assert before == 1
                release.set()
                await asyncio.wait_for(task, 2)
                rows = app.state.request_monitor.log(exclude_monitoring=True)['requests']
                assert rows[0]['id'] < rows[1]['id']
                assert rows[0]['completion_id'] > rows[1]['completion_id']
                assert rows[0]['state'] == 'completed'
                older = app.state.request_monitor.log(before=rows[0]['completion_id'], exclude_monitoring=True)['requests']
                assert [row['path'] for row in older] == [PREFIX + '/health/live']
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_cancelled_request_is_removed_from_activity_and_aggregated_once():
    async def scenario():
        entered = asyncio.Event()
        app = create_compute_app(ComputeSettings())
        @app.get(PREFIX + '/waiting')
        async def waiting():
            entered.set()
            await asyncio.Event().wait()
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url='http://compute') as client:
            task = asyncio.create_task(client.get(PREFIX + '/waiting'))
            await asyncio.wait_for(entered.wait(), 2)
            assert app.state.request_monitor.activity()['active_count'] == 1
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        monitor = app.state.request_monitor
        assert monitor.activity()['active_count'] == 0
        assert monitor.log()['requests'][0]['state'] == 'cancelled'
        assert monitor.histogram()['totals'] == {'requests': 1, 'errors': 0, 'cancelled': 1}
    asyncio.run(scenario())


def test_client_disconnect_during_send_releases_request_activity():
    async def scenario():
        monitor, _ = monitor_for()
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200})
            await send({'type': 'http.response.body', 'body': b'first', 'more_body': True})
        async def send(message):
            if message['type'] == 'http.response.body':
                raise OSError('private-disconnect-text')
        async def receive():
            return {'type': 'http.disconnect'}
        with pytest.raises(OSError):
            await RequestMonitoringMiddleware(app, monitor)({'type': 'http', 'method': 'GET', 'path': '/v1/chat/completions'}, receive, send)
        assert monitor.activity()['active_count'] == 0
        row = monitor.log()['requests'][0]
        assert row['state'] == 'disconnected' and row['response_bytes'] == 0
        assert 'private-disconnect-text' not in json.dumps(row)
    asyncio.run(scenario())


def test_pagination_and_filters_survive_concurrent_new_completions_and_duplicate_trace_ids():
    monitor, _ = monitor_for()
    for status in (200, 404, 200, 503, 200):
        finish(monitor, begin(monitor), status=status)
    page = monitor.log(limit=2)
    assert [row['completion_id'] for row in page['requests']] == [5, 4]
    finish(monitor, begin(monitor))
    older = monitor.log(limit=2, before=page['next_before'])
    assert [row['completion_id'] for row in older['requests']] == [3, 2]
    assert [row['completion_id'] for row in monitor.log(before=older['next_before'])['requests']] == [1]
    assert len({row['request_id'] for row in monitor.log()['requests']}) == 1
    assert len({row['id'] for row in monitor.log()['requests']}) == 6
    assert len(monitor.log(status_code=200, method='GET')['requests']) == 4
    assert monitor.log(path='/wrong-route')['requests'] == []


@pytest.mark.parametrize('interval,width,default', [(name, *values) for name, values in INTERVALS.items()])
def test_histograms_are_zero_filled_utc_aligned_and_independent_of_log_eviction(interval, width, default):
    monitor, clock = monitor_for(capacity=2)
    for status in (200, 500, 200, 404, 200):
        finish(monitor, begin(monitor), status=status)
        clock.advance(61)
    result = monitor.histogram(interval)
    assert len(result['buckets']) == default and result['bucket_seconds'] == width
    assert result['totals'] == {'requests': 5, 'errors': 2, 'cancelled': 0}
    assert result['retained_requests'] == 2 and result['evicted_requests'] == 3
    assert any(not bucket['observed'] for bucket in result['buckets'])
    assert result['buckets'][-1]['partial'] is True
    for row in result['buckets']:
        from datetime import datetime
        assert datetime.fromisoformat(row['start']).timestamp() % width == 0
    Draft202012Validator(HISTOGRAM).validate(result)


def test_bucket_boundaries_use_completion_time_and_durations_use_monotonic_time():
    monitor, clock = monitor_for()
    record = begin(monitor)
    clock.advance(2)
    finish(monitor, record)
    histogram = monitor.histogram(buckets=2)
    assert [row['requests'] for row in histogram['buckets']] == [0, 1]
    record = begin(monitor)
    clock.wall -= 10
    clock.mono += 3
    finish(monitor, record)
    assert monitor.log()['requests'][0]['duration_seconds'] == 3


def test_aggregate_retention_is_bounded_even_when_wall_clock_moves_backwards():
    monitor, clock = monitor_for(capacity=1)
    for _ in range(MAX_BUCKETS + 2):
        finish(monitor, begin(monitor))
        clock.advance(60)
    assert len(monitor.buckets['minute']) == MAX_BUCKETS
    assert monitor.histogram(buckets=MAX_BUCKETS)['totals']['requests'] == MAX_BUCKETS - 1
    clock.wall -= 1000000
    finish(monitor, begin(monitor))
    assert len(monitor.buckets['minute']) <= MAX_BUCKETS
    assert monitor.activity()['totals']['finished'] == MAX_BUCKETS + 3


def test_active_row_limit_reports_omissions_without_losing_total_counts():
    monitor, _ = monitor_for()
    records = [begin(monitor) for _ in range(3)]
    assert monitor.activity(limit=1)['active_count'] == 3
    assert monitor.activity(limit=1)['truncated'] is True
    assert len(monitor.activity(limit=1)['requests']) == 1
    for record in records:
        finish(monitor, record)
    monitor.finish(records[0])
    assert monitor.activity()['totals']['finished'] == 3


@pytest.mark.parametrize('query', ['limit=0', 'limit=1001', 'exclude_monitoring=invalid'])
def test_invalid_active_parameters_return_native_422(query):
    with TestClient(create_compute_app(ComputeSettings())) as client:
        assert client.get(MONITOR + '/active?' + query).status_code == 422


@pytest.mark.parametrize('suffix', ['/requests?before=0', '/requests?limit=501', '/requests?method=get',
                                   '/requests?status_code=600', '/histogram?interval=week', '/histogram?buckets=1441'])
def test_invalid_log_or_histogram_parameters_return_native_422(suffix):
    with TestClient(create_compute_app(ComputeSettings())) as client:
        assert client.get(MONITOR + suffix).status_code == 422


def test_selected_proxy_model_is_visible_without_logging_messages_or_keys():
    def upstream(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': 'Concrete-Model'}]})
        return httpx.Response(200, json={'choices': []})
    app = create_compute_app(ComputeSettings(
        endpoints=[{'url': 'https://provider.example/v1', 'api_key': 'private-api-key'}], small=['concrete-model']),
        chat_transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        assert client.post('/v1/chat/completions', json={
            'model': 'small', 'messages': [{'role': 'user', 'content': 'private-prompt'}]}).status_code == 200
        record = client.get(MONITOR + '/requests?exclude_monitoring=true').json()['requests'][0]
        assert record['model'] == 'Concrete-Model' and record['model_group'] == 'small'
        assert record['endpoint_id'].startswith('ep-')
        assert 'private-' not in json.dumps(record)


def test_daily_request_log_and_histogram_survive_restart_beyond_the_ram_cache(tmp_path):
    settings = ComputeSettings(request_log_directory=tmp_path / 'logs', request_log_capacity=2)
    app = create_compute_app(settings)
    assert not settings.request_log_directory.exists()
    with TestClient(app) as client:
        for _ in range(5):
            assert client.get(PREFIX + '/health/live').status_code == 200
        first = app.state.request_monitor.log(limit=2)
        assert first['storage'] == 'daily-jsonl' and first['retained_requests'] == 5
        assert first['cached_requests'] == 2 and first['evicted_requests'] == 0
        assert first['log_status']['buffered_entries'] == 5
        assert not list(settings.request_log_directory.glob('*.jsonl'))
        old_instance = first['requests'][0]['instance_id']
    assert len(list(settings.request_log_directory.glob('requests-*.jsonl'))) == 1
    next_app = create_compute_app(settings)
    with TestClient(next_app) as client:
        monitor = next_app.state.request_monitor
        assert monitor.activity()['totals']['received'] == 0
        assert monitor.histogram()['totals']['requests'] == 5
        restored = client.get(MONITOR + '/requests', params={'before': first['next_before']}).json()
        Draft202012Validator(LOG).validate(restored)
        assert [row['completion_id'] for row in restored['requests']] == [3, 2, 1]
        assert {row['instance_id'] for row in restored['requests']} == {old_instance}
        assert restored['instance_id'] != old_instance
        assert restored['retained_requests'] == 5
        assert client.get(PREFIX + '/health/live').status_code == 200
        assert monitor.log()['requests'][0]['completion_id'] == 7
        assert monitor.histogram()['totals']['requests'] == 7


def test_request_search_filters_old_archives_and_current_buffer_in_the_api(tmp_path):
    settings = ComputeSettings(request_log_directory=tmp_path / 'logs', request_log_capacity=1)
    with TestClient(create_compute_app(settings)) as client:
        client.get(PREFIX + '/health/live', headers={'X-Request-ID': 'search-one'})
        client.get(PREFIX + '/unknown/private-document?key=private-query')
    app = create_compute_app(settings)
    with TestClient(app) as client:
        client.get(PREFIX + '/status', headers={'X-Request-ID': 'search-two'})
        values = client.get(MONITOR + '/requests', params={'q': 'SEARCH-', 'exclude_monitoring': True,
            'since': '2020-01-01T00:00:00Z', 'until': '2100-01-01T00:00:00Z'}).json()
        assert [row['request_id'] for row in values['requests']] == ['search-two', 'search-one']
        old = client.get(MONITOR + '/requests', params={'status_code': 404, 'method': 'GET', 'path': '/api/{unmatched}'}).json()
        assert len(old['requests']) == 1 and old['requests'][0]['status_code'] == 404
        assert client.get(MONITOR + '/requests', params={'until': '2020-01-01T00:00:00Z'}).json()['requests'] == []
    disk = ''.join(path.read_text() for path in settings.request_log_directory.glob('*.jsonl'))
    assert 'private-document' not in disk and 'private-query' not in disk and '_scope' not in disk


@pytest.mark.parametrize('query', [
    {'since': '2026-10-25T12:00:00'}, {'until': '2026-10-25T12:00:00'},
    {'since': '2026-10-26T00:00:00Z', 'until': '2026-10-25T00:00:00Z'}, {'q': 'x' * 257},
])
def test_invalid_archive_search_ranges_and_queries_return_422(query):
    with TestClient(create_compute_app(ComputeSettings())) as client:
        response = client.get(MONITOR + '/requests', params=query)
        assert response.status_code == 422
        assert response.headers['content-type'].startswith('application/problem+json')


def test_log_directory_configuration_and_public_data_separation(monkeypatch, tmp_path):
    target = tmp_path / 'private-logs'
    monkeypatch.setenv('SUSI_COMPUTE_REQUEST_LOG_DIRECTORY', str(target))
    assert ComputeSettings.from_env().request_log_directory == target
    assert ComputeSettings().request_log_directory == Path('logs/compute')
    with pytest.raises(ValidationError, match='outside the public'):
        ComputeSettings(data_path=tmp_path, request_log_directory=tmp_path / 'nested-logs')


def test_flush_failure_is_visible_without_failing_api_requests_and_shutdown_retries(tmp_path, monkeypatch):
    app = create_compute_app(ComputeSettings(request_log_directory=tmp_path / 'logs'))
    store = app.state.request_monitor.log_store
    append = store._append
    with TestClient(app) as client:
        assert client.get(PREFIX + '/health/live').status_code == 200
        def failed(*_):
            raise OSError('private-filesystem-error')
        monkeypatch.setattr(store, '_append', failed)
        with pytest.raises(OSError):
            store.flush()
        response = client.get(MONITOR + '/requests')
        assert response.status_code == 200
        assert response.json()['log_status']['error'] == 'append_failed'
        assert response.json()['log_status']['buffered_entries'] == 1
        assert 'private-filesystem-error' not in response.text
        monkeypatch.setattr(store, '_append', append)
    assert store.metadata()['buffered_entries'] == 0


def test_time_search_boundaries_match_the_displayed_completion_timestamp():
    app = create_compute_app(ComputeSettings())
    with TestClient(app) as client:
        client.get(PREFIX + '/health/live', headers={'X-Request-ID': 'boundary-request'})
        row = app.state.request_monitor.log()['requests'][0]
        params = {'q': 'boundary-request', 'since': row['finished_at']}
        assert len(client.get(MONITOR + '/requests', params=params).json()['requests']) == 1
        params = {'q': 'boundary-request', 'until': row['finished_at']}
        assert client.get(MONITOR + '/requests', params=params).json()['requests'] == []
