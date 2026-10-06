"use strict";
const picker = {
  field: "source",
  provider: "telegram",
  generation: 0,
  branches: new Map(),
  entries: new Map(),
  expanded: new Set(),
  selected: null,
  providers: [],
};
const providerLabels = {
  telegram: "Telegram",
  gdrive: "Google Drive",
  msz: "MSZ Cloud",
};
const pickerDialog = document.getElementById("endpoint-picker");

function allowedProviders(fieldName) {
  if (fieldName === "gdrive_folder_id") return ["gdrive"];
  if (fieldName === "msz_target_folder") return ["msz"];
  if (app.kind !== "transfer") return ["telegram"];
  if (fieldName === "source") return ["telegram", "gdrive", "msz"];
  const target = document.getElementById("target")?.value;
  return target === "gd"
    ? ["gdrive"]
    : target === "msz"
      ? ["msz"]
      : target === "both"
        ? ["gdrive", "msz"]
        : target === "telegram"
          ? ["telegram"]
          : [];
}

function pickerSelectable(item) {
  if (!item.can_select) return false;
  if (picker.provider === "telegram" && !item.topic && app.kind !== "clone")
    return false;
  if (picker.field !== "source" && item.writable === false) return false;
  return true;
}

function renderPicker() {
  $("#picker-providers").innerHTML = picker.providers
    .map(
      (p) =>
        `<button type="button" class="tab ${p === picker.provider ? "active" : ""}" data-picker-provider="${p}">${escape(providerLabels[p])}</button>`,
    )
    .join("");
  const term = $("#picker-search").value.trim().toLowerCase();
  function branch(parent, depth, ancestors = new Set()) {
    if (ancestors.has(parent)) return "";
    const visited = new Set(ancestors);
    visited.add(parent);
    const data = picker.branches.get(parent);
    if (!data) return "";
    if (data.loading && !data.items?.length)
      return `<div class="picker-message" role="status">${icon("refresh")} Indexing ${escape(providerLabels[picker.provider])}…</div>`;
    if (data.error)
      return `<div class="picker-message picker-error">${escape(data.error)}<button type="button" class="text-button" data-picker-retry="${escape(parent)}">Try again</button></div>`;
    if (parent !== "root" && !data.items?.length && !term)
      return `<div class="picker-message">${picker.provider === "telegram" ? "No topics found." : "No subfolders."}</div>`;
    const rows = (data.items || [])
      .map((item) => {
        const expanded = picker.expanded.has(item.id);
        const children = expanded ? branch(item.id, depth + 1, visited) : "";
        if (
          term &&
          !`${item.name} ${item.description || ""}`
            .toLowerCase()
            .includes(term) &&
          !children
        )
          return "";
        const selectable = pickerSelectable(item);
        const selected = picker.selected?.id === item.id;
        const unavailable = picker.field !== "source" && item.writable === false
          ? " · Read-only folder"
          : !selectable && !item.expandable ? " · This workflow needs a forum topic" : "";
        return `<div class="picker-node"><div class="picker-row ${selected ? "selected" : ""}"><span class="picker-indent" data-picker-depth="${depth}"></span>${item.expandable ? `<button class="icon-button picker-expand ${expanded ? "expanded" : ""}" type="button" data-picker-expand="${escape(item.id)}" aria-label="${expanded ? "Collapse" : "Expand"} ${escape(item.name)}" aria-expanded="${expanded}">${icon("chevron")}</button>` : '<span class="picker-spacer"></span>'}<button type="button" class="picker-entry" ${selectable ? `data-picker-select="${escape(item.id)}"` : item.expandable ? `data-picker-expand="${escape(item.id)}"` : "disabled"} aria-pressed="${selected}"><span class="picker-entry-icon">${icon(item.kind === "topic" ? "layers" : item.kind === "chat" ? "clone" : "folder")}</span><span><b>${escape(item.name)}</b><small>${escape(item.description || "")}${escape(unavailable)}</small></span>${selected ? icon("check") : ""}</button></div>${children}</div>`;
      })
      .join("");
    return (
      rows +
      (data.next
        ? `<button type="button" class="text-button picker-more" data-picker-more="${escape(parent)}">${data.loading ? "Loading…" : "Load more folders"}</button>`
        : "")
    );
  }
  const markup = branch("root", 0);
  $("#picker-tree").innerHTML =
    markup ||
    `<div class="picker-message">${term ? "No matching entries. Expand a folder to search its children." : "No entries found in this account."}</div>`;
  $$("[data-picker-depth]").forEach((el) => {
    el.style.width = `${Math.min(Number(el.dataset.pickerDepth), 8) * 18}px`;
  });
  $("#picker-choose").disabled = !picker.selected;
  $("#picker-selection").textContent = picker.selected
    ? `${providerLabels[picker.provider]} / ${picker.selected.description || picker.selected.name}`
    : "Choose a chat/topic or folder to continue.";
  fillIcons();
}

async function loadPickerBranch(
  parent = "root",
  refresh = false,
  more = false,
) {
  const generation = picker.generation;
  const previous = picker.branches.get(parent);
  if (previous?.loading) return;
  const cursor = more ? previous?.next || "" : "";
  picker.branches.set(parent, {
    loading: true,
    items: more ? previous.items : [],
    next: more ? previous.next : null,
  });
  renderPicker();
  try {
    let response = await api("catalog", {
      method: "POST",
      body: JSON.stringify({
        provider: picker.provider,
        parent,
        cursor,
        refresh,
      }),
    });
    while (response.status === "loading") {
      const job = response.job;
      await new Promise((resolve) => setTimeout(resolve, 1200));
      if (generation !== picker.generation || !pickerDialog.open) return;
      response = await api("catalog?job=" + encodeURIComponent(job));
      if (response.status === "loading") response.job = job;
    }
    if (generation !== picker.generation || !pickerDialog.open) return;
    if (response.status === "failed") throw new Error(response.error);
    const data = response.data || response;
    const items = more ? [...previous.items, ...data.items] : data.items;
    const unique = [...new Map(items.map((item) => [item.id, item])).values()];
    picker.branches.set(parent, { ...data, items: unique, loading: false });
    unique.forEach((item) => picker.entries.set(item.id, item));
    $("#picker-note").textContent =
      `${data.note || ""} ${data.cached ? "Using cached index." : "Index updated."}`;
  } catch (error) {
    if (generation !== picker.generation || !pickerDialog.open) return;
    picker.branches.set(parent, { error: error.message, items: [] });
  }
  renderPicker();
}

function switchPickerProvider(provider) {
  picker.generation++;
  picker.provider = provider;
  picker.branches = new Map();
  picker.entries = new Map();
  picker.expanded = new Set();
  picker.selected = null;
  $("#picker-search").value = "";
  $("#picker-note").textContent =
    "Listings use the account connected to your bot. Refresh to include recent changes.";
  renderPicker();
  loadPickerBranch();
}

function openPicker(fieldName) {
  const providers = allowedProviders(fieldName);
  if (!providers.length) {
    toast("This workflow generates an index and does not need a destination.");
    return;
  }
  picker.field = fieldName;
  picker.providers = providers;
  $("#picker-title").textContent =
    fieldName === "source" ? "Choose a source" : "Choose a destination";
  pickerDialog.showModal();
  switchPickerProvider(providers[0]);
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.browse) openPicker(button.dataset.browse);
  if (button.dataset.pickerProvider)
    switchPickerProvider(button.dataset.pickerProvider);
  const expand = button.dataset.pickerExpand;
  if (expand) {
    if (picker.expanded.has(expand)) picker.expanded.delete(expand);
    else {
      picker.expanded.add(expand);
      if (!picker.branches.has(expand)) loadPickerBranch(expand);
    }
    renderPicker();
  }
  if (button.dataset.pickerSelect) {
    picker.selected = picker.entries.get(button.dataset.pickerSelect);
    renderPicker();
  }
  if (button.dataset.pickerMore)
    loadPickerBranch(button.dataset.pickerMore, false, true);
  if (button.dataset.pickerRetry)
    loadPickerBranch(button.dataset.pickerRetry, true);
});
$("#picker-close").onclick = () => pickerDialog.close();
pickerDialog.addEventListener("close", () => {
  picker.generation++;
});
$("#picker-search").oninput = renderPicker;
$("#picker-refresh").onclick = () => switchPickerRefresh();
function switchPickerRefresh() {
  picker.generation++;
  picker.branches.clear();
  picker.entries.clear();
  picker.expanded.clear();
  picker.selected = null;
  loadPickerBranch("root", true);
}
$("#picker-choose").onclick = () => {
  const item = picker.selected;
  if (!item) return;
  let fieldName = picker.field;
  let value = fieldName === "source" ? item.source : item.destination;
  if (
    app.kind === "transfer" &&
    fieldName === "destination" &&
    $("#target")?.value === "both"
  ) {
    fieldName =
      picker.provider === "gdrive" ? "gdrive_folder_id" : "msz_target_folder";
  }
  if (fieldName === "gdrive_folder_id") value = item.id;
  if (fieldName === "msz_target_folder")
    value = item.destination?.replace(/^msz:/, "");
  const input = document.getElementById(fieldName);
  if (!input || !value) return;
  input.value = value;
  const label = document.getElementById(fieldName + "-selection");
  if (label)
    label.textContent = `${providerLabels[picker.provider]} / ${item.name}`;
  if (input.closest("details")) input.closest("details").open = true;
  saveDraft();
  updatePreview();
  pickerDialog.close();
};
