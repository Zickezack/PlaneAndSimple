// Platform Settings sections: save and test without reloading the page. Every button of a
// section (Save, Check the login, Send a test mail, Test the connection) sends the values as
// entered; the answer (JSON) shows up right below the buttons, wrong fields are marked where
// they are – nothing typed is lost and the page does not move. Without JavaScript the same
// forms post normally (routes/settings_actions.py).
(() => {
  "use strict";

  const forms = document.querySelectorAll("form[data-settings-section]");
  if (!forms.length) return;

  const alertBox = (text, category) => {
    const box = document.createElement("div");
    box.className = `alert alert-${category}`;
    box.setAttribute("role", "status");
    box.textContent = text;
    return box;
  };

  const showErrors = (form, errors) => {
    form.querySelectorAll(".field-error").forEach((node) => node.remove());
    form.querySelectorAll(".has-error").forEach((node) => node.classList.remove("has-error"));
    Object.entries(errors || {}).forEach(([key, text]) => {
      const field = form.querySelector(`[data-setting-field="${CSS.escape(key)}"]`);
      if (!field) return;
      field.classList.add("has-error");
      const note = document.createElement("small");
      note.className = "field-error";
      note.textContent = text;
      field.append(note);
    });
  };

  // After a save: secrets are not shown again – empty their inputs, tell whether one is stored.
  const updateSecrets = (form, secrets, labels) => {
    Object.entries(secrets || {}).forEach(([key, stored]) => {
      const input = form.elements[key];
      if (input) {
        input.value = "";
        input.placeholder = stored ? labels.set : labels.unset;
      }
      const clear = form.elements[`${key}_clear`];
      if (clear) clear.checked = false;
    });
  };

  forms.forEach((form) => {
    const result = form.querySelector("[data-section-result]");
    const buttons = [...form.querySelectorAll("button[type=submit]")];
    const labels = { set: form.dataset.secretSet || "", unset: form.dataset.secretUnset || "" };

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submitter = event.submitter;
      const url = submitter?.getAttribute("formaction") || form.getAttribute("action");
      const body = new FormData(form, submitter);
      buttons.forEach((button) => { button.disabled = true; });
      result.replaceChildren(alertBox(form.dataset.working, "info"));
      try {
        const response = await fetch(url, {
          method: "POST",
          body,
          headers: { Accept: "application/json" },
          credentials: "same-origin",
        });
        const data = await response.json();
        showErrors(form, data.errors);
        result.replaceChildren(...data.messages.map((m) => alertBox(m.text, m.category)));
        updateSecrets(form, data.secrets, labels);
        const setup = document.getElementById("setup");
        if (data.setup_html !== undefined) {
          const holder = document.createElement("div");
          holder.innerHTML = data.setup_html.trim();
          const fresh = holder.querySelector("#setup");
          if (setup && fresh) setup.replaceWith(fresh);
          else if (setup && !fresh) setup.remove();
        }
      } catch {
        result.replaceChildren(alertBox(form.dataset.failed, "error"));
      } finally {
        buttons.forEach((button) => { button.disabled = false; });
      }
    });
  });
})();
