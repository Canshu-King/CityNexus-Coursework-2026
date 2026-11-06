import { expect, test, type BrowserContext, type Page } from "@playwright/test";

/* This file proves a production service-worker lifecycle, so it belongs to the
   `playwright.sw.config.ts` project, which builds and previews the shipped bundle. The
   default project runs `npm run dev`, where main.tsx registers the checked-in dev worker
   and never the generated one, so neither test could ever pass there -- a red under dev is
   not a product finding, it is a gate running in the wrong clothes. It skips itself and
   says so, rather than being deleted or loosened. */
test.beforeEach(async ({ request }) => {
  const html = await request
    .get("/v8/live")
    .then((response) => response.text())
    .catch(() => "");
  test.skip(
    html.includes("@vite/client"),
    "production-only lifecycle: the default project serves Vite dev, which never registers " +
      "/v10-sw.js. Run it with --config playwright.sw.config.ts.",
  );
});

async function resetServiceWorkerState(page: Page) {
  await page.evaluate(async () => {
    const registrations = await navigator.serviceWorker.getRegistrations();
    await Promise.all(registrations.map((registration) => registration.unregister()));
    const names = await caches.keys();
    await Promise.all(names.map((name) => caches.delete(name)));
  });
}

async function installRecoveryWorker(page: Page) {
  await page.evaluate(async () => {
    const registration = await navigator.serviceWorker.register("/sw.js", {
      scope: "/",
      updateViaCache: "none",
    });
    if (registration.waiting) {
      registration.waiting.postMessage({ type: "SKIP_WAITING" });
    }
    await navigator.serviceWorker.ready;
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => navigator.serviceWorker.controller?.scriptURL.includes("/sw.js") === true,
  );
}

async function legacyCacheNames(page: Page): Promise<string[]> {
  return page.evaluate(async () => {
    const names = await caches.keys();
    return names.filter(
      (name) =>
        name === "citynexus-pages" ||
        name === "citynexus-static" ||
        name.startsWith("citynexus-trip-operations-v"),
    );
  });
}

test("old recovery worker upgrades to the generated production worker without manual cache clearing", async ({
  browser,
  baseURL,
}) => {
  const context = await browser.newContext({ serviceWorkers: "allow" });
  const page = await context.newPage();

  // Use a same-origin non-app document so the test can install the historical
  // /sw.js scope before main.tsx gets a chance to register /v10-sw.js.
  await page.goto(`${baseURL}/manifest.webmanifest`, { waitUntil: "domcontentloaded" });
  await resetServiceWorkerState(page);
  await page.reload({ waitUntil: "domcontentloaded" });
  await installRecoveryWorker(page);

  // Seed the exact legacy cache names that previously held stale HTML/module
  // responses. This is test setup only. From this point on the test must not
  // unregister a worker or clear a cache.
  await page.evaluate(async () => {
    const pages = await caches.open("citynexus-pages");
    await pages.put(
      new Request("/v8/live"),
      new Response("<!doctype html><title>stale shell</title>", {
        status: 200,
        headers: { "content-type": "text/html; charset=utf-8" },
      }),
    );
    const statics = await caches.open("citynexus-static");
    await statics.put(
      new Request("/assets/poisoned-module.js"),
      new Response("<!doctype html><title>not javascript</title>", {
        status: 200,
        headers: { "content-type": "text/html; charset=utf-8" },
      }),
    );
  });
  expect((await legacyCacheNames(page)).length).toBeGreaterThan(0);

  await page.goto(`${baseURL}/v8/live`, { waitUntil: "domcontentloaded" });

  // Production main.tsx registers the generated worker with updateViaCache:none;
  // skipWaiting + clientsClaim move the page forward without asking the user to
  // clear storage. controllerchange may trigger the one-time safety reload.
  await page.waitForFunction(
    () => navigator.serviceWorker.controller?.scriptURL.includes("/v10-sw.js") === true,
    undefined,
    { timeout: 20_000 },
  );
  await page.waitForFunction(
    () => (document.querySelector("#root")?.childElementCount ?? 0) > 0,
    undefined,
    { timeout: 20_000 },
  );

  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => (document.querySelector("#root")?.childElementCount ?? 0) > 0,
  );

  expect(await legacyCacheNames(page)).toEqual([]);
  await context.close();
});

test("recovery worker is version-queryable and never supplies HTML to failed executable requests", async ({
  browser,
  baseURL,
}) => {
  const context: BrowserContext = await browser.newContext({ serviceWorkers: "allow" });
  const page = await context.newPage();
  await page.goto(`${baseURL}/manifest.webmanifest`, { waitUntil: "domcontentloaded" });
  await resetServiceWorkerState(page);
  await page.reload({ waitUntil: "domcontentloaded" });
  await installRecoveryWorker(page);

  const version = await page.evaluate(
    () =>
      new Promise<string>((resolve, reject) => {
        const controller = navigator.serviceWorker.controller;
        if (!controller) {
          reject(new Error("recovery worker is not controlling the page"));
          return;
        }
        const channel = new MessageChannel();
        const timer = window.setTimeout(
          () => reject(new Error("service worker version reply timed out")),
          5_000,
        );
        channel.port1.onmessage = (event) => {
          window.clearTimeout(timer);
          resolve(String(event.data?.version ?? ""));
        };
        controller.postMessage({ type: "CITYNEXUS_SW_GET_VERSION" }, [channel.port2]);
      }),
  );
  expect(version).toMatch(/^citynexus-recovery-m3-a4-v\d+$/);

  const observedHtmlExecutableResponses: string[] = [];
  page.on("response", (response) => {
    if (!response.url().includes("__sw_exec_probe__")) return;
    const contentType = response.headers()["content-type"] ?? "";
    if (contentType.includes("text/html")) {
      observedHtmlExecutableResponses.push(response.url());
    }
  });

  await context.setOffline(true);
  const result = await page.evaluate(async () => {
    const scriptResult = await new Promise<"load" | "error">((resolve) => {
      const script = document.createElement("script");
      script.src = "/__sw_exec_probe__.js";
      script.onload = () => resolve("load");
      script.onerror = () => resolve("error");
      document.documentElement.append(script);
    });

    const styleResult = await new Promise<"load" | "error">((resolve) => {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = "/__sw_exec_probe__.css";
      link.onload = () => resolve("load");
      link.onerror = () => resolve("error");
      document.documentElement.append(link);
    });

    const workerResult = await new Promise<"load" | "error">((resolve) => {
      try {
        const worker = new Worker("/__sw_exec_probe__.worker.js");
        worker.onmessage = () => {
          worker.terminate();
          resolve("load");
        };
        worker.onerror = () => {
          worker.terminate();
          resolve("error");
        };
      } catch {
        resolve("error");
      }
    });

    return { scriptResult, styleResult, workerResult };
  });
  await context.setOffline(false);

  expect(result).toEqual({
    scriptResult: "error",
    styleResult: "error",
    workerResult: "error",
  });
  expect(observedHtmlExecutableResponses).toEqual([]);

  await context.close();
});
