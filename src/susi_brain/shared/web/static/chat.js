/* Single-prompt chat. Transport chunks are never treated as model tokens. */
'use strict';

export function chatPayload(model, thinking, prompt) {
  return {
    model, messages: [{role: 'user', content: prompt}], stream: true,
    stream_options: {include_usage: true},
    reasoning_effort: thinking ? 'medium' : 'none',
    reasoning: {enabled: thinking}, chat_template_kwargs: {enable_thinking: thinking},
  };
}

export class ChatEvents {
  constructor(onDelta, now = () => performance.now()) {
    this.onDelta = onDelta;
    this.now = now;
    this.decoder = new TextDecoder('utf-8', {fatal: true});
    this.line = ''; this.data = []; this.skipLF = false;
    this.done = false; this.finishReason = null; this.firstTokenAt = null;
    this.completionTokens = null; this.model = null; this.bytes = 0;
    this.routing = null; this.jsonSeen = false; this.terminalSeen = false;
  }
  push(bytes, final = false) {
    this.bytes += bytes.byteLength;
    if (this.bytes > 8 * 1024 * 1024 + 8192) throw new Error('Response exceeds the stream size limit.');
    const text = this.decoder.decode(bytes, {stream: !final});
    for (const character of text) {
      if (this.skipLF) { this.skipLF = false; if (character === '\n') continue; }
      if (character === '\r' || character === '\n') {
        this.readLine(); this.skipLF = character === '\r';
      } else this.line += character;
    }
    if (final) { if (this.line) this.readLine(); this.event(); }
  }
  readLine() {
    const line = this.line;
    this.line = '';
    if (!line) this.event();
    else if (line === 'data' || line.startsWith('data:')) this.data.push(line.slice(5).replace(/^ /, ''));
  }
  event() {
    if (!this.data.length || this.done) { this.data = []; return; }
    const data = this.data.join('\n');
    this.data = [];
    if (!data.trim()) return;
    if (data.trim() === '[DONE]') { this.done = true; return; }
    let value;
    try { value = JSON.parse(data); } catch { throw new Error('Invalid JSON in the response stream.'); }
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid response event.');
    if (value.error) throw new Error('Chat stream failed: ' + (value.error.code || 'upstream error'));
    const terminal = Array.isArray(value.choices) && value.choices.some(choice => choice && choice.finish_reason != null);
    if (!this.jsonSeen || (terminal && !this.terminalSeen)) {
      if (['modelname', 'compute_group', 'compute_hostname'].every(key => typeof value[key] === 'string' && value[key].trim())
          && Number.isSafeInteger(value.request_bytes) && value.request_bytes >= 0) {
        this.routing = Object.fromEntries(['modelname', 'compute_group', 'compute_hostname', 'request_bytes'].map(key => [key, value[key]]));
      }
    }
    this.jsonSeen = true; this.terminalSeen ||= terminal;
    if (typeof value.model === 'string') this.model = value.model;
    const count = value.usage?.completion_tokens;
    if (Number.isSafeInteger(count) && count >= 0) this.completionTokens = count;
    if (!Array.isArray(value.choices)) return; // Usage-only or provider metadata event.
    const choice = value.choices.find(item => item && (item.index === 0 || item.index === undefined));
    if (!choice) return;
    if (choice.finish_reason != null) this.finishReason = choice.finish_reason;
    const delta = choice.delta || {};
    const content = typeof delta.content === 'string' ? delta.content : '';
    const reasoning = [delta.reasoning_content, delta.reasoning].find(item => typeof item === 'string' && item.length)
      || (Array.isArray(delta.reasoning_details) ? delta.reasoning_details.map(item =>
        [item?.text, item?.summary].find(value => typeof value === 'string') || '').join('') : '');
    if (content || reasoning) {
      if (this.firstTokenAt === null) this.firstTokenAt = this.now();
      this.onDelta(content, reasoning);
    }
  }
  get complete() { return this.finishReason === 'stop' || this.finishReason === 'length'; }
}

export function chatMetrics(events, start, end, completed) {
  const duration = Math.max(0, (end - start) / 1000);
  return {
    duration_seconds: duration,
    ttft_seconds: events.firstTokenAt === null ? null : Math.max(0, (events.firstTokenAt - start) / 1000),
    tokens_per_second: completed && events.completionTokens !== null && duration > 0 ? events.completionTokens / duration : null,
    completion_tokens: events.completionTokens,
  };
}

export function mountChat({panel, sidebar = panel, root, node, select, table}) {
  let controller = null, disposed = false;
  const settings = node('section', undefined, {id: 'chat-settings', 'aria-label': 'Chat settings'});
  const model = select('Model', [['smart', 'smart'], ['small', 'small']], 'small', () => {}, settings);
  const thinking = select('Thinking', [['off', 'off'], ['on', 'on']], 'off', () => {}, settings);
  settings.append(node('p', 'Applies to the next prompt. Thinking support depends on the model and provider.'));
  sidebar.append(settings);

  const view = node('div', undefined, {'data-ui': 'chat-view'});
  view.append(node('p', 'No history. Each prompt starts a new request.'));
  const form = node('form', undefined, {'data-ui': 'chat-form'}), label = node('label', 'Message');
  const prompt = node('textarea', undefined, {id: 'chat-prompt', rows: '4', required: '', autocomplete: 'off',
    placeholder: 'Write a message…', 'aria-describedby': 'chat-keyboard'});
  label.append(prompt);
  const actions = node('div', undefined, {'data-ui': 'chat-actions'});
  const send = node('button', 'Send →', {type: 'submit'}), stop = node('button', 'Stop', {type: 'button'});
  stop.disabled = true;
  stop.addEventListener('click', () => controller?.abort());
  actions.append(send, stop, node('small', 'Ctrl / ⌘ + Enter to send', {id: 'chat-keyboard'}));
  form.append(label, actions);
  const response = node('section', undefined, {'data-ui': 'chat-response', 'aria-label': 'Chat response'});
  const progress = node('p', 'Ready.', {role: 'status', 'aria-live': 'polite', 'data-state': 'neutral'});
  const answer = node('pre', 'Send a message to begin.', {id: 'chat-answer', 'aria-label': 'Response text'});
  const thoughts = node('details'), reasoning = node('pre', '', {'aria-label': 'Thinking text'});
  thoughts.hidden = true;
  thoughts.append(node('summary', '[thinking]'), reasoning);
  response.append(node('h3', 'Response'), progress, thoughts, answer);
  const speed = node('section', undefined, {'data-ui': 'chat-speed', 'aria-label': 'Chat speed'});
  speed.hidden = true;
  view.append(form, response, speed);
  panel.append(view);

  prompt.addEventListener('keydown', event => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) {
      event.preventDefault(); form.requestSubmit();
    }
  });
  const number = new Intl.NumberFormat('en', {maximumFractionDigits: 2});
  const metric = (value, unit, factor = 1) => typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? number.format(value * factor) + ' ' + unit : 'unavailable';
  function summary(target, measured, completed, routing) {
    const expected = target?.expected || {};
    speed.hidden = false;
    speed.replaceChildren(node('h3', 'Speed'), table(['Measurement', 'Expected', 'Measured in browser'], [
      ['Tokens / second', metric(expected.tokens_per_second, 'tok/s'), metric(measured.tokens_per_second, 'tok/s')],
      ['Time to first token', metric(expected.ttft_seconds, 'ms', 1000), metric(measured.ttft_seconds, 'ms', 1000)],
    ]), node('p', 'Elapsed: ' + metric(measured.duration_seconds, 's') + ' · completion tokens: '
      + (measured.completion_tokens ?? 'unavailable') + (completed ? '' : ' · incomplete response; throughput unavailable.')),
    node('p', 'Expected: selected endpoint/model calibration. Browser: completion tokens ÷ full request time, including discovery, network and any reasoning tokens. TTFT starts on the first text or thinking delta.'));
    if (routing) speed.append(node('p', 'Selected: ' + routing.modelname + ' @ ' + routing.compute_hostname
      + ' · group: ' + routing.compute_group + ' · request: ' + number.format(routing.request_bytes) + ' bytes.'));
    if (!completed || measured.completion_tokens === null) speed.append(node('p', completed
      ? 'The provider did not report token usage. No token rate can be calculated.'
      : 'Stopped, failed or truncated streams have no completed token rate.'));
    if (expected.tokens_per_second == null || expected.ttft_seconds == null) speed.append(node('p', 'Missing expected values require a completed calibration sample.'));
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (controller || !prompt.value.trim() || disposed) return;
    const current = new AbortController();
    controller = current;
    send.disabled = true; stop.disabled = false;
    model.disabled = true; thinking.disabled = true; prompt.readOnly = true;
    answer.textContent = ''; reasoning.textContent = ''; thoughts.hidden = true; speed.hidden = true;
    progress.textContent = 'Connecting…';
    progress.setAttribute('data-state', 'busy');
    let target = null, reader = null, upstream = null, completed = false, ended = null;
    const events = new ChatEvents((text, thought) => {
      if (disposed) return;
      if (text) answer.append(document.createTextNode(text));
      if (thought) { thoughts.hidden = false; reasoning.append(document.createTextNode(thought)); }
      progress.textContent = 'Streaming…';
    });
    const start = performance.now();
    try {
      upstream = await fetch(root.slice(0, -'/api/v1/compute'.length) + '/v1/chat/completions', {
        method: 'POST', cache: 'no-store', credentials: 'same-origin', signal: current.signal,
        headers: {'Content-Type': 'application/json', Accept: 'text/event-stream'},
        body: JSON.stringify(chatPayload(model.value, thinking.value === 'on', prompt.value)),
      });
      try { target = JSON.parse(upstream.headers.get('X-Compute-Target')); } catch { /* Older servers may omit metadata. */ }
      if (!upstream.ok) {
        let code = 'HTTP ' + upstream.status;
        try { const error = await upstream.json(); code += ' / ' + (error.error?.code || error.code || 'request failed'); } catch { /* Keep the HTTP status. */ }
        throw new Error(code);
      }
      if (upstream.headers.get('content-type')?.split(';')[0].trim() !== 'text/event-stream' || !upstream.body) {
        throw new Error('The proxy did not return an event stream.');
      }
      reader = upstream.body.getReader();
      while (!events.done) {
        const {value, done} = await reader.read();
        if (done) { events.push(new Uint8Array(), true); break; }
        events.push(value);
      }
      ended = performance.now();
      current.signal.throwIfAborted();
      if (!events.complete) throw new Error('The response stream ended without a completed answer.');
      completed = true;
      progress.setAttribute('data-state', 'success');
      const modelname = events.routing?.modelname || target?.model || events.model;
      const server = events.routing?.compute_hostname || target?.endpoint_id;
      if (!disposed) progress.textContent = (events.finishReason === 'length' ? 'Completed · output limit reached' : 'Completed')
        + (modelname ? ' · ' + modelname : '') + (server ? ' · ' + server : '');
    } catch (error) {
      progress.setAttribute('data-state', current.signal.aborted ? 'neutral' : 'error');
      if (!disposed) progress.textContent = current.signal.aborted ? 'Stopped. Partial response retained.' : 'Failed: ' + error.message;
    } finally {
      ended ??= performance.now();
      try { if (reader) await reader.cancel(); else if (upstream?.body && !upstream.body.locked) await upstream.body.cancel(); }
      catch { /* Preserve the original outcome if transport cleanup also fails. */ }
      finally { reader?.releaseLock(); }
      controller = null;
      if (!disposed) {
        send.disabled = false; stop.disabled = true;
        model.disabled = false; thinking.disabled = false; prompt.readOnly = false;
        if (!answer.textContent) answer.textContent = 'No response text returned.';
        summary(target, chatMetrics(events, start, ended, completed), completed, events.routing);
      }
    }
  });
  return {dispose() { disposed = true; controller?.abort(); settings.remove(); }};
}
