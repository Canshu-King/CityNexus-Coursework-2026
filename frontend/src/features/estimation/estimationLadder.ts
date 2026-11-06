/**
 * ORD-0455 -- the estimation ladder.
 *
 * Pi Travel never leaves an estimate standing alone as "仅供参考". Its corpus walks a traveller up a
 * ladder: say how the number was made, warn before it is used, ask for verification after it has
 * been shown, and name what the traveller can still do about it
 * (`D:/moodle/_apk_analysis_pi_travel/MAP/map_findings.md`, item F15).
 *
 * CityNexus already owns two of those steps as behaviour -- a stay with no rate prints 价格未知
 * instead of `0` (`features/stay/StayExplorer.tsx`, `priceLabel` / `pricePair`), and a plan whose
 * prices are only partly evidenced is refused a budget verdict (`lib/budgetEvidence.ts`). What was
 * missing is the wording of the four steps and the conditions that put each one on screen. That is
 * this file: one reading in, the rungs that reading owes out.
 *
 * Three rules keep the ladder from becoming a disclaimer generator:
 *  - A rung is derived from cells the server sent, never from a mood. `estimationFacts` is the whole
 *    predicate set, so every rung can be traced to the field that lit it.
 *  - `method` and `post_verify` refuse to fire when there is no figure -- on a screen that has a
 *    candidate. A number that was never produced has no method to describe and nothing to verify, so
 *    the absence belongs to `pre_warning` and an empty amount cannot buy two extra sentences. The one
 *    exception is ORD-0468's named empty reading, where there is no candidate either: see the
 *    `no_candidates` branch below, which says the absence in the 说方法 slot and stops there.
 *  - `remedy` fires exactly where a warning fired. A warning with no door is a dead end, so the rung
 *    names the door that exists; when none does, it says that and stops.
 *
 * Wording is not here -- see `estimationLadderCopy.ts`. This module answers "which rungs, and why";
 * the copy module answers "in what words".
 */

export const ESTIMATION_TIERS = ["method", "pre_warning", "post_verify", "remedy"] as const;
export type EstimationTier = (typeof ESTIMATION_TIERS)[number];

/** One estimate, reduced to the cells this server actually sent about it. */
export type EstimateReading = {
  /** The figure the screen would show, in minor units. null when it sent none. */
  amountMinor: number | null;
  /** That same figure in the words the screen already prints (`¥388.00`), so a rung can never quote
   *  a number that disagrees with the card above it. "" when the screen shows no figure. */
  amountLabel: string;
  /** `stay.evidence.source` -- where the answer came from, as labelled. "" when unlabelled. */
  source: string;
  /** `stay.evidence.confidence`, 0..1. null when the server sent no confidence at all. */
  confidence: number | null;
  /** Acting on this estimate carries an irreversible consequence (`stay.refundable === false`). */
  binding: boolean;
  /** How many other candidates this screen can offer in place of this one. */
  alternatives: number;
  /** A door that recomputes this figure exists -- Trip Studio re-plans under the same constraints. */
  canRecompute: boolean;
  /** The traveller's own word for that door (`t("nav.studio"` on every host), passed in so the
   *  remedy rung names the door the nav strip shows instead of a name this file invented. */
  doorLabel: string;
  /** ORD-0468 -- how many candidates this screen was given at all. Omitted means "this is a reading
   *  about a candidate that exists", which is every host that shipped before this order. 0 is a
   *  different question: there is no candidate, so there is no figure whose method could be named. */
  candidates?: number;
};

/** The predicate set every rung is read from, so a trigger condition is a reading and not a
 *  paragraph of prose. The screen carries the same names as `data-ladder-*`. */
export type EstimationFacts = {
  has_amount: boolean;
  has_source: boolean;
  has_confidence: boolean;
  binding: boolean;
  can_switch: boolean;
  can_recompute: boolean;
  has_candidates: boolean;
};

/** One rung this reading owes: which tier, which words, and the named condition that lit it. */
export type LadderRung = {
  tier: EstimationTier;
  key: string;
  trigger: string;
  vars: Record<string, string | number>;
};

export type EstimationLadder = { facts: EstimationFacts; rungs: LadderRung[] };

const word = (value: string | null | undefined): string => String(value ?? "").trim();

export function estimationFacts(reading: EstimateReading): EstimationFacts {
  return {
    // A zero amount is not a figure to reason about: OSM carries no rate, so a live stay arrives as
    // 0 and `StayExplorer` already refuses to print that as a price.
    has_amount: typeof reading.amountMinor === "number" && reading.amountMinor > 0,
    has_source: word(reading.source).length > 0,
    has_confidence: typeof reading.confidence === "number" && reading.confidence > 0,
    binding: Boolean(reading.binding),
    can_switch: reading.alternatives > 0,
    can_recompute: Boolean(reading.canRecompute),
    // An omitted `candidates` is not a zero: it means the host is describing a candidate it has.
    has_candidates: reading.candidates === undefined || reading.candidates > 0,
  };
}

/** Confidence as a traveller reads it. `0%` would look like a measurement of zero trust, and an
 *  absent cell is not a zero, so an absent cell yields no number at all. */
export function confidencePercent(confidence: number | null): string {
  return typeof confidence === "number" && confidence > 0
    ? String(Math.round(confidence * 100))
    : "";
}

export function estimationLadder(reading: EstimateReading): EstimationLadder {
  const facts = estimationFacts(reading);
  // A figure is only "on screen" when the host that owns currency actually produced words for it.
  const shown = facts.has_amount && word(reading.amountLabel).length > 0;
  const vars: Record<string, string | number> = {
    amount: word(reading.amountLabel),
    source: word(reading.source),
    confidence: confidencePercent(reading.confidence),
    n: Math.max(0, reading.alternatives),
    door: word(reading.doorLabel),
  };
  const rungs: LadderRung[] = [];

  // ORD-0468 -- the screen has no candidate at all. That is not a figure with an absent cell; it is
  // no figure and no candidate. The ladder says its own name there ("这一级无可报的方法") instead of
  // disappearing, because an absent block teaches the traveller nothing about which question went
  // unanswered. Only this one rung fires: there is nothing to warn about comparing, nothing to
  // verify with a provider, and the empty state's own sentence above already names the door.
  if (!facts.has_candidates) {
    return {
      facts,
      rungs: [{ tier: "method", key: "ladder.method.noCandidates", trigger: "no_candidates", vars: {} }],
    };
  }

  // ① 说方法 -- about the figure that exists, and only about that one.
  if (shown && facts.has_source && facts.has_confidence) {
    rungs.push({
      tier: "method",
      key: "ladder.method.sourced",
      trigger: "amount+source+confidence",
      vars,
    });
  } else if (shown && facts.has_source) {
    rungs.push({
      tier: "method",
      key: "ladder.method.noConfidence",
      trigger: "amount+source,no_confidence",
      vars,
    });
  } else if (shown) {
    rungs.push({
      tier: "method",
      key: "ladder.method.noSource",
      trigger: "amount,no_source",
      vars,
    });
  }

  // ② 事前警告 -- before anyone compares this with anything else.
  if (!shown) {
    rungs.push({ tier: "pre_warning", key: "ladder.pre.noAmount", trigger: "no_amount", vars });
  } else if (!facts.has_confidence) {
    rungs.push({
      tier: "pre_warning",
      key: "ladder.pre.noConfidence",
      trigger: "amount,no_confidence",
      vars,
    });
  }

  // ③ 事后核对 -- owed precisely when the figure looks usable enough that someone will act on it.
  if (shown) {
    rungs.push({
      tier: "post_verify",
      key: facts.binding ? "ladder.verify.binding" : "ladder.verify.open",
      trigger: facts.binding ? "amount,non_refundable" : "amount,refundable",
      vars,
    });
  }

  // ④ 给补救 -- wherever a warning was raised, name the door; when there is none, name that.
  if (!shown || !facts.has_confidence) {
    if (facts.can_switch) {
      rungs.push({ tier: "remedy", key: "ladder.remedy.switch", trigger: "door_switch", vars });
    } else if (facts.can_recompute) {
      rungs.push({
        tier: "remedy",
        key: "ladder.remedy.recompute",
        trigger: "door_recompute",
        vars,
      });
    } else {
      rungs.push({ tier: "remedy", key: "ladder.remedy.absent", trigger: "no_door", vars });
    }
  }

  return { facts, rungs };
}

/** Tier names alone, in ladder order -- what a gate compares a screen against. */
export function ladderTiers(ladder: EstimationLadder): EstimationTier[] {
  return ladder.rungs.map((rung) => rung.tier);
}
