"""Register computational services as FastAPI endpoints."""

PREFIX = "/api/v1/compute"


def install_service_routes(app):
    """Register the native handlers under the Compute API prefix."""
    from .audio_service import VoiceTranscription
    from .share_service import FileResource, SearchResource
    from .text_service import ChatCompletion

    chat_handler = ChatCompletion().post
    audio_handler = VoiceTranscription().post
    search_handler = SearchResource().post
    files = FileResource()

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
        "model": {"type": "string", "default": "gpt-3.5-turbo"},
        "temperature": {"type": "number", "default": 1.0},
        "max_tokens": {"type": "integer", "default": 50},
    }, required=("messages",),
                     example={"messages": [{"role": "user", "content": "Hello"}]})
    register(PREFIX + "/chat/completions", chat_handler, "POST", "compute__chat_0",
             "Generate a chat completion. Forwards messages, model, temperature and max_tokens to OpenAI. "
             "Omitted options default to model gpt-3.5-turbo, temperature 1.0 and max_tokens 50; other request options are ignored. "
             "Uses the configured OpenAI key. Missing data, messages or key return an error dictionary with HTTP 200. "
             "Returns the provider JSON, including provider errors, with HTTP 200.", body=chat)

    audio = {"required": False, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["file"], "properties": {
            "file": {"type": "string", "format": "binary"},
            "model": {"type": "string", "default": "tiny",
                      "description": "tiny, base, small, medium, large, whisper-1, en-us or de; other names return an empty dictionary when a key is set."},
        },
    }}}}
    register(PREFIX + "/audio/transcriptions", audio_handler, "POST", "compute__transcription_0",
             "Transcribe an audio file. The Authorization header overrides the configured OpenAI key. "
             "With a key, whisper-1 forwards to OpenAI; other supported names select local Whisper or Vosk models. "
             "Without a key every requested model becomes tiny. Whisper loads on each request. "
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
