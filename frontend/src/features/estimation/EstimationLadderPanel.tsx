/**
 * ORD-0455 -- the estimation ladder on screen.
 *
 * Renders the rungs `estimationLadder.ts` derived for one estimate, in ladder order, each with its
 * tier's own words in front of it. Three things this deliberately does not do:
 *  - It is not a collapsible "技术详情" block. A warning a traveller has to open is a warning the
 *    traveller did not read, and F15's point is that the four steps sit in the same view as the
 *    number.
 *  - It adds no control. The 给补救 rung names a door that already exists elsewhere in the app, and
 *    `{door}` arrives as `t("nav.studio")` so the sentence and the nav strip cannot disagree.
 *  - It never drops a rung it was given and never adds one. The list *is* the derivation, so a green
 *    screen means the triggers fired -- not that a component found some of them noisy.
 *
 * The facts behind the triggers travel as attributes on the list, not as painted text: this is a
 * traveller's screen, and the current line is being measured on how much machine vocabulary reaches
 * it. A gate reads `data-ladder-*`; the assertion on a rung reads the painted sentence.
 */

import { useLocale } from "../../i18n/LocaleContext";
import { estimationLadder, type EstimateReading, type EstimationTier } from "./estimationLadder";
import { fillLadder, ladderCopyFor, type LadderCopyKey } from "./estimationLadderCopy";
import "./estimationLadder.css";

const TIER_LABEL: Record<EstimationTier, LadderCopyKey> = {
  method: "ladder.tier.method",
  pre_warning: "ladder.tier.pre",
  post_verify: "ladder.tier.verify",
  remedy: "ladder.tier.remedy",
};

const flag = (value: boolean): string => (value ? "yes" : "no");

export function EstimationLadderPanel({
  estimate,
  sourceToken,
}: {
  /** The cells this server sent about the figure this ladder sits under. */
  estimate: EstimateReading;
  /** The raw `stay.evidence.source`, kept as an anchor so a reading can name the value behind the
   *  traveller words. null when the payload carried no source cell at all. */
  sourceToken: string | null;
}) {
  const { locale } = useLocale();
  const { facts, rungs } = estimationLadder(estimate);

  return (
    <ul
      className="cnx-ladder"
      data-testid="estimation-ladder"
      data-ladder-rungs={rungs.map((rung) => rung.tier).join("+")}
      data-ladder-has-amount={flag(facts.has_amount)}
      data-ladder-has-source={flag(facts.has_source)}
      data-ladder-has-confidence={flag(facts.has_confidence)}
      data-ladder-binding={flag(facts.binding)}
      data-ladder-can-switch={flag(facts.can_switch)}
      data-ladder-can-recompute={flag(facts.can_recompute)}
      data-ladder-has-candidates={flag(facts.has_candidates)}
      data-ladder-candidates={estimate.candidates === undefined ? "" : String(estimate.candidates)}
      data-ladder-amount-field="stay.total_amount_minor"
      data-ladder-amount-value={estimate.amountMinor === null ? "" : String(estimate.amountMinor)}
      data-cnx-field="stay.evidence.source"
      data-cnx-value={sourceToken ?? ""}
    >
      {rungs.map((rung) => (
        <li
          key={rung.key}
          className="cnx-ladder-rung"
          data-testid={`ladder-rung-${rung.tier}`}
          data-ladder-tier={rung.tier}
          data-ladder-trigger={rung.trigger}
          data-ladder-key={rung.key}
        >
          <b className="cnx-ladder-tier">{ladderCopyFor(locale, TIER_LABEL[rung.tier])}</b>
          <span className="cnx-ladder-said">
            {fillLadder(ladderCopyFor(locale, rung.key as LadderCopyKey), rung.vars)}
          </span>
        </li>
      ))}
    </ul>
  );
}
