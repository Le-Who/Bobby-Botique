// Tab-local authoring state. Draft content is never written to persistent storage.
function createDailyTriviaDrafts() {
  const entries = new Map();
  const clone = value => structuredClone(value);
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  function normalized(items, count) {
    return Array.from({length:count}, (_, i) => {
      const q = items?.[i] || {};
      const options = Array.from({length:4}, (_, j) => String(q.options?.[j] || ''));
      const question = String(q.question || '');
      const correct = Number.isInteger(q.correct_index) ? q.correct_index : 0;
      return {id:q.id || i+1, topic:q.topic || 'Общие знания', question, options, correct_index:correct,
        explanation:q.explanation || '', identity:{subject:q.identity?.subject || question,
          relation:q.identity?.relation || 'правильный ответ', answer:options[correct] || ''}};
    });
  }
  function replace(puzzle) {
    const main = normalized(puzzle.questions, 5), superQuestions = normalized(puzzle.super_questions, 3);
    const state = {date:puzzle.date, baseRevision:puzzle.revision || 0, baseMain:main, baseSuper:superQuestions,
      draftMain:clone(main), draftSuper:clone(superQuestions), mainDirty:false, superDirty:false, conflict:null, server:clone(puzzle)};
    entries.set(puzzle.date, state);
    return state;
  }
  function receive(puzzle) {
    const state = entries.get(puzzle.date);
    if (!state) return replace(puzzle);
    if (puzzle.revision < state.baseRevision) return state;
    if (puzzle.revision !== state.baseRevision && (state.mainDirty || state.superDirty || state.conflict)) {
      if (!state.conflict?.server || puzzle.revision >= state.conflict.server.revision) state.conflict = {server:clone(puzzle)};
      return state;
    }
    if (!state.mainDirty && !state.superDirty && !state.conflict) return replace(puzzle);
    return state;
  }
  function edit(date, main, superQuestions) {
    const state = entries.get(date);
    if (!state) return;
    state.draftMain = clone(main); state.draftSuper = clone(superQuestions);
    state.mainDirty = !same(state.draftMain, state.baseMain);
    state.superDirty = !same(state.draftSuper, state.baseSuper);
  }
  function saved(date, section, submitted, puzzle) {
    const state = entries.get(date);
    if (!state) return receive(puzzle);
    if (puzzle.revision < state.baseRevision) return state;
    if (state.conflict?.server?.revision > puzzle.revision) return state;
    const mainWasDirty = state.mainDirty, superWasDirty = state.superDirty;
    const nextMain = normalized(puzzle.questions, 5), nextSuper = normalized(puzzle.super_questions, 3);
    if ((!mainWasDirty && section !== 'main') || (section === 'main' && same(state.draftMain, submitted))) state.draftMain = clone(nextMain);
    if ((!superWasDirty && section !== 'super') || (section === 'super' && same(state.draftSuper, submitted))) state.draftSuper = clone(nextSuper);
    state.baseMain = nextMain; state.baseSuper = nextSuper; state.baseRevision = puzzle.revision || 0;
    state.server = clone(puzzle); state.conflict = null;
    state.mainDirty = !same(state.draftMain, nextMain); state.superDirty = !same(state.draftSuper, nextSuper);
    return state;
  }
  return {get:date => entries.get(date), receive, replace, edit, saved, clone,
    hasDirty:()=>[...entries.values()].some(state=>state.mainDirty || state.superDirty)};
}

const TriviaModule = (() => {
  const fetch = (...args) => (window.DailyChrome?.fetch || window.AdminApi?.adminFetch || window.fetch)(...args);
  let initialized = false;
  let puzzles = [];
  let selectedDate = null;
  let selectedPuzzle = null;
  let puzzleRequest=0, leaderboardRequest=0, statsRequest=0;
  const drafts = createDailyTriviaDrafts();
  let editorFocus = null;

  function captureDraft() {
    if (selectedDate && drafts.get(selectedDate)) drafts.edit(selectedDate, getFormQuestions(), getFormSuperQuestions());
    return drafts.get(selectedDate);
  }

  function rememberFocus(element) {
    if (!element?.matches('input,textarea')) return;
    editorFocus = {className:element.className, index:element.dataset.idx, name:element.name, value:element.value,
      start:element.selectionStart, end:element.selectionEnd};
  }

  function restoreFocus() {
    if (!editorFocus) return;
    const selector = editorFocus.index !== undefined
      ? `.${CSS.escape(editorFocus.className)}[data-idx="${CSS.escape(editorFocus.index)}"]`
      : `input[name="${CSS.escape(editorFocus.name)}"][value="${CSS.escape(editorFocus.value)}"]`;
    const element = document.querySelector(selector);
    if (element) {element.focus(); if (editorFocus.start !== null) element.setSelectionRange(editorFocus.start, editorFocus.end);}
  }

  function renderDraftStatus(state) {
    document.getElementById('trivia-editor-title').textContent = `Викторина на ${state.date} · ревизия ${state.baseRevision}`;
    document.getElementById('trivia-editor-status').textContent = `${state.server.status || 'draft'} · обычные ${state.mainDirty ? 'несохранены' : 'без изменений'} (${state.server.questions?.length || 0}/5) · супер ${state.superDirty ? 'несохранены' : 'без изменений'} (${state.server.super_questions?.length || 0}/3)${state.conflict ? ' · конфликт' : ''}`;
    const panel = document.getElementById('trivia-editor-conflict');
    panel.hidden = !state.conflict;
    if (state.conflict) document.getElementById('trivia-editor-comparison').textContent = `Сервер: r${state.conflict.server?.revision ?? '?'}. Локальная база: r${state.baseRevision}. Серверные вопросы: ${JSON.stringify({main:state.conflict.server?.questions,super:state.conflict.server?.super_questions})}. Локальные черновики сохранены в этой вкладке.`;
  }

  function renderDraft(state) {
    renderEditorQuestions(state.draftMain, state.draftSuper);
    renderDraftStatus(state);
    restoreFocus();
  }

  function saved(date, section, submitted, puzzle) {
    const state = drafts.saved(date, section, submitted, puzzle);
    if (puzzle.revision >= Math.max(state.baseRevision, state.conflict?.server?.revision || 0)) {
      const index = puzzles.findIndex(item => item.date === date);
      if (index >= 0) puzzles[index] = puzzle;
      else puzzles.push(puzzle);
    }
    if (selectedDate === date) {selectedPuzzle = state.server; renderDraft(state);}
    return state;
  }

  function conflict(date, data) {
    const state = drafts.get(date);
    if (!state) return;
    if (data.puzzle) drafts.receive(data.puzzle);
    state.conflict = state.conflict || {server:{...state.server,revision:data.current_revision || state.baseRevision}};
    if (selectedDate === date) renderDraftStatus(state);
  }
  let settingsRevision = null;
  let modelDirty = false;
  let savedModelValue = '';
  let settingsConflict = false;

  DailySettingsState.subscribe(revision => {
    if (settingsRevision !== null && !settingsConflict) {
      settingsRevision = revision;
      document.getElementById('trivia-model-status').textContent = `Модель: r${revision}`;
    }
  });

  async function loadStats({discard = false} = {}) {
    const ticket=++statsRequest;
    try {
      const res = await fetch('/api/admin/dailytrivia/stats', { headers: getHeaders });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if(ticket!==statsRequest) return;
      if (data.error) throw new Error(data.error);
      document.getElementById('trivia-stats-subs').textContent = `${data.sent_today ?? 0} / ${data.total_subscribed ?? 0}`;
      document.getElementById('trivia-stats-subs-detail').textContent = `Отправлено: ${data.sent_today ?? 0} | Ожидает: ${data.pending_today ?? 0}`;
      document.getElementById('trivia-stats-games').textContent = data.played_today || '0';
      document.getElementById('trivia-stats-games-detail').textContent = `Завершено: ${data.finished_today ?? 0} | В игре: ${data.active_today ?? 0}`;
      document.getElementById('trivia-stats-puzzles-ahead').textContent = data.ready_puzzles_ahead ?? '0';
      document.getElementById('trivia-stats-bank').textContent = data.bank_fact_count ?? '0';
      document.getElementById('trivia-stats-bank-detail').textContent = `За 90 дней: ${data.recent_bank_fact_count ?? 0}`;

      const modelInput = document.getElementById('trivia-model-input');
      const modelOptions = document.getElementById('trivia-model-options');
      if (modelOptions && Array.isArray(data.llm_models)) {
        modelOptions.replaceChildren(...data.llm_models.map(model => {
          const option = document.createElement('option');
          option.value = model;
          return option;
        }));
      }
      if (modelInput && data.llm_model && (settingsRevision===null || data.settings_revision>=settingsRevision) && (discard || (!modelDirty && !settingsConflict))) {
        modelInput.value = data.llm_model;
        settingsRevision = Number.isInteger(data.settings_revision) ? data.settings_revision : null;
        settingsConflict = false;
        modelDirty = false;
        savedModelValue=data.llm_model;
        document.getElementById('trivia-model-status').textContent = `Модель: r${settingsRevision}. ${(data.effective_models || []).join(' → ')}`;
      }
    } catch (e) {
      if(ticket!==statsRequest || e.code==='aborted') return;
      console.error('Trivia stats error:', e);
    }
  }

  async function loadPuzzles({discard = false} = {}) {
    const ticket=++puzzleRequest;
    captureDraft();
    const container = document.getElementById('trivia-puzzles-list');
    try {
      const res = await fetch('/api/admin/dailytrivia', { headers: getHeaders });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if(ticket!==puzzleRequest) return;
      if (data.error) throw new Error(data.error);
      puzzles = data.puzzles || [];
      const todayStr = data.today || getKyivDateString();
      renderPuzzles(puzzles, container, todayStr);

      if (selectedDate) {
        const p = puzzles.find(x => x.date === selectedDate);
        if (p) {
          if (discard) drafts.replace(p);
          selectPuzzle(p, true);
        }
      }
    } catch (e) {
      if(ticket!==puzzleRequest || e.code==='aborted') return;
      container.innerHTML = `<div class="empty-state error-text">Ошибка загрузки: ${escapeHtml(e.message)}</div>`;
    }
  }

  function renderPuzzles(list, container, todayStr) {
    if (!list.length) {
      container.innerHTML = '<div class="empty-state">Нет викторин</div>';
      return;
    }

    const future = [];
    const recent = [];
    const older = [];

    const sevenDaysAgo = new Date(`${todayStr}T12:00:00Z`);
    sevenDaysAgo.setUTCDate(sevenDaysAgo.getUTCDate() - 7);
    const sevenDaysAgoStr = sevenDaysAgo.toISOString().slice(0, 10);

    list.forEach(p => {
      if (p.date > todayStr) future.push(p);
      else if (p.date >= sevenDaysAgoStr) recent.push(p);
      else older.push(p);
    });

    const renderCard = (p) => {
      const isToday = p.date === todayStr;
      const isSelected = selectedDate === p.date;
      const isReady = p.status === 'ready';
      const qCount = (p.questions || []).length;
      const superCount = (p.super_questions || []).length;
      return `
        <div class="s2048-puzzle ${isSelected ? 'active' : ''}" data-date="${p.date}">
          <div class="s2048-puzzle-head">
            <span class="s2048-date">${p.date}${isToday ? ' <span class="today-badge">СЕГОДНЯ</span>' : ''}</span>
            <span class="s2048-badge">${isReady && qCount === 5 && superCount === 3 ? '✅ 5/5 + 3/3' : `⚠️ ${qCount}/5 + ${superCount}/3`}</span>
          </div>
          <div style="font-size:0.75rem; color:var(--text-muted); margin-top:6px;">
            ${(p.questions && p.questions[0]) ? '1. ' + escapeHtml(p.questions[0].question.slice(0, 45)) + '...' : 'Вопросы не сгенерированы'}
          </div>
        </div>
      `;
    };

    const renderGroup = (title, items) => {
      if (!items.length) return '';
      let groupHtml = `<h3 style="width: 100%; grid-column: 1 / -1; font-size: 0.9rem; color: var(--accent); margin-top: 10px; border-bottom: 1px solid var(--border); padding-bottom: 4px;">${title}</h3>`;
      groupHtml += items.map(renderCard).join('');
      return groupHtml;
    };

    let html = '';
    html += renderGroup('🔮 Будущие', future);
    html += renderGroup('📅 Последние 7 дней', recent);
    html += renderGroup('🕰 Более старые', older);

    container.innerHTML = html;

    container.querySelectorAll('.s2048-puzzle').forEach(el => {
      el.addEventListener('click', () => {
        const d = el.dataset.date;
        const target = puzzles.find(p => p.date === d);
        if (target) selectPuzzle(target);
      });
    });
  }

  async function selectPuzzle(p, isReload = false) {
    if (!p) return;
    const previous = isReload ? drafts.get(selectedDate) : captureDraft();
    if (!isReload && selectedDate !== p.date && previous && (previous.mainDirty || previous.superDirty)) {
      if (!await showConfirm('Несохранённые изменения', 'Перейти к другой дате? Черновики обеих частей останутся в памяти этой вкладки.')) {restoreFocus(); return;}
    }
    selectedDate = p.date;
    const state = drafts.receive(p);
    selectedPuzzle = state.server;
    document.getElementById('trivia-left-pane').style.display = 'none';
    const editorPane = document.getElementById('trivia-editor-pane');
    editorPane.style.display = 'block'; editorPane.style.gridColumn = '1 / -1';
    editorPane.style.maxWidth = '1000px'; editorPane.style.margin = '0 auto';
    document.querySelectorAll('#trivia-puzzles-list .s2048-puzzle').forEach(el => el.classList.toggle('active',el.dataset.date===p.date));
    renderDraft(state);
  }

  async function closeEditor() {
    const state = captureDraft();
    if (state && (state.mainDirty || state.superDirty)) {
      if (!await showConfirm('Несохранённые изменения', 'Вернуться к календарю? Черновики обеих частей останутся в памяти этой вкладки.')) {restoreFocus(); return;}
    }
    document.getElementById('trivia-left-pane').style.display = 'block';
    document.getElementById('trivia-editor-pane').style.display = 'none';
    selectedDate = null; selectedPuzzle = null;
    document.querySelectorAll('#trivia-puzzles-list .s2048-puzzle').forEach(el=>el.classList.remove('active'));
  }

  function renderEditorQuestions(questions, superQuestions) {
    const container = document.getElementById('trivia-editor-questions');
    const qList = [];
    for (let i = 0; i < 5; i++) {
      qList.push(questions[i] || {
        id: i + 1,
        topic: 'Общие знания',
        question: '',
        options: ['', '', '', ''],
        correct_index: 0,
        explanation: ''
      });
    }

    container.innerHTML = qList.map((q, i) => {
      const opts = q.options || ['', '', '', ''];
      while (opts.length < 4) opts.push('');
      return `
        <div style="border:1px solid var(--border); border-radius:8px; padding:10px; background:var(--bg-surface);">
          <div style="font-weight:700; font-size:0.8rem; color:var(--accent); margin-bottom:8px;">Вопрос #${i + 1}</div>
          <div class="field" style="margin-bottom:6px;">
            <label>Тема</label>
            <input type="text" class="t-q-topic" data-idx="${i}" value="${escapeHtml(q.topic || '')}" placeholder="Тема (напр. Наука)">
          </div>
          <div class="field" style="margin-bottom:6px;">
            <label>Текст вопроса</label>
            <textarea class="t-q-text" data-idx="${i}" style="min-height:50px;">${escapeHtml(q.question || '')}</textarea>
          </div>
          <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px; margin-bottom:6px;">
            <div class="field">
              <label>Сущность факта</label>
              <input type="text" class="t-q-subject" data-idx="${i}" value="${escapeHtml(q.identity?.subject || q.question || '')}" placeholder="Напр. Пенициллин">
            </div>
            <div class="field">
              <label>Что спрашиваем</label>
              <input type="text" class="t-q-relation" data-idx="${i}" value="${escapeHtml(q.identity?.relation || 'правильный ответ')}" placeholder="Напр. первооткрыватель">
            </div>
          </div>
          <div class="field" style="margin-bottom:6px;">
            <label>Варианты ответа (отметьте точкой верный):</label>
            <div style="display:flex; flex-direction:column; gap:4px;">
              ${opts.slice(0, 4).map((opt, optIdx) => `
                <div style="display:flex; align-items:center; gap:6px;">
                  <input type="radio" name="t-q-correct-${i}" value="${optIdx}" ${q.correct_index === optIdx ? 'checked' : ''}>
                  <input type="text" class="t-q-opt-${optIdx}" data-idx="${i}" value="${escapeHtml(opt)}" placeholder="Вариант ${optIdx + 1}" style="flex:1;">
                </div>
              `).join('')}
            </div>
          </div>
          <div class="field">
            <label>Познавательное объяснение</label>
            <textarea class="t-q-exp" data-idx="${i}" style="min-height:45px;">${escapeHtml(q.explanation || '')}</textarea>
          </div>
        </div>
      `;
    }).join('');

    // Render super questions (editable)
    const superSection = document.getElementById('trivia-super-questions-section');
    const superContainer = document.getElementById('trivia-editor-super-questions');
    if (superQuestions && superQuestions.length > 0) {
      superSection.style.display = 'block';
      const sqList = [];
      for (let i = 0; i < 3; i++) {
        sqList.push(superQuestions[i] || {
          id: i + 1, topic: 'Сложная тема', question: '', options: ['', '', '', ''], correct_index: 0, explanation: ''
        });
      }
      superContainer.innerHTML = sqList.map((q, i) => {
        const opts = q.options || ['', '', '', ''];
        while (opts.length < 4) opts.push('');
        return `
          <div style="border:1px solid rgba(255,128,191,0.3); border-radius:8px; padding:10px; background:rgba(255,0,128,0.04);">
            <div style="font-weight:700; font-size:0.8rem; color:#ff80bf; margin-bottom:8px;">⚡ Супервопрос #${i + 1}</div>
            <div class="field" style="margin-bottom:6px;">
              <label>Тема</label>
              <input type="text" class="t-sq-topic" data-idx="${i}" value="${escapeHtml(q.topic || '')}" placeholder="Тема">
            </div>
            <div class="field" style="margin-bottom:6px;">
              <label>Текст вопроса</label>
              <textarea class="t-sq-text" data-idx="${i}" style="min-height:50px;">${escapeHtml(q.question || '')}</textarea>
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px; margin-bottom:6px;">
              <div class="field">
                <label>Сущность факта</label>
                <input type="text" class="t-sq-subject" data-idx="${i}" value="${escapeHtml(q.identity?.subject || q.question || '')}" placeholder="Конкретная сущность">
              </div>
              <div class="field">
                <label>Что спрашиваем</label>
                <input type="text" class="t-sq-relation" data-idx="${i}" value="${escapeHtml(q.identity?.relation || 'правильный ответ')}" placeholder="Конкретное отношение">
              </div>
            </div>
            <div class="field" style="margin-bottom:6px;">
              <label>Варианты ответа (отметьте точкой верный):</label>
              <div style="display:flex; flex-direction:column; gap:4px;">
                ${opts.slice(0, 4).map((opt, optIdx) => `
                  <div style="display:flex; align-items:center; gap:6px;">
                    <input type="radio" name="t-sq-correct-${i}" value="${optIdx}" ${q.correct_index === optIdx ? 'checked' : ''}>
                    <input type="text" class="t-sq-opt-${optIdx}" data-idx="${i}" value="${escapeHtml(opt)}" placeholder="Вариант ${optIdx + 1}" style="flex:1;">
                  </div>
                `).join('')}
              </div>
            </div>
            <div class="field">
              <label>Познавательное объяснение</label>
              <textarea class="t-sq-exp" data-idx="${i}" style="min-height:45px;">${escapeHtml(q.explanation || '')}</textarea>
            </div>
          </div>
        `;
      }).join('');
    } else {
      superSection.style.display = 'none';
    }
  }


  function getFormQuestions() {
    const container = document.getElementById('trivia-editor-questions');
    const questions = [];
    for (let i = 0; i < 5; i++) {
      const topic = container.querySelector(`.t-q-topic[data-idx="${i}"]`)?.value || 'Общие знания';
      const question = container.querySelector(`.t-q-text[data-idx="${i}"]`)?.value || '';
      const opt0 = container.querySelector(`.t-q-opt-0[data-idx="${i}"]`)?.value || '';
      const opt1 = container.querySelector(`.t-q-opt-1[data-idx="${i}"]`)?.value || '';
      const opt2 = container.querySelector(`.t-q-opt-2[data-idx="${i}"]`)?.value || '';
      const opt3 = container.querySelector(`.t-q-opt-3[data-idx="${i}"]`)?.value || '';
      const correctRadio = container.querySelector(`input[name="t-q-correct-${i}"]:checked`);
      const correct_index = correctRadio ? parseInt(correctRadio.value) : 0;
      const explanation = container.querySelector(`.t-q-exp[data-idx="${i}"]`)?.value || '';
      const subject = container.querySelector(`.t-q-subject[data-idx="${i}"]`)?.value || question;
      const relation = container.querySelector(`.t-q-relation[data-idx="${i}"]`)?.value || 'правильный ответ';

      questions.push({
        id: i + 1,
        topic,
        question,
        options: [opt0, opt1, opt2, opt3],
        correct_index,
        explanation,
        identity: { subject, relation, answer: [opt0, opt1, opt2, opt3][correct_index] || '' }
      });
    }
    return questions;
  }

  function getFormSuperQuestions() {
    const container = document.getElementById('trivia-editor-super-questions');
    const questions = [];
    for (let i = 0; i < 3; i++) {
      const topic = container.querySelector(`.t-sq-topic[data-idx="${i}"]`)?.value || 'Общие знания';
      const question = container.querySelector(`.t-sq-text[data-idx="${i}"]`)?.value || '';
      const opt0 = container.querySelector(`.t-sq-opt-0[data-idx="${i}"]`)?.value || '';
      const opt1 = container.querySelector(`.t-sq-opt-1[data-idx="${i}"]`)?.value || '';
      const opt2 = container.querySelector(`.t-sq-opt-2[data-idx="${i}"]`)?.value || '';
      const opt3 = container.querySelector(`.t-sq-opt-3[data-idx="${i}"]`)?.value || '';
      const correctRadio = container.querySelector(`input[name="t-sq-correct-${i}"]:checked`);
      const correct_index = correctRadio ? parseInt(correctRadio.value) : 0;
      const explanation = container.querySelector(`.t-sq-exp[data-idx="${i}"]`)?.value || '';
      const subject = container.querySelector(`.t-sq-subject[data-idx="${i}"]`)?.value || question;
      const relation = container.querySelector(`.t-sq-relation[data-idx="${i}"]`)?.value || 'правильный ответ';
      questions.push({ id: i + 1, topic, question, options: [opt0, opt1, opt2, opt3], correct_index, explanation, identity: { subject, relation, answer: [opt0, opt1, opt2, opt3][correct_index] || '' } });
    }
    return questions;
  }

  async function currentServerPuzzle(date) {
    const response = await fetch('/api/admin/dailytrivia', {headers:getHeaders});
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
    const puzzle = data.puzzles?.find(item => item.date === date);
    if (!puzzle) throw new Error('Актуальная викторина не найдена');
    return puzzle;
  }

  async function reconcileTriviaJob(job, command, submitted) {
    const date = command.date;
    const puzzle = await currentServerPuzzle(date);
    const committed = job.result_summary?.puzzle_revision;
    if (job.state === 'succeeded' && Number.isInteger(committed) && puzzle.revision === committed) {
      for (const section of command.part === 'all' ? ['main','super'] : [command.part]) saved(date,section,submitted[section],puzzle);
    } else {
      const state = drafts.receive(puzzle);
      if (job.reason_code === 'revision_conflict') conflict(date,{puzzle});
      if (selectedDate === date) renderDraft(state);
    }
    if (job.state !== 'succeeded') showToast(`Проверка/генерация завершена: ${job.reason_code || job.state}. Черновики сохранены.`,'error');
    await window.DailyJobs?.calendar();
  }

  async function submitTriviaJob(command, submitted, button, confirmationNote = '') {
    if (!window.DailyJobs) {showToast('Очередь подготовки недоступна. Обновите страницу.','error');return;}
    if (button.disabled) return;
    button.disabled = true;
    try {
      return await window.DailyJobs.run(command, {
        confirmationNote,
        onTerminal:job=>reconcileTriviaJob(job,command,submitted)
      });
    } catch(error) {
      if (error.status === 409) {conflict(command.date,error.data || {});await loadPuzzles();}
      showToast(`Не удалось принять операцию: ${error.message}. Черновики сохранены.`,'error');
    } finally {button.disabled = false;}
  }

  async function saveSection(section) {
    const state = captureDraft();
    if (!state) {showToast('Выберите дату','error');return;}
    if (state.conflict) {renderDraftStatus(state);showToast('Сначала разрешите конфликт с серверной версией. Черновики сохранены.','error');return;}
    const submitted = {main:drafts.clone(state.draftMain),super:drafts.clone(state.draftSuper)};
    const command = {kind:'trivia.validate_save',date:state.date,part:section,mode:'replace',expected_revision:state.baseRevision,questions:submitted[section]};
    return submitTriviaJob(command,submitted,document.getElementById(section==='main'?'trivia-btn-save':'trivia-btn-save-super'));
  }

  async function save() {return saveSection('main');}
  async function saveSuperQuestions() {return saveSection('super');}

  async function regenerate() {
    const state = captureDraft();
    if (!state) {showToast('Выберите дату','error');return;}
    if (state.conflict) {renderDraftStatus(state);return;}
    const doMain = document.getElementById('trivia-regen-main').checked;
    const doSuper = document.getElementById('trivia-regen-super').checked;
    if (!doMain && !doSuper) {showToast('Выберите хотя бы одну часть для регенерации','error');return;}
    const part = doMain && doSuper ? 'all' : doMain ? 'main' : 'super';
    const dirty = doMain && state.mainDirty || doSuper && state.superDirty;
    const note = dirty ? 'Несохранённые изменения выбранной части будут заменены. Соседний черновик сохранится.' : 'Соседний черновик сохранится.';
    const submitted = {main:drafts.clone(state.draftMain),super:drafts.clone(state.draftSuper)};
    return submitTriviaJob({kind:'trivia.regenerate',date:state.date,part,mode:'replace',expected_revision:state.baseRevision},
      submitted,document.getElementById('trivia-btn-regen'),note);
  }

  let currentLbType = 'daily';

  async function loadLeaderboard(type = 'daily') {
    const ticket=++leaderboardRequest;
    currentLbType = type;
    const container = document.getElementById('trivia-leaderboard-container');
    const titleEl = document.getElementById('trivia-lb-title');
    const dailyBtn = document.getElementById('trivia-lb-btn-daily');
    const monthlyBtn = document.getElementById('trivia-lb-btn-monthly');

    if (dailyBtn && monthlyBtn) {
      dailyBtn.className = type === 'daily' ? 's2048-btn primary' : 's2048-btn';
      monthlyBtn.className = type === 'monthly' ? 's2048-btn primary' : 's2048-btn';
    }

    if (titleEl) {
      titleEl.textContent = type === 'daily'
        ? `🏆 Лидерборд Trivia (за ${selectedDate || 'сегодня'})`
        : `🏆 Лидерборд Trivia (за текущий месяц)`;
    }

    try {
      let url = `/api/admin/dailytrivia/leaderboard?type=${type}`;
      if (type === 'daily' && selectedDate) url += `&date=${selectedDate}`;
      const res = await fetch(url, { headers: getHeaders });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if(ticket!==leaderboardRequest) return;
      const items = data.items || [];

      if (!items.length) {
        container.innerHTML = '<div style="color:var(--text-muted); font-size:0.85rem; padding:8px 0;">Записей пока нет</div>';
        return;
      }

      container.innerHTML = `
        <table style="width:100%; border-collapse:collapse; font-size:0.85rem; text-align:left;">
          <thead>
            <tr style="border-bottom:1px solid var(--border); color:var(--text-muted);">
              <th style="padding:6px;">#</th>
              <th style="padding:6px;">Игрок</th>
              <th style="padding:6px;">Очки</th>
              <th style="padding:6px;">Верно</th>
              ${type === 'monthly' ? '<th style="padding:6px;">Игр</th>' : ''}
            </tr>
          </thead>
          <tbody>
            ${items.map((item, idx) => `
              <tr style="border-bottom:1px solid var(--border-subtle, rgba(255,255,255,0.05));">
                <td style="padding:6px; font-weight:700; color:var(--accent);">${idx + 1}</td>
                <td style="padding:6px;">${escapeHtml(item.name || 'User ' + item.user_id)}</td>
                <td style="padding:6px; font-weight:600;">${item.score}</td>
                <td style="padding:6px;">${item.correct} ${type === 'daily' ? '/ 5' : ''}</td>
                ${type === 'monthly' ? `<td style="padding:6px;">${item.games_played}</td>` : ''}
              </tr>
            `).join('')}
          </tbody>
        </table>
      `;
    } catch (e) {
      if(ticket!==leaderboardRequest || e.code==='aborted') return;
      container.innerHTML = `<div style="color:var(--danger, #ef4444); font-size:0.85rem;">Ошибка лидерборда: ${escapeHtml(e.message)}</div>`;
    }
  }

  async function updateModel(newModel) {
    const model = String(newModel || '').trim();
    if (!model) {
      showToast('Укажите модель LLM', 'error');
      return;
    }
    if (settingsRevision === null || settingsConflict) {
      showToast('Обновите редактор модели перед сохранением. Локальный выбор сохранён.', 'error');
      return;
    }
    const expected=settingsRevision;
    const button=document.getElementById('trivia-model-save');
    button.disabled=true;button.setAttribute('aria-busy','true');
    try {
      const res = await fetch('/api/admin/dailytrivia/settings', {
        method: 'POST',
        headers: apiHeaders,
        body: JSON.stringify({ llm_model: model, expected_revision: settingsRevision })
      });
      const data = await res.json().catch(() => ({}));
      if (res.status === 409) {
        settingsConflict = true;
        document.getElementById('trivia-model-status').textContent = `Конфликт r${data.settings_revision}. Сейчас на сервере: ${(data.settings?.effective_models || []).join(' → ')}. Локальный выбор сохранён. Нажмите «Обновить редактор модели», чтобы загрузить серверную версию.`;
        return;
      }
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      const input=document.getElementById('trivia-model-input');
      if(input.value.trim()===model) input.value=data.llm_model || model;
      savedModelValue=data.llm_model || model;
      modelDirty=input.value!==savedModelValue;
      DailySettingsState.saved(data.settings_revision);
      showToast(`Модель LLM успешно изменена на ${data.llm_model || model}`, 'success');
    } catch (e) {
      if(e.outcomeUnknown) {
        settingsConflict=true;
        const status=document.getElementById('trivia-model-status');
        status.textContent='Результат пока неизвестен. Читаем серверную ревизию; локальный выбор сохранён.';
        try {
          const state=await AdminApi.adminRequest('/api/admin/dailytrivia/stats',{headers:getHeaders});
          if(state.settings_revision>expected && state.llm_model===model) {
            settingsConflict=false;modelDirty=document.getElementById('trivia-model-input').value!==model;
            savedModelValue=model;
            DailySettingsState.saved(state.settings_revision);
            status.textContent=`Сохранение подтверждено чтением модели · r${state.settings_revision}.`;
          } else status.textContent=`Результат пока неизвестен · сервер r${state.settings_revision}: ${(state.effective_models || []).join(' → ')}. Локальный выбор сохранён. Обновите редактор модели перед повтором.`;
        } catch(_) {status.textContent='Результат пока неизвестен. Серверную ревизию прочитать не удалось. Локальный выбор сохранён; обновите редактор модели перед повтором.';}
      } else showToast(`Ошибка смены модели: ${e.message}`, 'error');
    } finally {button.disabled=false;button.setAttribute('aria-busy','false');}
  }

  function init() {
    if (initialized) return;
    initialized = true;
    window.addEventListener('daily-job-updated', event => {
      const job = event.detail;
      if (job.kind?.startsWith('trivia.') && ['succeeded','failed','cancelled','expired','unknown_delivery'].includes(job.state)) {
        loadPuzzles().catch(error => showToast(`Не удалось обновить викторину: ${error.message}`,'error'));
      }
    });
    loadStats();
    loadPuzzles();

    const pane = document.getElementById('trivia-editor-pane');
    pane.addEventListener('focusin', event => rememberFocus(event.target));
    pane.addEventListener('input', event => {rememberFocus(event.target); const state = captureDraft(); if (state) renderDraftStatus(state);});
    pane.addEventListener('change', () => {const state = captureDraft(); if (state) renderDraftStatus(state);});
    document.getElementById('trivia-refresh-list').addEventListener('click', () => loadPuzzles());
    document.getElementById('trivia-editor-reload').addEventListener('click', () => loadPuzzles({discard:true}));
    document.getElementById('trivia-editor-continue').addEventListener('click', () => {document.getElementById('trivia-editor-conflict').hidden=true;restoreFocus();});
    document.getElementById('trivia-btn-save').addEventListener('click', save);
    document.getElementById('trivia-btn-save-super').addEventListener('click', saveSuperQuestions);
    document.getElementById('trivia-btn-regen').addEventListener('click', regenerate);
    document.getElementById('trivia-btn-close').addEventListener('click', closeEditor);
    document.getElementById('trivia-lb-btn-daily').addEventListener('click', () => loadLeaderboard('daily'));
    document.getElementById('trivia-lb-btn-monthly').addEventListener('click', () => loadLeaderboard('monthly'));
    const modelInput = document.getElementById('trivia-model-input');
    modelInput.addEventListener('input', () => { modelDirty = modelInput.value!==savedModelValue; });
    document.getElementById('trivia-settings-reload').addEventListener('click', () => loadStats({discard:true}));
    document.getElementById('trivia-model-save').addEventListener('click', () => updateModel(modelInput.value));
    modelInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') updateModel(modelInput.value);
    });

    loadLeaderboard('daily');
  }

  return {init, hasDirtyDrafts:()=>drafts.hasDirty() || modelDirty,
    capture(section) {const state=captureDraft(); return state ? {date:state.date,revision:state.baseRevision,section,
      submitted:drafts.clone(section==='main'?state.draftMain:state.draftSuper),conflict:!!state.conflict} : null;},
    saved,
    receive(puzzle) {const state=drafts.receive(puzzle); if(selectedDate===puzzle.date) renderDraft(state);return state;},
    conflict, refresh:loadPuzzles
  };
})();
window.TriviaModule = TriviaModule;
