/* WAV synthesis, bounded browser buffering and temporary audio playback. */
'use strict';

const MAX_BYTES = 32 * 1024 * 1024;
const UNKNOWN_SIZE = 0xffffffff;
export const speechModelKey = name => name.toLowerCase() + (name.split('/').at(-1).includes(':') ? '' : ':latest');

export function waveHeader(bytes) {
  if (bytes.length < 12) return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const word = offset => String.fromCharCode(...bytes.subarray(offset, offset + 4));
  if (word(0) !== 'RIFF' || word(8) !== 'WAVE') throw new Error('The server did not return WAV audio.');
  let offset = 12, format = false;
  while (offset + 8 <= bytes.length) {
    const kind = word(offset), size = view.getUint32(offset + 4, true);
    offset += 8;
    if (kind === 'data') {
      if (!format || size === 0 || (size !== UNKNOWN_SIZE && size % 2)) throw new Error('Invalid PCM WAV data.');
      return {offset, dataSize: size};
    }
    const end = offset + size + size % 2;
    if (end > 65536) throw new Error('The WAV header exceeds its size limit.');
    if (end > bytes.length) return null;
    if (kind === 'fmt ') {
      if (size < 16 || view.getUint16(offset, true) !== 1 || view.getUint16(offset + 2, true) !== 1
          || view.getUint32(offset + 4, true) !== 24000 || view.getUint32(offset + 8, true) !== 48000
          || view.getUint16(offset + 12, true) !== 2 || view.getUint16(offset + 14, true) !== 16) {
        throw new Error('Expected 24 kHz mono 16-bit PCM WAV.');
      }
      format = true;
    }
    offset = end;
  }
  if (offset + 8 > 65536) throw new Error('The WAV header exceeds its size limit.');
  return null;
}

export async function readSpeech(response, {started, now = () => performance.now(), onProgress = () => {}}) {
  if (response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'audio/wav' || !response.body) {
    throw new Error('The server did not return a WAV stream.');
  }
  const reader = response.body.getReader(), chunks = [];
  let size = 0, header = new Uint8Array(), info = null, first = null, complete = false;
  try {
    for (;;) {
      const {value, done} = await reader.read();
      if (done) { complete = true; break; }
      size += value.length;
      if (size > MAX_BYTES) throw new Error('Audio exceeds the 32 MiB limit.');
      chunks.push(value);
      if (info === null) {
        const count = Math.min(65536, header.length + value.length), joined = new Uint8Array(count);
        joined.set(header); joined.set(value.subarray(0, count - header.length), header.length);
        header = joined; info = waveHeader(header);
        if (info === null && size > 65536) throw new Error('Invalid WAV header.');
      }
      if (first === null && info && size >= info.offset + 2) first = now();
      onProgress(size);
    }
    const ended = now();
    if (!info || first === null) throw new Error('The audio stream is empty.');
    const actual = size - info.offset, audioBytes = info.dataSize === UNKNOWN_SIZE ? actual : info.dataSize;
    if (audioBytes <= 0 || audioBytes > actual || audioBytes % 2) throw new Error('The WAV stream is truncated.');
    const bytes = new Uint8Array(await new Blob(chunks).arrayBuffer()), data = new DataView(bytes.buffer);
    // Give browser players/downloads finite sizes after the original stream has completed.
    data.setUint32(4, bytes.length - 8, true);
    data.setUint32(info.offset - 4, audioBytes, true);
    const duration = Math.max(0, (ended - started) / 1000), audioDuration = audioBytes / 48000;
    return {blob: new Blob([bytes], {type: 'audio/wav'}), metrics: {
      duration_seconds: duration, ttfa_seconds: Math.max(0, (first - started) / 1000),
      audio_duration_seconds: audioDuration, real_time_factor: duration / audioDuration,
    }};
  } finally {
    if (!complete) { try { await reader.cancel(); } catch {} }
    reader.releaseLock();
  }
}

export function mountTTS({panel, sidebar = panel, root, node, table, snapshot = null}) {
  let controller = null, disposed = false, objectURL = null, configuration = null;
  const view = node('div', undefined, {'data-ui': 'tts-view'}), form = node('form', undefined, {'data-ui': 'tts-form'});
  view.append(node('p', 'Turn text into speech. WAV audio stays in this page until you download it or leave.'));
  const settings = node('div', undefined, {'data-ui': 'tts-settings'});
  const modelLabel = node('label', 'Speech model'), model = node('select', undefined, {'aria-label': 'Speech model'});
  const voiceLabel = node('label', 'Voice'), voice = node('select', undefined, {'aria-label': 'Voice'});
  modelLabel.append(model); voiceLabel.append(voice); settings.append(modelLabel, voiceLabel);
  sidebar.append(settings, node('p', 'WAV / 24 kHz mono · up to 4096 characters. Voices come from the last calibration.', {'data-ui': 'tts-hint'}));
  const label = node('label', 'Text'), input = node('textarea', undefined, {rows: '6', required: '', autocomplete: 'off',
    'aria-label': 'Text to speak', placeholder: 'Write the text to read aloud…'});
  label.append(input);
  const length = node('p', '0 / 4096 characters', {'data-ui': 'tts-hint'});
  const count = () => { length.textContent = Array.from(input.value || '').length + ' / 4096 characters'; };
  input.addEventListener('input', count);
  const actions = node('div', undefined, {'data-ui': 'chat-actions'}), send = node('button', 'Generate speech →', {type: 'submit'});
  const stop = node('button', 'Cancel', {type: 'button'}); stop.disabled = true;
  stop.addEventListener('click', () => controller?.abort());
  actions.append(send, stop, node('small', 'Ctrl / ⌘ + Enter to generate'));
  const status = node('p', 'Loading speech configuration…', {role: 'status', 'aria-live': 'polite', id: 'tts-status'});
  const result = node('section', undefined, {'data-ui': 'tts-result', 'aria-label': 'Generated speech'}); result.hidden = true;
  const player = node('audio', undefined, {controls: '', preload: 'metadata', 'aria-label': 'Speech playback'});
  const download = node('a', 'Download WAV ↓', {download: 'speech.wav'}), speed = node('div');
  result.append(node('h3', 'Audio'), player, download, speed);
  form.append(label, length, actions);
  view.append(form, status, result); panel.append(view);
  const metric = (value, unit = '') => typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? new Intl.NumberFormat('en', {maximumFractionDigits: 3}).format(value) + unit : 'unavailable';
  function clearAudio() {
    player.pause(); player.removeAttribute('src'); player.load(); download.removeAttribute('href');
    if (objectURL) URL.revokeObjectURL(objectURL);
    objectURL = null; result.hidden = true;
  }
  function options(select, values, selected) {
    select.replaceChildren(...values.map(value => node('option', value, {value})));
    select.value = values.includes(selected) ? selected : values[0] || '';
  }
  function voices() {
    const selected = configuration?.speech_models?.find(value => value.name === model.value);
    const names = new Set(selected ? [selected.voice] : []);
    for (const records of Object.values(configuration?.results || {})) {
      for (const record of Object.values(records)) {
        if (record.task === 'speech' && speechModelKey(record.model) === speechModelKey(model.value || '')) {
          for (const name of record.speech?.voices || []) names.add(name);
        }
      }
    }
    options(voice, [...names], voice.value || selected?.voice);
    voice.disabled = Boolean(controller) || !names.size;
  }
  function update(value) {
    if (disposed || !value) return;
    const signature = JSON.stringify([value.speech_models, value.results]);
    if (signature === configuration?._signature) return;
    configuration = {...value, _signature: signature};
    const selected = model.value;
    options(model, (value.speech_models || []).map(item => item.name), selected);
    voices();
    model.disabled = Boolean(controller) || !model.value;
    send.disabled = Boolean(controller) || !model.value;
    if (!controller && !objectURL) status.textContent = model.value
      ? 'Ready. Voices come from the last calibration; the configured default is available before calibration.'
      : 'No speech models configured. Add compute.speech to the startup configuration and restart Compute.';
  }
  model.addEventListener('change', () => { voice.value = ''; voices(); });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (disposed || controller) return;
    const text = input.value || '';
    if (!model.value || !voice.value || !text.trim() || Array.from(text).length > 4096) {
      status.setAttribute('data-state', 'error');
      status.textContent = 'Choose a model and voice, and enter 1–4096 characters.'; return;
    }
    clearAudio(); controller = new AbortController();
    send.disabled = model.disabled = voice.disabled = true; stop.disabled = false; input.readOnly = true;
    status.textContent = 'Connecting…';
    status.setAttribute('data-state', 'busy');
    const started = performance.now();
    try {
      const response = await fetch(root.replace(/\/api\/v1\/compute$/, '') + '/v1/audio/speech', {
        method: 'POST', cache: 'no-store', credentials: 'same-origin', signal: controller.signal,
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({model: model.value, voice: voice.value, input: text, response_format: 'wav'}),
      });
      if (!response.ok) {
        let value; try { value = await response.json(); } catch {}
        throw new Error(value?.error?.message || value?.detail || 'HTTP ' + response.status);
      }
      let target = null;
      try { target = JSON.parse(response.headers.get('x-compute-target')); } catch {}
      const audio = await readSpeech(response, {started, onProgress: size => {
        if (!disposed) status.textContent = 'Receiving audio… ' + metric(size / 1024, ' KiB');
      }});
      if (disposed || controller.signal.aborted) return;
      objectURL = URL.createObjectURL(audio.blob); player.src = objectURL; download.href = objectURL;
      result.hidden = false;
      const expected = target?.expected || {}, measured = audio.metrics;
      speed.replaceChildren(table(['Measurement', 'Expected', 'Measured in browser'], [
        ['First audio', metric(expected.ttfa_seconds, ' s'), metric(measured.ttfa_seconds, ' s')],
        ['Real-time factor · lower is faster', metric(expected.real_time_factor), metric(measured.real_time_factor)],
        ['Audio duration', '—', metric(measured.audio_duration_seconds, ' s')],
      ]), node('p', 'Browser timing includes discovery and network. Expected values apply to the calibrated model and voice.'));
      status.textContent = 'Speech complete. Use the player to listen or download the WAV file.';
      status.setAttribute('data-state', 'success');
    } catch (error) {
      status.setAttribute('data-state', error.name === 'AbortError' ? 'neutral' : 'error');
      if (!disposed) status.textContent = error.name === 'AbortError' ? 'Speech generation cancelled.' : 'Speech failed: ' + error.message;
    } finally {
      controller = null;
      if (!disposed) { send.disabled = model.disabled = !model.value; voice.disabled = !voice.value; stop.disabled = true; input.readOnly = false; }
    }
  });
  function dispose() {
    if (disposed) return;
    disposed = true; controller?.abort(); clearAudio(); input.value = '';
    window.removeEventListener('pagehide', dispose);
  }
  window.addEventListener('pagehide', dispose);
  update(snapshot);
  return {update, dispose};
}
