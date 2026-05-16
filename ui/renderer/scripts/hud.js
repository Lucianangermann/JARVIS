// ============================================================
// JARVIS overlay — passive HUD ornaments
//
// Spawns the floating particle layer. Each particle is a tiny div
// with randomised drift and duration via CSS custom properties, so
// the actual animation runs on the compositor (no JS per frame).
// ============================================================

const PARTICLE_COUNT = 14;

const layer = document.querySelector(".particles");

function spawnParticle() {
  const el = document.createElement("span");
  el.className = "particle";

  // Random start position somewhere along the bottom of the HUD,
  // drifting upward and slightly horizontally.
  const startX = Math.random() * 100;
  const startY = 60 + Math.random() * 40;     // bottom half
  const driftX = (Math.random() - 0.5) * 60;  // ±30 px
  const driftY = -(40 + Math.random() * 80);  // up
  const duration = 4 + Math.random() * 5;
  const delay = Math.random() * duration;

  el.style.left = `${startX}%`;
  el.style.top = `${startY}%`;
  el.style.setProperty("--dx", `${driftX}px`);
  el.style.setProperty("--dy", `${driftY}px`);
  el.style.animationDuration = `${duration}s`;
  el.style.animationDelay = `-${delay}s`;     // start mid-cycle so we don't all-blink-on

  layer.appendChild(el);
}

for (let i = 0; i < PARTICLE_COUNT; i++) spawnParticle();
