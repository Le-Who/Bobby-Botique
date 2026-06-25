# Дизайн: Unified Admin Daily & Broadcast Center

**Дата:** 2026-06-25  
**Статус:** Draft — ждёт одобрения

---

## 1. Контекст и проблема

### Текущее состояние

| URL | Содержимое | Проблема |
|-----|-----------|---------|
| `/admin_dailycroc` | Настройки игры (слово, арт, промпт) + переключатель рассылки | Смешивает два разных домена |
| `/admin_daily2048` | Только настройки пазла | Нет рассылки — путаница |
| `dashboard.html` | Ссылки в topbar | Нет системного порядка |

### Типы рассылок (текущее состояние данных)

| Канал | Таблица | Поля |
|-------|---------|------|
| 🐊 Daily Croc | `crocodile_daily_preferences` | `is_subscribed`, `last_sent_puzzle_date`, `last_sent_local_date`, `discovery_snoozed_until`, `discovery_last_sent_at` |
| 🎲 Daily 2048 | реюзает croc-callback `dailycroc:subscribe` | нет своих подписок |
| ⭐ Гороскоп | `horoscope_subscriptions` | `time_today`, `time_tomorrow`, `sign`, `utc_offset`, `last_today_sent`, `last_tomorrow_sent` |
| 🔮 Дейли Таро | нет | нет таблицы |
| 📰 Discovery | `crocodile_daily_preferences` | `discovery_last_sent_at`, `discovery_snoozed_until` |
| 📢 Global Settings | `global_settings` | `daily_crocodile_delivery_enabled`, `daily_game_mode` |

### Боль пользователя (=автора проекта)

1. Переключатель "Глобальная рассылка" есть только в croc, не в 2048
2. Нет единого места посмотреть "кому когда что отправлялось"
3. Нет ни одного места, где видна общая картина всех рассылок
4. Управление игрой и управление рассылкой перемешаны

---

## 2. Цель

Создать **единый** `/admin_daily` с вкладками:
- **📡 Рассылки** — управление broadcast-системой (kill-switch, статистика, таблица подписчиков, тайм-лайн, ошибки, аналитика)
- **🐊 Croc** — настройки самой игры (без рассылки)
- **🎲 2048** — настройки пазла (без рассылки)
- **⭐ Horoscope** — настройки астро-продукта
- **🔮 Tarot** — настройки таро-продукта

Старые URL `/admin_dailycroc` и `/admin_daily2048` должны делать **redirect 301** на `/admin_daily#croc` и `/admin_daily#2048`.

---

## 3. Анализ вариантов

### 🅐 Вариант 1: Монолит (single HTML + inline JS)

**Плюсы:** Нет зависимостей. Прост. Быстрый старт. Совместим с CSP nonce.  
**Минусы:** Вырастет в непроходимую кашу (croc уже 872 строки). Невозможно тестировать изолированно. При добавлении 5-го продукта — 3000+ строк.

---

### 🅑 Вариант 2: Jinja2-includes (base + partials)

**Плюсы:** Шаблонизация на уровне сервера. CSS общий.  
**Минусы:** Quart рендерит всё в один HTTP-ответ — JS всё равно инлайн. Переключение вкладок требует page-reload (UX хуже) или того же JS-router.

---

### 🅒 Вариант 3: Единый HTML + внешние JS-модули

**Плюсы:** Каждый модуль — одна ответственность. Лёгкое добавление продуктов. Кэш браузера.  
**Минусы:** Нарушает существующий CSP-паттерн (все admin-страницы — inline `<script nonce>`, внешние файлы требуют иного подхода).

---

### 🅓 Вариант 4: SPA с JS-роутером (ванильный)

**Плюсы:** Максимально "живой" UI, нет page-reload.  
**Минусы:** Оверинжиниринг. Хрупко без фреймворка. Надо писать state management самому.

---

### 🅔 Вариант 5: Отдельный мини-сервис (FastAPI + React)

**Плюсы:** Максимальный потенциал, TypeScript, тесты.  
**Минусы:** Избыточная сложность. Отдельный деплой. Нарушает принцип единого процесса.

---

## 4. Синтез: Финальное решение

**Берём лучшее из 🅐 + 🅓:**

> **Один HTML-файл `admin_daily.html`** (разметка + CSS) +  
> **Inlined JS**, организованный в именованные IIFE-модули внутри одного `<script nonce>` — совместимо с текущим CSP, читаемо, расширяемо.

**Паттерн JS-организации:**
```javascript
const BroadcastModule = (() => {
  // всё про рассылки
  return { init, loadStats, toggleDelivery };
})();

const CrocModule = (() => {
  // всё про крокодила (перенесено из admin_dailycroc.html)
  return { init, loadPuzzles };
})();

// Tab router
const TabRouter = (() => {
  const tabs = { broadcast: BroadcastModule, croc: CrocModule, ... };
  function activate(tabId) { ... }
  window.addEventListener('hashchange', () => activate(location.hash.slice(1)));
  return { activate };
})();

TabRouter.activate(location.hash.slice(1) || 'broadcast');
```

**Почему не 🅒 (внешние файлы):** В текущей кодовой базе все admin-страницы — самодостаточные HTML с inline `<script nonce="{{ g.csp_nonce }}">`. Внешние файлы требуют иного подхода к CSP или изменения CSP-policy сервера. Сохраняем **однородность**.

---

## 5. Архитектура

### 5.1. URL-структура

```
/admin_daily           → admin_daily.html (вкладка по умолчанию: Рассылки)
/admin_daily#broadcast → вкладка Рассылки
/admin_daily#croc      → вкладка Croc
/admin_daily#2048      → вкладка 2048
/admin_daily#horoscope → вкладка Horoscope
/admin_daily#tarot     → вкладка Tarot

/admin_dailycroc       → redirect 301 → /admin_daily#croc
/admin_daily2048       → redirect 301 → /admin_daily#2048
```

### 5.2. Структура файлов

```
app/
  templates/
    admin_daily.html        ← НОВЫЙ (заменяет оба старых)
    admin_dailycroc.html    ← УДАЛИТЬ (фаза 4)
    admin_daily2048.html    ← УДАЛИТЬ (фаза 4)
  web.py                    ← новые routes + redirects + новые API
```

### 5.3. HTML-структура страницы

```html
<!-- Sticky header с title + действиями -->
<div class="header">
  <h1>📅 Daily Admin</h1>
  <div class="header-actions">
    <button>🔄 Обновить</button>
  </div>
</div>

<!-- Tab navigation -->
<nav class="tab-nav">
  <button data-tab="broadcast" class="tab active">📡 Рассылки</button>
  <button data-tab="croc" class="tab">🐊 Croc</button>
  <button data-tab="2048" class="tab">🎲 2048</button>
  <button data-tab="horoscope" class="tab">⭐ Horoscope</button>
  <button data-tab="tarot" class="tab">🔮 Tarot</button>
</nav>

<!-- Tab panels -->
<div id="tab-broadcast" class="tab-panel active"> ... </div>
<div id="tab-croc" class="tab-panel"> ... </div>
<div id="tab-2048" class="tab-panel"> ... </div>
<div id="tab-horoscope" class="tab-panel"> ... </div>
<div id="tab-tarot" class="tab-panel"> ... </div>
```

---

## 6. Вкладка "📡 Рассылки" — детальный дизайн

### 6.1. Channel Cards (верхняя строка)

Каждая карточка — один канал рассылки:

```
┌──────────────────────────────┐
│ 🐊 Daily Croc                │
│                              │
│ Подписчики:    42            │
│ Ожидают:       12   ←pending│
│ Отправлено:    30   (сегодня)│
│ Последняя:     09:05         │
│ Следующая:     09:00+1d      │
│                    [■ ON]    │
└──────────────────────────────┘
```

Каналы: 🐊 Croc, 🎲 2048 (shared-sub), ⭐ Гороскоп (×2 слота), 🔮 Таро (N/A), 📰 Discovery

Kill-switch — toggle с confirm-modal. Недоступен для каналов без рассылки.

### 6.2. Unified Subscribers Table

Единая таблица с фильтрами:

| User ID | Каналы | Timezone | Последняя | Следующая | Статус |
|---------|--------|----------|-----------|-----------|--------|
| 123456 | 🐊 ⭐ | Europe/Kyiv | 25.06 09:05 | 26.06 09:00 | ✅ active |
| 789012 | 🐊 | UTC | 24.06 10:00 | 26.06 07:00 | ❌ error |

Фильтры: `[Все каналы ▼]` `[Статус ▼]` `[Timezone ▼]` `[🔍 User ID]`  
Пагинация: 50 строк, lazy-load

### 6.3. Delivery Timeline

Хронологический лог последних 50 событий:

```
13:25:04  🐊  user 123456  ✅ delivered
13:25:03  🐊  user 789012  ❌ Forbidden (bot blocked)
09:00:11  ⭐  user 111222  ✅ delivered (♊ Близнецы, утро)
08:59:58  ⭐  user 333444  ✅ delivered (♈ Овен, утро)
```

> Примечание: потребует новой таблицы `broadcast_events` в БД (открытый вопрос ниже).

### 6.4. Errors Section

Таблица пользователей с ошибками:

| User ID | Канал | Ошибка | Последняя попытка |
|---------|-------|--------|-----------------|
| 789012 | 🐊 | Forbidden: bot was blocked | 25.06 13:25 |

### 6.5. Unsubscribe Analytics

Простая таблица (где доступны данные):
- Кто отписался (`is_subscribed = false`)
- Когда (через `updated_at`)
- От какого канала

---

## 7. Вкладки продуктов

### 🐊 Croc

Контент = текущий `admin_dailycroc.html` **минус** карточка "Глобальная рассылка".  
Статистика Croc-вкладки: Подписчики (кол-во), Игры сегодня (won/active), Параметры (banner, модель).

### 🎲 2048

Контент = текущий `admin_daily2048.html` полностью (mode selector + список пазлов + редактор).

### ⭐ Horoscope

Новая вкладка (минимальная v1):
- Статистика подписок (сколько на утро / вечер / оба)
- Разбивка по знакам зодиака
- Кнопка принудительной генерации

### 🔮 Tarot

Новая вкладка (минимальная v1):
- Статус подготовки readings: сегодня N/22, завтра N/22
- Кнопка принудительной регенерации на дату
- Список готовых карт

---

## 8. API Endpoints

### Новые (Broadcast)

```
GET  /api/admin/broadcast/overview
     Response: {
       channels: [{
         id: "croc",
         name: "Daily Croc",
         emoji: "🐊",
         subscribers: 42,
         pending_today: 12,
         sent_today: 30,
         delivery_enabled: true,
         last_sent_at: "2026-06-25T09:05:00Z",
         next_scheduled_at: "2026-06-26T06:00:00Z"
       }, ...]
     }

GET  /api/admin/broadcast/subscribers
     Query: ?channel=croc&status=active&timezone=Europe/Kyiv&limit=50&offset=0
     Response: {
       total: 42,
       rows: [{user_id, channels, timezone, last_sent, next_scheduled, status}]
     }

GET  /api/admin/broadcast/timeline
     Query: ?limit=50
     Response: {events: [{ts, channel, user_id, status, error?}]}

GET  /api/admin/broadcast/errors
     Response: {errors: [{user_id, channel, error_type, last_error_at, count}]}

POST /api/admin/broadcast/toggle
     Body: {channel: "croc", enabled: true}
     Response: {success, channel, enabled}
```

### Новые (Horoscope)

```
GET  /api/admin/horoscope/stats
     Response: {
       total: 15,
       breakdown: {today_only: 5, tomorrow_only: 3, both: 7},
       by_sign: {aries: 2, taurus: 1, ...}
     }
```

### Новые (Tarot)

```
GET  /api/admin/tarot/status
     Response: {
       today: {date, ready_count, total: 22, cards: [{label, ready}]},
       tomorrow: {...}
     }

POST /api/admin/tarot/regenerate
     Body: {date: "2026-06-25"}
     Response: {success, generated, skipped, failed}
```

### Существующие — без изменений

```
GET  /api/admin/dailycroc          (список пазлов)
POST /api/admin/dailycroc/*        (операции с пазлами)
GET  /api/admin/dailycroc/stats    (убрать delivery_enabled → уходит в broadcast/overview)
GET  /api/admin/daily2048
POST /api/admin/daily2048/*
POST /api/admin/daily-mode
POST /api/admin/dailycroc/toggle-delivery  (оставить для совместимости, вызывать из broadcast/toggle)
```

---

## 9. Изменения в `web.py`

```python
# Заменить page routes:
@quart_app.route("/admin_daily")
@require_auth
async def admin_daily_page():
    return await render_template("admin_daily.html")

@quart_app.route("/admin_dailycroc")
@require_auth
async def admin_dailycroc_redirect():
    return redirect("/admin_daily#croc", code=301)

@quart_app.route("/admin_daily2048")
@require_auth
async def admin_daily2048_redirect():
    return redirect("/admin_daily#2048", code=301)
```

---

## 10. Дизайн-система

Следуем стилю `admin_dailycroc.html` (единая база для всего проекта):

```css
:root {
  --bg: #0a0e1a;
  --bg-surface: #111827;
  --surface: rgba(255, 255, 255, 0.035);
  --accent: #06b6d4;        /* cyan — основной акцент */
  --violet: #8b5cf6;        /* broadcast toggle */
  --green: #10b981;         /* success, active */
  --amber: #f59e0b;         /* warning */
  --rose: #f43f5e;          /* error, disabled */
  --card-radius: 14px;
}
```

**Ключевые UI-компоненты:**
- **Tab Nav** — sticky, горизонтальная, с подсветкой активной вкладки и accent underline
- **Channel Card** — glassmorphism, kill-switch toggle (нижний правый угол)
- **Subscribers Table** — sticky header, zebra rows, inline channel badges, hover row highlight
- **Timeline** — монофонт, цветные иконки статуса, временная метка dim
- **Toast** — переиспользовать из croc (success/error/info)
- **Confirm Modal** — переиспользовать из croc

---

## 11. Открытые вопросы

> [!IMPORTANT]
> **Таблица ошибок доставки**: В коде нет таблицы событий рассылок (`broadcast_events`). Для Timeline и Errors нужно либо добавить таблицу, либо fallback через `last_sent_*` поля. Timeline в v1 может показывать только `last_sent_*` без детального лога — это честный минимум.

> [!IMPORTANT]
> **Daily 2048 подписки**: 2048 реюзает `dailycroc:subscribe`. В Broadcast UI подписчики 2048 отображаются как подписчики Croc — это технически верно, но UX-дезориентирует. Вариант: показывать единую карточку "🐊+🎲 Daily Challenge" вместо двух.

> [!NOTE]
> **Horoscope kill-switch**: Сейчас нет `global_settings` key для включения/выключения рассылки гороскопов. Нужно добавить `horoscope_delivery_enabled` или использовать индивидуальные `is_active` флаги.

> [!NOTE]
> **Tarot подписки**: Таро-рассылки в коде нет совсем. Вкладка Tarot в v1 — только статус подготовки карт (БЕЗ функционала рассылки). Рассылку добавить в отдельном MR.

---

## 12. Фазы реализации

### Фаза 1 — Фундамент (1 сессия)
- Создать `admin_daily.html` с tab-router (hash-based) + CSS
- Перенести Croc-вкладку (убрать секцию рассылки)
- Перенести 2048-вкладку
- Redirect 301 старых URL
- Обновить dashboard.html

### Фаза 2 — Вкладка Рассылки (1–2 сессии)
- API `GET /api/admin/broadcast/overview` (агрегирует из croc_stats + horoscope_stats)
- API `GET /api/admin/broadcast/subscribers` (JOIN croc_prefs + horoscope_subs)
- API `POST /api/admin/broadcast/toggle` (wrapper над существующим toggle)
- API `GET /api/admin/broadcast/errors` (stub на v1)
- UI: Channel Cards + kill-switch
- UI: Subscribers Table с фильтрами
- UI: упрощённый Timeline (без broadcast_events)

### Фаза 3 — Horoscope + Tarot (1 сессия)
- Horoscope stats API + UI
- Tarot status API + UI + кнопка регенерации

### Фаза 4 — Cleanup
- Удалить `admin_dailycroc.html` и `admin_daily2048.html`
- Убрать страничные роуты (оставить только API)

---

## 13. Критерии завершения (Definition of Done)

- [ ] `/admin_daily` открывается, все 5 вкладок переключаются через hash
- [ ] `/admin_dailycroc` → 301 → `/admin_daily#croc`
- [ ] `/admin_daily2048` → 301 → `/admin_daily#2048`
- [ ] Kill-switch работает для Croc и Horoscope
- [ ] Таблица подписчиков отображает данные из обеих таблиц
- [ ] Dashboard.html — ссылки обновлены
- [ ] Encoding check `python scripts/check_encoding.py` — OK
- [ ] `ruff check .` — clean
- [ ] Старые HTML-файлы удалены
