(() => {
  let theme = null;
  try {
    const savedTheme = localStorage.getItem("mealcircuit.theme");
    if (savedTheme === "light" || savedTheme === "dark") theme = savedTheme;
  } catch (_error) {
    // Storage is optional; fall back to the operating system preference.
  }
  document.documentElement.dataset.theme = theme || (
    window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"
  );
})();
