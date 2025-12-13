# SUSI Compute

This project incrementally develops an offline-capable inference service.
Chat, audio and file/search services run directly as native FastAPI handlers.
Chat proxies configured model servers through the smart/small model aliases;
TTS proxies configured Ollama speech models as WAV. Transcription and file/search services retain their existing processing logic.
The local Compute interface and Swagger UI are available offline.

## File evolution

The existing files keep their original names. Every module below is used by the current application:

| File in `src/susi_brain/compute/` | Responsibility |
| --- | --- |
| `main.py` | Compose Compute, describe its capabilities and register its routes |
| `text_service.py` | Chat proxy, live model discovery and JSON/SSE forwarding |
| `stream_metadata.py` | First/terminal SSE routing fields compatible with triton_llmlb; raw middle-line forwarding |
| `speech_service.py` / `speech_api.py` / `wav.py` | Ollama TTS metadata, WAV forwarding/validation and speech API contract |
| `calibration.py` / `calibration_api.py` | Endpoint/model capability probes, streaming timing and GUI-facing controls |
| `routing.py` | Active connection accounting and selection by load, ranking and measured speed |
| `web.py` | Local interface routes using the shared SUSI visual shell |
| `monitoring.py` / `monitoring_api.py` | HTTP request lifecycle accounting, durable request log, active dashboard and UTC histogram APIs |
| `audio_service.py` | Native audio service with the existing transcription logic |
| `system_service.py` | Host metrics and Compute system routes |
| `share_service.py` | Native file hosting and existing JSONL search |
| `api.py` | Register OpenAI chat/audio paths and the other Compute routes |
| `requirements.txt` | Runtime dependencies shared by installs, packaging and Docker |

Configuration, resource lifecycle, HTTP bootstrap and observability use `shared/`.
`shared/http/application.py` provides application creation, correlation, native
error handling, health and status routes. `shared/http/responses.py` builds
dictionary-based problem responses. Compute supplies its own description,
stage and capabilities; the shared server does not import Compute or its services.
Swagger UI is installed through `shared/web/swagger.py`; its
assets live in `shared/web/static/vendor/swagger-ui/` for reuse by all applications.
The native router calls bound service methods directly. Chat uses a lifespan-owned
asynchronous HTTPX connection pool. Blocking audio, model and file operations run
in FastAPI's worker threads. There is no Flask application or WSGI bridge.
The file service retains the path guard and Bearer-token checks, including the
restriction on access to the existing private history directory. Werkzeug remains
a utility dependency solely for the existing filename sanitization.

Chat and audio use the standard OpenAI paths `/v1/chat/completions` and
`/v1/audio/transcriptions`. Set an OpenAI client's base URL to
`http://127.0.0.1:8042/v1`. Other Compute endpoints are served under
`/api/v1/compute/`. The former Compute-prefixed chat/audio paths and other legacy
URL aliases return 404.
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
Startup logs show the interface and Swagger UI URLs and three ready-to-run curl examples for
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

Set `OPENAI_API_KEY` for cloud audio and `SUSI_API_KEY` for file uploads and
deletion. File reads and JSONL search remain public. The local native profile
remains anonymous; unsupported authenticated-profile settings are rejected.
Chat forwards backend status codes and response bodies; audio and file responses
retain their existing service behavior.

| Function | URL |
| --- | --- |
| Chat | `POST /v1/chat/completions` |
| Live chat connections | `GET /api/v1/compute/chat/connections` |
| Audio | `POST /v1/audio/transcriptions` |
| Files | `GET/POST/DELETE /api/v1/compute/data/{path}` |
| Search | `POST /api/v1/compute/data/search` |
| Original readiness payload | `GET /api/v1/compute/system/ready.json` |
| System metrics | `GET /api/v1/compute/system/status.json` |

Chat requires `model` to be exactly `smart` or `small` and a nonempty `messages`
array. Configure `compute.endpoints` as a list of `host:port` strings or API URL/key objects, and
`compute.smart`/`compute.small` as lists of model names or `{name, ranking}` objects:

```json
{
  "compute": {
    "endpoints": ["127.0.0.1:11434", "llm-server:8080"],
    "smart": [{"name": "your-smart-model", "ranking": 90}, {"name": "your-smart-fallback-model", "ranking": 70}],
    "small": [{"name": "your-small-model", "ranking": 30}]
  }
}
```

Replace these model placeholders with names advertised by your servers. Start
with `susi-compute --config config/compute.example.json`, or provide lists with
`--endpoints localhost:11434 --smart MODEL_A MODEL_B --small MODEL_C`.
Ranking is an operator-assigned finite number: larger values mean higher quality.
It defaults to `0`, including for string entries. Strings and objects may be mixed;
duplicate names within a group are rejected case-insensitively. Set rankings in
JSON configuration or JSON-array environment values, for example
`SUSI_COMPUTE_SMART='[{"name":"MODEL_A","ranking":90}]'`. CLI names use ranking 0.
String endpoints accept hostnames, IPv4 and bracketed IPv6 (`[::1]:11434`) and
expand to `http://host:port/v1`. For hosted or authenticated providers, use objects
with `url` and either `api_key` or `api_key_env`. The URL is the HTTP(S) API base,
including its prefix; a trailing `/chat/completions` is also accepted and removed.
HTTPS certificates are verified. Embedded URL credentials, queries and fragments
are rejected. Both endpoint forms may be mixed in one list.

For example, OpenRouter uses `https://openrouter.ai/api/v1` and Bearer keys
([provider documentation](https://openrouter.ai/docs/api_reference/authentication)):

```json
{
  "compute": {
    "endpoints": [
      "localhost:11434",
      {"url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"}
    ],
    "smart": ["provider/your-smart-model"],
    "small": ["your-local-small-model"]
  }
}
```

`api_key` accepts a literal key; `api_key_env` names an environment variable
resolved at configuration load. Choose one key source per endpoint. Missing or
empty referenced variables fail startup. Keys are excluded from settings repr
and JSON exports. Set `OPENROUTER_API_KEY` and replace the model placeholders with
provider model IDs, then run
`susi-compute --config config/compute-openrouter.example.json` for a hosted-only
example. Each key is sent as `Authorization: Bearer ...` only to its own endpoint,
on both model discovery and chat requests.

Each valid chat request refreshes `/models` concurrently on all configured servers.
Names match case-insensitively and requests use the advertised spelling. Selection
and reservation happen together without awaiting, so simultaneous requests see
the latest connection counts:

1. On each endpoint, reuse a running model from the requested group if possible.
   This prefers one active model per endpoint. If all its active models belong to
   the other group, another model may be used; there is no hard lock or queue.
2. Choose among endpoints with the fewest active proxy requests across all models.
3. At equal load, `smart` chooses the highest configured ranking, then the fastest
   candidate at that ranking. `small` chooses the fastest regardless of ranking.
4. Speed compares calibrated `tokens_per_second` descending, then `ttft_seconds`
   ascending. Missing measurements sort last for each metric; nothing is estimated.
   Remaining ties use model-list order, then endpoint order.

Measurements are endpoint-specific, so the same model can have a different
priority on each server. Routing uses the current calibration dictionary; starting
a new run clears the previous measurements. Live inventory availability is still
required. Configure rankings before calibration; measured speed takes effect as
successful speed samples complete.

`GET /api/v1/compute/chat/connections` exposes the total `active_connections` and
an `endpoints` array with stable `id`, `url`, `active_connections` and a `models`
dictionary mapping active concrete model names to counts. IDs match calibration.
Counters are shared by Chat and TTS. They include connecting/waiting requests,
non-streaming responses, upstream SSE/WAV lifetimes and calibration checks.
Chat releases on `[DONE]` without waiting for upstream EOF or client draining;
WAV releases after stream cleanup.
Completion, errors, timeout, cancellation and client disconnect release exactly
one reservation after cleanup. Empty endpoints report `0` and `{}`. Inventories,
ordinary metadata discovery and idle pooled sockets are excluded. Reading counts makes no
upstream calls and exposes no credentials. Counters belong to this application
process, start at zero, and are not shared between workers.

Only the request's `model` is replaced. All other JSON fields, including tools,
structured-output options, sampling and `stream`, pass through to the selected
server; omitted options use backend defaults. Non-streaming and error response
bodies, the standard `model` field, status, Content-Type and Retry-After are
preserved. Streaming follows the sibling `triton_llmlb` convention: the first
JSON `data:` object and the first terminal completion object receive these
top-level routing/statistics fields:

| Field | Value |
| --- | --- |
| `modelname` | Concrete model selected from the endpoint's inventory |
| `compute_group` | Requested `smart` or `small` group |
| `compute_hostname` | Selected endpoint's hostname, without scheme, port or path |
| `request_bytes` | Actual original incoming UTF-8 body length, including JSON whitespace, before model rewriting; excludes HTTP headers |

These values replace any same-named upstream fields on the two boundary objects.
The existing `X-Compute-Target` response header retains the exact endpoint ID and
expected calibration metrics. A first event that is already terminal is annotated
once. The terminal event is the one with a non-null `finish_reason` in a choice,
not a subsequent usage-only event or the `[DONE]` marker.

The proxy assembles byte lines across network chunks. It parses only the first
data payload and terminal candidates shortlisted by a byte regex. Other JSON
content, thinking, tools and usage lines pass through without decoding/parsing
or reserialization. Annotated objects use UTF-8 `data: ...\n\n`; existing blank
framing is retained. Invalid/non-object payloads remain unchanged. As in the
reference, an invalid first data line consumes the first annotation attempt;
multiline JSON is not combined for annotation. No response harmonization or
synthetic token usage is performed, and `stream_options.include_usage` is only
forwarded when supplied by the client.

`[DONE]`, including a marker without its last newline, closes upstream/releases
the reservation without waiting for EOF; already received trailing blank framing
is retained. Streams disable buffering/caching and send `no-transform`. HTTPX
handles upstream content encoding; network chunk boundaries can differ. The
browser independently renders content and reads unchanged upstream token usage;
calibration retains its own probing/measurement path. Caller Authorization/cookies and the global OpenAI key are not sent
to chat servers; endpoint-specific keys are configured separately. Model discovery
never downloads, loads or pulls models.

```sh
curl http://127.0.0.1:8042/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"small","messages":[{"role":"user","content":"Hello"}]}'
```

Invalid aliases/messages/stream flags return HTTP 400 with an OpenAI-style error
envelope. Empty configuration or no matching available model returns 503 with
`Retry-After: 1`; chat connection/protocol failures return 502 and timeouts 504.
Discovery has a configurable deadline (default 3 seconds per server) and 1 MiB
inventory limit. Chat has a configurable upstream operation/idle timeout (default
120 seconds) and 8 MiB upstream body limit; the Chat UI allows 8 KiB more for
annotation/framing overhead. Requests are sent once after selection;
generation is not automatically retried on another backend. A failure after
headers terminates the stream and is recorded as failed. No proxy-generated
error or `[DONE]` frame is appended. All exits close upstream and release the
reservation. HTTP readiness reports the service lifecycle, not model availability.

Audio accepts multipart `file` and `model` at
`/v1/audio/transcriptions`, using the OpenAI transcription parameter
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

## Local web interface

Open [Compute](http://127.0.0.1:8042/api/v1/compute/ui). The shared shell uses a warm Amber terminal theme with classless, semantic
HTML and [WebTUI](https://webtui.ironclad.sh/) 0.1.10. IBM Plex Mono 2.5.0
(400/500), CSS modules and their licenses are packaged locally; there is no CDN
or frontend runtime. Colors live in `shared/web/static/amber.css`. The horizontal top navigation groups Monitoring (Requests, Connections,
Models), Services (Chat, Speech to text, Text to speech), Configuration
(Startup configuration), and Documentation (Swagger UI, OpenAPI specification).
Menu items have no numbers; the active group and page are highlighted. Native
disclosure menus open by click or keyboard and close with Escape or an outside
click. The 220px left column contains the current view's options, filters or
constraints, beside the operational workspace. Below 720px the groups wrap into
two columns and the context column becomes a collapsible section above the
workspace. A status footer reports snapshot refreshes.
Keyboard focus is teal, successful results are green, errors are coral and
technical values are lavender; statuses also have text labels. The models view groups summaries by
endpoint, inspired by `llm-lab/gateway/ollama-dashboard.html`, using the local
SUSI design. It shows configured/measured models, group rankings, capability
results, TTFT, throughput, active requests and expandable per-probe checks.
Search, group filters, speed sorting and batches of 25 models per endpoint keep
large inventories usable. Unmeasured capabilities and missing metrics remain
explicitly unknown; active requests are not evidence that model weights reside
in server memory.

Navigation also provides service monitoring, active endpoint/model connections,
and read-only startup endpoint/model configuration with API-key presence only.
The `[chat]` pane at `/api/v1/compute/ui#chat` sends one prompt at a time with
no conversation history or browser persistence. Its left context column has
**Model** (`smart`/`small`, default `small`) and **Thinking** (`off`/`on`, default
`off`) controls. Each send includes only the current user message and replaces
the prior response. Stop cancels the stream; leaving the pane discards its text.
Ctrl/Command + Enter submits the active chat, TTS or monitoring-search form,
including from the service options in the context column. Replies and optional thinking text render as
plain text.

Thinking forwards `reasoning_effort: "medium"`/`"none"`, `reasoning.enabled`,
and `chat_template_kwargs.enable_thinking`. These options follow the
[Ollama](https://docs.ollama.com/api/openai-compatibility),
[OpenRouter](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
and [llama.cpp](https://github.com/ggml-org/llama.cpp/tree/master/tools/server#post-v1chatcompletions-openai-compatible-chat-completions-endpoint)
interfaces. Provider/model support determines whether thinking can actually
be enabled or disabled; the proxy passes the options through without retries.

After every chat stream, a speed table compares **Expected** calibration with
**Measured in browser** throughput and time to first text/thinking token.
The proxy's `X-Compute-Target` response header contains ASCII JSON with
`endpoint_id`, concrete `model`, and `expected: {tokens_per_second, ttft_seconds}`.
It freezes the exact selected pair's calibration when the request reserves
its target, before upstream I/O; absent metrics are `null`. The header is added
to forwarded JSON/SSE responses, including forwarded error statuses, exposed
through CORS, and omitted on proxy-generated errors. No credentials are included.
The Chat UI reads the first/terminal stream metadata to display the actual model,
hostname, group and original request byte count. Older streams without these
fields retain the header/model fallback. Expected performance stays in the
header; generated token counts and actual browser speed come from upstream usage.

The browser requests `stream_options.include_usage: true`. It uses
`performance.now()` to divide final upstream `usage.completion_tokens` by time
from starting the request to stream completion. This includes discovery,
network latency and any reported reasoning tokens. Expected throughput uses
the calibration probe's request duration, so differing prompts, thinking settings
and network paths affect comparability. Empty/role-only events do not start
TTFT. Missing calibration or token usage is explicitly `unavailable`; transport
chunks are never counted as tokens. Failed, stopped or truncated streams retain
partial text and elapsed/TTFT values, but have no completed token rate.

The `[speech to text]` pane at `/api/v1/compute/ui#speech` accepts one audio
file via drag and drop or the file picker and transcribes it immediately.
It uses the existing `POST /v1/audio/transcriptions` multipart API with local
Whisper `tiny`. WAV, MP3, M4A, OGG, FLAC, WebM and other FFmpeg-supported
audio containers work; the browser accepts files up to 25 MiB. Recognition
preserves the spoken language. The first local request may download model weights.

The microphone icon starts a browser recording after permission is granted.
Click it again to stop and transcribe; at three minutes it stops and submits
automatically. This sends a complete recording rather than live partial results.
The recorder selects an available audio container and preserves its MIME type
and matching filename instead of labeling encoded recordings as WAV. Microphone
access requires HTTPS or localhost and a compatible
[MediaRecorder browser](https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder).
No permission prompt or recording starts merely by opening the page.

Cancel discards a recording or aborts an upload in the browser. Leaving the
pane/page stops microphone tracks, including streams returned by a late permission
grant. An already-running server transcription may continue after upload
cancellation. Files, recordings and transcripts are not persisted by the frontend;
the latest transcript is rendered as plain text. The 25 MiB and three-minute
limits belong to this test interface and do not change the transcription API.

The interface reads calibration, connection and service snapshots every five
seconds while visible. Hidden tabs pause polling; failed reads retain prior
values with a stale-data message. Loading/refreshing the page makes no inference
or provider inventory calls. All browser requests stay on the Compute origin,
including under a reverse-proxy deployment prefix. Assets are packaged locally
without a frontend framework, CDN, build step or new runtime dependency.

Expand `[start / cancel calibration]` in the models pane to start a run; its default scope is the
configured smart/small/speech union. The all-advertised option is available. Starting
replaces prior results and may consume provider credits. Cancel retains partial
results. Mutating requests are never automatically retried; after an uncertain
response, check the refreshed run before starting another.

Swagger UI uses the same Amber theme, with navigation back to the
interface: [Swagger](http://127.0.0.1:8042/api/v1/compute/docs).
Expand an endpoint, select **Try it out**, then **Execute** to send a request.
For uploads or cloud audio, select **Authorize** and enter `Bearer YOUR_API_KEY`.
The UI uses the current host and port, including a deployment prefix when present.
Its JavaScript, CSS, fonts and icons are bundled locally; shared theme assets
are allowlisted under `/api/v1/compute/ui-assets/{asset_path}`; no CDN or external validator
is required. Chat and audio forms accept real inputs. Service-specific responses keep their existing payloads and status codes,
including HTTP 200 for a missing audio file. Chat proxy errors use the status
codes described above.
FastAPI request validation returns 422 problems; native authentication/path errors
and uncaught failures use the shared problem responses. JSON formatting and opaque
file ETag values follow the native response implementation; conditional and range
downloads remain supported.
The live schema is `/api/v1/compute/openapi.json`; the checked-in specification is
`src/susi_brain/compute/swagger.yaml`. OpenAPI is the only interface description; there is no
separate contract catalog or `/contracts` endpoint. Legacy API and documentation
URLs return 404 without redirects.

## HTTP request monitoring

Open the `[monitoring]` section of the web interface for server-wide live
requests, the completed-request log and a histogram. The histogram selector
offers minute buckets over the last hour, ten-minute buckets over the last day,
hour buckets over the last week, and day buckets over the last 30 days. Hover
bars for counts or expand the data table. Failed and cancelled/disconnected
requests are shown separately in the Amber chart, using labeled status colors. Log pages support
method/status filters and newest/newer/older navigation. Monitoring polling can
be hidden from these views; process totals always include all recorded traffic.
The monitoring APIs are polled only while this view is visible.

| Method | URL | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/compute/monitoring/active` | Process totals and ongoing requests, including streams and cleanup |
| `GET` | `/api/v1/compute/monitoring/requests` | Newest-completion-first request log with filters and pagination |
| `GET` | `/api/v1/compute/monitoring/histogram` | Zero-filled UTC completion buckets and aggregate totals |

```sh
curl 'http://127.0.0.1:8042/api/v1/compute/monitoring/active?exclude_monitoring=true'
curl 'http://127.0.0.1:8042/api/v1/compute/monitoring/requests?limit=50&status_code=500'
curl 'http://127.0.0.1:8042/api/v1/compute/monitoring/histogram?interval=ten_minutes&buckets=144'
```

An ASGI middleware records HTTP API traffic under `/api` and `/v1`, including
unknown API routes, documentation/assets, CORS preflights, validation failures,
non-streaming requests and full SSE lifetimes. Requests rejected by the HTTP
server before ASGI and non-API URLs are outside this monitor. Active rows include
monotonic elapsed time, processing/responding/streaming phase, current HTTP status
and body bytes read/sent. Chat rows expose the selected group, concrete model and
endpoint ID after selection. A request stays active through ASGI cleanup and is
finished exactly once on success, failure, cancellation or disconnect. HTTP
statuses >=400 and transport/application failures are `failed`; cancellation
and disconnection are separate states. This describes HTTP delivery, not the
semantic success of a model output or an SSE payload.

Metadata contains only method, matched route template, correlation ID, times,
status, state, byte counts and selected proxy target. No request/response content,
query strings, credentials, client addresses, actual document paths or raw
exception text is retained. Unmatched paths are redacted. `id` identifies arrival;
`completion_id` identifies completion order. Correlation `request_id` is nullable
for early CORS responses and may be reused by callers; it is not the log key.

Active requests and process totals reset on restart. Each row retains its original
`instance_id`; arrival `id` is scoped by that instance. Completion IDs continue
after the highest valid archived ID, so older-page cursors survive restarts in
the same log directory. `process_started_at` identifies the current process;
`observed_since` is the earliest restored request start, or current startup when
there is no history. These boundaries do not attest continuous uptime.

The default archive is **`logs/compute/requests-YYYY-MM-DD.jsonl`**. Configure
`request_log_directory` or `SUSI_COMPUTE_REQUEST_LOG_DIRECTORY` to change it.
Paths are relative to the working directory unless absolute. The directory must
be outside `data_path`, since the file service can expose that tree. Files are
UTF-8 JSON Lines with `sequence`, `timestamp` and safe request `data`. There is no
automatic archive deletion. `request_log_capacity`, default **10000** (1..100000),
bounds the recent RAM cache only; older records remain searchable on disk.
`retained_requests` includes valid archived and buffered rows, `cached_requests`
reports the cache, and `evicted_requests` is zero for the durable archive.

`shared/observability/daily_log.py` provides the reusable `BufferedDailyLog` class:

```python
from susi_brain.shared.observability.daily_log import BufferedDailyLog

with BufferedDailyLog("logs/example", prefix="events") as log:
    log.log({"event": "ready"})
    matches = log.search(query="ready", limit=50)
```

`log(data)` accepts a JSON object or string, copies it into RAM and returns a
monotonic sequence ID. Its background thread appends the pending batch **once
every 60 seconds**, using `O_APPEND`; it never rewrites an existing file.
Only the UTC date current at flush time is appended. A batch crossing midnight
therefore lands in the new day's file while its event timestamps stay unchanged.
`close()`, context exit, normal interpreter exit and application shutdown drain
the remainder immediately. An abrupt kill or crash can lose unflushed RAM data.
Reads and searches never force an early flush. The class also owns archive/RAM
search, exact dictionary filters, exclusions, time ranges and cursor pagination.

One writer owns each directory using a POSIX advisory lock; another writer fails
startup rather than mixing independent sequence counters. Separate server workers
need separate directories and have independent active counters. Startup streams
valid archived rows to reconstruct recent histogram buckets and the cache.
Malformed, oversized or incompatible rows are skipped; a truncated tail is kept
and separated from the next append. Failed writes retain the buffer; the worker
retries on its next minute tick and avoids duplicating complete rows from a partial
append. `log_status` exposes buffered rows, flush interval, last successful batch,
`append_failed` or null, and invalid archive rows skipped at startup.

GET requests also enter this log, including monitoring reads, after completion.
`exclude_monitoring=true` filters monitoring reads from active rows, log pages
and histogram totals without turning recording off.

The log accepts `limit` (1..500, default 100), exclusive completion cursor `before`,
exact uppercase `method`, `status_code` (100..599) and route-template `path` filters.
Add `q` (up to 256 characters) for a case-insensitive literal search of JSON request
metadata, `since` for an inclusive timestamp, and `until` for an exclusive timestamp.
Times must include a timezone, e.g. `2026-10-25T00:00:00Z`; a reversed or naive
time range returns 422. The web log offers the same text and UTC time filters.
Use `next_before` for an older page; null means no older matching retained rows.
Completion cursors remain stable when earlier requests finish late or new calls
arrive and remain valid across restart against the same archive. Search runs in
FastAPI's worker threads; recent cached pages need no archive reads.
Active results accept `limit` (1..1000, default 100), report filtered `active_count`
and `truncated`, while their `totals` always counts all traffic.

Histograms group **finished** requests by completion time, oldest first, with
`interval=minute|ten_minutes|hour|day` and optional `buckets=1..1440`.
Each resolution retains 1440 time buckets independently of raw-log eviction:
one day of minutes, ten days of ten-minute buckets, sixty days of hours and
1440 days of days. Startup reconstructs these windows from valid archive entries,
including those older than the recent cache. `requests` counts all outcomes;
`errors` and `cancelled` are disjoint subsets. `observed=false` marks time before
the earliest recorded request/startup boundary; `partial=true` marks a bucket
crossing that boundary or the current time. Earlier zero counts do not establish
observed idleness. Searches use bounded memory, never call providers and use no
additional runtime dependency.

## Text to speech with Ollama

`POST /v1/audio/speech` supports the WAV subset of the OpenAI speech API. Compute
forwards audio to an explicitly configured Ollama endpoint using the same HTTP
pool, endpoint-specific Bearer keys, TLS verification and load counters as chat.
The Compute proxy needs no additional synthesis library or resident speech model.
Ollama speech currently requires Apple Silicon; use Ollama 0.40.11 or later on
the target Mac. Compute can run separately on Linux or in Docker. Install a
speech model on that target before use; Compute never downloads one automatically.
See [Ollama speech](https://docs.ollama.com/capabilities/speech).

```json
{
  "compute": {
    "endpoints": ["127.0.0.1:11434"],
    "speech": [{"name": "phonon", "voice": "default"}],
    "speech_timeout_seconds": 120
  }
}
```

`speech` accepts up to 128 unique model-name strings (voice `default`) or
`{name, voice}` objects. It is separate from `smart`/`small`; the same model may
not appear in both chat and speech lists. Ollama names without tags match
`:latest`. Configure through JSON, `SUSI_COMPUTE_SPEECH` as a JSON array, or
`--speech phonon` for default voices. Lists default to empty. For German there is
`phonon:40m-de`; its 40M model license is noncommercial, unlike the Apache-2.0
English 31M model. See the [model catalog](https://ollama.com/library/phonon).

```sh
curl http://127.0.0.1:8042/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"phonon","input":"Hello from Compute.","voice":"default","response_format":"wav"}' \
  --output speech.wav
```

`model` and nonblank `input` (at most 4096 Unicode characters) are required.
`voice` defaults to the configured model voice. Only `response_format: "wav"`
(default), `speed: 1` and `stream_format: "audio"` (default) are accepted.
Unknown fields, `instructions`, SSE and other formats return 400. This is an
explicit WAV-only subset: its default is WAV, unlike OpenAI's MP3 default.

Each valid request refreshes `/models` and matching Ollama `/api/show` metadata.
TTS API bases must end in `/v1`; that suffix is replaced by `/api/show` while
preserving deployment prefixes. Only endpoints advertising `speech`, the model
and the requested voice are candidates. Voice matching ignores case/accents.
Select lowest **shared endpoint load**, then lowest calibrated real-time factor,
then first-audio latency for that exact model and voice. Missing metrics sort
last; final ties use endpoint order. There is no hard single-model lock or queue.
The historical `/api/v1/compute/chat/connections` endpoint includes both Chat/TTS
and calibration reservations. Counters remain local to one application process.

The response streams unmodified 24 kHz mono 16-bit PCM WAV bytes after header and
first-frame validation. Ollama's unknown-length `0xffffffff` size placeholders
are accepted. The limit is 32 MiB; `speech_timeout_seconds` defaults to 120 for
upstream operations/idle reads and first-audio waiting, not a total deadline.
Errors before audio use 502/503/504; unavailable model/voice returns 503 with
`Retry-After: 1`. Bounded upstream error statuses/bodies/Retry-After are retained.
Errors after headers terminate the stream and are recorded as failed; JSON is
never appended to audio. Cancellation/disconnection closes upstream and releases
the reservation. There is no automatic replay or alternate-model fallback.

`X-Compute-Target` exposes the selected `endpoint_id`, `model`, `voice` and
`expected: {ttfa_seconds, real_time_factor}`. Values are copied before synthesis;
unknown or different-voice measurements are null. Request logs store target and
timing metadata, never input text or generated audio.

The `[text to speech]` pane (`/api/v1/compute/ui#tts`) offers a text box, model and
voice selectors, explicit generation/cancel, audio playback and WAV download.
Voices come from calibration; the configured default is shown before measuring.
It displays expected and browser-measured first audio, real-time factor and
audio duration. Browser timing includes discovery/network; calibration timing
starts at the synthesis POST. The browser buffers at most 32 MiB and enables
playback after completion, fixes unknown WAV size fields in the completed Blob,
and releases the Blob URL/audio/fetch on navigation or page close. No autoplay,
browser persistence or inference on page loading.

## Endpoint calibration

Calibration runs explicitly through the API and keeps an internal dictionary of
results for every selected endpoint/model pair. Startup, ordinary chat and reads
do not run calibration. Default scope is the union of `smart`, `small` and `speech` models;
`scope: "all"` selects all models advertised by the selected endpoints. These are
real inference calls and can use a hosted provider's credits.

| Method | URL | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/compute/calibration` | Start a background run; returns 202 and a polling Location |
| `GET` | `/api/v1/compute/calibration` | Read endpoint IDs, progress and model results without triggering work |
| `DELETE` | `/api/v1/compute/calibration` | Cancel/drain the active run and retain partial results |

```sh
curl -X POST http://127.0.0.1:8042/api/v1/compute/calibration \
  -H 'Content-Type: application/json' -d '{"scope":"configured"}'
curl http://127.0.0.1:8042/api/v1/compute/calibration
```

POST also accepts optional `endpoint_ids` and `models` arrays. Endpoint IDs come
from GET; models are concrete names and must belong to `smart`/`small`/`speech` in
configured scope. Filters never introduce arbitrary target URLs or custom
prompts. Invalid selections return 422, a concurrent run 409, and an inactive
chat transport 503. There is no queue or durable retry deduplication.

The process first discovers models and checks basic chat availability. It then
tests three JSON-schema mood classifications (`forms`), recognition of the
packaged number-42 image (`vision`), and exposed reasoning fields or nonempty
think/thinking blocks (`thinking`). These probes follow Project Euler LLM
Benchmark's examples; see `src/susi_brain/shared/NOTICE.md` for fixture/parser
provenance. A false capability means this fixed probe did not demonstrate it;
thinking does not attest hidden model reasoning. Null means untested, skipped
or inconclusive, with the reason in the corresponding `checks` entry.

Only after all functional probes finish does the speed phase issue one streaming
request per available pair, asking for numbers 1 through 200 with a 256-output-token
ceiling. Availability/feature requests use 64/128/512-token ceilings. Probe calls
use the same endpoint-specific Bearer keys, TLS verification and HTTP pool as
chat, and never provision models or replay a failed generation.

The snapshot also exposes `model_groups.smart` and `model_groups.small` as
`{name, ranking}` lists, including before calibration; string entries have ranking 0.
`speech_models` exposes configured `{name, voice}` entries. Speech records have
`task: "speech"`, a measured `voice`, and `speech` metadata listing available
voices and the proxy's WAV sample format. Speech availability reads Ollama metadata
and checks the configured voice. A separate short synthesis must produce valid
nonempty PCM WAV before a longer speed sample is allowed. Chat-only feature
probes are skipped for speech models. In all scope, `/api/show` also recognizes
additional speech models; models/servers without Ollama speech metadata retain
chat probes. All Chat/TTS function probes finish before any speed sample.

Speech speed reports `ttfa_seconds` until the first complete PCM frame (a header
alone does not count), full `duration_seconds`, `audio_duration_seconds` derived
from actual PCM bytes, and `real_time_factor=duration_seconds/audio_duration_seconds`.
Lower RTF is faster: `0.2` means 2 seconds to produce 10 seconds of audio.
Audio probes have a 1 MiB response ceiling and the same configurable per-check
deadline as chat. These validate the transport/audio format, not pronunciation,
intelligibility or language quality. Each check reserves its endpoint/model for
its whole lifetime and releases on timeout/cancellation; forms reserves across its
sequential calls, and speech availability includes its metadata read.
No API key values or environment-variable names are returned.

The snapshot has `run.state`, `run.phase`, progress counters and the current
endpoint/model/check, plus `results[endpoint_id][model]` records containing
`listed`, `available`, `groups`, `capabilities`, `checks` and `metrics`.
All function checks finish before any speed sample starts. Each record's speed
metrics are:

- `ttft_seconds`: time from sending the speed request until its first nonempty
  content or reasoning delta; `first_token_kind` identifies which. Role-only,
  empty and keepalive events do not count.
- `tokens_per_second`: upstream-reported `completion_tokens` divided by the full
  streaming duration, including TTFT. Prompt tokens and transport chunk counts
  are excluded. Final usage-only frames are retained, following the
  [Chat Completions streaming contract](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events).
- `generation_tokens_per_second`: the transport approximation
  `(completion_tokens - 1) / (last_output_time - first_output_time)`. It is null
  for unknown token counts, a single output event, or reported hidden reasoning.
- `duration_seconds`, `generation_seconds`, `completion_tokens`,
  `reasoning_tokens`, `token_count_source` and `timing_source` expose the basis.
  Missing usage leaves token rates null while retaining a measured TTFT.

Measurements include network and current server load. A completed run means all
probes finished; individual checks may still report rejection, errors or missing
support. Truncated/invalid streams do not publish successful speed metrics.
Each check has a configurable deadline (default 60 seconds, covering all three
forms cases); discovery keeps its separate deadline. The default 1024-pair limit
fails excess runs before inference rather than silently omitting models.

Results stay in memory, are replaced when a new run starts, and disappear on
restart. Read responses use `Cache-Control: no-store`. Cancellation and shutdown
close upstream responses before releasing the shared HTTP pool. API keys,
probe prompts and raw model/reasoning outputs are omitted from the snapshot.

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
| `compute` | `openai_api_key` | Empty | Cloud audio only; also accepts `OPENAI_API_KEY` |
| `compute` | `susi_api_key` | Empty | Required for file mutations; also accepts `SUSI_API_KEY` |
| `compute` | `data_path` | Project `data/` for source starts | Explicit values are relative to the working directory or absolute |
| `compute` | `endpoints` | `[]` | Up to 32 unique `host:port` strings or `{url, api_key?, api_key_env?}` objects |
| `compute` | `smart` | `[]` | Up to 128 model names or `{name, ranking}` objects; highest ranking at equal endpoint load |
| `compute` | `small` | `[]` | Up to 128 model names or `{name, ranking}` objects; measured speed at equal endpoint load |
| `compute` | `speech` | `[]` | Up to 128 Ollama model names or `{name, voice}` objects; default voice `default` |
| `compute` | `model_probe_timeout_seconds` | `3` | Greater than 0, at most 30 |
| `compute` | `chat_timeout_seconds` | `120` | Greater than 0, at most 600 |
| `compute` | `speech_timeout_seconds` | `120` | Upstream operation/idle and first-audio timeout; greater than 0, at most 600 |
| `compute` | `calibration_timeout_seconds` | `60` | Deadline per availability/feature/speed check; greater than 0, at most 600 |
| `compute` | `calibration_max_pairs` | `1024` | Endpoint/model pairs per run; 1–16384 |
| `compute` | `request_log_capacity` | `10000` | Recent completed-request RAM cache; 1–100000, archives remain searchable |
| `compute` | `request_log_directory` | `logs/compute` | Daily JSONL archive; relative to working directory or absolute, outside `data_path` |

Environment names use `SUSI_SERVER_` and `SUSI_COMPUTE_` plus the uppercase field.
CLI options include `--config`, `--host`, `--port`, `--data-path`, and the original
`--openai_api_key`/`--susi_api_key` (also spelled with hyphens). Service-prefixed
environment variables override the original environment names; CLI overrides both.
Model lists also accept `--endpoints`, `--smart`, `--small` and `--speech`, each followed by
one or more values. `--endpoints` accepts the short string form; URL/key objects
use JSON configuration or `SUSI_COMPUTE_ENDPOINTS`. Environment list values are
JSON arrays, for example `SUSI_COMPUTE_SMALL='["your-small-model"]'` or
`SUSI_COMPUTE_ENDPOINTS='[{"url":"https://openrouter.ai/api/v1","api_key_env":"OPENROUTER_API_KEY"}]'`.

## Verification

```sh
python -m pytest
python scripts/export_compute_openapi.py --check
python -m build
```

The dependency-free browser stream, timing and audio-input checks additionally run with
`node --test test/test_chat_stream.mjs test/test_audio_input.mjs test/test_tts.mjs` when Node.js is available.

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
and `--file` to choose another model or recording. Chat needs a configured model
server and matching model group; `--chat-model smart|small` selects the alias
(default `small`). The file check needs `SUSI_API_KEY` or `--token`
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
  -v susi-data:/app/data -v susi-logs:/app/logs \
  -v susi-cache:/home/susi/.cache susi-compute
```

The Python 3.12 slim image installs `src/susi_brain/compute/requirements.txt`,
runs as an unprivileged user, and includes CPU-only Torch, Whisper, Vosk and FFmpeg.
Named volumes retain files and model weights. Models load/download on first
transcription; neither startup nor image creation downloads model weights.
