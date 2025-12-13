"""Compute's local Amber WebTUI interface and offline assets."""

import html

from fastapi import Request
from fastapi.responses import HTMLResponse, FileResponse

from susi_brain.shared.web.swagger import STATIC

PREFIX = '/api/v1/compute'
HEADERS = {
    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                             "font-src 'self'; img-src 'self' data:; media-src 'self' blob:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
}


def install_compute_web_routes(app):
    @app.get(PREFIX + '/ui', response_class=HTMLResponse, operation_id='compute__web_ui', tags=['Interface'])
    def interface(request: Request):
        """Local Amber WebTUI interface using version-pinned WebTUI 0.1.10 and IBM Plex Mono 2.5.0 served locally for single-prompt chat, speech to text, text to speech, model measurements, live connections, request monitoring and read-only configuration. Speech (#speech) accepts one dropped/chosen audio file up to 25 MiB or a microphone recording up to 3 minutes. Files upload immediately; microphone stop (also automatic at 3 minutes) submits the completed recording to POST /v1/audio/transcriptions as multipart file with model=tiny. Speech is transcribed in its original language; this is not live partial transcription. Recording uses a supported browser MIME/container, requires HTTPS or localhost and explicit microphone permission; page loading never requests access. Cancelling/navigation discards captured audio and stops tracks, including late permission grants; aborting an upload cannot stop already-running server inference. Audio/transcripts have no browser persistence. Horizontal top navigation groups Monitoring (Requests, Connections, Models), Services (Chat, Speech to text, Text to speech), Configuration (Startup configuration), and Documentation (Swagger UI, OpenAPI specification), without item numbers. Native disclosure menus support keyboard activation, Escape, and outside-click dismissal. The left context column shows service options, model/request filters or constraints; below 720px it becomes a collapsible section above the workspace. Ctrl/Command+Enter submits the active form, including from its sidebar options. Chat (#chat) has left-sidebar model smart/small (default small) and Thinking off/on (default off) controls; each explicit send streams POST /v1/chat/completions with only the current user message and requests usage. Thinking forwards reasoning_effort medium/none, reasoning.enabled and chat_template_kwargs.enable_thinking; actual support depends on the provider/model. No conversation history or browser persistence. Completion always displays expected selected-pair calibration and browser-measured throughput/TTFT; missing measurements/usage remain unavailable. Browser throughput uses upstream completion_tokens divided by request-to-stream-end elapsed time, including discovery/network and reasoning tokens; aborted/failed/truncated streams do not publish a successful rate. Navigating away stops the stream and discards the prompt/reply. Monitoring shows active API requests, a searchable completion log and UTC minute/ten-minute/hour/day histograms. Daily JSONL archives and reconstructed histograms survive restarts; text, exact and UTC time filters cover disk and buffered rows, and flush health is visible. Same-origin snapshots poll every five seconds while visible. Loading never starts inference or calibration. Text to speech (#tts) offers configured model/voice selectors in the left context column and a text box in the workspace, explicitly calls the WAV speech proxy, then enables playback/download and expected/browser first-audio/real-time-factor measurements after completion. It buffers at most 32 MiB, normalizes completed WAV sizes in a temporary Blob, and revokes the URL on navigation; no autoplay or persistent storage. Explicit calibration controls default to configured smart/small/speech models. Swagger is linked; all assets are local and root_path is honored."""
        prefix = html.escape(request.scope.get('root_path', '') + PREFIX, quote=True)
        body = (STATIC / 'index.html').read_text(encoding='utf-8')
        for key, value in {'__SERVICE__': 'Compute', '__OWNER__': 'compute', '__PREFIX__': prefix}.items():
            body = body.replace(key, value)
        return HTMLResponse(body, headers=HEADERS)

    @app.get(PREFIX + '/ui.css', response_class=FileResponse, operation_id='compute__web_style',
             tags=['Interface'], responses={200: {'content': {'text/css': {'schema': {'type': 'string'}}}}})
    def style():
        """Classless Amber WebTUI stylesheet with local @webtui/css 0.1.10 modules, IBM Plex Mono 2.5.0 fonts, semantic status colors, endpoint/model panels, horizontal grouped disclosure navigation and a left context column that collapses above the workspace below 720px. Styles and fonts load from the allowlisted ui-assets route; all theme colors are centralized in amber.css."""
        return FileResponse(STATIC / 'interface.css', media_type='text/css', headers=HEADERS)

    @app.get(PREFIX + '/ui.js', response_class=FileResponse, operation_id='compute__web_script',
             tags=['Interface'], responses={200: {'content': {'text/javascript': {'schema': {'type': 'string'}}}}})
    def script():
        """Packaged Compute interface script. Dynamic API values are rendered as text, never executable HTML. Polling reads local monitoring/calibration snapshots without contacting model servers; mutations are explicit and never automatically retried."""
        return FileResponse(STATIC / 'compute.js', media_type='text/javascript', headers=HEADERS)

    @app.get(PREFIX + '/chat.js', response_class=FileResponse, operation_id='compute__web_chat_script',
             tags=['Interface'], responses={200: {'content': {'text/javascript': {'schema': {'type': 'string'}}}}})
    def chat_script():
        """Packaged ES module for stateless chat, UTF-8/SSE decoding and browser timing. First/terminal modelname, compute_group, compute_hostname and request_bytes fields identify the actual selected model/host/group and original request size; older streams use X-Compute-Target/model fallback. Expected speed remains in the header; actual speed uses upstream usage. Allows 8 MiB plus 8 KiB annotation/framing overhead. Uses same-origin responses, renders text safely, requests usage and keeps no persistent history. Missing token counts are never estimated from chunks."""
        return FileResponse(STATIC / 'chat.js', media_type='text/javascript', headers=HEADERS)

    @app.get(PREFIX + '/audio.js', response_class=FileResponse, operation_id='compute__web_audio_script',
             tags=['Interface'], responses={200: {'content': {'text/javascript': {'schema': {'type': 'string'}}}}})
    def audio_script():
        """Packaged ES module for the speech-to-text drop zone, file picker and microphone recorder. Records with MediaRecorder, sends actual container MIME/filename as multipart to the existing transcription API, handles HTTP and legacy HTTP-200 errors, and releases microphone tracks on cancellation/navigation. Browser-only limits are 25 MiB and 3 minutes; the transcription API's limits are unchanged."""
        return FileResponse(STATIC / 'audio.js', media_type='text/javascript', headers=HEADERS)

    @app.get(PREFIX + '/tts.js', response_class=FileResponse, operation_id='compute__web_tts_script',
             tags=['Interface'], responses={200: {'content': {'text/javascript': {'schema': {'type': 'string'}}}}})
    def tts_script():
        """Packaged text-to-speech ES module. #tts selects configured speech model and default/calibrated voices in the left context column, explicitly POSTs /v1/audio/speech, buffers at most 32 MiB WAV and exposes playback/download after completion. Tracks browser first PCM/full-duration/real-time factor against exact model/voice calibration. Normalizes unknown WAV sizes only in the completed browser Blob for player compatibility. Abort/navigation/pagehide stop fetch, pause audio and revoke the Blob URL; no persistence, autoplay or generation on load. Input is limited to 4096 Unicode characters. Loading the page only reads local snapshots."""
        return FileResponse(STATIC / 'tts.js', media_type='text/javascript', headers=HEADERS)
