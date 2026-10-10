// Prices of the Suchabo detail page: chart (plain SVG, no library – the CSP only allows own
// scripts), pinned date selection and the grouping of the flights table.
// Data comes from <script type="application/json" id="price-chart-data"> (see web/flights.py).
// Without JavaScript the table lists every flight, so no value depends on this script.
(() => {
  "use strict";

  const dataNode = document.getElementById("price-chart-data");
  const figure = document.querySelector("[data-price-chart]");
  if (!dataNode || !figure) return;

  const data = JSON.parse(dataNode.textContent);
  const labels = data.labels;
  const locale = document.documentElement.lang === "de" ? "de-CH" : "en";
  // Categorical palette, validated (dataviz six checks) on the white card surface.
  // The order is part of the colour-blind safety – never reorder or extend it.
  const COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"];
  const PREVIOUS_YEAR_COLOR = "#b5b3ab"; // de-emphasis gray: context, not a series to compare
  const MAX_SERIES = COLORS.length;
  const HEIGHT = 300;
  const MARGIN = { top: 24, right: 20, bottom: 36 };

  const number = new Intl.NumberFormat(locale, { maximumFractionDigits: 0 });
  const exactNumber = new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const formatPrice = (value) => `${number.format(value)} ${data.currency}`;
  const formatChangePrice = (value) => `${exactNumber.format(value)} ${data.currency}`;
  const utc = { timeZone: "UTC" };
  const longDate = new Intl.DateTimeFormat(locale, { ...utc, weekday: "short", day: "numeric", month: "short", year: "numeric" });
  const shortDate = new Intl.DateTimeFormat(locale, { ...utc, weekday: "short", day: "numeric", month: "short" });
  const monthLabel = new Intl.DateTimeFormat(locale, { ...utc, month: "short", year: "numeric" });
  const monthTitle = new Intl.DateTimeFormat(locale, { ...utc, month: "long", year: "numeric" });
  const dayLabel = new Intl.DateTimeFormat(locale, { ...utc, day: "numeric", month: "short" });
  const DAY_MS = 86400000;
  const toDate = (iso) => new Date(`${iso}T00:00:00Z`);
  const fill = (template, values) => template.replace(/\{(\w+)\}/g, (_, key) => values[key] ?? "");
  const { INK, niceScale, textWidth, svg, diamond, element, colorKey } = window.PlaneChart;
  // What the history chart and the price calendar (own files) need from this page.
  const ctx = { data, labels, locale, utc, number, formatPrice, toDate, fill, longDate, monthLabel, monthTitle, dayLabel, MARGIN, DAY_MS };
  const priceGrid = (root, scale, y, left, width) =>
    window.PlaneChart.priceGrid(root, scale, y, left, width - MARGIN.right, formatPrice);

  const filterRow = document.querySelector("[data-price-filters]");
  const viewSwitch = document.querySelector("[data-view-switch]");
  // Filters are only rendered when they offer a real choice, so each may be missing.
  const select = (name) => document.querySelector(`select[name="${name}"]`);
  const controls = {
    origin: select("filter_origin"),
    destination: select("filter_destination"),
    cabin: select("filter_cabin"),
    stay: select("filter_stay"),
    view: select("filter_view"),
  };
  const valueOf = (control) => (control ? control.value : "");
  const plot = figure.querySelector("[data-chart-plot]");
  const legend = figure.querySelector("[data-chart-legend]");
  const tooltip = figure.querySelector("[data-chart-tooltip]");
  const selection = document.querySelector("[data-chart-selection]");
  const calendar = document.querySelector("[data-price-calendar]");
  const calendarBody = document.querySelector("[data-price-calendar-body]");
  const table = document.querySelector("[data-price-table]");
  const tableBody = table.tBodies[0];
  const emptyNote = document.querySelector("[data-filter-empty]");
  const rows = [...tableBody.rows];
  // Keep trend controls available whenever an individual sparkline can be shown.
  const trendLabel = figure.querySelector("[data-chart-trends-toggle]");
  const trendToggle = figure.querySelector("[data-chart-trends]");
  const hasTrends = data.points.some((p) => p.hist?.length > 1);
  const showTrends = () => hasTrends && trendToggle.checked;
  if (trendLabel) trendLabel.hidden = !hasTrends;

  filterRow.hidden = filterRow.childElementCount === 0;
  if (viewSwitch) viewSwitch.hidden = false;
  figure.hidden = false;

  let pinned = null; // departure date kept below the chart after a click
  const expandedGroups = new Set(); // month groups opened in the "cheapest per month" view

  function currentFilter() {
    return {
      origin: valueOf(controls.origin),
      destination: valueOf(controls.destination),
      cabin: valueOf(controls.cabin),
      stay: valueOf(controls.stay),
    };
  }

  function matches(filter, item) {
    return (
      (!filter.origin || item.origin === filter.origin) &&
      (!filter.destination || item.destination === filter.destination) &&
      (!filter.cabin || item.cabin === filter.cabin) &&
      (!filter.stay || String(item.stay ?? "") === filter.stay)
    );
  }

  const asItem = (point) => ({ origin: point.o, destination: point.d, cabin: point.cabin, stay: point.stay });
  const byDate = (a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0);

  function cheapestPerDate(points, value) {
    const best = new Map();
    points.forEach((point) => {
      const price = value(point);
      if (price == null) return;
      if (!best.has(point.date) || price < best.get(point.date).price) best.set(point.date, { ...point, price });
    });
    return [...best.values()].sort(byDate);
  }

  // One line per route (cheapest stay per date); past eight routes a single "cheapest of n
  // routes" line – never a 9th hue.
  function buildSeries(filter) {
    const points = data.points.filter((p) => matches(filter, asItem(p)));
    const routes = new Map();
    points.forEach((point) => {
      const route = `${point.o}-${point.d}`;
      if (!routes.has(route)) routes.set(route, []);
      routes.get(route).push(point);
    });
    const names = [...routes.keys()].sort();
    let series;
    if (names.length <= MAX_SERIES) {
      // Colour follows the route, not its position in the current selection (when possible).
      const stable = data.routes.length <= MAX_SERIES;
      series = names.map((route, index) => ({
        name: route.replace("-", " → "),
        color: COLORS[stable ? data.routes.indexOf(route) : index],
        points: cheapestPerDate(routes.get(route), (p) => p.price),
      }));
    } else {
      series = [{
        name: fill(labels.aggregate, { n: names.length }),
        color: COLORS[0],
        points: cheapestPerDate(points, (p) => p.price),
        aggregate: true,
      }];
    }
    // Previous year only as context for a single line – with several it would be noise.
    const previousYear = series.length === 1 ? cheapestPerDate(points, (p) => p.py) : [];
    return { series, previousYear, points };
  }

  function renderLegend(series, previousYear) {
    legend.replaceChildren();
    const items = series.length > 1 ? series.map((s) => [s.name, s.color]) : [];
    // The combined line must say what it is – and how to get the per-route lines.
    const aggregate = series.length === 1 && series[0].aggregate;
    if ((aggregate || previousYear.length) && !items.length) items.push([series[0].name, series[0].color]);
    if (previousYear.length) items.push([labels.previousYear, PREVIOUS_YEAR_COLOR]);
    items.forEach(([name, color]) => {
      const item = element("span", "legend-item");
      item.append(colorKey("line-key", color), document.createTextNode(name));
      legend.append(item);
    });
    if (aggregate) legend.append(element("span", "legend-hint", labels.aggregateHint));
    if (series.some((s) => s.points.some((p) => p.first != null)) && showTrends()) {
      legend.append(element("span", "legend-hint", labels.arrowHint));
    }
    legend.hidden = legend.childElementCount === 0;
  }

  function stopsText(stops) {
    if (stops == null) return null;
    if (stops === 0) return labels.direct;
    return stops === 1 ? labels.oneStop : fill(labels.stops, { n: stops });
  }

  function flightLine(point) {
    const parts = [];
    if (point.ret) {
      parts.push(`${fill(labels.returnOn, { date: shortDate.format(toDate(point.ret)) })} (${fill(labels.days, { n: point.stay })})`);
    }
    if (point.airline) parts.push(point.airline);
    if (point.flights.length) parts.push(point.flights.join(", "));
    if (point.dep && point.arr) parts.push(`${point.dep}–${point.arr}${point.arrDays ? ` +${point.arrDays}` : ""}`);
    if (point.dur) parts.push(point.dur);
    const stops = stopsText(point.stops);
    if (stops) parts.push(stops);
    point.via.forEach((layover) => parts.push(`${labels.via} ${layover.airport} (${layover.wait})`));
    return parts.join(" · ");
  }

  function priceRow(key, value, name, fake) {
    const row = element("div", "tt-row");
    row.append(key, element("strong", null, value), element("span", "tt-name", name));
    if (fake) row.append(element("span", "fake-tag", labels.invented));
    return row;
  }

  function periodChangeElement(change) {
    const direction = change.amount < 0 ? "down" : change.amount > 0 ? "up" : "flat";
    const arrow = change.amount < 0 ? "▼" : change.amount > 0 ? "▲" : "=";
    return element(
      "small",
      `trend-change ${direction === "down" ? "delta-down" : direction === "up" ? "delta-up" : "hint"}`,
      `${change.period} ${arrow} ${formatChangePrice(Math.abs(change.amount))}`,
    );
  }

  function periodChangesElement(changes) {
    const list = element("span", "trend-changes");
    changes.forEach((change) => list.append(periodChangeElement(change)));
    return list;
  }

  // Same sparkline as the flights table (web/flights.py): first price, curve, current price.
  function sparkline(history) {
    const width = 84;
    const height = 24;
    const pad = 3;
    const low = Math.min(...history);
    const span = Math.max(...history) - low;
    const step = (width - 2 * pad) / (history.length - 1);
    const coords = history.map((value, i) => [pad + i * step, span === 0 ? height / 2 : pad + (height - 2 * pad) * (1 - (value - low) / span)]);
    const first = history[0];
    const last = history[history.length - 1];
    const direction = last < first ? "down" : last > first ? "up" : "flat";
    const wrap = element("span", `spark-wrap spark-${direction}`);
    wrap.title = labels.firstToCurrent;
    const chart = svg("svg", { class: `spark spark-${direction}`, width, height, viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
    svg("polyline", { points: coords.map(([px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join(" "), fill: "none", stroke: "currentColor", "stroke-width": 1.5, "stroke-linejoin": "round", "stroke-linecap": "round" }, chart);
    const [lastX, lastY] = coords[coords.length - 1];
    svg("circle", { cx: lastX.toFixed(1), cy: lastY.toFixed(1), r: 2.2, fill: "currentColor" }, chart);
    wrap.append(element("small", null, number.format(first)), chart, element("small", null, number.format(last)));
    return wrap;
  }

  const historyChart = (flights) =>
    window.PlaneChart.historyChart(ctx, flights, { pinned, width: Math.max(selection.clientWidth - 32, 320), colorOf, priceGrid });
  const renderCalendar = (filter, fareDates) =>
    window.PlaneChart.renderCalendar(ctx, { calendar, calendarBody }, filter, fareDates, {
      onPick: (iso) => pinned !== iso && togglePin(iso),
    });

  let view = null; // geometry of the current render, used by the hover and pin layers

  function render() {
    const filter = currentFilter();
    const { series, previousYear, points } = buildSeries(filter);
    renderLegend(series, previousYear);
    plot.replaceChildren();
    tooltip.hidden = true;
    view = null;
    renderTable(filter);
    colorRouteDots(series);
    renderCalendar(filter, new Set(points.map((p) => p.date)));
    if (!series.length) {
      plot.append(element("p", "chart-empty", labels.empty));
      pinned = null;
      renderSelection([]);
      return;
    }

    const width = Math.max(plot.clientWidth, 320);
    const dates = [...new Set(series.flatMap((s) => s.points.map((p) => p.date)))].sort();
    let start = toDate(dates[0]).getTime();
    let end = toDate(dates[dates.length - 1]).getTime();
    if (start === end) {
      start -= 3 * 86400000;
      end += 3 * 86400000;
    }
    const values = series.flatMap((s) => s.points.map((p) => p.price)).concat(previousYear.map((p) => p.price));
    if (showTrends()) series.forEach((s) => s.points.forEach((p) => p.first != null && values.push(p.first)));
    const scale = niceScale(Math.min(...values), Math.max(...values));
    const left = Math.ceil(Math.max(...scale.ticks.map((t) => textWidth(formatPrice(t))))) + 16;
    const x = (iso) => left + ((toDate(iso).getTime() - start) / (end - start)) * (width - left - MARGIN.right);
    const y = (value) => MARGIN.top + ((scale.max - value) / (scale.max - scale.min)) * (HEIGHT - MARGIN.top - MARGIN.bottom);

    const root = svg("svg", { viewBox: `0 0 ${width} ${HEIGHT}`, width, height: HEIGHT, tabindex: "0", role: "img", "aria-label": labels.chart });

    priceGrid(root, scale, y, left, width);
    const month = new Date(start);
    month.setUTCDate(1);
    if (month.getTime() < start) month.setUTCMonth(month.getUTCMonth() + 1);
    let lastLabelX = -Infinity;
    for (; month.getTime() <= end; month.setUTCMonth(month.getUTCMonth() + 1)) {
      const position = x(month.toISOString().slice(0, 10));
      svg("line", { x1: position, x2: position, y1: y(scale.min), y2: y(scale.min) + 5, stroke: INK.axis }, root);
      if (position - lastLabelX < 56) continue;
      svg("text", { x: position, y: HEIGHT - 12, "text-anchor": "middle", class: "chart-axis" }, root).textContent = monthLabel.format(month);
      lastLabelX = position;
    }

    const path = (list) => list.map((p, i) => `${i ? "L" : "M"}${x(p.date).toFixed(1)},${y(p.price).toFixed(1)}`).join("");
    const line = (list, color) => svg("path", { d: path(list), fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, root);
    if (previousYear.length > 1) line(previousYear, PREVIOUS_YEAR_COLOR);
    series.forEach((s) => {
      if (s.points.length > 1) line(s.points, s.color);
      // Arrow from the first observed price to the current one: direction and size of the change.
      if (showTrends()) {
        s.points.forEach((p) => {
          if (p.first == null) return;
          const px = x(p.date);
          const from = y(p.first);
          const to = y(p.price);
          if (Math.abs(to - from) < 14) return;
          const dir = Math.sign(to - from);
          const tip = to - dir * 7;
          const base = tip - dir * 7;
          svg("line", { x1: px, x2: px, y1: from, y2: base, stroke: s.color, "stroke-width": 1.5, "stroke-opacity": 0.6 }, root);
          svg("path", { d: `M${px},${tip}L${px - 3.5},${base}L${px + 3.5},${base}Z`, fill: s.color, "fill-opacity": 0.85 }, root);
        });
      }
      // 2px surface ring keeps markers legible where lines cross.
      s.points.forEach((p) => svg("circle", { cx: x(p.date), cy: y(p.price), r: 4, fill: s.color, stroke: INK.surface, "stroke-width": 2 }, root));
    });

    // Selective direct label: only the lowest price of the visible selection (below its marker).
    const lowest = series.flatMap((s) => s.points).reduce((a, b) => (b.price < a.price ? b : a));
    const labelX = Math.min(Math.max(x(lowest.date), left + 60), width - MARGIN.right - 60);
    const labelY = Math.min(y(lowest.price) + 20, y(scale.min) - 6);
    svg("text", { x: labelX, y: labelY, "text-anchor": "middle", class: "chart-label" }, root).textContent = `${labels.lowest} ${formatPrice(lowest.price)}`;

    const pinLayer = svg("g", {}, root);
    const crosshair = svg("line", { y1: MARGIN.top, y2: y(scale.min), stroke: INK.muted, "stroke-width": 1, visibility: "hidden" }, root);
    const highlights = svg("g", {}, root);
    // The whole plot is the hit target: the crosshair snaps to the nearest departure date.
    const overlay = svg("rect", { x: left, y: 0, width: width - left - MARGIN.right, height: HEIGHT, fill: "transparent", class: "chart-overlay" }, root);

    plot.append(root);
    view = { root, dates, series, previousYear, points, x, y, width, crosshair, highlights, pinLayer, baseline: y(scale.min), active: -1 };
    if (pinned && !dates.includes(pinned)) pinned = null;
    drawPin();
    renderSelection(points);

    const nearest = (event) => {
      const box = root.getBoundingClientRect();
      const px = ((event.clientX - box.left) / box.width) * width;
      let best = 0;
      dates.forEach((date, index) => {
        if (Math.abs(x(date) - px) < Math.abs(x(dates[best]) - px)) best = index;
      });
      return best;
    };
    overlay.addEventListener("pointermove", (event) => show(nearest(event)));
    overlay.addEventListener("pointerleave", hide);
    overlay.addEventListener("click", (event) => togglePin(dates[nearest(event)]));
    root.addEventListener("blur", hide);
    root.addEventListener("focus", () => show(Math.max(view.active, 0)));
    root.addEventListener("keydown", (event) => {
      if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
        event.preventDefault();
        const step = event.key === "ArrowRight" ? 1 : -1;
        show(Math.min(Math.max(view.active + step, 0), dates.length - 1));
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        if (view.active >= 0) togglePin(dates[view.active]);
      } else if (event.key === "Escape") {
        hide();
      }
    });
  }

  function hide() {
    if (!view) return;
    view.crosshair.setAttribute("visibility", "hidden");
    view.highlights.replaceChildren();
    tooltip.hidden = true;
  }

  function show(index) {
    const { dates, series, previousYear, x, y, crosshair, highlights, root, width } = view;
    view.active = index;
    const date = dates[index];
    const position = x(date);
    crosshair.setAttribute("x1", position);
    crosshair.setAttribute("x2", position);
    crosshair.setAttribute("visibility", "visible");
    highlights.replaceChildren();

    // One tooltip, every series at this date – cheapest first; the value leads.
    const entries = series
      .map((s) => ({ s, point: s.points.find((p) => p.date === date) }))
      .filter((entry) => entry.point)
      .sort((a, b) => a.point.price - b.point.price);
    tooltip.replaceChildren(element("div", "tt-date", longDate.format(toDate(date))));
    entries.forEach(({ s, point }) => {
      svg("circle", { cx: position, cy: y(point.price), r: 6, fill: s.color, stroke: INK.surface, "stroke-width": 2 }, highlights);
      tooltip.append(priceRow(colorKey("line-key", s.color), formatPrice(point.price), series.length > 1 ? s.name : `${point.o} → ${point.d}`, point.fake));
      tooltip.append(element("div", "tt-note", flightLine(point)));
      if (showTrends() && point.changes?.length) {
        tooltip.append(periodChangesElement(point.changes));
      }
    });
    const lastYear = previousYear.find((p) => p.date === date);
    if (lastYear) tooltip.append(priceRow(colorKey("line-key", PREVIOUS_YEAR_COLOR), formatPrice(lastYear.price), labels.previousYear, false));
    if (date !== pinned) tooltip.append(element("div", "tt-hint", labels.clickToPin));

    tooltip.hidden = false;
    // Place beside the crosshair, flipped to the left near the right edge.
    const scaleFactor = root.getBoundingClientRect().width / width;
    const leftPx = position * scaleFactor + plot.offsetLeft;
    const flip = leftPx + tooltip.offsetWidth + 16 > figure.clientWidth;
    tooltip.style.left = `${flip ? leftPx - tooltip.offsetWidth - 12 : leftPx + 12}px`;
    tooltip.style.top = `${plot.offsetTop + MARGIN.top}px`;
  }

  function togglePin(date) {
    pinned = pinned === date ? null : date;
    drawPin();
    renderSelection(view ? view.points : []);
    highlightRows();
    if (view && view.active >= 0) show(view.active);
    if (pinned) selection.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function drawPin() {
    if (!view) return;
    view.pinLayer.replaceChildren();
    if (!pinned) return;
    const position = view.x(pinned);
    svg("line", { x1: position, x2: position, y1: MARGIN.top, y2: view.baseline, stroke: INK.primary, "stroke-width": 1.5 }, view.pinLayer);
    view.series.forEach((s) => {
      const point = s.points.find((p) => p.date === pinned);
      if (point) diamond(position, view.y(point.price), 7, s.color, view.pinLayer);
    });
  }

  function colorOf(point) {
    if (!view) return COLORS[0];
    const route = `${point.o} → ${point.d}`;
    const series = view.series.find((s) => s.aggregate || s.name === route);
    return series ? series.color : COLORS[0];
  }

  // The pinned date's flights (every route and stay of the filtered selection) stay visible
  // below the chart – with working links, unlike the hover tooltip.
  function renderSelection(points) {
    selection.replaceChildren();
    selection.hidden = !pinned;
    if (!pinned) return;
    const header = element("div", "selection-header");
    header.append(element("strong", null, fill(labels.selected, { date: longDate.format(toDate(pinned)) })));
    const close = element("button", "btn btn-ghost", labels.clearSelection);
    close.type = "button";
    close.addEventListener("click", () => togglePin(pinned));
    header.append(close);
    selection.append(header);
    // One compact line per flight; everything else is on its detail page.
    points
      .filter((p) => p.date === pinned)
      .sort((a, b) => a.price - b.price)
      .forEach((point) => {
        const item = element("div", "selection-item");
        const parts = [
          point.stay != null ? fill(labels.days, { n: point.stay }) : null,
          [point.airline, point.flights.join(", ")].filter(Boolean).join(" · "),
          stopsText(point.stops),
        ].filter(Boolean);
        item.append(
          colorKey("diamond-key", colorOf(point)),
          element("strong", null, formatPrice(point.price)),
        );
        if (showTrends() && point.hist) item.append(sparkline(point.hist));
        if (showTrends() && point.changes?.length) {
          item.append(periodChangesElement(point.changes));
        }
        item.append(
          element("span", "selection-route", `${point.o} → ${point.d}`),
          element("span", "hint", parts.join(" · ")),
        );
        if (point.fake) item.append(element("span", "fake-tag", labels.invented));
        if (point.id != null) {
          const link = element("a", "selection-link", `${labels.details} →`);
          link.href = `${data.detailUrl}${point.id}`;
          item.append(link);
        }
        selection.append(item);
      });
    if (showTrends()) {
      const history = historyChart(points.filter((p) => p.date === pinned));
      if (history) selection.append(history);
    }
  }

  // Table: filter rows, then either show every flight or, per month and route, the cheapest
  // one with a button to unfold the other departure dates. Rows come sorted by date.
  function renderTable(filter) {
    const monthly = valueOf(controls.view) !== "all";
    tableBody.querySelectorAll("tr.month-row").forEach((row) => row.remove());
    tableBody.querySelectorAll(".group-toggle").forEach((button) => button.remove());
    const groups = new Map();
    rows.forEach((row) => {
      row.hidden = true;
      row.classList.remove("group-member");
      if (!matches(filter, row.dataset)) return;
      const key = `${row.dataset.month}|${row.dataset.origin}|${row.dataset.destination}|${row.dataset.cabin}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    });
    // Order: by date for "all flights"; per month and route with the cheapest first otherwise,
    // so an unfolded group sits right below its summary row.
    const price = (row) => Number(row.dataset.price);
    const ordered = rows.slice().sort((a, b) => {
      const month = a.dataset.month.localeCompare(b.dataset.month);
      if (month) return month;
      if (monthly) {
        const route = `${a.dataset.origin}${a.dataset.destination}${a.dataset.cabin}`.localeCompare(`${b.dataset.origin}${b.dataset.destination}${b.dataset.cabin}`);
        return route || price(a) - price(b);
      }
      return a.dataset.date.localeCompare(b.dataset.date) || price(a) - price(b);
    });
    ordered.forEach((row) => tableBody.append(row));
    groups.forEach((members, key) => {
      const best = members.reduce((a, b) => (price(b) < price(a) ? b : a));
      const open = !monthly || expandedGroups.has(key);
      members.forEach((row) => {
        row.hidden = !(open || row === best);
        if (monthly && row !== best) row.classList.add("group-member");
      });
      if (monthly && members.length > 1) {
        const text = open ? labels.fewerDates : fill(labels.moreDates, { n: members.length - 1 });
        const button = element("button", "btn btn-ghost group-toggle", text);
        button.type = "button";
        button.setAttribute("aria-expanded", String(open));
        button.addEventListener("click", () => {
          if (expandedGroups.has(key)) expandedGroups.delete(key);
          else expandedGroups.add(key);
          renderTable(currentFilter());
          colorRouteDots(view ? view.series : []);
        });
        best.cells[0].append(button);
      }
    });
    // A header row before the first visible row of every month.
    let currentMonth = null;
    let visible = 0;
    ordered.forEach((row) => {
      if (row.hidden) return;
      visible += 1;
      if (row.dataset.month === currentMonth) return;
      currentMonth = row.dataset.month;
      const header = element("tr", "month-row");
      const cell = element("th", null, monthTitle.format(toDate(`${currentMonth}-01`)));
      cell.colSpan = table.tHead.rows[0].cells.length;
      header.append(cell);
      row.before(header);
    });
    if (emptyNote) emptyNote.hidden = visible > 0;
    highlightRows();
  }

  // Each table row carries the colour of its line in the chart (dot = chart marker).
  function colorRouteDots(series) {
    const colors = new Map(series.map((s) => [s.name.replace(" → ", "-"), s.color]));
    const aggregate = series.length === 1 && series[0].aggregate ? series[0].color : null;
    tableBody.querySelectorAll(".route-dot").forEach((dot) => {
      const color = aggregate || colors.get(dot.dataset.route);
      dot.style.backgroundColor = color || "transparent";
      dot.hidden = !color;
    });
  }

  function highlightRows() {
    rows.forEach((row) => row.classList.toggle("is-selected", Boolean(pinned) && row.dataset.date === pinned));
  }

  Object.values(controls).forEach((control) => control && control.addEventListener("change", render));
  if (trendToggle) trendToggle.addEventListener("change", render);
  // Re-render only when the width changes (not on the height change of our own render).
  let pending = null;
  let renderedWidth = plot.clientWidth;
  new ResizeObserver(() => {
    if (plot.clientWidth === renderedWidth) return;
    renderedWidth = plot.clientWidth;
    cancelAnimationFrame(pending);
    pending = requestAnimationFrame(render);
  }).observe(plot);
  render();
})();
