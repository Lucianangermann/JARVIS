// JARVIS PWA — camera capture + vision upload.
//
// We use the dead-simple <input type="file" accept="image/*" capture> path
// rather than getUserMedia. Reasons:
//   • iOS Safari standalone PWAs grant camera access via the file picker
//     consistently — getUserMedia inside an installed PWA still has
//     permission edge cases between iOS versions.
//   • One tap opens the native camera UI; the user shoots, returns, we
//     receive the File. No video stream lifecycle, no canvas-from-stream.
//   • Live translation (rear camera as a video stream with text overlay)
//     is a future enhancement; phase 5 ships single-shot photo upload.
//
// All four actions (analyze, scan, OCR/read, translate) share the same
// front half: capture → compress → base64. They diverge only in which
// /vision/* endpoint the base64 goes to.
//
// Compression: every photo is canvas-resized to ≤1920 px on the long
// edge and re-encoded to JPEG quality 0.8 before upload. The server's
// image_to_base64 will resize again to its own cap, but compressing
// client-side keeps the upload itself small on cellular.

import * as cfg from "./config.js";

const MAX_LONG_EDGE = 1920;
const JPEG_QUALITY = 0.8;

// One queued capture at a time. The file input fires `change` after
// the picker closes; we resolve a promise the caller is awaiting.
let pendingResolve = null;
let pendingReject = null;

// ── Wiring ────────────────────────────────────────────────────────

/** Initialise the camera UI. Call ONCE during app boot.
 *
 *  @param {object} opts
 *  @param {(role: string, text: string) => HTMLElement} opts.addMessage
 *      Function from app.js that appends a chat bubble.
 *  @param {(state: string) => void} opts.setState
 *      Optional — flips body[data-state] so the HUD shows processing.
 *  @param {(msg: string) => void} opts.logDebug
 *      Debug-log sink shared with the rest of app.js.
 *  @param {(text: string) => void} [opts.speakSentence]
 *      Optional — hands the reply to the parallel-prefetch TTS queue
 *      so the iPhone actually speaks the vision response. Without
 *      this the answer renders silently in the chat, because the
 *      vision HTTP path doesn't go through the WebSocket
 *      jarvis_partial stream that normally triggers speakSentence.
 *  @param {() => void} [opts.primeTts]
 *      Optional — fires the silent-WAV iOS audio unlocker inside
 *      the camera button gesture. If the user taps the camera as
 *      their FIRST action of the PWA session (no PTT, no send),
 *      iOS still has audio locked and the eventual ttsAudio.play()
 *      would silently fail. Calling primeTts here piggybacks on
 *      the camera gesture to satisfy the autoplay policy.
 */
export function initCamera({
  addMessage, setState, logDebug, speakSentence, primeTts,
}) {
  const cameraBtn    = document.getElementById("act-camera");
  const camPanel     = document.getElementById("camera-panel");
  const camClose     = document.getElementById("cam-close");
  const camInput     = document.getElementById("cam-input");

  // Action buttons inside the panel — each picks the endpoint.
  const btnPhoto     = document.getElementById("cam-act-photo");
  const btnDoc       = document.getElementById("cam-act-doc");
  const btnRead      = document.getElementById("cam-act-read");
  const btnTranslate = document.getElementById("cam-act-translate");

  if (!cameraBtn || !camPanel || !camInput) {
    // Defensive: phase 5 hasn't rolled out yet, or the HTML is older.
    console.warn("[camera] UI elements missing — skipping init");
    return;
  }

  function openPanel() {
    camPanel.classList.add("open");
    camPanel.setAttribute("aria-hidden", "false");
  }
  function closePanel() {
    camPanel.classList.remove("open");
    camPanel.setAttribute("aria-hidden", "true");
  }

  cameraBtn.addEventListener("click", () => {
    // Prime iOS audio inside THIS gesture so a later
    // ttsAudio.play() (triggered by the vision reply, far outside
    // any user gesture) isn't rejected by Safari's autoplay policy.
    // primeTts is idempotent — already-unlocked is a no-op.
    if (typeof primeTts === "function") primeTts();
    openPanel();
  });
  camClose.addEventListener("click", closePanel);

  // The file input's `change` event is the camera return signal.
  // Resolve whichever promise is currently waiting on a capture.
  camInput.addEventListener("change", () => {
    const file = camInput.files && camInput.files[0];
    // Reset so the same file can be picked again later (Safari quirk).
    camInput.value = "";
    if (!file) {
      if (pendingReject) pendingReject(new Error("no file selected"));
    } else if (pendingResolve) {
      pendingResolve(file);
    }
    pendingResolve = null;
    pendingReject = null;
  });

  /** Trigger the native camera UI and resolve with the chosen File. */
  function captureFile() {
    return new Promise((resolve, reject) => {
      pendingResolve = resolve;
      pendingReject = reject;
      // .click() must run inside a user gesture — every caller of
      // captureFile() is itself a button-handler call, so we're fine.
      camInput.click();
    });
  }

  async function runFlow(action) {
    closePanel();
    let file;
    try {
      file = await captureFile();
    } catch (e) {
      logDebug(`[camera] cancelled: ${e.message || e}`);
      return;
    }
    setState && setState("processing");
    addMessage("you", `📷 ${actionLabel(action)}…`);
    logDebug(`[camera] uploading ${action}, ${(file.size / 1024).toFixed(0)} KiB`);

    let base64;
    try {
      base64 = await compressToBase64(file);
    } catch (e) {
      addMessage("jarvis", `Bildverarbeitung fehlgeschlagen: ${e.message || e}`);
      setState && setState("idle");
      return;
    }
    logDebug(`[camera] compressed to ${(base64.length * 0.75 / 1024).toFixed(0)} KiB`);

    try {
      const result = await uploadToVision(action, base64);
      const text = formatResult(action, result);
      addMessage("jarvis", text);
      // Speak the result through the same prefetch+queue TTS pipeline
      // a normal WS reply uses. Fire-and-forget — speakSentence
      // enqueues and resolves quickly; the audio plays asynchronously.
      // Skipped silently if the caller didn't pass speakSentence
      // (older app.js wirings) — text still displays.
      if (typeof speakSentence === "function" && text) {
        try { speakSentence(text); }
        catch (e) { logDebug(`[camera] tts enqueue failed: ${e.message || e}`); }
      }
    } catch (e) {
      const errText = `Vision-Server-Fehler: ${e.message || e}`;
      addMessage("jarvis", errText);
      if (typeof speakSentence === "function") {
        try { speakSentence(errText); } catch { /* ignore */ }
      }
      logDebug(`[camera] upload failed: ${e.message || e}`);
    } finally {
      setState && setState("idle");
    }
  }

  btnPhoto    .addEventListener("click", () => runFlow("photo"));
  btnDoc      .addEventListener("click", () => runFlow("document"));
  btnRead     .addEventListener("click", () => runFlow("read"));
  btnTranslate.addEventListener("click", () => runFlow("translate"));
}


// ── Compression ───────────────────────────────────────────────────

/** File → resized JPEG → base64 string (no data: prefix). */
async function compressToBase64(file) {
  const bitmap = await loadBitmap(file);
  const { width, height } = fitToBounds(bitmap.width, bitmap.height);
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("canvas 2D context unavailable");
  ctx.drawImage(bitmap, 0, 0, width, height);

  const blob = await new Promise((resolve, reject) => {
    canvas.toBlob(
      (b) => b ? resolve(b) : reject(new Error("canvas.toBlob returned null")),
      "image/jpeg",
      JPEG_QUALITY,
    );
  });

  const buffer = await blob.arrayBuffer();
  // Convert to base64 in chunks to avoid call-stack blowup on >100k bytes.
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

async function loadBitmap(file) {
  if (typeof createImageBitmap === "function") {
    // Fastest path; available in iOS 15+ Safari.
    return await createImageBitmap(file);
  }
  // Fallback for very old engines: HTMLImageElement.
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    await new Promise((resolve, reject) => {
      img.onload = resolve;
      img.onerror = () => reject(new Error("image decode failed"));
      img.src = url;
    });
    return { width: img.naturalWidth, height: img.naturalHeight,
             // Make it usable as a drawImage source.
             draw: (ctx) => ctx.drawImage(img, 0, 0) };
  } finally {
    URL.revokeObjectURL(url);
  }
}

function fitToBounds(w, h) {
  const longEdge = Math.max(w, h);
  if (longEdge <= MAX_LONG_EDGE) return { width: w, height: h };
  const scale = MAX_LONG_EDGE / longEdge;
  return {
    width:  Math.max(1, Math.round(w * scale)),
    height: Math.max(1, Math.round(h * scale)),
  };
}


// ── Vision endpoint dispatch ──────────────────────────────────────

async function uploadToVision(action, base64) {
  const base = cfg.httpBase();
  if (!base) throw new Error("no server URL configured");
  const headers = {
    "Content-Type": "application/json",
    ...cfg.authHeader(),
  };

  let url, body;
  switch (action) {
    case "photo":
      url = `${base}/vision/analyze`;
      body = {
        image: base64,
        question: "Was ist auf diesem Bild zu sehen? Antworte auf Deutsch.",
      };
      break;
    case "document":
      url = `${base}/vision/scan`;
      body = { image: base64, doc_type: "auto" };
      break;
    case "read":
      url = `${base}/vision/analyze`;
      body = {
        image: base64,
        question: (
          "Extrahiere ALLEN sichtbaren Text aus diesem Bild exakt so, " +
          "wie er dort steht. Behalte die Formatierung wenn möglich."
        ),
      };
      break;
    case "translate":
      url = `${base}/vision/translate`;
      body = { image: base64, target_language: "de" };
      break;
    default:
      throw new Error(`unknown camera action: ${action}`);
  }

  const r = await fetch(url, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch { /* ignore */ }
    throw new Error(`HTTP ${r.status}${detail ? `: ${detail}` : ""}`);
  }
  return await r.json();
}


// ── Result rendering ──────────────────────────────────────────────

function actionLabel(action) {
  switch (action) {
    case "photo":     return "Foto";
    case "document":  return "Dokument";
    case "read":      return "Text";
    case "translate": return "Übersetzung";
    default:          return action;
  }
}

function formatResult(action, body) {
  if (!body) return "Keine Antwort vom Server.";
  switch (action) {
    case "photo":
    case "read":
      return body.result || "(leere Antwort)";
    case "document":
      // Show the speakable summary first, then the structured snippet
      // for the user to scan visually.
      if (!body.summary) return "(Dokument konnte nicht erkannt werden)";
      const dt = body.doc_type ? ` [${body.doc_type}]` : "";
      return body.summary + dt;
    case "translate":
      if (!body.translated && !body.original) {
        return "Keinen Text im Bild gefunden.";
      }
      const orig = body.original  ? `Original: ${body.original}` : "";
      const trans = body.translated ? `Übersetzung: ${body.translated}` : "";
      return [orig, trans].filter(Boolean).join("\n");
    default:
      return JSON.stringify(body);
  }
}
