(() => {
  const body = document.body;
  const sidebar = document.getElementById("app-sidebar");
  const menuButton = document.querySelector("[data-nav-open]");
  const scrim = document.querySelector("[data-nav-close]");
  const main = document.getElementById("main-content");
  const collapseButton = document.querySelector("[data-nav-collapse]");
  const themeButton = document.querySelector("[data-theme-toggle]");
  const desktopQuery = window.matchMedia("(min-width: 768px)");
  let returnFocus = null;

  const updateThemeButton = () => {
    if (!themeButton) return;
    const currentTheme = document.documentElement.dataset.theme;
    const label = currentTheme === "light" ? "切换到深色主题" : "切换到浅色主题";
    themeButton.setAttribute("aria-label", label);
    themeButton.setAttribute("title", label);
  };

  if (themeButton) {
    themeButton.hidden = false;
    updateThemeButton();
    themeButton.addEventListener("click", () => {
      const nextTheme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      document.documentElement.dataset.theme = nextTheme;
      updateThemeButton();
      try {
        localStorage.setItem("mealcircuit.theme", nextTheme);
      } catch (_error) {
        // Theme switching still works for this page when storage is unavailable.
      }
    });
  }

  try {
    if (localStorage.getItem("mealcircuit.sidebarCollapsed") === "true") {
      body.classList.add("sidebar-collapsed");
    }
  } catch (_error) {
    // Layout preference is optional; the app remains fully usable without storage.
  }
  if (collapseButton) {
    const collapsed = body.classList.contains("sidebar-collapsed");
    collapseButton.setAttribute("aria-label", collapsed ? "展开侧栏" : "收起侧栏");
    collapseButton.setAttribute("title", collapsed ? "展开侧栏" : "收起侧栏");
  }

  const setOpen = (open) => {
    body.classList.toggle("nav-open", open);
    menuButton?.setAttribute("aria-expanded", String(open));
    sidebar?.setAttribute("aria-hidden", String(!open && !desktopQuery.matches));
    if (!desktopQuery.matches && main) main.inert = open;
    if (scrim) scrim.tabIndex = open ? 0 : -1;
    if (open) {
      returnFocus = document.activeElement;
      sidebar?.querySelector("a")?.focus();
    } else if (returnFocus instanceof HTMLElement) {
      returnFocus.focus();
    }
  };

  menuButton?.addEventListener("click", () => setOpen(true));
  scrim?.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && body.classList.contains("nav-open")) {
      setOpen(false);
    }
    if (event.key === "Tab" && body.classList.contains("nav-open") && sidebar) {
      const focusable = [...sidebar.querySelectorAll('a[href], button:not([disabled])')];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });
  desktopQuery.addEventListener("change", (event) => {
    if (event.matches) setOpen(false);
    sidebar?.setAttribute("aria-hidden", String(!event.matches));
  });
  sidebar?.setAttribute("aria-hidden", String(!desktopQuery.matches));
  if (scrim) scrim.tabIndex = -1;

  collapseButton?.addEventListener("click", () => {
    const collapsed = body.classList.toggle("sidebar-collapsed");
    collapseButton.setAttribute("aria-label", collapsed ? "展开侧栏" : "收起侧栏");
    collapseButton.setAttribute("title", collapsed ? "展开侧栏" : "收起侧栏");
    try {
      localStorage.setItem("mealcircuit.sidebarCollapsed", String(collapsed));
    } catch (_error) {
      // Storage can be disabled without affecting navigation.
    }
  });
})();
