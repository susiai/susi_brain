"""Verify probe semantics, GUI lifecycle and measured streaming timings."""

import asyncio
import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from susi_brain.compute.calibration import EndpointCalibration, FORM_CASES, VISION_IMAGE
from susi_brain.compute.calibration_api import SNAPSHOT
from susi_brain.compute.config import ComputeSettings
from susi_brain.compute.main import create_compute_app
from susi_brain.compute.text_service import ChatCompletion

PATH = '/api/v1/compute/calibration'


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in os.environ:
        if name.startswith(('SUSI_COMPUTE_', 'SUSI_SERVER_')) or name in {'OPENAI_API_KEY', 'SUSI_API_KEY'}:
            monkeypatch.delenv(name)


class Clock:
    value = 10.0
    def __call__(self):
        return self.value


class SpeedStream(httpx.AsyncByteStream):
    def __init__(self, clock, *, usage=True, complete=True, hidden=False, thinking=False, delimiter='\r\n', tokens=11):
        self.clock, self.closed = clock, False
        def event(value):
            return ('data: ' + json.dumps(value, ensure_ascii=False) + delimiter * 2).encode()
        first = {'reasoning_content': 'work'} if thinking else {'content': 'Grüße'}
        encoded = event({'choices': [{'delta': first, 'finish_reason': None}]})
        split = encoded.index('ü'.encode()) + 1 if not thinking else len(encoded) // 2
        self.parts = [
            (0.1, b': keepalive' + delimiter.encode() * 2 + event({'choices': [{'delta': {'role': 'assistant', 'content': ''}}]})),
            (0.2, encoded[:split]), (0.4, encoded[split:]),
            (1.4, event({'choices': [{'delta': {'content': '1 2 3'}}]})),
        ]
        if complete:
            self.parts.append((1.45, event({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})))
        if usage:
            self.parts.append((1.5, event({'choices': [], 'usage': {'completion_tokens': tokens,
                'prompt_tokens': 777, 'total_tokens': 777 + tokens,
                'completion_tokens_details': {'reasoning_tokens': 9 if hidden else 3 if thinking else 0}}})))
        self.parts.append((1.6, b'data: [DONE]'))
    async def __aiter__(self):
        origin = self.clock.value
        for offset, part in self.parts:
            self.clock.value = origin + offset
            yield part
    async def aclose(self):
        self.closed = True


class ProbeServer:
    def __init__(self, clock):
        self.clock = clock
        self.models = {'first': ['SMART-MODEL', 'small-model', 'unconfigured'], 'second': ['smart-model']}
        self.events, self.streams = [], []
        self.forms = True
        self.vision = True
        self.thinking = True
        self.usage = True
        self.complete = True
        self.hidden = False
        self.stream_thinking = False
        self.delimiter = '\r\n'
        self.reject = None
        self.fail = None
        self.speed_tokens = {}
    async def __call__(self, request):
        if request.url.host == 'second':
            assert request.headers['authorization'] == 'Bearer second-test-key'
            assert request.url.path.startswith('/api/v1/')
        else:
            assert 'authorization' not in request.headers
        assert 'cookie' not in request.headers
        if request.method == 'GET':
            self.events.append((request.url.host, None, 'discovery'))
            return httpx.Response(200, json={'data': [{'id': name} for name in self.models[request.url.host]]})
        if request.url.path.endswith('/api/show'):
            return httpx.Response(404, json={'error': 'No Ollama metadata on this chat provider.'})
        body = json.loads(request.content)
        content = body['messages'][-1]['content']
        if body.get('stream'):
            kind = 'speed'
        elif 'response_format' in body:
            kind = 'forms'
        elif isinstance(content, list):
            kind = 'vision'
        elif body.get('reasoning_effort'):
            kind = 'thinking'
        else:
            kind = 'availability'
        self.events.append((request.url.host, body['model'], kind))
        if kind == self.reject:
            return httpx.Response(400, json={'error': 'unsupported field'})
        if kind == self.fail:
            raise httpx.ReadTimeout('private failure with second-test-key')
        if kind == 'speed':
            stream = SpeedStream(self.clock, usage=self.usage, complete=self.complete,
                hidden=self.hidden, thinking=self.stream_thinking, delimiter=self.delimiter,
                tokens=self.speed_tokens.get((request.url.host, body['model'].casefold()), 11))
            self.streams.append(stream)
            return httpx.Response(200, stream=stream, headers={'Content-Type': 'text/event-stream'})
        reply = {'role': 'assistant', 'content': 'OK'}
        if kind == 'forms':
            schema = body['response_format']['json_schema']
            assert schema['strict'] is True and schema['schema']['additionalProperties'] is False
            reply['content'] = json.dumps({'mood': dict(FORM_CASES)[content] if self.forms else 'wrong'})
        elif kind == 'vision':
            assert content[1]['image_url']['url'].startswith('data:image/png;base64,')
            reply['content'] = 'The number is 42.' if self.vision else '142'
        elif kind == 'thinking':
            reply['content'] = '42'
            if self.thinking:
                reply['reasoning_content'] = '17 + 25 is 42'
        return httpx.Response(200, json={'choices': [{'message': reply, 'finish_reason': 'stop'}]})


def setup_app(**overrides):
    clock = Clock()
    server = ProbeServer(clock)
    config = {'endpoints': ['first:11434', {'url': 'https://second/api/v1', 'api_key': 'second-test-key'}],
              'smart': ['smart-model'], 'small': ['small-model']}
    config.update(overrides)
    app = create_compute_app(ComputeSettings(**config), chat_transport=httpx.MockTransport(server))
    app.state.calibration.clock = clock
    return app, server


def wait(client, condition=None):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(PATH)
        assert response.status_code == 200
        value = response.json()
        Draft202012Validator(SNAPSHOT).validate(value)
        if condition(value) if condition is not None else value['run']['state'] != 'running':
            return value
        time.sleep(0.005)
    pytest.fail('Calibration did not reach the expected state.')


def records(snapshot):
    return [record for values in snapshot['results'].values() for record in values.values()]


def test_ranked_model_calibration_drives_endpoint_and_model_selection():
    group = [{'name': 'smart-model', 'ranking': 90}, {'name': 'small-model', 'ranking': 30}]
    app, server = setup_app(smart=group, small=group)
    server.speed_tokens = {('first', 'small-model'): 200, ('second', 'smart-model'): 100}
    with TestClient(app) as client:
        assert client.post(PATH, json={}).status_code == 202
        result = wait(client)
        assert result['run']['total_models'] == 4
        assert all(record['groups'] == ['smart', 'small'] for record in records(result))
        for alias, expected in [('small', ('first', 'small-model')), ('smart', ('second', 'smart-model'))]:
            response = client.post('/v1/chat/completions', json={
                'model': alias, 'messages': [{'role': 'user', 'content': 'Hello'}]})
            assert response.status_code == 200
            assert server.events[-1][:2] == expected
        assert client.get('/api/v1/compute/chat/connections').json()['active_connections'] == 0


def test_explicit_default_run_orders_all_features_before_speed_and_exports_safe_results():
    app, server = setup_app()
    with TestClient(app) as client:
        initial = client.get(PATH)
        assert initial.headers['cache-control'] == 'no-store'
        assert initial.json()['run'] is None and initial.json()['results'] == {}
        assert server.events == []
        start = client.post(PATH, json={})
        assert start.status_code == 202 and start.headers['location'] == PATH
        result = wait(client)
    assert result['run']['scope'] == 'configured'
    assert result['run']['state'] == 'completed'
    assert result['run']['total_models'] == result['run']['completed_models'] == result['run']['features_completed'] == 4
    measured = records(result)
    assert len(measured) == 4
    assert all(record['model'] != 'unconfigured' for record in measured)
    available = [record for record in measured if record['available']]
    assert len(available) == 3
    for record in available:
        assert record['capabilities'] == {'forms': True, 'vision': True, 'thinking': True}
        assert all(check['state'] == 'passed' for check in record['checks'].values())
        assert record['metrics']['completion_tokens'] == 11
    missing = next(record for record in measured if not record['available'])
    assert missing['listed'] is False
    assert missing['capabilities'] == dict.fromkeys(('forms', 'vision', 'thinking'))
    assert missing['checks']['speed']['state'] == 'skipped'
    assert missing['metrics'] is None
    speed = [index for index, event in enumerate(server.events) if event[2] == 'speed']
    functional = [index for index, event in enumerate(server.events) if event[2] not in ('discovery', 'speed')]
    assert min(speed) > max(functional)
    assert all(stream.closed for stream in server.streams)
    serialized = json.dumps(result)
    assert 'second-test-key' not in serialized and 'reasoning_content' not in serialized
    assert 'I hate programming' not in serialized and 'base64' not in serialized
    assert VISION_IMAGE.read_bytes().startswith(b'\x89PNG')


@pytest.mark.parametrize('delimiter', ['\n', '\r\n', '\r'])
def test_speed_uses_first_output_delta_and_final_usage_not_chunks_or_prompt_tokens(delimiter):
    app, server = setup_app(endpoints=['first:11434'], small=[])
    server.delimiter = delimiter
    with TestClient(app) as client:
        assert client.post(PATH).status_code == 202
        result = wait(client)
    metric = records(result)[0]['metrics']
    assert metric['ttft_seconds'] == pytest.approx(0.4)
    assert metric['duration_seconds'] == pytest.approx(1.6)
    assert metric['tokens_per_second'] == pytest.approx(11 / 1.6)
    assert metric['generation_tokens_per_second'] == pytest.approx(10)
    assert metric['first_token_kind'] == 'content'
    assert metric['token_count_source'] == 'upstream_usage'


@pytest.mark.parametrize('mode', ['missing_usage', 'hidden_reasoning', 'emitted_reasoning', 'incomplete'])
def test_speed_missing_usage_reasoning_and_truncation_are_explicit(mode):
    app, server = setup_app(endpoints=['first:11434'], small=[])
    server.usage = mode != 'missing_usage'
    server.complete = mode != 'incomplete'
    server.hidden = mode == 'hidden_reasoning'
    server.stream_thinking = mode == 'emitted_reasoning'
    with TestClient(app) as client:
        client.post(PATH)
        result = wait(client)
    record = records(result)[0]
    if mode == 'incomplete':
        assert record['metrics'] is None
        assert record['checks']['speed']['error_code'] == 'incomplete_stream'
    else:
        metric = record['metrics']
        if mode == 'missing_usage':
            assert metric['completion_tokens'] is None
            assert metric['tokens_per_second'] is metric['generation_tokens_per_second'] is None
            assert metric['ttft_seconds'] == pytest.approx(0.4)
        elif mode == 'hidden_reasoning':
            assert metric['generation_tokens_per_second'] is None
            assert metric['tokens_per_second'] is not None
        else:
            assert metric['first_token_kind'] == 'reasoning'
            assert metric['generation_tokens_per_second'] == pytest.approx(10)


@pytest.mark.parametrize('mode', ['wrong_answers', 'rejected', 'timeout'])
def test_negative_capabilities_are_distinct_from_inconclusive_failures(mode):
    app, server = setup_app(endpoints=['first:11434'], small=[])
    if mode == 'wrong_answers':
        server.forms = server.vision = server.thinking = False
    elif mode == 'rejected':
        server.reject = 'forms'
    else:
        server.fail = 'vision'
    with TestClient(app) as client:
        client.post(PATH)
        result = wait(client)
    record = records(result)[0]
    assert record['available'] is True
    assert record['metrics'] is not None
    if mode == 'wrong_answers':
        assert record['capabilities'] == {'forms': False, 'vision': False, 'thinking': False}
        assert record['checks']['vision']['state'] == 'not_demonstrated'
    elif mode == 'rejected':
        assert record['capabilities']['forms'] is False
        assert record['checks']['forms']['state'] == 'rejected'
        assert record['checks']['forms']['http_status'] == 400
    else:
        assert record['capabilities']['vision'] is None
        assert record['checks']['vision']['state'] == 'error'
        assert record['checks']['vision']['error_code'] == 'timeout'
    assert 'second-test-key' not in json.dumps(result)


def test_all_scope_filters_endpoint_ids_and_replaces_previous_snapshot():
    app, server = setup_app()
    with TestClient(app) as client:
        first_id = client.get(PATH).json()['endpoints'][0]['id']
        client.post(PATH, json={'scope': 'all', 'endpoint_ids': [first_id], 'models': ['unconfigured']})
        first = wait(client)
        assert [record['model'] for record in records(first)] == ['unconfigured']
        assert records(first)[0]['groups'] == []
        assert all(event[0] == 'first' for event in server.events)
        client.post(PATH, json={'endpoint_ids': [first_id], 'models': ['small-model']})
        second = wait(client)
    assert first['run']['id'] != second['run']['id']
    assert [record['model'] for record in records(second)] == ['small-model']


@pytest.mark.parametrize('body', [{'scope': 'unknown'}, {'endpoint_ids': []}, {'endpoint_ids': ['http://arbitrary:1']},
                                {'models': []}, {'models': 'smart-model'}, {'models': [False]},
                                {'models': ['smart-model', 'SMART-MODEL']}, {'models': ['unconfigured']},
                                {'prompt': 'free form'}])
def test_invalid_filters_cannot_trigger_network_or_arbitrary_inference(body):
    app, server = setup_app()
    with TestClient(app) as client:
        response = client.post(PATH, json=body)
    assert response.status_code == 422
    assert server.events == []


def test_pair_limit_fails_before_any_inference_and_inventory_failure_is_not_absence():
    app, server = setup_app(calibration_max_pairs=1)
    with TestClient(app) as client:
        client.post(PATH)
        result = wait(client)
    assert result['run']['state'] == 'failed'
    assert result['run']['error_code'] == 'model_limit_exceeded'
    assert all(event[2] == 'discovery' for event in server.events)

    def failure(request):
        return httpx.Response(401, json={'error': 'private key error'})
    config = ComputeSettings(endpoints=['first:11434'], smart=['smart-model'])
    with TestClient(create_compute_app(config, chat_transport=httpx.MockTransport(failure))) as client:
        client.post(PATH)
        result = wait(client)
    record = records(result)[0]
    assert record['listed'] is None and record['available'] is False
    assert record['checks']['availability']['error_code'] == 'inventory_http_401'
    assert record['capabilities'] == dict.fromkeys(('forms', 'vision', 'thinking'))


class Blocked(httpx.AsyncByteStream):
    closed = False
    async def __aiter__(self):
        await asyncio.Event().wait()
        yield b'never'
    async def aclose(self):
        self.closed = True


def blocked_app(timeout=60):
    stream = Blocked()
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': 'smart-model'}]})
        return httpx.Response(200, stream=stream)
    settings = ComputeSettings(endpoints=['first:11434'], smart=['smart-model'], calibration_timeout_seconds=timeout)
    return create_compute_app(settings, chat_transport=httpx.MockTransport(handler)), stream


def test_running_conflict_cancel_and_shutdown_drain_upstream():
    for shutdown in (False, True):
        app, stream = blocked_app()
        with TestClient(app) as client:
            assert client.post(PATH).status_code == 202
            wait(client, lambda value: value['run']['current'] is not None)
            assert client.post(PATH).status_code == 409
            if not shutdown:
                cancelled = client.delete(PATH)
                assert cancelled.status_code == 200
                assert cancelled.json()['run']['state'] == 'cancelled'
                assert records(cancelled.json())[0]['checks']['availability']['state'] == 'cancelled'
                assert client.delete(PATH).json()['run']['state'] == 'cancelled'
        assert stream.closed
        assert app.state.calibration.run['state'] == 'cancelled'


def test_probe_deadline_closes_upstream_and_records_unknown_availability():
    app, stream = blocked_app(timeout=0.02)
    with TestClient(app) as client:
        client.post(PATH)
        result = wait(client)
    record = records(result)[0]
    assert record['available'] is None
    assert record['checks']['availability']['error_code'] == 'timeout'
    assert record['metrics'] is None and stream.closed


def test_cancel_before_worker_starts_is_terminal_and_inactive_service_rejects_start():
    async def scenario():
        chat = ChatCompletion(ComputeSettings(endpoints=['first:11434'], smart=['smart-model']),
                              transport=httpx.MockTransport(lambda request: pytest.fail('No network before immediate cancel.')))
        service = EndpointCalibration(chat)
        async with chat.lifespan():
            service.start({})
            assert await service.cancel()
            assert service.snapshot()['run']['state'] == 'cancelled'
    asyncio.run(scenario())
    app, _ = setup_app()
    assert TestClient(app).post(PATH).status_code == 503
