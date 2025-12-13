/* Amber WebTUI service views; all dynamic values stay plain text. */
'use strict';
import {mountChat} from './chat.js';
import {mountSpeech} from './audio.js';
import {mountTTS, speechModelKey} from './tts.js';
(() => {
  const $ = id => document.getElementById(id);
  const root = document.body.dataset.prefix;
  const views = ['chat', 'speech', 'tts', 'models', 'monitoring', 'connections', 'configuration'];
  const viewTitle = view => ({chat: 'Chat', speech: 'Speech to text', tts: 'Text to speech',
    models: 'Models', monitoring: 'Requests', connections: 'Connections', configuration: 'Startup configuration'})[view];
  const menuGroups = [...document.querySelectorAll('[data-navigation-group]')];
  const compactNavigation = window.matchMedia('(max-width: 719px)');
  const state = {
    view: 'models', calibration: null, connections: null, service: null,
    fetching: null, mutating: false, timer: null, updated: null, errors: [],
    query: '', group: 'all', sort: 'speed', scope: 'configured', controlsOpen: false, expanded: new Set(), limits: new Map(),
    activity: null, requestLog: null, histogram: null, monitorInstance: null, readVersion: 0,
    interval: 'minute', excludeMonitoring: false, logBefore: null, logCursors: [], logMethod: '', logStatus: '',
    histogramExpanded: false, activeLimit: 100,
    logQuery: '', logSince: '', logUntil: '',
  };
  let parts = {};
  let chatView = null;
  let speechView = null;
  let ttsView = null;
  const number = new Intl.NumberFormat('en', {maximumFractionDigits: 2});
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const format = (value, unit = '') => finite(value) ? number.format(value) + unit : '—';
  const date = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : '—';
  const folded = value => String(value).toLowerCase();

  function node(tag, text, attrs = {}) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
    if (tag === 'input' && attrs.type === 'checkbox') element.setAttribute('is-', 'checkbox');
    if (tag === 'table') element.setAttribute('divide-', 'horizontal');
    return element;
  }
  function button(label, action, parent) {
    const element = node('button', label, {type: 'button'});
    element.addEventListener('click', action);
    parent.append(element);
    return element;
  }
  function status(message, kind = state.errors.length ? 'error' : 'success') {
    $('status').textContent = message;
    $('status').title = message;
    $('status').setAttribute('data-state', kind);
  }
  function stats(items, label) {
    const list = node('dl', undefined, {'data-ui': 'stats', 'aria-label': label});
    for (const [name, value] of items) {
      const item = node('div');
      item.append(node('dt', name), node('dd', value));
      list.append(item);
    }
    return list;
  }
  function table(headings, rows) {
    const wrap = node('div', undefined, {'data-ui': 'table-scroll', tabindex: '0'});
    const table = node('table'), head = node('thead'), line = node('tr'), body = node('tbody');
    for (const heading of headings) line.append(node('th', heading, {scope: 'col'}));
    head.append(line);
    for (const row of rows) {
      const line = node('tr');
      for (const value of row) line.append(node('td', String(value ?? '—')));
      body.append(line);
    }
    table.append(head, body);
    wrap.append(table);
    return wrap;
  }
  async function api(path, method = 'GET', body) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(root + path, {
        method, cache: 'no-store', credentials: 'same-origin', signal: controller.signal,
        headers: body === undefined ? {} : {'Content-Type': 'application/json'},
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const value = await response.json();
      if (!response.ok) throw new Error(value.code || value.error?.code || 'HTTP ' + response.status);
      return value;
    } finally { clearTimeout(timeout); }
  }
  function schedule(delay = 5000) {
    clearTimeout(state.timer);
    if (!document.hidden && !state.mutating) state.timer = setTimeout(refresh, delay);
  }
  function refresh() {
    if (state.fetching) return state.fetching;
    clearTimeout(state.timer);
    $('refresh').disabled = true;
    const version = state.readVersion;
    const reads = [['calibration', '/calibration'], ['connections', '/chat/connections'], ['service', '/status']];
    if (state.view === 'monitoring') {
      const exclude = String(state.excludeMonitoring), log = new URLSearchParams({limit: '50', exclude_monitoring: exclude});
      if (state.logBefore !== null) log.set('before', state.logBefore);
      if (state.logMethod) log.set('method', state.logMethod);
      if (state.logStatus) log.set('status_code', state.logStatus);
      if (state.logQuery) log.set('q', state.logQuery);
      if (state.logSince) log.set('since', new Date(state.logSince + 'Z').toISOString());
      if (state.logUntil) log.set('until', new Date(state.logUntil + 'Z').toISOString());
      reads.push(['activity', '/monitoring/active?limit=' + state.activeLimit + '&exclude_monitoring=' + exclude],
        ['requestLog', '/monitoring/requests?' + log],
        ['histogram', '/monitoring/histogram?interval=' + state.interval + '&exclude_monitoring=' + exclude]);
    }
    state.fetching = Promise.allSettled(reads.map(([, path]) => api(path))).then(results => {
      if (version !== state.readVersion) return;
      state.errors = [];
      results.forEach((result, index) => {
        if (result.status === 'fulfilled') {
          const value = result.value;
          if (value.instance_id) {
            if (state.monitorInstance && state.monitorInstance !== value.instance_id && value.storage !== 'daily-jsonl') {
              state.logBefore = null; state.logCursors = []; state.requestLog = null; state.readVersion += 1;
            }
            state.monitorInstance = value.instance_id;
          }
          state[reads[index][0]] = value;
        }
        else state.errors.push(reads[index][0] + ': ' + (result.reason.name === 'AbortError' ? 'request timed out' : result.reason.message));
      });
      if (results.every(result => result.status === 'fulfilled')) state.updated = new Date();
      draw();
      status(state.errors.length ? 'Refresh failed — last values retained. ' + state.errors.join('; ')
        : 'Updated ' + state.updated.toLocaleTimeString() + ' · snapshots only');
    }).catch(() => status('Unable to display the current snapshots. Use Refresh to try again.', 'error')).finally(() => {
      state.fetching = null;
      $('refresh').disabled = false;
      schedule(version === state.readVersion ? 5000 : 0);
    });
    return state.fetching;
  }
  async function mutate(method, body) {
    if (state.mutating) return;
    state.mutating = true;
    clearTimeout(state.timer);
    drawRun();
    let message, kind = 'success';
    try {
      // Finish older reads before applying a mutation's newer snapshot.
      if (state.fetching) await state.fetching;
      state.calibration = await api('/calibration', method, body);
      message = method === 'POST' ? 'Calibration started.' : 'Calibration cancelled; partial results retained.';
    } catch (error) {
      kind = 'error';
      message = error.name === 'AbortError' || error instanceof TypeError
        ? 'Request outcome uncertain. Check the refreshed run before starting again; the request was not retried.'
        : 'Calibration request failed: ' + error.message;
    } finally {
      await refresh();
      state.mutating = false;
      drawRun();
      schedule();
      status(message + (state.errors.length ? ' Refresh failed; displayed data may be stale.' : ''), kind);
    }
  }

  function endpointConnections(endpoint) {
    return state.connections?.endpoints.find(value => value.id === endpoint.id);
  }
  function endpointModels(endpoint) {
    const items = new Map();
    function item(name) {
      const speech = (state.calibration.speech_models || []).some(value => speechModelKey(value.name) === speechModelKey(name));
      const key = speech ? speechModelKey(name) : folded(name);
      if (!items.has(key)) items.set(key, {name, groups: [], record: null, active: null});
      return items.get(key);
    }
    for (const [group, models] of Object.entries(state.calibration.model_groups)) {
      for (const model of models) item(model.name).groups.push({group, ranking: model.ranking});
    }
    for (const model of state.calibration.speech_models || []) item(model.name).groups.push({group: 'speech'});
    for (const [name, record] of Object.entries(state.calibration.results[endpoint.id] || {})) {
      Object.assign(item(name), {name, record});
      if (record.task === 'speech' && !item(name).groups.some(value => value.group === 'speech')) item(name).groups.push({group: 'speech'});
    }
    const active = endpointConnections(endpoint);
    if (active) {
      for (const model of items.values()) model.active = 0;
      for (const [name, count] of Object.entries(active.models)) {
        Object.assign(item(name), {name, active: count});
      }
    }
    return [...items.values()];
  }
  function matches(model) {
    return folded(model.name).includes(folded(state.query))
      && (state.group === 'all' || model.groups.some(value => value.group === state.group));
  }
  function compare(left, right) {
    if (state.sort === 'speed') {
      const speech = model => model.groups.some(value => value.group === 'speech');
      if (speech(left) !== speech(right)) return speech(left) ? 1 : -1;
      const name = speech(left) ? 'real_time_factor' : 'tokens_per_second';
      const a = left.record?.metrics?.[name], b = right.record?.metrics?.[name];
      if (finite(a) !== finite(b)) return finite(a) ? -1 : 1;
      if (finite(a) && a !== b) return speech(left) ? a - b : b - a;
      const first = speech(left) ? 'ttfa_seconds' : 'ttft_seconds';
      const aFirst = left.record?.metrics?.[first], bFirst = right.record?.metrics?.[first];
      if (finite(aFirst) !== finite(bFirst)) return finite(aFirst) ? -1 : 1;
      if (finite(aFirst) && aFirst !== bFirst) return aFirst - bFirst;
    }
    return left.name.localeCompare(right.name);
  }
  function modelRow(endpoint, model) {
    const row = node('li', undefined, {'data-ui': 'model-row'}), record = model.record;
    const heading = node('div', undefined, {'data-ui': 'model-heading'});
    heading.append(node('h3', model.name));
    const speech = model.groups.some(value => value.group === 'speech');
    for (const {group, ranking} of model.groups) heading.append(node('span', group === 'speech' ? 'speech' : group + ' / ' + format(ranking), {'data-ui': 'badge'}));
    const availability = record?.available === true ? 'available' : record?.available === false ? 'unavailable' : 'not measured';
    heading.append(node('span', availability, {'data-ui': 'badge', 'data-state': record?.available === true ? 'success' : record?.available === false ? 'error' : 'neutral'}));
    if (model.active > 0) heading.append(node('span', model.active + ' active', {'data-ui': 'badge active'}));
    row.append(heading);
    const capabilities = node('div', undefined, {'data-ui': 'capabilities'});
    for (const name of speech ? ['speech'] : ['forms', 'vision', 'thinking']) {
      const supported = record?.capabilities?.[name];
      const meaning = supported === true ? 'demonstrated' : supported === false ? 'not demonstrated' : 'unknown';
      capabilities.append(node('span', name + ': ' + meaning, {title: name + ' fixed probe: ' + meaning,
        'data-state': supported === true ? 'success' : supported === false ? 'error' : 'neutral'}));
    }
    row.append(capabilities, stats([...(speech ? [
      ['Real-time factor', format(record?.metrics?.real_time_factor)],
      ['First audio', finite(record?.metrics?.ttfa_seconds) ? format(record.metrics.ttfa_seconds * 1000, ' ms') : '—'],
      ['Voice', record?.voice || '—'],
    ] : [
      ['Tokens / second', format(record?.metrics?.tokens_per_second, ' tok/s')],
      ['First token', finite(record?.metrics?.ttft_seconds) ? format(record.metrics.ttft_seconds * 1000, ' ms') : '—'],
    ]),
      ['Active requests', format(model.active)],
      ['Last measured', date(record?.finished_at)],
    ], model.name + ' measurements'));
    if (record) {
      const key = endpoint.id + '/' + folded(model.name), detail = node('details');
      detail.open = state.expanded.has(key);
      detail.addEventListener('toggle', () => detail.open ? state.expanded.add(key) : state.expanded.delete(key));
      detail.append(node('summary', '[checks / ' + record.state + ']'));
      detail.append(table(['Probe', 'State', 'Duration', 'Error / HTTP'], Object.entries(record.checks).map(([name, check]) => [
        name, check.state, format(check.duration_seconds, ' s'), [check.error_code, check.http_status].filter(value => value != null).join(' / ') || '—',
      ])));
      if (record.task === 'speech') detail.append(node('p', 'WAV / 24 kHz mono · voices: ' + (record.speech?.voices.join(', ') || 'not measured')
        + ' · audio duration: ' + format(record.metrics?.audio_duration_seconds, ' s')));
      else if (record.metrics) detail.append(node('p', 'Generation rate: ' + format(record.metrics.generation_tokens_per_second, ' tok/s')
        + ' · completion tokens: ' + format(record.metrics.completion_tokens) + ' · token source: ' + record.metrics.token_count_source));
      row.append(detail);
    }
    return row;
  }
  function endpointCard(endpoint, models) {
    const card = node('section', undefined, {'data-ui': 'endpoint', 'aria-label': endpoint.url});
    const header = node('header'), heading = node('h3', endpoint.url);
    const discovery = state.calibration.run?.discovery?.[endpoint.id];
    header.append(heading, node('span', discovery?.error_code || (discovery ? 'discovered' : 'not calibrated'), {'data-ui': 'badge'}));
    const records = Object.values(state.calibration.results[endpoint.id] || {}), active = endpointConnections(endpoint);
    card.append(header, stats([
      ['Available / measured', records.filter(record => record.available === true).length + ' / ' + records.length],
      ['Active requests', format(active?.active_connections)],
      ['Active models', active ? String(Object.keys(active.models).length) : '—'],
    ], endpoint.url + ' summary'));
    const list = node('ul', undefined, {'data-ui': 'model-list'}), limit = state.limits.get(endpoint.id) || 25;
    for (const model of models.slice(0, limit)) list.append(modelRow(endpoint, model));
    if (!models.length) list.append(node('li', 'No models match this filter.'));
    card.append(list);
    if (models.length > limit) button('show next 25 · ' + limit + ' / ' + models.length, () => {
      state.limits.set(endpoint.id, limit + 25);
      drawModels();
    }, card);
    return card;
  }
  function drawModels() {
    if (!parts.models) return;
    const signature = JSON.stringify([state.calibration, state.connections, state.query, state.group, state.sort, [...state.limits]]);
    if (parts.modelsSignature === signature) return;
    parts.modelsSignature = signature;
    const scroll = $('panel').scrollTop;
    parts.models.replaceChildren();
    if (!state.calibration) {
      parts.models.append(node('p', 'Waiting for calibration data. Use [refresh] if the API is unavailable.'));
      return;
    }
    if (!state.calibration.endpoints.length) {
      parts.models.append(node('p', 'No endpoints configured. Add compute.endpoints and smart/small/speech models to the startup configuration, then restart Compute.'));
    }
    for (const endpoint of state.calibration.endpoints) {
      parts.models.append(endpointCard(endpoint, endpointModels(endpoint).filter(matches).sort(compare)));
    }
    $('panel').scrollTop = scroll;
  }
  function drawRun() {
    if (!parts.run) return;
    const run = state.calibration?.run, running = run?.state === 'running';
    parts.start.disabled = state.mutating || !state.calibration?.endpoints.length || running;
    parts.stop.disabled = state.mutating || !running;
    parts.scope.disabled = state.mutating || running;
    const signature = JSON.stringify(run);
    if (parts.runSignature === signature) return;
    parts.runSignature = signature;
    parts.run.replaceChildren();
    if (!run) {
      parts.run.append(node('p', 'No calibration run yet. Configured models are shown with unknown capabilities.'));
      return;
    }
    parts.run.append(node('p', 'Run: ' + run.state + ' · phase: ' + run.phase
      + ' · features: ' + run.features_completed + '/' + run.total_models
      + ' · finished: ' + run.completed_models + '/' + run.total_models));
    if (running) {
      const progress = node('progress', undefined, {'aria-label': 'Calibration progress'});
      if (run.total_models) { progress.max = run.total_models * 2; progress.value = run.features_completed + run.completed_models; }
      parts.run.append(progress);
    }
    if (run.current) parts.run.append(node('p', run.current.model + ' · ' + run.current.step));
    if (run.error_code) parts.run.append(node('p', 'Run error: ' + run.error_code));
    if (run.finished_at) parts.run.append(node('p', 'Finished ' + date(run.finished_at)));
  }
  function drawService() {
    const service = state.service, run = state.calibration?.run;
    parts.service.replaceChildren(stats([
      ['Service', service ? service.ready ? 'ready' : 'not ready' : 'unknown'],
      ['Endpoints', state.calibration ? String(state.calibration.endpoints.length) : '—'],
      ['Active requests', format(state.connections?.active_connections)],
      ['Calibration', run?.state || 'not started'],
    ], 'Compute summary'));
    if (!service) { parts.service.append(node('p', 'Service status unavailable.')); return; }
    parts.service.append(table(['Service', 'Value'], [
      ['Name', service.service], ['Stage', service.stage], ['Authentication', service.authentication],
      ['Protocol', service.protocol_version], ['Implemented', service.implemented.join(', ')], ['Pending', service.pending.join(', ')],
    ]));
    parts.service.append(node('h3', 'Dependencies'));
    parts.service.append(service.dependencies.length ? table(['Name', 'Required', 'Ready'], service.dependencies.map(value => [value.name, value.required, value.ready]))
      : node('p', 'No managed dependencies. Service readiness does not imply model availability.'));
  }
  function requestTarget(record) {
    if (!record.model) return '—';
    const endpoint = state.calibration?.endpoints.find(value => value.id === record.endpoint_id);
    return record.model_group + ' / ' + record.model + (endpoint ? ' @ ' + endpoint.url : '');
  }
  function svgNode(tag, text, attributes = {}) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    if (text !== undefined) element.textContent = text;
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
    return element;
  }
  function drawHistogram() {
    const result = state.histogram;
    parts.histogram.replaceChildren();
    if (!result) { parts.histogram.append(node('p', 'Waiting for histogram data.')); return; }
    const total = result.totals;
    parts.histogram.append(node('p', format(total.requests) + ' finished · ' + format(total.errors) + ' failed · '
      + format(total.cancelled) + ' cancelled / disconnected'));
    const chartWidth = Math.max(320, Math.round(parts.histogram.clientWidth)), right = chartWidth - 20;
    const svg = svgNode('svg', undefined, {viewBox: '0 0 ' + chartWidth + ' 230', role: 'img', 'data-ui': 'request-histogram',
      'aria-label': total.requests + ' finished requests grouped by ' + result.interval + '; ' + total.errors + ' failed and ' + total.cancelled + ' cancelled.'});
    const maximum = Math.max(4, Math.ceil(Math.max(...result.buckets.map(bucket => bucket.requests)) / 4) * 4);
    const width = (right - 50) / result.buckets.length;
    for (let tick = 0; tick <= 4; tick++) {
      const y = 190 - tick * 40;
      svg.append(svgNode('line', undefined, {x1: 50, x2: right, y1: y, y2: y, 'data-ui': 'histogram-grid'}),
        svgNode('text', format(maximum * tick / 4), {x: 42, y: y + 4, 'text-anchor': 'end', 'data-ui': 'histogram-label'}));
    }
    result.buckets.forEach((bucket, index) => {
      const x = 50 + index * width, group = svgNode('g');
      group.append(svgNode('title', bucket.start + ' – ' + bucket.end + ': ' + bucket.requests + ' requests, '
        + bucket.errors + ' failed, ' + bucket.cancelled + ' cancelled' + (!bucket.observed ? ' (before observation)' : bucket.partial ? ' (partial bucket)' : '')));
      let y = 190;
      for (const [kind, count] of [['completed', bucket.requests - bucket.errors - bucket.cancelled], ['failed', bucket.errors], ['cancelled', bucket.cancelled]]) {
        const height = count / maximum * 160;
        y -= height;
        if (count) group.append(svgNode('rect', undefined, {x, y, width: Math.max(.5, width - .5), height,
          'data-ui': 'histogram-' + kind + (bucket.partial ? ' histogram-partial' : '')}));
      }
      group.append(svgNode('rect', undefined, {x, y: 30, width, height: 160, 'data-ui': 'histogram-hit'}));
      svg.append(group);
      if (index === 0 || index === Math.floor(result.buckets.length / 2) || index === result.buckets.length - 1) {
        const label = result.interval === 'day' ? bucket.start.slice(0, 10)
          : result.interval === 'hour' ? bucket.start.slice(5, 16).replace('T', ' ') : bucket.start.slice(11, 16);
        svg.append(svgNode('text', label, {x: index === result.buckets.length - 1 ? right : x, y: 216,
          'text-anchor': index === 0 ? 'start' : index === result.buckets.length - 1 ? 'end' : 'middle', 'data-ui': 'histogram-label'}));
      }
    });
    parts.histogram.append(svg, node('p', 'UTC buckets · completion time · last bucket is partial. No observations before '
      + date(result.observed_since) + '. Hover a bar for counts.'));
    const legend = node('p', undefined, {'data-ui': 'histogram-legend'});
    for (const [kind, label] of [['completed', 'completed'], ['failed', 'failed'], ['cancelled', 'cancelled / disconnected']]) {
      legend.append(node('span', label, {'data-ui': 'legend-' + kind}));
    }
    const details = node('details');
    details.open = state.histogramExpanded;
    details.append(node('summary', '[histogram values]'));
    details.addEventListener('toggle', () => { state.histogramExpanded = details.open; });
    details.append(table(['Start (UTC)', 'Requests', 'Failed', 'Cancelled', 'Coverage'], result.buckets.map(bucket => [
      bucket.start, bucket.requests, bucket.errors, bucket.cancelled, !bucket.observed ? 'not observed' : bucket.partial ? 'partial' : 'full',
    ])));
    parts.histogram.append(legend, details);
  }
  function drawMonitoring() {
    const scroll = $('panel').scrollTop, positions = {};
    for (const key of ['active', 'log', 'histogram', 'service']) {
      positions[key] = [...parts[key].querySelectorAll('[data-ui~="table-scroll"]')].map(element => ({
        left: element.scrollLeft, focused: element === document.activeElement,
      }));
    }
    drawService();
    const activity = state.activity, log = state.requestLog;
    parts.requestSummary.replaceChildren();
    if (activity) parts.requestSummary.append(stats([
      ['Received / process', format(activity.totals.received)], ['Finished / process', format(activity.totals.finished)],
      ['Active / process', format(activity.totals.active)], ['Failed / process', format(activity.totals.errors)],
      ['Cancelled / process', format(activity.totals.cancelled)],
    ], 'Server request totals'));
    parts.active.replaceChildren();
    if (!activity) parts.active.append(node('p', 'Waiting for active request data.'));
    else {
      parts.active.append(node('p', activity.active_count + ' active in this view'
        + (activity.truncated ? ' · showing first ' + activity.requests.length : '')));
      parts.active.append(activity.requests.length ? table(['ID', 'Method / route', 'Phase / HTTP', 'Elapsed', 'Bytes in / out', 'Proxy target'], activity.requests.map(record => [
        '#' + record.id, record.method + ' ' + record.path, record.phase + ' / ' + (record.status_code ?? 'pending'),
        format(record.duration_seconds * 1000, ' ms'), record.request_bytes + ' / ' + record.response_bytes, requestTarget(record),
      ])) : node('p', 'No ongoing API requests in this view.'));
    }
    parts.moreActive.hidden = !activity?.truncated || state.activeLimit >= 1000;
    drawHistogram();
    parts.log.replaceChildren();
    if (!log) parts.log.append(node('p', 'Waiting for request history.'));
    else {
      parts.log.append(node('p', 'Retained ' + log.retained_requests + ' · cached ' + log.cached_requests + ' / ' + log.log_capacity
        + ' · ' + (state.logBefore === null ? 'latest completions' : 'older page') + ' · since ' + date(log.observed_since)));
      if (log.log_status) parts.log.append(node('p', 'Daily log · buffered ' + log.log_status.buffered_entries
        + ' · flush every ' + log.log_status.flush_interval_seconds + ' s · last flush ' + date(log.log_status.last_flush_at)
        + (log.log_status.error ? ' · WRITE FAILED: buffered rows retained' : '')
        + (log.log_status.ignored_lines ? ' · skipped archive lines: ' + log.log_status.ignored_lines : '')));
      parts.log.append(log.requests.length ? table(['Completion', 'Finished', 'Method / route', 'HTTP / state', 'Duration', 'Proxy target', 'Request ID'], log.requests.map(record => [
        '#' + record.completion_id, date(record.finished_at), record.method + ' ' + record.path,
        (record.status_code ?? '—') + ' / ' + record.state, format(record.duration_seconds * 1000, ' ms'), requestTarget(record), record.request_id,
      ])) : node('p', 'No retained requests match the current filters.'));
    }
    parts.older.disabled = !log?.next_before;
    parts.newer.disabled = !state.logCursors.length;
    parts.newest.disabled = state.logBefore === null;
    for (const [key, tables] of Object.entries(positions)) {
      parts[key].querySelectorAll('[data-ui~="table-scroll"]').forEach((element, index) => {
        if (!tables[index]) return;
        element.scrollLeft = tables[index].left;
        if (tables[index].focused) element.focus({preventScroll: true});
      });
    }
    $('panel').scrollTop = scroll;
  }
  function monitoringChanged({resetLog = false} = {}) {
    if (resetLog) { state.logBefore = null; state.logCursors = []; state.requestLog = null; }
    state.readVersion += 1;
    drawMonitoring();
    refresh();
  }
  function mountMonitoring() {
    const parent = parts.content;
    parent.setAttribute('data-ui', 'request-monitoring');
    parent.append(node('p', 'Server-side API traffic, including streams, errors and dashboard reads. Daily logs and historical histograms survive restarts; active requests and process totals reset. No prompts, query strings or credentials are recorded.'));
    const label = node('label', undefined, {'data-ui': 'monitor-toggle'}), exclude = node('input', undefined, {type: 'checkbox'});
    exclude.checked = state.excludeMonitoring;
    exclude.addEventListener('change', () => {
      state.excludeMonitoring = exclude.checked; state.activity = null; state.histogram = null;
      monitoringChanged({resetLog: true});
    });
    label.append(exclude, node('span', 'Hide monitoring traffic in tables and histogram'));
    parts.requestSummary = node('div');
    $('context-panel').append(label);
    parent.append(parts.requestSummary, node('h3', 'Active API requests'));
    parts.active = node('div');
    parent.append(parts.active);
    parts.moreActive = button('show more active requests', () => {
      state.activeLimit = Math.min(1000, state.activeLimit + 100); monitoringChanged();
    }, parent);
    const section = node('section', undefined, {'data-ui': 'monitor-section', 'aria-label': 'Request histogram'});
    section.append(node('h3', 'Request histogram'));
    select('Bucket size / history window', [['minute', 'Minute / last hour'], ['ten_minutes', '10 min / last day'],
      ['hour', 'Hour / last week'], ['day', 'Day / last 30 days']], state.interval, value => {
      state.interval = value; state.histogram = null; monitoringChanged();
    }, $('context-panel'));
    parts.histogram = node('div');
    section.append(parts.histogram);
    parent.append(section, node('h3', 'API request log'));
    const filters = node('div', undefined, {'data-ui': 'model-filters'});
    select('Method', [['', 'All methods'], ...['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'].map(value => [value, value])], state.logMethod,
      value => { state.logMethod = value; monitoringChanged({resetLog: true}); }, filters);
    select('HTTP status', [['', 'All statuses'], ...[200, 202, 204, 400, 401, 403, 404, 409, 422, 429, 500, 502, 503, 504].map(value => [String(value), String(value)])], state.logStatus,
      value => { state.logStatus = value; monitoringChanged({resetLog: true}); }, filters);
    const search = node('form', undefined, {'data-ui': 'model-filters', 'aria-label': 'Search request archive'});
    const queryLabel = node('label', 'Find in request metadata'), query = node('input', undefined, {type: 'search', maxlength: '256'});
    query.value = state.logQuery;
    queryLabel.append(query);
    const sinceLabel = node('label', 'From (UTC)'), since = node('input', undefined, {type: 'datetime-local'});
    since.value = state.logSince; sinceLabel.append(since);
    const untilLabel = node('label', 'Until (UTC, exclusive)'), until = node('input', undefined, {type: 'datetime-local'});
    until.value = state.logUntil; untilLabel.append(until);
    const submit = node('button', '[search]', {type: 'submit'});
    search.append(queryLabel, sinceLabel, untilLabel, submit);
    search.addEventListener('submit', event => {
      event.preventDefault();
      if (since.value && until.value && since.value > until.value) {
        status('The end of the search range must not precede its start.', 'error'); return;
      }
      state.logQuery = query.value.trim(); state.logSince = since.value; state.logUntil = until.value;
      monitoringChanged({resetLog: true});
    });
    button('clear search', () => {
      state.logQuery = query.value = ''; state.logSince = since.value = ''; state.logUntil = until.value = '';
      monitoringChanged({resetLog: true});
    }, search);
    parts.log = node('div');
    $('context-panel').append(filters);
    parent.append(search, parts.log);
    const paging = node('div', undefined, {'data-ui': 'log-paging'});
    parts.newest = button('newest', () => { state.logBefore = null; state.logCursors = []; state.requestLog = null; monitoringChanged(); }, paging);
    parts.newer = button('newer', () => { state.logBefore = state.logCursors.pop() ?? null; state.requestLog = null; monitoringChanged(); }, paging);
    parts.older = button('older', () => {
      if (!state.requestLog?.next_before) return;
      state.logCursors.push(state.logBefore); state.logBefore = state.requestLog.next_before; state.requestLog = null; monitoringChanged();
    }, paging);
    const service = node('details');
    parts.service = node('div');
    service.append(node('summary', '[service details]'), parts.service);
    parent.append(paging, service);
  }
  function drawConnections() {
    parts.content.replaceChildren(node('p', 'Shared Chat / TTS / calibration reservations, including requests waiting for headers. Counts are local to this process; idle sockets and discovery are excluded.'));
    if (!state.connections) { parts.content.append(node('p', 'Connection data unavailable.')); return; }
    parts.content.append(stats([['Total active requests', format(state.connections.active_connections)]], 'Connection summary'));
    if (!state.connections.endpoints.length) parts.content.append(node('p', 'No endpoints configured.'));
    for (const endpoint of state.connections.endpoints) {
      const section = node('section', undefined, {'data-ui': 'endpoint'}), header = node('header');
      header.append(node('h3', endpoint.url), node('span', endpoint.active_connections + ' active', {'data-ui': 'badge'}));
      section.append(header, Object.keys(endpoint.models).length ? table(['Model', 'Active requests'], Object.entries(endpoint.models))
        : node('p', 'No active requests.'));
      parts.content.append(section);
    }
  }
  function drawConfiguration() {
    parts.content.replaceChildren(node('p', 'Startup configuration · read only. Change the JSON configuration or environment and restart Compute to apply changes.'));
    if (!state.calibration) { parts.content.append(node('p', 'Configuration data unavailable.')); return; }
    parts.content.append(node('h3', 'Endpoints'), table(['API base URL', 'API key'], state.calibration.endpoints.map(endpoint => [endpoint.url, endpoint.key_configured ? 'configured' : 'not configured'])));
    for (const [group, models] of Object.entries(state.calibration.model_groups)) {
      parts.content.append(node('h3', group), models.length ? table(['Model', 'Ranking'], models.map(model => [model.name, model.ranking]))
        : node('p', 'No models configured in this group.'));
    }
    const speech = state.calibration.speech_models || [];
    parts.content.append(node('h3', 'speech'), speech.length ? table(['Model', 'Default voice'], speech.map(model => [model.name, model.voice]))
      : node('p', 'No speech models configured.'));
    parts.content.append(node('p', 'Routing: lowest endpoint load first. smart uses highest ranking, then speed; small uses speed. Speed compares throughput, then TTFT. A running model from the requested group is reused when possible.'));
    parts.content.append(node('p', 'Speech uses the same endpoint counters, then the lowest measured real-time factor and time to first audio for the selected model / voice.'));
  }
  function draw() {
    $('service-state').textContent = state.service ? state.service.ready ? 'Service ready' : 'Service not ready' : 'Service status unknown';
    $('service-state').setAttribute('data-state', state.service ? state.service.ready ? 'success' : 'error' : 'neutral');
    $('endpoint-count').textContent = state.calibration ? state.calibration.endpoints.length + ' configured endpoint' + (state.calibration.endpoints.length === 1 ? '' : 's') : 'Waiting for snapshots';
    if (state.view === 'models') { drawRun(); drawModels(); }
    else if (state.view === 'monitoring') drawMonitoring();
    else if (state.view === 'connections') drawConnections();
    else if (state.view === 'configuration') drawConfiguration();
    else if (state.view === 'tts') ttsView?.update(state.calibration);
  }
  function select(label, values, selected, change, parent) {
    const wrapper = node('label', label), input = node('select');
    for (const [value, text] of values) input.append(node('option', text, {value}));
    input.value = selected;
    input.addEventListener('change', () => change(input.value));
    wrapper.append(input);
    parent.append(wrapper);
    return input;
  }
  function mount() {
    chatView?.dispose();
    chatView = null;
    speechView?.dispose();
    speechView = null;
    ttsView?.dispose();
    ttsView = null;
    state.readVersion += 1;
    state.view = views.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'models';
    $('view-title').textContent = viewTitle(state.view);
    const group = ['chat', 'speech', 'tts'].includes(state.view) ? 'services'
      : state.view === 'configuration' ? 'configuration' : 'monitoring';
    const page = state.view === 'monitoring' ? 'requests' : state.view === 'configuration' ? 'startup' : state.view;
    $('view-path').textContent = 'compute / ' + group + ' / ' + page;
    for (const menu of menuGroups) {
      menu.open = false;
      menu.toggleAttribute('data-current', menu.dataset.navigationGroup === group);
    }
    for (const link of $('application-navigation').querySelectorAll('a[data-view]')) {
      if (link.dataset.view === state.view) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    $('panel').replaceChildren();
    $('context-panel').replaceChildren();
    const contextTitle = ({chat: 'Chat options', speech: 'Audio constraints', tts: 'Speech options',
      models: 'Model filters', monitoring: 'Request filters', connections: 'Connection context',
      configuration: 'Configuration context'})[state.view];
    $('context-title').textContent = $('context-summary').textContent = contextTitle;
    $('context-details').open = !compactNavigation.matches;
    parts = {};
    if (state.view === 'chat') {
      chatView = mountChat({panel: $('panel'), sidebar: $('context-panel'), root, node, select, table});
    } else if (state.view === 'speech') {
      speechView = mountSpeech({panel: $('panel'), root, node});
      $('context-panel').append(stats([['Local model', 'Whisper / tiny'], ['Maximum file', '25 MiB'],
        ['Maximum recording', '3 minutes'], ['Language', 'Original spoken language']], 'Transcription constraints'),
        node('p', 'Choose a file or record in the workspace. The microphone requires HTTPS or localhost and explicit permission.'));
    } else if (state.view === 'tts') {
      ttsView = mountTTS({panel: $('panel'), sidebar: $('context-panel'), root, node, table, snapshot: state.calibration});
    } else if (state.view === 'models') {
      const controls = node('section', undefined, {'data-ui': 'calibration-controls', 'aria-label': 'Calibration controls', 'box-': 'square'});
      controls.append(node('h3', 'Endpoint calibration'));
      parts.run = node('div', undefined, {'aria-live': 'polite'});
      const controlsDetail = node('details');
      controlsDetail.open = state.controlsOpen;
      controlsDetail.addEventListener('toggle', () => { state.controlsOpen = controlsDetail.open; });
      controlsDetail.append(node('summary', '[start / cancel calibration]'));
      const toolbar = node('div', undefined, {'data-ui': 'calibration-toolbar'});
      parts.scope = select('Scope', [['configured', 'Configured chat / speech'], ['all', 'All advertised models']], state.scope,
        value => { state.scope = value; }, toolbar);
      parts.start = button('start calibration', () => mutate('POST', {scope: state.scope}), toolbar);
      parts.start.setAttribute('data-primary', '');
      parts.stop = button('cancel run', () => mutate('DELETE'), toolbar);
      controlsDetail.append(toolbar, node('p', 'Starting replaces previous measurements and makes inference calls to configured providers; charges may apply.'));
      controls.append(parts.run, controlsDetail);
      const filters = node('div', undefined, {'data-ui': 'model-filters'}), searchLabel = node('label', 'Find model');
      const search = node('input', undefined, {type: 'search', placeholder: 'Model name', autocomplete: 'off'});
      search.value = state.query;
      search.addEventListener('input', () => { state.query = search.value; state.limits.clear(); drawModels(); });
      searchLabel.append(search);
      filters.append(searchLabel);
      select('Group', [['all', 'All groups'], ['smart', 'smart'], ['small', 'small'], ['speech', 'speech']], state.group,
        value => { state.group = value; state.limits.clear(); drawModels(); }, filters);
      select('Sort', [['name', 'Model name'], ['speed', 'Fastest measured']], state.sort,
        value => { state.sort = value; drawModels(); }, filters);
      parts.models = node('div', undefined, {id: 'endpoints'});
      $('context-panel').append(filters);
      $('panel').append(controls, node('p', 'Capabilities: demonstrated / not demonstrated / unknown. Chat throughput includes TTFT. Speech uses first-audio latency and real-time factor (lower is faster); — means no measurement.'), parts.models);
    } else {
      parts.content = node('div');
      $('panel').append(parts.content);
      if (state.view === 'monitoring') mountMonitoring();
      else if (state.view === 'connections') $('context-panel').append(node('p', 'Shared reservations for Chat, speech synthesis and calibration.'),
        node('p', 'Counters are local to this process. Idle sockets and model discovery are excluded.'));
      else $('context-panel').append(node('p', 'Startup settings / read only'),
        node('p', 'Edit the JSON configuration or environment and restart Compute to apply changes. API key values are never displayed.'));
    }
    draw();
    if (state.view === 'monitoring') refresh();
  }

  for (const menu of menuGroups) {
    menu.addEventListener('toggle', () => {
      if (menu.open) for (const other of menuGroups) if (other !== menu) other.open = false;
    });
  }
  document.addEventListener('click', event => {
    if (!$('application-navigation').contains(event.target) || event.target.closest('a')) {
      for (const menu of menuGroups) menu.open = false;
    }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') for (const menu of menuGroups) if (menu.open) {
      menu.open = false; menu.querySelector('summary').focus(); event.preventDefault();
    }
  });
  compactNavigation.addEventListener('change', () => { $('context-details').open = !compactNavigation.matches; });
  $('refresh').addEventListener('click', refresh);
  $('skip-navigation').addEventListener('click', event => {
    event.preventDefault(); $('panel').focus();
  });
  window.addEventListener('hashchange', () => { mount(); $('panel').focus({preventScroll: true}); });
  document.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.isComposing || event.key !== 'Enter' || !(event.ctrlKey || event.metaKey)) return;
    const form = event.target.closest('form') || ($('context-panel').contains(event.target) ? $('panel').querySelector('form') : null);
    if (form && $('panel').contains(form) && !form.querySelector('button[type="submit"]:disabled')) {
      event.preventDefault(); form.requestSubmit();
    }
  });
  document.addEventListener('visibilitychange', () => document.hidden ? clearTimeout(state.timer) : refresh());
  mount();
  refresh();
})();
