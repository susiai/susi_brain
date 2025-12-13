"""Exercise local interface delivery, privacy, deployment prefixes and API data."""

import os
import shutil
import subprocess
import types
from html.parser import HTMLParser

import httpx
import pytest
from fastapi.testclient import TestClient

from susi_brain.compute.config import ComputeSettings
from susi_brain.compute.main import create_compute_app

PREFIX = '/api/v1/compute'


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in os.environ:
        if name.startswith(('SUSI_COMPUTE_', 'SUSI_SERVER_')) or name in {'OPENAI_API_KEY', 'SUSI_API_KEY'}:
            monkeypatch.delenv(name)


class Assets(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.paths = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('src'):
            self.paths.append(attrs['src'])
        if tag == 'link' and attrs.get('rel') == 'stylesheet':
            self.paths.append(attrs['href'])


@pytest.mark.parametrize('root_path', ['', '/proxy'])
def test_interface_and_swagger_assets_are_local_and_prefix_aware(root_path):
    def unexpected(request):
        raise AssertionError('Loading the UI must never contact model servers.')
    app = create_compute_app(ComputeSettings(
        endpoints=[{'url': 'https://provider.example/api/v1', 'api_key': 'private-ui-key'}],
        smart=[{'name': 'quality-model', 'ranking': 90}], small=['fast-model'],
    ), chat_transport=httpx.MockTransport(unexpected))
    base = root_path + PREFIX
    with TestClient(app, root_path=root_path) as client:
        page = client.get(base + '/ui')
        assert page.status_code == 200 and 'text/html' in page.headers['content-type']
        assert 'data-prefix="' + base + '"' in page.text
        assert 'href="' + base + '/docs"' in page.text
        assert 'private-ui-key' not in page.text
        assert "script-src 'self';" in page.headers['content-security-policy']
        assert 'unsafe-inline' not in page.headers['content-security-policy']
        assert 'type="module"' in page.text
        assert page.headers['cache-control'] == 'no-store'
        paths = Assets(page.text).paths
        assert set(paths) == {base + '/ui.css', base + '/ui.js'}
        for path in [*paths, base + '/chat.js', base + '/audio.js', base + '/tts.js']:
            asset = client.get(path)
            assert asset.status_code == 200
            assert asset.headers['x-content-type-options'] == 'nosniff'
        docs = client.get(base + '/docs')
        assert 'href="' + base + '/ui"' in docs.text
        assert 'href="' + base + '/openapi.json"' in docs.text
        assert base + '/docs-assets/theme.css' in Assets(docs.text).paths
        for path in Assets(docs.text).paths:
            assert path.startswith(base + '/docs-assets/')
            assert client.get(path).status_code == 200
        assert 'ui-assets/amber.css' in client.get(base + '/docs-assets/theme.css').text
        assert 'data-webtui-theme="amber"' in page.text
        assert 'data-webtui-theme="amber"' in docs.text
        assert "font-src 'self'" in page.headers['content-security-policy']
        from susi_brain.shared.web.assets import PUBLIC_ASSETS
        for name, (_, media) in PUBLIC_ASSETS.items():
            asset = client.get(base + '/ui-assets/' + name)
            assert asset.status_code == 200 and asset.content
            assert asset.headers['content-type'].startswith(media)
        for name in ['missing.css', 'manifest.json', 'swagger-ui/swagger-ui-bundle.js',
                     '%2e%2e/config.py', '%2e%2e%2f%2e%2e%2fconfig.py']:
            assert client.get(base + '/ui-assets/' + name).status_code == 404
        snapshot = client.get(base + '/calibration').json()
        assert snapshot['run'] is None and snapshot['results'] == {}
        assert snapshot['model_groups'] == {
            'smart': [{'name': 'quality-model', 'ranking': 90}],
            'small': [{'name': 'fast-model', 'ranking': 0}],
        }
        assert 'private-ui-key' not in str(snapshot)
        assert client.get(base + '/chat/connections').json()['active_connections'] == 0


def test_interface_reports_expected_asset_types_and_documented_routes():
    app = create_compute_app(ComputeSettings())
    with TestClient(app) as client:
        for path, media in [('/ui', 'text/html'), ('/ui.css', 'text/css'), ('/ui.js', 'text/javascript'), ('/chat.js', 'text/javascript'), ('/audio.js', 'text/javascript'), ('/tts.js', 'text/javascript'),
                            ('/docs-assets/theme.css', 'text/css')]:
            response = client.get(PREFIX + path)
            assert response.headers['content-type'].startswith(media)
            assert response.content
            assert PREFIX + path in app.openapi()['paths']
            assert client.post(PREFIX + path).status_code == 405
        assert client.get(PREFIX + '/ui/unknown').status_code == 404
        assert client.get(PREFIX + '/docs-assets/not-packaged.js').status_code == 404
        assert client.get(PREFIX + '/calibration').json()['model_groups'] == {'smart': [], 'small': []}


def test_prefix_values_are_escaped_in_the_interface_html():
    with TestClient(create_compute_app(ComputeSettings()), root_path='/proxy\"<unsafe>') as client:
        page = client.get(PREFIX + '/ui')
        assert page.status_code == 200
        assert '/proxy&quot;&lt;unsafe&gt;' in page.text
        assert '<unsafe>' not in page.text


@pytest.mark.parametrize('root_path', ['', '/proxy'])
@pytest.mark.parametrize('container,codec,media', [('wav', 'pcm_s16le', 'audio/wav'),
    ('webm', 'libopus', 'audio/webm;codecs=opus'), ('m4a', 'aac', 'audio/mp4')])
def test_speech_files_and_browser_containers_use_the_existing_local_decoder(monkeypatch, tmp_path, root_path, container, codec, media):
    """Decode real audio containers while keeping model inference offline and mocked."""
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg is required for the local Whisper decoder.')
    from whisper import audio
    import whisper
    file = tmp_path / ('speech.' + container)
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:sample_rate=16000', '-t', '0.2', '-c:a', codec, str(file)], check=True)
    calls = []
    def transcribe(path):
        samples = audio.load_audio(path)
        assert len(samples) >= 3000 and abs(samples).max() > 0
        return {'text': 'Grüße aus dem Mikrofon. <b>Plain text</b>'}
    def model(name):
        calls.append(name)
        return types.SimpleNamespace(transcribe=transcribe)
    monkeypatch.setattr(whisper, 'load_model', model)
    with TestClient(create_compute_app(ComputeSettings()), root_path=root_path) as client:
        response = client.post(root_path + '/v1/audio/transcriptions',
            files={'file': (file.name, file.read_bytes(), media)}, data={'model': 'tiny'})
    assert response.status_code == 200
    assert response.json() == {'text': 'Grüße aus dem Mikrofon. <b>Plain text</b>'}
    assert calls == ['tiny']
