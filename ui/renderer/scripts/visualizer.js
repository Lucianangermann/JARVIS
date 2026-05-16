// ============================================================
// JARVIS overlay — Canvas voice visualizer
//
// Renders a ring of frequency-bar-like spokes around the central
// hexagon. In Phase 1 there is no real microphone input — bar
// amplitudes are synthesised from a state-driven model:
//
//   idle       → gentle sine breath (slow, low)
//   active     → low ambient noise
//   speaking   → multi-band oscillating envelope (mock TTS)
//   processing → travelling wave that orbits the ring
//
// Phase 2 will swap in real `getUserMedia` + AnalyserNode without
// changing the render path — we just feed the bar values from a
// different source.
// ============================================================

const canvas = document.getElementById("visualizer");
const ctx = canvas.getContext("2d");

const BARS = 72;            // ring spokes
const INNER_R_RATIO = 0.36; // distance from centre to bar inner edge
const OUTER_R_RATIO = 0.49; // outer edge — leaves room for the ring border

let currentState = "idle";
let lastTs = performance.now();
let phase = 0;              // animation clock (radians)
let bandPhases = Array.from({ length: 6 }, (_, i) => i * 0.7);

export function setVisualizerState(state) {
  currentState = state;
  // Hard reset the wave phase so transitions look snappy.
  phase = 0;
}

// Pick a HiDPI canvas backing size so the bars stay crisp on Retina.
function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const cssSize = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.floor(cssSize.width * dpr));
  canvas.height = Math.max(1, Math.floor(cssSize.height * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
resizeCanvas();
new ResizeObserver(resizeCanvas).observe(canvas);

// ---- per-state amplitude model -------------------------------------

function amplitude(i, t) {
  // i: bar index 0..BARS-1
  // t: total seconds elapsed
  switch (currentState) {
    case "idle": {
      // Soft breathing pulse — every bar carries the same wave so the
      // whole ring breathes together.
      const breath = (Math.sin(t * 0.9) + 1) * 0.5;
      return 0.05 + breath * 0.12;
    }
    case "active": {
      // Low ambient noise, individual bars wiggle independently.
      const wiggle = Math.sin(t * 2 + i * 0.5) * 0.06;
      return 0.08 + Math.max(0, wiggle);
    }
    case "speaking": {
      // Layered band oscillators give a believable "voice envelope".
      const bandIndex = i % bandPhases.length;
      const a =
        Math.sin(t * 6 + bandPhases[bandIndex]) * 0.35 +
        Math.sin(t * 13 + i * 0.12) * 0.15 +
        Math.sin(t * 2.3 + i * 0.04) * 0.20;
      return Math.max(0.06, 0.18 + a * 0.7);
    }
    case "processing": {
      // A bright peak that orbits the ring.
      const headIdx = (t * 30) % BARS;
      let d = Math.abs(i - headIdx);
      d = Math.min(d, BARS - d);                // wrap-around
      const env = Math.exp(-(d * d) / 30);      // gaussian halo
      return 0.07 + env * 0.85;
    }
    default:
      return 0.05;
  }
}

// ---- render loop --------------------------------------------------

function render(ts) {
  const dt = Math.min(0.066, (ts - lastTs) / 1000);
  lastTs = ts;
  phase += dt;

  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  const cx = w / 2;
  const cy = h / 2;
  const minDim = Math.min(w, h);
  const innerR = (minDim / 2) * INNER_R_RATIO;
  const outerR = (minDim / 2) * OUTER_R_RATIO;

  ctx.clearRect(0, 0, w, h);

  // Gradient stroke for the bars: cooler at the inner edge, hotter at the tip.
  for (let i = 0; i < BARS; i++) {
    const angle = (i / BARS) * Math.PI * 2 - Math.PI / 2;
    const amp = amplitude(i, phase);
    const len = (outerR - innerR) * amp + 1.5;

    const x1 = cx + Math.cos(angle) * innerR;
    const y1 = cy + Math.sin(angle) * innerR;
    const x2 = cx + Math.cos(angle) * (innerR + len);
    const y2 = cy + Math.sin(angle) * (innerR + len);

    const grad = ctx.createLinearGradient(x1, y1, x2, y2);
    grad.addColorStop(0, "rgba(0, 102, 255, 0.0)");
    grad.addColorStop(0.4, "rgba(0, 102, 255, 0.85)");
    grad.addColorStop(1, "rgba(0, 255, 255, 1.0)");

    ctx.strokeStyle = grad;
    ctx.lineWidth = 2;
    ctx.lineCap = "round";
    ctx.shadowColor = "rgba(0, 212, 255, 0.7)";
    ctx.shadowBlur = 4;
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  }

  requestAnimationFrame(render);
}

requestAnimationFrame(render);
