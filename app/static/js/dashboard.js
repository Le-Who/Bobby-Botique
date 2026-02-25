// =====================================================================
// Dashboard data fetcher — polls all API endpoints every 15 seconds
// =====================================================================

const API = {
  overview: "/api/overview",
  keys: "/api/keys",
  errors: "/api/errors",
  cache: "/api/cache",
  queue: "/api/queue",
  database: "/api/database",
  circuitBreakers: "/api/circuit-breakers",
};

const $ = (id) => document.getElementById(id);

// Clock
function updateClock() {
  const now = new Date();
  $("clock").textContent = now.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
setInterval(updateClock, 1000);
updateClock();

// Blink the live dot
setInterval(() => {
  const dot = $("live-dot");
  dot.style.opacity = dot.style.opacity === "0.3" ? "1" : "0.3";
}, 1000);

// Tab navigation
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('panel-' + btn.dataset.tab).classList.add('active');
  });
});

// Sparkline history buffers (last 20 readings)
const cpuHistory = [];
const memHistory = [];
const MAX_SPARK = 20;

function renderSparkline(container, data, color) {
  if (!container || data.length < 2) return;
  const w = 80, h = 22;
  const max = Math.max(...data, 1);
  const pts = data.map((v, i) => {
    const x = (i / (MAX_SPARK - 1)) * w;
    const y = h - (v / max) * h;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  container.innerHTML = `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="display:block">
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round"/>
  </svg>`;
}

// Helper: set bar color based on value
function setBarLevel(barEl, cardEl, pct) {
  barEl.style.width = pct + "%";
  barEl.className = "card-bar-fill";
  if (pct > 85) {
    barEl.classList.add("bar-critical");
    cardEl.classList.add("card-critical");
  } else if (pct > 70) {
    barEl.classList.add("bar-warning");
    cardEl.classList.add("card-warning");
  } else {
    cardEl.classList.remove("card-critical", "card-warning");
  }
}

// Helper: set service dot
function setDot(id, status) {
  const el = $(id);
  el.className = "dot";
  if (
    status === "connected" ||
    status === "running" ||
    status === "healthy"
  )
    el.classList.add("dot-ok");
  else if (
    status === "disconnected" ||
    status === "unhealthy" ||
    status === "error"
  )
    el.classList.add("dot-err");
  else el.classList.add("dot-warn");
}

// Fetch with session — redirect to login on 401
async function apiFetch(url) {
  const res = await fetch(url);
  if (res.status === 401) {
    window.location.href = "/login";
    return null;
  }
  if (!res.ok) return null;
  return await res.json();
}

// =====================================================================
// Data loaders
// =====================================================================

async function loadOverview() {
  const d = await apiFetch(API.overview);
  if (!d) return;

  const s = d.system;
  $("cpu-val").textContent = s.cpu_percent + "%";
  setBarLevel($("cpu-bar"), $("card-cpu"), s.cpu_percent);

  $("mem-val").textContent = s.memory_percent + "%";
  $("mem-detail").textContent =
    s.memory_used_mb + " / " + s.memory_total_mb + " MB";
  setBarLevel($("mem-bar"), $("card-mem"), s.memory_percent);

  $("disk-val").textContent = s.disk_percent + "%";
  setBarLevel($("disk-bar"), $("card-disk"), s.disk_percent);

  // Services
  const svc = d.services;
  setDot("svc-db", svc.database);
  setDot("svc-redis", svc.redis);
  setDot("svc-bot", svc.bot);

  // Metrics
  const m = d.metrics;
  if (m) {
    $("req-count").textContent = (m.request_count || 0).toLocaleString();
    const avgMs =
      m.average_response_time_ms ||
      (m.average_response_time ? m.average_response_time * 1000 : 0);
    $("avg-resp").textContent = avgMs ? avgMs.toFixed(0) + "ms" : "—";
    const errRate = m.error_rate_percent || m.error_rate || 0;
    $("error-rate").textContent =
      (typeof errRate === "number" ? errRate.toFixed(1) : errRate) + "%";
    $("error-count-sub").textContent =
      (m.error_count || 0) + " errors total";
    const cacheRate = m.cache_hit_rate_percent || m.cache_hit_rate || 0;
    $("cache-hit").textContent =
      (typeof cacheRate === "number" ? cacheRate.toFixed(1) : cacheRate) +
      "%";
  }

  $("last-update").textContent =
    "Updated: " + new Date().toLocaleTimeString();

  // Sparklines
  cpuHistory.push(s.cpu_percent);
  memHistory.push(s.memory_percent);
  if (cpuHistory.length > MAX_SPARK) cpuHistory.shift();
  if (memHistory.length > MAX_SPARK) memHistory.shift();
  renderSparkline($("spark-cpu"), cpuHistory, "#4ade80");
  renderSparkline($("spark-mem"), memHistory, "#60a5fa");
}

async function loadKeys() {
  const d = await apiFetch(API.keys);
  if (!d) return;

  const tbody = $("key-tbody");
  const empty = $("keys-empty");
  const rows = d.key_usage || [];

  if (rows.length === 0) {
    tbody.innerHTML = "";
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";

  tbody.innerHTML = rows
    .map((r) => {
      const pct =
        r.usage_percent != null
          ? Number(r.usage_percent).toFixed(1)
          : "—";
      const statusCls = r.is_available ? "tag-ok" : "tag-err";
      const statusTxt = r.is_available ? "OK" : "LIMIT";
      return `<tr>
          <td class="mono">${r.model_name || "—"}</td>
          <td class="mono dim">${r.api_key_preview || r.key_hash?.substring(0, 10) || "—"}</td>
          <td class="mono">${r.request_count ?? 0}</td>
          <td class="mono">${r.daily_limit ?? "∞"}</td>
          <td class="mono">${pct}%</td>
          <td><span class="tag ${statusCls}">${statusTxt}</span></td>
      </tr>`;
    })
    .join("");
}

async function loadCircuitBreakers() {
  const d = await apiFetch(API.circuitBreakers);
  if (!d) return;

  const list = $("cb-list");
  const cbs = d.circuit_breakers || {};
  const names = Object.keys(cbs);

  if (names.length === 0) {
    list.innerHTML =
      '<div class="empty-state">No circuit breakers active</div>';
    return;
  }

  list.innerHTML = names
    .map((name) => {
      const cb = cbs[name];
      const state = cb.state || "unknown";
      const dotCls =
        state === "closed"
          ? "dot-ok"
          : state === "open"
            ? "dot-err"
            : "dot-warn";
      return `<div class="cb-item">
          <span class="dot ${dotCls}"></span>
          <span class="cb-name">${name}</span>
          <span class="cb-state mono">${state.toUpperCase()}</span>
          <span class="cb-detail mono dim">${cb.failure_count || 0} fails</span>
      </div>`;
    })
    .join("");
}

async function loadDatabase() {
  const d = await apiFetch(API.database);
  if (!d || !d.database) return;

  const db = d.database;
  $("db-status").textContent = db.status || "—";
  $("db-status").className =
    "sl-val " + (db.status === "connected" ? "val-ok" : "val-err");
  $("db-pool").textContent = db.pool_size ?? "—";
  $("db-active").textContent = db.active_connections ?? "—";
  $("db-free").textContent = db.free_size ?? "—";
  $("db-latency").textContent =
    db.response_time_ms != null ? db.response_time_ms + "ms" : "—";
}

async function loadQueue() {
  const d = await apiFetch(API.queue);
  if (!d || !d.queue) return;

  const q = d.queue;
  $("q-pending").textContent = q.pending ?? q.pending_tasks ?? "—";
  $("q-running").textContent = q.running ?? q.running_tasks ?? "—";
  $("q-completed").textContent = q.completed ?? q.completed_tasks ?? "—";
  $("q-failed").textContent = q.failed ?? q.failed_tasks ?? "—";
  $("q-workers").textContent = q.workers ?? q.active_workers ?? "—";
}

async function loadCache() {
  const d = await apiFetch(API.cache);
  if (!d) return;

  const ml = d.multi_layer || {};
  const mem = ml.memory || {};
  $("c-mem-hits").textContent = mem.hits ?? ml.memory_hits ?? "—";
  $("c-redis-hits").textContent = ml.redis_hits ?? "—";
  $("c-misses").textContent = mem.misses ?? ml.misses ?? "—";
  $("c-mem-items").textContent =
    mem.total_items ?? ml.memory_items ?? "—";
}

// =====================================================================
// Poll loop
// =====================================================================

async function refreshAll() {
  try {
    await Promise.all([
      loadOverview(),
      loadKeys(),
      loadCircuitBreakers(),
      loadDatabase(),
      loadQueue(),
      loadCache(),
    ]);
  } catch (e) {
    console.error("Dashboard refresh error:", e);
  }
}

async function loadErrors() {
  const d = await apiFetch(API.errors);
  if (!d) return;

  const badge = $("error-badge");
  const count = d.error_count || 0;
  if (count > 0) {
    badge.textContent = count;
    badge.style.display = "inline-block";
  } else {
    badge.style.display = "none";
  }

  const logEl = $("error-log");
  const recent = d.recent_errors || [];
  if (recent.length === 0) {
    logEl.innerHTML = '<div class="empty-state">No recent errors \u2014 all systems normal</div>';
    return;
  }
  logEl.innerHTML = recent.slice(0, 30).map(err => {
    const ts = err.timestamp ? new Date(err.timestamp).toLocaleTimeString() : '';
    const errType = err.type || err.error_type || 'unknown';
    const msg = err.message || err.error || String(err);
    return `<div class="error-row">
      <span class="error-ts mono dim">${ts}</span>
      <span class="error-type mono tag tag-err">${errType}</span>
      <span class="error-msg">${msg}</span>
    </div>`;
  }).join('');
}

// Initial load
refreshAll();
loadErrors();
setInterval(refreshAll, 15000);
setInterval(loadErrors, 30000);
