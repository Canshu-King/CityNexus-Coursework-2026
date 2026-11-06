import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { evidencePath } from "./e2e/_qoderEvidence";

/* Shared startup environment for the Playwright configs.
 *
 * Two configs exist on purpose:
 *   playwright.config.ts         — the current-product acceptance suite.  This is
 *                                  the one that can sign a gate, and in
 *                                  CITYNEXUS_E2E_AUTHORITATIVE=1 it refuses to run
 *                                  against anything it did not start itself.
 *   playwright.legacy.config.ts  — archived legacy-surface tests.  Non-gating, and
 *                                  deliberately unable to become authoritative, so
 *                                  nobody can mistake a green legacy run for a
 *                                  merge-ready product.
 * The port/python/DB resolution lives here so the two cannot drift apart. */

export type E2eEnvironment = {
  authoritative: boolean;
  frontendPort: number;
  backendPort: number;
  backendPython: string;
  databaseUrl: string | undefined;
  runId: string | undefined;
  reuseExistingServer: boolean;
  ai: string;
};

function refuse(reason: string): never {
  throw new Error(
    `[citynexus e2e] authoritative mode refuses to start: ${reason}\n` +
      `[citynexus e2e] fix the invocation, not the assertion.  See docs/E2E_ACCEPTANCE_POLICY.md.`,
  );
}

function requiredEnv(name: string, why: string): string {
  const value = process.env[name];
  if (!value) refuse(`${name} must be set explicitly (${why})`);
  return value;
}

function port(value: string, name: string): number {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1024 || parsed > 65535) {
    refuse(`${name} must be a numeric port, got "${value}"`);
  }
  return parsed;
}

/**
 * @param label      what to print in the provenance line, e.g. "current-product"
 * @param allowAuthoritative  false for the legacy config: an archived run may not
 *                   claim gate evidence, so the flag is reported and ignored rather
 *                   than honoured.
 */
export function resolveE2eEnvironment(
  label: string,
  options: { allowAuthoritative?: boolean } = {},
): E2eEnvironment {
  const allowAuthoritative = options.allowAuthoritative !== false;
  const authoritative = allowAuthoritative && process.env.CITYNEXUS_E2E_AUTHORITATIVE === "1";

  if (!allowAuthoritative && process.env.CITYNEXUS_E2E_AUTHORITATIVE === "1") {
    console.error(
      `[citynexus e2e] ${label}: CITYNEXUS_E2E_AUTHORITATIVE is ignored — archived legacy ` +
        `results cannot sign a gate.`,
    );
  }

  if (authoritative) {
    const frontendPort = port(
      requiredEnv("CITYNEXUS_E2E_PORT", "the frontend must not be borrowed"),
      "CITYNEXUS_E2E_PORT",
    );
    const backendPort = port(
      requiredEnv("CITYNEXUS_BACKEND_PORT", "the backend must not be borrowed"),
      "CITYNEXUS_BACKEND_PORT",
    );
    if (frontendPort === backendPort) refuse("frontend and backend ports must differ");

    const python = requiredEnv("CITYNEXUS_PYTHON", "falling back to system Python is what tested a neighbour");
    if (!existsSync(resolve(python))) refuse(`CITYNEXUS_PYTHON="${python}" does not exist`);

    const databaseUrl = requiredEnv("CITYNEXUS_DATABASE_URL", "an authoritative run gets its own database");
    if (!databaseUrl.startsWith("sqlite:///")) {
      refuse(`CITYNEXUS_DATABASE_URL must be a sqlite:/// path in this mode, got "${databaseUrl}"`);
    }
    /* Nothing is ever deleted for a run.  The "_*.db" shape is required because
       the run-stamped backend refuses to reopen a file that already exists
       (fresh-or-no-run), so this path must never be able to name something a
       person made.  A leading underscore is also git-ignored. */
    const dbFile = databaseUrl.replace(/^sqlite:\/\/\//, "");
    const dbBase = dbFile.split(/[\\/]/).pop() ?? "";
    if (!dbBase.startsWith("_") || !dbBase.endsWith(".db")) {
      refuse(`CITYNEXUS_DATABASE_URL must point at a throwaway "_*.db" file, got "${dbFile}"`);
    }
    const runId = requiredEnv("CITYNEXUS_E2E_RUN_ID", "the backend echoes it back to prove who started it");
    /* Fresh-or-no-run is *not* checked here.  This module is evaluated again in
       the worker process after the backend has already created the file, so an
       existence check at config time rejects the run it belongs to (observed).
       The refusal lives in backend/app/db/session.py, in the process that opens
       the database; the run-id naming rule below is what makes that refusal
       reachable at all. */
    if (!dbBase.includes(runId)) {
      refuse(`CITYNEXUS_DATABASE_URL ("${dbFile}") must contain CITYNEXUS_E2E_RUN_ID "${runId}"`);
    }

    const liveKeys = ["DEEPSEEK_API_KEY", "CITYNEXUS_AI_API_KEY"].filter((k) => !!process.env[k]);
    if (liveKeys.length) {
      refuse(`external AI must be off for the deterministic suite (found ${liveKeys.join(", ")})`);
    }
    if (process.env.CITYNEXUS_AI_ENABLED !== "false") {
      refuse('CITYNEXUS_AI_ENABLED must be "false"');
    }

    return {
      authoritative: true,
      frontendPort,
      backendPort,
      backendPython: python,
      databaseUrl,
      runId,
      reuseExistingServer: false,
      ai: "disabled",
    };
  }

  return {
    authoritative: false,
    frontendPort: Number(process.env.CITYNEXUS_E2E_PORT ?? (label === "legacy" ? 4174 : 4173)),
    backendPort: Number(process.env.CITYNEXUS_BACKEND_PORT ?? 8000),
    backendPython:
      process.env.CITYNEXUS_PYTHON ??
      (process.platform === "win32" && existsSync("../.venv/Scripts/python.exe")
        ? "..\\.venv\\Scripts\\python.exe"
        : "python"),
    databaseUrl: process.env.CITYNEXUS_DATABASE_URL,
    runId: process.env.CITYNEXUS_E2E_RUN_ID,
    reuseExistingServer: true,
    ai: "inherited from the environment",
  };
}

/** One provenance line per config load, on stderr so `--reporter=json` stays parseable. */
export function announce(env: E2eEnvironment, label: string): void {
  const line =
    `[citynexus e2e] suite=${label} authoritative=${env.authoritative} ` +
    `python=${env.backendPython} frontend=:${env.frontendPort} backend=:${env.backendPort} ` +
    `db=${env.databaseUrl ?? "(inherited default)"} ai=${env.ai} ` +
    `reuseExistingServer=${env.reuseExistingServer}`;
  console.error(line);
  if (env.authoritative) {
    const target = evidencePath("e2e-provenance/authoritative.json");
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(
      target,
      JSON.stringify(
        {
          authoritative: true,
          suite: label,
          runId: env.runId ?? "(none)",
          python: env.backendPython,
          pythonResolved: resolve(env.backendPython),
          frontendPort: env.frontendPort,
          backendPort: env.backendPort,
          databaseUrl: env.databaseUrl ?? "(inherited default)",
          ai: env.ai,
          reuseExistingServer: env.reuseExistingServer,
          workers: 1,
          retries: 0,
          recordedAt: new Date().toISOString(),
          provenanceLine: line,
        },
        null,
        2,
      ) + "\n",
      "utf-8",
    );
  }
}

export function backendCommand(env: E2eEnvironment): string {
  const python = env.backendPython.includes(" ") ? `"${env.backendPython}"` : env.backendPython;
  return `${python} -m uvicorn app.main:app --host 127.0.0.1 --port ${env.backendPort}`;
}

export function backendEnv(env: E2eEnvironment): Record<string, string> {
  return {
    ...(process.env as Record<string, string>),
    // ORD-0058: the authoritative backend now refuses `POST /api/v1/demo/reset` unless the
    // process declares itself a simulation stage, and the E2E specs use that route as their
    // fixture door.  The switch is set here (not required from the caller) because an
    // authoritative run is by definition running against seeded demo data - and the guard's
    // own refusal is gated in the backend suite, so this cannot mask a missing guard.
    CITYNEXUS_DEMO_MODE: "true",
    ...(env.databaseUrl ? { CITYNEXUS_DATABASE_URL: env.databaseUrl } : {}),
    ...(env.runId ? { CITYNEXUS_E2E_RUN_ID: env.runId } : {}),
  };
}
