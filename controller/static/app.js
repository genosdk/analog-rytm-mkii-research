"use strict";

const LANES = 8;
const DEFAULT_VALUE = 64;
const NOTE_KEYS = { a:48, w:49, s:50, e:51, d:52, f:53, t:54, g:55, y:56, h:57, u:58, j:59, k:60 };
const BLACK_KEYS = new Set(["w", "e", "t", "y", "u"]);
const state = { values: Array(LANES).fill(DEFAULT_VALUE), selectedLane: 0, held: new Set(), online: false };
let filterQueue = Promise.resolve();
let noteQueue = Promise.resolve();
let filterRevision = 0;
let noteRevision = 0;

const clamp = value => Math.max(0, Math.min(127, Math.round(value)));
const angleFor = value => -135 + (value / 127) * 270;

function knobMarkup(lane) {
  return `
    <div class="knob-channel" data-lane="${lane}">
      <button class="knob" type="button" role="slider" aria-label="Filter 2 lane ${lane + 1}"
        aria-valuemin="0" aria-valuemax="127" aria-valuenow="64" aria-orientation="vertical" data-lane="${lane}">
        <svg viewBox="0 0 120 120" aria-hidden="true">
          <circle class="knob-track" cx="60" cy="60" r="47"></circle>
          <circle class="knob-body" cx="60" cy="60" r="39"></circle>
          <g class="knob-pointer" transform="rotate(1.06 60 60)">
            <line x1="60" y1="60" x2="60" y2="27"></line>
            <circle cx="60" cy="27" r="3"></circle>
          </g>
        </svg>
      </button>
      <div class="channel-meta"><span>F2 ${lane + 1}</span><output>064</output></div>
      <span class="address">0x${(0x7ff8 + lane).toString(16).toUpperCase()}</span>
    </div>`;
}

function pianoMarkup() {
  return Object.keys(NOTE_KEYS).map(key => `
    <button class="piano-key ${BLACK_KEYS.has(key) ? "black" : "white"}" type="button"
      data-note-key="${key}" aria-label="Note ${NOTE_KEYS[key]} on keyboard key ${key.toUpperCase()}">
      <span>${key.toUpperCase()}</span><small>${NOTE_KEYS[key]}</small>
    </button>`).join("");
}

function renderKnob(lane) {
  const channel = document.querySelector(`.knob-channel[data-lane="${lane}"]`);
  const button = channel.querySelector(".knob");
  const value = state.values[lane];
  channel.classList.toggle("selected", lane === state.selectedLane);
  button.setAttribute("aria-valuenow", value);
  button.querySelector(".knob-pointer").setAttribute("transform", `rotate(${angleFor(value)} 60 60)`);
  channel.querySelector("output").textContent = String(value).padStart(3, "0");
  if (lane === state.selectedLane) {
    document.querySelector("#active-readout").textContent = `F2 ${lane + 1} · ${String(value).padStart(3, "0")}`;
    document.querySelector("#publication-readout").textContent = `0x${(0x7ff8 + lane).toString(16).toUpperCase()}`;
  }
}

function renderNotes() {
  document.querySelectorAll("[data-note-key]").forEach(key => {
    key.classList.toggle("active", state.held.has(NOTE_KEYS[key.dataset.noteKey]));
  });
  const held = [...state.held].sort((a, b) => a - b);
  document.querySelector("#note-readout").textContent = held.length ? `Held: ${held.join(", ")}` : "No notes held";
}

function setConnection(online, label) {
  state.online = online;
  const node = document.querySelector("#connection");
  node.classList.toggle("online", online);
  node.querySelector("span:last-child").textContent = label;
}

async function request(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function publishFilter(lane, value, source) {
  value = clamp(value);
  const revision = ++filterRevision;
  state.selectedLane = lane;
  state.values[lane] = value;
  for (let item = 0; item < LANES; item += 1) renderKnob(item);
  filterQueue = filterQueue.then(async () => {
    try {
      const body = await request("/api/filter2", { lane, value, source });
      if (revision === filterRevision) {
        state.values = body.state.filter2.values;
        state.selectedLane = body.state.filter2.selected_lane;
        for (let item = 0; item < LANES; item += 1) renderKnob(item);
      }
      setConnection(true, "Emulator connected");
    } catch (error) {
      setConnection(false, "Publication failed");
      console.error(error);
    }
  });
  return filterQueue;
}

function publishNote(key, action) {
  const note = NOTE_KEYS[key];
  const revision = ++noteRevision;
  if (action === "on") state.held.add(note); else state.held.delete(note);
  renderNotes();
  noteQueue = noteQueue.then(async () => {
    try {
      const body = await request("/api/note", { key, action, velocity: 100 });
      if (revision === noteRevision) {
        state.held = new Set(body.state.notes.held);
        renderNotes();
      }
    } catch (error) {
      setConnection(false, "Note event failed");
      console.error(error);
    }
  });
  return noteQueue;
}

function bindKnobs() {
  document.querySelectorAll(".knob").forEach(knob => {
    const lane = Number(knob.dataset.lane);
    let drag = null;
    knob.addEventListener("pointerdown", event => {
      drag = { y: event.clientY, value: state.values[lane] };
      state.selectedLane = lane;
      knob.setPointerCapture(event.pointerId);
      renderKnob(lane);
    });
    knob.addEventListener("pointermove", event => {
      if (!drag) return;
      const scale = event.shiftKey ? 0.2 : 0.65;
      const next = clamp(drag.value + (drag.y - event.clientY) * scale);
      if (next !== state.values[lane]) publishFilter(lane, next, "mouse");
    });
    knob.addEventListener("pointerup", () => { drag = null; });
    knob.addEventListener("pointercancel", () => { drag = null; });
    knob.addEventListener("wheel", event => {
      event.preventDefault();
      publishFilter(lane, state.values[lane] + (event.deltaY < 0 ? 1 : -1), "wheel");
    }, { passive: false });
    knob.addEventListener("dblclick", () => publishFilter(lane, DEFAULT_VALUE, "mouse"));
    knob.addEventListener("keydown", event => {
      const delta = { ArrowUp:1, ArrowRight:1, ArrowDown:-1, ArrowLeft:-1, PageUp:8, PageDown:-8 }[event.key];
      if (delta !== undefined) {
        event.preventDefault();
        publishFilter(lane, state.values[lane] + delta, "keyboard");
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        publishFilter(lane, event.key === "Home" ? 0 : 127, "keyboard");
      }
    });
  });
}

function bindNotes() {
  const down = new Set();
  window.addEventListener("keydown", event => {
    const key = event.key.toLowerCase();
    if (NOTE_KEYS[key] === undefined || down.has(key) || event.metaKey || event.ctrlKey || event.altKey) return;
    if (["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    event.preventDefault();
    down.add(key);
    publishNote(key, "on");
  });
  window.addEventListener("keyup", event => {
    const key = event.key.toLowerCase();
    if (!down.has(key)) return;
    event.preventDefault();
    down.delete(key);
    publishNote(key, "off");
  });
  window.addEventListener("blur", () => {
    for (const key of down) publishNote(key, "off");
    down.clear();
  });
  document.querySelectorAll("[data-note-key]").forEach(key => {
    const value = key.dataset.noteKey;
    key.addEventListener("pointerdown", event => {
      key.setPointerCapture(event.pointerId);
      publishNote(value, "on");
    });
    key.addEventListener("pointerup", () => publishNote(value, "off"));
    key.addEventListener("pointercancel", () => publishNote(value, "off"));
  });
}

async function initialize() {
  document.querySelector("#knob-grid").innerHTML = Array.from({ length: LANES }, (_, lane) => knobMarkup(lane)).join("");
  document.querySelector("#piano").innerHTML = pianoMarkup();
  bindKnobs();
  bindNotes();
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const body = await response.json();
    state.values = body.filter2.values;
    state.selectedLane = body.filter2.selected_lane;
    state.held = new Set(body.notes.held);
    for (let lane = 0; lane < LANES; lane += 1) renderKnob(lane);
    renderNotes();
    setConnection(true, "Emulator connected");
  } catch (error) {
    setConnection(false, "Service unavailable");
    console.error(error);
  }
}

initialize();
