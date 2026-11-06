import { useLocale } from "../../i18n/LocaleContext";
import { formatV8Date } from "../../lib/v8Formatting";
import { useAuthoritativePlans } from "../../api/v12Authoritative";
import type { PlanVersion } from "../../api/v12Authoritative";
import { fillHistory, historyCopy, type PlanVersionCopyKey } from "./planVersionHistoryCopy";

/**
 * ORD-0386 step 1: show the traveller which plan versions this trip already has.
 *
 * Read-only by construction -- there is no mutation, no idempotency key and no revert control
 * here, and the reason for the last one is said out loud rather than left as an absence.
 *
 * The panel owns its read so the shell cannot gate it on having got a plan: mounting it behind
 * `plan &&` made a failed list read look like "you have not chosen a plan yet", which is the same
 * class of defect this order's copy rules exist to prevent. React Query shares the cache entry the
 * shell already fills (`["v12-authoritative-plans", journeyId]`), so this costs no second request.
 *
 * `created_at` and `is_current` are typed locally rather than taken from `generated/api.ts`,
 * because that committed file is already 552 lines away (15 hunks) from what openapi-typescript
 * 7.13.0 produces from today's contract: it has no `/demo/prepare-scenario`, and its `PlanVersion`
 * carries neither of these two fields even though the contract declares both -- `created_at` from
 * this order, `is_current` since ORD-0097 (contracts/openapi.yaml). Regenerating it here would
 * fold other orders' drift into this one's diff, so the fields are declared where they are used and
 * the contract stays the source of truth. Measurement: `review/ord0386-plan-version-history/api_ts_drift.txt`.
 */
type VersionRow = PlanVersion & {
  created_at?: string | null;
  /** The server's own answer to "which row does the journey run" -- never derived from order,
   *  status or version_no (ORD-0097). */
  is_current?: boolean;
};

const SOURCE_KEYS: Record<string, PlanVersionCopyKey> = {
  INITIAL: "history.sourceInitial",
  REPLAN: "history.sourceReplan",
  MANUAL: "history.sourceManual",
};

export function PlanVersionHistoryPanel({ journeyId }: { journeyId: string }) {
  const { locale } = useLocale();
  const versions = useAuthoritativePlans(journeyId);
  const plans: VersionRow[] = versions.data ?? [];
  const loading = versions.isPending && !versions.isError;
  const failed = versions.isError;
  const say = (key: PlanVersionCopyKey, params: Record<string, string | number> = {}) =>
    fillHistory(historyCopy(locale, key), params);
  const zh = locale.startsWith("zh");
  const byId = new Map(plans.map((item) => [item.id, item]));
  const ordered = [...plans].sort((a, b) => a.version_no - b.version_no);

  return (
    <section className="v12-trip-studio plan-version-history" data-testid="plan-version-history">
      <header className="v12-panel-header">
        <div>
          <span className="v8-kicker">{say("history.title")}</span>
          <p className="v8-muted">{say("history.sub")}</p>
        </div>
      </header>

      {loading && (
        <p className="v8-muted" data-testid="history-loading">
          {say("history.loading")}
        </p>
      )}
      {!loading && failed && (
        <p className="v8-message" role="alert" data-testid="history-failed">
          {say("history.failed")}
        </p>
      )}
      {!loading && !failed && ordered.length === 0 && (
        <p className="v8-muted" data-testid="history-empty">
          {say("history.empty")}
        </p>
      )}

      {ordered.length > 0 && (
        <>
          {/* The count and the retention sentence are computed from the same array the rows are
              drawn from, so the number on screen cannot describe a different read. */}
          <p className="v8-muted" data-testid="history-count">
            {say("history.count", { n: ordered.length })}
          </p>
          <p className="plan-version-history-retention" data-testid="history-retention">
            {say("history.retention", { n: ordered.length })}
          </p>
          <ol className="plan-version-history-list" data-testid="history-list">
            {ordered.map((item) => {
              const parent = item.parent_plan_version_id ? byId.get(item.parent_plan_version_id) : null;
              return (
                <li key={item.id} data-testid="history-row" data-current={item.is_current ? "true" : "false"}>
                  <b data-testid="history-version">
                    {say("history.version")} {item.version_no}
                  </b>
                  <span data-testid="history-source">
                    {SOURCE_KEYS[item.source]
                      ? say(SOURCE_KEYS[item.source])
                      : say("history.sourceUnknown", { code: item.source })}
                  </span>
                  <span data-testid="history-created">
                    {item.created_at ? formatV8Date(item.created_at, locale) : say("history.createdMissing")}
                  </span>
                  <span data-testid="history-parent">
                    {parent
                      ? `${say("history.parent")} ${parent.version_no}`
                      : item.parent_plan_version_id
                        ? `${say("history.parent")} —`
                        : say("history.noParent")}
                  </span>
                  {item.is_current && (
                    <em data-testid="history-current">{say("history.current")}</em>
                  )}
                </li>
              );
            })}
          </ol>
          <p className="v8-muted plan-version-history-norestore" data-testid="history-no-restore">
            {say("history.noRestore")}
          </p>
        </>
      )}
      <span className="v8-sr-only" data-testid="history-locale">{zh ? "zh" : "en"}</span>
    </section>
  );
}
