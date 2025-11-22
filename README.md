# SUSI Compute

This project incrementally develops an offline-capable inference service.
Step 1 connects the existing Flask services to FastAPI and shared utilities.
`audio_service.py`, `share_service.py` and `text_service.py` are restored exactly
from the original Git revision. Their implementation is unchanged.
Swagger UI is available offline.

## File evolution

The existing files keep their original names. Every module below is used by the current application:

| File in `src/susi_brain/compute/` | Responsibility |
| --- | --- |
| `main.py` | Compose Compute, describe its capabilities and register its routes |
| `text_service.py` | Unchanged Flask chat service |
| `audio_service.py` | Unchanged Flask audio service |
| `system_service.py` | Host metrics and original system/documentation URL aliases |
| `share_service.py` | Unchanged Flask file hosting and JSONL search |
| `service_adapter.py` | Supply Flask configuration, adapt WSGI to ASGI and document URL aliases |
| `requirements.txt` | Runtime dependencies shared by installs, packaging and Docker |

Configuration, resource lifecycle, HTTP bootstrap and observability use `shared/`.
`shared/http/application.py` provides application creation, correlation, native
error handling, health and status routes. `shared/http/responses.py` builds
dictionary-based problem responses. Compute supplies its own description,
stage and capabilities; the shared server does not import Compute or its services.
Swagger UI is installed through `shared/web/swagger.py`; its
assets live in `shared/web/static/vendor/swagger-ui/` for reuse by all applications.
The adapter invokes the original Flask namespaces; it does not
reimplement chat, transcription or file operations. Flask and its service
dependencies remain runtime requirements during this transition.
The server boundary rejects document traversal and symlinks before calling the
file handlers, and keeps the existing private history directory inaccessible.

Original HTTP URLs and the new URLs call the same original handlers.
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

| Function | Native URL | Original URL kept working |
| --- | --- | --- |
| Chat | `POST /api/v1/compute/inference/chat` | `/api/text/chat/completions` |
| Audio | `POST /api/v1/compute/inference/transcriptions` | `/api/audio/transcriptions`, `/v1/audio/transcriptions`, `/api/audio/voice/transcriptions` |
| Files | `GET/POST/DELETE /api/v1/compute/data/{path}` | `/api/data/{path}` |
| Search | `POST /api/v1/compute/data/search` | `/api/data/search` |
| Original readiness | `GET /api/v1/compute/system/ready.json` | `/api/system/ready.json` |
| System metrics | `GET /api/v1/compute/system/status.json` | `/api/system/status.json` |

Chat accepts a JSON dictionary with `messages`. The original handler fixes the
model to `gpt-3.5-turbo`, temperature to 1 and max_tokens to 50; other request
options are ignored. Audio accepts multipart `file` and `model`.
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

Restoring the exact code also preserves existing defects: the DELETE handler
references an undefined `full_path`, and Vosk indexes JSON strings as dictionaries.
Those paths currently fail with HTTP 500. Fixing them is a separate change;
step 1 does not replace their implementation or conceal the failures.

Swagger UI: <http://127.0.0.1:8042/api/v1/compute/docs>.
Expand an endpoint, select **Try it out**, then **Execute** to send a request.
The UI uses the current host and port, including a deployment prefix when present.
Its JavaScript, CSS and icons are bundled locally; no CDN or external validator
is required. Chat and audio forms accept real inputs. Native operational routes
use problem responses; the three existing services keep their original responses,
including HTTP 200 for missing chat configuration or a missing audio file.
The live schema is `/api/v1/compute/openapi.json`; the checked-in specification is
`src/susi_brain/compute/swagger.yaml`. OpenAPI is the only interface description; there is no
separate contract catalog or `/contracts` endpoint. Original `/api/docs/`
redirects to Swagger UI, and `/swagger.json` serves the current OpenAPI schema.

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
The adapter passes through the original Flask responses. Pydantic validates configuration only.

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
key and uses the original fixed chat model. The file check needs `SUSI_API_KEY` or `--token`
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
