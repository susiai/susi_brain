# SUSI Compute

This project incrementally develops an offline-capable inference service.
The existing chat, audio and file/search services run directly as native FastAPI
handlers. Their classes, method names and processing logic are retained; only
the HTTP inputs, configuration access and response construction have changed.
Swagger UI is available offline.

## File evolution

The existing files keep their original names. Every module below is used by the current application:

| File in `src/susi_brain/compute/` | Responsibility |
| --- | --- |
| `main.py` | Compose Compute, describe its capabilities and register its routes |
| `text_service.py` | Native chat service with the existing forwarding logic |
| `audio_service.py` | Native audio service with the existing transcription logic |
| `system_service.py` | Host metrics and Compute system routes |
| `share_service.py` | Native file hosting and existing JSONL search |
| `api.py` | Register bound service methods under the Compute API prefix |
| `requirements.txt` | Runtime dependencies shared by installs, packaging and Docker |

Configuration, resource lifecycle, HTTP bootstrap and observability use `shared/`.
`shared/http/application.py` provides application creation, correlation, native
error handling, health and status routes. `shared/http/responses.py` builds
dictionary-based problem responses. Compute supplies its own description,
stage and capabilities; the shared server does not import Compute or its services.
Swagger UI is installed through `shared/web/swagger.py`; its
assets live in `shared/web/static/vendor/swagger-ui/` for reuse by all applications.
The native router calls the existing synchronous service methods directly. There
is no Flask application or WSGI bridge. Request bodies and uploads are parsed by
FastAPI; blocking model, HTTP and file operations run in its worker threads.
The file service retains the path guard and Bearer-token checks, including the
restriction on access to the existing private history directory. Werkzeug remains
a utility dependency solely for the existing filename sanitization.

All endpoints are served under `/api/v1/compute/`. Legacy URL aliases are removed.
Existing files in `data/` are unchanged. There is one application, one launcher
family and one Dockerfile.

## Install and run

Use Python 3.12 or later. From the repository root:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m susi_brain compute
```

Equivalent installed commands are `susi-brain compute` and `susi-compute`.
With the runtime dependencies installed in your Python 3.12 interpreter, you can
also start the source file directly from the repository root:

```sh
python3.12 src/susi_brain/compute/main.py
```

This command accepts the same `--config`, `--host` and `--port` options.
The default listener is `127.0.0.1:8042`; stop it with Ctrl+C.
Startup logs show the Swagger UI URL and three ready-to-run curl examples for
readiness, status and capabilities, using the configured host and port.
Local Whisper requires FFmpeg (`brew install ffmpeg` on macOS). Runtime
requirements include Whisper, Vosk, multipart uploads, HTTP forwarding and metrics.
Local model weights download on first use, not during application startup.

```sh
curl -f http://127.0.0.1:8042/api/v1/compute/health/ready
curl -f http://127.0.0.1:8042/api/v1/compute/status
curl -f http://127.0.0.1:8042/api/v1/compute/capabilities
```

Readiness returns `{"status":"ok"}` after startup. Capabilities lists `chat`
and `transcription`. The original audio handler retains its Whisper, Vosk and
OpenAI branches. Without an OpenAI key, it always selects local `tiny`, regardless
of the requested model. With a key, `whisper-1` uses OpenAI and other names
select their original local branches. A rejected key does not trigger fallback.

Set `OPENAI_API_KEY` for chat/cloud audio and `SUSI_API_KEY` for file uploads and
deletion. File reads and JSONL search remain public. The local native profile
remains anonymous; unsupported authenticated-profile settings are rejected.
Provider responses, including errors, retain the original service behavior.

| Function | URL |
| --- | --- |
| Chat | `POST /api/v1/compute/chat/completions` |
| Audio | `POST /api/v1/compute/audio/transcriptions` |
| Files | `GET/POST/DELETE /api/v1/compute/data/{path}` |
| Search | `POST /api/v1/compute/data/search` |
| Original readiness payload | `GET /api/v1/compute/system/ready.json` |
| System metrics | `GET /api/v1/compute/system/status.json` |

Chat accepts a JSON dictionary with `messages` containing `role` and `content`,
using the OpenAI Chat Completions parameter names. The handler forwards `model`,
`temperature` and `max_tokens` from the request, defaulting omitted values to
`gpt-3.5-turbo`, 1.0 and 50 respectively; other request
options are ignored. Audio accepts multipart `file` and `model` at
`/api/v1/compute/audio/transcriptions`, using the OpenAI transcription parameter
names. The local default model is `tiny`; local transcription returns `{"text":"..."}`.
An optional OpenAI `Authorization: Bearer ...` header overrides the server key
for cloud audio. Vosk requires mono PCM16 WAV and the existing `en-us` model
directory relative to the working directory. Whisper uses its original cache
location and reloads the model on each request.

Source starts retain the project's `data/` directory regardless of the working
directory; installed packages default to `data/` in the working directory.
Upload using multipart `file` and the SUSI
Bearer token; downloads return the stored bytes. Request `index.json` for directory
metadata, or a directory path for HTML links. Search accepts `file`, `query` and
optional `max_rerank`; it preserves the case-insensitive all-word match on each
JSONL object's `text_t`, returning original objects in file order. Uploads keep
the original HTTP 200 response with a JSON `status` field of 201.

Preserving the existing processing logic retains two known defects: the DELETE handler
references an undefined `full_path`, and Vosk indexes JSON strings as dictionaries.
Those paths currently fail with HTTP 500. Fixing them is a separate change;
step 1 does not replace their implementation or conceal the failures.

Swagger UI: <http://127.0.0.1:8042/api/v1/compute/docs>.
Expand an endpoint, select **Try it out**, then **Execute** to send a request.
For uploads or cloud audio, select **Authorize** and enter `Bearer YOUR_API_KEY`.
The UI uses the current host and port, including a deployment prefix when present.
Its JavaScript, CSS and icons are bundled locally; no CDN or external validator
is required. Chat and audio forms accept real inputs. Service-specific responses keep their existing payloads and status codes,
including HTTP 200 for missing chat configuration or a missing audio file.
FastAPI request validation returns 422 problems; native authentication/path errors
and uncaught failures use the shared problem responses. JSON formatting and opaque
file ETag values follow the native response implementation; conditional and range
downloads remain supported.
The live schema is `/api/v1/compute/openapi.json`; the checked-in specification is
`src/susi_brain/compute/swagger.yaml`. OpenAPI is the only interface description; there is no
separate contract catalog or `/contracts` endpoint. Legacy API and documentation
URLs return 404 without redirects.

## Configuration

Precedence is defaults, JSON file, environment, CLI. Unknown fields/groups,
duplicate JSON fields and unknown service-prefixed environment variables are
rejected. JSON files are limited to 64 KiB. Errors omit submitted values.

```sh
susi-brain compute --config config/compute.example.json --port 8043
SUSI_SERVER_PORT=8043 SUSI_COMPUTE_READINESS_TIMEOUT_SECONDS=1 susi-compute
```

| Group | Field | Default | Limits |
| --- | --- | --- | --- |
| `server` | `host` | `127.0.0.1` | Plain hostname or IP address |
| `server` | `port` | `8042` | 1–65535 |
| `compute` | `readiness_timeout_seconds` | `2` | Greater than 0, at most 30 |
| `compute` | `openai_api_key` | Empty | Secret; also accepts `OPENAI_API_KEY` |
| `compute` | `susi_api_key` | Empty | Required for file mutations; also accepts `SUSI_API_KEY` |
| `compute` | `data_path` | Project `data/` for source starts | Explicit values are relative to the working directory or absolute |

Environment names use `SUSI_SERVER_` and `SUSI_COMPUTE_` plus the uppercase field.
CLI options include `--config`, `--host`, `--port`, `--data-path`, and the original
`--openai_api_key`/`--susi_api_key` (also spelled with hyphens). Service-prefixed
environment variables override the original environment names; CLI overrides both.

## Verification

```sh
python -m pytest
python scripts/export_compute_openapi.py --check
python -m build
```

Regenerate the specification after an API change with
`python scripts/export_compute_openapi.py`. Runtime dependencies come from
`src/susi_brain/compute/requirements.txt`; development tools are installed through the `pyproject.toml`
dev extra. Shared modules have no imports from Compute or other applications.
HTTP responses use dictionaries and native response classes. There are no additional
Pydantic request/response classes; application configuration uses Pydantic.

The existing manual test tools have also been updated for this intermediate
API. Against a configured running server, from the repository root:

```sh
python3.12 test/bin/offline_whisper.py status
python3.12 test/bin/offline_whisper.py openapi
sh test/bin/test_voice.sh
sh test/bin/test_chat.sh
sh test/bin/test_share.sh
```

Use `--base-url http://127.0.0.1:8043` to test a different listener.
Audio checks transcribe a real WAV using local `tiny` by default; pass `--model`
and `--file` to choose another model or recording. Chat needs a configured OpenAI
key; `--chat-model` overrides the default `gpt-3.5-turbo`. The file check needs `SUSI_API_KEY` or `--token`
and exercises upload, read, listing, search and deletion using a unique test path.
The file check exposes the inherited DELETE defect described above; it currently
fails at deletion and leaves its uniquely named file in `data/checks/`.
`all` runs every HTTP check. Running `offline_whisper.py` without arguments retains
its original offline Whisper transcription behavior. `test/html/test_voice.html`
retains microphone recording and transcription; WAV fixtures stay in `test/wav/`.

## Docker

```sh
docker build -f docker/compute.Dockerfile -t susi-compute .
docker run --rm -p 127.0.0.1:8042:8042 \
  -e OPENAI_API_KEY -e SUSI_API_KEY \
  -v susi-data:/app/data -v susi-cache:/home/susi/.cache susi-compute
```

The Python 3.12 slim image installs `src/susi_brain/compute/requirements.txt`,
runs as an unprivileged user, and includes CPU-only Torch, Whisper, Vosk and FFmpeg.
Named volumes retain files and model weights. Models load/download on first
transcription; neither startup nor image creation downloads model weights.
