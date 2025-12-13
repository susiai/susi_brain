"""The documented OpenAI-compatible WAV subset for Ollama speech synthesis."""

from fastapi.responses import Response


def install_speech_routes(app, speech):
    error = {'type': 'object', 'properties': {'error': {'type': 'object', 'properties': {
        'message': {'type': 'string'}, 'type': {'type': 'string'},
        'param': {'type': ['string', 'null']}, 'code': {'type': 'string'},
    }}}}
    app.add_api_route('/v1/audio/speech', speech.post, methods=['POST'], response_class=Response,
        operation_id='compute__speech', tags=['Services'], summary='Generate speech through an Ollama endpoint',
        description=(
            'Generate streamed 24 kHz mono 16-bit PCM WAV through a configured Ollama server. '
            'model is a concrete name from compute.speech, with optional :latest; smart/small are chat-only. '
            'input is required, nonblank and at most 4096 Unicode characters. voice defaults to the model configuration '
            '(default if omitted there). Voice matching ignores case and accents, as in Ollama. '
            'Only response_format=wav (default), speed=1 and stream_format=audio (default) are supported. '
            'instructions, SSE, other formats and unknown fields return 400; no transcoding is performed. '
            'Every valid request reads bounded /models inventories and /api/show metadata to find an advertised '
            'speech-capable model and voice. /api/show replaces the trailing /v1 in the configured base with /api/show, '
            'preserving a deployment prefix; TTS bases must end in /v1. Discovery defaults to 3 seconds and 1 MiB per response. '
            'Choose the endpoint with the fewest shared Chat/TTS/calibration reservations, then lowest calibrated real_time_factor '
            'and ttfa_seconds for the exact model/voice; unknown metrics sort last and final ties use endpoint order. '
            'Selection/reservation is atomic in this process; no hard single-model lock, waiting queue or cross-worker accounting. '
            'Configured endpoint Bearer credentials and verified TLS are shared with chat; caller keys/cookies, redirects '
            'and upstream cookies are not forwarded. No download, automatic replay or fallback to a different model/voice. '
            'WAV headers and first PCM frames are validated before success headers; audio streams without whole-response buffering. '
            'Ollama may use 0xffffffff WAV size placeholders. Headers and PCM are forwarded unchanged; clients must accept '
            'unknown-length streamed WAV. The response limit is 32 MiB; speech_timeout_seconds defaults to 120 for '
            'upstream operations/idle reads and waiting for first audio, not a total generation deadline. '
            'Failures after headers terminate the audio stream, never append JSON or change its status; monitoring marks them failed. '
            'All paths release the reservation after closing the upstream response. '
            'X-Compute-Target is ASCII JSON with endpoint_id, concrete model, voice and expected {ttfa_seconds, real_time_factor}; '
            'metrics are copied at reservation time and null before a matching successful calibration. The header is exposed through CORS. '
            'Upstream error status/body/Content-Type/Retry-After are preserved within 1 MiB; redirect and transport failures '
            'produce local JSON errors. Request logging stores only target/timing metadata, never input text or audio. '
            'This is a WAV-only OpenAI-compatible subset; unlike OpenAI the default output format here is WAV. '
            'Ollama speech currently requires Apple Silicon; the Compute proxy itself can run on Linux/Docker.'),
        openapi_extra={'requestBody': {'required': True, 'content': {'application/json': {
            'schema': {'type': 'object', 'additionalProperties': False, 'required': ['model', 'input'], 'properties': {
                'model': {'type': 'string', 'minLength': 1, 'maxLength': 256},
                'input': {'type': 'string', 'minLength': 1, 'maxLength': 4096},
                'voice': {'type': 'string', 'minLength': 1, 'maxLength': 256,
                          'description': 'Configured default voice when omitted.'},
                'response_format': {'type': 'string', 'const': 'wav', 'default': 'wav'},
                'speed': {'type': 'number', 'const': 1, 'default': 1},
                'stream_format': {'type': 'string', 'const': 'audio', 'default': 'audio'},
            }}, 'example': {'model': 'phonon', 'input': 'Hello from Compute.', 'voice': 'default', 'response_format': 'wav'},
        }}}},
        responses={
            200: {'description': 'Streamed PCM WAV, including unknown-size Ollama headers',
                  'headers': {'X-Compute-Target': {'schema': {'type': 'string'},
                      'description': 'ASCII JSON {endpoint_id, model, voice, expected: {ttfa_seconds, real_time_factor}}. Missing metrics are null.'}},
                  'content': {'audio/wav': {'schema': {'type': 'string', 'format': 'binary'}}}},
            **{status: {'description': description, 'content': {'application/json': {'schema': error}},
                        **({'headers': {'Retry-After': {'schema': {'type': 'string'},
                              'description': '1 for local unavailability; otherwise the upstream value.'}}}
                           if status in (429, 503) else {})}
               for status, description in {
                   400: 'Invalid model/input/voice or unsupported speech options; upstream rejection may also return 400.',
                   429: 'Upstream rate limit; Retry-After is preserved.',
                   502: 'Upstream redirect, connection failure, oversized error or invalid WAV before response headers.',
                   503: 'No compatible configured Ollama model/voice or inactive transport; Retry-After: 1.',
                   504: 'Upstream operation or first-audio timeout before response headers.',
               }.items()},
        })
