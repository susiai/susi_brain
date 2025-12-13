"""HTTP controls and dictionary schemas for endpoint calibration clients."""

from fastapi import Body, Request
from fastapi.responses import JSONResponse

from susi_brain.shared.http.responses import problem_responses

PREFIX = '/api/v1/compute/calibration'
NULL_BOOL = {'type': ['boolean', 'null']}
NULL_NUMBER = {'type': ['number', 'null'], 'minimum': 0}
CHECK = {'type': 'object', 'required': ['state', 'passed', 'error_code', 'http_status', 'duration_seconds'],
         'properties': {
             'state': {'type': 'string', 'enum': ['pending', 'running', 'passed', 'not_demonstrated',
                        'rejected', 'error', 'skipped', 'cancelled', 'not_listed']},
             'passed': NULL_BOOL, 'error_code': {'type': ['string', 'null']},
             'http_status': {'type': ['integer', 'null']}, 'duration_seconds': NULL_NUMBER,
         }}
METRICS = {'type': ['object', 'null'], 'properties': {
    'ttft_seconds': NULL_NUMBER, 'duration_seconds': NULL_NUMBER, 'generation_seconds': NULL_NUMBER,
    'completion_tokens': {'type': ['integer', 'null'], 'minimum': 1},
    'reasoning_tokens': {'type': ['integer', 'null'], 'minimum': 0},
    'tokens_per_second': NULL_NUMBER, 'generation_tokens_per_second': NULL_NUMBER,
    'first_token_kind': {'type': 'string', 'enum': ['content', 'reasoning']},
    'token_count_source': {'type': 'string', 'enum': ['upstream_usage', 'unavailable']},
    'timing_source': {'type': 'string', 'enum': ['client_monotonic_stream', 'client_monotonic_audio_stream']},
    'ttfa_seconds': NULL_NUMBER, 'audio_duration_seconds': NULL_NUMBER, 'real_time_factor': NULL_NUMBER,
    'audio_bytes': {'type': 'integer', 'minimum': 1}, 'sample_rate': {'type': 'integer', 'const': 24000},
    'channels': {'type': 'integer', 'const': 1},
}}
SPEECH = {'type': ['object', 'null'], 'properties': {
    'voices': {'type': 'array', 'items': {'type': 'string'}},
    'formats': {'type': 'array', 'items': {'type': 'string', 'const': 'wav'}},
    'sample_rate': {'type': 'integer', 'const': 24000}, 'channels': {'type': 'integer', 'const': 1},
    'sample_width': {'type': 'integer', 'const': 2},
}}
MODEL = {'type': 'object', 'required': ['endpoint_id', 'model', 'requested_model', 'task', 'groups', 'listed',
         'available', 'state', 'capabilities', 'checks', 'metrics', 'started_at', 'finished_at'], 'properties': {
    'endpoint_id': {'type': 'string'}, 'model': {'type': 'string'}, 'requested_model': {'type': 'string'},
    'groups': {'type': 'array', 'items': {'type': 'string', 'enum': ['smart', 'small', 'speech']}},
    'task': {'type': 'string', 'enum': ['chat', 'speech']},
    'voice': {'type': 'string'}, 'speech': SPEECH,
    'listed': NULL_BOOL, 'available': NULL_BOOL,
    'state': {'type': 'string', 'enum': ['pending', 'running', 'unavailable', 'completed', 'failed', 'cancelled']},
    'capabilities': {'type': 'object', 'required': ['forms', 'vision', 'thinking'],
                     'properties': {name: NULL_BOOL for name in ('forms', 'vision', 'thinking', 'speech')}},
    'checks': {'type': 'object', 'required': ['availability', 'speed'],
               'properties': {name: CHECK for name in ('availability', 'forms', 'vision', 'thinking', 'speech', 'speed')}},
    'metrics': METRICS, 'started_at': {'type': ['string', 'null']}, 'finished_at': {'type': ['string', 'null']},
}}
SNAPSHOT = {'type': 'object', 'required': ['probe_version', 'storage', 'default_scope', 'model_groups', 'speech_models', 'endpoints', 'run', 'results'],
            'properties': {
    'probe_version': {'type': 'string'}, 'storage': {'type': 'string'},
    'default_scope': {'type': 'string', 'const': 'configured'},
    'speech_models': {'type': 'array', 'items': {'type': 'object', 'required': ['name', 'voice'],
        'properties': {'name': {'type': 'string'}, 'voice': {'type': 'string', 'default': 'default'}}}},
    'model_groups': {'type': 'object', 'required': ['smart', 'small'], 'properties': {
        group: {'type': 'array', 'items': {'type': 'object', 'required': ['name', 'ranking'], 'properties': {
            'name': {'type': 'string'}, 'ranking': {'type': 'number', 'default': 0}}}} for group in ('smart', 'small')}},
    'endpoints': {'type': 'array', 'items': {'type': 'object', 'required': ['id', 'url', 'key_configured'],
        'properties': {'id': {'type': 'string'}, 'url': {'type': 'string'}, 'key_configured': {'type': 'boolean'}}}},
    'run': {'type': ['object', 'null'], 'properties': {
        'id': {'type': 'string'}, 'state': {'type': 'string', 'enum': ['running', 'completed', 'failed', 'cancelled']},
        'phase': {'type': 'string', 'enum': ['discovery', 'features', 'speed', 'completed', 'failed', 'cancelled']},
        'scope': {'type': 'string', 'enum': ['configured', 'all']},
        'started_at': {'type': 'string'}, 'finished_at': {'type': ['string', 'null']},
        'total_models': {'type': 'integer', 'minimum': 0}, 'features_completed': {'type': 'integer', 'minimum': 0},
        'completed_models': {'type': 'integer', 'minimum': 0},
        'current': {'type': ['object', 'null'], 'properties': {
            'endpoint_id': {'type': 'string'}, 'model': {'type': 'string'}, 'step': {'type': 'string'}}},
        'error_code': {'type': ['string', 'null']}, 'discovery': {'type': 'object', 'additionalProperties': {
            'type': 'object', 'properties': {'state': {'type': 'string'}, 'model_count': {'type': 'integer'},
                                            'error_code': {'type': ['string', 'null']}}}},
    }},
    'results': {'type': 'object', 'description': 'Endpoint ID -> concrete model name -> latest probe record.',
                'additionalProperties': {'type': 'object', 'additionalProperties': MODEL}},
}}
RESULT = {200: {'description': 'Process-local calibration snapshot', 'content': {'application/json': {'schema': SNAPSHOT}}}}


def install_calibration_routes(app, calibration):
    @app.get(PREFIX, operation_id='compute__calibration_results', responses=RESULT)
    async def results():
        """Read progress and endpoint/model capability/latency snapshots without triggering work. model_groups exposes smart/small names and rankings; speech_models lists explicitly configured speech names/default voices even before calibration. Each record has task chat or speech. Speech records expose Ollama voices and the proxy's WAV format/sample rate in speech, the measured voice, capability speech, and availability/speech/speed checks; chat-only capabilities remain null. These audio probes validate nonempty aligned 24 kHz mono 16-bit PCM WAV, not pronunciation, intelligibility or language quality. Speech metrics use ttfa_seconds from before the POST until the first complete PCM frame (WAV header alone does not count), audio_duration_seconds from actual PCM length, and real_time_factor=full request duration/audio duration; lower is faster. Timing includes transport/server load and only applies to the measured voice. Results are process-local, replaced by each run and cleared on restart. Credentials, prompts and generated audio/text are omitted. Null capabilities mean untested, skipped or inconclusive; false means a fixed probe did not demonstrate support. Chat forms validates three mood classifications; vision reads 42; thinking detects exposed reasoning. Chat TTFT starts before the speed POST and ends at the first nonempty content/reasoning delta. tokens_per_second=reported completion_tokens/full stream duration including TTFT. generation_tokens_per_second=(completion_tokens-1)/(last-output-time-first-output-time), null for unknown counts, one output event or hidden reasoning. Transport chunks are never token counts. run completed means probes finished; inspect individual checks for failures."""
        return JSONResponse(calibration.snapshot(), headers={'Cache-Control': 'no-store'})

    @app.post(PREFIX, status_code=202, operation_id='compute__start_calibration',
              responses={202: {'description': 'Calibration accepted; poll Location', 'content': {'application/json': {'schema': SNAPSHOT}}},
                         **problem_responses(409, 422, 503)},
              openapi_extra={'requestBody': {'required': False, 'content': {'application/json': {'schema': {
                  'type': 'object', 'additionalProperties': False, 'properties': {
                      'scope': {'type': 'string', 'enum': ['configured', 'all'], 'default': 'configured'},
                      'endpoint_ids': {'type': 'array', 'minItems': 1, 'maxItems': 32, 'uniqueItems': True,
                                       'items': {'type': 'string'}},
                      'models': {'type': 'array', 'minItems': 1, 'maxItems': 256, 'uniqueItems': True,
                                 'items': {'type': 'string', 'minLength': 1, 'maxLength': 256}},
                  }}, 'example': {'scope': 'configured'}}}}})
    async def start(request: Request, body: dict | None = Body(default=None)):
        """Explicitly start one background calibration. Default scope is the union of configured smart/small/speech models on every selected endpoint; all probes advertised models instead. Speech configuration recognizes Ollama's omitted :latest tag. In all scope, /api/show detects additional speech models; non-speech models and servers without Ollama metadata retain chat probes. Optional concrete model and configured endpoint-ID filters bound the run. Configured filters must name smart/small/speech entries. Speech availability checks metadata/voice, then one fixed short WAV synthesis demonstrates speech; only a successful functional audio probe permits the separate longer speed sample. Chat availability/forms/vision/thinking probes retain their existing behavior. ALL model function tests finish before ANY speed sample. Chat speed uses 256 output tokens; audio probes use at most 1 MiB and a 60-second default per-check deadline, including metadata. Each check shares the same connection counters as Chat/TTS and releases on cancellation/failure; inventories and ordinary metadata discovery do not reserve. No arbitrary URL/prompt, automatic startup run, retries, model download or persistence. Starting replaces previous results. Authenticated HTTP pool, TLS and bounded discovery are shared with chat. calibration_max_pairs defaults to 1024; excess discovery fails before inference. Calls can incur configured provider charges. Concurrent run 409; inactive transport 503; invalid filters/empty selection 422. 202 is admission only; poll checks for results."""
        snapshot = calibration.start(body)
        return JSONResponse(snapshot, status_code=202, headers={
            'Location': request.scope.get('root_path', '') + PREFIX, 'Cache-Control': 'no-store'})

    @app.delete(PREFIX, operation_id='compute__cancel_calibration', responses=RESULT)
    async def cancel():
        """Cancel and drain an active calibration, close its upstream response and retain partial results marked cancelled. Idempotent when idle; GET/polling remains available. Shutdown performs the same cleanup before closing the shared chat client. This controls the local calibration only; it cannot attest remote provider cancellation or billing."""
        await calibration.cancel()
        return JSONResponse(calibration.snapshot(), headers={'Cache-Control': 'no-store'})
