# run this i.e. with
# docker build -f docker/compute.Dockerfile -t susi_api .
# docker run -d -p 8080:8080 --name susi_api susi_api
# docker run -d -p 8080:8080 -e OPENAI_API_KEY=<apikey> --name susi_api susi_api
FROM python:3.12-slim
ENV DEBIAN_FRONTEND noninteractive
ARG default_branch=master

COPY src/susi_brain/compute/requirements.txt /app/
COPY src/ ./app/src/

WORKDIR /app
RUN \
    apt-get update && apt-get install -y ca-certificates bash ffmpeg net-tools sysstat procps && \
    export PYTHONHTTPSVERIFY=0 && \
    python3.12 -m pip install --upgrade pip && \
    python3.12 -m pip install --trusted-host=pypi.python.org --trusted-host=pypi.org --trusted-host=files.pythonhosted.org --no-cache-dir -r requirements.txt && \
    python3.12 -W ignore -c "import whisper; whisper.load_model('tiny'); whisper.load_model('base')" && \
    rm -rf /tmp/* /var/tmp/* /var/cache/apk/* /var/cache/distfiles/*

EXPOSE 8080
CMD ["python3.12", "src/susi_brain/compute/main.py"]