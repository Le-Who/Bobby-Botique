/* Read-only model evidence. Configuration assignments are never execution counts. */
(() => {
  "use strict";
  const byId = id => document.getElementById(id);
  const holder = byId("diagnostics-list");
  if (!holder) return;
  let configuration = null;
  let snapshot = null;
  let receivedAt = 0;
  let pending = false;
  let failed = false;
  let active = location.hash === "#diagnostics";
  const number = value => Number.isFinite(value) ? value.toLocaleString("ru") : "—";
  const node = (tag, className, text) => {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = String(text);
    return result;
  };

  function updateStatus() {
    if (!active) return;
    const status = byId("diagnostics-status");
    const stale = receivedAt > 0 && (failed || Date.now() - receivedAt > 60000);
    const partial = snapshot && (snapshot.providers.gemini.available === false || snapshot.providers.key_health.available === false);
    const degraded = snapshot?.providers.gemini.rows?.some(row => row.settings_degraded);
    status.className = `diagnostics-status${stale || failed || partial || degraded ? " warning" : " muted"}`;
    if (pending) status.textContent = "Получаем локальные счётчики и состояние ключей…";
    else if (!receivedAt) status.textContent = failed
      ? "Учёт недоступен. Назначения показаны по загруженным настройкам; повторите обновление."
      : "Учёт загрузится при открытии раздела.";
    else {
      const time = new Date(receivedAt).toLocaleTimeString("ru", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      status.textContent = `${stale ? "Данные устарели" : "Снимок получен"} в ${time}${failed ? ". Обновление не удалось; повторите попытку." : ". Обновите диагностику для нового снимка."}`;
      if (partial) status.textContent += " Часть источников недоступна; неизвестные значения показаны отдельно.";
      if (degraded) status.textContent += " Пороги RPD получены из последней подтверждённой конфигурации: хранилище настроек недоступно.";
    }
  }

  async function refresh() {
    if (pending) return;
    pending = true;
    byId("diagnostics-refresh").disabled = true;
    updateStatus();
    try {
      const response = await fetch("/api/dashboard", {
        credentials: "same-origin", headers: { Accept: "application/json" }, signal: AbortSignal.timeout(10000),
      });
      if (!response.ok) throw new Error("Snapshot unavailable");
      const data = await response.json();
      if (!data.providers || !data.providers.gemini || !data.providers.key_health) throw new Error("Invalid snapshot");
      snapshot = data;
      receivedAt = Date.now();
      failed = false;
    } catch (_) { failed = true; }
    finally {
      pending = false;
      byId("diagnostics-refresh").disabled = false;
      if (active) render();
      updateStatus();
    }
  }

  function models() {
    const result = new Map();
    function add(value) {
      if (typeof value !== "string" || !value.trim()) return null;
      if (!result.has(value)) result.set(value, { id: value, assignments: [], catalogs: [], rows: [], health: [] });
      return result.get(value);
    }
    for (const process of configuration?.processes || []) {
      if ((process.capabilities || []).includes("provider_order")) continue;
      for (const model of new Set(process.models || [])) {
        const entry = add(model);
        if (entry) entry.assignments.push(process);
      }
    }
    for (const catalog of configuration?.catalogs || []) {
      for (const model of catalog.models || []) {
        const entry = add(model);
        if (entry) entry.catalogs.push(catalog.title || catalog.provider || catalog.id || "Каталог");
      }
    }
    for (const limit of configuration?.limits || []) add(limit.model);
    const providers = snapshot?.providers;
    if (providers?.gemini.available !== false) {
      for (const row of providers?.gemini.rows || []) {
        const entry = add(row.model_name);
        if (entry) entry.rows.push(row);
      }
    }
    if (providers?.key_health.available !== false) {
      for (const health of providers?.key_health.rows || []) {
        const entry = add(health.model || health.model_name);
        if (entry) entry.health.push(health);
      }
    }
    return [...result.values()].sort((left, right) => {
      const priority = entry => entry.rows.some(row => row.is_available === false) ||
        entry.health.some(row => row.status === "suspended" || row.status === "disabled");
      return Number(priority(right)) - Number(priority(left)) ||
        right.assignments.length - left.assignments.length || left.id.localeCompare(right.id, "ru");
    });
  }

  function metric(label, value, detail) {
    const block = node("div");
    block.append(node("dt", "", label), node("dd", "", value));
    if (detail) block.append(node("p", "muted", detail));
    return block;
  }

  function healthText(row) {
    if (row.status === "suspended") {
      const until = new Date(row.suspended_until);
      return `Ключ приостановлен${Number.isFinite(until.getTime()) ? ` до ${until.toLocaleString("ru")}` : ""}`;
    }
    if (row.status === "disabled") return "Ключ отключён";
    if (row.suspension_expired) return "Срок приостановки истёк; новых проверок доступа нет";
    if (["active", "healthy"].includes(row.status)) return "В учёте ключ не приостановлен";
    return "Состояние ключа неизвестно";
  }

  function modelCard(item) {
    const article = node("article", "control-card diagnostic-card");
    article.dataset.model = item.id;
    article.dataset.assigned = String(item.assignments.length > 0);
    article.dataset.measured = String(item.rows.length > 0);
    const restricted = item.rows.some(row => row.is_available === false) ||
      item.health.some(row => ["suspended", "disabled"].includes(row.status));
    article.dataset.attention = String(restricted);
    article.dataset.search = [item.id, ...item.assignments.flatMap(process => [process.id, process.title])]
      .join(" ").toLocaleLowerCase("ru");
    const heading = node("div", "card-heading");
    heading.append(node("h3", "diagnostic-model", item.id),
      node("span", `diagnostic-state${restricted ? " warning" : ""}`,
        restricted ? "Есть локальное ограничение" : item.rows.length ? "Есть данные RPD" : "Нет данных RPD"));
    const metrics = node("dl", "diagnostic-metrics");
    const total = item.rows.every(row => Number.isFinite(row.request_count))
      ? item.rows.reduce((sum, row) => sum + row.request_count, 0) : null;
    const allowed = item.rows.filter(row => row.is_available === true).length;
    const count = item.assignments.length;
    const suffix = count % 10 === 1 && count % 100 !== 11 ? "маршрут"
      : [2, 3, 4].includes(count % 10) && ![12, 13, 14].includes(count % 100) ? "маршрута" : "маршрутов";
    const routeLabel = `${count} ${suffix}`;
    metrics.append(metric("Сохранённые назначения", routeLabel, "Основная или резервная модель"),
      metric("Запросы сегодня / RPD", item.rows.length ? number(total) : "Нет данных RPD", "Только общий локальный учёт Gemini"),
      metric("Ключи с допуском по RPD", item.rows.length ? `${allowed} из ${item.rows.length}` : "Неизвестно", "Приостановка ключа учитывается отдельно"));
    article.append(heading, metrics, node("p", "diagnostic-provider", "Доступ у провайдера не проверялся. Локальный допуск не подтверждает, что запрос выполнится."));
    const details = node("details", "diagnostic-details");
    details.append(node("summary", "", "Маршруты, ключи и причины"), node("h4", "", "Назначения в сохранённой конфигурации"));
    if (item.assignments.length) {
      const list = node("ul", "diagnostic-routes");
      for (const process of item.assignments) {
        const entry = node("li");
        const link = node("a", "", process.title || process.id);
        const url = new URL(location.href);
        url.hash = "processes";
        url.searchParams.set("process", process.id);
        url.searchParams.delete("group");
        link.href = url.pathname + url.search + url.hash;
        link.addEventListener("click", event => {
          if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
          event.preventDefault();
          history.pushState(null, "", link.href);
          window.dispatchEvent(new PopStateEvent("popstate"));
        });
        entry.append(link, node("span", "muted", ` · ${process.id}${process.inherit_user_model ? " · учитывает модель пользователя" : ""}`));
        list.append(entry);
      }
      details.append(list);
    } else details.append(node("p", "muted", "В сохранённых маршрутах не назначена. Данные счётчика могут относиться к другому пути или прежней конфигурации."));
    if (item.catalogs.length) details.append(node("p", "muted", `Каталоги выбора: ${[...new Set(item.catalogs)].join(", ")}. Членство в каталоге не является назначением в маршрут.`));
    details.append(node("h4", "", "Локальные счётчики и состояние ключей"));
    if (!item.rows.length) details.append(node("p", "muted", snapshot?.providers?.gemini.available === false
      ? "Источник счётчиков Gemini недоступен. Повторите обновление диагностики."
      : "Для этой модели нет записей общего учёта RPD. Отсутствие записей не означает нулевой расход."));
    const healthSource = snapshot?.providers?.key_health;
    // Truncation metadata belongs to the health summary in the canonical API.
    const incomplete = healthSource?.truncated ?? healthSource?.summary?.truncated;
    for (const row of item.rows) {
      const key = node("div", "diagnostic-key");
      key.append(node("strong", "technical", row.api_key_preview || "Ключ без метки"));
      const limit = row.daily_limit == null ? "без локального предела" : `суточный предел ${number(row.daily_limit)}`;
      key.append(node("p", "", `Учтено запросов: ${number(row.request_count)}; ${limit}.`));
      const admission = row.admission_limit != null ? `Порог допуска: ${number(row.admission_limit)}. ` : "";
      key.append(node("p", row.is_available === false ? "warning" : "muted", `${admission}${row.is_available === true ? "По локальному RPD допуск есть." : row.is_available === false ? "Локальный RPD блокирует новые запросы." : "Локальный допуск неизвестен."}`));
      const health = item.health.find(entry => entry.key_hash && entry.key_hash === row.key_hash);
      key.append(node("p", "muted", health ? healthText(health) : healthSource?.available === false || !healthSource || incomplete
        ? "Состояние ключа неизвестно: источник недоступен или показан не полностью."
        : "Для этой пары ключ/модель событий состояния нет. Доступ у провайдера неизвестен."));
      if (health?.last_error) key.append(node("p", "diagnostic-error", `Последняя записанная ошибка: ${health.last_error}`));
      details.append(key);
    }
    for (const health of item.health.filter(entry => !item.rows.some(row => row.key_hash && row.key_hash === entry.key_hash))) {
      const key = node("div", "diagnostic-key");
      key.append(node("strong", "technical", health.key || health.key_preview || "Ключ без метки"), node("p", "", healthText(health)));
      if (health.last_error) key.append(node("p", "diagnostic-error", `Последняя записанная ошибка: ${health.last_error}`));
      details.append(key);
    }
    if (restricted) details.append(node("p", "diagnostic-guidance", "Проверьте порог допуска и срок приостановки ключа. Дождитесь сброса локального счётчика или проверьте резервный маршрут. Изменяйте лимит только после проверки квоты аккаунта у провайдера."));
    article.append(details);
    return article;
  }

  function filter() {
    const query = byId("diagnostics-search").value.trim().toLocaleLowerCase("ru");
    const kind = byId("diagnostics-filter").value;
    const cards = [...holder.children];
    let count = 0;
    for (const card of cards) {
      card.hidden = !card.dataset.search.includes(query) || (kind === "attention" && card.dataset.attention !== "true") ||
        (kind === "assigned" && card.dataset.assigned !== "true") || (kind === "unmeasured" && card.dataset.measured === "true");
      if (!card.hidden) count += 1;
    }
    byId("diagnostics-visible").textContent = `Показано ${count} из ${cards.length}`;
    byId("diagnostics-empty").hidden = count > 0 || !configuration;
  }

  function render() {
    if (!configuration) return;
    const opened = new Set([...holder.querySelectorAll("details[open]")].map(details => details.closest("article").dataset.model));
    const items = models();
    holder.replaceChildren(...items.map(modelCard));
    for (const card of holder.children) if (opened.has(card.dataset.model)) card.querySelector("details").open = true;
    const summary = byId("diagnostics-summary");
    summary.replaceChildren(node("span", "", `Настройки: ревизия ${configuration.revision}`),
      node("span", "", `Назначены в маршрутах: ${items.filter(item => item.assignments.length).length}`),
      node("span", "", `Есть записи RPD: ${items.filter(item => item.rows.length).length}`));
    const usage = snapshot?.providers.gemini.rows?.[0];
    if (usage?.usage_date) summary.append(node("span", "", `День учёта RPD: ${usage.usage_date} (Pacific Time)`));
    if (usage?.settings_revision !== undefined && usage.settings_revision !== configuration.revision) {
      summary.append(node("span", "warning", `Пороги RPD: ревизия ${usage.settings_revision}. Ревизии различаются; обновите настройки и диагностику.`));
    }
    const health = snapshot?.providers.key_health;
    if (health?.truncated || health?.summary?.truncated) {
      const total = health.total_key_model_pairs ?? health.summary?.total_key_model_pairs;
      summary.append(node("span", "warning", `Состояния ключей показаны не полностью${Number.isFinite(total) ? `: ${health.rows?.length || 0} из ${total} пар ключ/модель` : ""}. Отсутствующие состояния неизвестны.`));
    }
    filter();
  }

  document.addEventListener("controls:snapshot", event => { configuration = event.detail; if (active) render(); });
  document.addEventListener("controls:sectionchange", event => {
    active = event.detail === "diagnostics";
    if (!active) return;
    render();
    updateStatus();
    if (!snapshot && !pending) refresh();
  });
  byId("diagnostics-refresh").addEventListener("click", refresh);
  byId("diagnostics-search").addEventListener("input", filter);
  byId("diagnostics-filter").addEventListener("change", filter);
  document.dispatchEvent(new CustomEvent("controls:request-snapshot"));
  if (location.hash === "#diagnostics") refresh();
  setInterval(updateStatus, 1000);
})();
