# SUSI Compute

Run `python3.12 src/susi_brain/compute/main.py` from the repository root after
installing the runtime dependencies. Installed alternatives are
`python -m susi_brain compute` and `susi-compute`. The default listener is
`127.0.0.1:8042`.

`audio_service.py` and `share_service.py` keep their existing processing logic.
`text_service.py` implements a configurable chat proxy with asynchronous HTTPX.
Their HTTP inputs and responses use FastAPI directly. `api.py`
registers chat and audio at the standard OpenAI paths `/v1/chat/completions` and
`/v1/audio/transcriptions`. Other service routes use `/api/v1/compute/`.
The OpenAI client base URL is `http://127.0.0.1:8042/v1`.
Legacy API and documentation aliases return 404. Blocking audio/file operations run in FastAPI's worker threads.
There is no Flask application or WSGI bridge. File requests retain path checks,
Bearer authorization, conditional downloads and byte ranges.

Swagger UI at `/api/v1/compute/docs` supports **Try it out** with local assets.
Use **Authorize** and enter `Bearer YOUR_API_KEY` for uploads or cloud audio.
The reusable installer is `shared/web/swagger.py`; all UI assets live in
`shared/web/static/vendor/swagger-ui/` and are shared by the applications.
It documents the original request defaults, payloads and response status codes.
The specification is `src/susi_brain/compute/swagger.yaml`.

The local interface is `/api/v1/compute/ui`, with its packaged `ui.css` and
`ui.js` and its local `chat.js`/`audio.js` ES modules. It uses the classless Amber WebTUI shell and responsive navigation. The
version-pinned @webtui/css 0.1.10 modules and IBM Plex Mono 2.5.0 fonts are
packaged offline, with an allowlisted `/api/v1/compute/ui-assets/{asset_path}`
route and licenses. All theme colors are in `shared/web/static/amber.css`.
The top navigation groups Monitoring, Services, Configuration and Documentation
with subordinate menu items and no numbering. The left context column holds
service controls, model/request filters or constraints. Below 720px it becomes
a collapsible section above the workspace. Ctrl/Command+Enter submits the active
form, including from its sidebar options. The models pane groups entries by endpoint, inspired by the sibling
Ollama dashboard, and shows rankings, tri-state capabilities, TTFT, throughput,
live request counts and expandable probe details. Search, group/speed filters
and batches of 25 entries support larger inventories. Monitoring, connections
and read-only configuration have separate panes. Swagger uses the matching
theme and links back to the interface.

The chat pane (`/api/v1/compute/ui#chat`) has Model smart/small and Thinking off/on
settings in the left context column, defaulting to small/off. Sends contain only the
current user prompt; each new send replaces the reply without keeping history
or browser storage. Stop aborts the stream; navigating away discards the text.
Thinking passes through reasoning_effort medium/none, reasoning.enabled and
chat_template_kwargs.enable_thinking, with provider/model-dependent support.
After each stream, expected selected-pair calibration is compared with browser
TTFT and throughput. `X-Compute-Target` carries ASCII JSON containing endpoint_id,
model and expected tokens_per_second/ttft_seconds frozen at reservation time.
It accompanies forwarded JSON/SSE responses and is exposed through CORS; absent
metrics are null and proxy-generated errors omit the header. Browser throughput
uses final upstream completion_tokens divided by full request-to-stream-end
time (including discovery/network/reasoning); no usage means unavailable.
Stopped/failed/truncated responses have no completed token rate. Streams and
thinking are parsed incrementally and rendered as plain text. See the repository
README for timing formulas, compatibility references and limitations.

Speech to text (`/api/v1/compute/ui#speech`) transcribes one dropped/chosen audio
file immediately, or a microphone recording after stop. The microphone icon
requests access only on click; recordings automatically stop and submit at
three minutes. Files are limited to 25 MiB in this interface. Both paths send
multipart file/model=tiny to the existing local Whisper API and preserve the
spoken language. Browser recordings keep their real supported MIME/container
and filename (for example WebM or M4A); FFmpeg decodes them on the server.
HTTPS or localhost and a MediaRecorder-compatible browser are needed for the
microphone. Cancel/navigation stops tracks and discards recordings, including
late permission grants. Upload cancellation only aborts the browser request;
already-running server inference can continue. The frontend keeps no persistent
audio/transcript history. These are completed-clip transcriptions, not live
partial audio-stream results. API behavior and limits are unchanged.

The browser polls only existing Compute snapshots every five seconds while
visible, with bounded requests and stale-data messages. It never contacts model
servers directly or starts tests merely by loading the page. Explicit start/cancel
controls use the calibration API; configured smart/small/speech is the default scope.
Starts replace prior results and may consume provider credits. Mutations are
not automatically retried. `model_groups` in calibration snapshots exposes
configured names/rankings for read-only display, without credential values.
All routes/assets honor reverse-proxy root_path; there is no frontend build or CDN.

The monitoring pane additionally reads `/api/v1/compute/monitoring/active`,
`/monitoring/requests` and `/monitoring/histogram` while visible. It shows all
current HTTP API requests, a paginated completion log with method/status filters,
and a native SVG histogram with an accessible value table. Select minute,
ten-minute, hour or day buckets; hide monitoring API reads when desired. The
ASGI middleware tracks `/api` and `/v1` traffic, including validation errors,
CORS preflights, documentation and complete streaming/cleanup lifetimes.

Log entries contain safe route templates, correlation IDs, HTTP status,
monotonic duration, actual body byte counts and selected chat target metadata.
No bodies, queries, credentials, actual document paths or raw exceptions are
stored. Failed, cancelled and disconnected requests remain distinguishable.
The reusable `shared/observability/daily_log.py` class `BufferedDailyLog` owns
buffering, daily append-only files, search and pagination. A `log(data)` call
queues a JSON object/string in RAM. Its own thread appends every 60 seconds to
the current UTC day's `requests-YYYY-MM-DD.jsonl`; it never rewrites the file.
Shutdown/context/normal interpreter exit flushes remaining rows. Abrupt crashes
can lose the unflushed buffer. Pending events crossing midnight go into the file
for the flush day with their original timestamps. Failed appends retain the buffer
for retry; recovery avoids duplicating complete rows already written.

`request_log_directory` defaults to `logs/compute`, outside the public file-service
`data_path`; an environment override is `SUSI_COMPUTE_REQUEST_LOG_DIRECTORY`.
`request_log_capacity` defaults to 10000 (1..100000) and bounds only the recent RAM
cache. No archive files are automatically deleted. Search covers archives and
pending rows with exact filters, literal `q`, timezone-aware inclusive `since`,
exclusive `until` and stable completion-ID cursors. The web log exposes text/time
filters and buffered-row/flush/error status. Invalid archive lines are skipped;
`log_status.ignored_lines` reports startup skips. Directory ownership uses a POSIX
file lock; separate workers require separate log directories.

Active rows/process totals reset on restart. Completed rows retain their original
instance IDs; completion IDs continue from disk. Startup rebuilds the cache and
four histogram resolutions from archive records using bounded memory. Each
resolution retains at most 1440 UTC time buckets. Coverage, partial/unobserved
buckets and process start times are explicit; coverage does not establish uptime.
See the repository README and Swagger for full filtering/retention contracts.

Generate chat completions with `POST /v1/chat/completions`, required `model`
(`smart` or `small`) and a nonempty OpenAI-style `messages` array. Configure
`endpoints` as `host:port` strings or `{url, api_key?, api_key_env?}` objects,
and `smart`/`small` as lists of model names or `{name, ranking}` objects. Ranking
is a finite operator-assigned number; strings and omitted rankings default to 0.
Short endpoint strings use
`http://host:port/v1`; objects set an HTTP(S) API base with its prefix, for example
`https://openrouter.ai/api/v1`. A trailing `/chat/completions` is removed.
`api_key_env` resolves an environment variable when configuration loads; missing
or empty referenced variables fail startup. `api_key` accepts a literal instead.
Keys are hidden from settings repr/JSON exports. Each provider's Bearer key is
sent only to that endpoint, for both `/models` and `/chat/completions` requests.
HTTPS certificates are verified; URL-embedded credentials and redirects are rejected.
Selection first reuses an active model in the requested group on each endpoint,
then chooses the endpoint with the fewest active proxy requests across all models.
At equal load, `smart` uses the highest ranking, then the fastest candidate;
`small` uses the fastest regardless of ranking. Speed compares calibrated
`tokens_per_second` descending, then `ttft_seconds` ascending; missing values
sort last for each metric. Remaining ties use model-list then endpoint order.
A busy endpoint can use another model if none of its active models belongs to
the requested group; the single-model preference is not a lock or a waiting queue.
The current calibration dictionary supplies endpoint-specific speed values.
Starting a new run clears old measurements; new samples take effect as completed.
Names match case-insensitively and the advertised spelling is sent upstream.
Only `model` is replaced; other options, including streaming, pass through with
backend defaults. Upstream status, Content-Type, Retry-After and non-stream/error bodies are retained.
Caller credentials/cookies and the global OpenAI key are not forwarded to chat servers.
No match returns 503, invalid input 400, upstream failures 502 and timeouts 504.
The proxy never provisions models or automatically retries generation. Chat
streaming follows `triton_llmlb`: first and first terminal JSON data objects gain
`modelname`, `compute_group`, `compute_hostname`, `request_bytes`. These are the
selected concrete model, smart/small group, endpoint hostname (no port/path) and
original incoming UTF-8 body size before model rewriting. Existing values of
these four keys are overridden; the standard `model` field is retained. The
first object may itself be terminal and is then annotated once. A byte regex
shortlists terminal events with string `finish_reason`; choices must contain a
non-null finish reason. Only first/terminal candidates are parsed and rewritten
as UTF-8 `data: ...` with LF/LF framing. Middle content/thinking/tool/usage lines
stay unchanged; no synthetic usage or response harmonization. Byte lines are
assembled across network chunks without decoding middle JSON. Invalid/nonobject
data pass through; an invalid first data line consumes the initial annotation
attempt, and multiline JSON is not joined for annotation. `[DONE]` closes and
releases upstream without waiting for EOF, including a marker without a final
newline. Already received blank framing is retained. HTTPX handles content
encoding. The frontend and calibration parse their own streams separately. A failure after headers
terminates the stream without appending a proxy error or `[DONE]` frame, is
recorded as failed, and releases upstream/reservations. Discovery defaults
to 3 seconds/1 MiB per server; chat to a 120-second operation/idle timeout and
8 MiB upstream body limit. The Chat UI allows 8 KiB extra for annotation/framing
overhead, displays model/host/group/request size from the stream, and retains
header-based expected calibration. The HTTPX pool starts/stops with the application.

`GET /api/v1/compute/chat/connections` reports total `active_connections` plus
an `endpoints` array of `id`, `url`, `active_connections`, and `models` (active
concrete model name to count). IDs match calibration. Selection and reservation
are atomic within this process, before connecting to the upstream. JSON requests
release after upstream cleanup; SSE requests retain their reservation until
`[DONE]` closes upstream, or cleanup on EOF, failure, cancellation or disconnect.
Counters are shared by Chat, TTS and calibration checks. They start at zero, include waiting-for-headers requests and full WAV streams, exclude inventories,
ordinary metadata discovery and idle sockets, and are not shared between worker processes.
Reads trigger no upstream work and expose no keys.

Endpoint calibration is explicitly controlled through
`POST/GET/DELETE /api/v1/compute/calibration`. POST defaults to models from the
smart/small/speech union and accepts `scope: "all"`, configured `endpoint_ids`, and
concrete `models` filters. It returns 202 immediately; GET exposes the run's
progress and `results[endpoint_id][model]` dictionary without triggering work.
DELETE and shutdown cancel/drain active work. A second active run returns 409.

Availability, three JSON-schema mood cases (`forms`), the packaged 42 image
(`vision`), and exposed reasoning/think blocks (`thinking`) are checked first.
Only after every model's functional checks finish does a streaming speed sample
measure TTFT and reported completion-token throughput. `tokens_per_second`
includes the full request duration; `generation_tokens_per_second` additionally
reports the first-to-last-output transport approximation. Usage-only final
frames count; role/empty chunks do not start TTFT, and chunks are never tokens.
Missing usage keeps rates null. Hidden reasoning is not attested by these probes.

Capabilities are true/false/null with per-check states, safe error codes and
timestamps. False means support was not demonstrated by the fixed probe; null
means untested/skipped/inconclusive. Results omit keys and raw prompts/outputs,
stay in memory, and are replaced by the next run or cleared by restart. No
inference is triggered at startup. Explicit runs can incur provider charges.
`calibration_timeout_seconds` defaults to 60 per check and
`calibration_max_pairs` to 1024 per run. See the repository README for formulas,
filters and curl examples, and `shared/NOTICE.md` for probe fixture provenance.

Generate speech with `POST /v1/audio/speech` using required `model` and
`input`, optional `voice` (configured default), `response_format: "wav"`,
`speed: 1` and `stream_format: "audio"`. This first version accepts only WAV,
24 kHz mono 16-bit PCM, up to 4096 input characters and 32 MiB response audio.
Configure a separate `speech` list of names or `{name, voice}` objects. Missing
tags match Ollama's `:latest`. The same model cannot belong to both chat and speech.
Ollama speech runs on Apple Silicon (use 0.40.11 or later); Compute can proxy it
from Linux/Docker without another synthesis dependency. Models must be installed
on the target already. Speech bases end in `/v1`; `/api/show` replaces that suffix
and preserves deployment prefixes. Its speech capability and voice catalog are
checked alongside `/models`. Endpoint keys/TLS are shared with chat.

TTS chooses lowest shared endpoint load, then lowest calibrated real-time factor
and first-audio latency for that model/voice. No hard single-model lock, automatic
retry, format conversion or alternate-model fallback. `speech_timeout_seconds`
defaults to 120 per operation/idle read and first-audio wait. Invalid input/options
return 400, unavailable model/voice 503, transport/invalid audio 502, timeout 504.
Upstream error statuses/bodies/Retry-After are retained within 1 MiB. Midstream
failures terminate WAV and release upstream; JSON is never appended to audio.
`X-Compute-Target` carries model/voice/endpoint and expected TTFA/real-time factor.

Calibration exposes `speech_models`, speech task records, voices and WAV format.
A short functional synthesis must yield valid audio before the separate longer
speed sample. All Chat/TTS feature checks finish before any speed check. TTFA
ends at the first PCM frame, ignoring headers. Audio duration comes from actual
PCM bytes; `real_time_factor=full_request_seconds/audio_seconds`, lower is faster.
The probes check format/availability, not intelligibility or pronunciation.

The `[text to speech]` pane at `/api/v1/compute/ui#tts` has model/voice controls in the left context column and text input in the workspace,
generate/cancel, a player and WAV download. It compares expected and measured
TTFA/real-time factor. Playback starts after the complete bounded WAV download;
temporary Blob sizes are normalized for browser players. Navigation/pagehide
aborts fetch, pauses playback and revokes the Blob URL. No persistent history or
autoplay. Voice choices come from calibration or the configured default.

Transcribe audio with `POST /v1/audio/transcriptions` using the
OpenAI-style multipart fields `file` and `model`. The local default is `tiny`;
local transcription returns `{"text":"..."}`.

Set `OPENAI_API_KEY` for cloud audio and `SUSI_API_KEY` for file mutations.
The original `--openai_api_key` and `--susi_api_key` flags still work. File reads
and search remain public. Files default to the project's `data/` directory;
change this with `--data-path`. Local Whisper requires FFmpeg. Without an OpenAI
key the original audio handler always selects `tiny`. Configure chat lists in
the JSON compute group, through JSON-array environment values
(`SUSI_COMPUTE_ENDPOINTS`, `SUSI_COMPUTE_SMART`, `SUSI_COMPUTE_SMALL`) or through
`--endpoints`, `--smart` and `--small` CLI lists. Defaults are empty lists.
Use JSON configuration or `SUSI_COMPUTE_ENDPOINTS` for URL/key objects, and JSON
configuration or `SUSI_COMPUTE_SMART`/`SUSI_COMPUTE_SMALL` for model rankings. See
`config/compute-openrouter.example.json`: set `OPENROUTER_API_KEY`, replace its
model placeholders with provider IDs, then start with `--config` pointing there.

The unchanged processing logic retains two existing defects: DELETE references an undefined
`full_path`, and Vosk treats JSON strings as dictionaries. These paths return
HTTP 500; their correction requires a separate service change.

Configuration, resource lifecycle, native errors and observability use `shared/`.
The generic HTTP factory, correlation and health/status routes live in
`shared/http/application.py`; problem responses live in `shared/http/responses.py`.
`main.py` supplies Compute's description, stage and capabilities. `system_service.py`
keeps host metrics and the Compute system routes.
`create_compute_app(settings=None, resources=None, chat_transport=None)` creates no storage directory,
downloads no models and makes no network calls. Whisper and Vosk libraries are imported on demand
by the existing transcription branches. Injected resources start and stop with the server.
Application configuration uses Pydantic; there are no additional request/response
model classes, contract layer or catalog endpoint. Existing audio/file HTTP 200
errors are retained; invalid FastAPI inputs return 422 and native HTTP failures
use the shared problem responses.

See the [repository README](../../../README.md) for installation, configuration,
verification and Docker.
