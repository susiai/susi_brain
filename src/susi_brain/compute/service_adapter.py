"""Connect the unchanged Flask services to the ASGI server and its OpenAPI UI."""

from pathlib import Path, PurePosixPath
from threading import Lock

from a2wsgi import WSGIMiddleware
from fastapi import Request
from fastapi.responses import Response

PREFIX = "/api/v1/compute"


def create_service_app(settings):
    """Supply the configuration and namespaces previously assembled by main.py."""
    from flask import Flask, abort, request
    from flask_restx import Api
    from . import audio_service, share_service, text_service

    app = Flask(__name__)
    app.config.update(
        OPENAI_API_KEY=settings.openai_api_key.get_secret_value(),
        SUSI_API_KEY=settings.susi_api_key.get_secret_value() or None,
        DATA_PATH=str(settings.data_path.resolve()),
    )
    api = Api(app, doc=False)
    for namespace in (share_service.api, text_service.api, audio_service.api, audio_service.v1api):
        api.add_namespace(namespace)

    @app.before_request
    def confine_document_paths():
        """Keep the shared server boundary inside its separate document root."""
        if not request.path.startswith("/api/data/"):
            return
        path = request.path.removeprefix("/api/data/")
        searching = path == "search" and request.method == "POST"
        if searching:
            body = request.get_json(silent=True)
            if not isinstance(body, dict) or not isinstance(body.get("file"), str):
                return
            path = body["file"]
        if PurePosixPath(path).is_absolute() or {"..", ".susi-history"} & set(path.split("/")) or "\\" in path:
            abort(403)
        current = Path(app.config["DATA_PATH"])
        relative = path if searching else share_service.secure_path(path)
        for part in relative.split("/"):
            current = current / part
            if current.is_symlink():
                abort(403)
    return app


class ServiceResponse(Response):
    """Let the WSGI bridge transmit the original status, headers and body."""

    def __init__(self, adapter, path):
        super().__init__()
        self.adapter, self.path = adapter, path

    async def __call__(self, scope, receive, send):
        scope = {**scope, "path": scope.get("root_path", "") + self.path}
        await self.adapter(scope, receive, send)


def install_service_routes(app):
    """Register URL aliases without parsing or replacing the service responses."""
    services = None
    lock = Lock()

    def dispatch(environ, start_response):
        nonlocal services
        with lock:
            if services is None:
                services = create_service_app(app.state.settings)
        return services(environ, start_response)

    adapter = WSGIMiddleware(dispatch)

    def register(path, target, method, name, description, *, body=None, responses=None, token=False):
        async def call_service(request: Request):
            target_path = target.format(**request.path_params)
            return ServiceResponse(adapter, target_path)

        operation = {}
        if "{req_path:path}" in path:
            operation["parameters"] = [{"name": "req_path", "in": "path", "required": True,
                                        "schema": {"type": "string"}}]
        if body:
            operation["requestBody"] = body
        if token:
            operation.setdefault("parameters", []).append({
                "name": "Authorization", "in": "header", "required": False,
                "description": "Bearer SUSI_API_KEY for file mutations; an OpenAI Bearer key for cloud audio.",
                "schema": {"type": "string"},
            })
        app.add_api_route(
            path, call_service, methods=[method], response_class=Response,
            operation_id=name, summary=description.split(". ")[0], description=description,
            tags=["Existing services"], openapi_extra=operation,
            responses={200: {"description": "Original service response", "content": {"application/json": {"schema": {}}}},
                       400: {"description": "Original invalid input response", "content": {"application/json": {"schema": {}}}},
                       403: {"description": "The server boundary rejects traversal and symlinks", "content": {"application/json": {"schema": {"type": "object"}}}},
                       404: {"description": "Resource not found", "content": {"application/json": {"schema": {}}}},
                       415: {"description": "A JSON body requires application/json", "content": {"application/json": {"schema": {"type": "object"}}}},
                       500: {"description": "Original service failure or adapter failure",
                             "content": {"application/json": {"schema": {"type": "object"}},
                                         "application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}},
                       **(responses or {})},
        )
        # These handlers retain Flask validation and error responses.
        app.routes[-1].responses.pop(422, None)

    def json_body(properties, *, required=(), example=None):
        schema = {"type": "object", "properties": properties}
        if required:
            schema["required"] = list(required)
        content = {"schema": schema}
        if example is not None:
            content["example"] = example
        return {"required": True, "content": {"application/json": content}}

    chat = json_body({"messages": {"type": "array", "items": {"type": "object"}}}, required=("messages",),
                     example={"messages": [{"role": "user", "content": "Hello"}]})
    for index, path in enumerate((PREFIX + "/inference/chat", "/api/text/chat/completions")):
        register(path, "/api/text/chat/completions", "POST", f"compute__chat_{index}",
                 "Run the original chat service. Forwards messages to OpenAI with the fixed model gpt-3.5-turbo, "
                 "temperature 1.0 and max_tokens 50; other request options are ignored. "
                 "Uses the configured OpenAI key. Missing data, messages or key return an error dictionary with HTTP 200. "
                 "The provider JSON is returned unchanged, including provider errors, with HTTP 200.", body=chat)

    audio = {"required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["file"], "properties": {
            "file": {"type": "string", "format": "binary"},
            "model": {"type": "string", "default": "tiny",
                      "description": "tiny, base, small, medium, large, whisper-1, en-us or de; other names return an empty dictionary when a key is set."},
        },
    }}}}
    for index, path in enumerate((PREFIX + "/inference/transcriptions", "/api/audio/transcriptions",
                                  "/v1/audio/transcriptions", "/api/audio/voice/transcriptions")):
        register(path, "/api/audio/transcriptions", "POST", f"compute__transcription_{index}",
                 "Run the original audio service. The Authorization header overrides the configured OpenAI key. "
                 "With a key, whisper-1 forwards to OpenAI; other names use the existing local branches. "
                 "Without a key every requested model becomes tiny. Whisper loads on each request. "
                 "Vosk requires mono PCM16 WAV and the existing en-us model directory. "
                 "Missing files return an error dictionary with HTTP 200; unsupported model names return an empty dictionary. "
                 "The original Vosk JSON handling is unchanged and can raise a server error.", body=audio, token=True)

    search = json_body({
        "file": {"type": "string"}, "query": {"type": "string"}, "max_rerank": {"type": "integer"},
    }, required=("file", "query"), example={"file": "documents.jsonl", "query": "hello", "max_rerank": 5})
    for index, path in enumerate((PREFIX + "/data/search", "/api/data/search")):
        register(path, "/api/data/search", "POST", f"compute__search_{index}",
                 "Run the original JSONL search. Case-insensitive all-word matching on text_t; "
                 "returns original objects in file order, limited by max_rerank when supplied.", body=search,
                 responses={400: {"description": "Invalid JSON input"}, 404: {"description": "File not found"}})

    upload = {"required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["file"], "properties": {"file": {"type": "string", "format": "binary"}},
    }}}}
    for index, base in enumerate((PREFIX + "/data/", "/api/data/")):
        for method, description in (
            ("GET", "Read files or directory listings using the original file service. index.json returns directory metadata."),
            ("POST", "Upload a file using the original file service. Requires the SUSI Bearer key. "
                     "The original response has HTTP 200 and a JSON status field of 201."),
            ("DELETE", "Call the original delete handler. Requires the SUSI Bearer key. "
                       "The original handler references an undefined full_path and currently returns HTTP 500."),
        ):
            register(base + "{req_path:path}", "/api/data/{req_path}", method,
                     f"compute__file_{method.lower()}_{index}", description,
                     body=upload if method == "POST" else None, token=method != "GET",
                     responses={**({200: {"description": "File bytes, HTML directory listing or JSON index",
                                          "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
                                                      "text/html": {"schema": {"type": "string"}}, "application/json": {"schema": {}}}},
                                    206: {"description": "Requested byte range"}, 304: {"description": "File unchanged"},
                                    416: {"description": "Invalid byte range"}} if method == "GET" else
                                   {401: {"description": "Invalid or missing SUSI Bearer token", "content": {"text/html": {"schema": {"type": "string"}}}}})})
