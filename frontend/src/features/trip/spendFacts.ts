import type { V8Data } from "../../app/v8Types";
import { localDateKey } from "./TripItinerary";
import { currencyExponent, currencyMinorFactor } from "../../lib/money";

/**
 * ORD-0504 -- the trip's spend, assembled from amounts this server actually sent.
 *
 * Why this file exists at all: the 花费 tab was the only page in the trips workspace that could not
 * name a single thing the traveller is being asked to pay. It printed the declared budget (a promise
 * the traveller made to themselves, from `journeys.budget_amount_minor`) and a stop count, under a
 * heading that reads "这趟旅行花多少". A number that is not a price and a count that is not a total
 * are what the tab had.
 *
 * The order's first clause was a measurement, and it was made (`review/ord0504/spend_surface_r1.py`):
 * the store holds 22 money columns, and the two read routes this screen already has --
 * `GET /journeys/{id}/experience` and `GET /journeys/{id}/wallet` -- carry four classes of real
 * price: plan legs (`cost_amount_minor` + `cost_status`), the selected stay option
 * (`stay_total_minor`), transport reservations (`price` + `price_status`), and commitments
 * (`monetary_value` + `monetary_value_status`). Five other classes exist in the store and reach no
 * journey-scoped read at all; those are named in `missing` rather than estimated, because a screen
 * that shows a plausible number for a class the server never sent is the exact defect this order is
 * not allowed to create.
 *
 * Four rules this file does not bend:
 *
 *  1. **No invented money.** Every amount here came out of a door response. There is no exchange
 *     rate, no unit price, no default, and no fallback to zero: an absent amount stays absent and is
 *     counted in `unknown_count`, which is a different claim from "0.00".
 *  2. **One currency per total.** Amounts are summed only inside their own currency, so a CNY leg
 *     and a USD reservation never merge into a figure that means neither.
 *  3. **The confirmed/estimated split is the server's, not this device's opinion.** Each class maps
 *     through the table below, keyed on the words the answer actually carries
 *     (`CONFIRMED`/`FULFILLED`, `C2_Reserved`..`C4_Anchored`, `KNOWN`/`FREE_KNOWN`/`ESTIMATED`/
 *     `UNKNOWN`). A word this table does not know lands in `UNNAMED`, which is reported as unknown
 *     money -- not quietly added to either side.
 *  4. **No double counting.** A plan leg is the plan's own segment and the mobility fact it was
 *     resolved from is the same money seen twice, so only the leg is summed. A reservation that
 *     carries a `commitment_id` and a commitment that names that reservation are also one promise;
 *     the reservation is counted and the commitment is recorded as its backing, not as a second cost.
 */

export type SpendStrength = "CONFIRMED" | "ESTIMATED" | "UNNAMED";

export type SpendLine = {
  /** Stable per fact so a screen key and a judge can point at one line. */
  id: string;
  /** Which answer this came from -- never a label invented here. */
  source: "LEG" | "STAY_OPTION" | "RESERVATION" | "COMMITMENT";
  strength: SpendStrength;
  /** The server's own word, kept verbatim so the screen can show what it read. */
  status: string;
  amountMinor: number | null;
  currency: string | null;
  /** Local calendar day in the trip's own timezone, or null when the answer gives no instant. */
  dayKey: string | null;
  /** The two ids a traveller may recognise from elsewhere on the screen. */
  fromKey?: string;
  toKey?: string;
  confirmationRef?: string | null;
};

export type SpendGroup = {
  currency: string;
  confirmedMinor: number;
  estimatedMinor: number;
  /** How many lines are actually in each bucket. A bucket with none is not a zero: "估算 USD 0.00"
   *  reads as "the estimates cost nothing" as easily as it reads as "there are no estimates", and the
   *  second one is the only thing this server knows. The face prints the count-free words instead. */
  confirmedLines: number;
  estimatedLines: number;
  /** Lines this server said it has and could not price. Not zero, and not silent. */
  unknownCount: number;
  lineCount: number;
};

export type SpendDay = {
  dayKey: string;
  /** Only currencies this day actually priced. A day with nothing but unpriced lines has none. */
  byCurrency: Array<{
    currency: string; confirmedMinor: number; estimatedMinor: number;
    confirmedLines: number; estimatedLines: number;
  }>;
  unpriced: number;
};

export type SpendModel = {
  lines: SpendLine[];
  groups: SpendGroup[];
  /** Lines the server answered without an amount. Counted, never shown as a total. */
  unpriced: number;
  /** Grouped by the trip's own day, each day split by currency -- a day with two currencies shows
   *  both, because adding them would be the invention rule again. */
  days: SpendDay[];
  /** Money the store holds and no journey-scoped read answers. Named, never guessed. */
  missing: string[];
  /** Reservations whose money is also carried by a commitment -- one promise, counted once. */
  backedCommitmentIds: string[];
};

/**
 * The classes that exist in `backend/app/db/models.py` and reach no read route this screen has.
 * Measured, not assumed: `review/ord0504/spend_surface_r1.json` records the answer each candidate
 * door gave, and the activity/plan fields are absent from the `/experience` body it also records.
 */
export const SPEND_CLASSES_WITHOUT_A_READ = [
  "STOP_ESTIMATE",
  "PLAN_TOTAL",
  "BOOKING",
  "ORDER",
  "TRANSACTION",
] as const;

/** The server's own words for "this promise is real", read off `backend/db/schema.sql`. */
const ANCHORED_LEVELS = new Set(["C2_Reserved", "C3_Guaranteed", "C4_Anchored"]);
const LIVE_COMMITMENT_STATES = new Set(["ACTIVE", "FULFILLED"]);

function legStrength(status: string): SpendStrength {
  // A leg is a planned segment: even a KNOWN cost is a price the traveller has not been charged, so
  // it is never CONFIRMED here. `UNKNOWN` is not "free" either -- it is an amount this server does
  // not have, and `amountMinor` is null for exactly that reason.
  if (status === "KNOWN") return "ESTIMATED";
  if (status === "ESTIMATED") return "ESTIMATED";
  return "UNNAMED";
}

function reservationStrength(status: string, priceStatus: string): SpendStrength {
  if (status === "CONFIRMED" && priceStatus === "KNOWN") return "CONFIRMED";
  if (status === "CONFIRMED" && priceStatus === "FREE_KNOWN") return "CONFIRMED";
  if (priceStatus === "ESTIMATED") return "ESTIMATED";
  return "UNNAMED";
}

function commitmentStrength(status: string, level: string, valueStatus: string): SpendStrength {
  if (LIVE_COMMITMENT_STATES.has(status) && ANCHORED_LEVELS.has(level) && valueStatus === "KNOWN") {
    return "CONFIRMED";
  }
  if (valueStatus === "ESTIMATED") return "ESTIMATED";
  if (LIVE_COMMITMENT_STATES.has(status) && ANCHORED_LEVELS.has(level)) return "UNNAMED";
  return "UNNAMED";
}

type WalletLike = {
  reservations?: Array<{
    id: string;
    confirmation_ref?: string | null;
    status?: string;
    scheduled_departure_at?: string | null;
    price?: { amount_minor?: number | null; currency?: string | null; status?: string | null } | null;
    commitment_id?: string | null;
  }>;
  commitments?: Array<{
    id: string;
    kind?: string;
    level?: string;
    status?: string;
    monetary_value?: { amount_minor?: number | null; currency?: string | null } | null;
    monetary_value_status?: string | null;
    policy?: { scheduled_start_at?: string | null } | null;
  }>;
} | null | undefined;

/**
 * The one assembly. Pure: same two answers in, same model out, no fetch and no clock -- the trip's
 * timezone is an argument because "which day is this stop on" is a fact about the trip, not about
 * the device that opened the page.
 */
export function buildSpendModel(
  data: V8Data | null,
  wallet: WalletLike,
  timeZone: string,
): SpendModel {
  const lines: SpendLine[] = [];
  const plan = data?.active_plan ?? null;

  for (const leg of plan?.legs ?? []) {
    const cost = (leg as { cost_amount_minor?: number | null }).cost_amount_minor;
    const status = String((leg as { cost_status?: string }).cost_status ?? "");
    const currency = (leg as { currency?: string | null }).currency ?? null;
    const departAt = (leg as { depart_at?: string | null }).depart_at ?? null;
    lines.push({
      id: `LEG:${leg.id}`,
      source: "LEG",
      strength: legStrength(status),
      status,
      amountMinor: typeof cost === "number" ? cost : null,
      currency,
      dayKey: departAt ? localDateKey(departAt, timeZone) : null,
      fromKey: leg.from_key,
      toKey: leg.to_key,
    });
  }

  const chosen = (data?.options ?? []).find((item) => item.id === data?.selected_option_id);
  if (chosen) {
    // The stay the traveller actually picked. Selected is not booked, so this is an estimate by the
    // same rule as a leg -- and the label the screen uses says 已选, never 已确认.
    const total = chosen.stay_total_minor;
    lines.push({
      id: `STAY_OPTION:${chosen.id}`,
      source: "STAY_OPTION",
      strength: typeof total === "number" ? "ESTIMATED" : "UNNAMED",
      status: "SELECTED_OPTION",
      amountMinor: typeof total === "number" ? total : null,
      currency: chosen.stay?.currency ?? null,
      dayKey: chosen.stay?.check_in_at ? localDateKey(chosen.stay.check_in_at, timeZone) : null,
    });
  }

  const countedCommitments = new Set<string>();
  for (const reservation of wallet?.reservations ?? []) {
    const price = reservation.price ?? {};
    const amount = price.amount_minor;
    lines.push({
      id: `RESERVATION:${reservation.id}`,
      source: "RESERVATION",
      strength: reservationStrength(String(reservation.status ?? ""), String(price.status ?? "")),
      status: String(price.status ?? ""),
      amountMinor: typeof amount === "number" ? amount : null,
      currency: price.currency ?? null,
      dayKey: reservation.scheduled_departure_at
        ? localDateKey(reservation.scheduled_departure_at, timeZone)
        : null,
      confirmationRef: reservation.confirmation_ref ?? null,
    });
    if (reservation.commitment_id) countedCommitments.add(reservation.commitment_id);
  }

  for (const commitment of wallet?.commitments ?? []) {
    const value = commitment.monetary_value ?? {};
    const amount = value.amount_minor;
    const backed = countedCommitments.has(commitment.id);
    /* `commitments.monetary_value_minor` is NOT NULL, so a price the server never learned is stored as 0
       with `monetary_value_status = UNKNOWN`. Reading the digit made this face print `CNY 0.00` for an
       amount it had been told twice -- once that it exists, once that it is unknown -- while the same
       column already had the words 还没有价 for every other unpriced line. */
    const learned = String(commitment.monetary_value_status ?? "") === "KNOWN";
    lines.push({
      id: `COMMITMENT:${commitment.id}`,
      source: "COMMITMENT",
      // A commitment that backs a reservation already counted is reported with strength UNNAMED and
      // no amount: it is the same promise seen twice, and the second sighting must not add money.
      strength: backed ? "UNNAMED" : commitmentStrength(
        String(commitment.status ?? ""), String(commitment.level ?? ""),
        String(commitment.monetary_value_status ?? ""),
      ),
      status: backed ? "BACKED_BY_RESERVATION" : String(commitment.monetary_value_status ?? ""),
      amountMinor: backed || !learned
        ? null
        : typeof amount === "number"
          ? amount
          : null,
      currency: backed ? null : value.currency ?? null,
      dayKey: !backed && commitment.policy?.scheduled_start_at
        ? localDateKey(commitment.policy.scheduled_start_at, timeZone)
        : null,
    });
  }

  const groups = new Map<string, SpendGroup>();
  const days = new Map<string, SpendDay>();
  let unpriced = 0;
  for (const line of lines) {
    /* A line the server could not price is counted, never totalled. It used to get its own currency
       group under the label `UNKNOWN_CURRENCY` with two zeros beside it, which is the exact shape this
       order is not allowed to produce: a currency that exists in no column, and a "0.00" a traveller
       reads as an answer. Found by this lane's own counterexample arm on a trip with no priced fact. */
    if (line.amountMinor === null || !line.currency) {
      unpriced += 1;
      if (line.dayKey) {
        const day = days.get(line.dayKey) ?? { dayKey: line.dayKey, byCurrency: [], unpriced: 0 };
        day.unpriced += 1;
        days.set(line.dayKey, day);
      }
      continue;
    }
    const currency = line.currency;
    const group = groups.get(currency) ?? {
      currency, confirmedMinor: 0, estimatedMinor: 0, unknownCount: 0, lineCount: 0,
      confirmedLines: 0, estimatedLines: 0,
    };
    group.lineCount += 1;
    if (line.strength === "CONFIRMED") {
      group.confirmedMinor += line.amountMinor;
      group.confirmedLines += 1;
    } else if (line.strength === "ESTIMATED") {
      group.estimatedMinor += line.amountMinor;
      group.estimatedLines += 1;
    } else {
      group.unknownCount += 1;
    }
    groups.set(currency, group);

    if (line.dayKey) {
      const day = days.get(line.dayKey) ?? { dayKey: line.dayKey, byCurrency: [], unpriced: 0 };
      let cell = day.byCurrency.find((item) => item.currency === currency);
      if (!cell) {
        cell = { currency, confirmedMinor: 0, estimatedMinor: 0, confirmedLines: 0, estimatedLines: 0 };
        day.byCurrency.push(cell);
      }
      if (line.strength === "CONFIRMED") {
        cell.confirmedMinor += line.amountMinor;
        cell.confirmedLines += 1;
      } else if (line.strength === "ESTIMATED") {
        cell.estimatedMinor += line.amountMinor;
        cell.estimatedLines += 1;
      }
      days.set(line.dayKey, day);
    }
  }

  return {
    lines,
    groups: [...groups.values()].sort((a, b) => a.currency.localeCompare(b.currency)),
    unpriced,
    days: [...days.values()].sort((a, b) => a.dayKey.localeCompare(b.dayKey)),
    missing: [...SPEND_CLASSES_WITHOUT_A_READ],
    backedCommitmentIds: [...countedCommitments],
  };
}

/**
 * Minor units to the string a traveller reads. One division, no rounding of an unknown -- and the
 * divisor is the currency's own exponent, asked of `lib/money.ts` rather than assumed. This function
 * used to divide every currency by 100, which drew the same ticket one hundred times small beside the
 * 预订 face's `¥4,800`: JPY, KRW, VND and IDR carry no minor fraction, so 4800 minor is 4,800 yen.
 */
export function moneyText(amountMinor: number, currency: string): string {
  const digits = currencyExponent(currency);
  return `${currency} ${(amountMinor / currencyMinorFactor(currency)).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}
