import { defineConfig } from "@playwright/test";
import {
  announce,
  backendCommand,
  backendEnv,
  resolveE2eEnvironment,
} from "./playwrightEnv";

/* ------------------------------------------------------------------ *
 * The current-product acceptance suite.
 *
 * Two ways to run it.
 *
 * Default (developer) mode keeps the convenient behaviour: sensible ports, a
 * venv next to the repo if one exists, and reuse of any server already
 * listening.  Pass-12 showed what that convenience costs — a clone without its
 * own .venv fell back to a system Python that cannot import the app, and
 * reuseExistingServer adopted whatever process held :8000, which was another
 * agent lane's backend running other code with DeepSeek switched on.  Green
 * tests were reporting on a neighbour.
 *
 * Authoritative mode (CITYNEXUS_E2E_AUTHORITATIVE=1) makes that structurally
 * impossible: this run must own both processes or it must refuse to start.
 * The rules and the reason for each live in playwrightEnv.ts, and
 * docs/E2E_ACCEPTANCE_POLICY.md is the contract.  Archived legacy-surface tests
 * are not part of this suite at all — see e2e/legacy/ and
 * playwright.legacy.config.ts.
 * ------------------------------------------------------------------ */

const env = resolveE2eEnvironment("current-product");
announce(env, "current-product");

export default defineConfig({
  testDir: "./e2e",
  /* The archived cases are not discovered here, in any mode.  Moving a test out
     of the acceptance suite and then running it anyway would be an organisation
     chart change, not a retirement. */
  testIgnore: /[\\/]e2e[\\/]legacy[\\/]/,
  /* Authoritative mode asserts, through globalSetup, that the servers answering
     are the ones this invocation started.  Measured on Playwright 1.63:
     globalSetup runs *after* the webServer entries are up — the first version of
     this gate assumed the opposite and tried to check that the ports were free,
     only to find this run's own uvicorn already listening.  Anything that must
     happen before the servers start therefore belongs in the config file itself,
     which is why the run-id rule is there and the ownership check is here. */
  ...(env.authoritative ? { globalSetup: "./e2e/_qoderAuthoritativeSetup.ts" } : {}),
  // Guardian recovery is an asynchronous provider -> solver -> policy ->
  // mutation path.  Windows CI occasionally needs longer than Playwright's
  // 30s default while the browser reloads and the shared local SQLite worker
  // drains; the product assertions retain their own targeted time budgets.
  timeout: 60_000,
  expect: { timeout: 10_000 },
  // The local demo intentionally uses one shared SQLite process/database;
  // serial workers keep reset-and-replay scenarios isolated.
  workers: 1,
  /* The seed is a Playwright setup *project* rather than a globalSetup file: it
     needs the servers this run started, an APIRequest context, and somewhere to
     write the storageState that the chromium project then replays.  (globalSetup
     does run after webServer here, but it has no fixtures and no per-project
     output, so it is only used for the provenance assertion.) */
  projects: [
    { name: "real-seed", testMatch: /_qoderRealSeed\.setup\.ts/ },
    {
      name: "chromium",
      dependencies: ["real-seed"],
      use: { storageState: "./e2e/.qoder-seed-state.json" },
    },
  ],
  use: { baseURL: `http://127.0.0.1:${env.frontendPort}` },
  webServer: [
    {
      command: backendCommand(env),
      cwd: "../backend",
      env: backendEnv(env),
      url: `http://127.0.0.1:${env.backendPort}/api/v1/health`,
      reuseExistingServer: env.reuseExistingServer,
      timeout: 120000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${env.frontendPort}`,
      cwd: ".",
      url: `http://127.0.0.1:${env.frontendPort}`,
      reuseExistingServer: env.reuseExistingServer,
      timeout: 120000,
    },
  ],
});
