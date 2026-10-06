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
  creating: false,
  createChat: null,
  createRequest: null,
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
  if (typeof wholeGroupMode === "function" && wholeGroupMode() && picker.provider === "telegram")
    return item.kind === "chat" && item.forum;
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
        const description = item.forum && typeof wholeGroupMode === "function" && wholeGroupMode()
          ? "Forum · all topics" : item.description || "";
        const unavailable = picker.field !== "source" && item.writable === false
          ? " · Read-only folder"
          : !selectable && !item.expandable ? (typeof wholeGroupMode === "function" && wholeGroupMode() ? " · Choose a whole forum group" : " · This workflow needs a forum topic") : "";
        const create = picker.provider === "telegram" && picker.field === "destination" && item.forum && !(typeof wholeGroupMode === "function" && wholeGroupMode())
          ? `<button type="button" class="button secondary small picker-new-topic" data-picker-new-topic="${escape(item.id)}" aria-label="New topic in ${escape(item.name)}" ${picker.creating ? "disabled" : ""}>+ New topic</button>` : "";
        return `<div class="picker-node"><div class="picker-row ${selected ? "selected" : ""}"><span class="picker-indent" data-picker-depth="${depth}"></span>${item.expandable ? `<button class="icon-button picker-expand ${expanded ? "expanded" : ""}" type="button" data-picker-expand="${escape(item.id)}" aria-label="${expanded ? "Collapse" : "Expand"} ${escape(item.name)}" aria-expanded="${expanded}">${icon("chevron")}</button>` : '<span class="picker-spacer"></span>'}<button type="button" class="picker-entry" ${selectable ? `data-picker-select="${escape(item.id)}"` : item.expandable ? `data-picker-expand="${escape(item.id)}"` : "disabled"} aria-pressed="${selected}"><span class="picker-entry-icon">${icon(item.kind === "topic" ? "layers" : item.kind === "chat" ? "clone" : "folder")}</span><span><b>${escape(item.name)}</b><small>${escape(description)}${escape(unavailable)}</small></span>${selected ? icon("check") : ""}</button>${create}</div>${children}</div>`;
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
  $("#picker-choose").disabled = !picker.selected || picker.creating;
  $("#picker-topic-form").hidden = !picker.createChat;
  $("#picker-topic-submit").disabled = picker.creating || Boolean(picker.branches.get(picker.createChat?.id)?.loading);
  $("#picker-topic-title").disabled = picker.creating;
  $("#picker-topic-cancel").disabled = picker.creating;
  $("#picker-topic-submit").textContent = picker.creating ? "Creating…" : "Create & select";
  $("#picker-refresh").disabled = picker.creating;
  $("#picker-close").disabled = picker.creating;
  $$("[data-picker-provider]").forEach(button => button.disabled = picker.creating);
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
  if (picker.creating) return;
  picker.createChat = null;
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
  if (button.dataset.pickerNewTopic && !picker.creating) {
    picker.createChat = picker.entries.get(button.dataset.pickerNewTopic);
    picker.createRequest = null;
    $("#picker-topic-label").textContent = `New topic in ${picker.createChat.name}`;
    $("#picker-topic-title").value = "";
    $("#picker-topic-error").textContent = "";
    picker.expanded.add(picker.createChat.id);
    renderPicker();
    if (!picker.branches.has(picker.createChat.id)) loadPickerBranch(picker.createChat.id);
    $("#picker-topic-title").focus();
  }
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
pickerDialog.addEventListener("cancel", event => {
  if (picker.creating) event.preventDefault();
});
$("#picker-search").oninput = renderPicker;
$("#picker-refresh").onclick = () => switchPickerRefresh();
function switchPickerRefresh() {
  if (picker.creating) return;
  picker.createChat = null;
  picker.generation++;
  picker.branches.clear();
  picker.entries.clear();
  picker.expanded.clear();
  picker.selected = null;
  loadPickerBranch("root", true);
}
$("#picker-topic-cancel").onclick = () => {
  picker.createChat = null;
  renderPicker();
};
$("#picker-topic-form").onsubmit = async (event) => {
  event.preventDefault();
  if (picker.creating || !picker.createChat || picker.branches.get(picker.createChat.id)?.loading) return;
  const title = $("#picker-topic-title").value.trim();
  if (!title || new TextEncoder().encode(title).length > 128) {
    $("#picker-topic-error").textContent = "Enter a topic name of up to 128 UTF-8 bytes.";
    return;
  }
  const chat = picker.createChat;
  const generation = picker.generation;
  if (picker.createRequest?.title !== title || picker.createRequest?.chat !== chat.id)
    picker.createRequest = {title, chat: chat.id, id: crypto.randomUUID()};
  picker.creating = true;
  $("#picker-topic-error").textContent = "";
  renderPicker();
  try {
    const response = await api("topics", {method: "POST", body: JSON.stringify({
      chat_id: chat.id, title, request_id: picker.createRequest.id,
    })});
    if (generation !== picker.generation || !pickerDialog.open) {
      toast("Telegram topic created. Reopen the destination picker to choose it.");
      return;
    }
    const item = response.item;
    const branch = picker.branches.get(chat.id);
    picker.branches.set(chat.id, {...branch, loading: false, items: [
      ...(branch?.items || []).filter(existing => existing.id !== item.id), item,
    ]});
    picker.entries.set(item.id, item);
    picker.expanded.add(chat.id);
    picker.selected = item;
    picker.createChat = null;
    $("#picker-search").value = "";
    $("#picker-note").textContent = `Created ${item.name}. Use selection to set this destination.`;
  } catch (error) {
    if (generation === picker.generation && pickerDialog.open)
      $("#picker-topic-error").textContent = error.message;
  } finally {
    picker.creating = false;
    renderPicker();
  }
};
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
  if (typeof resetGroupMappings === "function") resetGroupMappings();
  const label = document.getElementById(fieldName + "-selection");
  if (label)
    label.textContent = `${providerLabels[picker.provider]} / ${item.name}`;
  if (input.closest("details")) input.closest("details").open = true;
  saveDraft();
  updatePreview();
  pickerDialog.close();
};
