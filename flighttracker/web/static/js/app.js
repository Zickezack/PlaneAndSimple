// Plane and simple – progressive enhancement only: every form also works without JavaScript.
// Loaded from our own origin because the CSP forbids inline and third-party scripts.
(() => {
  "use strict";

  const DEBOUNCE_MS = 150;

  // "Clear selection" buttons for the airport checkboxes of a country.
  document.querySelectorAll("[data-clear-choices]").forEach((button) => {
    button.hidden = false;
    button.addEventListener("click", () => {
      const name = CSS.escape(button.dataset.clearChoices);
      button.form.querySelectorAll(`input[name="${name}"]`).forEach((box) => {
        box.checked = false;
      });
    });
  });

  const tripLegList = document.querySelector("[data-trip-legs]");
  const addTripLeg = document.querySelector("[data-add-trip-leg]");
  const tripLegTemplate = document.querySelector("[data-trip-leg-template]");
  if (tripLegList && addTripLeg && tripLegTemplate) {
    const maxLegs = 8;
    const updateTripLegs = () => {
      const legs = [...tripLegList.querySelectorAll("[data-trip-leg]")];
      legs.forEach((leg, index) => {
        leg.querySelector("legend").textContent = tripLegList.dataset.legLabel.replace(
          "{number}",
          index + 1,
        );
        const layover = leg.querySelector(".trip-layover");
        if (layover) layover.hidden = index === 0;
        leg.querySelectorAll("[data-trip-airport-country]").forEach((country) => {
          const code = country.dataset.tripAirportCountry;
          const reviewed = country.querySelector("[data-trip-airport-reviewed]");
          if (reviewed) reviewed.value = `${index}:${code}`;
          country.querySelectorAll('input[type="checkbox"]').forEach((checkbox) => {
            checkbox.name = `trip_airports_${index}_${code}`;
          });
        });
        leg.querySelectorAll(".grid-2 > .field").forEach((field, fieldIndex) => {
          const label = field.querySelector("label");
          const input = field.querySelector("input");
          if (!label || !input) return;
          input.id = `trip-leg-${index}-${fieldIndex}`;
          label.htmlFor = input.id;
        });
      });
      addTripLeg.disabled = legs.length >= maxLegs;
    };

    addTripLeg.addEventListener("click", () => {
      if (tripLegList.querySelectorAll("[data-trip-leg]").length >= maxLegs) return;
      tripLegList.append(tripLegTemplate.content.cloneNode(true));
      updateTripLegs();
      tripLegList.lastElementChild.querySelectorAll("input[data-suggest]").forEach(setupSuggest);
    });
    tripLegList.addEventListener("click", (event) => {
      const remove = event.target.closest("[data-remove-trip-leg]");
      if (!remove || tripLegList.querySelectorAll("[data-trip-leg]").length <= 2) return;
      remove.closest("[data-trip-leg]").remove();
      updateTripLegs();
    });
    updateTripLegs();
  }

  // Suggestion dropdowns for inputs with data-suggest="<JSON endpoint>".
  document.querySelectorAll("input[data-suggest]").forEach(setupSuggest);

  function setupSuggest(input) {
    const multiple = input.hasAttribute("data-suggest-multiple");
    const list = document.createElement("ul");
    list.className = "suggest-list";
    list.id = `${input.id}-suggestions`;
    list.setAttribute("role", "listbox");
    list.hidden = true;
    input.setAttribute("aria-controls", list.id);
    input.after(list);

    let items = [];
    let active = -1;
    let timer = null;
    let requestNo = 0;

    // In comma-separated fields only the entry being typed is completed.
    const currentTerm = () =>
      multiple ? input.value.split(",").pop().trim() : input.value.trim();

    function close() {
      list.hidden = true;
      active = -1;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }

    function render() {
      list.replaceChildren(
        ...items.map((item, index) => {
          const option = document.createElement("li");
          option.id = `${list.id}-${index}`;
          option.setAttribute("role", "option");
          option.setAttribute("aria-selected", String(index === active));
          const code = document.createElement("strong");
          code.textContent = item.value;
          const label = document.createElement("span");
          label.textContent = item.label;
          option.append(code, label);
          if (item.kind) {
            const kind = document.createElement("small");
            kind.textContent = item.kind;
            option.append(kind);
          }
          // mousedown instead of click: fires before the input loses focus.
          option.addEventListener("mousedown", (event) => {
            event.preventDefault();
            choose(index);
          });
          return option;
        }),
      );
      list.hidden = items.length === 0;
      input.setAttribute("aria-expanded", String(!list.hidden));
      if (active >= 0) {
        input.setAttribute("aria-activedescendant", `${list.id}-${active}`);
      } else {
        input.removeAttribute("aria-activedescendant");
      }
    }

    function choose(index) {
      const value = items[index].value;
      if (multiple) {
        const done = input.value.split(",").slice(0, -1).map((part) => part.trim()).filter(Boolean);
        input.value = `${[...done, value].join(", ")}, `;
      } else {
        input.value = value;
      }
      items = [];
      close();
      input.focus();
    }

    async function load() {
      const term = currentTerm();
      const current = ++requestNo;
      if (!term) {
        items = [];
        close();
        return;
      }
      try {
        const response = await fetch(`${input.dataset.suggest}?q=${encodeURIComponent(term)}`, {
          headers: { Accept: "application/json" },
          credentials: "same-origin",
        });
        const isJson = (response.headers.get("content-type") || "").includes("application/json");
        const data = response.ok && isJson ? await response.json() : [];
        if (current !== requestNo) return; // a newer request is on its way
        items = Array.isArray(data) ? data : [];
        active = -1;
        render();
      } catch {
        items = [];
        close();
      }
    }

    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(load, DEBOUNCE_MS);
    });
    input.addEventListener("blur", close);
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (!items.length) return;
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        active = (active + step + items.length) % items.length;
        render();
      } else if (event.key === "Enter" && !list.hidden && active >= 0) {
        event.preventDefault(); // pick the suggestion instead of submitting the form
        choose(active);
      } else if (event.key === "Escape" && !list.hidden) {
        event.preventDefault();
        close();
      }
    });
  }
})();
