/* File and microphone inputs share the existing multipart transcription API. */
'use strict';

export const MAX_AUDIO_BYTES = 25 * 1024 * 1024;
export const MAX_RECORDING_SECONDS = 180;
const EXTENSIONS = /\.(wav|wave|mp3|mpga|mpeg|mp4|m4a|ogg|oga|flac|webm|aac|aiff|aif|opus)$/i;

export function audioFileError(files) {
  if (files.length !== 1) return 'Choose one audio file at a time.';
  const file = files[0];
  if (!file.size) return 'The audio file is empty.';
  if (file.size > MAX_AUDIO_BYTES) return 'The audio file exceeds 25 MiB.';
  if (!file.type?.startsWith('audio/') && !EXTENSIONS.test(file.name || '')) return 'Choose an audio file, such as WAV, MP3, M4A, OGG, FLAC or WebM.';
  return null;
}

export function recordingName(type) {
  const extension = {'audio/webm': 'webm', 'audio/ogg': 'ogg', 'audio/mp4': 'm4a',
    'audio/wav': 'wav', 'audio/x-wav': 'wav', 'audio/aac': 'aac'}[type.split(';')[0].toLowerCase()];
  return 'microphone.' + (extension || 'bin');
}

export function microphoneError(error) {
  if (error?.name === 'NotAllowedError' || error?.name === 'SecurityError') return 'Microphone permission was denied. Allow access or choose an audio file.';
  if (error?.name === 'NotFoundError') return 'No microphone was found. Connect one or choose an audio file.';
  if (error?.name === 'NotReadableError') return 'The microphone is unavailable or in use by another application.';
  return 'Microphone recording failed. Try again or choose an audio file.';
}

export class MicrophoneCapture {
  constructor({mediaDevices = globalThis.navigator?.mediaDevices, Recorder = globalThis.MediaRecorder,
    onState = () => {}, onAudio = () => {}, onError = () => {}} = {}) {
    Object.assign(this, {mediaDevices, Recorder, onState, onAudio, onError});
    this.state = 'idle'; this.version = 0; this.session = null; this.disposed = false;
  }
  get supported() { return Boolean(this.mediaDevices?.getUserMedia && this.Recorder); }
  setState(state) { this.state = state; if (!this.disposed) this.onState(state); }
  release(session) {
    clearTimeout(session.timer);
    session.stream.getTracks().forEach(track => track.stop());
  }
  async start() {
    if (this.disposed || this.state !== 'idle') return;
    if (!this.supported) { this.onError('Microphone recording requires a supported browser and HTTPS or localhost.'); return; }
    const version = ++this.version;
    this.setState('requesting');
    let stream;
    try {
      stream = await this.mediaDevices.getUserMedia({audio: true, video: false});
      if (this.disposed || version !== this.version) { stream.getTracks().forEach(track => track.stop()); return; }
      const mimeType = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4', 'audio/wav']
        .find(type => this.Recorder.isTypeSupported?.(type));
      const recorder = new this.Recorder(stream, mimeType ? {mimeType} : undefined);
      const session = {recorder, stream, chunks: [], size: 0, error: null, version};
      this.session = session;
      recorder.addEventListener('dataavailable', event => {
        if (version !== this.version || !event.data?.size) return;
        session.size += event.data.size;
        if (session.size > MAX_AUDIO_BYTES) {
          session.error = 'The recording exceeds 25 MiB. Record a shorter clip.';
          session.chunks = []; this.stop();
        } else session.chunks.push(event.data);
      });
      recorder.addEventListener('error', () => {
        session.error = 'Microphone recording failed. Try again or choose an audio file.';
        session.chunks = [];
        this.release(session);
        if (version === this.version) { this.session = null; ++this.version; this.setState('idle'); this.onError(session.error); }
      });
      recorder.addEventListener('stop', () => {
        this.release(session);
        if (this.disposed || version !== this.version) { session.chunks = []; return; }
        this.session = null;
        this.setState('idle');
        const type = recorder.mimeType || session.chunks.find(chunk => chunk.type)?.type || 'application/octet-stream';
        const audio = new Blob(session.chunks, {type});
        session.chunks = [];
        if (session.error || !audio.size) this.onError(session.error || 'No audio was recorded. Record a longer clip.');
        else this.onAudio(audio, recordingName(type));
      });
      recorder.start(1000);
      session.timer = setTimeout(() => this.stop(), MAX_RECORDING_SECONDS * 1000);
      this.setState('recording');
    } catch (error) {
      if (stream) stream.getTracks().forEach(track => track.stop());
      if (!this.disposed && version === this.version) {
        this.session = null; this.setState('idle'); this.onError(microphoneError(error));
      }
    }
  }
  stop() {
    const session = this.session;
    if (!session || this.state !== 'recording') return;
    this.setState('stopping');
    clearTimeout(session.timer);
    try { if (session.recorder.state !== 'inactive') session.recorder.stop(); }
    catch { this.cancel(); this.onError('The recording could not be completed. Please try again.'); }
  }
  cancel() {
    ++this.version;
    const session = this.session;
    this.session = null;
    if (session) {
      session.chunks = [];
      this.release(session);
      try { if (session.recorder.state !== 'inactive') session.recorder.stop(); } catch { /* Tracks are already stopped. */ }
    }
    this.setState('idle');
  }
  dispose() { this.disposed = true; this.cancel(); }
}

export function mountSpeech({panel, root, node, captureOptions = {}}) {
  let disposed = false, controller = null, timer = null, recordingStarted = 0, dragDepth = 0;
  const view = node('div', undefined, {'data-ui': 'speech-view'});
  view.append(node('p', 'Drop an audio file to transcribe it immediately, or record with your microphone.'));
  const drop = node('section', undefined, {'data-ui': 'audio-drop', 'aria-label': 'Audio drop zone'});
  const fileLabel = node('label', 'Drop one audio file here, or choose a file');
  const input = node('input', undefined, {type: 'file', accept: 'audio/*,.wav,.mp3,.m4a,.mp4,.ogg,.flac,.webm,.aac,.aiff,.opus',
    'aria-label': 'Audio file', 'aria-describedby': 'audio-file-hint'});
  fileLabel.append(input);
  drop.append(fileLabel, node('p', 'WAV, MP3, M4A, OGG, FLAC, WebM and other common audio formats · up to 25 MiB.', {id: 'audio-file-hint'}));
  const actions = node('div', undefined, {'data-ui': 'speech-actions'});
  const microphone = node('button', undefined, {type: 'button', 'data-ui': 'microphone-button',
    'aria-label': 'Start microphone recording', 'aria-pressed': 'false', 'aria-describedby': 'audio-mic-hint'});
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  for (const [key, value] of Object.entries({viewBox: '0 0 24 24', width: '24', height: '24', 'aria-hidden': 'true', focusable: 'false'})) svg.setAttribute(key, value);
  const path = document.createElementNS(svg.namespaceURI, 'path');
  path.setAttribute('d', 'M12 15a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v7a3 3 0 0 0 3 3Zm-7-4v1a7 7 0 0 0 14 0v-1M12 19v3m-4 0h8');
  svg.append(path);
  const microphoneText = node('span', 'Record microphone');
  microphone.append(svg, microphoneText);
  const cancel = node('button', 'Cancel', {type: 'button'});
  cancel.disabled = true;
  actions.append(microphone, cancel);
  const progress = node('p', 'Ready.', {role: 'status', 'aria-live': 'polite', id: 'audio-status'});
  const result = node('section', undefined, {'data-ui': 'speech-result', 'aria-label': 'Transcription result'});
  const transcript = node('pre', 'Your transcript will appear here.', {id: 'audio-transcript', 'aria-label': 'Transcript text'});
  const details = node('p', '');
  result.append(node('h3', 'Transcript'), transcript, details);
  view.append(drop, actions, node('p', 'Click the microphone to start. Stop sends the recording for transcription; after 3 minutes it stops and transcribes automatically.', {id: 'audio-mic-hint'}),
    node('p', 'Local Whisper / tiny · spoken language is preserved. Microphone access requires HTTPS or localhost.'), progress, result);
  panel.append(view);

  function update() {
    if (disposed) return;
    const recording = capture.state === 'recording', active = capture.state !== 'idle';
    input.disabled = Boolean(controller) || active;
    drop.setAttribute('aria-disabled', String(input.disabled));
    microphone.disabled = Boolean(controller) || !capture.supported || (active && !recording);
    microphone.setAttribute('aria-label', recording ? 'Stop recording and transcribe' : 'Start microphone recording');
    microphone.setAttribute('aria-pressed', String(recording));
    
    microphoneText.textContent = recording ? 'Stop & transcribe' : 'Record microphone';
    cancel.disabled = !controller && !active;
  }
  async function transcribe(audio, filename) {
    if (disposed || controller || capture.state !== 'idle') return;
    const error = audioFileError([{size: audio.size, type: audio.type, name: filename}]);
    if (error) { progress.textContent = error; progress.setAttribute('data-state', 'error'); return; }
    const current = new AbortController();
    controller = current;
    update();
    progress.textContent = 'Transcribing ' + filename + '…';
    progress.setAttribute('data-state', 'busy');
    transcript.textContent = ''; details.textContent = '';
    const start = performance.now();
    try {
      const body = new FormData();
      body.append('file', audio, filename); body.append('model', 'tiny');
      const response = await fetch(root.slice(0, -'/api/v1/compute'.length) + '/v1/audio/transcriptions', {
        method: 'POST', body, signal: current.signal, credentials: 'same-origin', cache: 'no-store',
      });
      let value;
      try { value = await response.json(); } catch { throw new Error('The server returned an invalid transcription response.'); }
      current.signal.throwIfAborted();
      if (!response.ok || value?.error) throw new Error(value?.error?.message || value?.error || value?.detail || 'HTTP ' + response.status);
      if (typeof value?.text !== 'string') throw new Error('The server returned no transcript.');
      if (!disposed) {
        transcript.textContent = value.text || 'No speech was recognized.';
        progress.textContent = 'Transcription complete.';
        progress.setAttribute('data-state', 'success');
        details.textContent = filename + ' · ' + (audio.size / 1024).toFixed(1) + ' KiB · '
          + ((performance.now() - start) / 1000).toFixed(2) + ' s';
      }
    } catch (error) {
      if (!disposed) {
        progress.textContent = current.signal.aborted ? 'Request cancelled in this browser.' : 'Transcription failed: ' + error.message;
        progress.setAttribute('data-state', current.signal.aborted ? 'neutral' : 'error');
        transcript.textContent = 'No transcript available.';
      }
    } finally { controller = null; update(); }
  }
  const capture = new MicrophoneCapture({...captureOptions,
    onState(state) {
      clearInterval(timer);
      progress.setAttribute('data-state', state === 'idle' ? 'neutral' : 'busy');
      if (state === 'requesting') progress.textContent = 'Waiting for microphone permission…';
      if (state === 'recording') {
        recordingStarted = performance.now();
        transcript.textContent = 'Recording…'; details.textContent = '';
        const tick = () => {
          const seconds = Math.floor((performance.now() - recordingStarted) / 1000);
          progress.textContent = 'Recording · ' + Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0') + ' / 3:00';
        };
        tick(); timer = setInterval(tick, 1000);
      }
      if (state === 'stopping') progress.textContent = 'Finishing recording…';
      update();
    },
    onAudio: transcribe,
    onError(message) { if (!disposed) { progress.textContent = message; progress.setAttribute('data-state', 'error'); update(); } },
  });
  function choose(files) {
    if (controller || capture.state !== 'idle' || disposed) return;
    const error = audioFileError(files);
    if (error) { progress.textContent = error; progress.setAttribute('data-state', 'error'); return; }
    transcribe(files[0], files[0].name);
  }
  input.addEventListener('change', () => { if (input.files.length) choose(input.files); input.value = ''; });
  drop.addEventListener('dragover', event => { event.preventDefault(); if (!input.disabled && event.dataTransfer) event.dataTransfer.dropEffect = 'copy'; });
  drop.addEventListener('dragenter', event => { event.preventDefault(); dragDepth++; if (!input.disabled) drop.setAttribute('data-dragging', 'true'); });
  drop.addEventListener('dragleave', event => { event.preventDefault(); if (--dragDepth <= 0) { dragDepth = 0; drop.setAttribute('data-dragging', 'false'); } });
  drop.addEventListener('drop', event => {
    event.preventDefault(); dragDepth = 0; drop.setAttribute('data-dragging', 'false');
    choose(event.dataTransfer?.files || []);
  });
  microphone.addEventListener('click', () => capture.state === 'recording' ? capture.stop() : capture.start());
  cancel.addEventListener('click', () => {
    if (controller) controller.abort();
    else { capture.cancel(); progress.textContent = 'Recording cancelled.'; transcript.textContent = 'No transcript available.'; }
  });
  const pageHide = () => {
    const wasRecording = capture.state !== 'idle';
    capture.cancel(); controller?.abort();
    if (wasRecording) { progress.textContent = 'Recording cancelled.'; transcript.textContent = 'No transcript available.'; }
  };
  window.addEventListener('pagehide', pageHide);
  if (!capture.supported) progress.textContent = 'Microphone recording is unavailable. Use HTTPS or localhost with a supported browser, or choose an audio file.';
  update();
  return {dispose() { disposed = true; clearInterval(timer); capture.dispose(); controller?.abort(); window.removeEventListener('pagehide', pageHide); }};
}
