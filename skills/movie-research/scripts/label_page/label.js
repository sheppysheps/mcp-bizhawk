// Labeling station page. Talks only to the server it came from; every value
// from the capture or the labels is shown with textContent, never as HTML.
"use strict";

const OPTIONS = {
  kind: ["", "door", "room load", "edge scroll", "ladder", "warp", "death / respawn", "game over",
         "cutscene", "menu", "glitch", "not a transition", "other"],
  exit: ["", "walk off an edge", "door (press up)", "ladder up", "ladder down", "crawl", "fall",
         "warp / dance", "cutscene", "death", "other"],
  entry: ["", "standing", "climbing", "crawling", "falling", "swimming", "other"],
};
const FIELDS = ["arrival", "exit_door", "entry_door", "kind", "exit", "entry", "requires", "requires_back",
                "leave_requires", "notes"];

const token = new URLSearchParams(location.search).get("token") || "";
let events = [];
let labels = { version: 1, places: {}, events: {}, items: [] };
let shown = [];
let current = 0;
let saveTimer = null;

const $ = (id) => document.getElementById(id);

/** Fill a <select> with options. Returns: nothing. */
function fillSelect(select, values) {
  for (const v of values) {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = v || "(not set)";
    select.appendChild(o);
  }
}

/** The labels of one event (created on first use). Returns: object. */
function eventLabels(ev) {
  const key = String(ev.frame);
  if (!labels.events[key]) labels.events[key] = {};
  return labels.events[key];
}

/** Is an event labeled (has a kind)? Returns: boolean. */
function isLabeled(ev) {
  const l = labels.events[String(ev.frame)];
  return Boolean(l && l.kind);
}

/** The requirement names in a Requires text ("none" and blanks dropped). Returns: array of strings. */
function requireTokens(text) {
  return (text || "").split(",").map((t) => t.trim()).filter((t) => t && t.toLowerCase() !== "none");
}

/** Draw one checkbox per vocabulary requirement, ticked when the current
 *  event's Requires text names it. Returns: nothing. */
function renderRequireBoxes() {
  const box = $("require-boxes");
  box.replaceChildren();
  const names = (labels.vocab && labels.vocab.requires) || [];
  const have = requireTokens($("requires").value);
  for (const name of names) {
    const label = document.createElement("label");
    label.className = "check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = have.includes(name);
    cb.addEventListener("change", () => {
      const ev = shown[current];
      if (!ev) return;
      let list = requireTokens($("requires").value).filter((t) => t !== name);
      if (cb.checked) list.push(name);
      $("requires").value = list.join(", ");
      eventLabels(ev).requires = $("requires").value;
      queueSave();
    });
    label.append(cb, document.createTextNode(` ${name}`));
    box.appendChild(label);
  }
}

/** Put "none" into Requires of every labeled event (one with a kind) whose
 *  Requires is still empty. Returns: nothing. */
function fillNone() {
  let n = 0;
  for (const ev of events) {
    const l = labels.events[String(ev.frame)];
    if (l && l.kind && !(l.requires || "").trim()) {
      l.requires = "none";
      n += 1;
    }
  }
  if (n) queueSave();
  render();
  $("status").textContent = n ? `"none" filled in ${n} events; saving` : "nothing to fill";
}

/** Shorten a long hex value to its ends ("77FFFFFF…0000"). Returns: string. */
function short(hex) {
  return hex.length > 16 ? `${hex.slice(0, 8)}…${hex.slice(-4)}` : hex;
}

/** Fill a <datalist> with every value used so far in the given label fields. Returns: nothing. */
function fillSuggestions(listId, fields) {
  const names = new Set();
  for (const l of Object.values(labels.events)) {
    for (const f of fields) if ((l[f] || "").trim()) names.add(l[f].trim());
  }
  const list = $(listId);
  list.replaceChildren();
  for (const n of [...names].sort()) {
    const o = document.createElement("option");
    o.value = n;
    list.appendChild(o);
  }
}

/** Offer the place and door names used so far as suggestions. Returns: nothing. */
function refreshArrivalNames() {
  fillSuggestions("arrival-names", ["arrival"]);
  fillSuggestions("door-names", ["exit_door", "entry_door"]);
}

/** Add an event at the typed frame (for transitions the list misses), save,
 *  and reload the list from the server, which adds the screenshots around it.
 *  Returns: a promise. */
async function addEvent() {
  const frame = parseInt($("add-frame").value, 10);
  if (!Number.isInteger(frame) || frame < 0) {
    $("status").textContent = "type a frame number first";
    return;
  }
  const key = String(frame);
  if (!labels.events[key]) labels.events[key] = { notes: "added by hand" };
  clearTimeout(saveTimer);
  await save();
  await loadEvents();
  const i = shown.findIndex((ev) => ev.frame === frame);
  if (i >= 0) current = i;
  render();
}

/** Short text for an event in the list: where it arrives, else place names. Returns: string. */
function summary(ev) {
  const l = labels.events[String(ev.frame)];
  if (l && l.arrival) return `→ ${l.arrival}`;
  if (!ev.changes.length) return ev.guess;
  return ev.changes.map((c) => {
    const p = labels.places[c.key];
    return `${c.source} ${p && p.name ? p.name : short(c.new_hex)}`;
  }).join(", ");
}

/** Apply the filters and redraw the list. Returns: nothing. */
function refreshList() {
  const onlyUnlabeled = $("unlabeled").checked;
  const guess = $("guess-filter").value;
  const keep = shown.length ? shown[current] : null;
  shown = events.filter((ev) => (!onlyUnlabeled || !isLabeled(ev)) && (!guess || ev.guess === guess));
  const at = keep ? shown.indexOf(keep) : -1;
  current = at >= 0 ? at : Math.min(current, Math.max(0, shown.length - 1));
  const list = $("list");
  list.replaceChildren();
  shown.forEach((ev, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = (i === current ? "current " : "") + (isLabeled(ev) ? "done" : "");
    const f = document.createElement("span");
    f.className = "frame";
    f.textContent = `f${ev.frame}`;
    const g = document.createElement("span");
    g.className = "guess";
    g.textContent = ev.guess;
    const s = document.createElement("span");
    s.className = "sum";
    s.textContent = summary(ev);
    b.append(f, g, s);
    b.addEventListener("click", () => { current = i; render(); });
    list.appendChild(b);
  });
  const sel = list.querySelector(".current");
  if (sel) sel.scrollIntoView({ block: "nearest" });
}

/** Show the screenshot `name` in an <img>, or clear it. Returns: nothing. */
function setShot(img, cap, name, label) {
  if (name) {
    img.src = `/shots/${encodeURIComponent(name)}`;
    cap.textContent = `${label}: ${name}`;
  } else {
    img.removeAttribute("src");
    cap.textContent = `${label}: none`;
  }
}

/** Draw the current event. Returns: nothing. */
function render() {
  refreshList();
  const ev = shown[current];
  $("detail").hidden = !ev;
  if (!ev) return;
  $("position").textContent =
    `Event ${current + 1} of ${shown.length} · frame ${ev.frame}` + (ev.end !== ev.frame ? `-${ev.end}` : "") +
    ` · guessed: ${ev.guess}` + (ev.lag ? ` (lag burst ${ev.lag})` : "");
  setShot($("shot-before"), $("cap-before"), ev.shot_before, "before");
  setShot($("shot-after"), $("cap-after"), ev.shot_after, "after");

  const box = $("changes");
  box.replaceChildren();
  if (!ev.changes.length) {
    const p = document.createElement("p");
    p.textContent = "No watched variable changed here: a transition without a known variable, or not a transition.";
    box.appendChild(p);
  }
  for (const c of ev.changes) {
    const row = document.createElement("label");
    row.className = "change";
    const what = document.createElement("span");
    // Records (more than 4 bytes) are saved state, not an ID: shorten them and
    // ask where they lead back to; the full value stays in the tooltip.
    const record = c.width > 4;
    what.textContent = `${c.source} ${short(c.old_hex)} → ${short(c.new_hex)}` +
      (c.pcs.length ? `  (pc ${c.pcs.join(", ")})` : "");
    what.title = `${c.old_hex} → ${c.new_hex}`;
    const input = document.createElement("input");
    const place = labels.places[c.key] || {};
    input.value = place.name || "";
    input.placeholder = record
      ? `${c.source} record (${c.width} bytes): where leaving returns to, if this was an entrance`
      : `name of ${c.source} ${c.new_hex}; leave empty if it isn't a place`;
    input.addEventListener("input", () => {
      labels.places[c.key] = Object.assign(labels.places[c.key] || {}, { name: input.value });
      queueSave();
    });
    row.append(what, input);
    box.appendChild(row);
  }
  const l = labels.events[String(ev.frame)] || {};
  for (const f of FIELDS) $(f).value = l[f] || "";
  renderRequireBoxes();
  refreshArrivalNames();
}

/** Save the labels soon (debounced). Returns: nothing. */
function queueSave() {
  $("status").textContent = "unsaved";
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, 500);
}

/** Send the labels to the server. Returns: a promise. */
async function save() {
  try {
    const r = await fetch(`/api/labels?token=${encodeURIComponent(token)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(labels),
    });
    $("status").textContent = r.ok ? "saved" : `NOT SAVED (${r.status}): reopen the URL the server printed`;
    document.body.classList.toggle("save-failed", !r.ok);
  } catch (err) {
    $("status").textContent = "NOT SAVED: the server stopped. Restart it and open the new URL";
    document.body.classList.add("save-failed");
  }
}

/** Load events and labels, wire up the controls. Returns: a promise. */
async function start() {
  for (const f of ["kind", "exit", "entry"]) fillSelect($(f), OPTIONS[f]);
  for (const f of FIELDS) {
    $(f).addEventListener("input", () => {
      const ev = shown[current];
      if (!ev) return;
      eventLabels(ev)[f] = $(f).value;
      queueSave();
      if (f === "kind" || f === "arrival") refreshList();
      if (f === "requires") renderRequireBoxes();
    });
  }
  $("fill-none").addEventListener("click", fillNone);
  $("add-event").addEventListener("click", addEvent);
  $("unlabeled").addEventListener("change", render);
  $("guess-filter").addEventListener("change", () => { current = 0; render(); });
  document.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
    // The right-hand key goes forward: k next, j previous.
    if (e.key === "k" && current < shown.length - 1) { current += 1; render(); }
    if (e.key === "j" && current > 0) { current -= 1; render(); }
  });

  if (!(await loadEvents())) return;
  fillSelect($("guess-filter"), [...new Set(events.map((e) => e.guess))].sort());
  $("guess-filter").firstChild.textContent = "all methods";
  $("status").textContent = `${events.length} events`;
  render();
}

/** Fetch events and labels from the server. Returns: a promise of true when loaded. */
async function loadEvents() {
  const r = await fetch(`/api/events?token=${encodeURIComponent(token)}`);
  if (!r.ok) {
    $("status").textContent = r.status === 403 ? "open the URL the server printed (it has the token)" : `error ${r.status}`;
    return false;
  }
  const data = await r.json();
  events = data.events;
  labels = Object.assign({ version: 1, places: {}, events: {}, items: [] }, data.labels);
  $("folder").textContent = data.folder;
  shown = [];
  return true;
}

start();
