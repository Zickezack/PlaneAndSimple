// Observed price range of the price chart; used by price-chart.js. For each departure date of a
// line: the lowest and highest price ever observed for it (all polls), shaded around the line of
// the current prices – how far each date's price has moved and where it stands now.
(() => {
  "use strict";

  const { svg } = window.PlaneChart;

  // [{date, low, high, atLowest}] for the points of one line, sorted by date. `hist` holds every
  // observed price of the point's flight; without it only the current price is known.
  function priceRange(points) {
    return [...points]
      .sort((a, b) => a.date.localeCompare(b.date))
      .map((p) => {
        const seen = p.hist?.length ? [...p.hist, p.price] : [p.price];
        const low = Math.min(...seen);
        const high = Math.max(...seen);
        // Currently at its lowest observed price after it has been higher: a good time to buy.
        return { date: p.date, low, high, atLowest: high > low && p.price <= low };
      });
  }

  // Shaded area from the lowest to the highest observed price, drawn below the price line.
  function drawRange(root, range, color, x, y) {
    if (!range.some((r) => r.high > r.low)) return;
    const edge = (key) => range.map((r) => `${x(r.date).toFixed(1)},${y(r[key]).toFixed(1)}`);
    const outline = [...edge("high"), ...edge("low").reverse()].join(" ");
    svg("polygon", { points: outline, fill: color, "fill-opacity": 0.14, stroke: color, "stroke-opacity": 0.25, "stroke-width": 1, class: "chart-band" }, root);
  }

  window.PlaneChart.priceBand = { priceRange, drawRange };
})();
