/* GemAI admin controls. All server-provided values enter the DOM as text. */
(function () {
  "use strict";

  function createDraftManager() {
    let snapshot = null;
    const drafts = new Map();
    const key = (section, id) => `${section}\u0000${id}`;
    return {
      setSnapshot(value) { snapshot = value; },
      getSnapshot() { return snapshot; },
      setDraft(section, id, value) {
        const name = key(section, id);
        const previous = drafts.get(name);
        drafts.set(name, { value, expected_revision: previous ? previous.expected_revision : snapshot.revision });
      },
      getDraft(section, id) { return drafts.get(key(section, id)); },
      clearDraft(section, id) { drafts.delete(key(section, id)); },
      acceptSavedDraft(section, id, submitted, baseRevision, savedRevision) {
        const name = key(section, id);
        const current = drafts.get(name);
        if (current === submitted) drafts.delete(name);
        else if (current && current.expected_revision === baseRevision) {
          // New typing in this card follows its own successful write, never
          // an unrelated revision fetched later from another administrator.
          current.expected_revision = savedRevision;
        }
      },
      rebaseDraft(section, id) {
        const draft = drafts.get(key(section, id));
        if (draft) draft.expected_revision = snapshot.revision;
      },
      entries(section) {
        return [...drafts.entries()]
          .filter(([name]) => name.startsWith(`${section}\u0000`))
          .map(([name, draft]) => [name.split("\u0000")[1], draft]);
      },
      count() { return drafts.size; },
    };
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { createDraftManager };
  }
  if (typeof document === "undefined") return;

  const drafts = createDraftManager();
  const byId = (id) => document.getElementById(id);
  let csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const endpoint = "/api/admin/controls";
  let busy = false;
  let needsRefresh = false;
  let loadSequence = 0;
  let minimumRevision = 0;
  const pendingSaves = [];

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined && value !== null) node.textContent = String(value);
    return node;
  }

  function action(label, className, handler) {
    const node = element("button", `button ${className || ""}`, label);
    node.type = "button";
    node.addEventListener("click", handler);
    return node;
  }

  function message(node, value, error) {
    node.textContent = value;
    node.classList.toggle("error", !!error);
  }

  function notice(value, error) { message(byId("notice"), value, error); }

  async function api(path, body) {
    const options = { credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.headers["X-CSRF-Token"] = csrf;
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    let data;
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) {
      const detail = data.error || data.message || `HTTP ${response.status}`;
      const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function errorText(error) {
    if (error.status === 409) return "Конфигурация изменилась после начала редактирования. Ваш текст сохранён на странице. Обновите данные и перенесите правку в новую ревизию.";
    if (error.status === 401 || error.status === 403) return "Сессия завершилась или доступ запрещён. Войдите снова перед сохранением.";
    return error.message || "Запрос не выполнен. Повторите попытку.";
  }

  function setBusy(value) {
    // A refresh begun before a write must not replace the card while that
    // write is pending, or its error would target detached feedback.
    if (value && !busy) loadSequence += 1;
    busy = value;
    byId("refresh").disabled = value;
    document.querySelectorAll("button[data-mutation], button[data-draft-action]").forEach((button) => { button.disabled = value || needsRefresh; });
  }

  async function load() {
    const sequence = ++loadSequence;
    try {
      const data = await api(endpoint);
      if (sequence !== loadSequence) return { ok: false, superseded: true };
      if (!Number.isSafeInteger(data.revision) || data.revision < minimumRevision ||
          ![data.processes, data.prompts, data.catalogs, data.limits, data.history].every(Array.isArray)) {
        throw new Error("Не получена актуальная конфигурация. Обновите данные ещё раз.");
      }
      if (data.degraded && needsRefresh) {
        throw new Error("Источник настроек недоступен. Подтвердите сохранённую ревизию повторным обновлением.");
      }
      if (data.csrf_token) csrf = data.csrf_token;
      drafts.setSnapshot(data);
      minimumRevision = Math.max(minimumRevision, data.revision);
      pendingSaves.splice(0).forEach(saved => drafts.acceptSavedDraft(
        saved.section, saved.id, saved.draft, saved.baseRevision, saved.revision));
      needsRefresh = false;
      render();
      setBusy(busy);
      notice(data.degraded
        ? "Часть данных недоступна. Проверьте источник и повторите загрузку перед сохранением."
        : `Данные обновлены · ревизия ${data.revision}${drafts.count() ? ` · черновиков: ${drafts.count()}` : ""}`,
      !!data.degraded);
      return { ok: true, data };
    } catch (error) {
      if (sequence !== loadSequence) return { ok: false, superseded: true };
      notice(`Не удалось загрузить настройки. ${errorText(error)}`, true);
      return { ok: false, error };
    }
  }

  function header(item, id) {
    const heading = element("div", "card-heading");
    const names = element("div");
    names.append(element("h3", "", item.title || id), element("div", "technical muted", id));
    const badges = element("div", "badges");
    if (item.source) badges.append(element("span", "badge", `Источник: ${item.source}`));
    if (item.execution) badges.append(element("span", "badge", item.execution));
    heading.append(names, badges);
    return heading;
  }

  function meta(item) {
    const row = element("div", "metadata");
    if (item.group) row.append(element("span", "", `Группа: ${item.group}`));
    if (item.apply) {
      const timing = item.apply === "new_session" ? "в новой сессии"
        : item.apply === "new_role" ? "при новом выборе роли"
        : item.apply === "next_request" ? "со следующего запроса"
        : item.apply === "inactive" ? "не используется"
        : item.apply === "manual_migration" ? "отдельная миграция" : item.apply;
      row.append(element("span", "", `Применение: ${timing}`));
    }
    if (Array.isArray(item.capabilities) && item.capabilities.length) {
      const labels = { text: "текст", image: "анализ изображений", json: "структурированный ответ", tools: "инструменты", embedding: "поиск по памяти", audio_output: "синтез речи", audio_input: "распознавание речи", image_output: "генерация изображений", music: "музыка", live: "голосовой сеанс", provider_order: "порядок провайдеров" };
      row.append(element("span", "", `Назначение: ${item.capabilities.map(value => labels[value] || value).join(", ")}`));
    }
    return row;
  }

  function card(section, id) {
    const node = element("article", "control-card");
    node.dataset.section = section;
    node.dataset.id = id;
    if (drafts.getDraft(section, id)) node.classList.add("dirty");
    return node;
  }

  function feedback(node) {
    const text = element("p", "feedback");
    text.setAttribute("role", "status");
    text.setAttribute("aria-live", "polite");
    node.append(text);
    return text;
  }

  function markDraft(section, id, value, node, status) {
    drafts.setDraft(section, id, value);
    node.classList.add("dirty");
    if (section === "process" || section === "prompt") {
      const content = section === "process" ? value.models : [value];
      node.dataset.search = [node.dataset.searchIdentity, ...content].join(" ").toLocaleLowerCase("ru");
    }
    message(status, `Черновик · исходная ревизия ${drafts.getDraft(section, id).expected_revision}`);
    discardDraftControl(node, section, id);
  }

  function discardDraftControl(node, section, id) {
    const actions = node.querySelector(".actions");
    if (!actions || !drafts.getDraft(section, id) || actions.querySelector("[data-draft-action]")) return;
    const button = action("Отменить черновик", "quiet", () => {
      if (busy || needsRefresh) return;
      drafts.clearDraft(section, id);
      render();
      const replacement = [...document.querySelectorAll("article[data-id]")].find(card =>
        card.dataset.section === section && card.dataset.id === id);
      replacement?.querySelector("input, textarea, select")?.focus({ preventScroll: true });
      notice(`Черновик отменён. Показано последнее загруженное значение${drafts.count() ? ` · других черновиков: ${drafts.count()}` : ""}.`);
    });
    button.dataset.draftAction = "true";
    button.disabled = busy || needsRefresh;
    actions.append(button);
  }

  function actionRow(node) {
    const row = element("div", "actions");
    node.append(row);
    return row;
  }

  function staleDraftControl(node, section, id, currentValue, status) {
    discardDraftControl(node, section, id);
    const draft = drafts.getDraft(section, id);
    const revision = drafts.getSnapshot().revision;
    if (!draft || draft.expected_revision === revision) return;
    const panel = element("div", "stale-draft");
    panel.append(element("strong", "", `Черновик от ревизии ${draft.expected_revision}; сейчас ревизия ${revision}.`));
    const details = element("details", "baseline");
    details.append(element("summary", "", "Посмотреть текущее значение перед переносом"));
    details.append(element("pre", "", typeof currentValue === "string" ? currentValue : JSON.stringify(currentValue, null, 2)));
    panel.append(details);
    panel.append(action("Перенести черновик на текущую ревизию", "secondary", () => {
      drafts.rebaseDraft(section, id);
      panel.remove();
      message(status, `Черновик перенесён на ревизию ${revision}. Проверьте значения и сохраните.`);
    }));
    node.append(panel);
  }

  function mutationButton(label, className, handler) {
    const button = action(label, className, handler);
    button.dataset.mutation = "true";
    button.disabled = busy || needsRefresh;
    return button;
  }

  async function save(section, id, status, reset) {
    if (busy || needsRefresh) return;
    const draft = drafts.getDraft(section, id);
    if (!draft && !reset) { message(status, "Изменений для сохранения нет."); return; }
    const payload = { section, id, expected_revision: draft ? draft.expected_revision : drafts.getSnapshot().revision };
    if (reset) payload.reset = true;
    else payload.value = draft.value;
    if (!reset && (section === "process" || section === "catalog")) {
      const models = section === "process" ? payload.value.models : payload.value;
      if (!Array.isArray(models) || (section === "process" && models.length === 0) || models.some((model) => !model)) {
        message(status, section === "process" ? "Укажите хотя бы один непустой ID модели." : "Уберите пустые строки модели перед сохранением.", true);
        return;
      }
    }
    if (!reset && section === "limit" && payload.value !== null &&
      (!Number.isSafeInteger(payload.value) || payload.value <= 0)) {
      message(status, "Введите целое число больше нуля или оставьте поле пустым.", true);
      return;
    }
    setBusy(true);
    message(status, reset ? "Восстанавливаем базовое значение…" : "Сохраняем…");
    try {
      const result = await api(endpoint, payload);
      minimumRevision = Math.max(minimumRevision, result.revision);
      needsRefresh = true;
      pendingSaves.push({ section, id, draft, baseRevision: payload.expected_revision, revision: result.revision });
      const refreshed = await load();
      if (refreshed.ok) {
        notice(`Сохранено · ревизия ${result.revision}.${drafts.count() ? ` На странице осталось черновиков: ${drafts.count()}.` : ""}`);
      } else if (!refreshed.superseded) {
        notice(`Изменения сохранены на сервере · ревизия ${result.revision}, но обновить данные не удалось. Текст оставлен на странице. Нажмите «Обновить данные» перед следующими изменениями.`, true);
      }
    } catch (error) {
      message(status, errorText(error), true);
    } finally { setBusy(false); }
  }

  function modelRows(holder, models, changed) {
    holder.replaceChildren();
    models.forEach((name, index) => {
      const row = element("div", "model-row");
      row.append(element("span", "model-number", String(index + 1).padStart(2, "0")));
      const input = element("input", "model-input");
      input.type = "text";
      input.value = name;
      input.setAttribute("aria-label", `Модель ${index + 1}`);
      input.placeholder = "ID модели";
      input.addEventListener("input", changed);
      row.append(input);
      const commands = [
        ["↑", "Выше", index > 0, -1],
        ["↓", "Ниже", index < models.length - 1, 1],
        ["×", "Удалить модель", true, 0],
      ];
      commands.forEach(([glyph, label, enabled, delta]) => {
        const button = action(glyph, "icon", () => {
          const next = [...holder.querySelectorAll(".model-input")].map((field) => field.value);
          if (delta === 0) next.splice(index, 1);
          else [next[index], next[index + delta]] = [next[index + delta], next[index]];
          modelRows(holder, next, changed);
          changed();
          const nextIndex = delta === 0 ? Math.min(index, next.length - 1) : index + delta;
          const target = holder.children[nextIndex]?.querySelector("input") ||
            holder.closest("article")?.querySelector(".actions button");
          target?.focus({ preventScroll: true });
        });
        button.setAttribute("aria-label", `${label}: ${name || `строка ${index + 1}`}`);
        button.disabled = !enabled;
        row.append(button);
      });
      holder.append(row);
    });
  }

  function renderProcess(item) {
    const id = String(item.id);
    const node = card("process", id);
    node.dataset.group = item.group || "";
    node.dataset.searchIdentity = [id, item.title].join(" ");
    node.dataset.search = [node.dataset.searchIdentity, ...(drafts.getDraft("process", id)?.value.models || item.models || [])].join(" ").toLocaleLowerCase("ru");
    node.append(header(item, id), meta(item));
    if (item.note) node.append(element("p", item.editable ? "muted" : "readonly", item.note));
    if (item.scope) node.append(element("p", "control-note", item.scope));
    if (item.cache_note) node.append(element("p", "muted", item.cache_note));
    if (Array.isArray(item.evidence) && item.evidence.length) {
      const evidence = element("details", "technical muted");
      evidence.append(element("summary", "", `Исполнитель: ${item.executor_family || "код приложения"}`));
      item.evidence.forEach(location => evidence.append(element("p", "", `${location.file} · ${location.function}`)));
      node.append(evidence);
    }
    if (!item.editable) {
      node.append(element("p", "readonly", item.apply === "manual_migration"
        ? "Смена модели выполняется отдельной миграцией с переиндексацией."
        : "Этот процесс пока не подключён к управлению. Настройка здесь недоступна."));
      return node;
    }
    const current = drafts.getDraft("process", id)?.value || {
      models: item.models || [], strategy: item.strategy || "sequential",
      inherit_user_model: !!item.inherit_user_model,
    };
    const chain = element("div", "model-chain");
    const strategyField = element("label", "field", "Порядок выполнения");
    const strategy = element("select");
    [["sequential", "Последовательно"], ["hedged", "Параллельный резерв"]].filter(([value]) => (item.strategies || ["sequential"]).includes(value)).forEach(([value, label]) => {
      const option = element("option", "", label);
      option.value = value;
      strategy.append(option);
    });
    strategy.value = current.strategy === "hedged" ? "hedged" : "sequential";
    strategyField.append(strategy);
    const inherit = element("label", "check");
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.checked = !!current.inherit_user_model;
    if ((item.capabilities || []).includes("provider_order")) inherit.hidden = true;
    inherit.append(checkbox, element("span", "", "Учитывать модель пользователя"));
    const options = element("div", "options");
    options.append(strategyField, inherit);
    const status = feedback(node);
    const readValue = () => ({
      models: [...chain.querySelectorAll(".model-input")].map((input) => input.value.trim()),
      strategy: strategy.value, inherit_user_model: checkbox.checked,
    });
    const changed = () => markDraft("process", id, readValue(), node, status);
    modelRows(chain, Array.isArray(current.models) ? current.models : [], changed);
    strategy.addEventListener("change", changed);
    checkbox.addEventListener("change", changed);
    node.insertBefore(chain, status);
    node.insertBefore(options, status);
    const actions = actionRow(node);
    actions.append(action("Добавить модель", "secondary", () => {
      const next = [...chain.querySelectorAll(".model-input")].map((field) => field.value);
      next.push("");
      modelRows(chain, next, changed);
      changed();
      chain.lastElementChild?.querySelector("input")?.focus();
    }));
    actions.append(action("Проверить маршрут", "secondary", async () => {
      if (busy) return;
      message(status, "Проверяем маршрут…");
      try {
        const result = await api(`${endpoint}/preview`, {
          section: "process", id, value: readValue(),
          expected_revision: drafts.getDraft("process", id)?.expected_revision ?? drafts.getSnapshot().revision,
        });
        const models = Array.isArray(result.models) ? result.models.join(" → ") : "—";
        const notes = Array.isArray(result.notes) ? result.notes.join(" · ") : "";
        message(status, `Маршрут: ${models}${notes ? ` · ${notes}` : ""}`, !result.valid);
      } catch (error) { message(status, errorText(error), true); }
    }));
    actions.append(mutationButton("Сбросить", "quiet", () => save("process", id, status, true)));
    actions.append(mutationButton("Сохранить маршрут", "save", () => save("process", id, status, false)));
    staleDraftControl(node, "process", id, {
      models: item.models || [], strategy: item.strategy || "sequential",
      inherit_user_model: !!item.inherit_user_model,
    }, status);
    if (drafts.getDraft("process", id)) message(status, `Черновик · исходная ревизия ${drafts.getDraft("process", id).expected_revision}`);
    return node;
  }

  function diffPreview(container, before, after) {
    container.replaceChildren();
    const left = String(before || "").split("\n");
    const right = String(after || "").split("\n");
    let changed = 0;
    const max = Math.max(left.length, right.length);
    for (let i = 0; i < max; i += 1) {
      if (left[i] === right[i]) continue;
      changed += 1;
      if (left[i] !== undefined) container.append(element("div", "diff-removed", `− ${left[i]}`));
      if (right[i] !== undefined) container.append(element("div", "diff-added", `+ ${right[i]}`));
    }
    if (!changed) container.append(element("div", "muted", "Отличий от базового шаблона нет."));
  }

  function renderPrompt(item) {
    const id = String(item.id);
    const node = card("prompt", id);
    node.dataset.searchIdentity = [id, item.title].join(" ");
    node.dataset.search = [node.dataset.searchIdentity, drafts.getDraft("prompt", id)?.value ?? item.text].join(" ").toLocaleLowerCase("ru");
    node.append(header(item, id), meta(item));
    if (Array.isArray(item.variables) && item.variables.length) {
      node.append(element("p", "muted technical", `Переменные: ${item.variables.join(", ")}`));
    }
    if (item.editable !== false && item.note) node.append(element("p", "muted", item.note));
    const input = element("textarea");
    input.value = drafts.getDraft("prompt", id)?.value ?? item.text ?? "";
    input.readOnly = item.editable === false;
    input.setAttribute("aria-label", `Текст промпта: ${item.title || id}`);
    input.spellcheck = false;
    node.append(input);
    const comparison = element("details", "baseline");
    comparison.append(element("summary", "", "Сравнить с базовым шаблоном"));
    const diff = element("div", "diff technical");
    diffPreview(diff, item.baseline, input.value);
    comparison.append(diff);
    node.append(comparison);
    if (item.editable === false) {
      node.append(element("p", "readonly", item.note || "Этот шаблон не используется текущими обработчиками."));
      if (item.current_prompt) {
        const link = element("a", "current-prompt-link", "Открыть действующую инструкцию");
        const url = new URL(location.href);
        url.searchParams.set("prompt", item.current_prompt);
        url.hash = "prompts";
        link.href = url.href;
        link.addEventListener("click", event => {
          if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
          event.preventDefault();
          const search = byId("prompt-search");
          search.value = item.current_prompt;
          search.dispatchEvent(new Event("input", { bubbles: true }));
          const current = [...byId("prompt-list").children].find(card => card.dataset.id === item.current_prompt);
          current?.querySelector("textarea")?.focus({ preventScroll: true });
          current?.scrollIntoView({ block: "start" });
        });
        node.append(link);
      }
      return node;
    }
    const status = feedback(node);
    input.addEventListener("input", () => {
      markDraft("prompt", id, input.value, node, status);
      if (comparison.open) diffPreview(diff, item.baseline, input.value);
    });
    comparison.addEventListener("toggle", () => { if (comparison.open) diffPreview(diff, item.baseline, input.value); });
    const actions = actionRow(node);
    actions.append(mutationButton("Сбросить к базовому", "quiet", () => save("prompt", id, status, true)));
    actions.append(mutationButton("Сохранить промпт", "save", () => save("prompt", id, status, false)));
    staleDraftControl(node, "prompt", id, item.text || "", status);
    if (drafts.getDraft("prompt", id)) message(status, `Черновик · исходная ревизия ${drafts.getDraft("prompt", id).expected_revision}`);
    return node;
  }

  function renderCatalog(item) {
    const id = String(item.provider);
    const node = card("catalog", id);
    node.append(header({ title: id, source: item.source }, id));
    const status = feedback(node);
    const chain = element("div", "model-chain");
    const current = drafts.getDraft("catalog", id)?.value ?? item.models ?? [];
    const changed = () => markDraft("catalog", id,
      [...chain.querySelectorAll(".model-input")].map((input) => input.value.trim()), node, status);
    modelRows(chain, Array.isArray(current) ? current : [], changed);
    node.insertBefore(chain, status);
    const actions = actionRow(node);
    actions.append(action("Добавить ID модели", "secondary", () => {
      const next = [...chain.querySelectorAll(".model-input")].map((input) => input.value);
      next.push("");
      modelRows(chain, next, changed);
      changed();
      chain.lastElementChild?.querySelector("input")?.focus();
    }));
    actions.append(mutationButton("Сбросить", "quiet", () => save("catalog", id, status, true)));
    actions.append(mutationButton("Сохранить каталог", "save", () => save("catalog", id, status, false)));
    staleDraftControl(node, "catalog", id, item.models || [], status);
    if (drafts.getDraft("catalog", id)) message(status, `Черновик · исходная ревизия ${drafts.getDraft("catalog", id).expected_revision}`);
    return node;
  }

  function renderLimit(item) {
    const id = String(item.model);
    const node = card("limit", id);
    node.append(header({ title: id, source: item.source }, id));
    if (item.note) node.append(element("p", "control-note", item.note));
    const field = element("label", "field", "Запросов на ключ в сутки");
    const input = element("input");
    input.type = "number";
    input.min = "1";
    input.step = "1";
    input.placeholder = "Без локального предела";
    const limitDraft = drafts.getDraft("limit", id);
    input.value = limitDraft ? (limitDraft.value ?? "") : (item.limit ?? "");
    field.append(input);
    node.append(field);
    const status = feedback(node);
    input.addEventListener("input", () => {
      const value = input.value === "" ? null : Number(input.value);
      markDraft("limit", id, value, node, status);
    });
    const actions = actionRow(node);
    actions.append(mutationButton("Сбросить", "quiet", () => save("limit", id, status, true)));
    actions.append(mutationButton("Сохранить лимит", "save", () => save("limit", id, status, false)));
    staleDraftControl(node, "limit", id, item.limit ?? null, status);
    if (drafts.getDraft("limit", id)) message(status, `Черновик · исходная ревизия ${drafts.getDraft("limit", id).expected_revision}`);
    return node;
  }

  function addCustom(section, holder, snapshotItems, idField, renderItem) {
    const form = element("div", "add-custom");
    const input = element("input");
    input.type = "text";
    input.placeholder = "gemini-…";
    input.setAttribute("aria-label", input.placeholder);
    const status = element("span", "feedback");
    const add = () => {
      const id = input.value.trim();
      if (!id) { message(status, "Введите ID.", true); return; }
      if (!id.startsWith("gemini-") || /\s/.test(id)) {
        message(status, "Лимит поддерживает только Gemini model ID без пробелов.", true); return;
      }
      if (snapshotItems.some((item) => String(item[idField]) === id) || drafts.getDraft(section, id)) {
        message(status, "Такой ID уже показан ниже.", true); return;
      }
      drafts.setDraft(section, id, null);
      holder.append(renderItem({ model: id, limit: null, source: "Новый" }));
      input.value = "";
      message(status, "Новая карточка добавлена. Укажите значение и сохраните.");
    };
    input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); add(); } });
    form.append(input, action("Добавить", "secondary", add), status);
    return form;
  }

  function renderHistory(items) {
    const holder = byId("history-list");
    holder.replaceChildren();
    if (!items.length) { holder.append(element("p", "muted", "История пока пуста.")); return; }
    items.forEach((entry) => {
      const row = element("div", "history-row");
      const details = element("div");
      details.append(element("strong", "technical", `Ревизия ${entry.revision}`));
      const date = new Date(entry.created_at);
      details.append(element("p", "", `${entry.changed_key || "Изменение"} · ${entry.actor || "Администратор"} · ${Number.isNaN(date.getTime()) ? entry.created_at : date.toLocaleString("ru-RU")}`));
      const status = element("span", "feedback");
      row.append(details, status);
      if (entry.revision !== drafts.getSnapshot().revision) {
        row.append(mutationButton("Восстановить", "secondary", async () => {
          if (busy || needsRefresh) return;
          const data = drafts.getSnapshot();
          const scope = `маршруты моделей (${data.processes.length}), промпты (${data.prompts.length}), каталоги и лимиты`;
          if (!window.confirm(`Восстановить ревизию ${entry.revision} всей конфигурации?\nБудут заменены ${scope}. Это создаст новую ревизию и повлияет на новые запросы. Черновики останутся на странице для сравнения.`)) return;
          setBusy(true);
          message(status, "Восстанавливаем…");
          try {
            const result = await api(`${endpoint}/restore`, {
              revision: entry.revision, expected_revision: drafts.getSnapshot().revision,
            });
            minimumRevision = Math.max(minimumRevision, result.revision);
            needsRefresh = true;
            const refreshed = await load();
            if (refreshed.ok) {
              notice(`Восстановлена ревизия ${entry.revision} · новая ревизия ${result.revision}. Черновики остались на странице.`);
            } else if (!refreshed.superseded) {
              notice(`Ревизия ${entry.revision} восстановлена на сервере · новая ревизия ${result.revision}, но обновить данные не удалось. Нажмите «Обновить данные» перед следующими изменениями. Черновики остались на странице.`, true);
            }
          } catch (error) { message(status, errorText(error), true); }
          finally { setBusy(false); }
        }));
      }
      holder.append(row);
    });
  }

  function render() {
    const focused = document.activeElement;
    const focusedCard = focused?.closest("article[data-id]");
    const focusIndex = focusedCard ? [...focusedCard.querySelectorAll("input, textarea, select, button")].indexOf(focused) : -1;
    const selection = focused && typeof focused.selectionStart === "number" ? [focused.selectionStart, focused.selectionEnd] : null;
    const data = drafts.getSnapshot();
    byId("revision").textContent = `Ревизия ${data.revision}`;
    const processes = Array.isArray(data.processes) ? data.processes : [];
    const prompts = Array.isArray(data.prompts) ? data.prompts : [];
    const catalogs = Array.isArray(data.catalogs) ? data.catalogs : [];
    const limits = Array.isArray(data.limits) ? data.limits : [];
    byId("process-count").textContent = String(processes.length);
    byId("prompt-count").textContent = String(prompts.length);
    const groups = ["Общение", "Поиск", "Изображения", "Аудио", "Документы", "Игры", "Астрология", "Память"];
    const collator = new Intl.Collator("ru", { numeric: true });
    const priority = id => id === "chat" ? 0 : id === "inline" ? 1 : 2;
    const groupOrder = item => groups.includes(item.group) ? groups.indexOf(item.group) : groups.length;
    const orderedProcesses = [...processes].sort((left, right) => groupOrder(left) - groupOrder(right) ||
      priority(left.id) - priority(right.id) || collator.compare(left.title || left.id, right.title || right.id));
    byId("process-list").replaceChildren(...orderedProcesses.map(renderProcess));
    const group = byId("process-group");
    const selectedGroup = group.value;
    group.replaceChildren(element("option", "", "Все группы"));
    group.firstElementChild.value = "";
    [...new Set(processes.map(item => item.group).filter(Boolean))].sort().forEach(name => {
      const option = element("option", "", name);
      option.value = name;
      group.append(option);
    });
    group.value = selectedGroup;
    const orderedPrompts = [...prompts].sort((left, right) => Number(left.editable === false) - Number(right.editable === false) ||
      collator.compare(left.title || left.id, right.title || right.id));
    byId("prompt-list").replaceChildren(...orderedPrompts.map(renderPrompt));
    const catalogHolder = byId("catalog-list");
    catalogHolder.replaceChildren(...catalogs.map(renderCatalog));
    const limitHolder = byId("limit-list");
    limitHolder.replaceChildren(addCustom("limit", limitHolder, limits, "model", renderLimit),
      ...limits.map(renderLimit));
    drafts.entries("limit").forEach(([id]) => {
      if (!limits.some((item) => String(item.model) === id)) limitHolder.append(renderLimit({ model: id, limit: null, source: "Новый" }));
    });
    renderHistory(Array.isArray(data.history) ? data.history : []);
    filterCards("process");
    filterCards("prompt");
    if (focusedCard && focusIndex >= 0) {
      const replacement = [...document.querySelectorAll("article[data-id]")].find(node =>
        node.dataset.id === focusedCard.dataset.id && node.dataset.section === focusedCard.dataset.section);
      const field = replacement?.querySelectorAll("input, textarea, select, button")[focusIndex];
      if (field && !field.disabled && !replacement.hidden) {
        field.focus({ preventScroll: true });
        if (selection && typeof field.setSelectionRange === "function" && field.type !== "number") field.setSelectionRange(...selection);
      }
    }
  }

  function filterCards(section) {
    const query = byId(`${section}-search`).value.trim().toLocaleLowerCase("ru");
    const group = section === "process" ? byId("process-group").value : "";
    const cards = [...byId(`${section}-list`).children];
    let visible = 0;
    cards.forEach(node => {
      node.hidden = (group && node.dataset.group !== group) || !(node.dataset.search || "").includes(query);
      if (!node.hidden) visible += 1;
    });
    byId(`${section}-visible`).textContent = `Показано ${visible} из ${cards.length}`;
  }

  ["process", "prompt"].forEach(section => byId(`${section}-search`).addEventListener("input", () => filterCards(section)));
  byId("process-group").addEventListener("change", () => filterCards("process"));

  byId("refresh").addEventListener("click", load);
  window.addEventListener("beforeunload", event => {
    if (!drafts.count() && !busy && !needsRefresh) return;
    event.preventDefault();
    event.returnValue = "";
  });
  load();
})();
