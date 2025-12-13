"""Exercise model-group routing, transport failures and streaming ownership."""

import asyncio
import json
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from susi_brain.compute import text_service
from susi_brain.compute.config import ComputeSettings
from susi_brain.compute.main import create_compute_app
from susi_brain.compute.routing import endpoint_id

CONNECTIONS = '/api/v1/compute/chat/connections'


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in os.environ:
        if name.startswith(('SUSI_COMPUTE_', 'SUSI_SERVER_')) or name in {'OPENAI_API_KEY', 'SUSI_API_KEY'}:
            monkeypatch.delenv(name)


def app_for(handler, **changes):
    values = {'endpoints': ['first:11434', 'second:8080'],
              'smart': ['smart-primary', 'smart-fallback'], 'small': ['small-model']}
    values.update(changes)
    return create_compute_app(ComputeSettings(**values), chat_transport=httpx.MockTransport(handler))


def chat(model='small', **options):
    return {'model': model, 'messages': [{'role': 'user', 'content': 'Hello'}], **options}


def inventory(names):
    return httpx.Response(200, json={'data': [{'id': name} for name in names]})


def test_group_priority_live_availability_and_transparent_forwarding():
    models = {'first': ['small-model', 'smart-fallback'], 'second': ['SMART-PRIMARY']}
    posts, gets = [], []

    def handler(request):
        assert 'authorization' not in request.headers
        assert 'cookie' not in request.headers
        assert request.headers['x-request-id'] == 'proxy-request'
        if request.method == 'GET':
            assert request.url.path == '/v1/models'
            gets.append(request.url.host)
            return inventory(models[request.url.host])
        assert request.url.path == '/v1/chat/completions'
        payload = json.loads(request.content)
        posts.append((request.url.host, payload))
        return httpx.Response(200, json={'model': payload['model'], 'choices': [], 'usage': {'total_tokens': 7}},
                              headers={'Set-Cookie': 'upstream=private', 'X-Secret': 'hidden'})

    app = app_for(handler, openai_api_key='audio-only-key')
    payload = chat('smart', temperature=0, tools=[{'type': 'function'}],
                   response_format={'type': 'json_object'}, max_completion_tokens=321)
    headers = {'X-Request-ID': 'proxy-request', 'Authorization': 'Bearer caller-key', 'Cookie': 'caller=private'}
    with TestClient(app) as client:
        result = client.post('/v1/chat/completions', json=payload, headers=headers)
        assert result.status_code == 200
        assert result.json() == {'model': 'SMART-PRIMARY', 'choices': [], 'usage': {'total_tokens': 7}}
        assert result.headers['x-request-id'] == 'proxy-request'
        assert 'set-cookie' not in result.headers and 'x-secret' not in result.headers
        assert posts[-1] == ('second', {**payload, 'model': 'SMART-PRIMARY'})
        models['second'] = []
        assert client.post('/v1/chat/completions', json=chat('smart'), headers=headers).json()['model'] == 'smart-fallback'
        assert client.post('/v1/chat/completions', json=chat(), headers=headers).json()['model'] == 'small-model'
    assert len(gets) == 6
    assert posts[-1][1] == chat('small-model')


@pytest.mark.parametrize('payload', [None, {}, chat('gpt-3.5-turbo'), chat('SMART'), chat(['small']),
                                      {'messages': [{'role': 'user', 'content': 'Hello'}]},
                                      chat(messages=[]), chat(messages='Hello'), chat(messages=[1]), chat(stream='true')])
def test_invalid_inputs_never_contact_a_server(payload):
    def handler(request):
        raise AssertionError('Invalid requests must not reach a backend.')
    with TestClient(app_for(handler)) as client:
        response = client.post('/v1/chat/completions', json=payload)
    assert response.status_code == 400
    assert response.json()['error']['type'] == 'invalid_request_error'


@pytest.mark.parametrize('failure', ['connection', 'malformed', 'shape', 'redirect', 'timeout'])
def test_unusable_inventory_is_skipped(failure):
    async def handler(request):
        if request.method == 'GET':
            if request.url.host == 'second':
                return inventory(['small-model'])
            if failure == 'connection':
                raise httpx.ConnectError('private connection detail')
            if failure == 'malformed':
                return httpx.Response(200, content=b'not json')
            if failure == 'shape':
                return httpx.Response(200, json={'data': [42]})
            if failure == 'redirect':
                return httpx.Response(302, headers={'Location': 'http://foreign:1/v1/models'})
            await asyncio.sleep(0.1)
            return inventory(['small-model'])
        assert request.url.host == 'second'
        return httpx.Response(200, json={'model': 'small-model'})
    with TestClient(app_for(handler, model_probe_timeout_seconds=0.01)) as client:
        response = client.post('/v1/chat/completions', json=chat())
    assert response.status_code == 200


@pytest.mark.parametrize('changes', [{'endpoints': []}, {'small': []}, {'small': ['missing']}])
def test_no_matching_model_does_not_cross_groups_or_use_openai(changes):
    def handler(request):
        assert request.method == 'GET'
        return inventory(['smart-primary'])
    with TestClient(app_for(handler, **changes)) as client:
        response = client.post('/v1/chat/completions', json=chat())
    assert response.status_code == 503
    assert response.headers['retry-after'] == '1'
    assert response.json()['error']['code'] == 'model_unavailable'


@pytest.mark.parametrize('failure,status,code', [('timeout', 504, 'upstream_timeout'),
                                                ('connection', 502, 'upstream_unavailable'),
                                                ('redirect', 502, 'upstream_redirect')])
def test_chat_failure_is_safe_and_never_replayed(failure, status, code):
    calls = []
    def handler(request):
        if request.method == 'GET':
            return inventory(['small-model'])
        calls.append(request.url.host)
        if failure == 'timeout':
            raise httpx.ReadTimeout('private backend detail')
        if failure == 'connection':
            raise httpx.ConnectError('private backend detail')
        return httpx.Response(307, headers={'Location': 'http://foreign:1'})
    with TestClient(app_for(handler)) as client:
        response = client.post('/v1/chat/completions', json=chat())
        assert client.get(CONNECTIONS).json()['active_connections'] == 0
    assert response.status_code == status
    assert response.json()['error']['code'] == code
    assert 'private' not in response.text and 'foreign' not in response.text
    assert calls == ['first']


def test_backend_error_status_body_and_retry_after_are_preserved():
    def handler(request):
        if request.method == 'GET':
            return inventory(['small-model'])
        return httpx.Response(429, content=b'{"error":{"message":"busy"}}',
                              headers={'Content-Type': 'application/json', 'Retry-After': '17'})
    with TestClient(app_for(handler)) as client:
        response = client.post('/v1/chat/completions', json=chat(stream=True))
    assert response.status_code == 429
    assert response.content == b'{"error":{"message":"busy"}}'
    assert response.headers['retry-after'] == '17'


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, *, fail=False):
        self.values, self.fail, self.closed = chunks, fail, False
    async def __aiter__(self):
        for chunk in self.values:
            yield chunk
        if self.fail:
            raise httpx.ReadError('private stream failure')
    async def aclose(self):
        self.closed = True


def test_streams_preserve_bytes_and_close_upstream():
    content = 'data: {"model":"small-model","delta":"Grüße"}\n\n'.encode()
    chunks = Chunks([content[:43], content[43:], b'data: [DONE]\n\n'])
    def handler(request):
        if request.method == 'GET':
            return inventory(['small-model'])
        assert json.loads(request.content)['stream'] is True
        assert request.headers['accept'] == 'text/event-stream'
        return httpx.Response(203, stream=chunks, headers={'Content-Type': 'text/event-stream; charset=utf-8',
                                                         'Retry-After': '2'})
    with TestClient(app_for(handler)) as client:
        response = client.post('/v1/chat/completions', json=chat(stream=True))
    assert response.status_code == 203
    assert response.headers['content-type'] == 'text/event-stream; charset=utf-8'
    assert response.headers['retry-after'] == '2'
    first = json.loads(response.content.split(b'\n', 1)[0][6:])
    assert first == {'model': 'small-model', 'delta': 'Grüße', 'modelname': 'small-model',
                     'compute_group': 'small', 'compute_hostname': 'first',
                     'request_bytes': len(response.request.content)}
    assert response.content.endswith(b'data: [DONE]\n\n')
    assert chunks.closed


@pytest.mark.parametrize('failure', [None, 'transport', 'limit'])
def test_middle_lines_and_stream_failures_never_inject_error_frames(monkeypatch, failure):
    """Raw comments/invalid middle data pass through; transport failures add no JSON."""
    async def scenario():
        released, received = asyncio.Event(), asyncio.Event()
        parts = [b': heartbeat\n', b'data: not JSON\r', b'\n: heartbeat\r\n\r\n',
                 b'event: custom\r\ndata: not JSON\x00\xff\n\n']
        class Opaque(Chunks):
            async def __aiter__(self):
                yield parts[0]
                await released.wait()
                for chunk in parts[1:]:
                    yield chunk
                if failure == 'transport':
                    raise httpx.ReadError('private transport details')
        if failure == 'limit':
            monkeypatch.setattr(text_service, 'MAX_RESPONSE_BYTES', len(parts[0]) + len(parts[1]) - 1)
        chunks = Opaque(parts)
        app = app_for(lambda request: None)
        routing = app.state.calibration.chat.routing
        endpoint, _, connection = routing.select('small', [['small-model'], []])
        response = text_service.ChatStream(httpx.Response(200, stream=chunks), metadata={}, connection=connection)
        bodies = []
        async def send(message):
            if message['type'] == 'http.response.body' and message.get('body'):
                bodies.append(message['body'])
                received.set()
        async def receive():
            return {'type': 'http.disconnect'}
        task = asyncio.create_task(response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send))
        try:
            await asyncio.wait_for(received.wait(), 1)
            # Prelude comments do not wait for the first JSON event or full response.
            assert bodies == parts[:1] and not task.done()
            assert routing.count(endpoint) == 1 and not chunks.closed
            released.set()
            if failure is None:
                await task
            else:
                with pytest.raises(ValueError if failure == 'limit' else httpx.ReadError):
                    await task
            assert b''.join(bodies) == b''.join(parts[:1] if failure == 'limit' else parts)
            assert chunks.closed and routing.snapshot()['active_connections'] == 0
        finally:
            released.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_client_disconnect_closes_stream_before_next_chunk():
    async def scenario():
        chunks = Chunks([b'data: first\n\n', b'data: second\n\n'])
        app = app_for(lambda request: None)
        routing = app.state.calibration.chat.routing
        endpoint, _, connection = routing.select('small', [['small-model'], []])
        assert routing.count(endpoint) == 1
        response = text_service.ChatStream(httpx.Response(200, stream=chunks), metadata={}, connection=connection)
        async def send(message):
            if message['type'] == 'http.response.body':
                raise OSError('client disconnected')
        async def receive():
            return {'type': 'http.disconnect'}
        with pytest.raises(ClientDisconnect):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        assert chunks.closed
        assert routing.snapshot()['active_connections'] == 0
        connection.release()
        assert routing.count(endpoint) == 0
    asyncio.run(scenario())


def test_inventory_and_response_limits_are_enforced(monkeypatch):
    monkeypatch.setattr(text_service, 'MAX_MODELS_BYTES', 4)
    def handler(request):
        if request.method == 'GET':
            return inventory(['small-model'])
        return httpx.Response(200, content=b'too large')
    with TestClient(app_for(handler)) as client:
        assert client.post('/v1/chat/completions', json=chat()).status_code == 503
        monkeypatch.setattr(text_service, 'MAX_MODELS_BYTES', 1024)
        monkeypatch.setattr(text_service, 'MAX_RESPONSE_BYTES', 4)
        assert client.post('/v1/chat/completions', json=chat()).status_code == 502


@pytest.mark.parametrize('endpoint', ['http://localhost:80', 'localhost', 'localhost:0', 'localhost:65536',
                                     'user:pass@localhost:80', 'localhost:80/v1', 'localhost:80?x=1',
                                     'localhost:80#x', 'local host:80', 'localhost:80\\evil', '::1:80'])
def test_endpoint_configuration_rejects_values_other_than_host_port(endpoint):
    with pytest.raises(ValidationError):
        ComputeSettings(endpoints=[endpoint])


def test_list_configuration_json_environment_and_cli_precedence(monkeypatch):
    import sys
    import types
    from susi_brain.cli import main
    monkeypatch.setenv('SUSI_COMPUTE_ENDPOINTS', '["LOCALHOST:11434", "[::1]:8080"]')
    monkeypatch.setenv('SUSI_COMPUTE_SMART', '["model-a", "model-b"]')
    monkeypatch.setenv('SUSI_COMPUTE_SMALL', '["model-c"]')
    settings = ComputeSettings.from_env()
    assert settings.endpoints == ['localhost:11434', '[::1]:8080']
    assert settings.smart == ['model-a', 'model-b'] and settings.small == ['model-c']
    called = {}
    monkeypatch.setitem(sys.modules, 'uvicorn', types.SimpleNamespace(run=lambda app, **kw: called.update(app=app)))
    assert main(['compute', '--endpoints', 'cli:8081', '--smart', 'one', 'two', '--small', 'three']) == 0
    assert called['app'].state.settings.endpoints == ['cli:8081']
    assert called['app'].state.settings.smart == ['one', 'two']
    assert called['app'].state.settings.small == ['three']


@pytest.mark.parametrize('values', [{'smart': ['']}, {'small': ['a', 'A']}, {'small': ['with spaces']},
                                   {'endpoints': ['localhost:80', 'LOCALHOST:80']},
                                   {'model_probe_timeout_seconds': 0}, {'chat_timeout_seconds': float('inf')}])
def test_invalid_chat_configuration(values):
    with pytest.raises(ValidationError):
        ComputeSettings(**values)


@contextmanager
def backend(names, *, prefix='/v1', api_key=None):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass
        def reply(self, value):
            content = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def do_GET(self):
            assert self.path == prefix + '/models'
            assert self.headers.get('Authorization') == (('Bearer ' + api_key) if api_key else None)
            self.reply({'data': [{'id': name} for name in names]})
        def do_POST(self):
            assert self.path == prefix + '/chat/completions'
            assert self.headers.get('Authorization') == (('Bearer ' + api_key) if api_key else None)
            value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(value)
            self.reply({'model': value['model'], 'choices': [{'message': {'content': 'Hello'}}]})
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'127.0.0.1:{server.server_port}', calls
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_real_http_servers_route_both_groups_and_pool_stops_cleanly():
    with backend(['small-model']) as (first, first_calls), backend(['smart-model']) as (second, second_calls):
        settings = ComputeSettings(endpoints=[first, second], small=['small-model'], smart=['smart-model'])
        app = create_compute_app(settings)
        proxy = next(route.endpoint.__self__ for route in app.routes if getattr(route, 'path', '') == '/v1/chat/completions')
        assert proxy.client is None
        with TestClient(app) as client:
            pool = proxy.client
            for alias in ('smart', 'small'):
                response = client.post('/v1/chat/completions', json=chat(alias))
                assert response.status_code == 200
                assert response.json()['model'] == alias + '-model'
        assert proxy.client is None and pool.is_closed
        assert first_calls == [chat('small-model')]
        assert second_calls == [chat('smart-model')]


@pytest.mark.parametrize('stream', [False, True])
def test_authenticated_https_providers_keep_prefixes_and_keys_separate(monkeypatch, stream):
    monkeypatch.setenv('TEST_OPENROUTER_KEY', 'openrouter-test-key')
    calls = []
    def handler(request):
        calls.append((request.method, str(request.url)))
        assert 'cookie' not in request.headers
        assert request.headers['x-request-id'] == 'endpoint-auth'
        if request.url.host == 'local':
            assert 'authorization' not in request.headers
            assert request.url.path == '/v1/models'
            return inventory([])
        assert request.url.scheme == 'https'
        if request.url.host == 'openrouter.ai':
            assert request.headers['authorization'] == 'Bearer openrouter-test-key'
            prefix, model = '/api/v1', 'provider/smart-model'
        else:
            assert request.url.host == 'other.example'
            assert request.headers['authorization'] == 'Bearer other-test-key'
            prefix, model = '/custom/v2', 'provider/small-model'
        if request.method == 'GET':
            assert request.url.path == prefix + '/models'
            return inventory([model])
        assert request.url.path == prefix + '/chat/completions'
        assert json.loads(request.content)['model'] == model
        if stream:
            return httpx.Response(200, content=b'data: [DONE]\n\n',
                                  headers={'Content-Type': 'text/event-stream', 'Set-Cookie': 'private=backend'})
        return httpx.Response(200, json={'model': model}, headers={'Set-Cookie': 'private=backend'})

    app = app_for(handler, endpoints=[
        'local:11434',
        {'url': 'https://openrouter.ai/api/v1/', 'api_key_env': 'TEST_OPENROUTER_KEY'},
        {'url': 'https://other.example/custom/v2/chat/completions', 'api_key': 'other-test-key'},
    ], smart=['provider/smart-model'], small=['provider/small-model'], openai_api_key='audio-only-test-key')
    settings = app.state.settings
    assert settings.chat_endpoints[1].url == 'https://openrouter.ai/api/v1'
    assert settings.chat_endpoints[2].url == 'https://other.example/custom/v2'
    with TestClient(app) as client:
        for alias in ('smart', 'small', 'smart'):
            response = client.post('/v1/chat/completions', json=chat(alias, stream=stream), headers={
                'X-Request-ID': 'endpoint-auth', 'Authorization': 'Bearer caller-key', 'Cookie': 'private=caller',
            })
            assert response.status_code == 200
            assert 'set-cookie' not in response.headers
            if stream:
                assert response.content == b'data: [DONE]\n\n'
            else:
                assert response.json()['model'] == 'provider/' + alias + '-model'
    exported = settings.model_dump_json()
    schema = json.dumps(app.openapi())
    for secret in ('openrouter-test-key', 'other-test-key', 'audio-only-test-key'):
        assert secret not in repr(settings) and secret not in exported and secret not in schema
    assert len(calls) == 12


@pytest.mark.parametrize('phase', ['models', 'chat'])
def test_authenticated_redirects_never_send_key_to_redirect_target(phase):
    calls = []
    def handler(request):
        assert request.url.host == 'provider.example'
        assert request.headers['authorization'] == 'Bearer endpoint-test-key'
        calls.append(str(request.url))
        if phase == 'chat' and request.method == 'GET':
            return inventory(['small-model'])
        return httpx.Response(307, headers={'Location': 'https://foreign.example/collect'})
    with TestClient(app_for(handler, endpoints=[{
            'url': 'https://provider.example/api/v1', 'api_key': 'endpoint-test-key'}])) as client:
        response = client.post('/v1/chat/completions', json=chat())
    assert response.status_code == (503 if phase == 'models' else 502)
    assert len(calls) == (1 if phase == 'models' else 2)
    assert 'endpoint-test-key' not in response.text and 'foreign.example' not in response.text


def test_rejected_provider_key_is_not_reused_for_anonymous_local_target():
    calls = []
    def handler(request):
        calls.append((request.method, request.url.host))
        if request.url.host == 'protected.example':
            assert request.headers['authorization'] == 'Bearer rejected-test-key'
            return httpx.Response(401, json={'error': 'invalid key'})
        assert 'authorization' not in request.headers
        return inventory(['small-model']) if request.method == 'GET' else httpx.Response(200, json={'model': 'small-model'})
    with TestClient(app_for(handler, endpoints=[
            {'url': 'https://protected.example/v1', 'api_key': 'rejected-test-key'}, 'local:11434'])) as client:
        response = client.post('/v1/chat/completions', json=chat())
    assert response.status_code == 200
    assert calls.count(('POST', 'protected.example')) == 0
    assert ('POST', 'local') in calls


@pytest.mark.parametrize('endpoint', [
    {'url': 'ftp://provider.example/v1'},
    {'url': 'https://user:password@provider.example/v1'},
    {'url': 'https://provider.example/v1?key=private-key'},
    {'url': 'https://provider.example/v1#fragment'},
    {'url': 'https://provider.example:0/v1'},
    {'url': 'https://provider.example:65536/v1'},
    {'url': 'https://provider.example/a/../v1'},
    {'url': 'https://provider.example/a/%2e%2e/v1'},
    {'url': 'https://provider.example/v1', 'api_key': 'private-key\r\nX-Injected: bad'},
    {'url': 'https://provider.example/v1', 'api_key': 'private-key with-spaces'},
    {'url': 'https://provider.example/v1', 'api_key': 'private-key', 'api_key_env': 'TEST_PROVIDER_KEY'},
    {'url': 'https://provider.example/v1', 'api_key_env': 'invalid env name'},
])
def test_invalid_authenticated_endpoint_configuration_hides_secret_values(endpoint):
    with pytest.raises(ValidationError) as error:
        ComputeSettings(endpoints=[endpoint])
    assert 'private-key' not in str(error.value)
    assert 'password' not in str(error.value)


@pytest.mark.parametrize('value', [None, ''])
def test_missing_referenced_api_key_fails_configuration_instead_of_downgrading(monkeypatch, value):
    monkeypatch.delenv('TEST_PROVIDER_KEY', raising=False)
    if value is not None:
        monkeypatch.setenv('TEST_PROVIDER_KEY', value)
    with pytest.raises(ValidationError, match='must be set and nonempty'):
        ComputeSettings(endpoints=[{'url': 'https://provider.example/v1', 'api_key_env': 'TEST_PROVIDER_KEY'}])


def test_ranked_model_configuration_environment_and_legacy_strings(monkeypatch):
    monkeypatch.setenv('SUSI_COMPUTE_SMART', '[{"name":"best","ranking":92.5},"fallback"]')
    settings = ComputeSettings.from_env(small=[{'name': 'fast', 'ranking': -1}])
    assert [(model.name, model.ranking) for model in settings.chat_models('smart')] == [('best', 92.5), ('fallback', 0)]
    assert settings.chat_models('small')[0].ranking == -1
    exported = json.loads(settings.model_dump_json())
    assert exported['smart'] == [{'name': 'best', 'ranking': 92.5}, 'fallback']


@pytest.mark.parametrize('models', [
    [{'name': 'model', 'ranking': value}] for value in [True, '90', None, float('nan'), float('inf'), -float('inf')]
] + [[{'name': ''}], [{'name': 'with spaces'}], [{'name': 'model', 'extra': 1}],
     ['model', {'name': 'MODEL', 'ranking': 1}]])
def test_invalid_ranked_models_are_rejected(models):
    with pytest.raises(ValidationError):
        ComputeSettings(smart=models)


def measurements(app, values):
    """Supply successful calibration rates indexed by endpoint and advertised model."""
    service = app.state.calibration
    for host, model, rate, ttft in values:
        endpoint = next(endpoint for endpoint in service.chat.endpoints if httpx.URL(endpoint.url).host == host)
        service.results.setdefault(endpoint_id(endpoint), {})[model] = {
            'metrics': {'tokens_per_second': rate, 'ttft_seconds': ttft}}


@pytest.mark.parametrize('alias,rankings,samples,expected', [
    ('smart', [100, 10], [('first', 'preferred', 5, 1), ('second', 'fallback', 100, .1)], ('first', 'preferred')),
    ('smart', [10, 100], [('first', 'preferred', 100, .1), ('second', 'fallback', 5, 1)], ('second', 'fallback')),
    ('smart', [50, 50], [('first', 'preferred', 5, .1), ('second', 'fallback', 50, 1)], ('second', 'fallback')),
    ('small', [100, 10], [('first', 'preferred', 5, .1), ('second', 'fallback', 50, 1)], ('second', 'fallback')),
    ('small', [0, 0], [('first', 'preferred', 10, 1), ('second', 'fallback', 10, .1)], ('second', 'fallback')),
    ('small', [0, 0], [('second', 'fallback', 10, 1)], ('second', 'fallback')),
    ('small', [0, 0], [('first', 'preferred', None, 1), ('second', 'fallback', None, .1)], ('second', 'fallback')),
    ('smart', [0, 0], [], ('first', 'preferred')),
    ('small', [0, 0], [('first', 'preferred', 0, None), ('second', 'fallback', -1, None)], ('first', 'preferred')),
])
def test_ranking_and_endpoint_specific_speed_choose_the_model(alias, rankings, samples, expected):
    calls = []
    def handler(request):
        if request.method == 'GET':
            return inventory(['PREFERRED'] if request.url.host == 'first' else ['FALLBACK'])
        body = json.loads(request.content)
        calls.append((request.url.host, body['model'].casefold()))
        return httpx.Response(200, json={'model': body['model']})
    group = [{'name': name, 'ranking': ranking} for name, ranking in zip(('preferred', 'fallback'), rankings)]
    app = app_for(handler, smart=group, small=group)
    measurements(app, samples)
    with TestClient(app) as client:
        assert client.post('/v1/chat/completions', json=chat(alias)).status_code == 200
        assert client.get(CONNECTIONS).json()['active_connections'] == 0
    assert calls == [expected]


@pytest.mark.parametrize('stream', [False, True])
def test_target_header_freezes_the_selected_pair_and_exposes_it_to_browsers(stream):
    def handler(request):
        if request.method == 'GET':
            return inventory(['MÖDEL'])
        assert request.url.host == 'second'
        assert json.loads(request.content)['model'] == 'MÖDEL'
        # A simultaneous run must not alter the metadata already attached to this request.
        app.state.calibration.results.clear()
        headers = {'X-Compute-Target': 'untrusted upstream value', 'X-Secret': 'private'}
        if stream:
            return httpx.Response(200, stream=Chunks([b'data: [DONE]\n\n']),
                                  headers={**headers, 'Content-Type': 'text/event-stream'})
        return httpx.Response(200, json={'model': 'MÖDEL'}, headers=headers)
    app = app_for(handler, small=['mödel'])
    measurements(app, [('first', 'mödel', 3, .5), ('second', 'mödel', 42.5, .2)])
    with TestClient(app) as client:
        response = client.post('/v1/chat/completions', json=chat(stream=stream), headers={'Origin': 'https://gui.example'})
    assert response.status_code == 200
    value = response.headers['x-compute-target']
    assert value.isascii()
    assert json.loads(value) == {
        'endpoint_id': endpoint_id(app.state.calibration.chat.endpoints[1]), 'model': 'MÖDEL',
        'expected': {'tokens_per_second': 42.5, 'ttft_seconds': .2},
    }
    assert 'x-compute-target' in response.headers['access-control-expose-headers'].lower()
    assert 'x-secret' not in response.headers and 'private' not in value


@pytest.mark.parametrize('metrics', [None, {'tokens_per_second': float('nan'), 'ttft_seconds': -1},
                                     {'tokens_per_second': True, 'ttft_seconds': float('inf')}])
def test_target_header_reports_missing_or_invalid_calibration_as_null(metrics):
    def handler(request):
        return inventory(['small-model']) if request.method == 'GET' else httpx.Response(429, json={'error': 'busy'})
    app = app_for(handler)
    endpoint = app.state.calibration.chat.endpoints[0]
    app.state.calibration.results[endpoint_id(endpoint)] = {'small-model': {'metrics': metrics}}
    with TestClient(app) as client:
        response = client.post('/v1/chat/completions', json=chat())
        error = client.post('/v1/chat/completions', json=chat('unknown'))
        operation = app.openapi()['paths']['/v1/chat/completions']['post']
    assert json.loads(response.headers['x-compute-target'])['expected'] == {'tokens_per_second': None, 'ttft_seconds': None}
    assert response.status_code == 429
    assert 'x-compute-target' not in error.headers
    assert 'X-Compute-Target' in operation['responses']['200']['headers']


class HeldStream(httpx.AsyncByteStream):
    def __init__(self):
        self.finish = asyncio.Event()
        self.started = asyncio.Event()
        self.closed = False
    async def __aiter__(self):
        yield b'data: first\n\n'
        self.started.set()
        await self.finish.wait()
        yield b'data: [DONE]\n\n'
    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize('alias', ['smart', 'small'])
def test_concurrent_streams_balance_load_before_ranking_and_speed_and_hold_counts_until_closed(alias):
    async def scenario():
        calls = asyncio.Queue()
        async def handler(request):
            if request.method == 'GET':
                await asyncio.sleep(0)
                return inventory(['small-model'] if request.url.host == 'first' else ['fallback-model'])
            headers, stream = asyncio.Event(), HeldStream()
            calls.put_nowait((request.url.host, headers, stream))
            await headers.wait()
            return httpx.Response(200, stream=stream, headers={'Content-Type': 'text/event-stream'})
        models = [{'name': 'small-model', 'ranking': 100}, {'name': 'fallback-model', 'ranking': 1}]
        app = app_for(handler, smart=models, small=models)
        measurements(app, [('first', 'small-model', 100, .1), ('second', 'fallback-model', 1, 1)])
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url='http://compute') as client:
            tasks, streams = [], []
            try:
                for index, expected in enumerate(('first', 'second', 'first')):
                    tasks.append(asyncio.create_task(client.post('/v1/chat/completions', json=chat(alias, stream=True))))
                    host, headers, stream = await asyncio.wait_for(calls.get(), 2)
                    assert host == expected
                    status = await client.get(CONNECTIONS)
                    assert status.headers['cache-control'] == 'no-store'
                    snapshot = status.json()
                    assert snapshot['active_connections'] == index + 1
                    assert sum(endpoint['active_connections'] for endpoint in snapshot['endpoints']) == index + 1
                    headers.set()
                    await asyncio.wait_for(stream.started.wait(), 2)
                    streams.append(stream)
                assert [endpoint['models'] for endpoint in snapshot['endpoints']] == [
                    {'small-model': 2}, {'fallback-model': 1}]
                streams[0].finish.set()
                assert (await asyncio.wait_for(tasks[0], 2)).status_code == 200
                assert (await client.get(CONNECTIONS)).json()['active_connections'] == 2
                tasks[2].cancel()
                await asyncio.gather(tasks[2], return_exceptions=True)
                assert (await client.get(CONNECTIONS)).json()['active_connections'] == 1
                streams[1].finish.set()
                await asyncio.wait_for(tasks[1], 2)
                final = (await client.get(CONNECTIONS)).json()
                assert final['active_connections'] == 0
                assert all(endpoint['models'] == {} for endpoint in final['endpoints'])
                assert all(stream.closed for stream in streams)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(scenario())


def test_running_model_is_reused_and_becomes_replaceable_when_idle():
    async def scenario():
        calls = asyncio.Queue()
        async def handler(request):
            if request.method == 'GET':
                return inventory(['quality', 'shared'])
            stream = HeldStream()
            calls.put_nowait((json.loads(request.content)['model'], stream))
            return httpx.Response(200, stream=stream, headers={'Content-Type': 'text/event-stream'})
        app = app_for(handler, endpoints=['first:11434'], small=['shared'],
                      smart=[{'name': 'quality', 'ranking': 100}, {'name': 'shared', 'ranking': 1}])
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url='http://compute') as client:
            tasks = []
            try:
                for alias, expected in [('small', 'shared'), ('smart', 'shared')]:
                    tasks.append(asyncio.create_task(client.post('/v1/chat/completions', json=chat(alias, stream=True))))
                    model, stream = await asyncio.wait_for(calls.get(), 2)
                    assert model == expected
                    await asyncio.wait_for(stream.started.wait(), 2)
                active = (await client.get(CONNECTIONS)).json()
                assert active['endpoints'][0]['models'] == {'shared': 2}
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                tasks.append(asyncio.create_task(client.post('/v1/chat/completions', json=chat('smart', stream=True))))
                model, stream = await asyncio.wait_for(calls.get(), 2)
                assert model == 'quality'
                stream.finish.set()
                await asyncio.wait_for(tasks[-1], 2)
                assert (await client.get(CONNECTIONS)).json()['active_connections'] == 0
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(scenario())


def test_cancellation_waiting_for_headers_releases_the_reservation():
    async def scenario():
        entered = asyncio.Event()
        async def handler(request):
            if request.method == 'GET':
                return inventory(['small-model'])
            entered.set()
            await asyncio.Event().wait()
        app = app_for(handler)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url='http://compute') as client:
            task = asyncio.create_task(client.post('/v1/chat/completions', json=chat()))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                assert (await client.get(CONNECTIONS)).json()['active_connections'] == 1
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert (await client.get(CONNECTIONS)).json()['active_connections'] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['status', 'content_type', 'size', 'stream', 'close'])
def test_failed_upstream_responses_release_reservations(monkeypatch, failure):
    class CloseError(Chunks):
        async def aclose(self):
            raise httpx.ReadError('cleanup failed')
    def handler(request):
        if request.method == 'GET':
            return inventory(['small-model'])
        if failure == 'status':
            return httpx.Response(429, json={'error': 'busy'})
        if failure == 'content_type':
            return httpx.Response(200, json={'unexpected': 'json'})
        if failure == 'size':
            return httpx.Response(200, content=b'too large')
        cls = CloseError if failure == 'close' else Chunks
        return httpx.Response(200, stream=cls([b'data: first\n\n'], fail=failure == 'stream'),
                              headers={'Content-Type': 'text/event-stream'})
    app = app_for(handler)
    if failure == 'size':
        monkeypatch.setattr(text_service, 'MAX_RESPONSE_BYTES', 4)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post('/v1/chat/completions', json=chat(stream=failure != 'size'))
        value = client.get(CONNECTIONS).json()
        assert value['active_connections'] == 0
        assert all(endpoint['models'] == {} for endpoint in value['endpoints'])
        if failure == 'stream':
            rows = client.get('/api/v1/compute/monitoring/requests', params={'path': '/v1/chat/completions'}).json()['requests']
            assert rows[0]['state'] == 'failed'
            assert rows[0]['status_code'] == 200
            assert rows[0]['response_bytes'] == len(b'data: first\n\n')


def test_connection_read_is_inert_safe_and_matches_its_openapi_schema():
    from jsonschema import Draft202012Validator
    def handler(request):
        raise AssertionError('Reading connections must not contact upstreams.')
    app = app_for(handler, endpoints=[{'url': 'https://provider.example/v1', 'api_key': 'private-endpoint-key'}])
    with TestClient(app) as client:
        response = client.get(CONNECTIONS)
        snapshot = response.json()
        assert snapshot['active_connections'] == 0
        assert snapshot['endpoints'][0]['models'] == {}
        schema = app.openapi()['paths'][CONNECTIONS]['get']['responses']['200']['content']['application/json']['schema']
        Draft202012Validator(schema).validate(snapshot)
        assert snapshot['endpoints'][0]['id'] == client.get('/api/v1/compute/calibration').json()['endpoints'][0]['id']
        assert 'private-endpoint-key' not in response.text


def test_endpoint_objects_work_in_json_environment_config(monkeypatch):
    monkeypatch.setenv('TEST_PROVIDER_KEY', 'environment-test-key')
    monkeypatch.setenv('SUSI_COMPUTE_ENDPOINTS', json.dumps([
        'localhost:11434', {'url': 'https://provider.example/prefix/v1', 'api_key_env': 'TEST_PROVIDER_KEY'}]))
    settings = ComputeSettings.from_env()
    assert settings.chat_endpoints[0].url == 'http://localhost:11434/v1'
    assert settings.chat_endpoints[1].authorization_headers() == {'Authorization': 'Bearer environment-test-key'}
    assert 'environment-test-key' not in settings.model_dump_json()


def test_endpoint_base_url_duplicates_are_rejected_across_forms():
    with pytest.raises(ValidationError, match='must be unique'):
        ComputeSettings(endpoints=['localhost:80', {'url': 'http://LOCALHOST/v1/'}])


def test_real_authenticated_http_discovery_and_chat_preserve_api_prefix():
    with backend(['provider/smart-model'], prefix='/provider/api/v1', api_key='integration-test-key') as (address, calls):
        settings = ComputeSettings(endpoints=[{
            'url': 'http://' + address + '/provider/api/v1', 'api_key': 'integration-test-key',
        }], smart=['provider/smart-model'])
        with TestClient(create_compute_app(settings)) as client:
            response = client.post('/v1/chat/completions', json=chat('smart'))
        assert response.status_code == 200
        assert response.json()['model'] == 'provider/smart-model'
        assert calls == [chat('provider/smart-model')]
