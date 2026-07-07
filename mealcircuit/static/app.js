(() => {
  const body = document.body;
  const sidebar = document.getElementById("app-sidebar");
  const menuButton = document.querySelector("[data-nav-open]");
  const scrim = document.querySelector("[data-nav-close]");
  const collapseButton = document.querySelector("[data-nav-collapse]");
  const desktopQuery = window.matchMedia("(min-width: 768px)");
  let returnFocus = null;

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
  });
  desktopQuery.addEventListener("change", (event) => {
    if (event.matches) setOpen(false);
    sidebar?.setAttribute("aria-hidden", String(!event.matches));
  });

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
