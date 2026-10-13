// Poll schedule: drag the bars of the timeline to move a poll's start. Progressive
// enhancement – the bars mirror the start fields of the form below, which alone work too.
(() => {
  const dataNode = document.getElementById("schedule-data");
  const svgRoot = document.querySelector("[data-schedule-timeline]");
  const form = document.getElementById("schedule-form");
  if (!dataNode || !svgRoot || !form) return;

  const { interval, days, geometry, polls } = JSON.parse(dataNode.textContent);
  const { width: UNITS, axis: AXIS, lane: LANE, bar: BAR } = geometry;
  const DAY = 1440;
  const SNAP = interval > 6 * 60 ? 5 : 1; // minutes
  const MIN_BAR = 4;
  const NS = "http://www.w3.org/2000/svg";
  const barsGroup = svgRoot.querySelector("[data-schedule-bars]");
  const pad = (n) => String(n).padStart(2, "0");
  const mod = (n) => ((n % interval) + interval) % interval;

  const field = (name, key) => form.elements[`${name}-${key}`];
  const readStart = (key) => {
    const clock = field("start", key)?.value;
    if (!clock) return null;
    const [hours, minutes] = clock.split(":").map(Number);
    const day = days ? Number(field("day", key)?.value || 1) : 1;
    const start = (day - 1) * DAY + hours * 60 + minutes;
    return Number.isFinite(start) && start < interval ? start : null;
  };
  const writeStart = (key, start) => {
    const day = Math.floor(start / DAY);
    const minute = start % DAY;
    field("start", key).value = `${pad(Math.floor(minute / 60))}:${pad(minute % 60)}`;
    if (days) field("day", key).value = String(day + 1);
  };

  // Rows of the timeline: overlapping polls below each other, a poll running past the end of
  // the interval also blocks its row from the start (as domain/poll_plan.lanes).
  const segments = (poll) => {
    const end = poll.start + poll.minutes;
    return end <= interval ? [[poll.start, end]] : [[poll.start, interval], [0, end - interval]];
  };
  const lanes = (placed) => {
    const taken = [];
    const result = new Map();
    [...placed].sort((a, b) => a.start - b.start).forEach((poll) => {
      const own = segments(poll);
      const free = (row) => own.every(([b, e]) => row.every(([begin, end]) => e <= begin || end <= b));
      let lane = taken.findIndex(free);
      if (lane < 0) lane = taken.push([]) - 1;
      taken[lane].push(...own);
      result.set(poll.key, lane);
    });
    return result;
  };

  // Polls that start while another one still runs (one worker: they wait).
  const late = (placed) => {
    const ordered = [...placed].sort((a, b) => a.start - b.start);
    const result = new Set();
    let freeAt = -Infinity;
    for (const lap of [0, 1]) {
      for (const poll of ordered) {
        const planned = poll.start + lap * interval;
        const begin = Math.max(planned, freeAt);
        if (lap && begin - planned >= 1) result.add(poll.key);
        freeAt = begin + poll.minutes;
      }
    }
    return result;
  };

  const element = (name, attrs, parent) => {
    const node = document.createElementNS(NS, name);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    parent.appendChild(node);
    return node;
  };

  // Drawn in screen pixels (the server's SVG stretches its 1000 units, distorting the text).
  const render = () => {
    const WIDTH = svgRoot.clientWidth || UNITS;
    const placed = polls.filter((poll) => poll.start !== null);
    const rows = lanes(placed);
    const waiting = late(placed);
    const laneCount = Math.max(0, ...rows.values()) + 1;
    const height = AXIS + laneCount * LANE + 4;
    svgRoot.setAttribute("viewBox", `0 0 ${WIDTH} ${height}`);
    svgRoot.setAttribute("height", height);
    svgRoot.querySelectorAll("[data-x]").forEach((node) => {
      const x = (Number(node.dataset.x) / UNITS) * WIDTH;
      if (node.tagName === "text") node.setAttribute("x", x + 3);
      else {
        node.setAttribute("x1", x);
        node.setAttribute("x2", x);
        node.setAttribute("y2", height);
      }
    });
    barsGroup.replaceChildren();
    for (const poll of placed) {
      const group = element("g", { class: `schedule-bar${waiting.has(poll.key) ? " is-late" : ""}`, "data-key": poll.key }, barsGroup);
      const x = (poll.start / interval) * WIDTH;
      const w = Math.max((poll.minutes / interval) * WIDTH, MIN_BAR);
      const y = AXIS + rows.get(poll.key) * LANE;
      element("rect", { x, y, width: Math.min(w, WIDTH - x), height: BAR, rx: 3 }, group);
      if (x + w > WIDTH) element("rect", { x: 0, y, width: x + w - WIDTH, height: BAR, rx: 3 }, group);
      element("text", { x: x + 3, y: y + 14 }, group).textContent = poll.number;
      element("title", {}, group).textContent = `${poll.number}. ${poll.name} · ${field("start", poll.key).value}`;
    }
  };

  // Dragging: horizontal movement only, snapped to whole minutes.
  let drag = null;
  svgRoot.addEventListener("pointerdown", (event) => {
    const group = event.target.closest(".schedule-bar");
    if (!group) return;
    const poll = polls.find((p) => p.key === group.dataset.key);
    drag = { poll, x: event.clientX, start: poll.start };
    svgRoot.setPointerCapture(event.pointerId);
    svgRoot.classList.add("is-dragging");
    event.preventDefault();
  });
  svgRoot.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const minutes = ((event.clientX - drag.x) / svgRoot.clientWidth) * interval;
    const start = mod(Math.round((drag.start + minutes) / SNAP) * SNAP);
    if (start === drag.poll.start) return;
    drag.poll.start = start;
    writeStart(drag.poll.key, start);
    render();
  });
  const endDrag = () => {
    if (!drag) return;
    form.querySelector(`tr[data-key="${drag.poll.key}"]`)?.classList.add("is-changed");
    drag = null;
    svgRoot.classList.remove("is-dragging");
  };
  svgRoot.addEventListener("pointerup", endDrag);
  svgRoot.addEventListener("pointercancel", endDrag);

  form.addEventListener("change", (event) => {
    const row = event.target.closest("tr[data-key]");
    if (!row) return;
    const poll = polls.find((p) => p.key === row.dataset.key);
    poll.start = readStart(poll.key);
    row.classList.add("is-changed");
    render();
  });

  window.addEventListener("resize", render);
  render();
})();
