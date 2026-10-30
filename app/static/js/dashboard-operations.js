/* Owner-emitted timing samples and process load; never infer missing values as 0. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  if (!byId('operations-stages')) return;
  const node = (tag, text, className = '') => {
    const item = document.createElement(tag); item.textContent = String(text);
    if (className) item.className = className;
    return item;
  };
  const number = value => Number.isFinite(value) ? String(value) : '—';
  const seconds = value => Number.isFinite(value) ? value.toFixed(3) : '—';
  function render(data) {
    const body = byId('operations-stages'); body.replaceChildren();
    if (!data || !Array.isArray(data.stages)) {
      const row = node('tr', ''), cell = node('td', 'Stage observations unavailable'); cell.colSpan = 7; row.append(cell); body.append(row);
    } else {
      for (const stage of data.stages) {
        const row = node('tr', ''), available = stage.available === true;
        row.append(node('td', `${stage.stage} / ${stage.workload}`));
        for (const name of ['p50_seconds', 'p95_seconds', 'p99_seconds']) row.append(node('td', available ? seconds(stage[name]) : 'Unavailable'));
        row.append(node('td', available ? `${number(stage.count)}${stage.low_sample ? ' · low sample' : ''}` : 'Unavailable', stage.low_sample ? 'low-sample' : ''),
          node('td', `${number(stage.window_seconds)} s`), node('td', stage.scope || 'unknown'));
        body.append(row);
      }
      if (!data.stages.length) { const row = node('tr', ''), cell = node('td', 'No stage samples in this process window'); cell.colSpan = 7; row.append(cell); body.append(row); }
    }
    const amplification = data?.amplification;
    const coverage = amplification?.coverage === 'owned_routed_and_direct_generation_scopes'
      ? 'Owned routed and direct generation scopes; detached jobs excluded' : 'Coverage details unavailable';
    byId('operations-amplification').textContent = amplification?.available === true
      ? `${seconds(amplification.ratio).replace(/0$/, '')} API attempts / admitted generation · ${number(amplification.api_attempts)} attempts / ${number(amplification.accepted_generation_requests)} admissions · ${number(amplification.window_seconds)} s · ${amplification.scope} · ${coverage}`
      : 'Generation amplification unavailable';
    const completed = data?.completed_generation_wait_seconds_per_window_second;
    byId('operations-generation-wait').textContent = completed
      ? `Completed generation wait / observed window: ${seconds(completed.value)} s/s · ${number(completed.window_seconds)} s · ${completed.scope}. Completed owned iterator waits only; unfinished work and delivery waits are excluded.`
      : 'Completed generation wait rate unavailable';
    byId('operations-races').textContent = (data?.races || []).map(row => `${row.model} / ${row.workload}: ${row.outcome} ${number(row.count)} (${row.kind}, ${row.scope}, ${number(row.window_seconds)} s)`).join('; ') || 'No measured race outcomes in this process window.';
    const load = byId('operations-load'); load.replaceChildren();
    const names = { ingress: ['buffered', 'outstanding', 'capacity'], task_manager: ['outstanding', 'running', 'accepted_total', 'rejected_total', 'capacity'],
      heavy: ['waiters', 'active', 'capacity'], ultra: ['waiters', 'active', 'capacity'], tts: ['jobs', 'bytes', 'running', 'queued'] };
    for (const [name, fields] of Object.entries(names)) {
      const source = data?.load?.[name], item = node('article', ''); item.dataset.source = name;
      item.append(node('h3', name));
      const age = Number.isFinite(source?.observed_at) ? `${Math.max(0, Math.round(Date.now() / 1000 - source.observed_at))} s old` : 'observation age unknown';
      item.append(node('p', `${source?.scope || 'scope unknown'} · ${age}`));
      if (source?.available !== true) item.append(node('p', 'Unavailable', 'unavailable'));
      else {
        item.append(node('p', fields.map(field => `${field}: ${number(source[field])}`).join(' · ')));
        if (source.modes) item.append(node('p', `distributed: ${number(source.modes.distributed)} · local fallback: ${number(source.modes.local_fallback)}${source.degraded ? ' · degraded' : ''}`));
      }
      load.append(item);
    }
  }
  globalThis.GemAIOperations = { render };
  if (globalThis.GemAIOperationsData) render(globalThis.GemAIOperationsData);
})();
