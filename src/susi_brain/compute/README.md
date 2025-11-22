# SUSI Compute

Run `python3.12 src/susi_brain/compute/main.py` from the repository root after
installing the runtime dependencies. Installed alternatives are
`python -m susi_brain compute` and `susi-compute`. The default listener is
`127.0.0.1:8042`.

Step 1 keeps `audio_service.py`, `share_service.py` and `text_service.py` exactly
as they were in the original Git revision, under their original filenames.
`service_adapter.py` supplies their Flask configuration and connects their WSGI
handlers to the new ASGI server. Original URLs and `/api/v1/compute/` aliases
invoke the same handlers. The adapter contains no replacement service logic.
The server boundary rejects document traversal and symlinks before dispatch.

Swagger UI at `/api/v1/compute/docs` supports **Try it out** with local assets.
The reusable installer is `shared/web/swagger.py`; all UI assets live in
`shared/web/static/vendor/swagger-ui/` and are shared by the applications.
It documents the original request defaults, payloads and response status codes.
The specification is `src/susi_brain/compute/swagger.yaml`.

Set `OPENAI_API_KEY` for chat/cloud audio and `SUSI_API_KEY` for file mutations.
The original `--openai_api_key` and `--susi_api_key` flags still work. File reads
and search remain public. Files default to the project's `data/` directory;
change this with `--data-path`. Local Whisper requires FFmpeg. Without an OpenAI
key the original audio handler always selects `tiny`. The original chat handler
always uses `gpt-3.5-turbo`, temperature 1 and max_tokens 50.

Exact restoration retains two existing defects: DELETE references an undefined
`full_path`, and Vosk treats JSON strings as dictionaries. These paths return
HTTP 500; their correction requires a separate service change.

Configuration, resource lifecycle, native errors and observability use `shared/`.
The generic HTTP factory, correlation and health/status routes live in
`shared/http/application.py`; problem responses live in `shared/http/responses.py`.
`main.py` supplies Compute's description, stage and capabilities. `system_service.py`
keeps host metrics and the original system/documentation URL aliases.
`create_compute_app(settings=None, resources=None)` creates no storage directory,
downloads no models and makes no network calls. Flask services are initialized
on the first service request. Injected resources start and stop with the server.
Pydantic validates configuration; there is no contract layer or catalog endpoint.

See the [repository README](../../../README.md) for installation, configuration,
verification and Docker.
