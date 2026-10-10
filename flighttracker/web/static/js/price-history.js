// Price over time of a pinned departure date ("when to buy"); used by price-chart.js.
(() => {
  "use strict";

  const { INK, niceScale, textWidth, svg, element } = window.PlaneChart;

  // Price over time of the pinned date's flights ("when to buy"): x = poll date, a step line
  // per flight (the price holds until the next poll) in its route colour; further stay lengths
  // of the same route are dashed.
  const HISTORY_HEIGHT = 220;
  const DASHES = ["", "6 4", "2 3", "8 3 2 3"];
  function historyChart(ctx, flights, { pinned, width, colorOf, priceGrid }) {
    const { data, labels, toDate, fill, formatPrice, longDate, monthLabel, dayLabel, MARGIN, DAY_MS } = ctx;
    const lines = flights
      .filter((p) => p.seen && p.hist?.length > 1)
      .sort((a, b) => `${a.o}${a.d}`.localeCompare(`${b.o}${b.d}`) || (a.stay ?? 0) - (b.stay ?? 0));
    if (!lines.length || !data.seenBase) return null;
    const base = toDate(data.seenBase).getTime();
    const times = lines.flatMap((p) => p.seen.map((offset) => base + offset * DAY_MS));
    let start = Math.min(...times);
    let end = Math.max(...times);
    if (start === end) {
      start -= 3 * DAY_MS;
      end += 3 * DAY_MS;
    }
    const prices = lines.flatMap((p) => p.hist);
    const scale = niceScale(Math.min(...prices), Math.max(...prices));
    const left = Math.ceil(Math.max(...scale.ticks.map((t) => textWidth(formatPrice(t))))) + 16;
    const x = (time) => left + ((time - start) / (end - start)) * (width - left - MARGIN.right);
    const y = (value) => MARGIN.top + ((scale.max - value) / (scale.max - scale.min)) * (HISTORY_HEIGHT - MARGIN.top - MARGIN.bottom);
    const root = svg("svg", { viewBox: `0 0 ${width} ${HISTORY_HEIGHT}`, width, height: HISTORY_HEIGHT, role: "img", "aria-label": fill(labels.historyChart, { date: longDate.format(toDate(pinned)) }) });
    priceGrid(root, scale, y, left, width);

    // Weekly ticks for a few weeks of polls, monthly ones for longer histories.
    const weekly = end - start <= 62 * DAY_MS;
    const tick = new Date(start);
    if (weekly) tick.setUTCDate(tick.getUTCDate() + ((8 - tick.getUTCDay()) % 7)); // Mondays
    else {
      tick.setUTCDate(1);
      if (tick.getTime() < start) tick.setUTCMonth(tick.getUTCMonth() + 1);
    }
    let lastLabelX = -Infinity;
    for (; tick.getTime() <= end; weekly ? tick.setUTCDate(tick.getUTCDate() + 7) : tick.setUTCMonth(tick.getUTCMonth() + 1)) {
      const position = x(tick.getTime());
      svg("line", { x1: position, x2: position, y1: y(scale.min), y2: y(scale.min) + 5, stroke: INK.axis }, root);
      if (position - lastLabelX < 56) continue;
      svg("text", { x: position, y: HISTORY_HEIGHT - 12, "text-anchor": "middle", class: "chart-axis" }, root).textContent = (weekly ? dayLabel : monthLabel).format(tick);
      lastLabelX = position;
    }

    const legendList = element("div", "history-legend");
    const perRoute = new Map();
    lines.forEach((point) => {
      const route = `${point.o} → ${point.d}`;
      const dash = DASHES[(perRoute.get(route) ?? 0) % DASHES.length];
      perRoute.set(route, (perRoute.get(route) ?? 0) + 1);
      const color = colorOf(point);
      const coords = point.seen.map((offset, i) => [x(base + offset * DAY_MS), y(point.hist[i])]);
      const d = coords.map(([px, py], i) => (i ? `H${px.toFixed(1)}V${py.toFixed(1)}` : `M${px.toFixed(1)},${py.toFixed(1)}`)).join("");
      const name = [route, point.stay != null ? fill(labels.days, { n: point.stay }) : null].filter(Boolean).join(" · ");
      const path = svg("path", { d, fill: "none", stroke: color, "stroke-width": 2, "stroke-dasharray": dash, "stroke-linejoin": "round" }, root);
      svg("title", {}, path).textContent = `${name}: ${formatPrice(point.hist[0])} → ${formatPrice(point.price)}`;
      const [lastX, lastY] = coords[coords.length - 1];
      svg("circle", { cx: lastX, cy: lastY, r: 3.5, fill: color, stroke: INK.surface, "stroke-width": 1.5 }, root);

      const key = svg("svg", { width: 22, height: 10, "aria-hidden": "true" });
      svg("line", { x1: 1, x2: 21, y1: 5, y2: 5, stroke: color, "stroke-width": 2, "stroke-dasharray": dash }, key);
      const item = element("span", "history-legend-item");
      item.append(key, element("span", null, name));
      legendList.append(item);
    });

    const wrap = element("div", "selection-history");
    wrap.append(element("strong", null, labels.historyTitle), element("p", "hint", labels.historyHint), legendList, root);
    return wrap;
  }

  window.PlaneChart.historyChart = historyChart;
})();
