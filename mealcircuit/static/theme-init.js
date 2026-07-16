(() => {
  let preference = "system";
  try {
    const savedTheme = localStorage.getItem("mealcircuit.theme");
    if (["system", "light", "dark"].includes(savedTheme)) preference = savedTheme;
  } catch (_error) {
    // Storage is optional; fall back to the operating system preference.
  }
  const resolvedTheme = preference === "system" ? (
    window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"
  ) : preference;
  document.documentElement.dataset.themePreference = preference;
  document.documentElement.dataset.theme = resolvedTheme;

  let language = "en";
  try {
    const savedLanguage = localStorage.getItem("mealcircuit.language");
    if (savedLanguage === "en" || savedLanguage === "zh-CN") language = savedLanguage;
  } catch (_error) {
    // English remains the default when storage is unavailable.
  }
  document.documentElement.lang = language;
  document.documentElement.dataset.language = language;
})();
