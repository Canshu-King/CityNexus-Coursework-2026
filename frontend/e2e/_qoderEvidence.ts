import { mkdirSync } from "node:fs";
import { dirname, isAbsolute, join, resolve, sep } from "node:path";

/**
 * Where verification writes its artefacts.
 *
 * A canonical gate run must leave the checkout byte-identical: PASS-17 requires
 * `git status --short` to be empty after verification, and that is not allowed to be
 * achieved by restoring files afterwards.  Screenshots and the provenance record are
 * outputs, so when `CITYNEXUS_EVIDENCE_DIR` is set they are written there instead of
 * into tracked `reports/**`.
 *
 * Unset keeps the historical behaviour: artefacts land in the tracked `reports/`
 * directory, because a developer looking at `reports/` is not doing anything wrong.
 */
const ROOT = process.env.CITYNEXUS_EVIDENCE_DIR?.trim();

/** Callers name artefacts relative to the evidence base, e.g.
 *  `evidencePath("product-v8/ui/after/journey-hub-desktop.png")`. */
export function evidencePath(...segments: string[]): string {
  const relative = segments
    .join("/")
    .replace(/^\.\.[\\/]/, "")
    .replace(/^[\\/]+/, "");
  /* Unset, the base is the tracked `reports/` directory the suite has always used;
     set, it is that directory.  One rule, so a freeze run and a developer run differ
     only in where the artefacts land, never in whether they are produced. */
  const base = ROOT
    ? resolve(isAbsolute(ROOT) ? ROOT : join(process.cwd(), ROOT))
    : resolve("..", "reports");
  const target = isAbsolute(relative) ? resolve(relative) : resolve(base, relative);
  // Never let a caller walk out of the evidence base.  Both sides go through
  // resolve() first: an environment variable written with forward slashes names the
  // same directory as the backslash paths Node produces on Windows.
  const boundary = base.endsWith(sep) ? base : base + sep;
  if (target !== base && !target.startsWith(boundary)) {
    throw new Error(`evidence path ${target} escapes the evidence base ${base}`);
  }
  mkdirSync(dirname(target), { recursive: true });
  return target;
}
