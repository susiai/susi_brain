/* Run with node --test test/test_tts.mjs; no speech model or audio device is used. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const source = await readFile(new URL('../src/susi_brain/shared/web/static/tts.js', import.meta.url));
const {readSpeech, waveHeader, mountTTS} = await import('data:text/javascript;base64,' + source.toString('base64'));

function wav(unknown = false) {
  const bytes = new Uint8Array(96044), view = new DataView(bytes.buffer);
  for (const [offset, text] of [[0, 'RIFF'], [8, 'WAVE'], [12, 'fmt '], [36, 'data']]) {
    for (let index = 0; index < text.length; index++) bytes[offset + index] = text.charCodeAt(index);
  }
  for (const [offset, value] of [[4, unknown ? 0xffffffff : bytes.length - 8], [16, 16], [24, 24000], [28, 48000],
    [40, unknown ? 0xffffffff : 96000]]) view.setUint32(offset, value, true);
  for (const [offset, value] of [[20, 1], [22, 1], [32, 2], [34, 16]]) view.setUint16(offset, value, true);
  return bytes;
}

test('first PCM timing excludes headers and unknown WAV lengths are fixed for playback', async () => {
  const bytes = wav(true), parts = [bytes.subarray(0, 18), bytes.subarray(18, 44), bytes.subarray(44, 46), bytes.subarray(46)];
  let index = 0, time = 0, released = false;
  const response = {headers: new Headers({'Content-Type': 'audio/wav'}), body: {getReader: () => ({
    async read() { time = [100, 200, 400, 1000, 1000][index]; return index < parts.length ? {value: parts[index++]} : {done: true}; },
    releaseLock() { released = true; },
  })}};
  const result = await readSpeech(response, {started: 0, now: () => time});
  assert.deepEqual(result.metrics, {duration_seconds: 1, ttfa_seconds: .4, audio_duration_seconds: 2, real_time_factor: .5});
  const completed = new Uint8Array(await result.blob.arrayBuffer()), data = new DataView(completed.buffer);
  assert.equal(data.getUint32(4, true), completed.length - 8);
  assert.equal(data.getUint32(40, true), 96000);
  assert.deepEqual(completed.subarray(44), bytes.subarray(44));
  assert.ok(released);
});

test('truncated, empty and non-PCM WAV responses cannot become successful audio', async () => {
  for (const bytes of [wav().subarray(0, 50), new Uint8Array(), new TextEncoder().encode('not valid audio')]) {
    await assert.rejects(readSpeech(new Response(bytes, {headers: {'Content-Type': 'audio/wav'}}), {started: 0}));
  }
  const wrongRate = wav(); new DataView(wrongRate.buffer).setUint32(24, 16000, true);
  assert.throws(() => waveHeader(wrongRate), /24 kHz/);
});

test('a stream failure cancels and releases its reader', async () => {
  let cancelled = false, released = false;
  const response = {headers: new Headers({'Content-Type': 'audio/wav'}), body: {getReader: () => ({
    async read() { throw new TypeError('stream failed'); },
    async cancel() { cancelled = true; }, releaseLock() { released = true; },
  })}};
  await assert.rejects(readSpeech(response, {started: 0}), /stream failed/);
  assert.ok(cancelled && released);
});

function fixture(t, fetch, snapshot = {speech_models: [{name: 'phonon', voice: 'default'}], results: {}}) {
  const nodes = [], revoked = [];
  class Element extends EventTarget {
    constructor(tag, text = '', attributes = {}) {
      super(); Object.assign(this, {tag, textContent: text, attributes, children: [], value: ''}); nodes.push(this);
    }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    removeAttribute(name) { delete this.attributes[name]; }
    setAttribute(name, value) { this.attributes[name] = value; }
    pause() { this.paused = true; }
    load() {}
  }
  const window = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', {configurable: true, value: new EventTarget()});
  t.mock.method(globalThis, 'fetch', fetch);
  t.mock.method(URL, 'createObjectURL', () => 'blob:test-audio');
  t.mock.method(URL, 'revokeObjectURL', value => revoked.push(value));
  const view = mountTTS({panel: new Element('panel'), root: '/proxy/api/v1/compute', snapshot,
    node: (...args) => new Element(...args), table: () => new Element('table')});
  t.after(() => { view.dispose(); if (window) Object.defineProperty(globalThis, 'window', window); else delete globalThis.window; });
  const find = label => nodes.find(node => node.attributes['aria-label'] === label || node.attributes.id === label)
    || nodes.find(node => node.textContent === label);
  return {view, find, revoked, submit(text = 'Grüße.') {
    find('Text to speak').value = text;
    nodes.find(node => node.tag === 'form').dispatchEvent(new Event('submit', {cancelable: true}));
  }};
}

async function until(check) {
  for (let count = 0; count < 40 && !check(); count++) await new Promise(resolve => setImmediate(resolve));
  assert.ok(check(), 'Expected the synthesis state to settle.');
}

test('only explicit generation calls the prefixed API; playback URLs are released on navigation', async t => {
  let calls = 0;
  const f = fixture(t, async (url, options) => {
    calls++;
    assert.equal(url, '/proxy/v1/audio/speech');
    assert.deepEqual(JSON.parse(options.body), {model: 'phonon', voice: 'default', input: 'Grüße.', response_format: 'wav'});
    return new Response(wav(true), {headers: {'Content-Type': 'audio/wav'}});
  });
  assert.equal(calls, 0);
  f.submit();
  await until(() => f.find('tts-status').textContent.startsWith('Speech complete'));
  assert.equal(f.find('Speech playback').src, 'blob:test-audio');
  assert.equal(calls, 1);
  f.view.dispose();
  assert.deepEqual(f.revoked, ['blob:test-audio']);
  assert.equal(f.find('Speech playback').paused, true);
});

test('metadata refresh supplies voices without triggering inference; invalid input stays local', t => {
  const f = fixture(t, () => assert.fail('No generation expected.'));
  f.view.update({speech_models: [{name: 'phonon', voice: 'default'}], results: {endpoint: {
    'phonon:latest': {task: 'speech', model: 'phonon:latest', speech: {voices: ['default', 'Fréya <plain text>']}},
  }}});
  assert.equal(f.find('Voice').children[1].textContent, 'Fréya <plain text>');
  f.submit('😀'.repeat(4097));
  assert.match(f.find('tts-status').textContent, /1–4096/);
});

test('upstream errors and cancellation leave no downloadable audio', async t => {
  const f = fixture(t, async () => new Response(JSON.stringify({error: {message: 'No model available.'}}), {status: 503}));
  f.submit();
  await until(() => f.find('tts-status').textContent.startsWith('Speech failed'));
  assert.match(f.find('tts-status').textContent, /No model available/);
  assert.equal(f.find('Generated speech').hidden, true);
});

test('cancelling a pending request aborts its fetch and restores the form', async t => {
  let aborted = false;
  const f = fixture(t, (url, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => { aborted = true; reject(new DOMException('Cancelled', 'AbortError')); });
  }));
  f.submit(); f.find('Cancel').dispatchEvent(new Event('click'));
  await until(() => f.find('tts-status').textContent === 'Speech generation cancelled.');
  assert.ok(aborted);
  assert.equal(f.find('Generate speech →').disabled, false);
  assert.equal(f.find('Generated speech').hidden, true);
});
