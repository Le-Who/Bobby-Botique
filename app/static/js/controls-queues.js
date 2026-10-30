/* Revision-owned queue commands; server metadata only, never job payloads. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  if (!byId('queues')) return;
  const node = (tag, className, text) => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = String(text);
    return item;
  };
  const number = value => Number.isFinite(value) ? String(value) : '—';
  let active = location.hash === '#queues', pendingRead = null, busy = false, needsRefresh = false;
  let selectedJob = null, pendingCommand = null, cursor = null, nextCursor = null, selectionSequence = 0;
  let refreshSequence = 0;
  let openingJob = null;
  let laneData = {}, loadData = {}, sourceNotes = [];
  function status(text, error = false) {
    byId('queue-status').textContent = text;
    byId('queue-status').className = error ? 'feedback error' : 'muted';
  }
  function sourceText(source) {
    if (!source) return 'источник неизвестен';
    const ageSeconds = Number.isFinite(source.observed_at) ? Math.max(0, Math.round(Date.now() / 1000 - source.observed_at)) : null;
    const age = ageSeconds === null ? 'возраст неизвестен' : `${ageSeconds} с`;
    return `${source.available === false ? 'недоступно' : source.stale || ageSeconds > 60 ? 'устарело' : 'наблюдение'}; ${source.scope || 'scope неизвестен'}; ${age}${source.scope === 'process' ? '; только этот экземпляр' : ''}`;
  }
  function sourceNote(source) {
    const item = node('p', 'muted queue-source-age', sourceText(source));
    sourceNotes.push([item, source]);
    return item;
  }
  async function read(url) {
    return window.AdminApi.adminRequest(url);
  }
  function commandButton(text, action, handler) {
    const button = node('button', 'button secondary', text);
    button.type = 'button'; button.dataset.action = action;
    button.disabled = busy || needsRefresh || Boolean(pendingCommand);
    button.addEventListener('click', handler);
    return button;
  }
  function renderDetail() {
    const holder = byId('queue-job-detail');
    holder.hidden = !selectedJob;
    if (!selectedJob) return;
    const job = selectedJob, summary = job.result_summary || {};
    holder.replaceChildren(node('h3', 'technical', job.task_id), node('p', 'metadata', `Ревизия ${job.revision}; ${job.scope}; ${job.kind}; ${job.lane}`),
      node('p', '', `Состояние: ${job.state}`), node('p', 'muted', `Попытки: ${number(job.attempts_started)} / ${number(job.max_attempts)}; возраст: ${number(Math.round(job.age_seconds))} с`));
    if (job.state === 'cancel_requested') holder.append(node('p', 'notice', 'Отмена запрошена. Выполнение ещё не подтверждено как cancelled.'));
    if (job.state === 'unknown_delivery') holder.append(node('p', 'notice', 'Результат внешней отправки неизвестен. Автоматический повтор может создать дубликат; повтор здесь недоступен.'));
    if (job.reason_code) holder.append(node('p', 'technical muted', job.reason_code));
    const summaryList = node('dl', 'queue-summary');
    for (const [key, value] of Object.entries(summary)) summaryList.append(node('dt', '', key), node('dd', 'technical', value));
    holder.append(summaryList);
    const actions = node('div', 'actions');
    if (job.capabilities?.cancel && ['queued', 'delayed', 'running'].includes(job.state)) {
      actions.append(commandButton('Запросить отмену', 'cancel', () => startCommand('cancel', job)));
    }
    if (job.capabilities?.retry && ['failed', 'expired'].includes(job.state)) {
      actions.append(commandButton('Создать повторное задание', 'retry', () => startCommand('retry', job)));
    }
    const refresh = node('button', 'button quiet', 'Обновить это задание');
    refresh.type = 'button'; refresh.id = 'queue-job-refresh'; refresh.disabled = busy;
    refresh.addEventListener('click', async () => { try { await openJob(job.task_id); needsRefresh = false; renderDetail(); status('Задание обновлено из backend.'); } catch (_) { status('Задание недоступно; прежняя ревизия не подтверждена.', true); } });
    actions.append(refresh); holder.append(actions);
  }
  function renderLanes() {
    const filter=byId('queue-lane-filter');
    for(const name of Object.keys(laneData)) {
      if(![...filter.options].some(option=>option.value===name)) filter.add(new Option(name,name));
    }
    sourceNotes = [];
    const holder = byId('queue-lanes'); holder.replaceChildren();
    for (const [name, lane] of Object.entries(laneData)) {
      const card = node('article', 'control-card queue-lane'); card.dataset.lane = name;
      card.append(node('h3', 'technical', name), sourceNote(lane));
      if (lane.available === false) { card.append(node('p', 'notice', 'Источник очереди недоступен. Значения и команды неизвестны.')); holder.append(card); continue; }
      const facts = node('dl', 'queue-facts');
      for (const [label, value] of [['Ожидают', lane.queued], ['Запущены', lane.running], ['Отложены', lane.delayed], ['Повторные попытки', lane.retrying],
        ['Возраст старейшего, с', lane.oldest_age_seconds], ['Заданий / предел', `${number(lane.nonterminal)} / ${number(lane.policy?.max_jobs)}`],
        ['Payload bytes / предел', `${number(lane.payload_bytes)} / ${number(lane.policy?.max_total_payload_bytes)}`]]) facts.append(node('dt', '', label), node('dd', '', typeof value === 'string' ? value : number(value)));
      card.append(facts, node('p', 'metadata', `Ревизия ${lane.revision}. Пределы доступны только для чтения.`));
      const checks = {};
      for (const [key, label, value] of [['admission', 'Пауза приёма новых заданий', lane.admission_paused], ['claim', 'Пауза запуска ожидающих заданий', lane.claim_paused]]) {
        const wrap = node('label', 'check'), input = node('input'); input.type = 'checkbox'; input.dataset.pause = key; input.checked = value === true;
        input.disabled = busy || needsRefresh || Boolean(pendingCommand) || !lane.capabilities?.set_state;
        checks[key] = input; wrap.append(input, document.createTextNode(label)); card.append(wrap);
      }
      const save = commandButton('Применить режим очереди', 'set-state', () => {
        const message = `Очередь ${name}: ожидают ${number(lane.queued)}, отложены ${number(lane.delayed)}, запущены ${number(lane.running)}.\nПриём новых заданий: ${checks.admission.checked ? 'пауза' : 'включён'}. Запуск ожидающих: ${checks.claim.checked ? 'пауза' : 'включён'}.\nУже запущенные задачи продолжают работу. Применить к ревизии ${lane.revision}?`;
        if (!confirm(message)) return;
        sendNew(`/api/controls/queues/${encodeURIComponent(name)}/state`, { expected_revision: lane.revision,
          admission_paused: checks.admission.checked, claim_paused: checks.claim.checked }, 'lane', name);
      });
      if (!lane.capabilities?.set_state) save.disabled = true;
      card.append(save); holder.append(card);
    }
    if (!holder.children.length) holder.append(node('p', 'notice', 'Источник очередей недоступен.'));
    const load = byId('queue-load'); load.replaceChildren();
    const fields = ['buffered', 'outstanding', 'running', 'accepted_total', 'rejected_total', 'waiters', 'active', 'capacity', 'jobs', 'bytes'];
    for (const [name, source] of Object.entries(loadData)) {
      const item = node('article', 'queue-load-item'); item.dataset.source = name;
      item.append(node('h3', 'technical', name), sourceNote(source));
      if (source.available !== false) item.append(node('p', 'technical', fields.filter(key => source[key] !== undefined).map(key => `${key}: ${number(source[key])}`).join('; ')));
      load.append(item);
    }
  }
  function renderPage(page) {
    const body = byId('queue-jobs'); body.replaceChildren();
    for (const job of page.items || []) {
      const row = node('tr'), identity = node('td'), button = node('button', 'button quiet technical', job.task_id);
      button.type = 'button'; button.dataset.job = job.task_id;
      button.addEventListener('click', () => openJob(job.task_id).catch(() => status('Задание недоступно.', true)));
      identity.append(button, node('div', 'muted', job.kind)); row.append(identity);
      row.append(node('td', '', `${job.lane} / ${job.result_summary?.date || '—'}`), node('td', 'technical', job.state),
        node('td', '', `${number(Math.round(job.age_seconds))} с / ${number(job.attempts_started)} из ${number(job.max_attempts)}`),
        node('td', 'technical', `${job.result_summary?.actual_model || 'факт неизвестен'} / ${job.result_summary?.settings_revision ?? job.result_summary?.effective_revision ?? '—'}`));
      body.append(row);
    }
    if (!body.children.length) { const row = node('tr'), cell = node('td', 'muted', 'Задания для этих фильтров не найдены.'); cell.colSpan = 5; row.append(cell); body.append(row); }
    nextCursor = page.next_cursor || null; byId('queue-next').disabled = !nextCursor;
  }
  async function openJob(identity) {
    const sequence = ++selectionSequence;
    openingJob=sequence;
    try {
      const response = await read(`/api/controls/jobs/${encodeURIComponent(identity)}`);
      if (sequence !== selectionSequence) return;
      if (!response.job) throw new Error('job_unavailable');
      selectedJob = response.job; renderDetail();
    } finally {if(openingJob===sequence) openingJob=null;}
  }
  async function refresh() {
    const sequence=++refreshSequence, selection=selectionSequence;
    const canRefreshSelection=openingJob===null;
    const identity = selectedJob?.task_id;
    pendingRead = (async () => {
      byId('queue-refresh').disabled = true;
      const params = new URLSearchParams({ limit: '50' });
      if (byId('queue-lane-filter').value) params.set('lane', byId('queue-lane-filter').value);
      if (byId('queue-state-filter').value) params.set('state', byId('queue-state-filter').value);
      if (cursor) params.set('cursor', cursor);
      const [overview, page] = await Promise.allSettled([read('/api/controls/queues'), read(`/api/controls/jobs?${params}`)]);
      if(sequence!==refreshSequence) return;
      let failed = false;
      if (overview.status === 'fulfilled') { laneData = overview.value.queue?.lanes || {}; loadData = overview.value.load || {}; }
      else { failed = true; for (const lane of Object.values(laneData)) lane.capabilities = { set_state: false }; }
      if (page.status === 'fulfilled') renderPage(page.value); else failed = true;
      if (canRefreshSelection && identity && selectedJob?.task_id === identity && selection===selectionSequence) { try { await openJob(identity); } catch (_) { failed = true; } }
      if(sequence!==refreshSequence) return;
      needsRefresh = failed;
      renderLanes(); renderDetail();
      status(failed ? 'Часть источников недоступна. Прежние значения не подтверждены; команды заблокированы до обновления.' : 'Состояние получено. Общие Redis и локальные счётчики показаны раздельно.', failed);
    })().finally(() => { if(sequence===refreshSequence) {pendingRead = null; byId('queue-refresh').disabled = false;} });
    return pendingRead;
  }
  function operationId() {
    return window.AdminApi.newOperationId();
  }
  function startCommand(action, job) {
    if (!confirm(`${action === 'cancel' ? 'Запросить отмену' : 'Создать новое повторное задание'} ${job.task_id}?\nОчередь ${job.lane}; состояние ${job.state}; ревизия ${job.revision}. Уже начатая внешняя отправка не откатывается.`)) return;
    sendNew(`/api/controls/jobs/${encodeURIComponent(job.task_id)}/${action}`, { expected_revision: job.revision }, action, job.task_id);
  }
  function sendNew(url, body, action, target) {
    if (busy || needsRefresh || pendingCommand) return;
    pendingCommand = { url, body: { ...body, operation_id: operationId() }, action, target };
    return sendCommand();
  }
  async function sendCommand() {
    if (busy || !pendingCommand) return;
    busy = true; renderDetail(); renderLanes(); byId('queue-reconcile').hidden = true;
    const command = pendingCommand;
    try {
      const response = await window.AdminApi.adminFetch(command.url, { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content },
        body: JSON.stringify(command.body) });
      const result = await response.json();
      if ([400, 401, 403].includes(response.status)) {
        pendingCommand = null; needsRefresh = true;
        status('Команда отклонена до выполнения. Проверьте доступ и обновите страницу перед новой командой.', true);
      } else if (response.status === 409) {
        pendingCommand = null; needsRefresh = true;
        status('Состояние изменилось или команда недоступна. Открытое задание сохранено; обновите данные и проверьте ревизию.', true);
      } else if (!response.ok || result.confirmed !== true) {
        status('Команда не подтверждена. Проверьте результат той же команды; новая команда может создать дубликат.', true);
      } else {
        pendingCommand = null;
        if (result.job) selectedJob = result.job;
        if (result.source_job) selectedJob = result.source_job;
        if (result.lane) laneData[command.target] = { ...laneData[command.target], ...result.lane };
        if (result.admission) {
          if (!result.source_job) needsRefresh = true;
          status(`Повтор подтверждён backend: ${result.admission.task_id}. Исходное задание не переотправляется.`);
        } else status('Backend подтвердил состояние. Для нового наблюдения обновите данные.');
      }
    } catch (_) { status('Ответ команды не получен. Проверьте результат с прежним operation_id.', true); }
    finally {
      busy = false; renderDetail(); renderLanes(); byId('queue-reconcile').hidden = !pendingCommand;
    }
  }
  byId('queue-refresh').addEventListener('click', refresh);
  byId('queue-reconcile').addEventListener('click', sendCommand);
  byId('queue-next').addEventListener('click', () => { cursor = nextCursor; refresh(); });
  for (const identity of ['queue-lane-filter', 'queue-state-filter']) byId(identity).addEventListener('change', () => { cursor = null; refresh(); });
  byId('queue-find-job').addEventListener('submit', event => { event.preventDefault(); const identity = byId('queue-job-search').value.trim(); if (/^[A-Za-z0-9_-]{1,128}$/.test(identity)) openJob(identity).catch(() => status('Задание не найдено или источник недоступен.', true)); });
  document.addEventListener('controls:sectionchange', event => { active = event.detail === 'queues'; if (active && !Object.keys(laneData).length) refresh(); });
  if (active) refresh();
  setInterval(() => {
    if (active && !document.hidden) for (const [item, source] of sourceNotes) item.textContent = sourceText(source);
  }, 1000);
})();
