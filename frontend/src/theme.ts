/**
 * The theme root contract (Mission 003 ruling ④).
 *
 * Three choices, one attribute:
 *   "system" -- follow `prefers-color-scheme` (the default for anyone who never chose);
 *   "light" / "dark" -- pinned, and the choice survives a reload.
 *
 * The resolved value is written to `<html data-theme="light|dark">`, and that is the only
 * theme selector the stylesheets use.  Resolving in one place rather than with
 * `@media (prefers-color-scheme: dark)` blocks means a pinned choice cannot disagree with
 * the OS block, and there is exactly one dark token table to keep honest.
 *
 * A switch is not a cover for an unreadable theme: both resolved values must clear the same
 * contrast gates, which `e2e/qoder-theme-contract.spec.ts` enforces.
 */

export type ThemeChoice = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

const STORAGE_KEY = "citynexus.theme";

const isChoice = (value: string | null): value is ThemeChoice =>
  value === "system" || value === "light" || value === "dark";

/** What the traveller chose.  Anything unrecognised falls back to following the system. */
export function readChoice(): ThemeChoice {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return isChoice(stored) ? stored : "system";
  } catch {
    // Private mode / blocked storage: following the system is still a correct answer.
    return "system";
  }
}

export function writeChoice(choice: ThemeChoice): void {
  try {
    localStorage.setItem(STORAGE_KEY, choice);
  } catch {
    /* The choice then lives for this page only.  applyTheme still ran, so the screen is
       not left disagreeing with itself. */
  }
}

export function systemTheme(): ResolvedTheme {
  return typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function resolveTheme(choice: ThemeChoice = readChoice()): ResolvedTheme {
  return choice === "system" ? systemTheme() : choice;
}

/**
 * Publish the resolved theme on the root and keep the browser chrome in step with it.
 *
 * The `theme-color` pair in index.html is media-based, so a pinned choice on a machine
 * whose OS disagrees would colour the browser UI for the wrong theme.  Reading the value
 * back from `--cn-canvas` after the attribute is set means the chrome colour is the canvas
 * the traveller is actually looking at, with no second copy of the palette to drift.
 */
export function applyTheme(choice: ThemeChoice = readChoice()): ResolvedTheme {
  const theme = resolveTheme(choice);
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.style.colorScheme = theme;

  const canvas = getComputedStyle(root).getPropertyValue("--cn-canvas").trim();
  if (canvas) {
    document
      .querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')
      .forEach((meta) => meta.remove());
    const meta = document.createElement("meta");
    meta.name = "theme-color";
    meta.content = canvas;
    document.head.appendChild(meta);
  }
  return theme;
}

/** Re-resolve when the OS flips while the traveller is on "system". */
export function followSystemTheme(): () => void {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return () => undefined;
  }
  const query = window.matchMedia("(prefers-color-scheme: dark)");
  const onChange = () => {
    if (readChoice() === "system") applyTheme("system");
  };
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

export function setThemeChoice(choice: ThemeChoice): ResolvedTheme {
  writeChoice(choice);
  return applyTheme(choice);
}
