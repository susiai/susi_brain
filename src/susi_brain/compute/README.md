# SUSI Compute

Flask API for audio transcription, an OpenAI chat proxy, file storage/search, and system status.

Requires Python 3.12 and FFmpeg. On macOS: `brew install python@3.12 ffmpeg`.

Run from the repository root:

```bash
pip3.12 install --break-system-packages -r src/susi_brain/compute/requirements.txt
python3.12 src/susi_brain/compute/main.py
```

API documentation: http://localhost:8080/api/docs/. Stop with Ctrl+C.

Check readiness in another terminal:

```bash
curl -f http://localhost:8080/api/system/ready.json
```

Expected response: `{"health":"ok"}`.

Set `OPENAI_API_KEY` for chat and cloud transcription. Local Whisper needs no key and downloads its model on first use. Files are stored in the repository's `data/` directory.

Alternatively, run with Docker from the repository root:

```bash
docker build -f docker/compute.Dockerfile -t susi_api .
docker run --rm -p 127.0.0.1:8080:8080 -v "$PWD/data:/app/data" susi_api
```
