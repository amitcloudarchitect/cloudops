"use client";

import { useEffect, useState } from "react";

type Theme = "system" | "light" | "dark";
const KEY = "cloudops.theme";

function apply(theme: Theme) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("system");
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(KEY);
      if (stored === "light" || stored === "dark") setTheme(stored);
    } catch {
      // Storage blocked: follow the system.
    }
  }, []);
  useEffect(() => {
    apply(theme);
    try {
      if (theme === "system") window.localStorage.removeItem(KEY);
      else window.localStorage.setItem(KEY, theme);
    } catch {
      // Storage blocked: the choice lasts for this page.
    }
  }, [theme]);
  return (
    <label className="flex items-center gap-2 text-sm text-chrome-muted">
      Theme
      <select
        className="min-h-8 rounded-control border border-chrome-line bg-chrome-raised px-2 py-1 text-sm text-chrome-ink hover:border-chrome-ink"
        value={theme}
        onChange={(e) => setTheme(e.target.value as Theme)}
      >
        <option value="system">System</option>
        <option value="light">Light</option>
        <option value="dark">Dark</option>
      </select>
    </label>
  );
}
