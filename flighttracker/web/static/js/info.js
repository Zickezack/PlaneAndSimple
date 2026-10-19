// "i" explanations (macro ui.info). Hover and keyboard focus show the text by CSS alone; here a
// tap or click opens and closes it, Escape or a click elsewhere closes it, and the bubble is
// moved so it stays inside the window (left/right, above when there is no room below).
(() => {
  "use strict";

  const MARGIN = 8;

  const place = (info) => {
    const text = info.querySelector(".info-text");
    if (!text) return;
    text.style.setProperty("--info-shift", "0px");
    info.classList.remove("is-up");
    const box = text.getBoundingClientRect();
    if (box.width <= 1) return; // not shown (dismissed)
    // The layout width: on phones window.innerWidth grows with whatever sticks out.
    const width = document.documentElement.clientWidth;
    const height = document.documentElement.clientHeight;
    let shift = 0;
    if (box.right > width - MARGIN) shift = width - MARGIN - box.right;
    if (box.left + shift < MARGIN) shift = MARGIN - box.left;
    text.style.setProperty("--info-shift", `${Math.round(shift)}px`);
    if (box.bottom > height - MARGIN && box.top - box.height > 4 * MARGIN) {
      info.classList.add("is-up");
    }
  };

  const open = (info) => {
    info.classList.remove("is-dismissed");
    info.classList.add("is-open");
    place(info);
  };

  const close = (info) => {
    info.classList.remove("is-open");
    info.classList.add("is-dismissed"); // also while still hovered or focused
  };

  document.addEventListener("click", (event) => {
    const inside = event.target.closest(".info");
    // In a <summary> a click on the "i" or its bubble never folds the section.
    if (inside && inside.closest("summary")) event.preventDefault();
    const button = event.target.closest(".info-button");
    const clicked = button ? button.closest(".info") : null;
    document.querySelectorAll(".info.is-open").forEach((info) => {
      if (info !== clicked && !info.contains(event.target)) close(info);
    });
    if (!clicked) return;
    event.preventDefault(); // never submits or toggles anything around it
    if (clicked.classList.contains("is-open")) close(clicked);
    else open(clicked);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    document.querySelectorAll(".info").forEach((info) => {
      if (info.classList.contains("is-open") || info.matches(":hover, :focus-within")) close(info);
    });
  });

  // Hovered or focused anew: shown again (after a dismissal) and placed.
  const shown = (event) => {
    const info = event.target instanceof Element ? event.target.closest(".info") : null;
    if (!info || info.contains(event.relatedTarget)) return;
    info.classList.remove("is-dismissed");
    requestAnimationFrame(() => place(info));
  };
  document.addEventListener("mouseover", shown);
  document.addEventListener("focusin", shown);
})();
