// ============================================================
// JARVIS emblem — programmatic tick / dot ring generator
//
// The static SVG markup in index.html declares each ring as an empty
// <g> with data-attributes (count, radius, stroke, …). At boot we walk
// those groups and append <line> / <circle> children. Keeps the HTML
// readable while still producing 100+ shapes for a dense look.
// ============================================================

const NS = "http://www.w3.org/2000/svg";

/** Build N radial tick lines between r1 (outer) and r2 (inner). */
function buildTicks(g) {
  const count   = +g.dataset.count;
  const r1      = +g.dataset.r1;
  const r2      = +g.dataset.r2;
  const stroke  = g.dataset.stroke || "currentColor";
  const width   = +g.dataset.width || 1;

  for (let i = 0; i < count; i++) {
    const a = (i / count) * Math.PI * 2 - Math.PI / 2;
    const x1 = Math.cos(a) * r1;
    const y1 = Math.sin(a) * r1;
    const x2 = Math.cos(a) * r2;
    const y2 = Math.sin(a) * r2;

    const line = document.createElementNS(NS, "line");
    line.setAttribute("x1", x1.toFixed(2));
    line.setAttribute("y1", y1.toFixed(2));
    line.setAttribute("x2", x2.toFixed(2));
    line.setAttribute("y2", y2.toFixed(2));
    line.setAttribute("stroke", stroke);
    line.setAttribute("stroke-width", width);
    line.setAttribute("stroke-linecap", "round");
    g.appendChild(line);
  }
}

/** Build N small circles around radius r, with optional brighter dots
 *  at index positions listed in data-highlights="0,6,12,…". */
function buildDots(g) {
  const count   = +g.dataset.count;
  const r       = +g.dataset.r;
  const dotR    = +g.dataset.dotR || 1.2;
  const fill    = g.dataset.fill || "currentColor";
  const hl      = (g.dataset.highlights || "")
                    .split(",").map((s) => parseInt(s, 10))
                    .filter((n) => Number.isFinite(n));
  const hlFill  = g.dataset.highlightFill || fill;
  const hlR     = +g.dataset.highlightR || dotR;

  for (let i = 0; i < count; i++) {
    const a = (i / count) * Math.PI * 2 - Math.PI / 2;
    const x = Math.cos(a) * r;
    const y = Math.sin(a) * r;
    const dot = document.createElementNS(NS, "circle");
    const bright = hl.includes(i);
    dot.setAttribute("cx", x.toFixed(2));
    dot.setAttribute("cy", y.toFixed(2));
    dot.setAttribute("r", (bright ? hlR : dotR).toFixed(2));
    dot.setAttribute("fill", bright ? hlFill : fill);
    g.appendChild(dot);
  }
}

document.querySelectorAll(".ticks-long, .ticks-short").forEach(buildTicks);
document.querySelectorAll(".dots-ring").forEach(buildDots);
