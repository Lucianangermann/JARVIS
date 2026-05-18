// JARVIS PWA — push-to-talk recorder.
//
// Touch / pointer driven. Hold the hex button → MediaRecorder
// runs → release → POST blob to /transcribe → server returns text
// → caller fires ws.send(text) so the brain replies via streaming.
//
// iOS Safari constraints baked in:
//   - getUserMedia only works on a SECURE origin (HTTPS or
//     localhost). On HTTP the start() call rejects with a clear
//     error so the UI can prompt the user.
//   - Mic prompt only fires on the FIRST start. Subsequent
//     records reuse the granted permission.
//   - Safari prefers audio/mp4 (mostly H.264-AAC); audio/webm is
//     "supported" but the encoded file often refuses to decode
//     on the server side. We probe + pick the best match.
//   - touch-action: none on the button (in CSS) + pointercancel
//     handling here keep accidental scrolls / context menus out
//     of the recording path.

import * as cfg from "./config.js";

export const STATE = Object.freeze({
  IDLE: "idle",
  RECORDING: "recording",
  PROCESSING: "processing",
  ERROR: "error",
});

const MIN_RECORD_MS = 500;
const MAX_RECORD_MS = 30000;

let state = STATE.IDLE;
let mediaStream = null;
let recorder = null;
let chunks = [];
let recordStartedAt = 0;
let maxTimer = null;

let audioCtx = null;
let analyser = null;
let amplitudeRaf = 0;

const stateListeners = new Set();
const transcriptListeners = new Set();
let amplitudeCallback = null;


export function getState() { return state; }
export function onStateChange(cb) {
  stateListeners.add(cb);
  cb(state);
  return () => stateListeners.delete(cb);
}
export function onTranscript(cb) {
  transcriptListeners.add(cb);
  return () => transcriptListeners.delete(cb);
}
export function onAmplitude(cb) { amplitudeCallback = cb; }


function setState(next) {
  if (state === next) return;
  state = next;
  for (const cb of stateListeners) {
    try { cb(next); } catch (e) { console.error("[ptt] listener:", e); }
  }
}

/** Pick the highest-fidelity MIME the current Safari build will
 *  actually emit. Order is intentional: server-side ffmpeg /
 *  Apple Speech handles audio/mp4 + wav best. */
function pickMime() {
  const candidates = [
    "audio/mp4",
    "audio/mp4;codecs=mp4a.40.2",
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
  ];
  if (typeof MediaRecorder === "undefined") return "";
  for (const m of candidates) {
    if (MediaRecorder.isTypeSupported(m)) return m;
  }
  return "";
}


/** Begin recording. Called from the button's pointerdown handler.
 *  Async because we may need to ask for mic permission on the
 *  first ever press. */
export async function start() {
  if (state !== STATE.IDLE) return;
  if (!window.isSecureContext) {
    notifyError("Microphone requires HTTPS — open the PWA via the Tailscale URL.");
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    notifyError("This browser does not support microphone access.");
    return;
  }
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
  } catch (err) {
    console.warn("[ptt] getUserMedia denied:", err);
    notifyError("Microphone permission denied — enable JARVIS in Settings → Safari.");
    return;
  }

  const mime = pickMime();
  try {
    recorder = mime ? new MediaRecorder(mediaStream, { mimeType: mime })
                    : new MediaRecorder(mediaStream);
  } catch (err) {
    console.warn("[ptt] MediaRecorder ctor:", err);
    notifyError("This browser cannot record audio.");
    teardownStream();
    return;
  }
  chunks = [];
  recorder.addEventListener("dataavailable", (ev) => {
    if (ev.data && ev.data.size > 0) chunks.push(ev.data);
  });
  recorder.addEventListener("stop", finishRecording);
  recorder.start();
  recordStartedAt = performance.now();

  // Drive the visualiser from the live mic stream — gives the
  // user a "yes you are being heard" cue without relying on the
  // PTT label change alone.
  attachAnalyser(mediaStream);

  if ("vibrate" in navigator) navigator.vibrate(45);
  setState(STATE.RECORDING);

  // Safety: auto-release after MAX_RECORD_MS even if the user
  // somehow keeps holding (eg. screen lock).
  maxTimer = setTimeout(stop, MAX_RECORD_MS);
}


/** End recording. Called from pointerup / pointercancel. Short
 *  presses (<MIN_RECORD_MS) are discarded — likely accidental. */
export function stop() {
  if (state !== STATE.RECORDING) return;
  clearTimeout(maxTimer);
  maxTimer = null;
  try { recorder?.stop(); } catch (e) { console.warn("[ptt] stop:", e); }
  if ("vibrate" in navigator) navigator.vibrate([28, 40, 28]);
}


/** Discard the current recording without sending it (user slid
 *  finger off the button). */
export function cancel() {
  if (state !== STATE.RECORDING) return;
  clearTimeout(maxTimer);
  maxTimer = null;
  try { recorder?.stop(); } catch {}
  chunks = [];      // dropped
  teardownStream();
  setState(STATE.IDLE);
}


async function finishRecording() {
  const durationMs = performance.now() - recordStartedAt;
  teardownStream();
  if (durationMs < MIN_RECORD_MS) {
    chunks = [];
    setState(STATE.IDLE);
    return;
  }
  if (chunks.length === 0) {
    setState(STATE.IDLE);
    return;
  }
  const mime = recorder?.mimeType || "audio/mp4";
  const blob = new Blob(chunks, { type: mime });
  chunks = [];

  setState(STATE.PROCESSING);
  const transcript = await transcribe(blob);
  setState(STATE.IDLE);
  if (transcript && transcript.trim()) {
    for (const cb of transcriptListeners) {
      try { cb(transcript.trim()); } catch (e) { console.error("[ptt] transcript:", e); }
    }
  }
}


async function transcribe(blob) {
  const base = cfg.httpBase();
  if (!base) {
    notifyError("Not connected — open settings to configure the server URL.");
    return "";
  }
  const form = new FormData();
  // Server's POST /transcribe accepts a multipart "audio" field —
  // see server/main.py route below this commit.
  const ext = (blob.type.includes("mp4") ? "m4a"
            : blob.type.includes("webm") ? "webm"
            : blob.type.includes("ogg")  ? "ogg"
            : "audio");
  form.append("audio", blob, `clip.${ext}`);
  try {
    const r = await fetch(base + "/transcribe", {
      method: "POST",
      headers: cfg.authHeader(),
      body: form,
    });
    if (!r.ok) {
      const txt = await r.text().catch(() => "");
      console.warn("[ptt] transcribe http", r.status, txt);
      notifyError(`Transcribe failed: HTTP ${r.status}`);
      return "";
    }
    const data = await r.json();
    return data.transcript || data.text || "";
  } catch (err) {
    console.warn("[ptt] transcribe fetch:", err);
    notifyError("Could not reach server for transcription.");
    return "";
  }
}


function teardownStream() {
  if (mediaStream) {
    for (const track of mediaStream.getTracks()) track.stop();
    mediaStream = null;
  }
  if (amplitudeRaf) {
    cancelAnimationFrame(amplitudeRaf);
    amplitudeRaf = 0;
  }
  if (audioCtx) {
    audioCtx.close().catch(() => {});
    audioCtx = null;
    analyser = null;
  }
}


/** Wire an AnalyserNode into the input stream so the PTT button's
 *  internal canvas + the main visualizer can draw a real waveform
 *  amplitude. Cheap — single FFT per rAF tick. */
function attachAnalyser(stream) {
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const src = audioCtx.createMediaStreamSource(stream);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;
    src.connect(analyser);
    const buf = new Uint8Array(analyser.frequencyBinCount);
    const tick = () => {
      if (!analyser) return;
      analyser.getByteTimeDomainData(buf);
      // RMS in 0..1
      let sumSq = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128;
        sumSq += v * v;
      }
      const rms = Math.sqrt(sumSq / buf.length);
      if (amplitudeCallback) amplitudeCallback(Math.min(1, rms * 2));
      amplitudeRaf = requestAnimationFrame(tick);
    };
    tick();
  } catch (err) {
    console.warn("[ptt] analyser unavailable:", err);
  }
}

function notifyError(msg) {
  console.warn("[ptt]", msg);
  setState(STATE.ERROR);
  // Allow the UI to clear the error after the user sees it.
  setTimeout(() => setState(STATE.IDLE), 1500);
  // No alert() — let app.js render a banner instead.
  for (const cb of stateListeners) {
    try { cb(STATE.ERROR, msg); } catch {}
  }
}
