"use strict";
const tg = window.Telegram?.WebApp;
try {
  tg?.ready();
  tg?.expand();
} catch (_) {}
const icons = {
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
  moon: '<path d="M20.5 14A9 9 0 0 1 10 3.5 9 9 0 1 0 20.5 14Z"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  layers: '<path d="m12 3 10 5-10 5L2 8l10-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  settings:
    '<path d="M4 7h16M4 17h16"/><circle cx="8" cy="7" r="3"/><circle cx="16" cy="17" r="3"/>',
  terminal:
    '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m7 9 3 3-3 3m6 0h4"/>',
  refresh:
    '<path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 2M4 16l2 2a7 7 0 0 0 12-1"/>',
  arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  clone:
    '<rect x="8" y="8" width="13" height="13" rx="3"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
  transfer: '<path d="M4 7h15l-4-4m4 4-4 4M20 17H5l4-4m-4 4 4 4"/>',
  export: '<path d="M12 3v12m-4-4 4 4 4-4M4 15v5h16v-5"/>',
  index:
    '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h4"/>',
  file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6Z"/><path d="M14 3v6h6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 1v5m6-5v5M9 18v5m6-5v5M1 9h5m-5 6h5m12-6h5m-5 6h5"/>',
  stop: '<rect x="5" y="5" width="14" height="14" rx="3"/>',
  up: '<path d="M12 20V4m-6 6 6-6 6 6"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  alert: '<path d="m12 3 10 17H2L12 3Z"/><path d="M12 9v5m0 3h.01"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="3"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 4v3"/>',
  play: '<path d="m8 4 12 8-12 8V4Z"/>',
  search: '<circle cx="10" cy="10" r="7"/><path d="m16 16 5 5"/>',
};
const icon = (name) =>
  `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.file}</svg>`;
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const $ = (q) => document.querySelector(q);
const $$ = (q) => Array.from(document.querySelectorAll(q));
const names = {
  dashboard: "Overview",
  create: "New task",
  queue: "Queue",
  history: "History",
  settings: "Settings",
  tools: "Tools & logs",
};
const types = {
  clone: { label: "Clone", description: "Telegram to Telegram", icon: "clone" },
  transfer: {
    label: "Transfer",
    description: "Drive, MSZ & Telegram",
    icon: "transfer",
  },
  export: {
    label: "Export",
    description: "Export topic messages",
    icon: "export",
  },
  index: {
    label: "Index",
    description: "Build a topic link index",
    icon: "index",
  },
};
const app = {
  view: "dashboard",
  kind: "clone",
  data: null,
  settings: null,
  settingsTab: "clone",
  filter: "all",
  connected: false,
  locked: false,
  detail: null,
  operation: null,
  polling: false,
  seen: new Set(),
  drafts: {},
  browserAuth: null,
  settingsLoaded: 0,
};
function fillIcons() {
  $$("[data-icon]").forEach((el) => (el.innerHTML = icon(el.dataset.icon)));
  $$("[data-width]").forEach(
    (el) =>
      (el.style.width =
        Math.max(0, Math.min(100, Number(el.dataset.width) || 0)) + "%"),
  );
}
fillIcons();
function bytes(n) {
  if (n == null) return "—";
  let v = Math.max(0, Number(n) || 0),
    i = 0;
  const u = ["B", "KB", "MB", "GB", "TB"];
  while (v >= 1024 && i < 4) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i ? 2 : 0)} ${u[i]}`;
}
function duration(n) {
  if (n == null) return "Estimating";
  n = Math.max(0, Math.floor(n));
  if (n >= 86400)
    return `${Math.floor(n / 86400)}d ${Math.floor((n % 86400) / 3600)}h`;
  if (n >= 3600)
    return `${Math.floor(n / 3600)}h ${Math.floor((n % 3600) / 60)}m`;
  if (n >= 60) return `${Math.floor(n / 60)}m ${n % 60}s`;
  return `${n}s`;
}
function progress(n, extra = "") {
  return `<div class="progress-track ${extra}"><span data-width="${Number(n) || 0}"></span></div>`;
}
function toast(text, error = false) {
  const el = document.createElement("div");
  el.className = "toast" + (error ? " error" : "");
  el.textContent = text;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 6500);
  try {
    tg?.HapticFeedback?.notificationOccurred(error ? "error" : "success");
  } catch (_) {}
}
async function api(path, options = {}) {
  const headers = {
    ...(tg?.initData ? { Authorization: "tma " + tg.initData } : {}),
    ...(app.browserAuth?.csrf ? { "X-CSRF-Token": app.browserAuth.csrf } : {}),
    ...(options.body instanceof FormData
      ? {}
      : { "Content-Type": "application/json" }),
    ...options.headers,
  };
  const response = await fetch("/api/" + path, { ...options, headers });
  let data;
  try {
    data = await response.json();
  } catch (_) {
    throw new Error("The bot is restarting. Please try again shortly.");
  }
  if (!response.ok) {
    const e = new Error(data.error || "Could not complete this action.");
    e.status = response.status;
    throw e;
  }
  return data;
}
function requestId() {
  return (
    window.crypto?.randomUUID?.() ||
    Date.now() + "-" + Math.random().toString(36).slice(2)
  );
}
async function send(command, extra = {}) {
  const data = await api("command", {
    method: "POST",
    body: JSON.stringify({ command, request_id: requestId(), ...extra }),
  });
  app.operation = data.operation;
  toast("Action accepted. Progress will appear here.");
  poll();
  return data;
}
function confirmAction(title, text, action) {
  $("#confirm-title").textContent = title;
  $("#confirm-text").textContent = text;
  $("#confirm").showModal();
  $("#confirm-no").onclick = () => $("#confirm").close();
  $("#confirm-yes").onclick = async () => {
    $("#confirm").close();
    try {
      await action();
    } catch (e) {
      toast(e.message, true);
    }
  };
}
function heading(eyebrow, title, subtitle, action = "") {
  return `<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p>${subtitle}</p></div>${action}</div>`;
}
function empty(title, text, action = "", compact = false) {
  return `<div class="empty ${compact ? "compact" : ""}">${icon("layers")}<h3>${title}</h3><p>${text}</p>${action}</div>`;
}
function badge(phase) {
  return `<span class="badge ${escape(phase)}">${escape(phase)}</span>`;
}
function route(task) {
  return `<div class="route"><div><small>SOURCE</small><b title="${escape(task.source)}">${escape(task.source || "Source topic")}</b></div><span class="route-arrow">${icon("arrow")}</span><div><small>DESTINATION</small><b title="${escape(task.destination)}">${escape(task.destination || "Generated document")}</b></div></div>`;
}
function results(t) {
  return `<div class="results"><span><b>${["export", "index"].includes(t.kind) ? t.processed || 0 : t.success || 0}</b> ${t.kind === "clone" ? "forwarded" : t.kind === "transfer" ? "uploaded" : "processed"}</span><span><b>${t.skipped || 0}</b> skipped</span><span><b>${t.failed || 0}</b> failed</span></div>`;
}
function taskCard(t) {
  const filePercent = t.file_total
    ? Math.min(100, ((t.file_done || 0) / t.file_total) * 100)
    : 0;
  return `<article class="panel task-card"><div class="task-head"><div class="task-ident"><div class="kind-icon ${escape(t.kind)}">${icon(t.kind)}</div><div><h3>${types[t.kind]?.label || "Task"} in progress</h3><div class="tiny">TASK #${escape(String(t.id).slice(0, 8))}</div></div></div>${badge(t.flood_wait ? "waiting" : t.stage || t.phase)}</div>${route(t)}<div class="progress-header"><span>Overall progress · ${t.processed} / ${t.total || "…"} ${t.kind === "transfer" ? "files" : "messages"}</span><b>${Number(t.percent || 0).toFixed(1)}%</b></div>${progress(t.percent)}<div class="task-times"><span>Elapsed ${duration(t.elapsed)}</span><span>${t.flood_wait ? "Flood wait " + duration(t.flood_wait) : t.eta == null ? "Estimating remaining time" : "About " + duration(t.eta) + " remaining"}</span></div>${t.file ? `<div class="current-file"><div class="file-head">${icon("file")}<span title="${escape(t.file)}">${escape(t.file)}</span></div>${t.file_total ? progress(filePercent, "file") : ""}<div class="file-meta"><span>${filePercent.toFixed(1)}% · ${bytes(t.file_done)} / ${bytes(t.file_total)}</span><span>${t.speed ? bytes(t.speed) + "/s" : "—"}</span><span>ETA ${t.file_eta == null ? "—" : duration(t.file_eta)}</span></div></div>` : ""}<div class="task-bottom">${results(t)}<div class="task-actions"><button class="button small ghost" data-detail="${escape(t.id)}">Details</button><button class="icon-button" aria-label="Cancel ${escape(t.kind)} task" data-cancel="${escape(t.kind)}">${icon("stop")}</button></div></div></article>`;
}
function historyTable(tasks) {
  if (!tasks.length)
    return empty(
      "A clean slate",
      "Finished tasks will appear here, with their results and time taken.",
    );
  return `<div class="table-wrap"><table><thead><tr><th>Task</th><th>Type</th><th>Status</th><th>Processed</th><th>Time</th></tr></thead><tbody>${tasks.map((t) => `<tr class="clickable" data-detail="${escape(t.id)}" tabindex="0"><td class="task-title" title="${escape(t.source)}">${escape(t.name)}</td><td>${escape(types[t.kind]?.label)}</td><td>${badge(t.phase)}</td><td>${t.processed} ${t.failed ? `· ${t.failed} failed` : ""}</td><td>${duration(t.elapsed)}</td></tr>`).join("")}</tbody></table></div>`;
}
function metric(label, value, note, name) {
  return `<div class="metric"><div class="metric-top"><span>${label}</span><span class="metric-icon">${icon(name)}</span></div><div class="metric-value">${value}</div><div class="metric-foot">${note}</div></div>`;
}
function dashboard() {
  const d = app.data;
  const completed = d.history.filter((t) => t.phase === "completed").length;
  const s = d.stats;
  return (
    heading(
      "YOUR CONTROL ROOM",
      `Good to see you, ${escape(d.user.name)}.`,
      `Here's what's moving through your workspace.`,
      `<button class="button" data-view="create">${icon("plus")} New task</button>`,
    ) +
    `<div class="metrics">${metric("Active tasks", d.active.length, '<span class="mint">● Live</span> across all workflows', "play")}${metric("In the queue", d.queue.length, "Ready when the current task finishes", "layers")}${metric("Completed", completed, "From your recent task history", "check")}${metric("Bot uptime", duration(s.uptime), `${s.cpu.toFixed(1)}% CPU · ${s.ram.toFixed(1)}% RAM`, "cpu")}</div><div class="dashboard-grid"><div><div class="section-heading"><h2>Live activity <span class="subcount">${d.active.length} active</span></h2><span class="live-label"><span class="live-dot"></span>Updates every 3 seconds</span></div>${d.active.length ? d.active.map(taskCard).join("") : empty("Ready for your next task", "Create a clone, transfer, export or index. We’ll keep you updated as it runs.", `<button class="button secondary" data-view="create">${icon("plus")} Create a task</button>`)}<div class="history-section"><div class="section-heading"><h2>Recent activity</h2><button class="text-button" data-view="history">View all ${icon("arrow")}</button></div>${historyTable(d.history.slice(0, 5))}</div></div><aside class="dashboard-aside"><section class="panel small-panel quick-actions"><div class="section-heading"><h2>Quick start</h2>${icon("plus")}</div><div class="quick-actions-list">${Object.entries(
      types,
    )
      .map(
        ([k, v]) =>
          `<button class="quick-action" data-new="${k}"><span class="quick-icon">${icon(k)}</span><span><h3>${v.label} files</h3><p>${v.description}</p></span><span class="chevron">${icon("chevron")}</span></button>`,
      )
      .join(
        "",
      )}</div></section><section class="panel small-panel queue-box"><div class="section-heading"><h2>Up next</h2><button class="text-button" data-view="queue">${icon("arrow")}</button></div>${
      d.queue.length
        ? d.queue
            .slice(0, 4)
            .map(
              (t, i) =>
                `<div class="queue-preview"><span class="queue-position">${i + 1}</span><div><h3>${escape(t.name || t.source)}</h3><p>${types[t.kind]?.label} · Waiting</p></div></div>`,
            )
            .join("")
        : empty("Nothing waiting", "Your queue is clear.", "", true)
    }</section><section class="panel small-panel resource-box"><div class="section-heading"><h2>System health</h2><span class="live-dot"></span></div>${[
      ["CPU", s.cpu],
      ["Memory", s.ram],
      ["Disk usage", s.disk],
    ]
      .map(
        ([n, v]) =>
          `<div class="resource"><div class="resource-label"><span>${n}</span><b>${Number(v).toFixed(1)}%</b></div>${progress(v)}</div>`,
      )
      .join(
        "",
      )}<div class="resource-label"><span>Free storage</span><b>${bytes(s.free)}</b></div></section></aside></div>`
  );
}
const field = (
  name,
  label,
  placeholder = "",
  type = "text",
  value = "",
  full = false,
  help = "",
) =>
  `<div class="field ${full ? "full" : ""}"><label for="${name}">${label}</label><input id="${name}" name="${name}" type="${type}" placeholder="${escape(placeholder)}" value="${escape(value)}" ${["source", "destination"].includes(name) && app.kind === "clone" ? "required" : ""} ${type === "number" ? 'min="0" step="any"' : ""}>${help ? `<small>${help}</small>` : ""}</div>`;
const check = (name, label) =>
  `<label class="check-row"><input type="checkbox" name="${name}">${label}</label>`;
function create() {
  const k = app.kind;
  let fields = field(
    "source",
    k === "transfer" ? "Source link or MSZ folder" : "Source Telegram topic",
    "https://t.me/c/… or a folder link",
    "text",
    "",
    true,
  );
  if (k === "clone")
    fields += field(
      "destination",
      "Destination Telegram topic",
      "https://t.me/c/…",
      "text",
      "",
      true,
    );
  if (k === "transfer")
    fields +=
      `<div class="field"><label for="target">Send to</label><select id="target" name="target"><option value="gd">Google Drive</option><option value="msz">MSZ Drive</option><option value="telegram">Telegram</option><option value="both">MSZ + Google Drive</option><option value="index">Generate folder index</option></select></div>` +
      field(
        "destination",
        "Destination (optional)",
        "Folder ID, MSZ folder or Telegram topic",
        "text",
        "",
        false,
        "Leave blank to use your saved destination.",
      );
  if (k === "export")
    fields += field(
      "destination",
      "Upload topic (optional)",
      "Use the bot’s default destination",
      "text",
      "",
      true,
    );
  let advanced = "";
  if (k === "clone") {
    advanced =
      field("limit", "Message limit", "Use bot default", "number") +
      field(
        "start_id",
        "Start message ID",
        "Start from linked message",
        "number",
      ) +
      field("batch_size", "Batch size", "Use saved default", "number") +
      field(
        "delay_sec",
        "Delay between messages (seconds)",
        "Use saved default",
        "number",
      ) +
      field("filename_prefix", "Filename prefix", "Optional") +
      field("filename_suffix", "Filename suffix", "Optional") +
      field("text_prefix", "Text prefix", "Optional") +
      field("text_suffix", "Text suffix", "Optional") +
      field(
        "message_ids",
        "Specific message IDs",
        "7151,7152,7153",
        "text",
        "",
        true,
      );
  } else if (k === "export" || k === "index") {
    advanced =
      field("batch_size", "Batch size", "Use saved default", "number") +
      field(
        "batch_delay_sec",
        "Batch delay (seconds)",
        "Use saved default",
        "number",
      );
    if (k === "index")
      advanced += field("header", "Index title", "Optional", "text", "", true);
  } else {
    advanced =
      field(
        "gdrive_folder_id",
        "Google Drive folder ID",
        "Override saved folder",
      ) +
      field(
        "msz_target_folder",
        "MSZ destination folder",
        "Override saved folder",
      ) +
      `<div class="field full"><label for="index_file">Edited folder index (optional)</label><input id="index_file" name="index_file" type="file" accept=".txt"><small>Upload the edited .txt index to select which files to transfer.</small></div>`;
  }
  return (
    heading(
      "MAKE YOUR NEXT MOVE",
      "Create a task",
      "Choose a workflow. Add your source. We’ll handle the queue.",
    ) +
    `<div class="type-selector">${Object.entries(types)
      .map(
        ([name, t]) =>
          `<button class="type-card ${name === k ? "selected" : ""}" data-kind="${name}"><span class="kind-icon ${name}">${icon(name)}</span><b>${t.label}</b><p>${t.description}</p></button>`,
      )
      .join(
        "",
      )}</div><div class="form-layout"><form id="task-form"><div class="panel"><h2 class="form-section">${types[k].label} setup</h2><div class="form-grid">${fields}</div><div class="divider"></div><details><summary class="form-section">Options & customization</summary><div class="form-grid">${advanced}</div><div class="divider"></div>${["clone", "transfer"].includes(k) ? check("dry_run", "Dry run — preview without transferring files") : ""}${k === "clone" ? check("continue_on_error", "Continue if an individual message fails") + check("hide_sender_name", "Hide the original sender") : k === "transfer" ? check("resume", "Resume and skip recorded successes") + check("continue_on_error", "Continue if an individual file fails") + check("caption_file_names", "Use captions as filenames") : check("onwards", "Include messages after the linked topic") + (k === "export" ? check("caption_file_names", "Use captions as filenames") : "")}</details></div><div class="form-actions"><p>Tasks run using your saved bot credentials.</p><button type="submit" class="button">${icon("plus")} Add to queue</button></div></form><aside class="form-aside"><div class="panel"><h2 class="form-section">Task preview</h2><p class="muted">This is the command your bot will run.</p><pre id="command-preview" class="code">Add a source to get started.</pre><div class="tip"><b>One queue, everywhere.</b>Tasks created here and in Telegram run through the same bot engine. You can leave this app while they run.</div><div class="divider"></div><button class="text-button" data-view="settings">Manage saved defaults ${icon("arrow")}</button></div></aside></div>`
  );
}
const quote = (v) => "'" + String(v).replace(/'/g, "'\\''") + "'";
function buildCommand(form) {
  const data = new FormData(form),
    k = app.kind,
    source = String(data.get("source") || "").trim(),
    destination = String(data.get("destination") || "").trim();
  let args = ["/" + k];
  if (k === "clone") {
    args.push(
      "--source-link",
      quote(source),
      "--destination-link",
      quote(destination),
    );
  } else if (k === "transfer") {
    args.push(quote(source));
    if (destination) args.push(quote(destination));
    if (data.get("target") === "index") args.push("--index");
    else args.push("--up", String(data.get("target") || "gd"));
    if ($("#index_file")?.files.length) args.push("--index-done");
  } else {
    args.push("--topic-link", quote(source));
    if (k === "export" && destination)
      args.push("--upload-topic-link", quote(destination));
  }
  for (const name of [
    "limit",
    "start_id",
    "batch_size",
    "delay_sec",
    "filename_prefix",
    "filename_suffix",
    "text_prefix",
    "text_suffix",
    "message_ids",
    "batch_delay_sec",
    "header",
    "gdrive_folder_id",
    "msz_target_folder",
  ]) {
    const value = String(data.get(name) || "").trim();
    if (value) args.push("--" + name.replace(/_/g, "-"), quote(value));
  }
  for (const name of [
    "dry_run",
    "continue_on_error",
    "hide_sender_name",
    "resume",
    "caption_file_names",
    "onwards",
  ])
    if (
      data.get(name) &&
      !(name === "dry_run" && ["export", "index"].includes(k))
    )
      args.push("--" + name.replace(/_/g, "-"));
  return args.join(" ");
}
function queue() {
  const jobs = app.data.queue.filter(
    (j) => app.filter === "all" || j.kind === app.filter,
  );
  return (
    heading(
      "IN THE PIPELINE",
      "Your task queue",
      "Move a task to the front, or remove it before it starts.",
    ) +
    `<div class="toolbar"><div class="tabbar">${["all", ...Object.keys(types)].map((k) => `<button class="tab ${app.filter === k ? "active" : ""}" data-filter="${k}">${k === "all" ? "All tasks" : types[k].label}</button>`).join("")}</div><button class="button small danger" data-clear-queue="${app.filter}">${icon("trash")} Clear waiting</button></div>${jobs.length ? jobs.map((t, i) => `<article class="queue-row"><span class="queue-position">${i + 1}</span><span class="kind-icon ${t.kind}">${icon(t.kind)}</span><div class="queue-main"><h3 title="${escape(t.name)}">${escape(t.name || t.source)}</h3><p>${types[t.kind]?.label} ${t.destination ? "→ " + escape(t.destination) : ""}</p></div>${badge("queued")}<div class="button-row"><button class="button small ghost" data-queue="first" data-id="${escape(t.id)}" data-type="${t.kind}">${icon("up")} Move first</button><button class="button small danger" data-queue="remove" data-id="${escape(t.id)}" data-type="${t.kind}">${icon("close")} Remove</button></div></article>`).join("") : empty("No waiting tasks", "Add a new task and it will appear here until its turn starts.", `<button class="button secondary" data-view="create">${icon("plus")} New task</button>`)}<div class="tip"><b>Active tasks keep running.</b>Each workflow runs one task at a time. Clearing this queue only removes tasks that haven’t started.</div>`
  );
}
function history() {
  const jobs = app.data.history.filter(
    (j) => app.filter === "all" || j.kind === app.filter,
  );
  return (
    heading(
      "THE WORK YOU’VE DONE",
      "Task history",
      "Results, routes, and timings from your last 50 tasks.",
    ) +
    `<div class="toolbar"><div class="tabbar">${["all", ...Object.keys(types)].map((k) => `<button class="tab ${app.filter === k ? "active" : ""}" data-filter="${k}">${k === "all" ? "All tasks" : types[k].label}</button>`).join("")}</div><button class="button secondary small" data-view="create">${icon("plus")} New task</button></div>${historyTable(jobs)}<div class="panel history-section"><h2 class="form-section">Pick up where you left off</h2><p class="muted">Use the most recently saved profile for each workflow.</p><div class="button-row">${Object.entries(
      types,
    )
      .map(
        ([k, v]) =>
          `<button class="button secondary small" data-command="/${k} resume">${icon("play")} Resume latest ${v.label.toLowerCase()}</button>`,
      )
      .join("")}</div></div>`
  );
}
function settings() {
  if (!app.settings)
    return (
      heading(
        "MAKE IT YOURS",
        "Workspace settings",
        "Fetching your saved bot preferences…",
      ) + empty("Loading settings", "")
    );
  const group =
    app.settings.groups.find((g) => g.id === app.settingsTab) ||
    app.settings.groups[0];
  return (
    heading(
      "MAKE IT YOURS",
      "Workspace settings",
      "Your credentials and preferences stay with the bot.",
    ) +
    `<div class="settings-toolbar"><div class="tabbar">${app.settings.groups.map((g) => `<button class="tab ${g.id === group.id ? "active" : ""}" data-settings-tab="${g.id}">${escape(g.name)}</button>`).join("")}</div><button class="text-button" data-reload-settings>${icon("refresh")} Reload</button></div><div class="panel"><h2 class="form-section">${escape(group.name)} settings</h2><input class="search" id="settings-search" placeholder="Find a setting…" aria-label="Find a setting">${group.entries.map((s) => `<div class="settings-row" data-setting="${escape(s.key)}"><div><h3>${escape(s.label)}${s.secret ? `<span class="secret-tag">${s.configured ? "Configured" : "Not set"}</span>` : ""}</h3><div class="setting-key">${escape(s.key)}</div>${s.secret ? "<small>Leave blank to keep the current value.</small>" : ""}</div>${s.type === "boolean" ? `<input class="switch" aria-label="${escape(s.label)}" type="checkbox" ${s.value ? "checked" : ""} data-setting-toggle="${escape(s.key)}">` : `<div class="field"><input aria-label="${escape(s.label)}" data-setting-input="${escape(s.key)}" type="${s.secret ? "password" : s.type === "number" ? "number" : "text"}" ${s.type === "number" ? 'min="0" step="any"' : ""} autocomplete="off" value="${escape(s.value)}" placeholder="${s.secret ? "Enter a new value" : "Not set"}"></div>`}<div class="button-row">${s.type !== "boolean" ? `<button class="button small secondary" data-setting-save="${escape(s.key)}">Save</button>` : ""}<button class="icon-button" data-setting-reset="${escape(s.key)}" aria-label="Reset ${escape(s.label)}">${icon("refresh")}</button></div></div>`).join("")}</div><div class="panel"><h2 class="form-section">Import credentials</h2><p class="muted">Choose a credential file to save it using the bot’s existing import tools.</p><form id="credential-form" class="form-grid"><div class="field"><label for="credential-type">Credential type</label><select id="credential-type"><option value="gdrive_token_json">Google Drive OAuth JSON</option><option value="gdrive_token_pickle">Google Drive token.pickle</option><option value="msz_credentials">MSZ credentials</option><option value="tg_session_string">Telegram session string</option></select></div><div class="field"><label for="credential-file">File (up to 64 KB)</label><input type="file" id="credential-file" required></div><div class="field full"><button class="button secondary" type="submit">${icon("export")} Import file</button></div></form></div>`
  );
}
function tools() {
  const step = app.data.login_step;
  return (
    heading(
      "THE EXTRA CONTROLS",
      "Tools & diagnostics",
      "Reconnect, inspect logs, or use the full bot command interface.",
    ) +
    `<div class="tool-grid"><section class="panel"><div class="kind-icon">${icon("lock")}</div><h2 class="history-section">Telegram account</h2><p>Connect or replace the user account your bot uses for topic access and transfers.</p>${step ? `<form id="login-form"><div class="field"><label for="login-value">${step === "phone" ? "Phone number (international format)" : step === "code" ? "Telegram login code" : "Two-step verification password"}</label><input id="login-value" type="${step === "password" ? "password" : "text"}" autocomplete="off" required></div><div class="button-row history-section"><button class="button" type="submit">Continue ${icon("arrow")}</button><button class="button ghost" type="button" data-command="/login cancel">Cancel login</button></div></form>` : `<div class="button-row"><button class="button secondary" data-command="/login">${icon("lock")} Connect account</button><button class="button ghost" data-replace-login>Replace account</button></div>`}</section><section class="panel"><div class="kind-icon transfer">${icon("refresh")}</div><h2 class="history-section">Bot controls</h2><p>Restart the bot or repeat a saved task profile. Restart after confirming here in the app.</p><div class="button-row"><button class="button secondary" data-command="/help">Bot help</button><button class="button danger" data-command="/restart">Restart bot</button><button class="button ghost" data-view="history">Resume a task</button></div></section><section class="panel"><div class="section-heading"><h2>Diagnostic logs</h2><div class="button-row"><button class="text-button" data-logs="bot">Bot</button><button class="text-button" data-logs="transfer">Transfer</button></div></div><p>Recent logs with saved credentials redacted.</p><div class="button-row"><button class="button secondary small" data-logs="bot">${icon("terminal")} Load logs</button><button class="button ghost small" data-command="/log">Send full log to Telegram</button></div><pre class="console" id="log-output">Select a log to inspect.</pre></section><section class="panel"><h2>Command workspace</h2><p>All existing bot commands are available here. Task commands still use the shared queues.</p><form id="command-form"><div class="field"><label for="raw-command">Bot command</label><textarea id="raw-command" placeholder="/transfer msz:Course --up gd" required spellcheck="false"></textarea></div><button class="button secondary history-section" type="submit">${icon("terminal")} Run command</button></form></section></div><div class="reply-list"><div class="section-heading"><h2>Recent actions</h2><span class="muted">Replies also appear in your bot chat</span></div>${operationReplies()}</div>`
  );
}
function operationReplies() {
  return (
    (app.data.operations || [])
      .slice(-8)
      .reverse()
      .map(
        (o) =>
          `<div class="reply"><small>${escape(o.kind)} · ${escape(o.phase)}</small>${escape(o.messages.join("\n") || "Working…")}</div>`,
      )
      .join("") || '<p class="muted">Your action results will appear here.</p>'
  );
}
function locked(message) {
  if (!tg?.initData) {
    const enabled = app.browserAuth?.enabled;
    const errors = {
      setup: "Website login needs to be configured in BotFather and Heroku.",
      admin: "This Telegram account is not a bot admin.",
      failed: "Login was cancelled or could not be verified. Please try again.",
    };
    const loginError =
      errors[new URLSearchParams(location.search).get("login_error")];
    return `<div class="panel locked"><span class="brand-icon">↗</span><div class="eyebrow">MSZ WORKSPACE</div><h1>Your bot’s control room.</h1><p>${escape(loginError || (enabled ? "Sign in with your Telegram admin account to manage tasks, queues and settings from your browser." : app.browserAuth ? "Website login is awaiting BotFather setup. You can still use the app inside Telegram." : "Connecting to your workspace…"))}</p>${enabled ? '<a class="button" href="/auth/login">Log in with Telegram</a>' : ""}<div class="history-section"><a class="text-button" href="https://t.me/mszec_bot">Open the bot in Telegram</a></div><p class="muted history-section">Admin access only · Sessions expire after 12 hours</p></div>`;
  }
  return `<div class="panel locked"><span class="brand-icon">↗</span><div class="eyebrow">MSZ WORKSPACE</div><h1>Your bot’s control room.</h1><p>${escape(message || "Open this Mini App from your Telegram bot to securely access your tasks, queues and settings.")}</p><button class="button" id="retry-auth">${icon("refresh")} Try connecting again</button><p class="muted history-section">In your bot chat, send /app or tap Open App.</p></div>`;
}
function render() {
  if (app.locked || !app.data) {
    $("#content").innerHTML = locked(app.error);
    fillIcons();
    return;
  }
  const pages = { dashboard, create, queue, history, settings, tools };
  $("#content").innerHTML = pages[app.view]();
  $("#page-name").textContent = names[app.view];
  $("#avatar").textContent = (app.data.user.name || "M")
    .slice(0, 1)
    .toUpperCase();
  $("#queue-count").textContent = app.data.queue.length;
  $$("[data-view]").forEach((el) =>
    el.classList.toggle("active", el.dataset.view === app.view),
  );
  fillIcons();
  if (app.view === "create") {
    restoreDraft();
    updatePreview();
  }
}
function navigate(view) {
  if (!names[view]) return;
  if (app.view === "create") saveDraft();
  app.view = view;
  app.filter = "all";
  location.hash = view;
  render();
  window.scrollTo({ top: 0, behavior: "smooth" });
  try {
    if (view === "dashboard") tg?.BackButton?.hide();
    else tg?.BackButton?.show();
  } catch (_) {}
  if (view === "settings") loadSettings();
}
function saveDraft() {
  const form = $("#task-form");
  if (!form) return;
  const draft = {};
  form.querySelectorAll("input,select").forEach((el) => {
    if (el.type !== "file")
      draft[el.name] = el.type === "checkbox" ? el.checked : el.value;
  });
  app.drafts[app.kind] = draft;
}
function restoreDraft() {
  const form = $("#task-form"),
    draft = app.drafts[app.kind] || {};
  form?.querySelectorAll("input,select").forEach((el) => {
    if (el.type !== "file" && draft[el.name] != null) {
      if (el.type === "checkbox") el.checked = draft[el.name];
      else el.value = draft[el.name];
    }
  });
}
function updatePreview() {
  const form = $("#task-form");
  if (form && $("#command-preview"))
    $("#command-preview").textContent = buildCommand(form);
}
async function loadSettings() {
  try {
    app.settings = await api("settings");
    app.settingsLoaded = Date.now();
    if (app.view === "settings") render();
  } catch (e) {
    toast(e.message, true);
  }
}
function showDetail(id) {
  const t = [...app.data.active, ...app.data.history, ...app.data.latest].find(
    (t) => t.id === id,
  );
  if (!t) return;
  app.detail = id;
  const active = app.data.active.some((j) => j.id === id);
  $("#detail-body").innerHTML =
    `<div class="eyebrow">${escape(types[t.kind]?.label)} TASK</div><h2>${escape(t.file || t.name)}</h2>${badge(t.phase)}<div class="history-section">${route(t)}</div><div class="progress-header"><span>${t.processed} / ${t.total || "…"} processed</span><b>${Number(t.percent || 0).toFixed(1)}%</b></div>${progress(t.percent)}<div class="detail-grid">${[
      ["Forwarded / uploaded", t.success],
      ["Skipped", t.skipped],
      ["Failed", t.failed],
      ["Time taken", duration(t.elapsed)],
      ["Stage", t.stage],
      ["Time remaining", duration(t.eta)],
    ]
      .map(
        ([label, value]) =>
          `<div class="detail-item"><small>${label}</small><b>${escape(value)}</b></div>`,
      )
      .join(
        "",
      )}</div><div class="button-row">${active ? `<button class="button danger" data-cancel="${t.kind}">${icon("stop")} Cancel task</button>` : ""}<button class="button secondary" data-command="/status ${t.kind}">Show in bot chat</button>${t.kind === "transfer" ? `<button class="button ghost" data-command="/transfer logs">Send task log</button>` : ""}</div>`;
  fillIcons();
  if (!$("#detail").open) $("#detail").showModal();
}
async function poll() {
  if (app.polling || document.hidden) return;
  app.polling = true;
  try {
    if (!tg?.initData && (!app.browserAuth || app.locked)) {
      const response = await fetch("/auth/session");
      if (!response.ok)
        throw new Error("Could not check your browser session.");
      app.browserAuth = await response.json();
      $("#browser-logout").hidden = !app.browserAuth.authenticated;
      if (!app.browserAuth.authenticated) {
        app.data = null;
        app.locked = true;
        $("#connection").textContent = "Sign in required";
        $("#sync").textContent = "Signed out";
        render();
        return;
      }
    }
    const data = await api("state");
    app.data = data;
    app.connected = true;
    app.locked = false;
    app.error = "";
    $("#connection").textContent = "Bot connected";
    $("#sync").textContent = "Live";
    const dynamic = ["dashboard", "queue", "history"].includes(app.view);
    if (dynamic || !$("#content").children.length || $("#retry-auth")) render();
    if (app.view === "tools") {
      const oldStep =
        $("#login-form")?.querySelector("label")?.textContent || "";
      if (
        (data.login_step && !oldStep) ||
        (!data.login_step && $("#login-form"))
      )
        render();
      else if (data.login_step === "code" && !oldStep.includes("code"))
        render();
      else if (data.login_step === "password" && !oldStep.includes("password"))
        render();
      const list = $(".reply-list");
      if (list)
        list.innerHTML = `<div class="section-heading"><h2>Recent actions</h2></div>${operationReplies()}`;
    }
    for (const o of data.operations) {
      if (
        o.id === app.operation &&
        ["completed", "failed"].includes(o.phase) &&
        !app.seen.has(o.id)
      ) {
        app.seen.add(o.id);
        toast(o.messages.at(-1) || "Action completed.", o.phase === "failed");
        if (app.view === "settings") loadSettings();
      }
    }
    if ($("#detail").open && app.detail) showDetail(app.detail);
  } catch (e) {
    app.connected = false;
    $("#connection").textContent = "Reconnecting";
    $("#sync").textContent = "Offline";
    if (e.status === 401) {
      app.locked = true;
      app.data = null;
      app.settings = null;
      $("#detail").close();
      $("#confirm").close();
      app.error = e.message;
      render();
    } else if (!app.data) {
      app.error = e.message;
      render();
    }
  } finally {
    app.polling = false;
  }
}
document.addEventListener("click", async (event) => {
  const button = event.target.closest("button,[data-detail]");
  if (!button) return;
  try {
    if (button.dataset.view) {
      navigate(button.dataset.view);
      return;
    }
    if (button.dataset.new) {
      app.kind = button.dataset.new;
      navigate("create");
      return;
    }
    if (button.dataset.kind) {
      saveDraft();
      app.kind = button.dataset.kind;
      render();
      return;
    }
    if (button.dataset.filter) {
      app.filter = button.dataset.filter;
      render();
      return;
    }
    if (button.dataset.settingsTab) {
      app.settingsTab = button.dataset.settingsTab;
      render();
      return;
    }
    if (button.dataset.detail) {
      showDetail(button.dataset.detail);
      return;
    }
    if (button.dataset.cancel) {
      const k = button.dataset.cancel;
      confirmAction(
        "Cancel this task?",
        "The current task will stop. Waiting tasks will continue in order.",
        async () => {
          await send("/cancel " + k);
          $("#detail").close();
        },
      );
      return;
    }
    if (button.dataset.queue) {
      const action = button.dataset.queue;
      const run = async () => {
        await api("queue", {
          method: "POST",
          body: JSON.stringify({
            action,
            kind: button.dataset.type,
            id: button.dataset.id,
          }),
        });
        toast(
          action === "first" ? "Task moved to the front." : "Task removed.",
        );
        await poll();
      };
      if (action === "remove")
        confirmAction(
          "Remove waiting task?",
          "This task will be removed before it starts.",
          run,
        );
      else await run();
      return;
    }
    if (button.hasAttribute("data-clear-queue")) {
      const kind = button.dataset.clearQueue;
      confirmAction(
        "Clear waiting tasks?",
        "Active tasks will keep running.",
        async () => {
          for (const k of kind === "all" ? Object.keys(types) : [kind]) {
            if (app.data.queue.some((t) => t.kind === k))
              await api("queue", {
                method: "POST",
                body: JSON.stringify({ action: "clear", kind: k }),
              });
          }
          toast("Waiting queue cleared.");
          await poll();
        },
      );
      return;
    }
    if (button.dataset.command) {
      if (button.dataset.command === "/restart")
        confirmAction(
          "Restart the bot?",
          "The bot will reconnect and restore its waiting queues.",
          () => send("/restart", { confirmed: true }),
        );
      else await send(button.dataset.command);
      return;
    }
    if (button.dataset.settingSave) {
      const key = button.dataset.settingSave,
        input = $(`[data-setting-input="${CSS.escape(key)}"]`);
      if (input.type === "password" && !input.value)
        return toast("Enter a new value to replace the saved credential.");
      await send("/settings set " + key + " " + input.value);
      if (input.type === "password") input.value = "";
      return;
    }
    if (button.dataset.settingReset) {
      confirmAction("Reset this setting?", "Restore its default value.", () =>
        send("/settings reset " + button.dataset.settingReset),
      );
      return;
    }
    if (button.hasAttribute("data-reload-settings")) {
      await loadSettings();
      return;
    }
    if (button.hasAttribute("data-replace-login")) {
      confirmAction(
        "Replace Telegram account?",
        "Start a new login to replace the saved user session.",
        () => send("/login force"),
      );
      return;
    }
    if (button.dataset.logs) {
      button.disabled = true;
      const data = await api("logs?kind=" + button.dataset.logs);
      $("#log-output").textContent = data.text;
      button.disabled = false;
      return;
    }
    if (button.id === "refresh" || button.id === "retry-auth") {
      app.locked = false;
      await poll();
      return;
    }
    if (button.classList.contains("dialog-close")) {
      $("#detail").close();
      return;
    }
  } catch (e) {
    button.disabled = false;
    toast(e.message, true);
  }
});
$("#browser-logout").addEventListener("click", async () => {
  try {
    const response = await fetch("/auth/logout", {
      method: "POST",
      headers: { "X-CSRF-Token": app.browserAuth?.csrf || "" },
    });
    if (!response.ok)
      throw new Error("Could not log out. Refresh the page and try again.");
    location.replace("/");
  } catch (error) {
    toast(error.message, true);
  }
});
document.addEventListener("change", async (e) => {
  if (e.target.matches("#task-form input,#task-form select")) {
    saveDraft();
    updatePreview();
  }
  if (e.target.dataset.settingToggle) {
    const checkbox = e.target;
    try {
      await send(
        "/settings set " +
          checkbox.dataset.settingToggle +
          " " +
          checkbox.checked,
      );
    } catch (err) {
      checkbox.checked = !checkbox.checked;
      toast(err.message, true);
    }
  }
});
document.addEventListener("input", (e) => {
  if (e.target.closest("#task-form")) {
    saveDraft();
    updatePreview();
  }
  if (e.target.id === "settings-search") {
    const term = e.target.value.toLowerCase();
    $$(".settings-row").forEach(
      (row) => (row.hidden = !row.textContent.toLowerCase().includes(term)),
    );
  }
});
document.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target,
    button = form.querySelector('[type="submit"]');
  if (button) button.disabled = true;
  try {
    if (form.id === "task-form") {
      const source = form.elements.source.value.trim();
      if (!source) throw new Error("Add a source link first.");
      if (app.kind === "clone" && !form.elements.destination.value.trim())
        throw new Error("Add a destination topic.");
      const command = buildCommand(form),
        file = $("#index_file")?.files[0];
      if (file) {
        if (file.size > 2 * 1024 * 1024)
          throw new Error("Index file must be smaller than 2 MB.");
        const data = new FormData();
        data.append("command", command);
        data.append("file", file);
        const response = await api("upload", { method: "POST", body: data });
        app.operation = response.operation;
        toast("Task accepted.");
      } else await send(command);
      navigate("queue");
      app.drafts[app.kind] = {};
    }
    if (form.id === "command-form") {
      await send($("#raw-command").value.trim());
      $("#raw-command").value = "";
    }
    if (form.id === "credential-form") {
      const file = $("#credential-file").files[0];
      if (!file) throw new Error("Choose a credential file.");
      if (file.size > 65536)
        throw new Error("Credential files must be smaller than 64 KB.");
      const data = new FormData();
      data.append("command", "/settings upload " + $("#credential-type").value);
      data.append("file", file);
      const response = await api("upload", { method: "POST", body: data });
      app.operation = response.operation;
      toast("File import accepted.");
      $("#credential-file").value = "";
      poll();
    }
    if (form.id === "login-form") {
      const value = $("#login-value").value;
      $("#login-value").value = "";
      await send("", { login_input: value });
    }
  } catch (err) {
    toast(err.message, true);
  } finally {
    if (button) button.disabled = false;
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.matches("tr[data-detail]"))
    showDetail(e.target.dataset.detail);
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();
});
try {
  tg?.BackButton?.onClick(() => navigate("dashboard"));
  tg?.onEvent("activated", poll);
  tg?.SettingsButton?.onClick(() => navigate("settings"));
} catch (_) {}
window.addEventListener("hashchange", () => {
  const view = location.hash.slice(1);
  if (names[view] && view !== app.view) navigate(view);
});
if (names[location.hash.slice(1)]) app.view = location.hash.slice(1);
render();
poll();
setInterval(poll, 3000);
