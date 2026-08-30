"use strict";

// The WebSocket only says that something changed. The API is the source of truth,
// so after a reconnect or a "resync" message the page reloads everything.

const PAGE_SIZE = 50;
const EXAMPLES = {
  add: { a: 2, b: 3 },
  sleep: { seconds: 2 },
  cpu: { n: 200000 },
  flaky: { fail_times: 2 },
  fail: { message: "This job always fails." },
};
const STATE_NAMES = {
  scheduled: "Scheduled",
  queued: "Queued",
  running: "Running",
  retry_scheduled: "Retry scheduled",
  succeeded: "Succeeded",
  failed: "Failed",
  dead_letter: "Dead letter",
  cancelled: "Cancelled",
};
const CANCELLABLE = new Set(["scheduled", "queued", "retry_scheduled"]);
const LABELS = {
  add: (p) => `Add ${p.a} + ${p.b}`,
  sleep: (p) => `Wait ${p.seconds} s`,
  cpu: (p) => `Busy loop, ${Number(p.n).toLocaleString("en-US")} steps`,
  flaky: (p) => `Fail ${p.fail_times} ${plural(p.fail_times, "time", "times")}, then succeed`,
  fail: () => "Always fail",
};

const $ = (id) => document.getElementById(id);
const jobs = new Map();
const workerNumbers = new Map();
let pending = [];
let openJobId = null;

// Small helpers

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  for (const child of children) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
}

function badge(state) {
  return el("span", { className: `state s-${state}`, textContent: STATE_NAMES[state] || state });
}

// Jobs and workers are shown by what they are, not by their random IDs.
function describe(job) {
  const label = LABELS[job.type];
  return label && job.payload ? label(job.payload) : job.type;
}

function workerName(id, unknown = "a stopped worker") {
  const n = workerNumbers.get(id);
  return n ? `Worker ${n}` : unknown;
}

function ago(ms) {
  if (!ms) return "–";
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ms).toLocaleDateString();
}

function duration(ms) {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

function timeCell(ms) {
  return el("td", { textContent: ago(ms), title: ms ? new Date(ms).toLocaleString() : "" });
}

function throttle(fn, wait) {
  let timer = null;
  return () => {
    if (timer) return;
    timer = setTimeout(() => {
      timer = null;
      fn();
    }, wait);
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(explain(body) || `Request failed (${response.status})`);
  }
  return body;
}

function explain(body) {
  if (!body || body.detail === undefined) return "";
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) {
    return body.detail.map((e) => `${(e.loc || []).join(".")}: ${e.msg}`).join("; ");
  }
  return "";
}

// Jobs table

function filters() {
  return { state: $("f-state").value, type: $("f-type").value };
}

function matches(job) {
  const f = filters();
  return (!f.state || job.state === f.state) && (!f.type || job.type === f.type);
}

function jobRow(job) {
  const row = el(
    "tr",
    { tabIndex: 0, title: "Show details" },
    el("td", { className: "job-name", textContent: describe(job) }),
    el("td", {}, badge(job.state)),
    el("td", { className: "num", textContent: `${job.attempts} / ${job.max_attempts}` }),
    el("td", { className: "num", textContent: job.priority }),
    timeCell(job.updated_at),
    el("td", { className: "err", textContent: job.error || "", title: job.error || "" }),
  );
  row.dataset.id = job.id;
  return row;
}

function renderJobs() {
  const body = $("jobs");
  const rows = [...jobs.values()].sort((a, b) => b.created_at - a.created_at).map(jobRow);
  body.replaceChildren(...rows);
  $("jobs-empty").hidden = rows.length > 0;
}

function updateRow(job) {
  const old = $("jobs").querySelector(`tr[data-id="${job.id}"]`);
  const row = jobRow(job);
  row.classList.add("flash");
  if (old) {
    old.replaceWith(row);
  } else {
    $("jobs").prepend(row);
    $("jobs-empty").hidden = true;
  }
}

async function loadJobs() {
  const f = filters();
  const query = new URLSearchParams({ limit: PAGE_SIZE });
  if (f.state) query.set("state", f.state);
  if (f.type) query.set("type", f.type);
  const data = await api(`/api/jobs?${query}`);
  jobs.clear();
  for (const job of data.jobs) jobs.set(job.id, job);
  renderJobs();
  heroField?.seed(data.jobs);
}

const reloadJobs = throttle(() => loadJobs().catch(() => {}), 1000);

function trimJobs() {
  const extra = [...jobs.values()].sort((a, b) => a.created_at - b.created_at);
  while (jobs.size > PAGE_SIZE) {
    const oldest = extra.shift();
    jobs.delete(oldest.id);
    $("jobs").querySelector(`tr[data-id="${oldest.id}"]`)?.remove();
  }
}

function applyEvents() {
  const batch = pending;
  pending = [];
  const filtered = Boolean(filters().state || filters().type);
  for (const e of batch) {
    heroField?.event(e);
    if (e.job_id === openJobId) refreshDetail();
    const known = jobs.get(e.job_id);
    if (known) {
      Object.assign(known, {
        state: e.state,
        attempts: e.attempts,
        max_attempts: e.max_attempts,
        updated_at: e.ts,
        error: e.error || null,
        worker_id: e.worker_id || null,
      });
      if (matches(known)) updateRow(known);
      else reloadJobs();
    } else if (filtered) {
      reloadJobs();
    } else if (!e.previous) {
      const job = {
        id: e.job_id,
        type: e.job_type,
        state: e.state,
        attempts: e.attempts,
        max_attempts: e.max_attempts,
        priority: e.priority,
        created_at: e.ts,
        updated_at: e.ts,
        error: e.error || null,
      };
      jobs.set(job.id, job);
      updateRow(job);
      trimJobs();
      // Events carry no payload, so fetch the list to fill in the job's name.
      reloadJobs();
    }
  }
  reloadStats();
}

// Summary and workers

function setText(id, value) {
  $(id).textContent = value === null || value === undefined ? "–" : String(value);
}

function plural(n, one, many) {
  return n === 1 ? one : many;
}

// The big heading is a sentence built from live numbers.
function setHeadline(m) {
  const n = (value) => el("em", { textContent: String(value) });
  const running = m.queue.running;
  const waiting = m.queue.ready;
  const alive = m.workers.alive;
  const done = m.totals.succeeded;
  const jobsWord = (count) => plural(count, "job", "jobs");
  let parts;
  if (running + waiting > 0 && alive === 0) {
    parts = [n(running + waiting), ` ${jobsWord(running + waiting)} waiting, but no worker is on.`];
  } else if (running > 0 && waiting > 0) {
    parts = [n(running), ` ${jobsWord(running)} running, `, n(waiting), " waiting in line."];
  } else if (running > 0) {
    parts = [n(running), ` ${jobsWord(running)} running.`];
  } else if (waiting > 0) {
    parts = [n(waiting), ` ${jobsWord(waiting)} waiting in line.`];
  } else if (done > 0) {
    parts = ["All done. ", n(done), ` ${plural(done, "job has", "jobs have")} succeeded.`];
  } else {
    parts = ["No jobs yet. Submit one to start."];
  }
  $("headline").replaceChildren(...parts);
}

async function loadMetrics() {
  const m = await api("/api/metrics");
  setText("m-ready", m.queue.ready);
  setText("m-scheduled", m.queue.scheduled);
  setText("m-running", m.queue.running);
  setText("m-dead", m.queue.dead_letter);
  setText("m-succeeded", m.totals.succeeded);
  setText("m-failed", m.totals.failed);
  setText("m-retried", m.totals.retried);
  const d = m.job_duration_ms;
  setText("m-duration", d.samples ? `${duration(d.p50)} / ${duration(d.p95)}` : null);
  setHeadline(m);
}

async function loadWorkers() {
  const list = await api("/api/workers");
  list.sort((a, b) => Number(a.started_at) - Number(b.started_at) || a.id.localeCompare(b.id));
  workerNumbers.clear();
  list.forEach((w, i) => workerNumbers.set(w.id, i + 1));
  list.sort((a, b) => Number(b.alive) - Number(a.alive));
  const items = list.map((w) => {
    const status = w.alive ? w.status || "idle" : "not responding";
    const slots = Number(w.concurrency || 1);
    return el(
      "li",
      {},
      el("span", { className: "name", textContent: workerName(w.id), title: w.id }),
      el("span", {
        className: w.alive ? "alive" : "alive stale",
        textContent: w.alive ? "Alive" : "Stale",
      }),
      el("span", {
        className: "meta",
        textContent:
          `${status} · ${slots} ${slots === 1 ? "slot" : "slots"} · ` +
          `${w.processed || 0} run, ${w.failures || 0} failed since start · seen ${ago(w.last_seen)}`,
      }),
    );
  });
  $("workers").replaceChildren(...items);
  heroField?.setWorkers(list.filter((w) => w.alive).map((w) => w.id));
  $("workers-empty").hidden = items.length > 0;
}

const reloadStats = throttle(() => loadMetrics().catch(() => {}), 1000);

async function loadAll() {
  const tasks = [loadJobs(), loadMetrics(), loadWorkers()];
  if (!$("s-type").options.length) tasks.push(loadTypes());
  await Promise.allSettled(tasks);
}

// Job detail

function field(name, value) {
  return [el("dt", { textContent: name }), el("dd", {}, value ?? "–")];
}

function when(ms) {
  return ms ? new Date(ms).toLocaleString() : "–";
}

async function showDetail(id) {
  openJobId = id;
  $("d-status").textContent = "";
  if (!$("detail").open) $("detail").showModal();
  await refreshDetailNow();
}

async function refreshDetailNow() {
  if (!openJobId) return;
  let job;
  try {
    job = await api(`/api/jobs/${openJobId}`);
  } catch (err) {
    $("d-status").textContent = err.message;
    return;
  }
  $("d-type").textContent = `${job.type} job`;
  $("d-title").textContent = describe(job);
  $("d-fields").replaceChildren(
    ...field("ID", el("span", { className: "id", textContent: job.id })),
    ...field("Type", job.type),
    ...field("State", badge(job.state)),
    ...field("Tries", `${job.attempts} of ${job.max_attempts}`),
    ...field("Priority", String(job.priority)),
    ...field("Worker", job.worker_id ? workerName(job.worker_id, "A stopped worker") : null),
    ...field("Created", when(job.created_at)),
    ...field("Runs after", when(job.eligible_at)),
    ...field("Started", when(job.started_at)),
    ...field("Finished", when(job.finished_at)),
    ...field("Last error", job.error ? el("span", { className: "bad-text", textContent: job.error }) : null),
  );
  $("d-payload").textContent = JSON.stringify(job.payload, null, 2);
  $("d-result").textContent = job.result === null ? "–" : JSON.stringify(job.result, null, 2);
  $("d-history").replaceChildren(
    ...job.history.map((h) =>
      el(
        "li",
        {},
        el("span", { className: "when", textContent: new Date(h.ts).toLocaleTimeString() }),
        `${h.event}${h.attempt ? ` (try ${h.attempt})` : ""}${h.worker_id ? ` on ${workerName(h.worker_id)}` : ""}`,
        h.detail ? el("span", { className: "detail", textContent: h.detail }) : null,
      ),
    ),
  );
  $("d-cancel").hidden = !CANCELLABLE.has(job.state);
  $("d-replay").hidden = job.state !== "dead_letter";
}

const refreshDetail = throttle(() => refreshDetailNow(), 300);

async function detailAction(path, done) {
  $("d-status").textContent = "";
  try {
    await api(path, { method: "POST" });
    $("d-status").textContent = done;
    $("d-status").className = "small ok-text";
  } catch (err) {
    $("d-status").textContent = err.message;
    $("d-status").className = "small bad-text";
  }
  await refreshDetailNow();
}

// Submitting jobs

async function loadTypes() {
  const types = await api("/api/job-types");
  const submitOptions = types.map((t) => el("option", { value: t.name, textContent: t.name }));
  const filterOptions = types.map((t) => el("option", { value: t.name, textContent: t.name }));
  $("s-type").replaceChildren(...submitOptions);
  $("f-type").replaceChildren(el("option", { value: "", textContent: "All" }), ...filterOptions);
  $("s-type").dataset.help = JSON.stringify(Object.fromEntries(types.map((t) => [t.name, t.description])));
  pickType();
}

function pickType() {
  const name = $("s-type").value;
  const help = JSON.parse($("s-type").dataset.help || "{}");
  $("s-help").textContent = help[name] || "";
  $("s-payload").value = JSON.stringify(EXAMPLES[name] || {});
}

async function submitJobs(event) {
  event.preventDefault();
  const status = $("s-status");
  status.className = "small";
  let payload;
  try {
    payload = JSON.parse($("s-payload").value);
  } catch {
    status.textContent = "The payload is not valid JSON.";
    status.className = "small bad-text";
    return;
  }
  const body = {
    type: $("s-type").value,
    payload,
    priority: Number($("s-priority").value || 5),
    delay_seconds: Number($("s-delay").value || 0),
  };
  if ($("s-attempts").value) body.max_attempts = Number($("s-attempts").value);
  const count = Math.min(100, Math.max(1, Number($("s-count").value || 1)));

  const button = event.submitter;
  button.disabled = true;
  try {
    for (let i = 0; i < count; i++) {
      await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
    }
    status.textContent = count === 1 ? "Submitted 1 job." : `Submitted ${count} jobs.`;
    status.className = "small ok-text";
  } catch (err) {
    status.textContent = err.message;
    status.className = "small bad-text";
  } finally {
    button.disabled = false;
  }
}

// Live connection

function setLink(kind, text) {
  const link = $("link");
  link.className = `pill pill-${kind}`;
  $("link-text").textContent = text;
}

function connect(delay = 500) {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}/ws/events`);
  ws.onmessage = (message) => {
    const data = JSON.parse(message.data);
    if (data.type === "hello") {
      setLink(data.redis ? "ok" : "bad", data.redis ? "Live" : "Redis is down");
      delay = 500;
      loadAll();
    } else if (data.type === "resync") {
      setLink("ok", "Live");
      loadAll();
    } else if (data.type === "job") {
      if (pending.length === 0) requestAnimationFrame(applyEvents);
      pending.push(data);
    }
  };
  ws.onclose = () => {
    setLink("wait", "Reconnecting");
    setTimeout(() => connect(Math.min(delay * 2, 10000)), delay);
  };
}

// Hero picture: every job is a point of light and every live worker is a ring.
// It is driven by the same events as the table, so it shows the real queue.

const COLORS = {
  wait: [217, 210, 189],
  run: [227, 199, 127],
  ok: [159, 194, 127],
  bad: [217, 103, 94],
};
const MAX_POINTS = 400;

function makeField(canvas) {
  const ctx = canvas.getContext("2d");
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const sprites = {};
  const points = new Map();
  const workers = new Map();
  const dust = [];
  let w = 0;
  let h = 0;
  let visible = true;

  for (const [name, [r, g, b]] of Object.entries(COLORS)) {
    const s = document.createElement("canvas");
    s.width = s.height = 64;
    const c = s.getContext("2d");
    const grad = c.createRadialGradient(32, 32, 0, 32, 32, 32);
    grad.addColorStop(0, `rgba(${r},${g},${b},1)`);
    grad.addColorStop(0.18, `rgba(${r},${g},${b},0.85)`);
    grad.addColorStop(0.45, `rgba(${r},${g},${b},0.18)`);
    grad.addColorStop(1, `rgba(${r},${g},${b},0)`);
    c.fillStyle = grad;
    c.fillRect(0, 0, 64, 64);
    sprites[name] = s;
  }

  function resize() {
    const dpr = Math.min(devicePixelRatio || 1, 2);
    w = canvas.clientWidth;
    h = canvas.clientHeight;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    placeWorkers();
    if (!dust.length) {
      for (let i = 0; i < 90; i++) {
        dust.push({
          x: Math.random(),
          y: Math.random(),
          s: 0.4 + Math.random() * 1.2,
          v: 0.00004 + Math.random() * 0.00012,
        });
      }
    }
  }

  // The waiting cloud sits in the middle; workers stand in an arc on the right.
  function cloudPoint() {
    const a = Math.random() * Math.PI * 2;
    const d = Math.sqrt(Math.random());
    const narrow = w < 700;
    const cy = narrow ? 0.27 : 0.4;
    const ry = narrow ? 0.12 : 0.2;
    const rx = narrow ? 0.2 : 0.12;
    return { x: w * ((narrow ? 0.4 : 0.5) + Math.cos(a) * rx * d), y: h * (cy + Math.sin(a) * ry * d) };
  }

  function placeWorkers() {
    const order = (id) => workerNumbers.get(id) ?? Infinity;
    const ids = [...workers.keys()].sort((a, b) => order(a) - order(b));
    ids.forEach((id, i) => {
      const t = ids.length === 1 ? 0.5 : i / (ids.length - 1);
      const node = workers.get(id);
      const narrow = w < 700;
      node.x = w * ((narrow ? 0.78 : 0.8) + Math.sin(t * Math.PI) * 0.06);
      node.y = h * (narrow ? 0.14 + t * 0.3 : 0.18 + t * 0.5);
    });
  }

  function setWorkers(ids) {
    const keep = new Set(ids);
    for (const id of workers.keys()) if (!keep.has(id)) workers.delete(id);
    for (const id of ids) {
      if (!workers.has(id)) workers.set(id, { x: w * 0.8, y: h * 0.4, pulse: 0 });
    }
    placeWorkers();
  }

  function move(p, state, worker) {
    p.worker = null;
    if (state === "running") {
      if (worker && !workers.has(worker)) setWorkers([...workers.keys(), worker]);
      p.worker = worker || null;
      p.color = "run";
      const node = workers.get(worker);
      if (node) node.pulse = 1;
    } else if (state === "succeeded") {
      p.color = "ok";
      p.fade = 1;
      p.tx = w + 60;
      p.ty = p.y + (Math.random() - 0.5) * h * 0.3;
    } else if (state === "failed" || state === "dead_letter" || state === "cancelled") {
      p.color = "bad";
      p.fade = 1;
      p.tx = p.x + (Math.random() - 0.5) * 80;
      p.ty = h + 40;
    } else {
      p.color = "wait";
      const c = cloudPoint();
      p.tx = c.x;
      p.ty = c.y;
    }
    if (still) {
      p.x = p.tx;
      p.y = p.ty;
    }
  }

  function add(id, state, worker) {
    if (points.has(id)) return points.get(id);
    if (points.size >= MAX_POINTS) points.delete(points.keys().next().value);
    const y = h * (0.15 + Math.random() * 0.6);
    const p = { x: -20, y, tx: -20, ty: y, color: "wait", alpha: 0, fade: 0, worker: null };
    p.angle = Math.random() * Math.PI * 2;
    p.seed = Math.random();
    points.set(id, p);
    move(p, state, worker);
    return p;
  }

  function event(e) {
    const p = points.get(e.job_id);
    if (p) move(p, e.state, e.worker_id);
    else if (e.state !== "succeeded" && e.state !== "failed") add(e.job_id, e.state, e.worker_id);
  }

  function seed(list) {
    for (const job of list) {
      if (["queued", "scheduled", "retry_scheduled", "running"].includes(job.state)) {
        const fresh = !points.has(job.id);
        const p = add(job.id, job.state, job.worker_id);
        if (fresh) {
          const c = cloudPoint();
          p.x = c.x;
          p.y = c.y;
        }
      }
    }
  }

  function draw(time) {
    ctx.clearRect(0, 0, w, h);
    ctx.globalCompositeOperation = "lighter";

    ctx.fillStyle = "rgba(253,250,241,0.22)";
    for (const d of dust) {
      if (!still) d.x = (d.x + d.v) % 1;
      ctx.fillRect(d.x * w, d.y * h, d.s, d.s);
    }

    const busy = new Set([...points.values()].map((p) => p.worker));
    ctx.font = "14px \"Times New Roman\", Times, serif";
    ctx.textAlign = "center";
    for (const [id, node] of workers) {
      node.pulse *= 0.96;
      ctx.strokeStyle = `rgba(227,199,127,${busy.has(id) ? 0.75 : 0.3})`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(node.x, node.y, 22 + node.pulse * 10, 0, Math.PI * 2);
      ctx.stroke();
      ctx.drawImage(sprites.run, node.x - 16, node.y - 16, 32, 32);
      ctx.fillStyle = "rgba(253,250,241,0.55)";
      ctx.fillText(workerName(id, "Worker"), node.x, node.y + 46);
    }

    for (const [id, p] of points) {
      const node = p.worker ? workers.get(p.worker) : null;
      if (node) {
        if (!still) p.angle += 0.035;
        p.tx = node.x + Math.cos(p.angle) * 34;
        p.ty = node.y + Math.sin(p.angle) * 34;
        ctx.strokeStyle = "rgba(227,199,127,0.18)";
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        ctx.lineTo(node.x, node.y);
        ctx.stroke();
      } else if (p.color === "wait" && !still) {
        p.tx += Math.sin(time / 1400 + p.seed * 10) * 0.25;
        p.ty += Math.cos(time / 1700 + p.seed * 10) * 0.25;
      }
      const ease = still ? 1 : p.fade ? 0.025 : 0.05;
      p.x += (p.tx - p.x) * ease;
      p.y += (p.ty - p.y) * ease;
      p.alpha = p.fade ? p.alpha - 0.006 : Math.min(1, p.alpha + 0.04);
      if (p.alpha <= 0 || p.x > w + 40 || p.y > h + 30) {
        points.delete(id);
        continue;
      }
      const size = p.color === "run" ? 26 : 20;
      ctx.globalAlpha = Math.min(1, p.alpha);
      ctx.drawImage(sprites[p.color], p.x - size / 2, p.y - size / 2, size, size);
      ctx.globalAlpha = 1;
    }
    ctx.globalCompositeOperation = "source-over";
  }

  function loop(time) {
    if (visible) draw(time);
    requestAnimationFrame(loop);
  }

  new ResizeObserver(resize).observe(canvas);
  new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
  }).observe(canvas);
  resize();
  requestAnimationFrame(loop);
  return { event, seed, setWorkers };
}

let heroField = null;

// Start

document.addEventListener("DOMContentLoaded", () => {
  heroField = makeField($("field"));
  // The top bar is clear over the dark hero and solid below it.
  new IntersectionObserver(
    ([entry]) => {
      document.querySelector(".bar").classList.toggle("solid", entry.intersectionRatio < 0.12);
    },
    { threshold: [0, 0.12, 0.5] },
  ).observe($("hero"));
  $("f-state").addEventListener("change", () => loadJobs().catch(() => {}));
  $("f-type").addEventListener("change", () => loadJobs().catch(() => {}));
  $("s-type").addEventListener("change", pickType);
  $("submit").addEventListener("submit", submitJobs);

  $("jobs").addEventListener("click", (e) => {
    const row = e.target.closest("tr");
    if (row) showDetail(row.dataset.id);
  });
  $("jobs").addEventListener("keydown", (e) => {
    const row = e.target.closest("tr");
    if (row && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      showDetail(row.dataset.id);
    }
  });
  $("d-close").addEventListener("click", () => $("detail").close());
  $("detail").addEventListener("close", () => {
    openJobId = null;
  });
  $("d-cancel").addEventListener("click", () =>
    detailAction(`/api/jobs/${openJobId}/cancel`, "Cancelled."),
  );
  $("d-replay").addEventListener("click", () =>
    detailAction(`/api/dead-letters/${openJobId}/replay`, "Queued again."),
  );

  loadTypes().catch(() => {});
  connect();
  // Heartbeats do not send events, so workers and totals are polled.
  setInterval(() => {
    loadWorkers().catch(() => {});
    loadMetrics().catch(() => {});
  }, 3000);
  setInterval(renderJobs, 10000);
});
