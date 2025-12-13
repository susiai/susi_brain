"""Register computational services as FastAPI endpoints."""

from fastapi.responses import JSONResponse

PREFIX = "/api/v1/compute"
OPENAI_PREFIX = "/v1"


def install_service_routes(app, chat, speech):
    """Register Compute routes and the standard OpenAI chat/audio paths."""
    from .audio_service import VoiceTranscription
    from .share_service import FileResource, SearchResource
    from .speech_api import install_speech_routes

    install_speech_routes(app, speech)

    chat_handler = chat.post
    routing = chat.routing
    audio_handler = VoiceTranscription().post
    search_handler = SearchResource().post
    files = FileResource()

    @app.get(PREFIX + "/chat/connections", operation_id="compute__chat_connections", tags=["Services"],
             responses={200: {"description": "Current process-local proxy reservations", "content": {"application/json": {
                 "schema": {"type": "object", "required": ["storage", "active_connections", "endpoints"], "properties": {
                     "storage": {"type": "string", "const": "process-local"},
                     "active_connections": {"type": "integer", "minimum": 0},
                     "endpoints": {"type": "array", "items": {"type": "object",
                         "required": ["id", "url", "active_connections", "models"], "properties": {
                             "id": {"type": "string"}, "url": {"type": "string"},
                             "active_connections": {"type": "integer", "minimum": 0},
                             "models": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 1}},
                         }}},
                 }}}}}})
    async def connections():
        """Read shared Chat/TTS reservations per endpoint and concrete model without upstream calls. Counts include connecting/waiting requests, JSON responses, SSE/WAV upstream lifetimes and calibration checks. Chat releases on [DONE] without waiting for upstream EOF or client draining; otherwise cleanup releases on completion, error, timeout, cancellation or disconnect. WAV releases after streaming cleanup. Discovery, idle pooled sockets and ordinary metadata reads are excluded. Selection/reservation is atomic after discovery. Calibration checks reserve for the whole check, including sequential forms calls or availability metadata. Chat and speech selection share endpoint load. Empty endpoints have zero and an empty models dictionary. Counters are process-local, start at zero and are not shared between workers. IDs match calibration endpoint IDs; no credentials are exposed. The historical chat/connections path also covers TTS."""
        return JSONResponse(routing.snapshot(), headers={"Cache-Control": "no-store"})

    def register(path, handler, method, name, description, *, body=None, responses=None, optional_auth=False):
        operation = {}
        if optional_auth:
            operation["security"] = [{}]
        if body:
            operation["requestBody"] = body
        app.add_api_route(
            path, handler, methods=[method],
            operation_id=name, summary=description.split(". ")[0], description=description,
            tags=["Services"], openapi_extra=operation,
            responses={200: {"description": "Service response", "content": {"application/json": {"schema": {}}}},
                       400: {"description": "Invalid input", "content": {"application/json": {"schema": {}}}},
                       403: {"description": "The server boundary rejects traversal and symlinks", "content": {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}},
                       404: {"description": "Resource not found", "content": {"application/json": {"schema": {}}}},
                       500: {"description": "Service failure",
                             "content": {"application/json": {"schema": {"type": "object"}},
                                         "application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}},
                       **(responses or {})},
        )

    def json_body(properties, *, required=(), example=None):
        schema = {"type": "object", "properties": properties}
        if required:
            schema["required"] = list(required)
        content = {"schema": schema}
        if example is not None:
            content["example"] = example
        return {"required": False, "content": {"application/json": content}}

    chat = json_body({
        "messages": {"type": "array", "items": {"type": "object"}},
        "model": {"type": "string", "enum": ["smart", "small"]},
        "temperature": {"type": "number"},
        "max_tokens": {"type": "integer"},
        "stream": {"type": "boolean", "default": False},
        "stream_options": {"type": "object", "properties": {
            "include_usage": {"type": "boolean", "description": "Request final upstream token usage; provider support is required."},
        }},
        "reasoning_effort": {"type": "string", "description": "Passed through. The local chat uses medium for Thinking on and none for off; supported values depend on the provider/model."},
        "reasoning": {"type": "object", "description": "Passed-through provider reasoning options. The local chat sets enabled to the Thinking toggle.",
                      "properties": {"enabled": {"type": "boolean"}}},
        "chat_template_kwargs": {"type": "object", "description": "Passed-through local-server template options. The local chat sets enable_thinking to the Thinking toggle.",
                                 "properties": {"enable_thinking": {"type": "boolean"}}},
    }, required=("messages", "model"),
                     example={"model": "small", "messages": [{"role": "user", "content": "Hello"}]})
    proxy_error = {"type": "object", "required": ["error"], "properties": {"error": {
        "type": "object", "required": ["message", "type", "param", "code"], "properties": {
            "message": {"type": "string"}, "type": {"type": "string"},
            "param": {"type": ["string", "null"]}, "code": {"type": "string"},
        },
    }}}
    register(OPENAI_PREFIX + "/chat/completions", chat_handler, "POST", "compute__chat_0",
             "Proxy a chat completion using model smart or small. Both model and nonempty messages are required. "
             "Queries each configured API base URL plus /models concurrently for every valid request. "
             "Legacy host:port targets use http://host:port/v1; URL objects preserve HTTP(S) and API prefixes such as "
             "https://openrouter.ai/api/v1. Each object may set api_key or api_key_env for its own Bearer credential, "
             "used for both discovery and chat. A missing/empty referenced key is a startup configuration error. "
             "Reserves the matching endpoint with the fewest active requests, counting all models, Chat JSON/SSE, TTS WAV and calibration checks. "
             "On each endpoint, reuses an active model in the requested group when one is available; otherwise another model may be used. "
             "Among endpoints with equal load, smart selects the highest operator-configured numeric ranking, then the fastest model; "
             "small selects the fastest model regardless of ranking. Group configuration accepts model-name strings (ranking 0) "
             "or objects with name and finite numeric ranking (default 0). Speed uses calibration tokens_per_second descending, "
             "then ttft_seconds ascending; missing measurements sort last for each metric. Remaining ties use model-list then endpoint order. "
             "There is no waiting queue or hard single-model lock. Selection and reservation are atomic within this process; "
             "counters are not shared across workers. GET /api/v1/compute/chat/connections exposes live counts. "
             "Model discovery compares names case-insensitively and forwards the advertised spelling. Only model is rewritten; "
             "all other request JSON fields pass through with backend defaults. Streaming follows triton_llmlb: "
             "the first data line (when it contains a JSON object) and the first terminal completion object receive four top-level fields: "
             "modelname (the concrete selected model), compute_group (smart/small), compute_hostname (selected API base's hostname, "
             "without port/path) and request_bytes (original incoming UTF-8 HTTP body length, before model rewriting; no headers). "
             "These proxy-owned values override same-named upstream fields on those two objects; the standard model field is preserved. "
             "Byte lines are assembled across network chunks; only the first data payload and byte-prefiltered terminal candidates are parsed. "
             "A terminal candidate contains a string-valued finish_reason, then must have a non-null finish_reason in a choice. "
             "The first event may itself be terminal and is then annotated once. Annotation reserializes the JSON as UTF-8 data: ... with LF/LF framing. "
             "Middle lines, content/thinking/tool deltas, usage-only events, invalid/non-object JSON and ordinary SSE framing pass unchanged; "
             "multiline JSON data is not combined for annotation. Like the reference, an invalid first data line consumes the first-line attempt. "
             "No usage request, token counts, finish reason or missing completion is synthesized. Receiving [DONE], even without a final newline, "
             "closes upstream and releases its reservation immediately; already received trailing blank framing is preserved without waiting for EOF. "
             "HTTPX handles upstream content encoding; HTTP chunk boundaries can change. Streams disable caches/proxy buffering and request no-transform. "
             "Forwarded JSON and SSE responses add X-Compute-Target: an ASCII JSON object containing endpoint_id, "
             "the concrete model and expected {tokens_per_second, ttft_seconds}. These are a snapshot of the selected pair's "
             "calibration at reservation time; unavailable metrics are null. The header is exposed through CORS. "
             "Upstream status, Content-Type and Retry-After are preserved; non-streaming/error bodies remain unchanged. Only the selected endpoint's configured "
             "key is sent upstream; caller credentials/cookies and redirects are not forwarded. HTTPS certificates are verified. "
             "The server OpenAI key is used only by cloud audio, not this proxy. No model provisioning or automatic chat retries. "
             "Discovery defaults to a 3-second deadline per endpoint and a 1 MiB inventory limit; chat uses a 120-second "
             "upstream operation/idle timeout and an 8 MiB upstream body limit. The browser allows an extra 8 KiB for annotation/framing overhead. "
             "Failures after response headers terminate the stream "
             "and are recorded as failed; no proxy-generated error or [DONE] frame is appended. Upstream cleanup always releases "
             "the reservation. The Chat UI displays the stream's selected model, host, group and original request size; "
             "expected speed remains in X-Compute-Target and actual speed uses unchanged upstream usage. "
             "Frontend rendering and explicit calibration parse their own streams separately. "
             "Readiness does not imply an available model. Malformed JSON/body types use native 422 problems.",
             body=chat, responses={
                 200: {"description": "Upstream JSON or SSE with first/terminal routing metadata", "headers": {"X-Compute-Target": {
                     "schema": {"type": "string"},
                     "description": 'ASCII JSON, e.g. {"endpoint_id":"ep-0123456789abcdef","model":"fast-model","expected":{"tokens_per_second":42.5,"ttft_seconds":0.2}}. Exact selected-pair calibration copied before upstream I/O; missing metrics are null. Also present on other forwarded upstream statuses, absent on proxy-generated errors. Contains no credentials or endpoint URL.',
                 }}, "content": {
                     "application/json": {"schema": {}}, "text/event-stream": {"schema": {"type": "string"},
                         "example": 'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null}],"modelname":"fast-model","compute_group":"small","compute_hostname":"llm.example","request_bytes":123}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}],"modelname":"fast-model","compute_group":"small","compute_hostname":"llm.example","request_bytes":123}\n\ndata: [DONE]\n\n'},
                 }},
                 **{status: {"description": detail, "content": {"application/json": {"schema": proxy_error}},
                             **({"headers": {"Retry-After": {"schema": {"type": "string"},
                                 "description": "1 for proxy unavailability; otherwise the upstream value."}}}
                                if status == 503 else {})}
                    for status, detail in {
                        400: "Missing/invalid model alias, messages or stream flag.",
                        502: "Upstream connection, redirect, protocol or response-size failure.",
                        503: "No configured/available model in this group; Retry-After: 1.",
                        504: "Selected chat server timed out.",
                    }.items()},
                 429: {"description": "Upstream rate limit; body and Retry-After are preserved.",
                       "headers": {"Retry-After": {"schema": {"type": "string"}}},
                       "content": {"application/json": {"schema": {}}}},
                 "default": {"description": "Upstream error status/body, including rate limits; Retry-After is preserved.",
                             "content": {"application/json": {"schema": {}}}},
             })

    audio = {"required": False, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["file"], "properties": {
            "file": {"type": "string", "format": "binary", "description": "Audio file bytes. Local Whisper decodes FFmpeg-supported containers, including WAV, MP3, M4A, OGG, FLAC and browser WebM/MP4 recordings."},
            "model": {"type": "string", "default": "tiny",
                      "description": "tiny, base, small, medium, large, whisper-1, en-us or de; other names return an empty dictionary when a key is set."},
        },
    }}}}
    register(OPENAI_PREFIX + "/audio/transcriptions", audio_handler, "POST", "compute__transcription_0",
             "Transcribe an audio file. The Authorization header overrides the configured OpenAI key. "
             "With a key, whisper-1 forwards to OpenAI; other supported names select local Whisper or Vosk models. "
             "Without a key every requested model becomes tiny. Whisper loads on each request. "
             "Local Whisper decodes supported audio containers through FFmpeg and preserves the spoken language. "
             "The web speech pane submits files or completed microphone recordings with model=tiny; no live audio-stream protocol is provided. "
             "Vosk requires mono PCM16 WAV and an en-us model directory in the working directory. "
             "Missing files return an error dictionary with HTTP 200; unsupported model names return an empty dictionary. "
             "Vosk result processing can fail with HTTP 500.", body=audio, optional_auth=True)

    search = json_body({
        "file": {"type": "string"}, "query": {"type": "string"}, "max_rerank": {"type": "integer"},
    }, required=("file", "query"), example={"file": "documents.jsonl", "query": "hello", "max_rerank": 5})
    register(PREFIX + "/data/search", search_handler, "POST", "compute__search_0",
             "Search a JSONL file. Case-insensitive all-word matching on text_t; "
             "returns matching JSON objects in file order, limited by max_rerank when supplied.", body=search,
             responses={400: {"description": "Invalid JSON input"}, 404: {"description": "File not found"}})

    upload = {"required": False, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["file"], "properties": {"file": {"type": "string", "format": "binary"}},
    }}}}
    for method, description in (
        ("GET", "Read a file or list a directory. index.json returns directory metadata."),
        ("POST", "Upload a file. Requires the SUSI Bearer key. "
                 "Returns HTTP 200 with a JSON status field of 201."),
        ("DELETE", "Delete a file. Requires the SUSI Bearer key. "
                   "File deletion is currently unavailable and returns HTTP 500."),
    ):
        register(PREFIX + "/data/{req_path:path}", getattr(files, method.lower()), method,
                 f"compute__file_{method.lower()}_0", description,
                 body=upload if method == "POST" else None,
                 responses={**({200: {"description": "File bytes, HTML directory listing or JSON index",
                                      "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
                                                  "text/html": {"schema": {"type": "string"}}, "application/json": {"schema": {}}}},
                                206: {"description": "Requested byte range"}, 304: {"description": "File unchanged"},
                                416: {"description": "Invalid byte range"}} if method == "GET" else
                               {401: {"description": "Invalid or missing SUSI Bearer token", "content": {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}}})})
