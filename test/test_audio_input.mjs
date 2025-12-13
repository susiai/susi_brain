/* Run with node --test test/test_audio_input.mjs; no microphone is accessed. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const source = await readFile(new URL('../src/susi_brain/shared/web/static/audio.js', import.meta.url));
const {MicrophoneCapture, MAX_AUDIO_BYTES, audioFileError, recordingName, mountSpeech} = await import('data:text/javascript;base64,' + source.toString('base64'));
const flush = () => new Promise(resolve => queueMicrotask(resolve));

function fixture({permission, Recorder} = {}) {
  const track = {stopped: 0, stop() { this.stopped++; }};
  const stream = {getTracks: () => [track]};
  const states = [], errors = [], audio = [], recorders = [];
  class FakeRecorder extends EventTarget {
    static isTypeSupported(type) { return type === 'audio/webm;codecs=opus'; }
    constructor(stream, options) {
      super(); this.stream = stream; this.mimeType = options?.mimeType || 'audio/mp4';
      this.state = 'inactive'; recorders.push(this);
    }
    start(timeslice) { this.timeslice = timeslice; this.state = 'recording'; }
    chunk(value = new Blob(['audio'], {type: this.mimeType})) {
      const event = new Event('dataavailable'); event.data = value; this.dispatchEvent(event);
    }
    stop() {
      this.state = 'inactive';
      queueMicrotask(() => { this.chunk(); this.dispatchEvent(new Event('stop')); });
    }
  }
  const capture = new MicrophoneCapture({
    mediaDevices: {getUserMedia: permission || (async constraints => { assert.deepEqual(constraints, {audio: true, video: false}); return stream; })},
    Recorder: Recorder || FakeRecorder,
    onState: value => states.push(value), onError: value => errors.push(value), onAudio: (...value) => audio.push(value),
  });
  return {capture, track, stream, states, errors, audio, recorders, FakeRecorder};
}

test('file validation rejects multiple, empty, oversized and non-audio input', () => {
  const file = {name: 'speech.wav', size: 12, type: ''};
  assert.equal(audioFileError([file]), null);
  assert.equal(audioFileError([{...file, name: 'SPEECH.M4A'}]), null);
  assert.equal(audioFileError([{...file, name: 'clip', type: 'audio/webm'}]), null);
  assert.match(audioFileError([]), /one audio file/);
  assert.match(audioFileError([file, file]), /one audio file/);
  assert.match(audioFileError([{...file, size: 0}]), /empty/);
  assert.match(audioFileError([{...file, size: MAX_AUDIO_BYTES + 1}]), /25 MiB/);
  assert.match(audioFileError([{...file, name: 'document.html', type: 'text/html'}]), /Choose an audio file/);
});

test('recorded filenames follow the real container, including Safari MP4', () => {
  for (const [type, extension] of [['audio/webm;codecs=opus', 'webm'], ['audio/ogg;codecs=opus', 'ogg'],
    ['audio/mp4', 'm4a'], ['audio/wav', 'wav'], ['unknown', 'bin']]) {
    assert.equal(recordingName(type), 'microphone.' + extension);
  }
});

test('explicit stop combines chunks, preserves MIME and releases the microphone', async () => {
  const f = fixture();
  assert.equal(f.recorders.length, 0);
  await f.capture.start();
  const recorder = f.recorders[0];
  assert.equal(recorder.timeslice, 1000);
  recorder.chunk();
  f.capture.stop(); await flush();
  assert.deepEqual(f.states, ['requesting', 'recording', 'stopping', 'idle']);
  assert.equal(f.audio.length, 1);
  assert.equal(f.audio[0][0].type, 'audio/webm;codecs=opus');
  assert.equal(await f.audio[0][0].text(), 'audioaudio');
  assert.equal(f.audio[0][1], 'microphone.webm');
  assert.ok(f.track.stopped > 0);
  f.capture.dispose();
});

test('cancel discards captured chunks and never sends a partial recording', async () => {
  const f = fixture();
  await f.capture.start(); f.recorders[0].chunk();
  f.capture.cancel(); await flush();
  assert.equal(f.audio.length, 0);
  assert.equal(f.capture.state, 'idle');
  assert.ok(f.track.stopped > 0);
  f.capture.dispose();
});

test('navigating away before permission is granted closes the late stream', async () => {
  let grant;
  const permission = new Promise(resolve => { grant = resolve; });
  const f = fixture({permission: () => permission});
  const starting = f.capture.start();
  assert.equal(f.capture.state, 'requesting');
  f.capture.dispose(); grant(f.stream); await starting;
  assert.equal(f.track.stopped, 1);
  assert.equal(f.recorders.length, 0);
  assert.equal(f.audio.length, 0);
  assert.deepEqual(f.errors, []);
});

test('permission rejection and unsupported browsers leave file upload available', async () => {
  const f = fixture({permission: async () => { throw Object.assign(new Error(), {name: 'NotAllowedError'}); }});
  await f.capture.start();
  assert.equal(f.capture.state, 'idle');
  assert.match(f.errors[0], /permission was denied/);
  const errors = [];
  const unsupported = new MicrophoneCapture({mediaDevices: null, Recorder: null, onError: message => errors.push(message)});
  await unsupported.start();
  assert.equal(unsupported.supported, false);
  assert.match(errors[0], /HTTPS or localhost/);
});

test('recording size limit discards audio and releases all tracks', async () => {
  const f = fixture();
  await f.capture.start();
  f.recorders[0].chunk({size: MAX_AUDIO_BYTES + 1}); await flush();
  assert.equal(f.audio.length, 0);
  assert.match(f.errors[0], /exceeds 25 MiB/);
  assert.ok(f.track.stopped > 0);
  f.capture.dispose();
});

test('automatic duration limit stops and submits the complete clip', async t => {
  let finish;
  t.mock.method(globalThis, 'setTimeout', (callback, delay) => { assert.equal(delay, 180000); finish = callback; return null; });
  const f = fixture();
  await f.capture.start(); finish(); await flush();
  assert.equal(f.audio.length, 1);
  assert.equal(f.capture.state, 'idle');
  assert.ok(f.track.stopped > 0);
  f.capture.dispose();
});

test('recorder failures release resources without uploading audio', async () => {
  const f = fixture();
  await f.capture.start();
  f.recorders[0].dispatchEvent(new Event('error'));
  f.recorders[0].stop(); await flush();
  assert.equal(f.audio.length, 0);
  assert.equal(f.errors.length, 1);
  assert.equal(f.capture.state, 'idle');
  assert.ok(f.track.stopped > 0);
  f.capture.dispose();
});

function speechFixture(t, fetch) {
  const nodes = [];
  class Element extends EventTarget {
    constructor(tag, text = '', attributes = {}) {
      super(); this.tag = tag; this.textContent = text; this.attributes = {...attributes}; this.children = [];
      this.classList = {add() {}, remove() {}, toggle() {}};
      nodes.push(this);
    }
    append(...children) { this.children.push(...children); }
    setAttribute(name, value) { this.attributes[name] = value; }
  }
  const descriptors = ['document', 'window'].map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]);
  Object.defineProperty(globalThis, 'document', {configurable: true, value: {createElementNS: (ns, tag) => new Element(tag)}});
  Object.defineProperty(globalThis, 'window', {configurable: true, value: new EventTarget()});
  t.mock.method(globalThis, 'fetch', fetch);
  const view = mountSpeech({panel: new Element('panel'), root: '/proxy/api/v1/compute',
    node: (tag, text, attributes) => new Element(tag, text, attributes), captureOptions: {mediaDevices: null, Recorder: null}});
  t.after(() => {
    view.dispose();
    for (const [name, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor); else delete globalThis[name];
    }
  });
  const find = name => nodes.find(element => element.attributes.id === name || element.attributes['aria-label'] === name);
  return {find, drop(files) {
    const event = new Event('drop', {cancelable: true}); event.dataTransfer = {files};
    assert.equal(find('Audio drop zone').dispatchEvent(event), false);
  }};
}

async function until(check) {
  for (let attempt = 0; attempt < 30 && !check(); attempt++) await new Promise(resolve => setImmediate(resolve));
  assert.ok(check(), 'Expected the upload state to settle.');
}

test('dropping audio immediately posts multipart to the prefixed API and renders plain text', async t => {
  let calls = 0;
  const f = speechFixture(t, async (url, options) => {
    calls++;
    assert.equal(url, '/proxy/v1/audio/transcriptions');
    assert.equal(options.body.get('model'), 'tiny');
    assert.equal(options.body.get('file').name, 'Grüße.webm');
    assert.equal(options.body.get('file').type, 'audio/webm');
    assert.equal(await options.body.get('file').text(), 'encoded-audio');
    assert.equal(options.headers, undefined); // The browser supplies the multipart boundary.
    return new Response(JSON.stringify({text: 'Grüße <b>plain text</b>'}), {status: 200});
  });
  f.drop([new File(['encoded-audio'], 'Grüße.webm', {type: 'audio/webm'})]);
  await until(() => f.find('audio-status').textContent === 'Transcription complete.');
  assert.equal(calls, 1);
  assert.equal(f.find('audio-transcript').textContent, 'Grüße <b>plain text</b>');
  assert.equal(f.find('Audio file').disabled, false);
});

test('legacy HTTP-200 error objects are shown as transcription failures', async t => {
  const f = speechFixture(t, async () => new Response(JSON.stringify({error: 'No file provided'}), {status: 200}));
  f.drop([new File(['audio'], 'sample.wav', {type: 'audio/wav'})]);
  await until(() => f.find('audio-status').textContent.startsWith('Transcription failed:'));
  assert.match(f.find('audio-status').textContent, /No file provided/);
  assert.equal(f.find('audio-transcript').textContent, 'No transcript available.');
});

test('invalid drops never upload and give a visible error', t => {
  const f = speechFixture(t, () => { assert.fail('Invalid drops must not trigger inference.'); });
  f.drop([new File(['document'], 'readme.html', {type: 'text/html'})]);
  assert.match(f.find('audio-status').textContent, /Choose an audio file/);
});
