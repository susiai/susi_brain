"""Explicit endpoint/model probes and process-local GUI snapshots.

Forms, vision and thinking probes follow Project Euler LLM Benchmark's probe
ideas. See shared/NOTICE.md for the unchanged vision fixture's provenance.
"""

import asyncio
import base64
import copy
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from susi_brain.shared.http.errors import ServiceError
from susi_brain.shared.http.sse import SSEDecoder
from susi_brain.shared.observability.correlation import outbound_headers
from .text_service import bounded_body
from .routing import endpoint_id
from .config import speech_model_key
from .speech_service import SpeechSynthesis, WAV_TYPES, voice_key
from .wav import wav_header, wav_duration

PROBE_VERSION = 'compute-calibration-v2'
VISION_IMAGE = Path(__file__).with_name('fixtures') / 'vision-42.png'
MAX_PROBE_BYTES = 1024 * 1024
STEPS = ('availability', 'forms', 'vision', 'thinking', 'speed')
FORM_CASES = (
    ('I hate programming', 'angry'),
    ('I love programming', 'happy'),
    ('Wait, that worked perfectly?', 'surprised'),
)


def now():
    return datetime.now(UTC).isoformat()


def unique(values):
    result = {}
    for value in values:
        result.setdefault(value.casefold(), value)
    return list(result.values())


def message(payload):
    try:
        choices = payload['choices']
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0]['message'], dict):
            raise ValueError()
        return choices[0]['message']
    except (KeyError, TypeError, ValueError, IndexError):
        raise ProbeFailure('invalid_response') from None


def text(value):
    content = value.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(part.get('text', '') for part in content
                       if isinstance(part, dict) and isinstance(part.get('text'), str))
    return ''


def reasoning(value):
    for key in ('reasoning_content', 'reasoning'):
        if isinstance(value.get(key), str) and value[key].strip():
            return True
    details = value.get('reasoning_details')
    if isinstance(details, list) and any(isinstance(item, dict) and any(
            isinstance(item.get(key), str) and item[key].strip() for key in ('text', 'summary')) for item in details):
        return True
    return any(match.group(2).strip() for match in re.finditer(
        r'<(think|thinking)>(.*?)</\1>', text(value), flags=re.DOTALL | re.IGNORECASE))


class ProbeFailure(Exception):
    def __init__(self, code, http_status=None):
        self.code, self.http_status = code, http_status
        super().__init__(code)


class EndpointCalibration:
    def __init__(self, chat, *, clock=None):
        self.chat = chat
        self.settings = chat.settings
        self.clock = clock or time.monotonic
        self.endpoints = {endpoint_id(endpoint): endpoint for endpoint in chat.endpoints}
        self.results = chat.routing.measurements
        self.speech = SpeechSynthesis(chat)
        self.run = None
        self.task = None

    def snapshot(self):
        return copy.deepcopy({
            'probe_version': PROBE_VERSION,
            'storage': 'process-local; replaced by each new run; cleared on restart',
            'default_scope': 'configured',
            'model_groups': {group: [model.model_dump() for model in self.settings.chat_models(group)]
                             for group in ('smart', 'small')},
            'speech_models': [model.model_dump() for model in self.settings.speech_models],
            'endpoints': [{'id': key, 'url': endpoint.url,
                           'key_configured': bool(endpoint.authorization_headers())}
                          for key, endpoint in self.endpoints.items()],
            'run': self.run,
            'results': self.results,
        })

    def start(self, body):
        if self.task is not None and not self.task.done():
            raise ServiceError(409, 'calibration_running', 'A calibration is already running.')
        if self.chat.client is None:
            raise ServiceError(503, 'not_ready', 'The chat transport has not started.')
        body = body if body is not None else {}
        if set(body) - {'scope', 'endpoint_ids', 'models'}:
            raise ServiceError(422, 'invalid_calibration_request', 'Unknown calibration options.')
        scope = body.get('scope', 'configured')
        if scope not in ('configured', 'all'):
            raise ServiceError(422, 'invalid_calibration_request', 'Choose scope configured or all.')
        selected = body.get('endpoint_ids', list(self.endpoints))
        models = body.get('models')
        if (not isinstance(selected, list) or not selected or len(selected) > 32
                or any(not isinstance(key, str) or key not in self.endpoints for key in selected)
                or len(set(selected)) != len(selected)):
            raise ServiceError(422, 'invalid_calibration_request', 'Select configured endpoint IDs.')
        if models is not None and (not isinstance(models, list) or not models or len(models) > 256
                or any(not isinstance(name, str) or not name or len(name) > 256 for name in models)
                or len({name.casefold() for name in models}) != len(models)):
            raise ServiceError(422, 'invalid_calibration_request', 'Provide a bounded unique model-name list.')
        configured = unique([model.name for group in ('smart', 'small') for model in self.settings.chat_models(group)]
                            + [model.name for model in self.settings.speech_models])
        speech_names = {speech_model_key(model.name) for model in self.settings.speech_models}
        if models is not None:
            keys = [speech_model_key(name) if speech_model_key(name) in speech_names else name.casefold() for name in models]
            if len(set(keys)) != len(keys):
                raise ServiceError(422, 'invalid_calibration_request', 'Select each speech model only once, including :latest aliases.')
        if scope == 'configured':
            if models is not None and any(name.casefold() not in {value.casefold() for value in configured}
                    and speech_model_key(name) not in speech_names for name in models):
                raise ServiceError(422, 'invalid_calibration_request', 'Configured scope accepts smart/small/speech models only.')
            models = configured if models is None else models
            if not models:
                raise ServiceError(422, 'no_configured_models', 'Configure smart/small/speech model lists or select scope all.')
        self.results.clear()
        self.run = {'id': 'cal-' + str(uuid4()), 'state': 'running', 'phase': 'discovery',
                    'scope': scope, 'started_at': now(), 'finished_at': None,
                    'total_models': 0, 'features_completed': 0, 'completed_models': 0,
                    'current': None, 'error_code': None, 'discovery': {}}
        self.task = asyncio.create_task(self._run(selected, models), name=self.run['id'])
        return self.snapshot()

    async def cancel(self):
        stopped = self.task is not None and not self.task.done()
        if stopped:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            # Cancellation before the task's first instruction cannot run its finally block.
            if self.run['state'] == 'running':
                self._finish('cancelled')
        return stopped

    async def close(self):
        await self.cancel()

    def _finish(self, state, error=None):
        self.run.update(state=state, phase=state, finished_at=now(), current=None, error_code=error)
        for records in self.results.values():
            for record in records.values():
                if record['state'] in ('pending', 'running'):
                    record.update(state=state, finished_at=now())
                for check in record['checks'].values():
                    if check['state'] in ('pending', 'running'):
                        check.update(state='cancelled' if state == 'cancelled' else 'skipped')

    def _record(self, key, model, listed, requested):
        return {'endpoint_id': key, 'model': model, 'requested_model': requested,
                'groups': [group for group in ('smart', 'small') if requested.casefold() in
                           {value.name.casefold() for value in self.settings.chat_models(group)}],
                'listed': listed, 'available': None, 'task': 'chat',
                'state': 'pending', 'capabilities': dict.fromkeys(('forms', 'vision', 'thinking')),
                'checks': {step: {'state': 'pending', 'passed': None, 'error_code': None,
                                  'http_status': None, 'duration_seconds': None} for step in STEPS},
                'metrics': None, 'started_at': None, 'finished_at': None}

    def _speech_record(self, record, configured=None, description=None):
        record.update(task='speech', groups=['speech'], voice=configured.voice if configured else 'default',
                      speech=description, capabilities=dict.fromkeys(('forms', 'vision', 'thinking', 'speech')))
        record['checks'] = {step: record['checks']['availability'].copy() for step in ('availability', 'speech', 'speed')}

    async def _speech_availability(self, endpoint, record):
        description = record['speech'] or await self.speech.describe(endpoint, record['model'])
        record['speech'] = description
        return any(voice_key(voice) == voice_key(record['voice']) for voice in description['voices'])

    async def _wave(self, endpoint, record, prompt):
        started, first, data = self.clock(), None, bytearray()
        async with self.chat.client.stream('POST', endpoint.url + '/audio/speech',
                json={'model': record['model'], 'voice': record['voice'], 'input': prompt,
                      'response_format': 'wav', 'stream_format': 'audio'},
                headers={**outbound_headers(), **endpoint.authorization_headers(), 'Accept': 'audio/wav'},
                timeout=self.settings.calibration_timeout_seconds) as response:
            if not response.is_success:
                raise ProbeFailure(f'upstream_http_{response.status_code}', response.status_code)
            if response.headers.get('content-type', '').split(';', 1)[0].strip().lower() not in WAV_TYPES:
                raise ProbeFailure('invalid_audio_type')
            info = None
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_PROBE_BYTES:
                    raise ProbeFailure('response_too_large')
                info = info or wav_header(data)
                if first is None and info is not None and len(data) >= info['offset'] + info['bytes_per_frame']:
                    first = self.clock()
        duration = max(0, self.clock() - started)
        if info is None or first is None:
            raise ProbeFailure('invalid_audio')
        audio = wav_duration(info, len(data))
        return {'ttfa_seconds': max(0, first - started), 'duration_seconds': duration,
                'audio_duration_seconds': audio, 'real_time_factor': duration / audio,
                'audio_bytes': len(data), 'sample_rate': info['sample_rate'], 'channels': info['channels'],
                'timing_source': 'client_monotonic_audio_stream'}

    async def _speech_feature(self, endpoint, record):
        await self._wave(endpoint, record, 'Hello. Guten Tag.')
        return True

    async def _speech_speed(self, endpoint, record):
        record['metrics'] = await self._wave(endpoint, record,
            'The speech test measures how quickly audio starts and how long generation takes. '
            'Dieser Sprachtest misst die Zeit bis zum ersten Audio und die Erzeugungsdauer.')
        return True

    async def _run(self, selected, requested):
        pairs = []
        try:
            for key in selected:
                endpoint = self.endpoints[key]
                error = None
                try:
                    names = await self.chat.discover_models(endpoint)
                except ServiceError as exc:
                    names, error = [], exc.code
                names = unique(names)
                advertised = {name.casefold(): name for name in names}
                targets = names if requested is None else requested
                if len(pairs) + len(targets) > self.settings.calibration_max_pairs:
                    raise ProbeFailure('model_limit_exceeded')
                self.run['discovery'][key] = {'state': 'error' if error else 'completed',
                                              'model_count': len(names), 'error_code': error}
                self.results[key] = {}
                for target in targets:
                    speech = next((value for value in self.settings.speech_models
                                   if speech_model_key(value.name) == speech_model_key(target)), None)
                    model = advertised.get(target.casefold(), target)
                    if speech is not None:
                        model = next((name for name in names if speech_model_key(name) == speech_model_key(target)), model)
                    record = self._record(key, model, None if error else model.casefold() in advertised, target)
                    if speech is not None:
                        self._speech_record(record, speech)
                    elif record['listed'] and not any(target.casefold() == value.name.casefold()
                            for group in ('smart', 'small') for value in self.settings.chat_models(group)):
                        try:
                            description = await self.speech.describe(endpoint, model)
                        except ServiceError:
                            pass  # Non-Ollama servers and ordinary chat models retain their existing chat probes.
                        else:
                            self._speech_record(record, description=description)
                    self.results[key][model] = record
                    pairs.append((endpoint, record))
                    if error or not record['listed']:
                        record.update(available=False, state='unavailable', finished_at=now())
                        record['checks']['availability'].update(state='error' if error else 'not_listed',
                            passed=False, error_code=error or 'model_not_listed')
                        for step, check in record['checks'].items():
                            if step != 'availability':
                                check['state'] = 'skipped'
                self.run['total_models'] = len(pairs)
            self.run['total_models'] = len(pairs)
            self.run['phase'] = 'features'
            for endpoint, record in pairs:
                if record['state'] == 'pending':
                    record.update(state='running', started_at=now())
                    is_speech = record['task'] == 'speech'
                    record['available'] = await self._check(endpoint, record, 'availability',
                        self._speech_availability if is_speech else self._availability)
                    if record['available'] is True:
                        probes = (('speech', self._speech_feature),) if is_speech else (
                            ('forms', self._forms), ('vision', self._vision), ('thinking', self._thinking))
                        for step, probe in probes:
                            record['capabilities'][step] = await self._check(endpoint, record, step, probe)
                        if is_speech and record['capabilities']['speech'] is not True:
                            record.update(available=record['capabilities']['speech'], state='unavailable')
                            record['checks']['speed']['state'] = 'skipped'
                    else:
                        record['state'] = 'unavailable'
                        for step, check in record['checks'].items():
                            if step != 'availability':
                                check['state'] = 'skipped'
                self.run['features_completed'] += 1
            # All feature probes finish before any performance sample starts.
            self.run['phase'] = 'speed'
            for endpoint, record in pairs:
                if record['available'] is True:
                    await self._check(endpoint, record, 'speed',
                                      self._speech_speed if record['task'] == 'speech' else self._speed)
                    record['state'] = 'completed'
                record['finished_at'] = now()
                self.run['completed_models'] += 1
            self._finish('completed')
        except asyncio.CancelledError:
            self._finish('cancelled')
            raise
        except ProbeFailure as exc:
            self._finish('failed', exc.code)
        except Exception:
            self._finish('failed', 'internal_error')

    async def _check(self, endpoint, record, step, probe):
        check = record['checks'][step]
        check['state'] = 'running'
        self.run['current'] = {'endpoint_id': record['endpoint_id'], 'model': record['model'], 'step': step}
        started = self.clock()
        connection = self.chat.routing.reserve(endpoint, record['model'])
        try:
            result = await asyncio.wait_for(probe(endpoint, record), self.settings.calibration_timeout_seconds)
            check.update(state='passed' if result else 'not_demonstrated', passed=result)
            return result
        except (httpx.TimeoutException, asyncio.TimeoutError):
            check.update(state='error', error_code='timeout')
        except httpx.HTTPError:
            check.update(state='error', error_code='connection_failed')
        except ProbeFailure as exc:
            rejected = step in ('forms', 'vision', 'thinking', 'speech') and exc.http_status in {400, 404, 415, 422}
            check.update(state='rejected' if rejected else 'error', passed=False if rejected else None,
                         error_code=exc.code, http_status=exc.http_status)
            return False if rejected else None
        except ServiceError as exc:
            check.update(state='error', error_code=exc.code)
        except (ValueError, RecursionError):
            check.update(state='error', error_code='invalid_response')
        finally:
            connection.release()
            check['duration_seconds'] = max(0, self.clock() - started)
        return None

    def _payload(self, record, prompt, limit=128, **options):
        return {'model': record['model'], 'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': limit, 'stream': False, **options}

    async def _json(self, endpoint, payload):
        async with self.chat.client.stream('POST', endpoint.url + '/chat/completions', json=payload,
                headers={**outbound_headers(), **endpoint.authorization_headers()},
                timeout=self.settings.calibration_timeout_seconds) as response:
            if not response.is_success:
                raise ProbeFailure(f'upstream_http_{response.status_code}', response.status_code)
            result = json.loads(await bounded_body(response, MAX_PROBE_BYTES))
            if not isinstance(result, dict) or 'error' in result:
                raise ProbeFailure('invalid_response')
            return result

    async def _availability(self, endpoint, record):
        result = await self._json(endpoint, self._payload(record, 'Reply with exactly OK.', 64))
        reply = message(result)
        return bool(text(reply).strip() or reasoning(reply))

    async def _forms(self, endpoint, record):
        schema = {'type': 'object', 'properties': {'mood': {'type': 'string',
                  'enum': ['angry', 'happy', 'surprised']}}, 'required': ['mood'], 'additionalProperties': False}
        for prompt, expected in FORM_CASES:
            payload = self._payload(record, prompt, response_format={'type': 'json_schema', 'json_schema': {
                'name': 'calibration_mood', 'strict': True, 'schema': schema}})
            payload['messages'].insert(0, {'role': 'system', 'content': 'Classify the mood. Return only the requested JSON object.'})
            reply = message(await self._json(endpoint, payload))
            parsed = reply.get('parsed', reply.get('content'))
            if not isinstance(parsed, dict):
                try:
                    parsed = json.loads(text(reply))
                except ValueError:
                    return False
            if parsed != {'mood': expected}:
                return False
        return True

    async def _vision(self, endpoint, record):
        encoded = base64.b64encode(VISION_IMAGE.read_bytes()).decode('ascii')
        payload = self._payload(record, [
            {'type': 'text', 'text': 'What number is shown in this image? Reply with the number.'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + encoded}},
        ])
        reply = message(await self._json(endpoint, payload))
        return re.search(r'(?<!\d)42(?!\d)', text(reply)) is not None

    async def _thinking(self, endpoint, record):
        result = await self._json(endpoint, self._payload(record,
            'Think step by step and answer: what is 17 plus 25?', 512, reasoning_effort='low'))
        return reasoning(message(result)) or any(reasoning(choice) for choice in result['choices'] if isinstance(choice, dict))

    async def _speed(self, endpoint, record):
        started = self.clock()
        first = last = None
        first_kind = None
        emitted_reasoning = False
        tokens = reasoning_tokens = None
        finished = False
        decoder = SSEDecoder(max_frame_bytes=64 * 1024)
        payload = self._payload(record, 'Write the numbers from 1 to 200 separated by spaces. Do not add explanations.',
                                256, stream=True, stream_options={'include_usage': True})

        def event(raw):
            nonlocal first, last, first_kind, tokens, reasoning_tokens, finished, emitted_reasoning
            if raw.strip() == '[DONE]':
                return True
            value = json.loads(raw)
            if not isinstance(value, dict) or 'error' in value:
                raise ProbeFailure('invalid_stream')
            usage = value.get('usage')
            if isinstance(usage, dict):
                count = usage.get('completion_tokens')
                if type(count) is int and count > 0:
                    tokens = count
                details = usage.get('completion_tokens_details')
                count = details.get('reasoning_tokens') if isinstance(details, dict) else None
                if type(count) is int and count >= 0:
                    reasoning_tokens = count
            choices = value.get('choices', [])
            if not isinstance(choices, list) or len(choices) > 1:
                raise ProbeFailure('invalid_stream')
            for choice in choices:
                if not isinstance(choice, dict) or not isinstance(choice.get('delta', {}), dict):
                    raise ProbeFailure('invalid_stream')
                delta = choice.get('delta', {})
                has_reasoning = reasoning(delta)
                content = text(delta)
                if has_reasoning or content:
                    measured = self.clock()
                    if first is None:
                        first, first_kind = measured, 'reasoning' if has_reasoning else 'content'
                    last = measured
                    emitted_reasoning = emitted_reasoning or has_reasoning
                finished = finished or choice.get('finish_reason') in {'stop', 'length'}
            return False

        async with self.chat.client.stream('POST', endpoint.url + '/chat/completions', json=payload,
                headers={**outbound_headers(), **endpoint.authorization_headers(), 'Accept': 'text/event-stream'},
                timeout=self.settings.calibration_timeout_seconds) as response:
            if not response.is_success:
                raise ProbeFailure(f'upstream_http_{response.status_code}', response.status_code)
            if response.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'text/event-stream':
                raise ProbeFailure('invalid_stream_type')
            size, done = 0, False
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_PROBE_BYTES:
                    raise ProbeFailure('response_too_large')
                for raw in decoder.feed(chunk):
                    done = event(raw)
                    if done:
                        break
                if done:
                    break
            if not done:
                for raw in decoder.feed(b'', final=True):
                    if event(raw):
                        break
        duration = max(0, self.clock() - started)
        if not finished or first is None:
            raise ProbeFailure('incomplete_stream')
        generation = max(0, last - first)
        hidden_reasoning = reasoning_tokens is not None and reasoning_tokens > 0 and not emitted_reasoning
        record['metrics'] = {
            'ttft_seconds': max(0, first - started), 'first_token_kind': first_kind,
            'duration_seconds': duration, 'completion_tokens': tokens, 'reasoning_tokens': reasoning_tokens,
            'tokens_per_second': tokens / duration if tokens is not None and duration > 0 else None,
            'generation_tokens_per_second': (tokens - 1) / generation
                if tokens is not None and tokens > 1 and generation > 0 and not hidden_reasoning else None,
            'generation_seconds': generation,
            'token_count_source': 'upstream_usage' if tokens is not None else 'unavailable',
            'timing_source': 'client_monotonic_stream',
        }
        return True
