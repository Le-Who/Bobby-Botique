/* Broadcast admission and observed delivery receipts. Never retries a mutation. */
(function (root) {
  'use strict';
  const PREFIX = '/api/admin/broadcast';
  const KEY = 'admin-broadcast-batches-v1';
  const MAX = 10;
  const records = new Map();
  const commands = new Map();
  const retries = new Map();
  const reads = new Set();
  const filters = ['channel', 'date', 'state', 'reason'];
  let initialized = false, timer = null, polling = false, stopped = false, delay = 2000;
  let journalCursor = null, nextCursor = null, cursors = [], journalVersion = 0, journalQuery = null;
  const journalRead = () => root.AdminApi.createLatestRequest();
  let latestJournal;
  const api = (...args) => root.AdminApi.adminRequest(...args);
  const el = id => document.getElementById(id);
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0;
  const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
  const finished = batch => batch?.state === 'finished';
  const completed = batch => ['sent', 'failed', 'skipped', 'unknown_delivery'].reduce((sum, key) => sum + count(batch?.[key]), 0);
  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  }
  function notify(message, kind = 'info') { root.showToast ? root.showToast(message, kind) : showToast(message, kind); }
  function persist() {
    const entries = [...records.values()].slice(-MAX).map(record => ({ operation_id: record.operation, batch_id: record.batch?.batch_id || record.batchId || null }));
    const savedFilters = Object.fromEntries(filters.map(name => [name, el(`broadcast-journal-${name}`)?.value || '']));
    try { sessionStorage.setItem(KEY, JSON.stringify({ operations: entries, filters: savedFilters })); } catch (_) { /* Storage may be disabled. */ }
  }
  function restore() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(KEY) || '{}');
      for (const item of (Array.isArray(saved.operations) ? saved.operations.slice(-MAX) : [])) {
        if (uuid(item.operation_id)) records.set(item.operation_id, { operation: item.operation_id, batchId: uuid(item.batch_id) ? item.batch_id : null, batch: null, unavailable: false });
      }
      for (const name of filters) {
        const value = saved.filters?.[name];
        if (typeof value === 'string' && value.length <= 64 && el(`broadcast-journal-${name}`)) el(`broadcast-journal-${name}`).value = value;
      }
    } catch (_) { /* Ignore invalid or unavailable browser storage. */ }
  }
  function admitSlot(operation) {
    if (records.size >= MAX) {
      const removable = [...records.values()].find(record => finished(record.batch));
      if (!removable) throw new Error('Уже отслеживается 10 незавершённых операций. Дождитесь результата.');
      records.delete(removable.operation);
    }
    const record = { operation, batch: null, unavailable: false, admission: null };
    records.set(operation, record);
    persist(); // Operation identity is durable before the first mutation byte.
    render();
    return record;
  }
  function accept(record, batch) {
    if (!batch || !uuid(batch.batch_id)) return;
    if (record.batch?.batch_id === batch.batch_id &&
        ((finished(record.batch) && !finished(batch)) || completed(batch) < completed(record.batch))) return;
    const wasFinished = finished(record.batch);
    record.batch = batch;
    record.batchId = batch.batch_id;
    record.unavailable = false;
    // Reconstruct the retry relationship from server IDs after a tab reload.
    for (const candidate of records.values()) {
      if (candidate.batch?.parent_batch_id === batch.batch_id) record.retryOperation = candidate.operation;
      if (batch.parent_batch_id && (candidate.batch?.batch_id || candidate.batchId) === batch.parent_batch_id) candidate.retryOperation = record.operation;
    }
    persist();
    render();
    if (!wasFinished && finished(batch)) root.dispatchEvent(new CustomEvent('broadcast-batch-finished', { detail: { batch_id: batch.batch_id } }));
  }
  function render() {
    const list = el('broadcast-jobs-list');
    if (!list) return;
    list.replaceChildren();
    if (!records.size) list.append(node('p', 'Нет отслеживаемых batch. Подтверждение очереди ещё не означает доставку.'));
    for (const record of [...records.values()].reverse()) {
      const card = node('article', undefined, 'section-card');
      card.dataset.batchOperation = record.operation;
      const batch = record.batch;
      card.append(node('h3', batch ? `${batch.channel} · ${batch.mode === 'delivery' ? 'доставка' : 'оффер'}` : 'Операция: результат приёма пока неизвестен'));
      card.append(node('p', `Operation ID: ${record.operation}`));
      if (batch) {
        const rejected = batch.admission_state === 'rejected' || record.admission?.state === 'rejected';
        const state = rejected ? 'Отклонено очередью' : finished(batch) ? 'Завершено' : batch.state === 'running' ? 'Выполняется' : 'В очереди';
        card.append(node('p', `${state} · Batch: ${batch.batch_id}`));
        card.append(node('p', `Всего: ${count(batch.total)} · Ожидает: ${count(batch.pending)} · Начато: ${count(batch.started)} · Отправлено: ${count(batch.sent)} · Ошибки: ${count(batch.failed)} · Пропущено: ${count(batch.skipped)} · Исход неизвестен: ${count(batch.unknown_delivery)}`));
        if (batch.unknown_delivery) card.append(node('p', 'Для неизвестного исхода автоматический повтор запрещён: Telegram мог принять сообщение.'));
        if (record.admission?.storage_mode || batch.job?.storage_mode) card.append(node('p', `Хранилище очереди: ${record.admission?.storage_mode || batch.job.storage_mode}`));
        if (batch.coverage_started_at) card.append(node('p', `Журнал наблюдается с ${batch.coverage_started_at}; персональная история хранится 30 дней.`));
      }
      if (record.unavailable) card.append(node('p', 'Статус временно недоступен. Последние наблюдённые значения сохранены.'));
      const check = node('button', 'Проверить статус', 'btn btn-sm');
      check.type = 'button';
      check.addEventListener('click', () => readRecord(record));
      const retry = node('button', 'Повторить определённо неотправленные', 'btn btn-sm');
      retry.type = 'button';
      retry.dataset.broadcastRetry = '';
      retry.disabled = !finished(batch) || !count(batch?.retryable_failed) || retries.has(batch?.batch_id) || Boolean(record.retryOperation);
      retry.addEventListener('click', () => retryFailed(record));
      card.append(check, retry);
      list.append(card);
    }
    root.DailyChrome?.enhanceLabels();
  }
  async function readRecord(record) {
    if (!canPoll()) return;
    const controller = new AbortController(); reads.add(controller);
    try {
      const path = record.batchId ? `/jobs/${encodeURIComponent(record.batchId)}` : `/jobs/by-operation/${encodeURIComponent(record.operation)}`;
      const data = await api(PREFIX + path, { signal: controller.signal });
      accept(record, data.batch || data);
      record.unavailable = false;
    } catch (error) { if (error.name !== 'AbortError') record.unavailable = true; }
    finally { reads.delete(controller); render(); schedule(); }
  }
  function canPoll() { return !stopped && !document.hidden && navigator.onLine !== false; }
  function schedule(immediate = false) {
    clearTimeout(timer);
    if (initialized && canPoll() && [...records.values()].some(record => !finished(record.batch))) timer = setTimeout(poll, immediate ? 0 : delay);
  }
  async function poll() {
    if (polling || !canPoll()) return;
    const active = [...records.values()].filter(record => !finished(record.batch));
    if (!active.length) return;
    polling = true;
    const controller = new AbortController(); reads.add(controller);
    try {
      const result = await api(`${PREFIX}/jobs?operations=${active.map(record => encodeURIComponent(record.operation)).join(',')}`, { signal: controller.signal });
      for (const record of active) {
        const item = result.items?.find(item => item.operation_id === record.operation);
        if (item?.available === true && item.batch) accept(record, item.batch);
        else record.unavailable = item?.available !== true;
      }
      delay = 2000;
    } catch (error) {
      if (error.name !== 'AbortError') for (const record of active) record.unavailable = true;
      delay = Math.min(delay * 2, 10000);
    } finally { reads.delete(controller); polling = false; render(); schedule(); }
  }
  async function submit(record, path, body) {
    try {
      const data = await api(PREFIX + path, { method: 'POST', body: { ...body, operation_id: record.operation } });
      record.admission = data.admission || null;
      accept(record, data.batch);
      notify(data.admission?.state === 'rejected' ? 'Очередь отклонила batch. Результат доступен в журнале.' : data.admission?.state === 'unknown' ? 'Приём не подтверждён. Проверяем операцию по ID.' : 'Batch принят. Доставка проверяется по журналу.');
    } catch (error) {
      record.admission = error.data?.admission || null;
      accept(record, error.data?.batch);
      if (!error.outcomeUnknown && !error.data?.batch) {
        // A definite refusal did not create a batch. It can safely leave the active set.
        records.delete(record.operation);
        persist();
        notify(error.message, 'error');
        return;
      }
      notify(error.outcomeUnknown ? 'Результат приёма неизвестен. Только проверка по operation ID; повторная отправка не выполняется.' : 'Batch отклонён; подробности и безопасные повторы доступны в журнале.', error.outcomeUnknown ? 'info' : 'error');
    }
    render();
    await readRecord(record); // Includes rejected admission: its returned batch may precede terminal persistence.
  }
  async function start({ channel, userIds, force = false }, { single = false } = {}) {
    const ids = [...new Set(userIds)].sort((a, b) => a - b);
    if (!['horoscope', 'daily_challenge', 'tarot'].includes(channel) || !ids.length || ids.length > 100 || ids.some(id => !Number.isSafeInteger(id) || id <= 0)) {
      notify('Выберите от 1 до 100 корректных User ID и канал.', 'error'); return;
    }
    const key = JSON.stringify([channel, ids]);
    if (commands.has(key)) { notify('Эта операция уже отслеживается. Проверьте её статус.'); return; }
    commands.set(key, { pending: true });
    try {
      const preview = await api(PREFIX + '/preview', { method: 'POST', body: { channel, user_ids: ids, force: Boolean(force) } });
      const exclusions = Object.entries(preview.excluded || {}).map(([reason, total]) => `${reason}: ${count(total)}`).join('; ') || 'нет';
      const confirmed = await showConfirm('Подтвердить batch офферов', `Канал: ${channel}. Выбрано: ${count(preview.requested)}. Допущено: ${count(preview.eligible)}. Исключения: ${exclusions}. force: ${force ? 'да (обходит только текущую подписку)' : 'нет'}. Согласие и доступность проверяются повторно перед отправкой.`);
      if (!confirmed) { commands.delete(key); return; }
      if (!count(preview.eligible)) { notify('Нет допустимых получателей. Batch не создан.'); commands.delete(key); return; }
      const record = admitSlot(root.AdminApi.newOperationId());
      commands.set(key, record);
      await submit(record, single ? '/send-offer' : '/send-offer-batch', single ? { channel, user_id: ids[0], force: Boolean(force) } : { channel, user_ids: ids, force: Boolean(force) });
      if (!records.has(record.operation)) commands.delete(key);
    } catch (error) { commands.delete(key); notify(error.message, 'error'); }
  }
  async function retryFailed(parent) {
    const batch = parent.batch;
    if (!finished(batch) || !count(batch.retryable_failed) || retries.has(batch.batch_id) || parent.retryOperation) return;
    retries.set(batch.batch_id, true); render();
    try {
      if (!await showConfirm('Повторить определённые ошибки', `Канал: ${batch.channel}. Только retryable failed: ${batch.retryable_failed}. Уже sent и unknown исключены. Перед каждой отправкой проверяется согласие.`)) return;
      const child = admitSlot(root.AdminApi.newOperationId());
      parent.retryOperation = child.operation;
      await submit(child, `/jobs/${encodeURIComponent(batch.batch_id)}/retry`, {});
      if (!records.has(child.operation)) parent.retryOperation = null;
    } catch (error) { notify(error.message, 'error'); }
    finally { retries.delete(batch.batch_id); render(); }
  }
  function journalParams(cursor) {
    const params = new URLSearchParams({ limit: '50' });
    for (const name of filters) { const value = el(`broadcast-journal-${name}`).value.trim(); if (value) params.set(name, value); }
    if (cursor) params.set('cursor', cursor);
    return params;
  }
  async function loadJournal(cursor = null, direction = 'replace') {
    const reason = el('broadcast-journal-reason').value.trim();
    if (reason && !/^[a-z0-9_]{1,64}$/.test(reason)) { notify('Reason: только a-z, 0-9 и _, до 64 символов.', 'error'); return; }
    const version = ++journalVersion;
    const query = journalParams(null).toString();
    if (query !== journalQuery) { cursor = null; direction = 'replace'; }
    el('broadcast-journal-next').disabled = true;
    el('broadcast-journal-prev').disabled = true;
    try {
      const data = await latestJournal.request(`${PREFIX}/errors?${journalParams(cursor)}`);
      if (version !== journalVersion) return;
      if (direction === 'next') cursors.push(journalCursor);
      else if (direction === 'prev') cursors.pop();
      else cursors = [];
      journalCursor = cursor;
      journalQuery = query;
      nextCursor = uuid(data.next_cursor) ? data.next_cursor : null;
      const tbody = el('broadcast-journal-body'); tbody.replaceChildren();
      for (const event of data.events || []) {
        const row = node('tr');
        for (const value of [event.started_at, event.channel, event.mode, event.recipient_id, event.outcome, event.reason_code, event.batch_id]) row.append(node('td', value == null ? '—' : String(value)));
        tbody.append(row);
      }
      if (!tbody.children.length) { const row = node('tr'); const cell = node('td', 'Нет наблюдённых записей по фильтрам. Это не подтверждает доставку до начала покрытия.'); cell.colSpan = 7; row.append(cell); tbody.append(row); }
      el('broadcast-journal-coverage').textContent = `Начало покрытия: ${data.coverage_started_at || 'неизвестно, история ещё не наблюдалась'}. Персональная история: ${data.retention_days || 30} дней. unknown_delivery означает возможную отправку без подтверждения; автоматически не повторяется. Тексты сообщений не сохраняются.`;
      persist();
    } catch (error) { if (error.name !== 'AbortError') notify(`Журнал временно недоступен: ${error.message}`, 'error'); }
    finally { if (version === journalVersion) { el('broadcast-journal-next').disabled = !nextCursor; el('broadcast-journal-prev').disabled = !cursors.length; } }
  }
  function pause() { clearTimeout(timer); for (const controller of reads) controller.abort(); latestJournal?.abort(); }
  function init() {
    if (initialized) return;
    initialized = true; latestJournal = journalRead(); restore(); render();
    el('broadcast-journal-apply').addEventListener('click', () => loadJournal());
    el('broadcast-journal-next').addEventListener('click', () => { if (nextCursor) loadJournal(nextCursor, 'next'); });
    el('broadcast-journal-prev').addEventListener('click', () => { if (cursors.length) loadJournal(cursors.at(-1), 'prev'); });
    el('broadcast-jobs-refresh').addEventListener('click', () => { delay = 2000; schedule(true); });
    document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); else schedule(true); });
    root.addEventListener('offline', pause);
    root.addEventListener('online', () => schedule(true));
    root.addEventListener('pagehide', () => { stopped = true; pause(); });
    root.addEventListener('pageshow', () => { stopped = false; schedule(true); });
    loadJournal(); schedule(true);
  }
  root.BroadcastJobs = { init, start };
})(window);
