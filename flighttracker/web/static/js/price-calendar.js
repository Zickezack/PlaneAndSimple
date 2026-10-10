// Price calendar (heat map) of the tracked search page; used by price-chart.js.
(() => {
  "use strict";

  const { element } = window.PlaneChart;

  // Price calendar (heat map): cheapest price per departure day of the filtered routes and
  // stays. Sequential blue, light = cheaper; step 400 is left out because neither ink nor
  // white text reaches 4.5:1 on it. The price is printed in every cell (colour is not the
  // only cue) and the cheapest day gets a ring.
  const HEAT = ["#cde2fb", "#9ec5f4", "#6da7ec", "#256abf", "#184f95", "#0d366b"];
  const HEAT_DARK_FROM = 3; // white text from this step on

  function renderCalendar(ctx, { calendar, calendarBody }, filter, fareDates, { onPick }) {
    if (!calendar) return;
    const { data, labels, locale, utc, number, formatPrice, toDate, fill, longDate, monthTitle } = ctx;
    const weekdayNames = [...Array(7)].map((_, i) =>
      new Intl.DateTimeFormat(locale, { ...utc, weekday: "short" }).format(new Date(Date.UTC(2024, 0, 1 + i))),
    ); // 1 Jan 2024 is a Monday
    const days = new Map();
    (data.calendar || [])
      .filter((c) => (!filter.origin || c.o === filter.origin) && (!filter.destination || c.d === filter.destination)
        && (!filter.cabin || c.cabin === filter.cabin) && (!filter.stay || String(c.stay ?? "") === filter.stay))
      .forEach((c) => {
        const known = days.get(c.date);
        if (!known || c.price < known.price) days.set(c.date, c);
      });
    calendar.hidden = days.size === 0;
    calendarBody.replaceChildren();
    if (!days.size) return;
    const prices = [...days.values()].map((c) => c.price);
    const low = Math.min(...prices);
    const high = Math.max(...prices);
    const step = (price) => (high === low ? 0 : Math.min(HEAT.length - 1, Math.floor(((price - low) / (high - low)) * HEAT.length)));

    const legend = element("div", "calendar-legend");
    legend.append(element("span", null, `${labels.calendarCheaper} ${formatPrice(low)}`));
    const swatches = element("span", "calendar-swatches");
    HEAT.forEach((color) => {
      const swatch = element("span", "calendar-swatch");
      swatch.style.backgroundColor = color;
      swatches.append(swatch);
    });
    legend.append(swatches, element("span", null, `${formatPrice(high)} ${labels.calendarDearer}`));
    calendarBody.append(legend, element("p", "hint", labels.calendarHint));

    const months = element("div", "calendar-months");
    const dates = [...days.keys()].sort();
    const month = toDate(dates[0]);
    month.setUTCDate(1);
    const lastMonth = toDate(dates[dates.length - 1]);
    for (; month <= lastMonth; month.setUTCMonth(month.getUTCMonth() + 1)) {
      const block = element("div", "calendar-month");
      block.append(element("div", "calendar-title", monthTitle.format(month)));
      const grid = element("div", "calendar-grid");
      weekdayNames.forEach((name) => grid.append(element("span", "calendar-weekday", name)));
      const offset = (month.getUTCDay() + 6) % 7; // Monday first
      for (let i = 0; i < offset; i++) grid.append(element("span", "calendar-blank"));
      const day = new Date(month);
      for (; day.getUTCMonth() === month.getUTCMonth(); day.setUTCDate(day.getUTCDate() + 1)) {
        const iso = day.toISOString().slice(0, 10);
        const entry = days.get(iso);
        const cell = element(entry && fareDates.has(iso) ? "button" : "span", "calendar-day");
        cell.append(element("small", null, String(day.getUTCDate())));
        if (entry) {
          const index = step(entry.price);
          cell.style.backgroundColor = HEAT[index];
          if (index >= HEAT_DARK_FROM) cell.classList.add("is-dark");
          if (entry.price === low) cell.classList.add("is-lowest");
          if (entry.fake) cell.classList.add("is-fake");
          cell.append(element("span", "calendar-price", number.format(entry.price)));
          cell.title = fill(labels.calendarDay, { date: longDate.format(toDate(iso)), price: formatPrice(entry.price) });
          if (fareDates.has(iso)) {
            cell.type = "button";
            cell.classList.add("has-flights");
            cell.addEventListener("click", () => onPick(iso));
          }
        } else {
          cell.classList.add("is-empty");
        }
        grid.append(cell);
      }
      block.append(grid);
      months.append(block);
    }
    calendarBody.append(months);
  }

  window.PlaneChart.renderCalendar = renderCalendar;
})();
