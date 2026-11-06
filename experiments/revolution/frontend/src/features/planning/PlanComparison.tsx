/**
 * Candidate plan comparison — "比较候选方案 -> 采用方案".
 *
 * Design rules honoured:
 *  - the budget relationship is REAL: within budget / near the limit / over by
 *    an explicit amount with a stated reason (design corrections §7);
 *  - only facts the backend actually returned are shown; missing data is
 *    omitted, never invented;
 *  - stale / non-selectable candidates are shown as such and cannot be chosen.
 */

import { useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import type { PlanVersion } from "../../api/v12Authoritative";
import { readResponseFacts, type RawFacts } from "../../lib/rawFacts";
import { RawFactsPanel } from "../../components/RawFactsPanel";

// The generated OpenAPI PlanVersion schema lags the backend, which also emits
// plan.stale, plan.selectable and per-activity cost_status / metadata. View
// types layer those additive runtime fields without editing the generated schema.
type PlanVersionView = PlanVersion & { stale?: boolean; selectable?: boolean };
type PlanActivityView = NonNullable<PlanVersion["activities"]>[number] & {
  cost_status?: string;
  metadata?: Record<string, unknown>;
};

const MINOR = 100;

function money(amountMinor: number, currency: string, zh: boolean): string {
  const major = Math.round(amountMinor / MINOR);
  return zh ? `${currency} ${major.toLocaleString("zh-CN")}` : `${currency} ${major.toLocaleString("en-GB")}`;
}

function budgetBand(
  costMinor: number,
  budgetMinor: number,
): { key: "within" | "near" | "over"; deltaMinor: number } {
  if (budgetMinor <= 0) return { key: "within", deltaMinor: 0 };
  const ratio = costMinor / budgetMinor;
  if (ratio <= 0.9) return { key: "within", deltaMinor: costMinor - budgetMinor };
  if (ratio <= 1.0) return { key: "near", deltaMinor: costMinor - budgetMinor };
  return { key: "over", deltaMinor: costMinor - budgetMinor };
}

function walkingMinutes(plan: PlanVersion): number | null {
  const legs = (plan.activities ?? []).length;
  const evidence = plan.mobility_evidence as { facts?: Array<{ walking_min?: number | null; mode?: string | null }> } | undefined;
  const facts = evidence?.facts;
  if (!Array.isArray(facts) || facts.length === 0) return null;
  let total = 0;
  let seen = false;
  for (const fact of facts) {
    if (fact?.walking_min != null) {
      total += Number(fact.walking_min);
      seen = true;
    } else if (String(fact?.mode || "").toUpperCase() === "WALK") {
      seen = true;
    }
  }
  return seen ? total : null;
}

export function PlanComparison({
  journeyId,
  plans,
  budgetMinor,
  currency,
  onSelected,
}: {
  journeyId: string;
  plans: PlanVersion[];
  budgetMinor: number;
  currency: string;
  onSelected: () => void;
}) {
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [banner, setBanner] = useState<{ text: string | null; facts: RawFacts | null }>({
    text: null,
    facts: null,
  });
  /** Text and reading are one announcement: a sentence from a later attempt must not stay up beside
   *  the fields an earlier one measured, so every setError clears the reading unless it passes one. */
  const error = banner.text;
  const setError = (text: string | null, facts: RawFacts | null = null) =>
    setBanner({ text, facts });

  const candidates = (plans as PlanVersionView[]).filter(
    (plan) => plan.status === "CANDIDATE" && !plan.stale && plan.selectable !== false,
  );
  if (candidates.length === 0) return null;

  const choose = async (planId: string) => {
    setBusyId(planId);
    setError(null);
    try {
      const response = await fetch(
        `/api/v1/journeys/${journeyId}/plans/${planId}/select`,
        { method: "POST", headers: { Accept: "application/json" } },
      );
      if (!response.ok) {
        /* Q64=1: the refusal's own sentence stays out of the DOM.  What this device measured --
           status, media type, byte count -- goes on screen beside our own wording, so the traveller
           can still tell "the server said no" from "the server never answered". */
        const { facts } = await readResponseFacts(response);
        setError(
          zh ? "这个方案暂时不能采用。" : "This plan could not be chosen.",
          facts,
        );
        return;
      }
      onSelected();
    } catch {
      setError(zh ? "连不上服务器，请重试。" : "Could not reach the server. Try again.");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section className="plan-cmp" data-testid="plan-comparison">
      <div className="plan-cmp-head">
        <h2>{zh ? "选择你的方案" : "Choose your plan"}</h2>
        <p>
          {zh
            ? `我们按这座城可核验的地点和你的预算，生成了 ${candidates.length} 个可选方案。`
            : `We built ${candidates.length} options from the places we can verify for this city and this trip's budget.`}
          {/* ORD-0238: this sentence used to promise the plans followed her 节奏.  `pace` never
              leaves the browser, and `generate_plans(session, journey, planning_mode,
              candidate_count, place_ids)` reads neither it nor the stored preferences -- naming an
              input the planner does not take is a claim, not a description.  It comes back when a
              consumer exists. */}
        </p>
      </div>

      <div className="plan-cmp-list">
        {candidates.map((plan, index) => {
          const costMinor = plan.total_cost?.amount_minor ?? 0;
          // A plan whose stops carry no price evidence must not be presented as
          // "within budget": the total is unknown, not zero.  Claiming a budget
          // relationship from missing data is a fabricated fact.
          // Partial coverage is not coverage: if ANY stop's price is unknown the
          // total is unknown, so no within/over verdict may be claimed from it.
          const stops = (plan.activities ?? []) as PlanActivityView[];
          const knownFlags = stops.map((activity) => {
            const status = String(activity.cost_status ?? "").toUpperCase();
            if (status === "KNOWN_FREE" || status === "KNOWN_PAID") return true;
            if (status === "UNKNOWN") return false;
            const meta = (activity.metadata ?? {}) as Record<string, unknown>;
            return meta.price_known === true || (activity.estimated_cost?.amount_minor ?? 0) > 0;
          });
          const knownCount = knownFlags.filter(Boolean).length;
          const allPricesKnown = stops.length > 0 && knownCount === stops.length;
          const partialPrices = knownCount > 0 && knownCount < stops.length;
          const costKnown = allPricesKnown;
          const band = budgetBand(costMinor, budgetMinor);
          const walk = walkingMinutes(plan);
          const highlights = (plan.activities ?? [])
            .slice(0, 3)
            .map((activity) => activity.title)
            .filter(Boolean);
          const bandLabel = partialPrices
            ? zh
              ? `部分价格未知（${knownCount}/${stops.length}）`
              : `Partly unknown (${knownCount}/${stops.length})`
            : !costKnown
            ? zh
              ? "花费未知"
              : "Cost unknown"
            : band.key === "within"
              ? zh
                ? "预算内"
                : "Within budget"
              : band.key === "near"
                ? zh
                  ? "接近预算上限"
                  : "Near your limit"
                : zh
                  ? `超预算 ${money(Math.abs(band.deltaMinor), currency, zh)}`
                  : `Over by ${money(Math.abs(band.deltaMinor), currency, zh)}`;
          return (
            <article
              key={plan.id}
              className="plan-cmp-card"
              data-testid="plan-cmp-card"
            >
              <header>
                <b>
                  {zh ? `方案 ${String.fromCharCode(65 + index)}` : `Option ${String.fromCharCode(65 + index)}`}
                </b>
                <span
                  className={`plan-cmp-band ${costKnown ? band.key : "unknown"}`}
                  data-testid="plan-cmp-band"
                >
                  {bandLabel}
                </span>
              </header>

              <dl>
                <div>
                  <dt>{zh ? "总花费" : "Total cost"}</dt>
                  <dd>
                    {allPricesKnown
                      ? money(costMinor, plan.total_cost?.currency ?? currency, zh)
                      : partialPrices
                        ? zh
                          ? `至少 ${money(costMinor, plan.total_cost?.currency ?? currency, zh)}`
                          : `At least ${money(costMinor, plan.total_cost?.currency ?? currency, zh)}`
                        : zh
                          ? "暂无价格"
                          : "Price unknown"}
                  </dd>
                </div>
                <div>
                  <dt>{zh ? "活动数" : "Stops"}</dt>
                  <dd>{(plan.activities ?? []).length}</dd>
                </div>
                {walk != null && (
                  <div>
                    <dt>{zh ? "步行" : "Walking"}</dt>
                    <dd>
                      {walk >= 60
                        ? zh
                          ? `${Math.floor(walk / 60)} 小时 ${walk % 60} 分`
                          : `${Math.floor(walk / 60)}h ${walk % 60}m`
                        : zh
                          ? `${walk} 分钟`
                          : `${walk} min`}
                    </dd>
                  </div>
                )}
              </dl>

              {highlights.length > 0 && (
                <p className="plan-cmp-highlights">
                  {zh ? "亮点：" : "Highlights: "}
                  {highlights.join(" · ")}
                </p>
              )}

              {band.key === "over" && (
                <p className="plan-cmp-why">
                  {zh
                    ? `超出预算 ${money(Math.abs(band.deltaMinor), currency, zh)}。通常来自住宿档次或更长的交通。`
                    : `This is ${money(Math.abs(band.deltaMinor), currency, zh)} over. Usually the stay tier or longer travel.`}
                </p>
              )}

              <button
                type="button"
                className="v8-primary-button"
                data-testid="plan-cmp-choose"
                disabled={busyId !== null}
                onClick={() => void choose(plan.id)}
              >
                {busyId === plan.id
                  ? zh
                    ? "采用中…"
                    : "Choosing…"
                  : zh
                    ? "采用这个方案"
                    : "Choose this plan"}
              </button>
            </article>
          );
        })}
      </div>

      {error && (
        <p className="plan-cmp-error" role="alert" data-testid="plan-cmp-error">
          {error}
        </p>
      )}
      {banner.facts && (
        <RawFactsPanel
          facts={banner.facts}
          zh={zh}
          testId="plan-cmp-raw-facts"
        />
      )}
    </section>
  );
}
