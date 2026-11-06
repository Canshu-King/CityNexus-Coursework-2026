import { expect, test } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0036 (b) — the development service worker must never answer a code request with HTML.
 *
 * The reported shape was: one network hiccup, a `.js` module request fails, the worker's
 * catch-all hands back the `/v8/live` HTML shell, the browser parses HTML as a script, the
 * app half-boots -- and that half-booted document is what the reload keeps serving.  The
 * first node below reproduces exactly that and asserts the page cannot receive HTML.
 *
 * The second node covers the other half of "I rebuilt and a refresh still shows the old
 * shell": the worker's activate handler used to delete every cache whose name was not its
 * own, which on one origin means it destroyed the Workbox production caches too.
 *
 * These run against `npm run dev` because that is the only place this worker is registered
 * (src/main.tsx).  They are a regression net for the worker's contract, not release
 * evidence for the shipped bundle -- the production walk is a separate artefact.
 */

const FILE = "e2e/qoder-service-worker.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const SW_CACHE_VERSION = "m3-a4-v1";
const OWN_CACHE = `citynexus-recovery-citynexus-recovery-${SW_CACHE_VERSION}`;
/* Owned by the generated production worker, not by this one: deleting these from a dev
   session is the "rebuilt but the refresh shows the old shell" bug all over again. */
const FOREIGN_CACHES = [
  `citynexus-pages-${SW_CACHE_VERSION}`,
  `citynexus-static-${SW_CACHE_VERSION}`,
  "workbox-precache-v2",
];
/* A4-1 names these as retired: the cleanup is expected to remove them, and a cleanup that
   deletes nothing policing nothing is the pass this node refused to be once already. */
const LEGACY_CACHES = ["citynexus-pages", "citynexus-static", "citynexus-trip-operations-v1"];

test.describe("ORD-0036 the dev service worker degrades honestly", () => {
  test("sw-never-answers-script-with-html — a failed module request stays a failure", async ({
    page,
    context,
  }) => {
    test.setTimeout(120_000);
    await page.goto("/v8/live");
    await page.waitForLoadState("networkidle");

    const controlled = await page.evaluate(async () => {
      const registration = await navigator.serviceWorker.getRegistration();
      if (registration && !navigator.serviceWorker.controller) {
        await new Promise<void>((resolve) => {
          const timer = window.setTimeout(resolve, 5000);
          navigator.serviceWorker.ready.then(() => {
            window.clearTimeout(timer);
            resolve();
          });
        });
      }
      return Boolean(navigator.serviceWorker.controller);
    });
    expect(
      controlled,
      node("sw-never-answers-script-with-html",
        "no service worker took control, so this node measured nothing. Without a controller "
        + "the test would pass vacuously — the whole point is what the worker answers"),
    ).toBe(true);

    const probe = "/__qoder_never_cached_module__.js";

    /* Offline: the module cannot be fetched and is not cached.  The old worker answered
       this with the HTML shell, which is the exact mechanism behind "a white screen that
       survives a reload". */
    await context.setOffline(true);
    const offline = await page.evaluate(async (url) => {
      try {
        const response = await fetch(url);
        const type = response.headers.get("content-type") ?? "";
        const body = (await response.text()).slice(0, 200);
        return { ok: response.ok, type, body, rejected: false };
      } catch (cause) {
        return {
          ok: false,
          type: "",
          body: "",
          rejected: true,
          error: cause instanceof Error ? cause.name : String(cause),
        };
      }
    }, probe);

    /* Online: the dev server answers an unknown `.js` path with a 200 HTML document, so a
       network-first worker still has to refuse to hand that to a code request. */
    await context.setOffline(false);
    const online = await page.evaluate(async (url) => {
      try {
        const response = await fetch(url);
        return {
          ok: response.ok,
          type: response.headers.get("content-type") ?? "",
          body: "",
          rejected: false,
        };
      } catch (cause) {
        return {
          ok: false,
          type: "",
          body: "",
          rejected: true,
          error: cause instanceof Error ? cause.name : String(cause),
        };
      }
    }, probe);

    const evidence = {
      generatedBy: "e345b9d6",
      order: "ORD-0036",
      node: "sw-never-answers-script-with-html",
      offline,
      online,
    };
    writeFileSync(
      evidencePath("ui-overnight/sw-script-fallback.json"),
      JSON.stringify(evidence, null, 2) + "\n",
      "utf-8",
    );

    expect(
      /text\/html/i.test(offline.type),
      node("sw-never-answers-script-with-html",
        `an offline .js request that missed the cache was answered with HTML `
        + `(content-type ${JSON.stringify(offline.type)}, body ${JSON.stringify(offline.body)}). `
        + "That is the white screen: HTML parsed as a script, then cached"),
    ).toBe(false);
    expect(
      /text\/html/i.test(online.type),
      node("sw-never-answers-script-with-html",
        `an online .js request for a path the dev server answers with a document was passed `
        + `through as ${JSON.stringify(online.type)}`),
    ).toBe(false);

    /* Positive control, and it has to be the right one.  The guarantee is asymmetry: a code
       request must never be answered with a document, while a *navigation* may be.  Asserting
       it with `fetch("/v8/live")` tested neither -- a window fetch is not a navigation, and the
       recovery worker answers only navigations with the shell.  And asserting it with "the page
       rendered" measures the module graph, not the contract: in dev the modules were fetched
       before the worker took control, so an offline reload can paint nothing however correct
       the fallback is.  `sw-offline-boot-serves-cached-code` owns that case. */
    await context.setOffline(true);
    const navigation = await page.goto("/v8/live", { waitUntil: "commit" }).catch(() => null);
    const shell = navigation
      ? await page
          .evaluate(() => ({ hasRoot: !!document.getElementById("root"), title: document.title }))
          .catch(() => ({ hasRoot: false, title: "" }))
      : { hasRoot: false, title: "the navigation produced no response" };
    await context.setOffline(false);

    expect(
      shell.hasRoot,
      node("sw-never-answers-script-with-html",
        "an offline navigation did not reach the cached shell, so the guard written for code "
        + `requests also took navigation's document away: ${JSON.stringify(shell)}`),
    ).toBe(true);
  });

  test("sw-activate-keeps-foreign-caches — its cleanup must not wipe another strategy's caches", async ({
    page,
  }) => {
    test.setTimeout(120_000);
    await page.goto("/v8/live");
    await page.waitForLoadState("networkidle");

    const result = await page.evaluate(
      async ({ own, foreign, legacy }) => {
        const opened = await caches.open(own);
        // A body of our own so the cache exists and is recognisably ours.
        await opened.put(
          "/__qoder_probe__.txt",
          new Response("own", { headers: { "content-type": "text/plain" } }),
        );
        for (const name of [...foreign, ...legacy]) {
          const cache = await caches.open(name);
          await cache.put(
            "/__qoder_probe__.txt",
            new Response("foreign", { headers: { "content-type": "text/plain" } }),
          );
        }
        const before = (await caches.keys()).slice();

        /* A brand new script URL, so install and activate genuinely run.  Re-registering
           the same URL can be satisfied by the worker already in place, which is how an
           earlier revision of this node passed while judging a cleanup that never
           happened. */
        const probe = `ord36-${Date.now()}`;
        await navigator.serviceWorker.register(`/sw.js?probe=${probe}`, { scope: "/" });
        let report = null;
        const deadline = Date.now() + 10000;
        while (Date.now() < deadline && !report) {
          await new Promise((resolve) => setTimeout(resolve, 250));
          const cache = await caches.open(own);
          const stored = await cache.match("/__sw_activate_report__");
          if (!stored) continue;
          const parsed = await stored.json();
          /* Only the report this registration produced.  The page's first activation wrote
             one already, and reading that would judge nothing at all. */
          if (typeof parsed.script === "string" && parsed.script.includes(probe)) report = parsed;
        }
        const after = (await caches.keys()).slice();
        return {
          before,
          after,
          report,
          survivors: foreign.filter((name) => after.includes(name)),
          legacySurvivors: legacy.filter((name) => after.includes(name)),
        };
      },
      { own: OWN_CACHE, foreign: FOREIGN_CACHES, legacy: LEGACY_CACHES },
    );

    const evidence = { generatedBy: "e345b9d6", order: "ORD-0036", node: "sw-activate-keeps-foreign-caches", ...result };
    writeFileSync(
      evidencePath("ui-overnight/sw-cache-scope.json"),
      JSON.stringify(evidence, null, 2) + "\n",
      "utf-8",
    );

    expect(
      result.report,
      node("sw-activate-keeps-foreign-caches",
        "the worker never reported an activation, so this node judged a cleanup it cannot show "
        + "ran. Listing caches afterwards is not evidence — that is how the first revision of "
        + "this node passed against the very worker it is meant to police"),
    ).not.toBeNull();
    expect(
      result.report.deleted.filter((name) => FOREIGN_CACHES.includes(name)),
      node("sw-activate-keeps-foreign-caches",
        `the worker's activate cleanup deleted caches it does not own: `
        + `${JSON.stringify(result.report.deleted)}. On one origin that is the production Workbox `
        + "precache, so a dev session alone can strand a rebuilt bundle"),
    ).toEqual([]);
    expect(
      result.survivors,
      node("sw-activate-keeps-foreign-caches",
        `foreign caches did not survive activation (survivors ${JSON.stringify(result.survivors)} `
        + `of ${JSON.stringify(FOREIGN_CACHES)}; keys after: ${JSON.stringify(result.after)})`),
    ).toEqual(expect.arrayContaining(FOREIGN_CACHES));
    expect(
      result.legacySurvivors,
      node("sw-activate-keeps-foreign-caches",
        "the retired cache names A4-1 lists survived activation, so an old strategy's entries "
        + `are still able to answer (survivors ${JSON.stringify(result.legacySurvivors)} of `
        + `${JSON.stringify(LEGACY_CACHES)}; deleted was ${JSON.stringify(result.report.deleted)})`),
    ).toEqual([]);
  });

  test("sw-offline-boot-serves-cached-code — refusing HTML for code must not refuse the cache", async ({
    page,
    context,
  }) => {
    test.setTimeout(120_000);
    /* This node exists because the first version of the HTML guard was written as
       "code requests are network-only", which broke an offline reload: the app could not
       fetch its already-cached modules and never booted.  Only another lane's gate caught
       it, so the contract is policed here as well. */
    await page.goto("/v8/live");
    await page.waitForFunction(() => Boolean(navigator.serviceWorker.controller), null, {
      timeout: 20_000,
    });
    await page.reload({ waitUntil: "networkidle" });

    const failedScripts: string[] = [];
    page.on("requestfailed", (request) => {
      if (["script", "style"].includes(request.resourceType())) {
        failedScripts.push(`${request.url().slice(-46)} :: ${request.failure()?.errorText}`);
      }
    });

    await context.setOffline(true);
    await page.reload({ waitUntil: "load" }).catch(() => undefined);
    await page.waitForTimeout(2000);
    const booted = await page.evaluate(() => document.querySelectorAll("#root > *").length);
    const cached = await page.evaluate(async () => {
      const names = await caches.keys();
      let entries = 0;
      for (const name of names) entries += (await (await caches.open(name)).keys()).length;
      return { names, entries };
    });
    await context.setOffline(false);

    writeFileSync(
      evidencePath("ui-overnight/sw-offline-boot.json"),
      JSON.stringify(
        { generatedBy: "e345b9d6", order: "ORD-0036", node: "sw-offline-boot-serves-cached-code", booted, cached, failedScripts },
        null,
        2,
      ) + "\n",
      "utf-8",
    );

    expect(
      cached.entries,
      node("sw-offline-boot-serves-cached-code",
        "nothing was cached while online, so an offline boot could not be expected either way"),
    ).toBeGreaterThan(10);
    expect(
      failedScripts,
      node("sw-offline-boot-serves-cached-code",
        `offline code requests were refused instead of served from cache: ${JSON.stringify(failedScripts)}`),
    ).toEqual([]);
    expect(
      booted,
      node("sw-offline-boot-serves-cached-code",
        "the app did not boot offline even though the cache is populated -- the HTML guard "
        + "must fall back to a cached response, not to a failure"),
    ).toBeGreaterThan(0);
  });
});
