/* ORD-0339 (lane S1) — a mount point for the *real* JourneyTrialSetup component.
 *
 * The state under test is the sealed-demo refusal, which today takes a live backend to reach
 * through the shell.  What this order asserts is narrower and truer: given the props the shell
 * already passes (`sealedDemoUnavailable` + `sealedDemoDetail`), does an exception's own words
 * reach the document tree?  So the product component is mounted directly, with those props, and
 * the DOM is inspected.  Nothing about the server is claimed here, and this module is reachable
 * only from the e2e harness -- the production bundle never imports it.
 */
import React from "react";
import { createRoot } from "react-dom/client";
import { JourneyTrialSetup } from "../../src/features/setup/JourneyTrialSetup";
import { LocaleProvider } from "../../src/i18n/LocaleContext";

const CANARY_MESSAGE =
  "CANARY Unexpected token < in JSON at position 0 while parsing text/html; charset=utf-8 near line 1";
const CANARY_NAME = "TypeError";

declare global {
  interface Window {
    __ord0339: { canaryMessage: string; canaryName: string };
  }
}

window.__ord0339 = { canaryMessage: CANARY_MESSAGE, canaryName: CANARY_NAME };

const host = document.createElement("div");
document.body.appendChild(host);

createRoot(host).render(
  /* The screen reads locale through context, so the harness supplies the same provider the shell
     does -- without it the component throws before anything is painted. */
  React.createElement(
    LocaleProvider,
    null,
    React.createElement(JourneyTrialSetup, {
      busy: false,
      onCreate: async () => {},
      onUseSealedDemo: async () => {},
      sealedDemoUnavailable:
        "没读到封存演示的信息：服务器刚才答了，但答的内容我们用不了。这不是你填错了什么。",
      /* The shell's real shape (V8Experience.tsx): the caught exception's message and name. */
      sealedDemoDetail: { message: CANARY_MESSAGE, name: CANARY_NAME },
    }),
  ),
);
