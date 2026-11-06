import { expect, test } from "@playwright/test";
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/* Regression demanded by FILE_OWNERSHIP for `playwrightEnv.ts` / `playwright.config.ts`:
 * "改它必须带回归：缺必需变量时必须非零退出并打印缺哪个".
 *
 * Why it exists as a *spawned child* rather than as a call into `resolveE2eEnvironment()`:
 * the whole point of the refusal is that it happens while Playwright evaluates the config,
 * i.e. before any test code can run.  Importing the module here would measure the function,
 * not the gate; a future refactor that moves the check behind a lazily-called helper would
 * keep such an import green while every scripted run silently stopped owning its servers.
 * So this file launches the real CLI with a real environment and judges what the caller
 * would see: an exit code and bytes on stderr.
 *
 * `--list` is the subcommand under test because it forces config evaluation without
 * starting a server, which keeps this honest (we are not adopting anyone's :8000) and cheap.
 *
 * One toolchain fact this file exists alongside, measured twice on this machine and once
 * mistaken for a product defect (ORD-0025, voided by its own author): launching the same
 * command as `env VAR=1 node …` instead of `VAR=1 node …` yields **zero bytes and exit 0**
 * for both stdout and stderr, because MSYS `env` breaks stream inheritance for Windows
 * executables.  Nothing here uses that launcher, and nothing here should; "0 output + exit 0"
 * names the launcher, not the code.
 */

// Specs load as ES modules here, where __dirname does not exist (measured: ReferenceError
// at collection).  import.meta.url is what actually survives the loader.
const FRONTEND_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const CLI = join(FRONTEND_DIR, "node_modules", "@playwright", "test", "cli.js");
const SPEC_UNDER_TEST = "qoder-live-recovery-cursor";
const PARENT_IS_AUTHORITATIVE = process.env.CITYNEXUS_E2E_AUTHORITATIVE === "1";

type Run = { status: number | null; stdout: string; stderr: string };

type Env = { set: Record<string, string>; unset: string[] };

function listWith(spec: Env): Run {
  // Clone then delete: passing `undefined` through an env object is version-dependent
  // (it can arrive as the four-character string "undefined"), which would make a refusal
  // name the right variable for entirely the wrong reason.
  const env: Record<string, string> = { ...process.env } as Record<string, string>;
  for (const name of spec.unset) delete env[name];
  Object.assign(env, spec.set);
  if (!existsSync(CLI)) {
    // A gate that cannot launch is not a refusal; say which of the two it is.
    throw new Error(`the gate cannot measure anything: ${CLI} is not installed`);
  }
  // process.execPath, not `npx`: npx resolves `run <script>` against the wrong package
  // on this machine, which once turned three failing npm invocations into three exit=0s.
  const result = spawnSync(process.execPath, [CLI, "test", SPEC_UNDER_TEST, "--list"], {
    cwd: FRONTEND_DIR,
    env: env as NodeJS.ProcessEnv,
    encoding: "utf8",
  });
  return { status: result.status, stdout: result.stdout ?? "", stderr: result.stderr ?? "" };
}

/** Every authoritative variable, so the first check that fires is the one under test. */
const ALL_REQUIRED = [
  "CITYNEXUS_E2E_AUTHORITATIVE",
  "CITYNEXUS_E2E_PORT",
  "CITYNEXUS_BACKEND_PORT",
  "CITYNEXUS_PYTHON",
  "CITYNEXUS_DATABASE_URL",
  "CITYNEXUS_E2E_RUN_ID",
  "CITYNEXUS_AI_ENABLED",
  "DEEPSEEK_API_KEY",
  "CITYNEXUS_AI_API_KEY",
];

function authoritativeWithout(...kept: string[]): Env {
  return {
    set: { CITYNEXUS_E2E_AUTHORITATIVE: "1" },
    unset: ALL_REQUIRED.filter((name) => name !== "CITYNEXUS_E2E_AUTHORITATIVE" && !kept.includes(name)),
  };
}

const PORT_SET = { CITYNEXUS_E2E_PORT: "3901", CITYNEXUS_BACKEND_PORT: "8901" };
// Exists, or the interpreter check would report the path rather than the variable under test.
const PYTHON_SET = { CITYNEXUS_PYTHON: process.execPath };

test("an authoritative run missing a required variable exits non-zero and names that variable", async () => {
  // Both halves are asserted on purpose.  A non-zero code alone is satisfied by a crash
  // that says nothing, and a printed message alone is satisfied by a warning that still
  // lets the run proceed against someone else's backend.
  const cases: { name: string; env: Env }[] = [
    { name: "CITYNEXUS_E2E_PORT", env: authoritativeWithout() },
    { name: "CITYNEXUS_BACKEND_PORT", env: authoritativeWithout("CITYNEXUS_E2E_PORT") },
    {
      name: "CITYNEXUS_PYTHON",
      env: { set: { ...PORT_SET }, unset: authoritativeWithout().unset },
    },
    {
      name: "CITYNEXUS_DATABASE_URL",
      env: {
        set: { ...PORT_SET, ...PYTHON_SET },
        unset: authoritativeWithout().unset,
      },
    },
  ];

  for (const item of cases) {
    const run = listWith(item.env);
    expect(
      run.status,
      `${item.name} was omitted but the run did not fail (exit ${run.status}); stderr was:\n${run.stderr}`,
    ).not.toBe(0);
    expect(
      run.stderr,
      `the refusal must name the missing variable (${item.name}), not just fail`
    ).toContain(item.name);
    // The contract of the message is "fix your invocation"; without it a reader cannot tell
    // this from a bug in the suite.
    expect(run.stderr).toContain("fix the invocation, not the assertion");
  }
});

test("an authoritative run that omits the interpreter cannot fall back to a system Python", async () => {
  // Pass-12 was exactly this: a clone without its own .venv resolved a Python that could not
  // import the app, and the suite went green against a neighbour's server anyway.
  const run = listWith({
    set: { ...PORT_SET },
    unset: authoritativeWithout().unset,
  });
  expect(run.stderr).toContain("falling back to system Python");
  expect(run.status).not.toBe(0);
});

test("a live external AI key refuses the deterministic suite instead of silently spending it", async () => {
  const run = listWith({
    set: {
      ...PORT_SET,
      ...PYTHON_SET,
      CITYNEXUS_DATABASE_URL: `sqlite:///${join(FRONTEND_DIR, "..", "backend", "_guard-nominal.db").replace(/\\/g, "/")}`,
      CITYNEXUS_E2E_RUN_ID: "guard",
      CITYNEXUS_AI_ENABLED: "false",
      DEEPSEEK_API_KEY: "sk-present-but-not-real",
    },
    unset: ["CITYNEXUS_AI_API_KEY"],
  });
  expect(run.status).not.toBe(0);
  expect(run.stderr).toContain("external AI must be off");
  expect(run.stderr).toContain("DEEPSEEK_API_KEY");
});

test("a complete authoritative environment is accepted and announces who it started", async () => {
  test.skip(!PARENT_IS_AUTHORITATIVE, "needs a real authoritative invocation to copy from");
  const run = listWith({ set: {}, unset: [] });
  expect(run.status, `stderr was:\n${run.stderr}`).toBe(0);
  // The provenance line is the only thing a reader of a log gets, so it must be on stderr
  // (keeping --reporter=json parseable) and must carry the values that make the run ownable.
  expect(run.stderr).toContain("suite=current-product");
  expect(run.stderr).toContain("authoritative=true");
  expect(run.stderr).toContain(`backend=:${process.env.CITYNEXUS_BACKEND_PORT}`);
  expect(run.stderr).toContain("ai=disabled");
  expect(run.stderr).toContain("reuseExistingServer=false");
  // A listing that found no test would make every assertion above vacuous.
  expect(run.stdout).toContain(`${SPEC_UNDER_TEST}.spec.ts`);
});
