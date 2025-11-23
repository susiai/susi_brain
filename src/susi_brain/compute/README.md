# SUSI Compute

Run `python3.12 src/susi_brain/compute/main.py` from the repository root after
installing the runtime dependencies. Installed alternatives are
`python -m susi_brain compute` and `susi-compute`. The default listener is
`127.0.0.1:8042`.

`audio_service.py`, `share_service.py` and `text_service.py` keep their original
classes, method names and processing logic. Their HTTP inputs and responses now
use FastAPI directly. `api.py`, renamed from the former adapter with `git mv`,
registers the bound synchronous methods under `/api/v1/compute/`.
Legacy API and documentation aliases return 404. Blocking operations run in FastAPI's worker threads.
There is no Flask application or WSGI bridge. File requests retain path checks,
Bearer authorization, conditional downloads and byte ranges.

Swagger UI at `/api/v1/compute/docs` supports **Try it out** with local assets.
Use **Authorize** and enter `Bearer YOUR_API_KEY` for uploads or cloud audio.
The reusable installer is `shared/web/swagger.py`; all UI assets live in
`shared/web/static/vendor/swagger-ui/` and are shared by the applications.
It documents the original request defaults, payloads and response status codes.
The specification is `src/susi_brain/compute/swagger.yaml`.

Generate chat completions with `POST /api/v1/compute/chat/completions` using
the OpenAI-style JSON field `messages` with `role` and `content`.

Transcribe audio with `POST /api/v1/compute/audio/transcriptions` using the
OpenAI-style multipart fields `file` and `model`. The local default is `tiny`;
local transcription returns `{"text":"..."}`.

Set `OPENAI_API_KEY` for chat/cloud audio and `SUSI_API_KEY` for file mutations.
The original `--openai_api_key` and `--susi_api_key` flags still work. File reads
and search remain public. Files default to the project's `data/` directory;
change this with `--data-path`. Local Whisper requires FFmpeg. Without an OpenAI
key the original audio handler always selects `tiny`. The original chat handler
uses request values for `model`, `temperature` and `max_tokens`, defaulting omitted
values to `gpt-3.5-turbo`, 1.0 and 50 respectively.

The unchanged processing logic retains two existing defects: DELETE references an undefined
`full_path`, and Vosk treats JSON strings as dictionaries. These paths return
HTTP 500; their correction requires a separate service change.

Configuration, resource lifecycle, native errors and observability use `shared/`.
The generic HTTP factory, correlation and health/status routes live in
`shared/http/application.py`; problem responses live in `shared/http/responses.py`.
`main.py` supplies Compute's description, stage and capabilities. `system_service.py`
keeps host metrics and the Compute system routes.
`create_compute_app(settings=None, resources=None)` creates no storage directory,
downloads no models and makes no network calls. Whisper and Vosk libraries are imported on demand
by the existing transcription branches. Injected resources start and stop with the server.
Application configuration uses Pydantic; there are no additional request/response
model classes, contract layer or catalog endpoint. Service-specific HTTP 200
errors are retained; invalid FastAPI inputs return 422 and native HTTP failures
use the shared problem responses.

See the [repository README](../../../README.md) for installation, configuration,
verification and Docker.
