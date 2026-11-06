import { request } from "@playwright/test";

/* Runs after Playwright has started both webServers, which is what makes an
   ownership assertion possible here at all — a port-free check would only ever
   see this run's own processes.

   The pass-13 provenance gate asks one question of the server it is about to
   test against: did *this* invocation start you?  The suite passes a per-run
   token into the backend it spawns; only that process can echo it back through
   /health, and because the request goes to the frontend origin, a matching
   answer also proves the dev server we are driving proxies to it.  A borrowed
   :8000 from another agent lane returns an empty token and the run stops. */

function refuse(reason: string): never {
  throw new Error(`[citynexus e2e] authoritative provenance check failed: ${reason}`);
}

export default async function authoritativeProvenanceCheck() {
  if (process.env.CITYNEXUS_E2E_AUTHORITATIVE !== "1") return;

  const runId = process.env.CITYNEXUS_E2E_RUN_ID;
  const frontendPort = process.env.CITYNEXUS_E2E_PORT;
  const backendPort = process.env.CITYNEXUS_BACKEND_PORT;
  const databaseUrl = process.env.CITYNEXUS_DATABASE_URL ?? "";
  if (!runId) refuse("CITYNEXUS_E2E_RUN_ID is required in authoritative mode");

  /* Freshness is enforced in the process that opens the database —
     backend/app/db/session.py refuses to reopen an existing file for a
     run-stamped process.  It cannot be checked here or in the config: by the time
     this hook runs, the backend this invocation started has already created the
     file.  What is verified below is which file that process opened, by basename,
     because /health deliberately does not expose a connection URI. */
  const expectedDatabaseId = databaseUrl.replace(/^sqlite:\/\/\//, "").split(/[\\/]/).pop();

  const api = await request.newContext({ baseURL: `http://127.0.0.1:${frontendPort}` });
  let health: Record<string, string>;
  try {
    const response = await api.get("/api/v1/health");
    if (!response.ok()) refuse(`/health answered ${response.status()} via :${frontendPort}`);
    health = await response.json();
  } catch (error) {
    refuse(
      `could not read /health through the frontend on :${frontendPort} (${error}). ` +
        `The suite may not be talking to the servers this run started.`,
    );
  } finally {
    await api.dispose();
  }

  if (health.e2e_run_id !== runId) {
    refuse(
      `the backend answering :${backendPort} reports e2e_run_id="${health.e2e_run_id ?? ""}" ` +
        `but this run issued "${runId}". That server was started by someone else — ` +
        `find it with "netstat -ano | grep ':${backendPort} .*LISTENING'".`,
    );
  }
  if (health.ai !== "disabled") {
    refuse(`external AI must be disabled for the authoritative suite, /health says "${health.ai}"`);
  }
  if (health.e2e_database_id !== expectedDatabaseId) {
    refuse(
      `this run asked for database "${expectedDatabaseId}" but the backend opened ` +
        `"${health.e2e_database_id ?? ""}" — the database is not the one this invocation created.`,
    );
  }

  console.error(
    `[citynexus e2e] provenance ok: run=${runId} frontend=:${frontendPort} -> ` +
      `backend=:${backendPort} ai=disabled db=${health.e2e_database_id}`,
  );
}
