"use strict";
const groupMappings = { generation: 0, sources: [], destinations: [], choices: {}, page: 0, search: "" };
function wholeGroupMode() { return app.kind === "clone" && $("#clone_scope")?.value === "group"; }
function syncCloneScope() {
  if (app.kind !== "clone" || !$("#clone_scope")) return;
  const whole = wholeGroupMode();
  $("#group-mapping").hidden = !whole;
  for (const field of ["source", "destination"])
    document.querySelector(`label[for="${field}"]`).textContent = whole
      ? `${field === "source" ? "Source" : "Destination"} forum group`
      : `${field === "source" ? "Source" : "Destination"} Telegram chat or topic`;
  document.querySelector('label[for="limit"]').textContent = whole ? "Message limit per topic (0 = all)" : "Message limit";
  $("#limit").placeholder = whole ? "0 = all messages in every topic" : "Use bot default";
  $("#message_ids").disabled = whole;
  if (whole) $("#message_ids").value = "";
}
function resetGroupMappings() {
  groupMappings.generation++;
  groupMappings.sources = [];
  groupMappings.choices = {};
  groupMappings.page = 0;
  groupMappings.search = "";
  if ($("#topic_map")) $("#topic_map").value = "{}";
  if ($("#group-mapping-list")) $("#group-mapping-list").innerHTML = "";
  if ($("#group-load-topics")) $("#group-load-topics").disabled = false;
  if (wholeGroupMode()) { saveDraft(); updatePreview(); }
}
function groupChatId(value) {
  const match = value.trim().match(/^https?:\/\/t\.me\/c\/(\d+)\/\d+\/?(?:\?.*)?$/);
  if (!match) throw new Error("Choose the whole source and destination forum groups with Browse first.");
  return "-100" + match[1];
}
async function groupCatalog(parent, generation) {
  let response = await api("catalog", {method: "POST", body: JSON.stringify({provider: "telegram", parent})});
  const job = response.job;
  while (response.status === "loading") {
    await new Promise(resolve => setTimeout(resolve, 1200));
    if (generation !== groupMappings.generation) return [];
    response = await api("catalog?job=" + encodeURIComponent(job));
  }
  if (response.status === "failed") throw new Error(response.error);
  const topics = (response.data || response).items.map(item => ({id: item.id.split(":").pop(), name: item.name}));
  if (!topics.some(topic => topic.id === "1")) topics.unshift({id: "1", name: "General"});
  return topics;
}
function renderGroupMappings() {
  const sources = groupMappings.sources.filter(topic => topic.name.toLowerCase().includes(groupMappings.search));
  const pages = Math.max(1, Math.ceil(sources.length / 40));
  groupMappings.page = Math.min(groupMappings.page, pages - 1);
  const start = groupMappings.page * 40;
  $("#group-mapping-list").innerHTML = `<div class="field"><label for="group-topic-search">Search source topics</label><input id="group-topic-search" value="${escape(groupMappings.search)}" placeholder="Find a topic in this group"></div><p class="muted">${groupMappings.sources.length} source topics · ${groupMappings.destinations.length} destination topics</p><div class="group-map-rows">${sources.slice(start, start + 40).map(topic => {
    const choice = groupMappings.choices[topic.id] ?? (topic.id === "1" ? "1" : "new");
    return `<div class="group-map-row"><span><b>${escape(topic.name)}</b><small>Source #${escape(topic.id)}</small></span><div class="field"><select data-group-map="${escape(topic.id)}" aria-label="Destination for ${escape(topic.name)}"><option value="new" ${choice === "new" ? "selected" : ""}>Create new topic: ${escape(topic.name)}</option>${groupMappings.destinations.map(destination => `<option value="${escape(destination.id)}" ${choice === destination.id ? "selected" : ""}>${escape(destination.name)} · #${escape(destination.id)}</option>`).join("")}</select></div></div>`;
  }).join("") || '<p class="muted">No matching topics.</p>'}</div><div class="button-row"><button type="button" class="button secondary small" data-group-page="-1" ${groupMappings.page === 0 ? "disabled" : ""}>Previous</button><span class="muted">Page ${groupMappings.page + 1} / ${pages}</span><button type="button" class="button secondary small" data-group-page="1" ${groupMappings.page + 1 >= pages ? "disabled" : ""}>Next</button></div>`;
}
document.addEventListener("change", event => {
  if (event.target.id === "clone_scope") {
    for (const field of ["source", "destination"]) { $("#" + field).value = ""; $("#" + field + "-selection").textContent = ""; }
    resetGroupMappings(); syncCloneScope(); saveDraft(); updatePreview();
  }
  if (wholeGroupMode() && ["source", "destination"].includes(event.target.id)) resetGroupMappings();
  if (event.target.dataset.groupMap) {
    groupMappings.choices[event.target.dataset.groupMap] = event.target.value;
    $("#topic_map").value = JSON.stringify(groupMappings.choices);
    saveDraft(); updatePreview();
  }
});
document.addEventListener("input", event => {
  if (event.target.id !== "group-topic-search") return;
  const selection = event.target.selectionStart;
  groupMappings.search = event.target.value.toLowerCase(); groupMappings.page = 0;
  renderGroupMappings();
  $("#group-topic-search").focus(); $("#group-topic-search").setSelectionRange(selection, selection);
});
document.addEventListener("click", async event => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.groupPage) { groupMappings.page += Number(button.dataset.groupPage); renderGroupMappings(); }
  if (button.id !== "group-load-topics" || !wholeGroupMode()) return;
  const generation = ++groupMappings.generation;
  button.disabled = true;
  $("#group-mapping-list").textContent = "Loading source and destination topics…";
  try {
    const source = groupChatId($("#source").value), destination = groupChatId($("#destination").value);
    if (source === destination) throw new Error("Choose different source and destination groups.");
    const [sources, destinations] = await Promise.all([groupCatalog(source, generation), groupCatalog(destination, generation)]);
    if (generation !== groupMappings.generation) return;
    groupMappings.sources = sources; groupMappings.destinations = destinations;
    groupMappings.choices = JSON.parse($("#topic_map").value || "{}");
    groupMappings.page = 0; groupMappings.search = "";
    renderGroupMappings();
  } catch (error) {
    if (generation === groupMappings.generation) $("#group-mapping-list").textContent = error.message;
  } finally { if (generation === groupMappings.generation) button.disabled = false; }
});
syncCloneScope();
