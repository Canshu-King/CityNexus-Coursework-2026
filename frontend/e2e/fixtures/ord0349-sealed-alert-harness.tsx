/* ORD-0349 (lane S1) -- one mount of the real sealed-demo alert per page load, with the sentence read
 * through the product's own `useT`.  One case per load is deliberate: three stacked wizards put the
 * second and third alerts thousands of pixels apart, and an element screenshot of something that far
 * down a page picks up whatever happens to overlap it, which is the noise a control arm must not see.
 * The spec freezes positions with a style override before shooting, so the three loads differ only in
 * the sentence -- that is the thing under test.
 */
import React from "react";
import { createRoot } from "react-dom/client";
import { JourneyTrialSetup } from "../../src/features/setup/JourneyTrialSetup";
import { LocaleProvider, useT } from "../../src/i18n/LocaleContext";

function SealedAlertCase({ sentence }: { sentence: string }) {
  return (
    <div data-testid="ord0349-case">
      <JourneyTrialSetup
        busy={false}
        onCreate={async () => {}}
        onUseSealedDemo={async () => {}}
        sealedDemoUnavailable={sentence}
      />
    </div>
  );
}

function WhichCase() {
  const t = useT();
  const answered = t("trial.sealedUnreadable");
  const noAnswer = t("trial.sealedNoAnswer");
  const wanted = new URLSearchParams(window.location.search).get("case") ?? "answered";
  const sentence = wanted === "no-answer" ? noAnswer : answered;
  return (
    <>
      <span data-testid="ord0349-dict-answered">{answered}</span>
      <span data-testid="ord0349-dict-no-answer">{noAnswer}</span>
      <SealedAlertCase sentence={sentence} />
    </>
  );
}

const host = document.createElement("div");
document.body.appendChild(host);
createRoot(host).render(React.createElement(LocaleProvider, null, React.createElement(WhichCase, {})));
