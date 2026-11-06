import { useEffect, useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import { useAuthoritativePlans } from "../../api/v12Authoritative";
import { rankPlans, RANK_DIMS, type ComparablePlan, type RankDim } from "./planRanking";
import { fillRank, rankCopy, type PlanRankCopyKey } from "./planRankingCopy";
import "./planRanking.css";

/* ORD-0439: the comparison screen. It ranks the candidates the server already holds -- it never
   manufactures one, and it never shows a number the payload did not send. */

/* `stale` and `selectable` are answered by the route but are not in the committed generated types
   (the same gap `PlanComparison.tsx` documents), so the view type declares them here instead of
   widening a contract claim. */
type Row = ComparablePlan & {
  id: string;
  version_no: number;
  status: string;
  stale?: boolean;
  selectable?: boolean;
};

const DIM_KEYS: Record<RankDim, PlanRankCopyKey> = {
  cost: "rank.dim.cost",
  walk: "rank.dim.walk",
  protected: "rank.dim.protected",
  evidence: "rank.dim.evidence",
};

const ABSENT_KEYS: Record<string, PlanRankCopyKey> = {
  priceUnknown: "rank.priceUnknown",
  walkNotMeasured: "rank.walkNotMeasured",
  protectedNotMarked: "rank.protectedNotMarked",
  noFacts: "rank.noFacts",
};

/** The shell already knows which journey it is on; the panel reads it the way the version list does,
 *  so mounting a new screen never needs another prop threaded through `V8Experience.tsx`. */
function useStoredJourneyId(): string | null {
  const [id, setId] = useState<string | null>(() => localStorage.getItem("citynexus.v8.journey_id"));
  useEffect(() => {
    const onStorage = () => setId(localStorage.getItem("citynexus.v8.journey_id"));
    window.addEventListener("storage", onStorage);
    const timer = window.setInterval(onStorage, 2000);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.clearInterval(timer);
    };
  }, []);
  return id;
}

export function PlanRankingPanel({ budgetMinor, currency }: { budgetMinor: number; currency: string }) {
  const { locale } = useLocale();
  const journeyId = useStoredJourneyId();
  const plans = useAuthoritativePlans(journeyId);
  const say = (key: PlanRankCopyKey, params: Record<string, string | number> = {}) =>
    fillRank(rankCopy(locale, key), params);

  const candidates = ((plans.data ?? []) as unknown as Row[]).filter(
    (plan) => plan.status === "CANDIDATE" && !plan.stale && plan.selectable !== false,
  );
  const selectedPlan = ((plans.data ?? []) as unknown as Row[]).find(
    (plan) => plan.status === "SELECTED" || plan.status === "ACTIVE",
  );
  const { rows, comparable, usedDims } = rankPlans(candidates);

  if (plans.isPending && !plans.isError) {
    return (
      <section className="plan-ranking" data-testid="plan-ranking">
        <p className="v8-muted" data-testid="rank-loading">{say("rank.loading")}</p>
      </section>
    );
  }
  if (plans.isError) {
    return (
      <section className="plan-ranking" data-testid="plan-ranking">
        <p className="v8-message" role="alert" data-testid="rank-failed">{say("rank.failed")}</p>
      </section>
    );
  }
  if (rows.length === 0) {
    return (
      <section className="plan-ranking" data-testid="plan-ranking">
        <p className="v8-muted" data-testid={selectedPlan ? "rank-selected" : "rank-none"}>
          {say(selectedPlan ? "rank.selected" : "rank.none")}
        </p>
      </section>
    );
  }

  const money = (minor: number) => `${currency} ${(minor / 100).toFixed(2)}`;

  return (
    <section className="plan-ranking" data-testid="plan-ranking">
      <header className="v12-panel-header">
        <div>
          <span className="v8-kicker">{say("rank.title")}</span>
          <p className="v8-muted">{say("rank.sub")}</p>
        </div>
      </header>
      <p className="plan-ranking-by" data-testid="rank-ranked-by">
        {usedDims.length > 0
          ? say("rank.rankedBy", { dims: usedDims.map((d) => say(DIM_KEYS[d])).join("\u3001") })
          : say("rank.tie", { dims: RANK_DIMS.map((d) => say(DIM_KEYS[d])).join("\u3001") })}
      </p>
      {rows.length === 1 && <p className="v8-muted" data-testid="rank-only-one">{say("rank.onlyOne")}</p>}
      <table className="plan-ranking-table">
        <thead>
          <tr>
            <th scope="col">{say("rank.version")}</th>
            {RANK_DIMS.map((d) => (
              <th key={d} scope="col" data-testid="rank-dim" data-comparable={comparable[d] ? "true" : "false"}>
                {say(DIM_KEYS[d])}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={row.id} data-testid="rank-row" data-place={index + 1}>
              <th scope="row" data-testid="rank-version">{say("rank.version")} {row.versionNo}</th>
              {RANK_DIMS.map((d) => {
                const cell = row.cells[d];
                let text: string;
                if (cell.kind === "absent") text = say(ABSENT_KEYS[cell.reason]);
                else if (d === "cost") text = money(cell.number);
                else if (d === "evidence") text = `${cell.number}/${row.evidence.total}`;
                else text = `${cell.number}${cell.partial ? ` (${say("rank.walkPartial")})` : ""}`;
                return (
                  <td key={d} data-testid={`rank-cell-${d}`} data-state={cell.kind}>
                    {text}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <ul className="plan-ranking-notes">
        {rows.map((row) => (
          <li key={row.id} data-testid="rank-note">
            {say("rank.evidenceNote", {
              hard: row.evidence.hard,
              total: row.evidence.total,
              quality: row.evidence.worstQuality ?? "\u2014",
            })}
          </li>
        ))}
      </ul>
    </section>
  );
}
