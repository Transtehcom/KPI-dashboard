/* Restore the palette before the first paint; switching preserves the current view and drafts. */
(() => {
  "use strict";
  const storageKey = "ttk-theme";
  const system = matchMedia("(prefers-color-scheme: dark)");
  const validTheme = value => value === "dark" || value === "light";
  let preference;
  try { preference = localStorage.getItem(storageKey); } catch (_) { /* Use the system palette. */ }

  function apply() {
    const dark = validTheme(preference) ? preference === "dark" : system.matches;
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.querySelectorAll("[data-theme-toggle]").forEach(button => {
      button.setAttribute("aria-checked", String(dark));
    });
  }

  globalThis.DashboardTheme = {
    button() {
      return `<button type="button" class="theme-toggle" data-theme-toggle role="switch" aria-checked="${document.documentElement.dataset.theme === "dark"}" aria-label="Тёмная тема" title="Переключить светлую и тёмную тему"><span class="theme-icon" aria-hidden="true"><span class="theme-rays"></span><span class="theme-orb"></span></span></button>`;
    }
  };
  apply();
  document.addEventListener("click", event => {
    if (!event.target.closest?.("[data-theme-toggle]")) return;
    preference = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    try { localStorage.setItem(storageKey, preference); } catch (_) { /* Keep the choice for this page. */ }
    apply();
  });
  system.addEventListener("change", () => { if (!validTheme(preference)) apply(); });
  globalThis.addEventListener("storage", event => {
    if (event.key !== storageKey && event.key !== null) return;
    preference = event.newValue;
    apply();
  });
})();
