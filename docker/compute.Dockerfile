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
    && mkdir -p /app/data /home/susi/.cache \
    && chown -R susi:susi /app/data /home/susi

COPY src/susi_brain/compute/requirements.txt ./
# Install CPU-only Torch before Whisper, avoiding unnecessary CUDA libraries.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir --prefer-binary -r requirements.txt && pip check
COPY src/susi_brain/ ./src/susi_brain/
# Ship Swagger UI locally so interactive documentation also works offline.
RUN python -c "from pathlib import Path; assets = Path('src/susi_brain/shared/web/static/vendor/swagger-ui'); assert all((assets / name).is_file() for name in ('swagger-ui-bundle.js', 'swagger-ui.css', 'favicon-32x32.png'))"
# Configuration retains Pydantic; HTTP routes use dictionaries and no catalog.
RUN python -c "from susi_brain.compute.config import ComputeSettings; assert ComputeSettings().readiness_timeout_seconds == 2"
# The shared server must also work without importing Compute or ML backends.
RUN python -c "import sys; from susi_brain.shared.config.settings import AppSettings; from susi_brain.shared.http.application import create_scaffold_app; from susi_brain.shared.http.runtime import ApplicationRuntime; app = create_scaffold_app('vault', AppSettings(), ApplicationRuntime([], 2)); assert '/api/v1/vault/health/ready' in app.openapi()['paths']; assert not any(name.startswith('susi_brain.compute') for name in sys.modules); assert not {'flask', 'psutil', 'torch', 'whisper', 'vosk'} & sys.modules.keys()"
RUN python -c "import sys; from susi_brain.compute.main import create_compute_app; app = create_compute_app(); assert '/api/v1/compute/health/ready' in app.openapi()['paths']; assert not {'flask', 'flask_restx', 'flask_httpauth', 'a2wsgi', 'torch', 'whisper', 'vosk'} & sys.modules.keys()"
RUN python -c "import multipart, werkzeug, requests, requests_toolbelt, psutil, whisper, vosk" && ffmpeg -version >/dev/null

USER 10001:10001
EXPOSE 8042
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8042/api/v1/compute/health/ready', timeout=2).close()"]
CMD ["python", "-m", "susi_brain", "compute", "--host", "0.0.0.0"]
