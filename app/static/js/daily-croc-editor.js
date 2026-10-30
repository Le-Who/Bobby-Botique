const CrocModule = (() => {
  const fetch = (...args) => (window.DailyChrome?.fetch || window.AdminApi?.adminFetch || window.fetch)(...args);
  let initialized = false;
  let currentPuzzles = [];
  let kyivTodayStr = '';
  let imageModels = [];
  let savedTextModel = '';
  let savedProcessModels = {};
  let savedImageModel = '';
  let settingsRevision = null;
  let settingsConflict = false;
  let modelsLoaded = false;
  let dayRequest = 0;
  let puzzleRequest = 0;
  let modelRequest = 0;
  const promptDrafts = new Map(), openPrompts = new Set();
  const imageModelDrafts = new Map();
  const promptKey=(date,diff)=>`${date}-${diff}`;
  function capturePromptDrafts() {
    for(const puzzle of currentPuzzles) {
      const key=promptKey(puzzle.date,puzzle.difficulty), input=document.getElementById(`croc-prompt-input-${key}`);
      if(!input) continue;
      let draft=promptDrafts.get(key);
      if(!draft && input.value!==(puzzle.image_prompt || '')) {
        draft={baseValue:puzzle.image_prompt || '',baseRevision:puzzle.content_revision || 0,value:input.value};promptDrafts.set(key,draft);
      }
      if(draft) {draft.value=input.value;if(draft.value===draft.baseValue&&!draft.saving&&!draft.conflict)promptDrafts.delete(key);}
    }
  }
  function acknowledgePrompt(date,diff,submitted,puzzle) {
    capturePromptDrafts();
    const key=promptKey(date,diff);
    const draft=promptDrafts.get(key);
    if(draft?.value===submitted) promptDrafts.delete(key);
    else if(draft) {draft.baseValue=submitted;draft.baseRevision=puzzle.content_revision;draft.conflict=false;draft.saving=false;draft.status='Сохранение подтверждено. Более поздний ввод сохранён.';}
    refreshPuzzle(puzzle);
    document.getElementById(`croc-card-${key}`).querySelector('.prompt-save-status').textContent='Сохранение подтверждено. Более поздний ввод сохранён.';
  }

  function fillModels(select, models, saved) {
    select.replaceChildren();
    const match = models.find(model => model.id === saved || (model.aliases || []).includes(saved));
    const selected = match ? match.id : saved;
    const groups = new Map();
    const sourceLabels = {pollinations: 'Pollinations', fta: 'FTA'};
    for (const model of models) {
      const option = new Option(model.title || model.id, model.id, false, model.id === selected);
      const sourceLabel = sourceLabels[model.source];
      if (sourceLabel) {
        if (!groups.has(model.source)) {
          const group = document.createElement('optgroup');
          group.label = sourceLabel;
          groups.set(model.source, group);
          select.append(group);
        }
        groups.get(model.source).append(option);
      } else {
        select.add(option);
      }
    }
    if (!match) select.add(new Option(`${saved || 'Авто'} (сохранено)`, saved, true, true));
  }

  DailySettingsState.subscribe(revision => {
    if (settingsRevision !== null && !settingsConflict) {
      settingsRevision = revision;
      document.getElementById('croc-model-status').textContent = `Модели: r${revision}`;
    }
  });

  async function reconcileSettingsUnknown({process='',image=false,submitted,expected}) {
    settingsConflict=true;
    const status=document.getElementById('croc-model-status');
    status.textContent='Результат пока неизвестен. Читаем серверную ревизию; локальный выбор сохранён.';
    try {
      const state=await AdminApi.adminRequest('/api/admin/dailycroc/models',{headers:getHeaders});
      const current=image?state.default_image_model:process?state.text_processes?.find(role=>role.id===process)?.model:state.text_model;
      if(state.settings_revision>expected && current===submitted) {
        settingsConflict=false;
        if(image)savedImageModel=submitted;else if(process)savedProcessModels[process]=submitted;else savedTextModel=submitted;
        DailySettingsState.saved(state.settings_revision);
        status.textContent=`Сохранение подтверждено чтением модели · r${state.settings_revision}.`;
      } else status.textContent=`Результат пока неизвестен · сервер r${state.settings_revision}, модель ${current || 'Auto'}. Локальный выбор сохранён. Обновите редактор моделей перед повтором.`;
    } catch(_) {status.textContent='Результат пока неизвестен. Прочитать серверную ревизию не удалось. Локальный выбор сохранён; обновите редактор моделей перед повтором.';}
  }

  async function loadModels({discard = false} = {}) {
    if (modelsLoaded && !discard) return;
    const ticket=++modelRequest;
    const select = document.getElementById('croc-text-model');
    try {
      const res = await fetch('/api/admin/dailycroc/models', { headers: getHeaders });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if(ticket!==modelRequest || (settingsRevision!==null && data.settings_revision<settingsRevision)) return;
      if (!Number.isInteger(data.settings_revision)) throw new Error('Не удалось подтвердить ревизию настроек');
      settingsRevision = data.settings_revision;
      settingsConflict = false;
      imageModels = data.image_models || [];
      savedTextModel = data.text_model || '';
      fillModels(select, data.text_models || [], savedTextModel);
      select.disabled = false;
      const processRoot = document.getElementById('croc-process-models');
      processRoot.replaceChildren();
      for (const process of data.text_processes || []) {
        savedProcessModels[process.id] = process.model || '';
        const field = document.createElement('div');
        field.className = 'field';
        const label = document.createElement('label');
        const input = document.createElement('select');
        input.id = `croc-process-${process.id}`;
        input.className = 'model-select';
        input.dataset.process = process.id;
        label.htmlFor = input.id;
        label.textContent = process.label;
        fillModels(input, (data.text_models || []).map(model => model.id ? model : {...model, title: 'Наследовать общую'}), process.model || '');
        field.append(label, input);
        processRoot.append(field);
      }
      const imageSelect = document.getElementById('croc-default-image-model');
      fillModels(imageSelect, imageModels, data.default_image_model || 'qwen-image');
      savedImageModel = imageSelect.value;
      imageSelect.disabled = false;
      document.getElementById('croc-model-status').textContent = data.catalog_unavailable
        ? 'Каталог Pollinations недоступен; сохранённые модели не изменены.' : `Модели: r${settingsRevision}`;
      modelsLoaded = true;
    } catch (e) {
      if(ticket!==modelRequest || e.code==='aborted') return;
      document.getElementById('croc-model-status').textContent = `Не удалось загрузить модели: ${e.message}`;
    }
  }

  async function saveTextModel(event) {
    const select = event?.target || document.getElementById('croc-text-model');
    const process = select.dataset.process || '';
    if (settingsRevision === null || settingsConflict) {
      showToast('Обновите редактор моделей перед сохранением. Локальный выбор сохранён.', 'error');
      return;
    }
    select.disabled = true;
    const expected=settingsRevision,submitted=select.value;
    try {
      const res = await fetch('/api/admin/dailycroc/text-model', {
        method: 'POST', headers: apiHeaders, body: JSON.stringify({ model: select.value, process, expected_revision: settingsRevision })
      });
      const data = await res.json();
      if (res.status === 409) {
        settingsConflict = true;
        const current = data.settings?.text_processes?.map(role => `${role.label || role.id}: ${(role.effective_models || []).join(' → ') || 'Auto'}`).join('; ') || data.settings?.text_model || 'Auto';
        document.getElementById('croc-model-status').textContent = `Конфликт r${data.settings_revision}. Сейчас на сервере: ${current}. Локальный выбор сохранён. Нажмите «Обновить редактор моделей», чтобы загрузить серверную версию.`;
        return;
      }
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      if (process) savedProcessModels[process] = data.model;
      else savedTextModel = data.model;
      DailySettingsState.saved(data.settings_revision);
      showToast('Текстовая модель сохранена');
    } catch (e) {
      if(e.outcomeUnknown) await reconcileSettingsUnknown({process,submitted,expected});
      else showToast(`Ошибка: ${e.message}`, 'error');
    } finally { select.disabled = false; }
  }

  async function saveDefaultImage() {
    const select = document.getElementById('croc-default-image-model');
    if (settingsRevision === null || settingsConflict) {
      showToast('Обновите редактор моделей перед сохранением. Локальный выбор сохранён.', 'error');
      return;
    }
    select.disabled = true;
    const expected=settingsRevision,submitted=select.value;
    try {
      const res = await fetch('/api/admin/dailycroc/image-default', {
        method: 'POST', headers: apiHeaders, body: JSON.stringify({model: select.value, expected_revision: settingsRevision})
      });
      const data = await res.json();
      if (res.status === 409) {
        settingsConflict = true;
        document.getElementById('croc-model-status').textContent = `Конфликт r${data.settings_revision}. Сейчас на сервере: ${data.settings?.default_image_model || 'Auto'}. Локальный выбор сохранён. Обновите редактор моделей для загрузки серверной версии.`;
        return;
      }
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      savedImageModel = data.model;
      DailySettingsState.saved(data.settings_revision);
      showToast('Модель новых изображений сохранена');
      loadStats();
    } catch (e) {
      if(e.outcomeUnknown) await reconcileSettingsUnknown({image:true,submitted,expected});
      else showToast(`Ошибка: ${e.message}`, 'error');
    } finally { select.disabled = false; }
  }

  async function loadImageQuota() {
    const input = document.getElementById('croc-image-quota');
    const button = document.getElementById('croc-image-quota-save');
    const status = document.getElementById('croc-image-quota-status');
    try {
      const res = await fetch('/api/admin/dailycroc/image-quota', {headers: getHeaders});
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      input.value = data.limit;
      input.disabled = false;
      button.disabled = false;
      status.textContent = '';
    } catch (e) { status.textContent = `Не удалось загрузить лимит: ${e.message}. Обновите страницу для повтора.`; }
  }

  async function saveImageQuota() {
    const input = document.getElementById('croc-image-quota');
    const button = document.getElementById('croc-image-quota-save');
    const status = document.getElementById('croc-image-quota-status');
    const limit = Number(input.value);
    if (!input.value.trim() || !Number.isInteger(limit) || limit < 0 || limit > 1000) {
      status.textContent = 'Введите целое число от 0 до 1000.';
      return;
    }
    input.disabled = true;
    button.disabled = true;
    try {
      const res = await fetch('/api/admin/dailycroc/image-quota', {
        method: 'POST', headers: apiHeaders, body: JSON.stringify({limit})
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      input.value = data.limit;
      status.textContent = 'Лимит сохранён. Применение на всех процессах — до 30 секунд; текущий расход не сбрасывается.';
    } catch (e) { status.textContent = `Лимит не сохранён: ${e.message}`; }
    finally { input.disabled = false; button.disabled = false; }
  }

  function renderDayStatus(data) {
    const root = document.getElementById('croc-day-status');
    root.replaceChildren();
    const states = {queued:'В очереди', running:'Подготовка идёт…', completed:'Подготовка завершена', partial:'Подготовлено частично', failed:'Не удалось завершить подготовку', interrupted:'Подготовка прервана', superseded:'Контент изменён; подготовку можно повторить'};
    const title = document.createElement('p');
    title.textContent = `${data.date}: ${data.fully_ready ? 'Всё готово' : 'Есть недостающие части'}${data.job ? ' · ' + (states[data.job.state] || data.job.state) : ''}`;
    root.append(title);
    for (const puzzle of data.puzzles || []) {
      const line = document.createElement('p');
      const flag = ready => ready ? '✅' : '❌';
      line.textContent = `${puzzle.difficulty.toUpperCase()}: слово ${flag(puzzle.word_ready)} · подсказки ${flag(puzzle.hints_ready)} · промпт ${flag(puzzle.prompt_ready)} · картинка ${flag(puzzle.image_ready)}`;
      root.append(line);
    }
    if (data.job?.errors?.length) {
      const error = document.createElement('p');
      error.textContent = `Проблемы: ${data.job.errors.join('; ')}`;
      root.append(error);
    }
  }

  async function inspectDay() {
    const date = document.getElementById('croc-day-date').value;
    const ticket = ++dayRequest;
    if (!date) {
      document.getElementById('croc-day-status').textContent = 'Выберите дату.';
      return;
    }
    try {
      const res = await fetch(`/api/admin/dailycroc/day?date=${encodeURIComponent(date)}`, {headers:getHeaders});
      const data = await res.json();
      if (ticket !== dayRequest) return;
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      renderDayStatus(data);
      document.getElementById('croc-day-prepare').disabled = false;
    } catch (e) {
      if (ticket !== dayRequest) return;
      document.getElementById('croc-day-status').textContent = `Проверка недоступна: ${e.message}. Нажмите «Проверить готовность», чтобы повторить.`;
      document.getElementById('croc-day-prepare').disabled = false;
    }
  }

  async function reconcileCrocJob(job) {
    await loadPuzzles();
    if (job.result_summary?.date === document.getElementById('croc-day-date').value) await inspectDay();
    if (job.state !== 'succeeded') showToast(`Подготовка завершена: ${job.reason_code || job.state}. Проверьте актуальный пазл.`, 'error');
    await window.DailyJobs?.calendar();
  }

  async function runCrocJob(command, button, confirmationNote = '') {
    if (!window.DailyJobs) {showToast('Очередь подготовки недоступна. Обновите страницу.','error');return;}
    if (button?.disabled) return;
    if (button) button.disabled = true;
    try {
      return await window.DailyJobs.run(command, {onTerminal:reconcileCrocJob,confirmationNote});
    } catch (error) {
      if (error.status === 409) await loadPuzzles();
      showToast(error.data?.blocked_reason === 'puzzle_in_use' ? 'Есть игроки: изменение отклонено. Результаты сохранены.' : error.message, 'error');
    } finally {if (button) button.disabled = false;}
  }

  async function prepareDay() {
    const date = document.getElementById('croc-day-date').value;
    if (!date) {await inspectDay();return;}
    return runCrocJob({kind:'croc.prepare_day',date,mode:'missing'}, document.getElementById('croc-day-prepare'));
  }

  async function loadStats() {
    try {
      const res = await fetch('/api/admin/dailycroc/stats', { headers: getHeaders });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      document.getElementById('croc-stats-total-subs').textContent = data.total_subscribed || '0';
      document.getElementById('croc-stats-subs-detail').textContent = `Осталось отправить: ${data.pending_today ?? 0}`;
      document.getElementById('croc-stats-games').textContent = data.finished || '0';
      document.getElementById('croc-stats-games-detail').textContent = `Побед: ${data.won ?? 0} | В игре: ${data.active ?? 0}`;
      document.getElementById('croc-stats-placeholder').textContent = `Баннер: ${data.placeholder_set ? 'Установлен ✅' : 'Не задан ⚠️'}`;
      document.getElementById('croc-stats-model').textContent = `Модель: ${data.image_model || '—'}`;
    } catch (e) { console.error('Croc stats error:', e); }
  }

  async function loadImage(img) {
    const fileId = img.dataset.fileId;
    if (!fileId) return;
    const container = img.parentNode;
    const spinner = document.createElement('div');
    spinner.className = 'image-spinner';
    container.appendChild(spinner);
    try {
      const res = await fetch(`/api/admin/dailycroc/image?file_id=${encodeURIComponent(fileId)}`, { headers: getHeaders });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      if(!img.isConnected) return;
      const objectUrl=URL.createObjectURL(blob);
      img.dataset.objectUrl=objectUrl;
      const release=()=>{URL.revokeObjectURL(objectUrl);delete img.dataset.objectUrl;};
      img.addEventListener('load',release,{once:true});img.addEventListener('error',release,{once:true});
      img.src = objectUrl;
      img.style.display = 'block';
    } catch (e) {
      img.style.display = 'none';
      const placeholder = document.createElement('div');
      placeholder.className = 'image-placeholder';
      placeholder.innerHTML = '<span>⚠️</span> <span>Ошибка загрузки</span>';
      container.appendChild(placeholder);
    } finally { spinner.remove(); }
  }

  async function loadPuzzles() {
    const container = document.getElementById('croc-puzzle-container');
    const ticket=++puzzleRequest;
    capturePromptDrafts();
    try {
      const res = await fetch('/api/admin/dailycroc', { headers: getHeaders });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if(ticket!==puzzleRequest) return;
      if (data.error) throw new Error(data.error);
      const received=data.puzzles || [];
      currentPuzzles=received.map(puzzle=>{
        const previous=currentPuzzles.find(old=>old.date===puzzle.date&&old.difficulty===puzzle.difficulty);
        return previous?.content_revision>puzzle.content_revision?previous:puzzle;
      }).concat(currentPuzzles.filter(old=>promptDrafts.has(promptKey(old.date,old.difficulty))&&!received.some(puzzle=>puzzle.date===old.date&&puzzle.difficulty===old.difficulty)));
      kyivTodayStr = getKyivDateString();
      await loadModels();
      if(ticket!==puzzleRequest) return;
      renderPuzzles(currentPuzzles, container);
      loadStats();
    } catch (e) {
      if(ticket!==puzzleRequest || e.code==='aborted') return;
      if(currentPuzzles.length) showToast(`Не удалось обновить пазлы: ${e.message}. Черновики сохранены.`,'error');
      else container.innerHTML = `<div class="loading-container error-text">Не удалось загрузить данные: ${escapeHtml(e.message)}</div>`;
    }
  }

  function revisionFor(date, diff) {
    return currentPuzzles.find(p => p.date === date && p.difficulty === diff)?.content_revision || 0;
  }

  function refreshPuzzle(puzzle) {
    const index = currentPuzzles.findIndex(p => p.date === puzzle.date && p.difficulty === puzzle.difficulty);
    if (index < 0) currentPuzzles.push(puzzle);
    else if(currentPuzzles[index].content_revision<=puzzle.content_revision) currentPuzzles[index] = puzzle;
    renderPuzzles(currentPuzzles, document.getElementById('croc-puzzle-container'));
  }

  const cardTemplate = document.createElement('template');
  cardTemplate.innerHTML = `
    <div class="puzzle-card"><div class="card-body">
      <div class="puzzle-header"><div class="puzzle-date"></div><div class="puzzle-diff"></div></div>
      <p class="s2048-meta played-notice" hidden></p><div class="badge-missing" hidden>Не создан</div>
      <div class="puzzle-info"><div class="puzzle-word-row"><div class="puzzle-word"></div></div><div class="puzzle-topic"></div></div>
      <div class="prompt-section prompt-controls"><div class="prompt-header"><span class="prompt-title">IMAGE PROMPT</span><button class="btn-text" data-croc-action="editPrompt">✏️ Изменить</button></div>
        <div class="puzzle-prompt"></div><div class="prompt-edit edit-hidden"><textarea class="prompt-input"></textarea>
          <div class="btn-row"><button class="btn btn-save" data-croc-action="savePrompt">Сохранить</button><button class="btn" data-croc-action="cancelPrompt">Отмена</button></div>
        </div>
      </div>
      <div class="prompt-section mt-8 model-controls"><div class="prompt-header"><span class="prompt-title">IMAGE MODEL</span></div><select class="model-select" data-croc-action="saveModel"></select></div>
    </div><div class="image-container"><img class="image-preview" alt="puzzle image" hidden><div class="image-placeholder"></div></div>
    <div class="card-actions"><div class="btn-row"><button class="btn btn-regen" data-croc-action="regen">🖼️ Обновить арт</button><button class="btn btn-reset" data-croc-action="reset">🔀 Новое слово</button></div></div></div>`;

  function renderPuzzles(puzzles, container) {
    const focusId=document.activeElement?.id;
    const selection=typeof document.activeElement?.selectionStart==='number'?[document.activeElement.selectionStart,document.activeElement.selectionEnd]:null;
    container.querySelectorAll('img[data-object-url]').forEach(image=>URL.revokeObjectURL(image.dataset.objectUrl));
    container.replaceChildren();
    if (!puzzles?.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = 'Нет пазлов для отображения';
      container.append(empty);
      return;
    }
    puzzles.forEach(p => {
      const card = cardTemplate.content.firstElementChild.cloneNode(true);
      const key = `${p.date}-${p.difficulty}`;
      const draft=promptDrafts.get(key);
      const missing = !p.target_word;
      const today = p.date === kyivTodayStr;
      card.id = `croc-card-${key}`;
      card.classList.toggle('card-today', today);
      card.querySelector('.puzzle-date').textContent = new Date(p.date).toLocaleDateString('ru-RU', {weekday:'short', month:'short', day:'numeric'});
      if (today) {
        const badge = document.createElement('span');
        badge.className = 'today-badge'; badge.textContent = 'сегодня';
        card.querySelector('.puzzle-date').append(badge);
      }
      const difficulty = card.querySelector('.puzzle-diff');
      difficulty.classList.add(p.difficulty === 'easy' ? 'diff-easy' : 'diff-hard');
      difficulty.textContent = `${p.difficulty} · r${p.content_revision || 0}`;
      const notice = card.querySelector('.played-notice');
      notice.hidden = !p.played;
      notice.textContent = `Есть игроки: слово, текст${p.image_file_id ? ' и изображение' : ''} заблокированы. Для другого пазла выберите новую дату.`;
      card.querySelector('.badge-missing').hidden = !missing;
      for (const selector of ['.puzzle-info','.prompt-controls','.model-controls','.image-container']) card.querySelector(selector).hidden = missing;
      card.querySelector('.puzzle-word').textContent = p.target_word || '';
      card.querySelector('.puzzle-topic').textContent = p.topic || '—';
      const prompt = card.querySelector('.puzzle-prompt');
      prompt.id = `croc-prompt-text-${key}`;
      prompt.textContent = p.image_prompt || '(Промпт отсутствует)';
      card.querySelector('.prompt-edit').id = `croc-prompt-edit-${key}`;
      const input = card.querySelector('.prompt-input');
      input.id = `croc-prompt-input-${key}`; input.value = draft?.value ?? p.image_prompt ?? ''; input.readOnly = !!p.played;
      input.setAttribute('aria-label',`Промпт изображения ${p.date}, ${p.difficulty}`);
      const editing=openPrompts.has(key)||!!draft;
      card.querySelector('.prompt-edit').classList.toggle('edit-hidden',!editing);
      prompt.classList.toggle('edit-hidden',editing);
      const feedback=document.createElement('p');feedback.className='prompt-save-status';feedback.setAttribute('role','status');feedback.setAttribute('aria-live','polite');
      if(draft && draft.baseRevision!==(p.content_revision || 0)) draft.conflict=true;
      feedback.textContent=draft?.status || (draft?.conflict?`Конфликт: локальная r${draft.baseRevision}, серверная r${p.content_revision}. Черновик сохранён; загрузите серверный промпт для сравнения.`:draft?'Есть несохранённый промпт.':'');
      card.querySelector('.prompt-edit').append(feedback);
      if(draft?.conflict) {
        const reload=document.createElement('button');reload.className='btn';reload.textContent='Загрузить серверный промпт';reload.dataset.crocAction='reloadPrompt';card.querySelector('.prompt-edit').append(reload);
      }
      const model = card.querySelector('.model-select');
      const imageDraft=imageModelDrafts.get(key);
      model.id = `croc-model-${key}`; model.disabled = !!p.played || !!imageDraft?.saving || !!imageDraft?.conflict;
      fillModels(model, imageModels, imageDraft?.value ?? p.image_model ?? 'qwen-image');
      const modelStatus=document.createElement('p');modelStatus.className='image-model-status';modelStatus.setAttribute('role','status');modelStatus.textContent=imageDraft?.status || '';
      card.querySelector('.model-controls').append(modelStatus);
      if(imageDraft?.conflict){const reload=document.createElement('button');reload.className='btn';reload.dataset.crocAction='reloadModel';reload.textContent='Загрузить серверную модель';card.querySelector('.model-controls').append(reload);}
      card.querySelector('[data-croc-action="editPrompt"]').disabled = !!p.played;
      card.querySelector('[data-croc-action="savePrompt"]').disabled = !!p.played || !!draft?.saving || !!draft?.conflict;
      const regen = card.querySelector('[data-croc-action="regen"]');
      regen.id = `croc-regen-${key}`; regen.disabled = missing || !!(p.played && p.image_file_id);
      regen.textContent = p.played && !p.image_file_id ? '🖼️ Добавить арт' : '🖼️ Обновить арт';
      const reset = card.querySelector('[data-croc-action="reset"]');
      reset.id = `croc-reset-${key}`; reset.disabled = !!p.played;
      card.querySelectorAll('[data-croc-action]').forEach(element => {element.dataset.date = p.date; element.dataset.diff = p.difficulty;});
      const image = card.querySelector('img');
      const placeholder = card.querySelector('.image-placeholder');
      placeholder.hidden = !!p.image_file_id;
      placeholder.textContent = '❌ Изображение отсутствует';
      if (p.image_file_id) {image.dataset.fileId = p.image_file_id; image.hidden = false; image.style.display = 'none';}
      container.append(card);
    });
    container.querySelectorAll('img[data-file-id]').forEach(loadImage);
    const focused=document.getElementById(focusId);
    if(focused && container.contains(focused)) {focused.focus({preventScroll:true});if(selection)focused.setSelectionRange?.(...selection);}
  }

  async function regenerate(date, diff) {
    const model = document.getElementById(`croc-model-${date}-${diff}`)?.value;
    return runCrocJob({kind:'croc.regenerate_image',date,difficulty:diff,mode:'replace',model,
      expected_revision:revisionFor(date,diff)}, document.getElementById(`croc-regen-${date}-${diff}`));
  }

  async function savePrompt(date, diff) {
    capturePromptDrafts();
    const key=promptKey(date,diff);
    let draft=promptDrafts.get(key);
    if(draft?.saving || draft?.conflict) {showToast('Сравните серверную версию перед повтором. Черновик сохранён.','error');return;}
    const newPrompt = document.getElementById(`croc-prompt-input-${date}-${diff}`).value;
    const expected=draft?.baseRevision ?? revisionFor(date,diff);
    if(!draft){draft={value:newPrompt,baseValue:newPrompt,baseRevision:expected};promptDrafts.set(key,draft);}
    const button=document.getElementById(`croc-card-${key}`).querySelector('[data-croc-action="savePrompt"]');
    button.disabled=true;button.setAttribute('aria-busy','true');if(draft)draft.saving=true;
    document.getElementById(`croc-card-${key}`).querySelector('.prompt-save-status').textContent='Сохраняем промпт…';
    try {
      const data=await AdminApi.adminRequest('/api/admin/dailycroc/prompt',{method:'POST',headers:apiHeaders,body:{date,difficulty:diff,prompt:newPrompt,expected_revision:expected}});
      acknowledgePrompt(date,diff,newPrompt,data.puzzle);
      showToast('Промпт сохранён.', 'success');
    } catch (e) {
      if(e.outcomeUnknown) {
        if(draft) {draft.conflict=true;draft.status='Результат пока неизвестен. Проверяем ревизию; черновик сохранён.';}
        try {
          const data=await AdminApi.adminRequest('/api/admin/dailycroc',{headers:getHeaders});
          const server=data.puzzles?.find(puzzle=>puzzle.date===date&&puzzle.difficulty===diff);
          if(server?.content_revision>expected && server.image_prompt===newPrompt) acknowledgePrompt(date,diff,newPrompt,server);
          else {if(draft)draft.status='Результат пока неизвестен. Серверный промпт отличается; сравните версии перед повтором.';if(server)refreshPuzzle(server);}
        } catch(_) {if(draft)draft.status='Результат пока неизвестен. Проверить серверную версию не удалось; черновик сохранён.';}
      } else {
        if(draft && e.status===409) {draft.conflict=true;draft.status='Конфликт: локальный промпт сохранён. Загрузите серверную версию для сравнения.';}
        if(e.data?.puzzle)refreshPuzzle(e.data.puzzle);
        showToast(e.message,'error');
      }
    } finally {
      const current=promptDrafts.get(key);if(current)current.saving=false;
      if(current?.conflict && !document.getElementById(`croc-card-${key}`)?.querySelector('[data-croc-action="reloadPrompt"]'))renderPuzzles(currentPuzzles,document.getElementById('croc-puzzle-container'));
      const currentButton=document.getElementById(`croc-card-${key}`)?.querySelector('[data-croc-action="savePrompt"]');
      if(currentButton){currentButton.disabled=!!current?.conflict || !!currentPuzzles.find(puzzle=>promptKey(puzzle.date,puzzle.difficulty)===key)?.played;currentButton.setAttribute('aria-busy','false');}
      const status=document.getElementById(`croc-card-${key}`)?.querySelector('.prompt-save-status');if(current?.status&&status)status.textContent=current.status;
    }
  }

  async function saveModel(date, diff, model) {
    const key=promptKey(date,diff), existing=imageModelDrafts.get(key);
    if(existing?.saving || existing?.conflict)return;
    capturePromptDrafts();
    const expected=revisionFor(date,diff);
    const puzzle=currentPuzzles.find(puzzle=>promptKey(puzzle.date,puzzle.difficulty)===key);
    const state={value:model,baseValue:puzzle?.image_model || 'qwen-image',saving:true,conflict:false,status:'Сохраняем модель…'};
    imageModelDrafts.set(key,state);
    document.getElementById(`croc-model-${key}`).disabled=true;
    const confirmed=server=>{
      imageModelDrafts.delete(key);
      const prompt=promptDrafts.get(key);
      if(prompt?.baseRevision===expected && prompt.baseValue===(server.image_prompt || '')) prompt.baseRevision=server.content_revision;
      refreshPuzzle(server);
      document.getElementById(`croc-card-${key}`).querySelector('.image-model-status').textContent='Сохранение модели подтверждено.';
    };
    try {
      const data=await AdminApi.adminRequest('/api/admin/dailycroc/model',{method:'POST',headers:apiHeaders,body:{date,difficulty:diff,model,expected_revision:expected}});
      confirmed(data.puzzle);
      showToast('Модель обновлена.', 'success');
    } catch (e) {
      state.saving=false;state.conflict=true;state.status=e.outcomeUnknown?'Результат пока неизвестен. Локальный выбор сохранён.':'Сохранение не подтверждено. Локальный выбор сохранён; сравните серверную модель.';
      if(e.outcomeUnknown) {
        try {
          const data=await AdminApi.adminRequest('/api/admin/dailycroc',{headers:getHeaders});
          const server=data.puzzles?.find(puzzle=>puzzle.date===date&&puzzle.difficulty===diff);
          if(server?.content_revision>expected && server.image_model===model)confirmed(server);
          else {if(server)refreshPuzzle(server);else renderPuzzles(currentPuzzles,document.getElementById('croc-puzzle-container'));}
        } catch(_) {renderPuzzles(currentPuzzles,document.getElementById('croc-puzzle-container'));}
      } else {if(e.data?.puzzle)refreshPuzzle(e.data.puzzle);else renderPuzzles(currentPuzzles,document.getElementById('croc-puzzle-container'));showToast(e.message,'error');}
    }
  }

  async function resetWord(date, diff) {
    return runCrocJob({kind:'croc.reset_word',date,difficulty:diff,mode:'replace',expected_revision:revisionFor(date,diff)},
      document.getElementById(`croc-reset-${date}-${diff}`), 'Если есть игроки, изменение отклоняется. Результаты сохраняются.');
  }

  function init() {
    if (initialized) return;
    initialized = true;
    window.addEventListener('daily-job-updated', event => {
      const job = event.detail;
      if (job.kind?.startsWith('croc.') && ['succeeded','failed','cancelled','expired','unknown_delivery'].includes(job.state)) {
        reconcileCrocJob(job).catch(error => showToast(`Не удалось обновить пазл: ${error.message}`,'error'));
      }
    });
    loadPuzzles();
    document.getElementById('croc-text-model').addEventListener('change', saveTextModel);
    document.getElementById('croc-settings-reload').addEventListener('click', () => loadModels({discard:true}));
    document.getElementById('croc-process-models').addEventListener('change', event => {
      if (event.target.matches('select[data-process]')) saveTextModel(event);
    });
    document.getElementById('croc-default-image-model').addEventListener('change', saveDefaultImage);
    loadImageQuota();
    document.getElementById('croc-image-quota-save').addEventListener('click', saveImageQuota);
    document.getElementById('croc-day-date').value = getKyivDateString();
    document.getElementById('croc-day-date').addEventListener('change', inspectDay);
    document.getElementById('croc-day-check').addEventListener('click', inspectDay);
    document.getElementById('croc-day-prepare').addEventListener('click', prepareDay);
    document.getElementById('croc-puzzle-container').addEventListener('input',capturePromptDrafts);
    document.getElementById('croc-puzzle-container').addEventListener('click', async (e) => {
      const target = e.target.closest('[data-croc-action]');
      if (!target) return;
      const action = target.dataset.crocAction;
      const date = target.dataset.date;
      const diff = target.dataset.diff;
      if (action === 'regen') regenerate(date, diff);
      else if (action === 'reset') resetWord(date, diff);
      else if (action === 'editPrompt') {
        openPrompts.add(promptKey(date,diff));
        document.getElementById(`croc-prompt-text-${date}-${diff}`).classList.add('edit-hidden');
        document.getElementById(`croc-prompt-edit-${date}-${diff}`).classList.remove('edit-hidden');
      }
      else if (action === 'savePrompt') savePrompt(date, diff);
      else if (action === 'reloadPrompt') {
        if(!await showConfirm('Загрузить серверный промпт','Локальный промпт будет заменён текущей серверной версией.'))return;
        promptDrafts.delete(promptKey(date,diff));renderPuzzles(currentPuzzles,document.getElementById('croc-puzzle-container'));
      }
      else if(action==='reloadModel') {
        if(!await showConfirm('Загрузить серверную модель','Локальный выбор модели будет заменён серверным значением.'))return;
        imageModelDrafts.delete(promptKey(date,diff));renderPuzzles(currentPuzzles,document.getElementById('croc-puzzle-container'));
      }
      else if (action === 'cancelPrompt') {
        if(promptDrafts.has(promptKey(date,diff))&&!await showConfirm('Отменить изменения','Удалить локальный несохранённый промпт?'))return;
        promptDrafts.delete(promptKey(date,diff));openPrompts.delete(promptKey(date,diff));
        const puzzle=currentPuzzles.find(puzzle=>puzzle.date===date&&puzzle.difficulty===diff);
        document.getElementById(`croc-prompt-input-${date}-${diff}`).value=puzzle?.image_prompt || '';
        document.getElementById(`croc-prompt-text-${date}-${diff}`).classList.remove('edit-hidden');
        document.getElementById(`croc-prompt-edit-${date}-${diff}`).classList.add('edit-hidden');
      }
    });
    document.getElementById('croc-puzzle-container').addEventListener('change', (e) => {
      if (e.target.matches('.model-select')) saveModel(e.target.dataset.date, e.target.dataset.diff, e.target.value);
    });
  }

  // Bind header refresh/security buttons (data-croc-action on header)
  document.addEventListener('click', (e) => {
    const target = e.target.closest('[data-croc-action]');
    if (!target) return;
    const action = target.dataset.crocAction;
    if (action === 'loadPuzzles') loadPuzzles();
    else if (action === 'toggleSecurity') {
      document.body.classList.toggle('security-mode');
      target.textContent = document.body.classList.contains('security-mode') ? '👁 Показать' : '🔒 Скрыть слова';
    }
  });

  return { init, hasDirtyDrafts:()=>{
    capturePromptDrafts();
    return [...promptDrafts.values(),...imageModelDrafts.values()].some(draft=>draft.value!==draft.baseValue) || (modelsLoaded && (
      document.getElementById('croc-text-model').value!==savedTextModel || document.getElementById('croc-default-image-model').value!==savedImageModel ||
      [...document.querySelectorAll('#croc-process-models select')].some(select=>select.value!==(savedProcessModels[select.dataset.process] || ''))));
  } };
})();
window.CrocModule = CrocModule;
