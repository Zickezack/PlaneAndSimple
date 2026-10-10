// Shared drawing helpers of the price charts (plain SVG, no library – the CSP only allows own
// scripts). Stateless; loaded before price-history.js, price-calendar.js and price-chart.js.
window.PlaneChart = (() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const INK = { primary: "#1c2430", muted: "#898781", grid: "#e1e0d9", axis: "#c3c2b7", surface: "#ffffff" };
  const AXIS_FONT = "11px system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";

  function niceScale(low, high) {
    if (low === high) {
      low -= 10;
      high += 10;
    }
    const raw = (high - low) / 4;
    const magnitude = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((k) => k * magnitude).find((s) => s >= raw);
    let min = Math.floor(low / step) * step;
    if (low - min < step * 0.25) min -= step;
    min = Math.max(0, min);
    const max = Math.ceil(high / step) * step;
    const ticks = [];
    for (let tick = min; tick <= max + step / 2; tick += step) ticks.push(tick);
    return { min, max, ticks };
  }

  // The left margin fits the widest price label, so labels are never cut off.
  const measureContext = document.createElement("canvas").getContext("2d");
  function textWidth(text) {
    measureContext.font = AXIS_FONT;
    return measureContext.measureText(text).width;
  }

  function svg(name, attributes = {}, parent = null) {
    const node = document.createElementNS(SVG_NS, name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
    if (parent) parent.append(node);
    return node;
  }

  // Pinned points are diamonds with a dark outline: shape, not only colour, marks the selection.
  function diamond(cx, cy, size, color, parent) {
    const d = `M${cx},${cy - size}L${cx + size},${cy}L${cx},${cy + size}L${cx - size},${cy}Z`;
    return svg("path", { d, fill: color, stroke: INK.primary, "stroke-width": 2 }, parent);
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function colorKey(className, color) {
    const key = element("span", className);
    key.style.backgroundColor = color; // CSSOM – allowed by the CSP, unlike inline style attributes
    return key;
  }

  // Recessive chrome: hairline grid, y labels in muted ink, one baseline.
  function priceGrid(root, scale, y, left, right, format) {
    scale.ticks.forEach((tick) => {
      svg("line", { x1: left, x2: right, y1: y(tick), y2: y(tick), stroke: INK.grid, "stroke-width": 1 }, root);
      svg("text", { x: left - 8, y: y(tick) + 4, "text-anchor": "end", class: "chart-axis" }, root).textContent = format(tick);
    });
    svg("line", { x1: left, x2: right, y1: y(scale.min), y2: y(scale.min), stroke: INK.axis, "stroke-width": 1 }, root);
  }

  return { SVG_NS, INK, AXIS_FONT, niceScale, textWidth, svg, diamond, element, colorKey, priceGrid };
})();
