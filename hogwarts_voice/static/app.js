"use strict";

/* ============================== helpers ============================== */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null) el.append(c instanceof Node ? c : String(c));
  return el;
}

const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem("sonorus." + key); return v == null ? fallback : JSON.parse(v); }
    catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem("sonorus." + key, JSON.stringify(value)); } catch { /* private mode */ } },
};

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw new Error((body && body.detail && (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail))) || `${res.status} ${res.statusText}`);
  return body;
}

function toast(msg, kind = "ok", ms = 4200) {
  const t = h("div", { class: `toast ${kind}` }, msg);
  $("#toasts").append(t);
  setTimeout(() => { t.classList.add("leaving"); setTimeout(() => t.remove(), 300); }, ms);
}

const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const shellQuote = (s) => /^[\w@%+=:,./-]+$/.test(s) ? s : `'${String(s).replace(/'/g, `'\\''`)}'`;
const ago = (iso) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
};

/* ============================== state ============================== */
const SAMPLES = [
  "Welcome, first-years! Mind the moving staircases, and never, ever feed the portraits after midnight.",
  "[laughter] You thought the Whomping Willow was being friendly? Oh, you sweet, innocent first-year.",
  "Ten points to whoever can tell me why this cauldron is singing. [sigh] Anyone? Anyone at all?",
  "The owl arrived at dawn, carrying a letter sealed with emerald wax and smelling faintly of peppermint.",
  "Attention, students. Today's Quidditch match has been postponed, on account of a rather enthusiastic dragon.",
  "Turn to page three hundred and ninety-four. And do try not to set your eyebrows on fire this time.",
  "Somewhere beneath the castle, a door that was not there yesterday creaked slowly open.",
];
const BREW_LINES = [
  "Stirring clockwise, seven times…", "Adding a pinch of powdered moonstone…", "Consulting the portraits…",
  "Polishing the vocal cords…", "Asking the Fat Lady for a high note…", "Letting it simmer…",
];

const S = {
  options: null,
  status: null,
  engine: store.get("engine", "omnivoice"),
  mode: store.get("mode", "clone"),
  source: store.get("source", "vault"),
  voice: store.get("voice", null),
  voices: [],
  ref: null, // { wav: Blob, samples: Float32Array, duration, name }
  design: {},
  params: {}, // knobs changed from the current engine's defaults
  history: [],
  last: null, // { item, request }
};

/* ============================== house theme ============================== */
function setHouse(house) {
  document.documentElement.dataset.house = house;
  $$(".house-btn").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.house === house)));
  store.set("house", house);
  if (S.last) drawWave();
  if (S.ref) drawMini();
}
$$(".house-btn").forEach((b) => b.addEventListener("click", () => {
  setHouse(b.dataset.house);
  toast(`${b.title}! You've been sorted.`, "ok", 1800);
}));

/* ============================== status ============================== */
let statusTimer;
async function pollStatus() {
  clearTimeout(statusTimer);
  let st;
  try { st = await api("/api/status"); }
  catch { st = { state: "offline", message: "The server is unreachable. Is `sonorus serve` running?" }; }
  S.status = st;
  renderStatus();
  statusTimer = setTimeout(pollStatus, engineStatus().state === "ready" ? 8000 : 1500);
}

/** Status of the selected engine; the server reports one entry per engine. */
function engineStatus() {
  const st = S.status;
  if (!st || st.state === "offline") return st || { state: "offline", message: "" };
  return { ...((st.engines || {})[S.engine] || st), busy: st.busy, device: st.device, dtype: st.dtype };
}

function renderStatus() {
  const st = engineStatus();
  const el = $("#status");
  const state = st.state === "ready" && st.busy ? "busy" : st.state;
  el.dataset.state = state;
  const text = {
    ready: S.engine === "fish" ? `Fish Speech ready · ${st.model}` : `Wand ready · ${st.device} · ${st.dtype}`,
    busy: "Casting a spell…",
  }[state] || st.message;
  $("#status-text").textContent = text;
  el.title = st.message || "";
  updateCast();
}

function updateCast() {
  const state = engineStatus().state;
  // "idle" = server started with --lazy: the first spell loads the model.
  const usable = state === "ready" || state === "idle";
  const btn = $("#cast");
  btn.disabled = !usable || S.casting;
  $(".cast-label", btn).textContent = S.casting ? "Casting…" : usable ? "Sonorus!" : state === "error" ? "Wand broken" : state === "offline" ? "No wand found" : state === "disabled" ? "Wand not set up" : "Waking the wand…";
  $(".cast-sub", btn).textContent = {
    ready: "Ctrl + Enter", idle: "Ctrl + Enter · the first spell wakes the model",
    error: "see the status above", offline: "is `sonorus serve` running?", disabled: "pick the other engine at the top",
  }[state] || "model is loading, hang tight";
}

/* ============================== engines ============================== */
const engineInfo = (id = S.engine) => S.options.engines.find((e) => e.id === id);
const engineLabel = (id) => (S.options && engineInfo(id || "omnivoice")?.label) || id;
// Changed knobs are remembered per engine ("params" alone is from before Fish Speech existed).
const loadParams = (id) => store.get(`params.${id}`, id === "omnivoice" ? store.get("params", {}) : {});
const saveParams = () => store.set(`params.${S.engine}`, S.params);
const TEXT_HINTS = {
  omnivoice: "Type what the voice should say…  Use [laughter] or [sigh] for sounds, and [B EY1 S] (CMU phonemes) or pinyin like ZHE2 to fix pronunciation.",
  fish: "Type what the voice should say…  Steer the delivery with tags like [whisper], [excited] or [pause] anywhere in the text.",
};

function buildEngines() {
  $("#engines").replaceChildren(...S.options.engines.map((e) => h("button", {
    class: "engine-btn", role: "radio", "data-engine": e.id, disabled: !e.available,
    title: e.available ? `Cast with ${e.label}` : `${e.label} isn't set up on this server (SONORUS_FISH_URL)`,
    onclick: () => { if (e.id !== S.engine) { setEngine(e.id); toast(`${e.label} is now your wand core.`, "ok", 1800); } },
  }, e.label)));
}

function setEngine(id) {
  if (!engineInfo(id)?.available) id = "omnivoice";
  S.engine = id;
  store.set("engine", id);
  const eng = engineInfo();
  $$(".engine-btn").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.engine === id)));
  $$("[data-feature]").forEach((el) => (el.hidden = !eng.features[el.dataset.feature]));
  if (!eng.features.design && S.mode === "design") setMode("auto");
  $("#tags").replaceChildren(...eng.nonverbal.map((t) => h("button", { class: "tag", title: t.tag, onclick: () => insertAtCursor(t.tag) }, t.label)));

  const norm = eng.features.normalize;
  $("#normalize").disabled = !norm;
  $("#normalize").checked = norm && store.get("normalize", false);
  $("#normalize-wrap").classList.toggle("disabled", !norm);
  $("#normalize-wrap").title = norm ? "" : id === "fish" ? "Fish Speech reads numbers on its own" : "Install the extra to enable: pip install 'omnivoice[tn]'";
  textEl.placeholder = TEXT_HINTS[id] || TEXT_HINTS.omnivoice;

  $("#ref-range").textContent = eng.ref_seconds.join("–");
  if (S.ref) setRefWarn();
  $("#restricted-sub").textContent = `every ${eng.label} knob`;
  $("#reset-params").textContent = `Reset to ${eng.label} defaults`;
  S.params = loadParams(id);
  buildParams();
  if (S.status) renderStatus();
}

/* ============================== modes ============================== */
const MODES = ["clone", "design", "auto"];
function setMode(mode) {
  if (mode === "design" && S.options && !engineInfo().features.design) mode = "auto";
  S.mode = mode;
  store.set("mode", mode);
  history.replaceState(null, "", `#${mode}`); // deep-linkable: /#design
  $$(".mode").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mode === mode)));
  $$("[data-panel]").forEach((p) => (p.hidden = p.dataset.panel !== mode));
}
$$(".mode").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));

function setSource(src) {
  S.source = src;
  store.set("source", src);
  $$("[data-source]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.source === src)));
  $$("[data-source-panel]").forEach((p) => (p.hidden = p.dataset.sourcePanel !== src));
}
$$("[data-source]").forEach((b) => b.addEventListener("click", () => setSource(b.dataset.source)));
document.addEventListener("click", (e) => {
  const a = e.target.closest("[data-goto]");
  if (a) { e.preventDefault(); setSource(a.dataset.goto); }
});

/* ============================== vault ============================== */
const sampleAudio = new Audio();

async function loadVoices() {
  try { S.voices = await api("/api/voices"); } catch { S.voices = []; }
  renderVoices();
}

function renderVoices() {
  const box = $("#vault");
  box.replaceChildren();
  $("#vault-empty").hidden = S.voices.length > 0;
  if (S.voice && !S.voices.some((v) => v.id === S.voice)) S.voice = null;
  if (!S.voice && S.voices.length) S.voice = S.voices[0].id;
  for (const v of S.voices) {
    const card = h("div", {
      class: "voice", role: "button", tabindex: "0", "aria-selected": String(v.id === S.voice), title: v.description || v.name,
      onclick: () => selectVoice(v.id),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectVoice(v.id); } },
    },
    h("b", {}, v.name),
    h("p", {}, v.description ? v.description : `“${v.ref_text}”`),
    h("div", { class: "voice-actions" },
      h("button", { title: "Hear the original clip", onclick: (e) => { e.stopPropagation(); playSample(v.id); } }, "▶"),
      h("button", { title: "Remove from vault", onclick: (e) => { e.stopPropagation(); forgetVoice(v); } }, "🗑")));
    box.append(card);
  }
}

function selectVoice(id) {
  S.voice = id;
  store.set("voice", id);
  renderVoices();
}

function playSample(id) {
  sampleAudio.src = `/api/voices/${encodeURIComponent(id)}/sample`;
  sampleAudio.play().catch(() => toast("No stored clip for this voice.", "error"));
}

async function forgetVoice(v) {
  if (!confirm(`Obliviate “${v.name}” from the vault? This cannot be undone.`)) return;
  try {
    await api(`/api/voices/${encodeURIComponent(v.id)}`, { method: "DELETE" });
    toast(`“${v.name}” has been obliviated.`);
    loadVoices();
  } catch (e) { toast(e.message, "error"); }
}

/* ============================== reference clip ============================== */
function encodeWav(samples, sr) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const w = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  w(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); w(8, "WAVE"); w(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, sr, true);
  v.setUint32(28, sr * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data");
  v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

/** Decode anything the browser can play, downmix to mono 44.1 kHz WAV. No ffmpeg needed server-side.
 *  OmniVoice resamples to 24 kHz itself; Fish Speech's codec works at 44.1 kHz, so keep that much detail. */
async function toWav(blob) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  let decoded;
  try { decoded = await ctx.decodeAudioData(await blob.arrayBuffer()); }
  finally { ctx.close(); }
  const sr = 44100;
  const off = new OfflineAudioContext(1, Math.max(1, Math.ceil(decoded.duration * sr)), sr);
  const src = off.createBufferSource();
  src.buffer = decoded;
  src.connect(off.destination);
  src.start();
  const out = await off.startRendering();
  const samples = out.getChannelData(0);
  return { wav: encodeWav(samples, sr), samples, duration: out.duration };
}

async function setRef(blob, name) {
  try {
    const r = await toWav(blob);
    S.ref = { ...r, name };
  } catch {
    toast("Couldn't read that audio. Try a WAV, MP3, M4A or OGG file.", "error");
    return;
  }
  $(".drop-empty").hidden = true;
  $(".drop-full").hidden = false;
  $("#ref-info").textContent = `${name} · ${S.ref.duration.toFixed(1)} s`;
  setRefWarn();
  drawMini();
}

function setRefWarn() {
  const [lo, hi] = S.options ? engineInfo().ref_seconds : [3, 10];
  const d = S.ref.duration;
  const warn = $("#ref-warn");
  // OmniVoice copes with a bit over its range; Fish's Whisper transcript and cloning both prefer staying within it.
  warn.hidden = d >= lo - 0.5 && d <= (S.engine === "fish" ? hi : hi * 1.5);
  warn.textContent = d < lo - 0.5
    ? `That's a very short sample. ${lo}–${hi} seconds of clear speech clones much better.`
    : `That's a long sample. ${lo}–${hi} seconds works best; longer clips are slower and can sound worse.`
      + (S.engine === "omnivoice" ? " Clips over 20 s get trimmed when no transcript is given." : "");
}

function clearRef() {
  S.ref = null;
  $(".drop-empty").hidden = false;
  $(".drop-full").hidden = true;
  $("#ref-warn").hidden = true;
  $("#ref-file").value = "";
}

function drawMini() {
  if (!S.ref) return;
  drawPeaks($("#ref-wave"), S.ref.samples, 0);
}

const drop = $("#drop");
$("#pick-file").addEventListener("click", () => $("#ref-file").click());
$("#ref-file").addEventListener("change", (e) => { const f = e.target.files[0]; if (f) setRef(f, f.name); });
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) setRef(f, f.name);
});
$("#ref-clear").addEventListener("click", clearRef);
$("#ref-play").addEventListener("click", () => {
  if (!S.ref) return;
  sampleAudio.src = URL.createObjectURL(S.ref.wav);
  sampleAudio.play();
});

let recorder = null;
let recordTimer = null;
$("#record").addEventListener("click", async () => {
  const btn = $("#record");
  if (recorder) { recorder.stop(); return; }
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
  catch { toast("Microphone access was refused. Allow it in the browser to record.", "error"); return; }
  const chunks = [];
  recorder = new MediaRecorder(stream);
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  recorder.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    clearInterval(recordTimer);
    recorder = null;
    btn.classList.remove("recording");
    $("span:last-child", btn).textContent = "Record";
    await setRef(new Blob(chunks, { type: chunks[0]?.type || "audio/webm" }), "Recording");
  };
  recorder.start();
  btn.classList.add("recording");
  const started = Date.now();
  const tick = () => {
    const s = (Date.now() - started) / 1000;
    $("span:last-child", btn).textContent = `Stop · ${s.toFixed(0)}s`;
    const cap = S.options ? Math.max(15, engineInfo().ref_seconds[1]) : 15;
    if (s >= cap && recorder) recorder.stop(); // keep clips short
  };
  tick();
  recordTimer = setInterval(tick, 250);
  toast("Recording… read a sentence or two naturally, then press Stop.", "ok", 3000);
});

$("#save-voice").addEventListener("change", (e) => {
  $("#voice-name").disabled = !e.target.checked;
  if (e.target.checked) $("#voice-name").focus();
});

async function storeVoice(name) {
  const fd = new FormData();
  fd.append("name", name);
  fd.append("ref_audio", S.ref.wav, "reference.wav");
  fd.append("ref_text", $("#ref-text").value.trim());
  // Vault voices are always prepared by OmniVoice (it also transcribes), whichever engine is selected.
  fd.append("preprocess", String(paramValue("preprocess_prompt", "omnivoice")));
  const meta = await api("/api/voices", { method: "POST", body: fd });
  await loadVoices();
  selectVoice(meta.id);
  toast(`“${meta.name}” is safe in your Gringotts vault.`);
  return meta;
}

/* ============================== design ============================== */
function buildDesign() {
  const box = $("#attrs");
  for (const [cat, values] of Object.entries(S.options.design)) {
    const chips = values.map((v) => h("button", {
      class: "chip", "aria-pressed": "false", "data-cat": cat, "data-val": v,
      onclick: () => {
        S.design[cat] = S.design[cat] === v ? undefined : v;
        $("#instruct").value = Object.values(S.design).filter(Boolean).join(", ");
        store.set("instruct", $("#instruct").value);
        syncChips();
      },
    }, v));
    box.append(h("div", { class: "attr-group" }, h("h4", {}, cat), h("div", { class: "chips" }, chips)));
  }
}

function syncChips() {
  $$(".chip").forEach((c) => c.setAttribute("aria-pressed", String(S.design[c.dataset.cat] === c.dataset.val)));
}

function parseInstruct(str) {
  const parts = str.split(/[,，]/).map((s) => s.trim().toLowerCase()).filter(Boolean);
  S.design = {};
  for (const [cat, values] of Object.entries(S.options.design)) {
    const hit = values.find((v) => parts.includes(v.toLowerCase()));
    if (hit) S.design[cat] = hit;
  }
  syncChips();
}
$("#instruct").addEventListener("input", (e) => { parseInstruct(e.target.value); store.set("instruct", e.target.value); });
$("#clone-instruct").addEventListener("input", (e) => store.set("cloneInstruct", e.target.value));

/* ============================== text ============================== */
const textEl = $("#text");
function updateCount() {
  const n = textEl.value.length;
  const est = Math.max(1, Math.round(textEl.value.replace(/\[[^\]]*\]/g, "").length / 14));
  $("#text-count").textContent = n ? `${n} characters · roughly ${est}s of speech` : "0 characters";
  store.set("text", textEl.value);
}
textEl.addEventListener("input", updateCount);

function insertAtCursor(snippet) {
  const { selectionStart: a, selectionEnd: b, value } = textEl;
  const before = value.slice(0, a);
  const pad = before && !/\s$/.test(before) ? " " : "";
  const ins = `${pad}${snippet} `;
  textEl.value = before + ins + value.slice(b);
  textEl.focus();
  textEl.selectionStart = textEl.selectionEnd = a + ins.length;
  updateCount();
}

$("#sample").addEventListener("click", () => {
  const pool = SAMPLES.filter((s) => s !== textEl.value);
  textEl.value = pool[Math.floor(Math.random() * pool.length)];
  updateCount();
});

function resolveLanguage() {
  const raw = $("#lang").value.trim();
  if (!raw) return null;
  const m = raw.match(/\(([^)]+)\)\s*$/);
  const langs = S.options.languages;
  if (m && langs.some((l) => l.id === m[1])) return m[1];
  const low = raw.toLowerCase();
  const hit = langs.find((l) => l.id === low || l.name.toLowerCase() === low);
  if (hit) return hit.id;
  throw new Error(`“${raw}” isn't a language I know. Pick one from the list, or leave it blank to auto-detect.`);
}
$("#lang").addEventListener("change", (e) => store.set("lang", e.target.value));

/* ============================== settings ============================== */
let speedSet = store.get("speedSet", false);
function renderSpeed() {
  $("#speed-out").textContent = speedSet ? `${Number($("#speed").value).toFixed(2)}×` : "auto";
  store.set("speedSet", speedSet);
  store.set("speed", $("#speed").value);
}
$("#speed").addEventListener("input", () => { speedSet = true; renderSpeed(); });
$("#speed-reset").addEventListener("click", () => { speedSet = false; $("#speed").value = 1; renderSpeed(); });

function bindToggle(checkId, inputId, key) {
  const c = $(checkId), i = $(inputId);
  const sync = () => { i.disabled = !c.checked; store.set(key, { on: c.checked, value: i.value }); };
  c.addEventListener("change", sync);
  i.addEventListener("input", sync);
  const saved = store.get(key, null);
  if (saved) { c.checked = saved.on; i.value = saved.value; }
  i.disabled = !c.checked;
}
bindToggle("#use-duration", "#duration", "duration");
bindToggle("#use-seed", "#seed", "seed");
$("#dice").addEventListener("click", () => {
  $("#seed").value = Math.floor(Math.random() * 2 ** 31);
  $("#use-seed").checked = true;
  $("#use-seed").dispatchEvent(new Event("change"));
});
$("#normalize").addEventListener("change", (e) => store.set("normalize", e.target.checked));
$("#format").addEventListener("change", (e) => store.set("format", e.target.value));

const paramDefault = (key, id = S.engine) => engineInfo(id).params.find((p) => p.key === key).default;
function paramValue(key, id = S.engine) {
  const changed = id === S.engine ? S.params : loadParams(id);
  return key in changed ? changed[key] : paramDefault(key, id);
}

function setParam(key, value) {
  if (value === paramDefault(key)) delete S.params[key];
  else S.params[key] = value;
  saveParams();
  syncParams();
}

function buildParams() {
  const box = $("#params");
  box.replaceChildren();
  const groups = {};
  for (const p of engineInfo().params) (groups[p.group] ||= []).push(p);
  for (const [group, list] of Object.entries(groups)) {
    const g = h("div", { class: "param-group" }, h("h4", {}, group));
    for (const p of list) {
      const id = `p-${p.key}`;
      let control;
      if (p.type === "bool") {
        control = h("div", { class: "row between" },
          h("label", { class: "check", for: id }, h("input", { type: "checkbox", id, "data-key": p.key,
            onchange: (e) => setParam(p.key, e.target.checked) }), p.label));
      } else {
        const cast = (v) => (p.type === "int" ? parseInt(v, 10) : parseFloat(v));
        const onInput = (e) => { const v = cast(e.target.value); if (!Number.isNaN(v)) setParam(p.key, v); };
        control = h("div", {},
          h("div", { class: "row between" },
            h("label", { for: id }, p.label),
            h("input", { class: "input num-in", type: "number", min: p.min, max: p.max, step: p.step, "data-key": p.key, "data-role": "num", onchange: onInput })),
          h("input", { type: "range", id, min: p.min, max: p.max, step: p.step, "data-key": p.key, oninput: onInput }));
      }
      g.append(h("div", { class: "param", "data-param": p.key }, control, h("p", {}, p.help)));
    }
    box.append(g);
  }
  syncParams();
}

function syncParams() {
  for (const p of engineInfo().params) {
    const v = paramValue(p.key);
    $$(`[data-key="${p.key}"]`).forEach((el) => {
      if (el.type === "checkbox") el.checked = v;
      else if (document.activeElement !== el) el.value = v;
    });
    $(`[data-param="${p.key}"]`).classList.toggle("changed", p.key in S.params);
  }
  if (!engineInfo().features.presets) return;
  $$("[data-preset]").forEach((b) => b.setAttribute("aria-selected", String(Number(b.dataset.preset) === paramValue("num_step"))));
}

$$("[data-preset]").forEach((b) => b.addEventListener("click", () => setParam("num_step", Number(b.dataset.preset))));
$("#reset-params").addEventListener("click", () => { S.params = {}; saveParams(); syncParams(); toast(`Restricted Section restored to ${engineLabel(S.engine)}'s defaults.`, "ok", 2000); });

/* ============================== casting ============================== */
function collectRequest() {
  const text = textEl.value.trim();
  if (!text) throw new Error("The incantation is empty. Write something for the voice to say.");
  const f = engineInfo().features;
  const req = { text, engine: S.engine, params: { ...S.params }, format: $("#format").value, normalize_text: f.normalize && $("#normalize").checked };
  const lang = f.language ? resolveLanguage() : null;
  if (lang) req.language = lang;
  if (f.duration && $("#use-duration").checked) req.duration = parseFloat($("#duration").value);
  else if (f.speed && speedSet) req.speed = parseFloat($("#speed").value);
  if ($("#use-seed").checked) req.seed = parseInt($("#seed").value, 10);

  if (S.mode === "clone") {
    const hints = f.style_hints ? $("#clone-instruct").value.trim() : "";
    if (hints) req.instruct = hints;
    if (S.source === "vault") {
      if (!S.voice) throw new Error(S.voices.length ? "Pick a voice from your vault first." : "Your vault is empty. Add a fresh sample to clone a voice.");
      req.voice = S.voice;
    } else {
      if (!S.ref) throw new Error("Add a voice sample first: drop a clip or record one.");
      req.fresh = true;
      req.ref_text = $("#ref-text").value.trim();
    }
  } else if (S.mode === "design") {
    const instruct = $("#instruct").value.trim();
    if (!instruct) throw new Error("Choose at least one attribute, or switch to the Sorting Hat.");
    req.instruct = instruct;
  }
  return req;
}

function sparkle(el) {
  const r = el.getBoundingClientRect();
  for (let i = 0; i < 16; i++) {
    const s = h("i", { class: "spark" });
    const ang = Math.random() * Math.PI * 2, dist = 60 + Math.random() * 90;
    s.style.left = `${r.left + r.width / 2}px`;
    s.style.top = `${r.top + r.height / 2}px`;
    s.style.setProperty("--dx", `${Math.cos(ang) * dist}px`);
    s.style.setProperty("--dy", `${Math.sin(ang) * dist}px`);
    document.body.append(s);
    setTimeout(() => s.remove(), 950);
  }
}

let brewTimer;
function brewing(on, label) {
  const o = $("#brewing");
  clearInterval(brewTimer);
  o.hidden = !on;
  if (!on) return;
  const started = Date.now();
  let line = 0;
  $("#brewing-text").textContent = label || BREW_LINES[0];
  brewTimer = setInterval(() => {
    const s = (Date.now() - started) / 1000;
    $("#brewing-time").textContent = `${s.toFixed(1)}s`;
    if (!label && Math.floor(s / 3) !== line) { line = Math.floor(s / 3); $("#brewing-text").textContent = BREW_LINES[line % BREW_LINES.length]; }
  }, 100);
}

async function cast() {
  if ($("#cast").disabled) return;
  let req;
  try { req = collectRequest(); } catch (e) { toast(e.message, "error"); return; }
  sparkle($("#cast"));
  S.casting = true;
  updateCast();
  try {
    // Fresh sample + "keep in vault": store first, then speak with the saved voice.
    if (req.fresh && $("#save-voice").checked) {
      const name = $("#voice-name").value.trim();
      if (!name) throw new Error("Give the voice a name to keep it in the vault.");
      brewing(true, "Brewing Polyjuice Potion…");
      const meta = await storeVoice(name);
      $("#save-voice").checked = false;
      $("#voice-name").value = "";
      $("#voice-name").disabled = true;
      clearRef();
      setSource("vault");
      delete req.fresh; delete req.ref_text;
      req.voice = meta.id;
    }
    brewing(true, engineStatus().state === "idle" ? "Waking the wand: the first spell also loads the model…" : undefined);
    const fd = new FormData();
    for (const [k, v] of Object.entries(req)) {
      if (k === "params") fd.append("params", JSON.stringify(v));
      else if (k !== "fresh") fd.append(k, String(v));
    }
    if (req.fresh) fd.append("ref_audio", S.ref.wav, "reference.wav");
    const item = await speakJob(fd);
    S.last = { item, request: req, ref: req.fresh ? S.ref : null };
    showResult();
    loadHistory();
  } catch (e) {
    toast(e.message, "error", 7000);
  } finally {
    S.casting = false;
    brewing(false);
    updateCast();
    pollStatus();
  }
}
/** Start a speak job and poll it: proxies such as RunPod's cut requests that run past ~100 s. */
async function speakJob(fd) {
  let job = await api("/api/speak?wait=false", { method: "POST", body: fd });
  while (job.state === "running") {
    await new Promise((r) => setTimeout(r, 1000));
    job = await api(`/api/jobs/${job.job}`);
  }
  if (job.state === "error") throw new Error(job.detail);
  return job.item;
}
$("#cast").addEventListener("click", cast);
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); cast(); }
});

/* ============================== result player ============================== */
const audio = $("#audio");
let peaks = null;

function drawPeaks(canvas, samples, progress) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, hgt = canvas.clientHeight;
  if (!w) return;
  canvas.width = w * dpr; canvas.height = hgt * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement);
  const played = css.getPropertyValue("--house").trim();
  const rest = "rgba(90,62,25,.35)";
  const bar = 3, gap = 2, n = Math.floor(w / (bar + gap));
  const step = Math.max(1, Math.floor(samples.length / n));
  let max = 0.01;
  const vals = [];
  for (let i = 0; i < n; i++) {
    let m = 0;
    for (let j = i * step; j < Math.min((i + 1) * step, samples.length); j += 4) m = Math.max(m, Math.abs(samples[j]));
    vals.push(m); max = Math.max(max, m);
  }
  vals.forEach((m, i) => {
    const bh = Math.max(2, (m / max) * (hgt - 6));
    ctx.fillStyle = i / n < progress ? played : rest;
    ctx.beginPath();
    ctx.roundRect(i * (bar + gap), (hgt - bh) / 2, bar, bh, 1.5);
    ctx.fill();
  });
}

function drawWave() {
  if (peaks) drawPeaks($("#wave"), peaks, audio.duration ? audio.currentTime / audio.duration : 0);
}

async function loadWave(url) {
  peaks = null;
  try {
    const buf = await (await fetch(url)).arrayBuffer();
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const decoded = await ctx.decodeAudioData(buf);
    ctx.close();
    peaks = decoded.getChannelData(0);
  } catch { peaks = new Float32Array(1); }
  drawWave();
}

function showResult() {
  const { item, request } = S.last;
  const card = $("#result");
  card.hidden = false;
  card.style.animation = "none"; void card.offsetWidth; card.style.animation = "";
  audio.src = item.url;
  loadWave(item.url);
  $("#download").href = `${item.url}?download=true`;
  const who = item.voice_name ? `Polyjuice · ${item.voice_name}` : item.mode === "clone" ? "Polyjuice · fresh sample" :
    item.mode === "design" ? `Transfiguration · ${item.instruct}` : "Sorting Hat";
  const speedNote = item.rtf ? (item.rtf < 1 ? `${(1 / item.rtf).toFixed(1)}× faster than real time` : `${item.rtf.toFixed(1)}× real time`) : "";
  const meta = $("#result-meta");
  meta.replaceChildren(
    h("span", {}, h("b", {}, who)),
    h("span", {}, "by ", h("b", {}, engineLabel(item.engine || "omnivoice"))),
    h("span", {}, h("b", {}, `${item.seconds.toFixed(1)}s`), " of speech"),
    h("span", {}, "cast in ", h("b", {}, `${item.elapsed.toFixed(1)}s`), speedNote ? ` (${speedNote})` : ""),
    h("span", {}, "seed ", h("b", {}, item.seed)),
  );
  $("#save-result-voice").hidden = !S.last.ref;
  audio.play().catch(() => { /* autoplay may be blocked */ });
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

$("#play").addEventListener("click", () => (audio.paused ? audio.play() : audio.pause()));
audio.addEventListener("play", () => { $("#play").textContent = "❚❚"; tickWave(); });
audio.addEventListener("pause", () => ($("#play").textContent = "▶"));
audio.addEventListener("ended", () => { $("#play").textContent = "▶"; drawWave(); });
audio.addEventListener("loadedmetadata", () => ($("#time").textContent = `0:00 / ${fmtTime(audio.duration)}`));
function tickWave() {
  drawWave();
  if (audio.duration) $("#time").textContent = `${fmtTime(audio.currentTime)} / ${fmtTime(audio.duration)}`;
  if (!audio.paused) requestAnimationFrame(tickWave);
}
$("#wave").addEventListener("click", (e) => {
  if (!audio.duration) return;
  const r = e.currentTarget.getBoundingClientRect();
  audio.currentTime = ((e.clientX - r.left) / r.width) * audio.duration;
  drawWave();
  if (audio.paused) audio.play();
});
window.addEventListener("resize", () => { drawWave(); drawMini(); });
window.addEventListener("hashchange", () => { const m = location.hash.slice(1); if (MODES.includes(m)) setMode(m); });

$("#pin-seed").addEventListener("click", () => {
  if (!S.last) return;
  $("#seed").value = S.last.item.seed;
  $("#use-seed").checked = true;
  $("#use-seed").dispatchEvent(new Event("change"));
  toast(`Seed ${S.last.item.seed} pinned in the Spellbook.`, "ok", 2200);
});

function cliCommand(item, req) {
  const parts = ["sonorus", "speak", shellQuote(item.text)];
  parts.push("-e", item.engine || "omnivoice");
  if (item.voice) parts.push("-v", shellQuote(item.voice));
  else if (item.mode === "clone") parts.push("-r", "your-clip.wav", ...(req && req.ref_text ? ["-t", shellQuote(req.ref_text)] : []));
  if (item.instruct) parts.push("-d", shellQuote(item.instruct));
  if (item.language) parts.push("-l", item.language);
  if (item.duration_target) parts.push("--duration", item.duration_target);
  else if (item.speed) parts.push("-s", item.speed);
  if (item.normalize_text) parts.push("--normalize");
  parts.push("--seed", item.seed);
  const flag = { num_step: "--steps", guidance_scale: "--guidance", t_shift: "--t-shift", position_temperature: "--position-temp",
    class_temperature: "--token-temp", layer_penalty_factor: "--layer-penalty", pad_duration: "--pad", fade_duration: "--fade",
    audio_chunk_duration: "--chunk-duration", audio_chunk_threshold: "--chunk-threshold",
    denoise: "--denoise", preprocess_prompt: "--preprocess", postprocess_output: "--postprocess",
    temperature: "--temperature", top_p: "--top-p", chunk_length: "--chunk-length", max_new_tokens: "--max-tokens" };
  for (const [k, v] of Object.entries(item.params || {})) {
    if (typeof v === "boolean") parts.push(v ? flag[k] : flag[k].replace("--", "--no-"));
    else parts.push(flag[k], v);
  }
  parts.push("-o", `speech.${item.format}`);
  return parts.join(" ");
}

$("#copy-cli").addEventListener("click", async () => {
  if (!S.last) return;
  const cmd = cliCommand(S.last.item, S.last.request);
  try { await navigator.clipboard.writeText(cmd); toast("CLI incantation copied to your clipboard."); }
  catch { prompt("Copy this command:", cmd); }
});

$("#save-result-voice").addEventListener("click", async () => {
  const dlg = $("#name-dialog");
  $("#dialog-name").value = "";
  dlg.showModal();
  dlg.addEventListener("close", async function onClose() {
    dlg.removeEventListener("close", onClose);
    const name = $("#dialog-name").value.trim();
    if (dlg.returnValue !== "ok" || !name) return;
    const keep = S.ref;
    S.ref = S.last.ref;
    try {
      brewing(true, "Brewing Polyjuice Potion…");
      await storeVoice(name);
      $("#save-result-voice").hidden = true;
      S.last.ref = null;
    } catch (e) { toast(e.message, "error"); }
    finally { S.ref = keep; brewing(false); }
  });
});

/* ============================== pensieve ============================== */
const memoryAudio = new Audio();
let playingId = null;
memoryAudio.addEventListener("ended", () => { playingId = null; renderHistory(); });

async function loadHistory() {
  try { S.history = await api("/api/history"); } catch { S.history = []; }
  renderHistory();
}

function renderHistory() {
  const list = $("#history");
  const q = $("#history-search").value.trim().toLowerCase();
  const items = S.history.filter((i) => !q || [i.text, i.voice_name, i.instruct, i.mode, i.engine].join(" ").toLowerCase().includes(q));
  $("#history-empty").hidden = S.history.length > 0;
  $("#clear-history").hidden = S.history.length === 0;
  list.replaceChildren(...items.map((i) => {
    const label = { clone: "POLYJUICE", design: "TRANSFIG", auto: "HAT" }[i.mode];
    const who = i.voice_name || i.instruct || (i.mode === "clone" ? "fresh sample" : "the hat's choice");
    return h("li", { class: `memory${playingId === i.id ? " playing" : ""}` },
      h("button", { class: "mplay", title: "Play", onclick: () => playMemory(i) }, playingId === i.id ? "❚❚" : "▶"),
      h("div", { style: "min-width:0" },
        h("p", { class: "mtext", title: i.text }, i.text),
        h("p", { class: "msub" }, h("span", { class: "badge" }, label), i.engine === "fish" ? h("span", { class: "badge alt" }, "FISH") : null,
          `${who} · ${i.seconds.toFixed(1)}s · ${ago(i.created)}`)),
      h("div", { class: "mactions" },
        h("button", { title: "Priori Incantatem: reuse these settings", onclick: () => reuse(i) }, "↺"),
        h("a", { title: "Download", href: `${i.url}?download=true` }, "⬇"),
        h("button", { title: "Copy as CLI command", onclick: () => navigator.clipboard.writeText(cliCommand(i)).then(() => toast("CLI incantation copied.")) }, "⌘"),
        h("button", { title: "Remove this memory", onclick: () => forgetMemory(i) }, "✕")));
  }));
}
$("#history-search").addEventListener("input", renderHistory);

function playMemory(i) {
  if (playingId === i.id) { memoryAudio.pause(); playingId = null; }
  else { audio.pause(); memoryAudio.src = i.url; memoryAudio.play(); playingId = i.id; }
  renderHistory();
}

async function forgetMemory(i) {
  try { await api(`/api/history/${i.id}`, { method: "DELETE" }); loadHistory(); }
  catch (e) { toast(e.message, "error"); }
}

$("#clear-history").addEventListener("click", async () => {
  if (!confirm("Empty the whole Pensieve? All generated audio will be deleted.")) return;
  await api("/api/history", { method: "DELETE" });
  loadHistory();
});

function reuse(i) {
  const eng = i.engine || "omnivoice";
  if (eng !== S.engine) {
    if (!engineInfo(eng)?.available) { toast(`That memory was cast with ${engineLabel(eng)}, which isn't set up here.`, "error"); return; }
    setEngine(eng);
  }
  textEl.value = i.text; updateCount();
  if (i.mode === "clone" && i.voice && S.voices.some((v) => v.id === i.voice)) {
    setMode("clone"); setSource("vault"); selectVoice(i.voice);
    $("#clone-instruct").value = i.instruct || "";
  } else if (i.mode === "design") {
    setMode("design"); $("#instruct").value = i.instruct; parseInstruct(i.instruct);
  } else if (i.mode === "auto") {
    setMode("auto");
  } else {
    toast("That voice came from a one-off sample; add the clip again to reuse it.", "error");
  }
  const lang = S.options.languages.find((l) => l.id === i.language);
  $("#lang").value = lang ? `${lang.name} (${lang.id})` : "";
  $("#use-duration").checked = !!i.duration_target;
  if (i.duration_target) $("#duration").value = i.duration_target;
  $("#use-duration").dispatchEvent(new Event("change"));
  speedSet = !!i.speed; $("#speed").value = i.speed || 1; renderSpeed();
  $("#use-seed").checked = true; $("#seed").value = i.seed; $("#use-seed").dispatchEvent(new Event("change"));
  $("#normalize").checked = !!i.normalize_text;
  $("#format").value = i.format;
  S.params = { ...(i.params || {}) }; saveParams(); syncParams();
  window.scrollTo({ top: 0, behavior: "smooth" });
  toast("Priori Incantatem! Settings restored, seed pinned.", "ok", 2200);
}

/* ============================== boot ============================== */
async function boot() {
  setHouse(store.get("house", "gryffindor"));
  const fromHash = location.hash.slice(1);
  setMode(MODES.includes(fromHash) ? fromHash : S.mode);
  setSource(S.source);
  pollStatus();
  try { S.options = await api("/api/options"); }
  catch { toast("Couldn't reach the server. Start it with `sonorus serve`.", "error", 10000); return; }

  $("#lang-list").append(...S.options.languages.map((l) => h("option", { value: `${l.name} (${l.id})` })));
  $("#format").append(...S.options.formats.map((f) => h("option", { value: f }, f.toUpperCase())));
  $("#format").value = store.get("format", "wav");
  buildDesign();
  buildEngines();
  setEngine(S.engine);

  textEl.value = store.get("text", "") || SAMPLES[0];
  updateCount();
  $("#lang").value = store.get("lang", "");
  $("#instruct").value = store.get("instruct", "");
  parseInstruct($("#instruct").value);
  $("#clone-instruct").value = store.get("cloneInstruct", "");
  $("#speed").value = store.get("speed", 1);
  renderSpeed();

  await Promise.all([loadVoices(), loadHistory()]);
  if (S.mode === "clone" && S.source === "vault" && !S.voices.length) setSource("fresh");
}
boot();
