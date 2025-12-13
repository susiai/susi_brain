"""Check triton_llmlb-compatible routing fields and the unparsed middle fast path."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from susi_brain.compute import stream_metadata
from susi_brain.compute.config import ComputeSettings
from susi_brain.compute.main import create_compute_app
from susi_brain.compute.text_service import ChatStream

METADATA = {'modelname': 'Mödel', 'compute_group': 'smart', 'compute_hostname': 'inference.example', 'request_bytes': 1234}
FIRST = b'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}\n'
MIDDLE = b'data: { "choices": [ { "delta": { "content": "<think>raw</think> answer", "tool_calls": [] }, "finish_reason": null } ] }\r\n'
TERMINAL = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
USAGE = b'data: {"choices":[],"usage":{"completion_tokens":7}}\n'


def payloads(output):
    return [json.loads(line[5:]) for line in output.splitlines() if line.startswith(b'data: {')]


@pytest.mark.parametrize('split', [1, 2, 7, 10000])
def test_first_and_terminal_objects_are_annotated_and_middle_usage_and_framing_are_untouched(monkeypatch, split):
    source = b': heartbeat\r\n\r\n' + FIRST + b'\n' + MIDDLE + b'\r\n' + TERMINAL + b'\n' + USAGE + b'\ndata: [DONE]'
    decoder, parsed = stream_metadata.StreamMetadata(METADATA), []
    original = stream_metadata.loads
    def counted(value):
        parsed.append(value)
        return original(value)
    monkeypatch.setattr(stream_metadata, 'loads', counted)
    result = b''.join(output for offset in range(0, len(source), split) for output in decoder.feed(source[offset:offset + split]))
    result += b''.join(decoder.feed(b'', final=True))
    assert len(parsed) == 2  # Role-only first and terminal; never middle content/usage.
    first, middle, last, usage = payloads(result)
    for key, value in METADATA.items():
        assert first[key] == last[key] == value
        assert key not in middle and key not in usage
    assert first['choices'] == json.loads(FIRST[5:])['choices']
    assert last['choices'] == json.loads(TERMINAL[5:])['choices']
    assert MIDDLE + b'\r\n' in result and USAGE + b'\ndata: [DONE]' in result
    assert result.startswith(b': heartbeat\r\n\r\n')
    assert decoder.done and decoder.terminal_annotated


def test_one_event_can_be_first_and_terminal_and_is_parsed_once(monkeypatch):
    calls, original = [], stream_metadata.loads
    def counted(value):
        calls.append(value)
        return original(value)
    monkeypatch.setattr(stream_metadata, 'loads', counted)
    decoder = stream_metadata.StreamMetadata(METADATA)
    result = b''.join(decoder.feed(TERMINAL + b'\ndata: [DONE]\n\n'))
    assert len(calls) == 1
    assert payloads(result)[0]['modelname'] == METADATA['modelname']
    assert result.endswith(b'data: [DONE]\n\n')


def test_invalid_nonobject_data_and_escaped_finish_reason_are_not_rewritten():
    # The sibling attempts the first data payload once, even when it is invalid.
    invalid = b'data: {not-json}\r\ndata: null\ndata: [1,2]\n'
    middle = b'data: {"choices":[{"delta":{"content":"\\\"finish_reason\\\": \\\"stop\\\""},"finish_reason":null}]}\n'
    decoder = stream_metadata.StreamMetadata(METADATA)
    output = b''.join(decoder.feed(invalid + middle + TERMINAL + b'\ndata: [DONE]\n\n'))
    assert output.startswith(invalid + middle)
    assert output.count(b'"modelname"') == 1
    annotated = next(line for line in output.splitlines() if b'"modelname"' in line)
    assert json.loads(annotated[5:])['modelname'] == METADATA['modelname']


@pytest.mark.parametrize('newline', [b'\n', b'\r', b'\r\n'])
def test_unterminated_eof_unicode_and_duplicate_terminal_annotations(newline):
    first = 'data: {"choices":[{"delta":{"content":"Grüße 👋"}}]}'.encode() + newline
    terminal = TERMINAL.rstrip(b'\n') + newline
    decoder = stream_metadata.StreamMetadata(METADATA)
    output = b''.join(decoder.feed(first + terminal + terminal + b'data: tail without newline'))
    output += b''.join(decoder.feed(b'', final=True))
    assert output.count(b'"modelname"') == 2
    assert 'Grüße 👋' in output.decode()
    assert output.endswith(terminal + b'data: tail without newline')


@pytest.mark.parametrize('done', [b'data: [DONE]\n\n', b'data: [DONE]'])
def test_done_closes_upstream_and_releases_reservation_without_waiting_for_eof(done):
    async def scenario():
        class Held(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                yield FIRST + b'\n' + TERMINAL + b'\n' + done
                await asyncio.Event().wait()
            async def aclose(self):
                self.closed = True
        class Connection:
            releases = 0
            def release(self):
                self.releases += 1
        upstream, connection, bodies = Held(), Connection(), []
        response = ChatStream(httpx.Response(200, stream=upstream), metadata=METADATA, connection=connection)
        async def send(message):
            if message['type'] == 'http.response.body':
                bodies.append(message.get('body', b''))
        async def receive():
            return {'type': 'http.disconnect'}
        await asyncio.wait_for(response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send), 1)
        assert upstream.closed and connection.releases == 1
        assert b''.join(bodies).endswith(done)
    asyncio.run(scenario())


def test_proxy_metadata_uses_selected_target_and_exact_incoming_utf8_body_not_headers_or_reserialization():
    received = []
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            def forged(source):
                value = json.loads(source[5:])
                value.update(model='provider-alias', modelname='wrong-model', compute_group='wrong-group',
                             compute_hostname='wrong-host', request_bytes=999999)
                return b'data: ' + json.dumps(value).encode() + b'\n\n'
            yield forged(FIRST) + MIDDLE + b'\r\n' + forged(TERMINAL) + USAGE + b'\ndata: [DONE]\n\n'
        async def aclose(self):
            pass
    def handler(request):
        assert request.url.host == 'llm.example'
        assert request.headers['authorization'] == 'Bearer endpoint-secret'
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': 'MÖDEL'}]})
        received.append(json.loads(request.content))
        return httpx.Response(200, stream=Chunks(), headers={'Content-Type': 'text/event-stream'})
    app = create_compute_app(ComputeSettings(endpoints=[{'url': 'https://llm.example:8443/tenant/v1', 'api_key': 'endpoint-secret'}],
        smart=['mödel']), chat_transport=httpx.MockTransport(handler))
    raw = '{\n "model" : "smart", "stream": true, "messages" : [{"role":"user", "content":"Grüße"}]\n}'.encode()
    with TestClient(app, root_path='/proxy') as client:
        response = client.post('/proxy/v1/chat/completions', content=raw, headers={'Content-Type': 'application/json'})
        assert response.status_code == 200
        first, middle, last, usage = payloads(response.content)
        expected = {'modelname': 'MÖDEL', 'compute_group': 'smart', 'compute_hostname': 'llm.example', 'request_bytes': len(raw)}
        for key, value in expected.items():
            assert first[key] == last[key] == value
            assert key not in middle and key not in usage
        assert first['model'] == last['model'] == 'provider-alias'
        assert received[0]['model'] == 'MÖDEL' and 'stream_options' not in received[0]
        assert 'endpoint-secret' not in response.text and ':8443' not in response.text
        assert json.loads(response.headers['x-compute-target'])['model'] == 'MÖDEL'
        assert client.get('/proxy/api/v1/compute/chat/connections').json()['active_connections'] == 0
