"""Ollama WAV synthesis through the shared authenticated HTTP pool and routing."""

import asyncio
import json
import unicodedata

import httpx
from fastapi import Body, Request
from fastapi.responses import Response, StreamingResponse

from susi_brain.shared.http.errors import ServiceError
from susi_brain.shared.observability.correlation import outbound_headers
from .config import speech_model_key
from .routing import endpoint_id
from .text_service import bounded_body, proxy_error, MAX_MODELS_BYTES
from .wav import wav_header, wav_duration, MAX_WAV_HEADER, UNKNOWN_SIZE

MAX_AUDIO_BYTES = 32 * 1024 * 1024
WAV_TYPES = {'audio/wav', 'audio/x-wav', 'audio/wave', 'audio/vnd.wave', 'application/octet-stream'}


def voice_key(value):
    return ''.join(c for c in unicodedata.normalize('NFD', value).casefold() if not unicodedata.combining(c))


class WaveStream(StreamingResponse):
    """Forward bounded audio bytes; a broken stream ends instead of appending JSON."""

    def __init__(self, upstream, iterator, prefix, info, connection, target_header):
        self.upstream, self.iterator, self.prefix, self.info = upstream, iterator, prefix, info
        self.connection = connection
        super().__init__(self.chunks(), status_code=upstream.status_code, media_type='audio/wav', headers={
            'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no',
            'X-Compute-Target': target_header, 'X-Content-Type-Options': 'nosniff',
        })

    async def chunks(self):
        size = len(self.prefix)
        yield self.prefix
        async for chunk in self.iterator:
            size += len(chunk)
            if size > MAX_AUDIO_BYTES:
                raise ValueError('The upstream audio stream exceeds its size limit.')
            yield chunk
        wav_duration(self.info, size)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self.upstream.aclose()
            finally:
                self.connection.release()


class SpeechSynthesis:
    def __init__(self, chat):
        self.chat, self.settings, self.routing = chat, chat.settings, chat.routing

    async def describe(self, endpoint, model):
        """Read only Ollama's bounded /api/show metadata, preserving a deployment prefix."""
        if not endpoint.url.endswith('/v1'):
            raise ServiceError(503, 'speech_metadata_unavailable', 'Ollama requires an API base ending in /v1.')
        try:
            async def read():
                async with self.chat.client.stream('POST', endpoint.url[:-3] + '/api/show',
                        json={'model': model}, headers={**outbound_headers(), **endpoint.authorization_headers()},
                        timeout=self.settings.model_probe_timeout_seconds) as response:
                    if response.status_code != 200:
                        raise ServiceError(503, 'speech_metadata_unavailable', 'Ollama model metadata is unavailable.')
                    value = json.loads(await bounded_body(response, MAX_MODELS_BYTES))
                    if not isinstance(value, dict) or not isinstance(value.get('capabilities'), list):
                        raise ValueError()
                    if 'speech' not in value['capabilities']:
                        raise ServiceError(503, 'not_speech_model', 'The model does not advertise speech synthesis.')
                    voices = value.get('voices', ['default'])
                    if (not isinstance(voices, list) or len(voices) > 1024 or any(not isinstance(voice, str)
                            or not voice.strip() or len(voice) > 256
                            or any(ord(c) < 32 or ord(c) == 127 for c in voice) for voice in voices)):
                        raise ValueError()
                    return {'voices': list(dict.fromkeys(['default', *voices])), 'formats': ['wav'],
                            'sample_rate': 24000, 'channels': 1, 'sample_width': 2}
            return await asyncio.wait_for(read(), self.settings.model_probe_timeout_seconds)
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError, RecursionError):
            raise ServiceError(503, 'speech_metadata_unavailable', 'Ollama model metadata is unavailable or invalid.') from None

    async def candidate(self, endpoint, requested, voice):
        names = await self.chat.models(endpoint)
        model = next((name for name in names if speech_model_key(name) == speech_model_key(requested)), None)
        if model is None:
            return None
        try:
            description = await self.describe(endpoint, model)
        except ServiceError:
            return None
        if not any(voice_key(name) == voice_key(voice) for name in description['voices']):
            return None
        return endpoint, model

    async def post(self, request: Request, data: dict | None = Body(default=None)):
        data = data or {}
        requested = data.get('model')
        configured = next((model for model in self.settings.speech_models
                           if isinstance(requested, str) and speech_model_key(model.name) == speech_model_key(requested)), None)
        if configured is None:
            return proxy_error(400, 'invalid_model', 'Choose a configured speech model.')
        if not isinstance(data.get('input'), str) or not data['input'].strip() or len(data['input']) > 4096:
            return proxy_error(400, 'invalid_input', 'Provide nonempty input of at most 4096 characters.')
        voice = data.get('voice', configured.voice)
        if (not isinstance(voice, str) or not voice.strip() or len(voice) > 256
                or any(ord(c) < 32 or ord(c) == 127 for c in voice)):
            return proxy_error(400, 'invalid_voice', 'Provide a nonempty voice name of at most 256 characters.')
        if (set(data) - {'model', 'input', 'voice', 'response_format', 'speed', 'stream_format'}
                or data.get('response_format', 'wav') != 'wav'
                or type(data.get('speed', 1)) not in (int, float) or data.get('speed', 1) != 1
                or data.get('stream_format', 'audio') != 'audio'):
            return proxy_error(400, 'unsupported_speech_option', 'Only WAV, speed 1 and stream_format audio are supported.')
        if self.chat.client is None:
            return proxy_error(503, 'speech_unavailable', 'The speech transport has not started.')
        candidates = await asyncio.gather(*(self.candidate(endpoint, configured.name, voice) for endpoint in self.chat.endpoints))
        target = self.routing.select_speech([item for item in candidates if item is not None], voice)
        if target is None:
            return proxy_error(503, 'speech_unavailable', 'No configured Ollama endpoint offers this speech model and voice.')
        endpoint, model, connection = target
        request.state.compute_target = {'group': 'speech', 'model': model, 'endpoint_id': endpoint_id(endpoint)}
        header = json.dumps({'endpoint_id': endpoint_id(endpoint), 'model': model, 'voice': voice,
                             'expected': self.routing.speech_expected(endpoint, model, voice)}, ensure_ascii=True, separators=(',', ':'))
        upstream = None
        try:
            upstream = await self.chat.client.send(self.chat.client.build_request('POST', endpoint.url + '/audio/speech',
                json={**data, 'model': model, 'voice': voice, 'response_format': 'wav', 'stream_format': 'audio'},
                headers={**outbound_headers(), **endpoint.authorization_headers(), 'Accept': 'audio/wav'},
                timeout=self.settings.speech_timeout_seconds), stream=True)
            if 300 <= upstream.status_code < 400:
                return proxy_error(502, 'upstream_redirect', 'The selected speech server returned a redirect.')
            if not upstream.is_success:
                content = await asyncio.wait_for(bounded_body(upstream, MAX_MODELS_BYTES), self.settings.speech_timeout_seconds)
                headers = {key: upstream.headers[key] for key in ('content-type', 'retry-after') if key in upstream.headers}
                return Response(content, status_code=upstream.status_code, headers=headers)
            if upstream.headers.get('content-type', '').split(';', 1)[0].strip().lower() not in WAV_TYPES:
                raise ValueError('Expected audio.')
            iterator, prefix, info = upstream.aiter_bytes(), bytearray(), None
            # Validate the header and wait for real PCM before committing the HTTP success.
            async def first_audio():
                nonlocal info
                async for chunk in iterator:
                    prefix.extend(chunk)
                    if len(prefix) > MAX_AUDIO_BYTES:
                        raise ValueError('Audio exceeds its size limit.')
                    info = wav_header(prefix)
                    if info is not None and info['data_size'] != UNKNOWN_SIZE and info['offset'] + info['data_size'] > MAX_AUDIO_BYTES:
                        raise ValueError('Declared audio exceeds its size limit.')
                    if info is not None and len(prefix) >= info['offset'] + info['bytes_per_frame']:
                        return
                    if len(prefix) > MAX_WAV_HEADER:
                        raise ValueError('Invalid WAV header.')
                raise ValueError('Empty audio.')
            await asyncio.wait_for(first_audio(), self.settings.speech_timeout_seconds)
            response = WaveStream(upstream, iterator, bytes(prefix), info, connection, header)
            upstream = connection = None
            return response
        except (httpx.TimeoutException, asyncio.TimeoutError):
            return proxy_error(504, 'upstream_timeout', 'The selected speech server timed out.')
        except httpx.HTTPError:
            return proxy_error(502, 'upstream_unavailable', 'The selected speech server could not complete the request.')
        except (ValueError, RecursionError):
            return proxy_error(502, 'invalid_upstream_audio', 'The speech server returned invalid or oversized WAV audio.')
        finally:
            try:
                if upstream is not None:
                    await upstream.aclose()
            finally:
                if connection is not None:
                    connection.release()
