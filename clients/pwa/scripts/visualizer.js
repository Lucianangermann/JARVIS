// JARVIS PWA — central core visualizer.
//
// Pure-Canvas concentric arcs that respond to the HUD's state
// machine. Mirrors what the Electron visualizer does in
// ui/renderer/scripts/visualizer.js but trimmed to one canvas per
// phone screen + sized off the viewport so it adapts when the
// device flips between portrait / landscape.
//
// Public surface:
//   start()                        — begin the rAF loop
//   setState(state)                — "idle" / "processing" /
//                                    "speaking" / "recording"
//   setAmplitude(level0to1)        — real-time mic / TTS level
//                                    (the PTT recorder pushes this
//                                    via an AnalyserNode tap)

const TAU = Math.PI * 2;

let canvas = null;
let ctx = null;
let dpr = 1;
let raf = 0;

let state = "idle";
let amplitude = 0;          // smoothed 0..1
let amplitudeTarget = 0;
let phase = 0;              // monotonic radian accumulator

const STATE_TUNING = {
  idle:       { speed: 0.20, glow: 0.35, hot: false },
  recording:  { speed: 0.50, glow: 0.90, hot: true  },
  processing: { speed: 1.40, glow: 0.55, hot: false },
  speaking:   { speed: 0.55, glow: 0.85, hot: false },
};


export function start(canvasEl) {
  canvas = canvasEl;
  ctx = canvas.getContext("2d", { alpha: true });
  resize();
  window.addEventListener("resize", resize, { passive: true });
  // The visibilitychange path stops the rAF loop when the PWA
  // backgrounds so we don't burn battery while the user is in
  // another app. Resume on visible.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) cancelAnimationFrame(raf);
    else tick();
  });
  tick();
}

export function setState(next) {
  if (STATE_TUNING[next]) state = next;
}

export function setAmplitude(v) {
  // Clamp + smooth so the visualisation doesn't pop on a single
  // loud sample. Decays toward zero between updates.
  amplitudeTarget = Math.max(0, Math.min(1, v || 0));
}


function resize() {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  dpr = Math.min(window.devicePixelRatio || 1, 2);   // cap @2× on
                                                     // Retina phones to
                                                     // keep paint cheap
  canvas.width  = Math.round(rect.width  * dpr);
  canvas.height = Math.round(rect.height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function tick() {
  if (!ctx) return;
  raf = requestAnimationFrame(tick);

  const t = performance.now() / 1000;
  const tune = STATE_TUNING[state] || STATE_TUNING.idle;

  // Smooth the amplitude toward its target so visual jitter
  // doesn't track every audio sample literally.
  amplitude += (amplitudeTarget - amplitude) * 0.15;
  amplitudeTarget *= 0.93;        // natural decay when input stalls

  phase += 0.016 * tune.speed;

  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  const cx = w / 2, cy = h / 2;
  const r = Math.min(cx, cy);

  ctx.clearRect(0, 0, w, h);

  // Outer faint ring
  drawCircle(cx, cy, r * 0.95, "rgba(0,212,255,0.18)", 1);

  // Concentric data arcs — three rings whose stroke widths +
  // glow scale with state amplitude. Sweep is constant; the
  // perceived motion comes from the rotation phase.
  drawArc(cx, cy, r * 0.80, phase * 1.0,         0.30 + amplitude * 0.4, tune);
  drawArc(cx, cy, r * 0.65, -phase * 0.7 + 1.2,  0.20 + amplitude * 0.5, tune);
  drawArc(cx, cy, r * 0.50, phase * 1.3  + 2.8,  0.15 + amplitude * 0.6, tune);

  // Centre spokes that pulse with amplitude — gives the same
  // "alive" feel as the desktop visualizer when speaking.
  drawSpokes(cx, cy, r * 0.45, phase * 0.4, 24, tune);
}

function drawCircle(cx, cy, radius, color, width = 1) {
  ctx.beginPath();
  ctx.arc(cx, cy, radius, 0, TAU);
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.stroke();
}

function drawArc(cx, cy, radius, rotation, spanFrac, tune) {
  const span = TAU * spanFrac;
  const color = tune.hot
    ? `rgba(255, 42, 74, ${0.4 + tune.glow * 0.6})`
    : `rgba(0, 212, 255, ${0.45 + tune.glow * 0.5})`;
  ctx.beginPath();
  ctx.arc(cx, cy, radius, rotation, rotation + span);
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5 + tune.glow * 1.5;
  ctx.lineCap = "round";
  ctx.shadowColor = color;
  ctx.shadowBlur = 6 + tune.glow * 10;
  ctx.stroke();
  ctx.shadowBlur = 0;
}

function drawSpokes(cx, cy, length, rotation, count, tune) {
  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate(rotation);
  const intensity = 0.20 + amplitude * 0.7;
  ctx.strokeStyle = tune.hot
    ? `rgba(255, 42, 74, ${intensity})`
    : `rgba(0, 255, 255, ${intensity})`;
  ctx.lineWidth = 1;
  for (let i = 0; i < count; i++) {
    const a = (i / count) * TAU;
    // Slight per-spoke jitter modulated by amplitude so the
    // "bars" feel responsive to TTS / mic input.
    const len = length * (0.4 + 0.6 * (0.5 + 0.5 * Math.sin(a * 3 + rotation * 4)) * (0.3 + amplitude));
    ctx.beginPath();
    ctx.moveTo(Math.cos(a) * (length * 0.55), Math.sin(a) * (length * 0.55));
    ctx.lineTo(Math.cos(a) * len, Math.sin(a) * len);
    ctx.stroke();
  }
  ctx.restore();
}
