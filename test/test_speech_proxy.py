"""Verify WAV forwarding, Ollama discovery, shared load and speech measurements."""

import asyncio
import io
import json
import struct
import time
import wave

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from susi_brain.compute import speech_service
from susi_brain.compute.calibration_api import SNAPSHOT
from susi_brain.compute.config import ComputeSettings
from susi_brain.compute.main import create_compute_app
from susi_brain.compute.routing import endpoint_id
from susi_brain.compute.wav import wav_header, wav_duration

PATH = '/v1/audio/speech'
COUNTS = '/api/v1/compute/chat/connections'
CALIBRATION = '/api/v1/compute/calibration'


def audio(*, unknown=False):
    content = io.BytesIO()
    with wave.open(content, 'wb') as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(24000)
        file.writeframes(struct.pack('<h', 123) * 48000)
    result = bytearray(content.getvalue())
    if unknown:
        struct.pack_into('<I', result, 4, 0xffffffff)
        struct.pack_into('<I', result, 40, 0xffffffff)
    return bytes(result)


class AudioChunks(httpx.AsyncByteStream):
    def __init__(self, data=None, *, fail=False, clock=None):
        self.data, self.fail, self.clock, self.closed = data or audio(), fail, clock, False

    async def __aiter__(self):
        origin = self.clock.value if self.clock else None
        for offset, part in [(0.1, self.data[:18]), (0.2, self.data[18:44]),
                             (0.4, self.data[44:46]), (1.0, self.data[46:])]:
            if self.clock:
                self.clock.value = origin + offset
            yield part
        if self.fail:
            raise httpx.ReadError('private transport failure')

    async def aclose(self):
        self.closed = True


def app_for(handler, **changes):
    values = {'endpoints': ['first:11434', 'second:11434'], 'small': ['chat'], 'speech': ['phonon']}
    values.update(changes)
    return create_compute_app(ComputeSettings(**values), chat_transport=httpx.MockTransport(handler))


def metadata(voices=None):
    return httpx.Response(200, json={'capabilities': ['speech'], 'voices': voices or ['default', 'Fréya']})


def inventory():
    return httpx.Response(200, json={'data': [{'id': 'phonon:latest'}, {'id': 'chat'}]})


def payload(**changes):
    return {'model': 'phonon', 'input': 'Grüße aus Compute.', **changes}


@pytest.mark.parametrize('unknown', [False, True])
def test_wav_stream_auth_prefix_alias_cleanup_monitoring_and_cors(unknown, tmp_path):
    data, streams, calls = audio(unknown=unknown), [], []
    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.url.host == 'ollama.example'
        assert request.headers['authorization'] == 'Bearer endpoint-secret'
        assert request.headers['x-request-id'] == 'speech-test'
        assert 'cookie' not in request.headers
        if request.method == 'GET':
            assert request.url.path == '/prefix/v1/models'
            return inventory()
        if request.url.path == '/prefix/api/show':
            assert json.loads(request.content) == {'model': 'phonon:latest'}
            return metadata()
        assert request.url.path == '/prefix/v1/audio/speech'
        assert json.loads(request.content) == payload(model='phonon:latest', voice='freya', response_format='wav', stream_format='audio')
        stream = AudioChunks(data)
        streams.append(stream)
        return httpx.Response(200, stream=stream, headers={'Content-Type': 'audio/wav', 'Set-Cookie': 'private=cookie'})
    app = app_for(handler, endpoints=[{'url': 'https://ollama.example/prefix/v1', 'api_key': 'endpoint-secret'}],
                  request_log_directory=tmp_path / 'logs')
    with TestClient(app, root_path='/gateway') as client:
        assert client.get('/gateway' + CALIBRATION).json()['speech_models'] == [{'name': 'phonon', 'voice': 'default'}]
        assert not calls
        response = client.post('/gateway' + PATH, json=payload(voice='freya'), headers={
            'Authorization': 'Bearer caller-secret', 'Cookie': 'caller=secret', 'X-Request-ID': 'speech-test', 'Origin': 'https://client.example'})
        assert response.status_code == 200 and response.content == data
        assert response.headers['content-type'] == 'audio/wav'
        assert 'set-cookie' not in response.headers
        assert 'X-Compute-Target' in response.headers['access-control-expose-headers']
        target = json.loads(response.headers['x-compute-target'])
        assert target['model'] == 'phonon:latest' and target['voice'] == 'freya'
        assert target['expected'] == {'ttfa_seconds': None, 'real_time_factor': None}
        assert client.get('/gateway' + COUNTS).json()['active_connections'] == 0
        rows = client.get('/gateway/api/v1/compute/monitoring/requests').json()['requests']
        row = next(row for row in rows if row['path'] == PATH)
        assert row['model_group'] == 'speech' and row['model'] == 'phonon:latest' and row['response_bytes'] == len(data)
        assert row['state'] == 'completed'
    assert all(stream.closed for stream in streams)
    archive = next((tmp_path / 'logs').glob('*.jsonl')).read_text()
    assert 'Grüße' not in archive and 'secret' not in archive and 'Fréya' not in archive
    restored = create_compute_app(app.state.settings, chat_transport=httpx.MockTransport(handler))
    with TestClient(restored) as client:
        rows = client.get('/api/v1/compute/monitoring/requests', params={'path': PATH}).json()['requests']
        assert len(rows) == 1 and rows[0]['model_group'] == 'speech'


@pytest.mark.parametrize('body', [None, {}, payload(model='smart'), payload(model='unknown'), payload(input=' '),
    payload(input='a' * 4097), payload(input=1), payload(voice=''), payload(voice='bad\nvoice'),
    payload(response_format='mp3'), payload(stream_format='sse'), payload(speed=2), payload(speed=True),
    payload(instructions='quiet'), payload(stream=True)])
def test_invalid_requests_never_contact_servers(body):
    def unexpected(request):
        raise AssertionError('Invalid speech must not contact providers.')
    with TestClient(app_for(unexpected)) as client:
        response = client.post(PATH, json=body)
    assert response.status_code == 400 and 'error' in response.json()


@pytest.mark.parametrize('value', [[''], ['space name'], ['a' * 257], ['phonon', 'phonon:latest'],
    [{'name': 'phonon', 'voice': '\n'}], [{'name': 'phonon', 'unknown': True}]])
def test_invalid_speech_configuration(value):
    with pytest.raises(ValidationError):
        ComputeSettings(speech=value)
    with pytest.raises(ValidationError):
        ComputeSettings(speech=['phonon'], small=['phonon:latest'])


@pytest.mark.parametrize('mode,status', [('not_speech', 503), ('voice', 503), ('metadata_redirect', 503),
    ('metadata_invalid', 503), ('metadata_timeout', 503), ('redirect', 502), ('connection', 502), ('timeout', 504),
    ('not_wav', 502), ('wrong_rate', 502), ('empty', 502), ('too_large', 502), ('rate_limit', 429)])
def test_errors_do_not_retry_and_release_every_reservation(mode, status, monkeypatch):
    posts = []
    def handler(request):
        if request.method == 'GET':
            return inventory()
        if request.url.path.endswith('/api/show'):
            if mode == 'not_speech':
                return httpx.Response(200, json={'capabilities': ['completion']})
            if mode == 'metadata_redirect':
                return httpx.Response(302, headers={'Location': 'https://foreign.example/steal'})
            if mode == 'metadata_invalid':
                return httpx.Response(200, json={'capabilities': ['speech'], 'voices': [12]})
            if mode == 'metadata_timeout':
                raise httpx.ReadTimeout('private failure')
            return metadata(['default'])
        posts.append(request.url.host)
        if mode == 'redirect':
            return httpx.Response(307, headers={'Location': 'https://foreign.example/steal'})
        if mode == 'connection':
            raise httpx.ConnectError('private failure')
        if mode == 'timeout':
            raise httpx.ReadTimeout('private failure')
        if mode == 'rate_limit':
            return httpx.Response(429, json={'error': {'message': 'busy'}}, headers={'Retry-After': '12'})
        content = b'not wav' if mode == 'not_wav' else b'' if mode == 'empty' else audio()
        if mode == 'wrong_rate':
            content = bytearray(content); struct.pack_into('<I', content, 24, 16000)
        return httpx.Response(200, content=bytes(content), headers={'Content-Type': 'audio/wav'})
    if mode == 'too_large':
        monkeypatch.setattr(speech_service, 'MAX_AUDIO_BYTES', 100)
    with TestClient(app_for(handler)) as client:
        response = client.post(PATH, json=payload(voice='missing' if mode == 'voice' else 'default'))
        assert client.get(COUNTS).json()['active_connections'] == 0
    assert response.status_code == status
    assert 'private' not in response.text and 'foreign' not in response.text
    assert len(posts) <= 1
    if mode == 'rate_limit':
        assert response.headers['retry-after'] == '12'


@pytest.mark.parametrize('first_task', ['chat', 'speech'])
def test_simultaneous_chat_and_speech_choose_the_other_endpoint(first_task):
    async def scenario():
        entered, release, posts = asyncio.Event(), asyncio.Event(), []
        async def handler(request):
            if request.method == 'GET':
                return inventory()
            if request.url.path.endswith('/api/show'):
                return metadata()
            posts.append((request.url.host, request.url.path))
            if len(posts) == 1:
                entered.set(); await release.wait()
            if request.url.path.endswith('/audio/speech'):
                return httpx.Response(200, stream=AudioChunks(), headers={'Content-Type': 'audio/wav'})
            return httpx.Response(200, json={'model': 'chat'})
        app = app_for(handler)
        routing = app.state.calibration.chat.routing
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            async def call(kind):
                return await client.post(PATH if kind == 'speech' else '/v1/chat/completions',
                    json=payload() if kind == 'speech' else {'model': 'small', 'messages': [{'role': 'user', 'content': 'Hello'}]})
            first = asyncio.create_task(call(first_task))
            await asyncio.wait_for(entered.wait(), 1)
            assert routing.snapshot()['endpoints'][0]['active_connections'] == 1
            second = await call('speech' if first_task == 'chat' else 'chat')
            assert second.status_code == 200 and posts[1][0] == 'second'
            assert routing.snapshot()['active_connections'] == 1
            release.set()
            assert (await first).status_code == 200
            assert routing.snapshot()['active_connections'] == 0
    asyncio.run(scenario())


def test_audio_disconnect_and_midstream_failure_close_and_release_without_json():
    async def scenario(fail):
        app = app_for(lambda request: None)
        routing = app.state.calibration.chat.routing
        endpoint = routing.endpoints[0]
        upstream = AudioChunks(fail=fail)
        response = speech_service.WaveStream(httpx.Response(200, stream=upstream), upstream.__aiter__(),
            audio()[:46], wav_header(audio()), routing.reserve(endpoint, 'phonon:latest'), '{}')
        sent = []
        async def send(message):
            sent.append(message)
            if not fail and message['type'] == 'http.response.body':
                raise OSError('client disconnected')
        async def receive():
            return {'type': 'http.disconnect'}
        with pytest.raises(httpx.ReadError if fail else ClientDisconnect):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        assert upstream.closed and routing.snapshot()['active_connections'] == 0
        assert all(b'error' not in message.get('body', b'') for message in sent)
    asyncio.run(scenario(False)); asyncio.run(scenario(True))


def wait(client):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = client.get(CALIBRATION).json()
        Draft202012Validator(SNAPSHOT).validate(value)
        if value['run']['state'] != 'running':
            return value
        time.sleep(0.005)
    pytest.fail('Calibration did not finish.')


def test_speech_calibration_default_scope_order_metrics_voices_and_routing():
    class Clock:
        value = 10.0
        def __call__(self):
            return self.value
    clock, events, streams = Clock(), [], []
    def handler(request):
        if request.method == 'GET':
            return inventory()
        if request.url.path.endswith('/api/show'):
            return metadata()
        body = json.loads(request.content)
        assert request.url.path == '/v1/audio/speech'  # Speech models never receive chat probes.
        events.append((request.url.host, body['input']))
        stream = AudioChunks(audio(unknown=True), clock=clock)
        streams.append(stream)
        return httpx.Response(200, stream=stream, headers={'Content-Type': 'audio/wav'})
    app = app_for(handler, small=[])
    calibration = app.state.calibration
    calibration.clock = clock
    with TestClient(app) as client:
        assert client.post(CALIBRATION).status_code == 202
        snapshot = wait(client)
        assert snapshot['run']['state'] == 'completed' and snapshot['run']['total_models'] == 2
        assert all('Hello.' in prompt for _, prompt in events[:2])
        assert all('speech test' in prompt for _, prompt in events[2:])
        for records in snapshot['results'].values():
            record = records['phonon:latest']
            assert record['task'] == 'speech' and record['available'] is True
            assert record['capabilities'] == {'forms': None, 'vision': None, 'thinking': None, 'speech': True}
            assert record['speech']['voices'] == ['default', 'Fréya']
            assert all(check['state'] == 'passed' for check in record['checks'].values())
            assert record['metrics']['ttfa_seconds'] == pytest.approx(.4)
            assert record['metrics']['duration_seconds'] == pytest.approx(1)
            assert record['metrics']['audio_duration_seconds'] == 2
            assert record['metrics']['real_time_factor'] == pytest.approx(.5)
            assert 'tokens_per_second' not in record['metrics']
        # At equal load, the fastest calibrated exact voice wins.
        first, second = calibration.chat.endpoints
        calibration.results[endpoint_id(second)]['phonon:latest']['metrics']['real_time_factor'] = .1
        response = client.post(PATH, json=payload())
        target = json.loads(response.headers['x-compute-target'])
        assert target['endpoint_id'] == endpoint_id(second)
        assert target['expected']['real_time_factor'] == .1
        assert client.get(COUNTS).json()['active_connections'] == 0
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize('scope', [{}, {'scope': 'all'}, {'scope': 'all', 'models': ['phonon:latest']}])
def test_speech_scope_classification_and_bad_audio_never_run_speed(scope):
    calls = []
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': 'phonon:latest'}]})
        if request.url.path.endswith('/api/show'):
            return metadata()
        calls.append(request.url.path)
        return httpx.Response(200, content=b'broken', headers={'Content-Type': 'audio/wav'})
    app = app_for(handler, endpoints=['first:11434'], small=[])
    with TestClient(app) as client:
        assert client.post(CALIBRATION, json=scope).status_code == 202
        record = wait(client)['results'][endpoint_id(app.state.calibration.chat.endpoints[0])]['phonon:latest']
    assert record['task'] == 'speech' and record['available'] is not True
    assert record['checks']['speed']['state'] == 'skipped'
    assert calls == [PATH]


def test_wav_parser_rejects_truncation_and_accepts_unknown_stream_lengths():
    data = audio()
    assert wav_header(data[:40]) is None
    assert wav_duration(wav_header(data), len(data)) == 2
    assert wav_duration(wav_header(audio(unknown=True)), len(data)) == 2
    with pytest.raises(ValueError):
        wav_duration(wav_header(data), len(data) - 2)
    with pytest.raises(ValueError):
        wav_duration(wav_header(audio(unknown=True)), len(data) - 1)


def test_calibration_detects_unconfigured_speech_in_all_scope_and_accepts_configured_latest_filter():
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': 'phonon:latest'}]})
        if request.url.path.endswith('/api/show'):
            return metadata()
        assert request.url.path == PATH
        return httpx.Response(200, stream=AudioChunks(), headers={'Content-Type': 'audio/wav'})
    for speech, body in [([], {'scope': 'all'}), (['phonon'], {'models': ['phonon:latest']})]:
        app = app_for(handler, speech=speech, small=[], endpoints=['first:11434'])
        with TestClient(app) as client:
            assert client.post(CALIBRATION, json=body).status_code == 202
            record = next(iter(next(iter(wait(client)['results'].values())).values()))
            assert record['task'] == 'speech' and record['capabilities']['speech'] is True
            if speech:
                assert client.post(CALIBRATION, json={'scope': 'all', 'models': ['phonon', 'phonon:latest']}).status_code == 422


def test_calibration_cancellation_releases_shared_speech_reservation_and_closes_audio():
    class Blocked(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b'never'
        async def aclose(self):
            self.closed = True
    stream = Blocked()
    def handler(request):
        if request.method == 'GET':
            return inventory()
        if request.url.path.endswith('/api/show'):
            return metadata()
        return httpx.Response(200, stream=stream, headers={'Content-Type': 'audio/wav'})
    with TestClient(app_for(handler, endpoints=['first:11434'], small=[])) as client:
        assert client.post(CALIBRATION).status_code == 202
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = client.get(CALIBRATION).json()
            if result['run']['current'] and result['run']['current']['step'] == 'speech':
                break
            time.sleep(.005)
        else:
            pytest.fail('No speech probe started.')
        counts = client.get(COUNTS).json()
        assert counts['active_connections'] == 1 and counts['endpoints'][0]['models'] == {'phonon:latest': 1}
        assert client.delete(CALIBRATION).json()['run']['state'] == 'cancelled'
        assert client.get(COUNTS).json()['active_connections'] == 0
        assert stream.closed


def test_speech_contract_assets_and_metadata_reads_are_inert():
    app = app_for(lambda request: pytest.fail('Snapshot and page loading must never contact Ollama.'))
    with TestClient(app) as client:
        assert client.get('/api/v1/compute/capabilities').json()['tasks'] == ['chat', 'transcription', 'speech']
        page = client.get('/api/v1/compute/ui')
        assert "media-src 'self' blob:" in page.headers['content-security-policy']
        assert client.get('/api/v1/compute/tts.js').status_code == 200
        snapshot = client.get(CALIBRATION).json()
        assert snapshot['speech_models'] == [{'name': 'phonon', 'voice': 'default'}]
        assert snapshot['results'] == {} and snapshot['run'] is None
        assert client.post(PATH, content='broken', headers={'Content-Type': 'application/json'}).status_code == 422
    spec = app.openapi()['paths'][PATH]['post']
    assert set(spec['responses']['200']['content']) == {'audio/wav'}
    assert spec['requestBody']['content']['application/json']['schema']['properties']['response_format']['default'] == 'wav'


def test_mixed_chat_and_speech_calibration_finishes_all_features_before_speed():
    events = []
    def handler(request):
        if request.method == 'GET':
            return inventory()
        if request.url.path.endswith('/api/show'):
            return metadata()
        body = json.loads(request.content)
        if request.url.path == PATH:
            events.append('speed' if 'The speech test' in body['input'] else 'feature')
            return httpx.Response(200, stream=AudioChunks(), headers={'Content-Type': 'audio/wav'})
        events.append('speed' if body['stream'] else 'feature')
        if body['stream']:
            return httpx.Response(200, content=(
                b'data: {"choices":[{"delta":{"content":"one two"},"finish_reason":"stop"}]}\n\n'
                b'data: {"choices":[],"usage":{"completion_tokens":2}}\n\ndata: [DONE]\n\n'),
                headers={'Content-Type': 'text/event-stream'})
        reply = {'content': '42', 'reasoning_content': 'A short calculation.'}
        if 'response_format' in body:
            reply['content'] = '{"mood":"angry"}'
        return httpx.Response(200, json={'choices': [{'message': reply}]})
    with TestClient(app_for(handler, endpoints=['first:11434'])) as client:
        assert client.post(CALIBRATION).status_code == 202
        result = wait(client)
        assert result['run']['state'] == 'completed' and result['run']['completed_models'] == 2
        assert events.count('speed') == 2
        assert max(index for index, kind in enumerate(events) if kind == 'feature') < events.index('speed')
        assert client.get(COUNTS).json()['active_connections'] == 0
