/* Run with node --test test/test_chat_stream.mjs; no frontend dependencies. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const source = await readFile(new URL('../src/susi_brain/shared/web/static/chat.js', import.meta.url));
const {ChatEvents, chatMetrics, chatPayload} = await import('data:text/javascript;base64,' + source.toString('base64'));
const bytes = new TextEncoder();
const frame = value => 'data: ' + JSON.stringify(value) + '\n\n';
const delta = (content, more = {}) => ({choices: [{index: 0, delta: {content, ...more}}]});
const finish = reason => ({choices: [{index: 0, delta: {}, finish_reason: reason}]});

test('stateless payload requests usage and both thinking settings', () => {
  for (const thinking of [false, true]) {
    const payload = chatPayload('smart', thinking, 'Grüße <script>');
    assert.deepEqual(payload.messages, [{role: 'user', content: 'Grüße <script>'}]);
    assert.equal(payload.stream_options.include_usage, true);
    assert.equal(payload.reasoning_effort, thinking ? 'medium' : 'none');
    assert.equal(payload.reasoning.enabled, thinking);
    assert.equal(payload.chat_template_kwargs.enable_thinking, thinking);
  }
  assert.deepEqual(chatPayload('small', false, 'Second prompt').messages, [{role: 'user', content: 'Second prompt'}]);
});

test('arbitrary byte boundaries, multibyte text, CRLF, comments and final usage', () => {
  let content = '', thinking = '';
  const events = new ChatEvents((text, thought) => { content += text; thinking += thought; }, () => 1250);
  const text = ': heartbeat\r\n\r\ndata:\r\n\r\n' + frame(delta(null, {role: 'assistant'}))
    + frame(delta('', {reasoning_content: 'Consider…'})) + frame(delta('Grüße 👋 <b>text</b>'))
    + frame(finish('stop')) + frame({choices: [], usage: {prompt_tokens: 1000, completion_tokens: 8}}) + 'data: [DONE]';
  // A byte at a time splits both UTF-8 codepoints and CRLF/event boundaries.
  for (const byte of bytes.encode(text)) events.push(new Uint8Array([byte]));
  events.push(new Uint8Array(), true);
  assert.equal(content, 'Grüße 👋 <b>text</b>');
  assert.equal(thinking, 'Consider…');
  assert.equal(events.done, true);
  assert.equal(events.complete, true);
  assert.deepEqual(chatMetrics(events, 1000, 3000, true), {
    duration_seconds: 2, ttft_seconds: .25, tokens_per_second: 4, completion_tokens: 8,
  });
});

test('empty and role deltas do not start TTFT; multiline SSE and LF/CR separators work', () => {
  let time = 0;
  const events = new ChatEvents(() => {}, () => time);
  events.push(bytes.encode(frame(delta(null, {role: 'assistant'})) + frame(delta(''))));
  assert.equal(events.firstTokenAt, null);
  time = 700;
  events.push(bytes.encode('data: {"choices":\rdata: [{"delta":{"reasoning_details":[{"text":"thinking"}]}}]}\r\r'));
  assert.equal(events.firstTokenAt, 700);
  events.push(bytes.encode(frame(delta('answer')) + frame(finish('length'))));
  events.push(new Uint8Array(), true);
  assert.equal(events.complete, true);
  assert.equal(chatMetrics(events, 0, 2000, true).tokens_per_second, null);
});

test('missing, invalid and prompt-only usage cannot produce a fake token rate', () => {
  for (const usage of [{}, {prompt_tokens: 50}, {completion_tokens: -1}, {completion_tokens: '10'}, {completion_tokens: 1.5}]) {
    const events = new ChatEvents(() => {}, () => 200);
    events.push(bytes.encode(frame(delta('A whole sentence in one chunk.')) + frame(finish('stop')) + frame({usage})));
    assert.equal(chatMetrics(events, 0, 1000, true).tokens_per_second, null);
  }
});

test('stream failures, malformed events and truncated UTF-8 are surfaced', () => {
  for (const text of [frame({error: {code: 'upstream_stream_failed'}}), 'data: broken\n\n', 'data: null\n\n']) {
    assert.throws(() => new ChatEvents(() => {}).push(bytes.encode(text)));
  }
  const events = new ChatEvents(() => {});
  events.push(new Uint8Array([0xf0]));
  assert.throws(() => events.push(new Uint8Array(), true));
});

test('incomplete or aborted output retains TTFT but never reports completed throughput', () => {
  const events = new ChatEvents(() => {}, () => 500);
  events.push(bytes.encode(frame(delta('partial')) + frame({usage: {completion_tokens: 20}})));
  assert.equal(events.complete, false);
  events.push(bytes.encode('data: [DONE]\n\n'));
  assert.equal(events.complete, false);
  assert.deepEqual(chatMetrics(events, 0, 2000, false), {
    duration_seconds: 2, ttft_seconds: .5, tokens_per_second: null, completion_tokens: 20,
  });
});

test('first and terminal triton metadata identify the target without changing text, thinking or usage', () => {
  const metadata = {modelname: 'Mödel', compute_group: 'small', compute_hostname: 'llm.example', request_bytes: 321};
  let content = '', reasoning = '';
  const events = new ChatEvents((text, thought) => { content += text; reasoning += thought; }, () => 250);
  const input = frame({...delta('', {role: 'assistant'}), ...metadata})
    + frame({...delta('Grüße', {reasoning_content: 'thinking'}), ...metadata, compute_hostname: 'untrusted middle'})
    + frame({...finish('stop'), ...metadata})
    + frame({choices: [], usage: {completion_tokens: 12}, ...metadata, compute_hostname: 'untrusted usage'})
    + 'data: [DONE]\n\n';
  for (const byte of bytes.encode(input)) events.push(new Uint8Array([byte]));
  assert.equal(content, 'Grüße'); assert.equal(reasoning, 'thinking');
  assert.deepEqual(events.routing, metadata);
  assert.equal(chatMetrics(events, 0, 2000, true).tokens_per_second, 6);
});

test('missing or invalid boundary metadata leaves older streams compatible', () => {
  const events = new ChatEvents(() => {});
  events.push(bytes.encode(frame({...delta('answer'), model: 'older-model', request_bytes: -1}) + frame(finish('length'))));
  assert.equal(events.routing, null);
  assert.equal(events.model, 'older-model'); assert.equal(events.complete, true);
});
