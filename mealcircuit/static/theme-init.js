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

  // The desktop application content is currently complete in Simplified Chinese.
  // Keep the public Cloudflare Pages site bilingual without exposing a partial
  // desktop language switch that mixes an English shell with Chinese content.
  document.documentElement.lang = "zh-CN";
  document.documentElement.dataset.language = "zh-CN";
})();
