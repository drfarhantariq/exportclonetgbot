/* Apply the saved theme before the stylesheet paints, including Telegram chrome. */
(() => {
  const key = "msz-workspace-theme";
  const telegram = window.Telegram?.WebApp;
  const system = window.matchMedia?.("(prefers-color-scheme: dark)");
  let preference;
  try {
    const saved = localStorage.getItem(key);
    if (saved === "light" || saved === "dark") preference = saved;
  } catch (_) {}

  function apply() {
    const theme =
      preference ||
      telegram?.colorScheme ||
      (system?.matches ? "dark" : "light");
    document.documentElement.dataset.theme = theme;
    const background = theme === "light" ? "#f4f7fb" : "#10141d";
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute("content", background);
    for (const method of [
      "setHeaderColor",
      "setBackgroundColor",
      "setBottomBarColor",
    ]) {
      try {
        telegram?.[method]?.(background);
      } catch (_) {}
    }
    const button = document.getElementById("theme-toggle");
    if (button) {
      const next = theme === "light" ? "dark" : "light";
      const label = `Switch to ${next} theme`;
      button.setAttribute("aria-label", label);
      button.title = label;
      button.dataset.icon = next === "light" ? "sun" : "moon";
      button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${next === "light" ? '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>' : '<path d="M20.5 14A9 9 0 0 1 10 3.5 9 9 0 1 0 20.5 14Z"/>'}</svg>`;
    }
  }

  apply();
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document.getElementById("theme-toggle")?.addEventListener("click", () => {
      preference =
        document.documentElement.dataset.theme === "light" ? "dark" : "light";
      try {
        localStorage.setItem(key, preference);
      } catch (_) {}
      apply();
    });
  });
  telegram?.onEvent?.("themeChanged", apply);
  system?.addEventListener?.("change", apply);
})();
