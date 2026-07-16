(() => {
  const body = document.body;
  const sidebar = document.getElementById("app-sidebar");
  const sidebarNav = sidebar?.querySelector(".sidebar-nav");
  const currentNavLink = sidebar?.querySelector('[aria-current="page"]');
  const menuButton = document.querySelector("[data-nav-open]");
  const scrim = document.querySelector("[data-nav-close]");
  const main = document.getElementById("main-content");
  const collapseButton = document.querySelector("[data-nav-collapse]");
  const themeButton = document.querySelector("[data-theme-toggle]");
  const themeSelect = document.querySelector("[data-theme-select]");
  const languageSelect = document.querySelector("[data-language-select]");
  const desktopQuery = window.matchMedia("(min-width: 768px)");
  const colorSchemeQuery = window.matchMedia("(prefers-color-scheme: light)");
  let returnFocus = null;

  const messages = {
    en: {
      "nav.today": "Today", "nav.plans": "Plans", "nav.me": "Me",
      "nav.primary": "Primary pages", "nav.open": "Open navigation", "nav.close": "Close navigation",
      "nav.collapse": "Collapse sidebar", "nav.expand": "Expand sidebar", "skip.main": "Skip to main content",
      "action.record": "Add a record", "storage.local": "Stored on this device only",
      "storage.sync": "Local first · Sync enabled",
      "page.me": "Me", "me.description": "Long-term goals, preferences, and device settings live here.",
      "me.profile": "Goals & eating preferences", "me.profile.help": "Goals, meal patterns, and health boundaries to keep in mind.",
      "me.learning": "What MealCircuit knows about you", "me.learning.help": "Review the preferences and needs currently shaping your plans.",
      "me.inventory": "Pantry & frequent foods", "me.inventory.help": "What you have at home and which ingredients to use first.",
      "settings.appearance": "Appearance & language", "settings.appearance.help": "These preferences are saved on this device.",
      "settings.theme": "Theme", "settings.theme.system": "Follow system", "settings.theme.light": "Light", "settings.theme.dark": "Dark",
      "settings.language": "Language", "settings.language.en": "English", "settings.language.zh": "简体中文",
      "settings.advanced": "Advanced settings", "settings.ai": "AI planning settings", "settings.ai.help": "Connect a model and adjust generation.",
      "settings.sync": "Sync & devices", "settings.sync.help": "Sync between your own devices.",
      "settings.backup": "Backup & migration", "settings.backup.help": "Export, restore, or move local data.",
      "settings.foods": "Food nutrition library", "settings.foods.help": "Maintain packaged foods and common ingredients.",
      "theme.toLight": "Switch to light theme", "theme.toDark": "Switch to dark theme", "title.me": "Me · MealCircuit"
    },
    "zh-CN": {
      "nav.today": "今天", "nav.plans": "计划", "nav.me": "我的",
      "nav.primary": "主要页面", "nav.open": "打开导航", "nav.close": "关闭导航",
      "nav.collapse": "收起侧栏", "nav.expand": "展开侧栏", "skip.main": "跳到主要内容",
      "action.record": "记一笔", "storage.local": "仅存于本机", "storage.sync": "本地优先 · 同步已启用",
      "page.me": "我的", "me.description": "长期目标、偏好和设备设置都放在这里。",
      "me.profile": "目标与饮食偏好", "me.profile.help": "目标、用餐方式和需要注意的健康边界。",
      "me.learning": "MealCircuit了解的你", "me.learning.help": "查看正在影响安排的偏好和需要。",
      "me.inventory": "库存与常用食物", "me.inventory.help": "家里有什么、哪些食材需要优先吃。",
      "settings.appearance": "外观与语言", "settings.appearance.help": "这些偏好保存在当前设备上。",
      "settings.theme": "主题", "settings.theme.system": "跟随系统", "settings.theme.light": "浅色", "settings.theme.dark": "深色",
      "settings.language": "语言", "settings.language.en": "English", "settings.language.zh": "简体中文",
      "settings.advanced": "高级设置", "settings.ai": "智能规划设置", "settings.ai.help": "连接模型和调整生成方式",
      "settings.sync": "同步与设备", "settings.sync.help": "在自己的设备之间同步",
      "settings.backup": "备份与迁移", "settings.backup.help": "导出、恢复或迁移本地数据",
      "settings.foods": "食品营养库", "settings.foods.help": "维护包装食品和常用原料",
      "theme.toLight": "切换到浅色主题", "theme.toDark": "切换到深色主题", "title.me": "我的 · MealCircuit"
    }
  };

  const language = () => document.documentElement.dataset.language || "en";
  const message = (key) => messages[language()]?.[key] || messages.en[key] || key;
  const applyLanguage = (nextLanguage) => {
    document.documentElement.lang = nextLanguage;
    document.documentElement.dataset.language = nextLanguage;
    document.querySelectorAll("[data-i18n]").forEach((element) => {
      element.textContent = messages[nextLanguage]?.[element.dataset.i18n] || element.textContent;
    });
    document.querySelectorAll("[data-i18n-label]").forEach((element) => {
      const label = messages[nextLanguage]?.[element.dataset.i18nLabel];
      if (label) {
        element.setAttribute("aria-label", label);
        if (element.hasAttribute("title")) element.setAttribute("title", label);
      }
    });
    if (languageSelect) languageSelect.value = nextLanguage;
    const titleKey = document.body.dataset.i18nTitle;
    if (titleKey && messages[nextLanguage]?.[titleKey]) document.title = messages[nextLanguage][titleKey];
    const localDate = document.querySelector("[data-local-date]");
    if (localDate) {
      const date = new Date(`${localDate.dataset.localDate}T12:00:00`);
      localDate.textContent = new Intl.DateTimeFormat(nextLanguage, {
        month: "short", day: "numeric", weekday: "short"
      }).format(date);
    }
    updateThemeButton();
  };

  const resolveTheme = (preference) => preference === "system"
    ? (colorSchemeQuery.matches ? "light" : "dark")
    : preference;
  const applyTheme = (preference) => {
    document.documentElement.dataset.themePreference = preference;
    document.documentElement.dataset.theme = resolveTheme(preference);
    if (themeSelect) themeSelect.value = preference;
    updateThemeButton();
  };

  const revealCurrentNavLink = () => {
    if (!sidebarNav || !currentNavLink) return;
    const navRect = sidebarNav.getBoundingClientRect();
    const linkRect = currentNavLink.getBoundingClientRect();
    if (linkRect.top < navRect.top || linkRect.bottom > navRect.bottom) {
      currentNavLink.scrollIntoView({ block: "nearest" });
    }
  };

  const updateThemeButton = () => {
    if (!themeButton) return;
    const currentTheme = document.documentElement.dataset.theme;
    const label = currentTheme === "light" ? message("theme.toDark") : message("theme.toLight");
    themeButton.setAttribute("aria-label", label);
    themeButton.setAttribute("title", label);
  };

  if (themeButton) {
    themeButton.hidden = false;
    updateThemeButton();
    themeButton.addEventListener("click", () => {
      const nextTheme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      applyTheme(nextTheme);
      try {
        localStorage.setItem("mealcircuit.theme", nextTheme);
      } catch (_error) {
        // Theme switching still works for this page when storage is unavailable.
      }
    });
  }

  colorSchemeQuery.addEventListener("change", () => {
    if (document.documentElement.dataset.themePreference === "system") applyTheme("system");
  });
  themeSelect?.addEventListener("change", () => {
    const preference = themeSelect.value;
    applyTheme(preference);
    try { localStorage.setItem("mealcircuit.theme", preference); } catch (_error) { /* Optional preference. */ }
  });
  languageSelect?.addEventListener("change", () => {
    const nextLanguage = languageSelect.value;
    applyLanguage(nextLanguage);
    try { localStorage.setItem("mealcircuit.language", nextLanguage); } catch (_error) { /* Optional preference. */ }
  });
  applyTheme(document.documentElement.dataset.themePreference || "system");
  applyLanguage(document.documentElement.dataset.language || "en");

  try {
    if (localStorage.getItem("mealcircuit.sidebarCollapsed") === "true") {
      body.classList.add("sidebar-collapsed");
    }
    const savedScroll = Number(sessionStorage.getItem("mealcircuit.sidebarScrollTop") || "0");
    if (sidebarNav && Number.isFinite(savedScroll)) sidebarNav.scrollTop = savedScroll;
  } catch (_error) {
    // Layout preference is optional; the app remains fully usable without storage.
  }
  revealCurrentNavLink();
  if (sidebarNav) {
    let scrollFrame = null;
    const saveSidebarScroll = () => {
      try {
        sessionStorage.setItem("mealcircuit.sidebarScrollTop", String(sidebarNav.scrollTop));
      } catch (_error) {
        // The current page still works when layout preferences cannot be stored.
      }
    };
    sidebarNav.addEventListener("scroll", () => {
      if (scrollFrame !== null) cancelAnimationFrame(scrollFrame);
      scrollFrame = requestAnimationFrame(() => {
        saveSidebarScroll();
        scrollFrame = null;
      });
    }, { passive: true });
    window.addEventListener("pagehide", saveSidebarScroll);
  }
  if (collapseButton) {
    const collapsed = body.classList.contains("sidebar-collapsed");
    collapseButton.setAttribute("aria-label", collapsed ? message("nav.expand") : message("nav.collapse"));
    collapseButton.setAttribute("title", collapsed ? message("nav.expand") : message("nav.collapse"));
  }

  const setOpen = (open) => {
    body.classList.toggle("nav-open", open);
    menuButton?.setAttribute("aria-expanded", String(open));
    sidebar?.setAttribute("aria-hidden", String(!open && !desktopQuery.matches));
    if (sidebar) sidebar.inert = !desktopQuery.matches && !open;
    if (!desktopQuery.matches && main) main.inert = open;
    if (scrim) scrim.tabIndex = open ? 0 : -1;
    if (open) {
      returnFocus = document.activeElement;
      revealCurrentNavLink();
      (currentNavLink || sidebar?.querySelector("a"))?.focus();
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
  if (sidebar) sidebar.inert = !desktopQuery.matches;
  if (scrim) scrim.tabIndex = -1;

  collapseButton?.addEventListener("click", () => {
    const collapsed = body.classList.toggle("sidebar-collapsed");
    collapseButton.setAttribute("aria-label", collapsed ? message("nav.expand") : message("nav.collapse"));
    collapseButton.setAttribute("title", collapsed ? message("nav.expand") : message("nav.collapse"));
    try {
      localStorage.setItem("mealcircuit.sidebarCollapsed", String(collapsed));
    } catch (_error) {
      // Storage can be disabled without affecting navigation.
    }
  });

  document.querySelectorAll("form[data-plan-feedback]").forEach((form) => {
    const status = form.querySelector('[name="status"]');
    const reasons = [...form.querySelectorAll('[name="reason_codes"]')];
    const reasonFieldset = reasons[0]?.closest("fieldset");
    const errorSlot = form.querySelector("[data-feedback-error-slot]");
    const clearError = () => {
      form.querySelector("[data-feedback-error]")?.remove();
      reasonFieldset?.removeAttribute("aria-invalid");
    };

    status?.addEventListener("change", clearError);
    reasons.forEach((reason) => reason.addEventListener("change", clearError));
    form.addEventListener("submit", (event) => {
      const needsReason = status?.value === "modified" || status?.value === "skipped";
      if (!needsReason || reasons.some((reason) => reason.checked)) return;
      event.preventDefault();
      clearError();
      const error = document.createElement("div");
      error.className = "form-error";
      error.dataset.feedbackError = "";
      error.setAttribute("role", "alert");
      error.tabIndex = -1;
      error.innerHTML = "<strong>还差一项</strong><p>请选择这顿发生变化的原因。</p>";
      errorSlot?.append(error);
      reasonFieldset?.setAttribute("aria-invalid", "true");
      error.focus();
    });
  });

  const agentProgress = document.querySelector("[data-agent-state-url]");
  if (agentProgress) {
    const initialStatus = agentProgress.dataset.agentStatus || "collecting";
    const initialVersion = Number(agentProgress.dataset.agentVersion || "0");
    let stopped = false;
    const poll = async () => {
      if (stopped || document.hidden) return;
      try {
        const response = await fetch(agentProgress.dataset.agentStateUrl, {
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        if (!response.ok) return;
        const state = await response.json();
        if (state.status !== initialStatus || Number(state.version || 0) !== initialVersion) {
          stopped = true;
          window.location.reload();
        }
      } catch (_error) {
        // Background planning is persistent; a transient poll failure must not break the page.
      }
    };
    const timer = window.setInterval(poll, 2500);
    window.addEventListener("beforeunload", () => {
      stopped = true;
      window.clearInterval(timer);
    }, { once: true });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) poll();
    });
  }
})();
