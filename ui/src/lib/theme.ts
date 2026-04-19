import { useEffect, useState } from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "pilk.theme";

/** Single source of truth for which palette the app is rendering in.
 *
 * Resolution order on first paint:
 *   1. `localStorage["pilk.theme"]` — explicit user choice wins.
 *   2. `prefers-color-scheme` — respect the OS-level choice.
 *   3. Fallback → "dark" (the pre-existing PILK look).
 *
 * The effective theme is written to `document.documentElement` as
 * `data-theme="<theme>"`, which the CSS layer reads via the
 * `[data-theme="light"]` override block in global.css.
 */
function readInitial(): Theme {
  if (typeof window === "undefined") return "dark";
  const saved = window.localStorage.getItem(STORAGE_KEY);
  if (saved === "light" || saved === "dark") return saved;
  if (window.matchMedia?.("(prefers-color-scheme: light)").matches) {
    return "light";
  }
  return "dark";
}

function apply(theme: Theme): void {
  document.documentElement.setAttribute("data-theme", theme);
  // Nudge the UA to re-render form controls in the matching scheme.
  document.documentElement.style.colorScheme = theme;
}

/** Apply the stored/detected theme before the first render so there's
 * no white flash on a dark-mode user. Call from `main.tsx`. */
export function initTheme(): void {
  apply(readInitial());
}

/** React hook: `[theme, setTheme, toggleTheme]`. The setter persists to
 * localStorage and pushes `data-theme` to the root element. */
export function useTheme(): {
  theme: Theme;
  setTheme: (t: Theme) => void;
  toggleTheme: () => void;
} {
  const [theme, setThemeState] = useState<Theme>(readInitial);

  useEffect(() => {
    apply(theme);
    try {
      window.localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // localStorage can throw in Safari private mode — not fatal.
    }
  }, [theme]);

  // Follow OS changes only if the user hasn't pinned a preference.
  useEffect(() => {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark") return;
    const mql = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = (e: MediaQueryListEvent) => {
      setThemeState(e.matches ? "light" : "dark");
    };
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  return {
    theme,
    setTheme: setThemeState,
    toggleTheme: () =>
      setThemeState((t) => (t === "light" ? "dark" : "light")),
  };
}
