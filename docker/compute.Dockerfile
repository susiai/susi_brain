# docker build -f docker/compute.Dockerfile -t susi-compute .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    HOME=/home/susi

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates grep ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 susi \
    && useradd --uid 10001 --gid 10001 --create-home susi \
    && mkdir -p /app/data /app/logs /home/susi/.cache \
    && chown -R susi:susi /app/data /app/logs /home/susi

COPY src/susi_brain/compute/requirements.txt ./
# Install CPU-only Torch before Whisper, avoiding unnecessary CUDA libraries.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir --prefer-binary -r requirements.txt && pip check
COPY src/susi_brain/ ./src/susi_brain/
# Ship Swagger UI locally so interactive documentation also works offline.
RUN python -c "from pathlib import Path; assets = Path('src/susi_brain/shared/web/static/vendor/swagger-ui'); assert all((assets / name).is_file() for name in ('swagger-ui-bundle.js', 'swagger-ui.css', 'favicon-32x32.png'))"
# The Amber interface vendors WebTUI 0.1.10 and IBM Plex Mono 2.5.0; no Node runtime.
RUN python -c "import json; from susi_brain.shared.web.assets import PUBLIC_ASSETS, STATIC; assert all(path.is_file() for path, _ in PUBLIC_ASSETS.values()); assert [p['version'] for p in json.loads((STATIC / 'vendor/manifest.json').read_text())['packages']] == ['0.1.10', '2.5.0']"
RUN python -c "from susi_brain.shared.web.swagger import STATIC; assert all((STATIC / name).is_file() for name in ('index.html', 'interface.css', 'compute.js', 'chat.js', 'audio.js', 'tts.js', 'swagger.css'))"
# Request history and histogram aggregation use only the standard library.
RUN python -c "from susi_brain.compute.monitoring import RequestMonitor; m = RequestMonitor(10); r = m.begin({'method': 'GET', 'path': '/api/v1/compute/health/live', 'state': {}}); m.response_started(r, {'status': 200}); m.sent_body(r, {'body': b'ok'}); m.finish(r); assert m.activity()['totals']['finished'] == 1; assert m.histogram()['totals']['requests'] == 1"
# Daily append-only JSONL archives use POSIX file locks and no extra runtime libraries.
RUN python -c "import tempfile; from pathlib import Path; from susi_brain.shared.observability.daily_log import BufferedDailyLog; d = tempfile.TemporaryDirectory(); log = BufferedDailyLog(d.name); log.log('offline build check'); assert not list(Path(d.name).glob('*.jsonl')); log.close(); recovered = BufferedDailyLog(d.name); recovered.start(); assert recovered.search()['entries'][0]['data'] == 'offline build check'; recovered.close(); d.cleanup()"
# Configuration retains Pydantic; HTTP routes use dictionaries and no catalog.
RUN python -c "from susi_brain.compute.config import ComputeSettings; s = ComputeSettings(endpoints=['localhost:11434'], smart=['smart-model'], small=['small-model']); assert s.endpoints == ['localhost:11434'] and s.smart == ['smart-model'] and s.small == ['small-model']"
# Ranking and connection accounting reuse Pydantic and the standard library.
RUN python -c "from susi_brain.compute.config import ComputeSettings; from susi_brain.compute.routing import ChatRouting; s = ComputeSettings(endpoints=['localhost:11434'], smart=[{'name': 'ranked-model', 'ranking': 90}]); r = ChatRouting(s, s.chat_endpoints); e, m, c = r.select('smart', [['ranked-model']]); assert s.chat_models('smart')[0].ranking == 90 and r.count(e) == 1; c.release(); c.release(); assert r.snapshot()['active_connections'] == 0"
# Endpoint-specific Bearer secrets use existing Pydantic/HTTPX and verified TLS.
RUN python -c "import ssl; from susi_brain.compute.config import ComputeSettings; s = ComputeSettings(endpoints=[{'url': 'https://openrouter.ai/api/v1', 'api_key': 'build-check-key'}]); e = s.chat_endpoints[0]; assert e.url + '/models' == 'https://openrouter.ai/api/v1/models'; assert e.authorization_headers() == {'Authorization': 'Bearer build-check-key'}; assert 'build-check-key' not in s.model_dump_json(); assert ssl.create_default_context().verify_mode == ssl.CERT_REQUIRED"
# Calibration ships its vision fixture and SSE parser without extra runtime libraries.
RUN python -c "from susi_brain.compute.calibration import VISION_IMAGE; from susi_brain.shared.http.sse import SSEDecoder; assert VISION_IMAGE.read_bytes().startswith(b'\\x89PNG'); assert list(SSEDecoder().feed(b'data: [DONE]')) == ['[DONE]']"
# TTS only proxies to Ollama; PCM WAV parsing uses stdlib and loads no synthesis model.
RUN python -c "import io, wave; from susi_brain.compute.wav import wav_header, wav_duration; from susi_brain.compute.config import ComputeSettings; b = io.BytesIO(); w = wave.open(b, 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(bytes(480)); w.close(); assert wav_duration(wav_header(b.getvalue()), len(b.getvalue())) == .01; assert ComputeSettings(speech=['phonon']).speech_models[0].voice == 'default'"
# The shared server must also work without importing Compute or ML backends.
RUN python -c "import sys; from susi_brain.shared.config.settings import AppSettings; from susi_brain.shared.http.application import create_scaffold_app; from susi_brain.shared.http.runtime import ApplicationRuntime; app = create_scaffold_app('vault', AppSettings(), ApplicationRuntime([], 2)); assert '/api/v1/vault/health/ready' in app.openapi()['paths']; assert not any(name.startswith('susi_brain.compute') for name in sys.modules); assert not {'flask', 'psutil', 'torch', 'whisper', 'vosk'} & sys.modules.keys()"
RUN python -c "import sys; from susi_brain.compute.main import create_compute_app; app = create_compute_app(); assert {'/api/v1/compute/health/ready', '/v1/audio/speech', '/api/v1/compute/tts.js'} <= app.openapi()['paths'].keys(); assert not {'flask', 'flask_restx', 'flask_httpauth', 'a2wsgi', 'torch', 'whisper', 'vosk'} & sys.modules.keys()"
# Chat annotates first/terminal JSON events with stdlib byte framing/regex/JSON.
RUN python -c "from susi_brain.compute.stream_metadata import StreamMetadata; m = StreamMetadata({'modelname': 'offline-check', 'compute_group': 'small', 'compute_hostname': 'local', 'request_bytes': 42}); p = b'data: {}' + bytes([10, 10]) + b'data: [DONE]' + bytes([10, 10]); result = b''.join(m.feed(p)); assert b'\"modelname\": \"offline-check\"' in result and m.done"
# Chat/TTS discovery and JSON/SSE/WAV forwarding use HTTPX; requests remains for cloud transcription.
RUN python -c "import multipart, werkzeug, requests, requests_toolbelt, httpx, psutil, whisper, vosk; c = httpx.AsyncClient(trust_env=False, follow_redirects=False); assert not c.is_closed" && ffmpeg -version >/dev/null

USER 10001:10001
EXPOSE 8042
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8042/api/v1/compute/health/ready', timeout=2).close()"]
CMD ["python", "-m", "susi_brain", "compute", "--host", "0.0.0.0"]
