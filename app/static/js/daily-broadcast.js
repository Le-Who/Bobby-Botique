const BroadcastModule = (() => {
  const fetch = (...args) => (window.AdminApi?.adminFetch || window.fetch)(...args);
  let initialized = false;
  let subOffset = 0;
  let offerOffset = 0;
  const SUB_LIMIT = 50;
  const OFFER_LIMIT = 50;

  function updateDailyDriverUI(mode) {
    const label = document.getElementById('daily-driver-label');
    if (label) {
      const modeNames = { crocodile: '🐊 Daily Crocodile', 2048: '🎲 Daily 2048', trivia: '🧠 Daily Trivia' };
      label.textContent = modeNames[mode] || mode;
    }
    document.querySelectorAll('#daily-mode-buttons button').forEach(btn => {
      const btnMode = btn.getAttribute('data-daily-mode');
      if (btnMode === mode) {
        btn.classList.add('btn-primary');
        btn.classList.remove('btn-refresh');
      } else {
        btn.classList.remove('btn-primary');
        btn.classList.add('btn-refresh');
      }
    });
  }

  async function setDailyMode(mode) {
    try {
      const res = await fetch('/api/admin/daily-mode', {
        method: 'POST',
        headers: apiHeaders,
        body: JSON.stringify({ mode })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(data.error || `HTTP ${res.status}`, 'error');
        return;
      }
      updateDailyDriverUI(mode);
      showToast(`Daily-игра по умолчанию: ${mode}`, 'success');
    } catch (err) {
      showToast(err.message, 'error');
    }
  }

  // ── Channel overview ──────────────────────────────────────────────────────
  async function loadOverview() {
    const container = document.getElementById('channel-cards');
    try {
      const res = await fetch('/api/admin/broadcast/overview', { headers: getHeaders });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      const channels = data.channels || [];
      renderChannelCards(channels);
      const challengeCh = channels.find(c => c.id === 'daily_challenge');
      if (challengeCh && challengeCh.active_game) {
        updateDailyDriverUI(challengeCh.active_game);
      }
    } catch (e) {
      container.innerHTML = `<div class="empty-state error-text">Ошибка загрузки: ${escapeHtml(e.message)}</div>`;
    }
  }

  function tarotDeliveryStats(journal) {
    if (!journal || journal.available !== true || journal.mode !== 'delivery') {
      return '<div class="channel-stat-line"><span>Доставка раскладов</span><span>нет наблюдаемых данных</span></div>';
    }
    const observed = value => Number.isSafeInteger(value) && value >= 0 ? value : '—';
    return `<div class="channel-stat-line"><span>Раскладов доставлено</span><span>${observed(journal.sent)}</span></div>
      <div class="channel-stat-line"><span>Исход неизвестен</span><span>${observed(journal.unknown_delivery)}</span></div>
      <div class="channel-stat-line"><span>Покрытие журнала (30 дней)</span><span>${escapeHtml(journal.coverage_started_at || 'ещё не наблюдалось')}</span></div>
      <div class="channel-stat-line"><span>Офферы не входят в число раскладов</span></div>`;
  }

  function renderChannelCards(channels) {
    const container = document.getElementById('channel-cards');
    if (!channels.length) {
      container.innerHTML = '<div class="empty-state">Нет данных по каналам</div>';
      return;
    }
    container.innerHTML = channels.map(ch => {
      const isEnabled = ch.delivery_enabled;
      const dotCls = isEnabled ? 'dot-on' : 'dot-off';
      const cardCls = isEnabled ? 'enabled' : 'disabled';
      const toggleHtml = ch.id !== 'tarot' || ch.subscribers > 0
        ? `<div class="channel-toggle">
            <label class="switch">
              <input type="checkbox" ${isEnabled ? 'checked' : ''} data-channel="${ch.id}" class="channel-toggle-chk">
              <span class="slider"></span>
            </label>
           </div>` : '';
      return `
        <div class="channel-card ${cardCls}">
          <div class="channel-card-header">
            <div class="channel-name">
              <span class="channel-status-dot ${dotCls}"></span>${ch.emoji} ${ch.name}
            </div>
            ${ch.active_game ? `<span class="channel-mode-badge">${ch.active_game}</span>` : ''}
          </div>
          <div class="channel-stats">
            <div class="channel-stat-line">
              <span>Подписчики</span>
              <span>${ch.subscribers != null ? ch.subscribers : '—'}</span>
            </div>
            ${ch.pending_today != null ? `<div class="channel-stat-line"><span>Ожидает</span><span>${ch.pending_today}</span></div>` : ''}
            ${ch.id === 'tarot' ? tarotDeliveryStats(ch.delivery_journal) : ch.sent_today != null ? `<div class="channel-stat-line"><span>Отправлено</span><span>${ch.sent_today}</span></div>` : ''}
            ${ch.last_sent_at ? `<div class="channel-stat-line"><span>Последняя</span><span>${new Date(ch.last_sent_at).toLocaleTimeString('ru-RU')}</span></div>` : ''}
          </div>
          ${toggleHtml}
        </div>`;
    }).join('');

    // Bind toggles
    container.querySelectorAll('.channel-toggle-chk').forEach(chk => {
      chk.addEventListener('change', async () => {
        const channel = chk.dataset.channel;
        const enabled = chk.checked;
        const label = enabled ? 'включить' : 'отключить';
        const confirmed = await showConfirm('Изменение рассылки', `${label} рассылку "${channel}"?`);
        if (!confirmed) { chk.checked = !enabled; return; }
        chk.disabled = true;
        try {
          const res = await fetch('/api/admin/broadcast/toggle', {
            method: 'POST', headers: apiHeaders,
            body: JSON.stringify({ channel, enabled })
          });
          const d = await res.json();
          if (!res.ok || d.error) throw new Error(d.error || `HTTP ${res.status}`);
          showToast(`Рассылка ${channel}: ${enabled ? 'включена' : 'отключена'}`, 'success');
          await loadOverview();
        } catch (e) {
          showToast(`Ошибка: ${e.message}`, 'error');
          chk.checked = !enabled;
        } finally {
          chk.disabled = false;
        }
      });
    });
  }

  // ── Subscribers table ─────────────────────────────────────────────────────
  async function loadSubscribers(offset = 0) {
    subOffset = offset;
    const channel = document.getElementById('sub-filter-channel').value;
    const status = document.getElementById('sub-filter-status').value;
    const uid = document.getElementById('sub-filter-uid').value.trim();
    const params = new URLSearchParams({ limit: SUB_LIMIT, offset });
    if (channel) params.set('channel', channel);
    if (status) params.set('status', status);
    if (uid) params.set('user_id', uid);

    const tbody = document.getElementById('sub-tbody');
    tbody.innerHTML = '<tr><td colspan="5" class="empty-state"><div class="image-spinner" style="margin:0 auto 8px;"></div>Загрузка...</td></tr>';
    try {
      const res = await fetch('/api/admin/broadcast/subscribers?' + params, { headers: getHeaders });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      const rows = data.rows || [];
      document.getElementById('sub-count-label').textContent = `Всего: ${data.total}`;
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty-state"><span class="empty-state-icon">🙅</span>Подписчиков не найдено</td></tr>';
      } else {
        tbody.innerHTML = rows.map(r => {
          const isActive = r.status === 'active';
          const isSnoozed = r.status === 'snoozed';
          const rowCls = isActive ? '' : 'row-inactive';
          let statusBadge;
          if (isActive) statusBadge = '<span class="badge-status badge-status-active">✦ active</span>';
          else if (isSnoozed) statusBadge = '<span class="badge-status badge-status-snoozed">⏸ snoozed</span>';
          else statusBadge = '<span class="badge-status badge-status-inactive">— inactive</span>';
          const channels = (r.channels || []).map(c => `<span class="badge-channel">${c}</span>`).join('');
          const lastSent = r.last_sent
            ? new Date(r.last_sent).toLocaleString('ru-RU', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})
            : null;
          const nameStr = escapeHtml(r.display_name || '');
          const hourStr = r.preferred_hour != null ? String(r.preferred_hour).padStart(2,'0') + ':00' : '—';
          const tzStr = r.timezone || '—';
          return `<tr class="${rowCls}">
            <td>
              <div class="cell-user">
                <span class="cell-user-name">${nameStr || 'Без имени'}</span>
                <span class="cell-user-id">${r.user_id}</span>
              </div>
            </td>
            <td>${channels || '<span class="cell-date-never">нет</span>'}</td>
            <td class="td-r">
              <div class="cell-time">
                <span class="cell-time-hour">${hourStr}</span>
                <span class="cell-time-tz">${tzStr}</span>
              </div>
            </td>
            <td class="td-r">${lastSent
              ? `<span class="cell-date-val">${lastSent}</span>`
              : '<span class="cell-date-never">никогда</span>'}</td>
            <td>${statusBadge}</td>
          </tr>`;
        }).join('');
      }
      document.getElementById('sub-prev').disabled = offset <= 0;
      document.getElementById('sub-next').disabled = rows.length < SUB_LIMIT;
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="5" class="error-state"><div class="error-state-icon">🚨</div><div class="error-state-msg">${escapeHtml(e.message)}</div></td></tr>`;
    }
  }

  // ── Offer history ─────────────────────────────────────────────────────────
  let _selectedUids = new Set();

  function _updateBatchBar() {
    const bar = document.getElementById('offer-batch-bar');
    const countEl = document.getElementById('offer-batch-count');
    const n = _selectedUids.size;
    bar.style.display = n > 0 ? 'flex' : 'none';
    countEl.textContent = `${n} выбрано`;
  }

  async function loadOfferHistory(offset = 0) {
    offerOffset = offset;
    const channel = document.getElementById('offer-filter-channel').value;
    const status  = document.getElementById('offer-filter-status').value;
    const never   = document.getElementById('offer-filter-never').checked;
    const uid     = document.getElementById('offer-filter-uid').value.trim();
    const params  = new URLSearchParams({ limit: OFFER_LIMIT, offset });
    if (channel && channel !== 'all') params.set('channel', channel);
    if (status) params.set('status', status);
    if (never) params.set('never_sent', '1');
    if (uid) params.set('user_id', uid);

    const tbody = document.getElementById('offer-tbody');
    tbody.innerHTML = '<tr><td colspan="8" class="empty-state"><div class="image-spinner" style="margin:0 auto 8px;"></div>Загрузка...</td></tr>';
    _selectedUids.clear();
    _updateBatchBar();
    document.getElementById('offer-select-all').checked = false;

    try {
      const res = await fetch('/api/admin/broadcast/offer-history?' + params, { headers: getHeaders });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      const rows = data.rows || [];
      document.getElementById('offer-count-label').textContent = `Всего: ${data.total}`;

      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty-state"><span class="empty-state-icon">🙅</span>Нет данных по выбранным фильтрам</td></tr>';
      } else {
        tbody.innerHTML = rows.map((r) => {
          const offerAt = r.offer_sent_at
            ? new Date(r.offer_sent_at).toLocaleString('ru-RU', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})
            : null;
          const snoozedUntil = r.snoozed_until
            ? new Date(r.snoozed_until).toLocaleString('ru-RU', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})
            : null;
          const subBadge = r.is_subscribed
            ? '<span class="badge-status badge-status-yes">✦ подписан</span>'
            : '<span class="badge-status badge-status-no">— нет</span>';
          const nameStr = escapeHtml(r.display_name || '');
          const rowCls = r.is_subscribed ? '' : 'row-inactive';
          return `<tr class="${rowCls}" data-uid="${r.user_id}" data-channel="${r.channel}">
            <td style="text-align:center;padding:9px 8px;"><input type="checkbox" class="offer-row-chk" value="${r.user_id}"></td>
            <td>
              <div class="cell-user">
                <span class="cell-user-name">${nameStr || 'Без имени'}</span>
                <span class="cell-user-id">${r.user_id}</span>
              </div>
            </td>
            <td><span class="badge-channel">${r.channel_emoji} ${r.channel}</span></td>
            <td class="td-r">${offerAt
              ? `<span class="cell-date-val">${offerAt}</span>`
              : '<span class="cell-date-never">никогда</span>'}</td>
            <td class="td-r">${snoozedUntil
              ? `<span class="badge-status badge-status-snoozed">⏸ ${snoozedUntil}</span>`
              : '<span class="cell-date-never">—</span>'}</td>
            <td>${subBadge}</td>
            <td>
              <button class="btn btn-sm offer-send-btn"
                data-uid="${r.user_id}"
                data-channel="${r.channel}"
                data-subscribed="${r.is_subscribed}"
                title="Отправить оффер">✈️</button>
            </td>
          </tr>`;
        }).join('');

        tbody.querySelectorAll('.offer-row-chk').forEach(chk => {
          chk.addEventListener('change', () => {
            const uid = parseInt(chk.value);
            chk.checked ? _selectedUids.add(uid) : _selectedUids.delete(uid);
            _updateBatchBar();
          });
        });

        tbody.querySelectorAll('.offer-send-btn').forEach(btn => {
          btn.addEventListener('click', () => _sendOfferConfirm(
            parseInt(btn.dataset.uid),
            btn.dataset.channel,
            btn.dataset.subscribed === 'true',
            btn
          ));
        });
      }
      document.getElementById('offer-prev').disabled = offset <= 0;
      document.getElementById('offer-next').disabled = rows.length < OFFER_LIMIT;
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="8" class="error-state"><div class="error-state-icon">🚨</div><div class="error-state-msg">${escapeHtml(e.message)}</div></td></tr>`;
    }
  }

  async function _sendOfferConfirm(uid, channel, isSubscribed, btnEl) {
    if (btnEl?.disabled) return;
    if (btnEl) btnEl.disabled = true;
    try {
      await window.BroadcastJobs.start({ channel, userIds: [uid], force: isSubscribed }, { single: true });
    } finally { if (btnEl) btnEl.disabled = false; }
  }

  async function _sendBatch() {
    const btn = document.getElementById('btn-offer-batch-send');
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      await window.BroadcastJobs.start({
        channel: document.getElementById('offer-batch-channel').value,
        userIds: [..._selectedUids],
        force: document.getElementById('offer-batch-force').checked
      });
    } finally { btn.disabled = false; }
  }

  async function _quickSend() {
    const btn = document.getElementById('btn-offer-quick-send');
    if (btn.disabled) return;
    const raw = document.getElementById('offer-quick-uid').value.trim();
    const uid = /^\d+$/.test(raw) ? Number(raw) : NaN;
    if (!Number.isSafeInteger(uid) || uid <= 0) { showToast('Введите корректный User ID', 'error'); return; }
    btn.disabled = true;
    try {
      await window.BroadcastJobs.start({
        channel: document.getElementById('offer-quick-channel').value,
        userIds: [uid],
        force: document.getElementById('offer-quick-force').checked
      }, { single: true });
    } finally { btn.disabled = false; }
  }

  // ── init ──────────────────────────────────────────────────────────────────
  function init() {
    if (initialized) return;
    initialized = true;
    window.BroadcastJobs.init();
    loadOverview();
    window.addEventListener('broadcast-batch-finished', () => { loadOverview(); loadOfferHistory(offerOffset); });

    document.getElementById('btn-refresh-broadcast').addEventListener('click', () => {
      loadOverview();
      loadSubscribers(subOffset);
      loadOfferHistory(offerOffset);
    });
    // Subscribers
    document.getElementById('btn-load-subs').addEventListener('click', () => loadSubscribers(0));
    document.getElementById('sub-prev').addEventListener('click', () => loadSubscribers(Math.max(0, subOffset - SUB_LIMIT)));
    document.getElementById('sub-next').addEventListener('click', () => loadSubscribers(subOffset + SUB_LIMIT));

    // Offer history
    document.getElementById('btn-load-offers').addEventListener('click', () => loadOfferHistory(0));
    document.getElementById('offer-prev').addEventListener('click', () => loadOfferHistory(Math.max(0, offerOffset - OFFER_LIMIT)));
    document.getElementById('offer-next').addEventListener('click', () => loadOfferHistory(offerOffset + OFFER_LIMIT));

    // Select all checkbox
    document.getElementById('offer-select-all').addEventListener('change', e => {
      document.querySelectorAll('.offer-row-chk').forEach(chk => {
        chk.checked = e.target.checked;
        const uid = parseInt(chk.value);
        e.target.checked ? _selectedUids.add(uid) : _selectedUids.delete(uid);
      });
      _updateBatchBar();
    });

    // Batch send
    document.getElementById('btn-offer-batch-send').addEventListener('click', _sendBatch);
    document.getElementById('btn-offer-batch-clear').addEventListener('click', () => {
      _selectedUids.clear();
      _updateBatchBar();
      document.getElementById('offer-select-all').checked = false;
      document.querySelectorAll('.offer-row-chk').forEach(c => { c.checked = false; });
    });

    // Quick send
    document.getElementById('btn-offer-quick-send').addEventListener('click', _quickSend);

    // Daily mode buttons
    document.querySelectorAll('#daily-mode-buttons button').forEach(btn => {
      btn.addEventListener('click', () => {
        const mode = btn.getAttribute('data-daily-mode');
        if (mode) setDailyMode(mode);
      });
    });
  }

  return { init };
})();
window.BroadcastModule = BroadcastModule;
