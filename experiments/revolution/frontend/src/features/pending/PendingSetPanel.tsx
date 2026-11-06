import { useEffect, useMemo, useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import { categoryLabel } from "../../i18n/travelerLabels";
import { CnxMeasuredFacts } from "../../components/CnxMeasuredFacts";
import { classifyReadFailure, copyFor, type Attribution } from "../../lib/failureAttribution";
import type { RawFacts } from "../../lib/rawFacts";
import { RejectionWithFacts, type Door } from "./pendingSetReads";
import { readCapturedPlaces, readCityPlaces } from "./pendingSetReads";
import {
  derivePendingSet,
  facetsOf,
  rowsForFilter,
  type PendingCandidate,
  type PendingPlaceRow,
  type PendingRow,
} from "./pendingSet";
import { fillPending, pendingCopy, type PendingSetCopyKey } from "./pendingSetCopy";
import "./pendingSet.css";

/**
 * ORD-0445 -- 待规划 as its own data state: a screen, a count, and a filter sheet of its own.
 *
 * Mounted through the panel registry (`app/v8PanelRegistry.tsx`, surface `route`, id `pending-set`),
 * so this directory plus one registry entry is the whole front-end change and the 4.6k-line shell
 * stays shut -- the reason ORD-0392 exists.
 *
 * Four things this screen may not do, each with the line that keeps it honest:
 *  - It may not answer an empty set with 「暂无数据」.  An empty set is a fact about this trip, so the
 *    empty state names the trip, names the step that can change it (the discovery door is real), and
 *    re-reads on request.
 *  - It may not filter by a category the server did not name.  Facets are built from the rows
 *    (`pendingSet.ts:facetsOf`), and a row whose category no read gave says so and stays under 全部.
 *  - It may not promise to put a place on a day.  This server has no such write
 *    (`backend/app/main.py:6798`, `:6803`, `:6808` only move an activity through execution), so the
 *    screen states the missing door instead of mounting a button for it (ORD-0385).
 *  - It may not count an unread door as zero places.  「这台服务器没答出那份清单」 is its own sentence,
 *    apart from 「没有待规划的地点」 and apart from 「没读到」 (`pendingSetReads.ts`).
 *
 * The first number on the count line is always the number of rows this screen renders, so
 * "计数与列表行数一致" is something a traveller can read, not something a gate has to trust.
 */

type DoorState<T> =
  | { phase: "loading" }
  | { phase: "ready"; door: Door<T> }
  | { phase: "failed"; facts: RawFacts | null; attribution: Attribution };

const LOADING: DoorState<never> = { phase: "loading" };

function openingLabel(row: PendingRow, zh: boolean): string {
  const status = row.openingStatus?.toUpperCase();
  if (!status || status === "UNKNOWN") return zh ? "营业时间未知" : "Opening hours unknown";
  return row.openingRaw || status;
}

function costLabel(row: PendingRow, zh: boolean): string {
  const status = row.costStatus?.toUpperCase();
  if (status === "KNOWN_FREE" || (status === "KNOWN" && row.costAmountMinor === 0)) {
    return zh ? "免费" : "Free";
  }
  const paid =
    (status === "KNOWN_PAID" || status === "KNOWN" || status === "VERIFIED_PROVIDER") &&
    row.costAmountMinor != null;
  if (paid) {
    return `${row.costCurrency ?? ""} ${Math.round(row.costAmountMinor / 100)}`.trim();
  }
  return zh ? "暂无价格（未知）" : "Price unavailable (unknown)";
}

function failed(cause: unknown): DoorState<never> {
  const rejection = cause instanceof RejectionWithFacts ? cause : null;
  return {
    phase: "failed",
    facts: rejection?.facts ?? null,
    attribution: classifyReadFailure(cause),
  };
}

export function PendingSetPanel({
  data,
  onNavigate,
}: {
  data: Parameters<typeof derivePendingSet>[0]["data"];
  onNavigate?: (route: string) => void;
}) {
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const say = (key: PendingSetCopyKey, params: Record<string, string | number> = {}) =>
    fillPending(pendingCopy(locale, key), params);

  const journeyId = data?.journey?.id ?? null;
  const cityCode = data?.journey?.city_code ?? null;

  const [candidateDoor, setCandidateDoor] = useState<DoorState<PendingCandidate>>(LOADING);
  const [placeDoor, setPlaceDoor] = useState<DoorState<PendingPlaceRow>>(LOADING);
  const [attempt, setAttempt] = useState(0);

  /* The two doors this screen owns. 「再读一次」 re-issues exactly these two GETs; the plan itself is
     the shell's read and this panel never claims to refresh it. */
  useEffect(() => {
    let alive = true;
    if (!journeyId) {
      /* Defensive only: the registry mounts this panel on a screen that already holds a journey
         (`v8PanelRegistry.tsx`, `when`), so a missing id means this device has nothing to name. */
      setCandidateDoor({ phase: "ready", door: { kind: "no_list", keys: ["no_journey_on_device"] } });
      setPlaceDoor({ phase: "ready", door: { kind: "no_list", keys: ["no_journey_on_device"] } });
      return;
    }
    setCandidateDoor(LOADING);
    setPlaceDoor(LOADING);
    void readCapturedPlaces(journeyId)
      .then((door) => alive && setCandidateDoor({ phase: "ready", door }))
      .catch((cause) => alive && setCandidateDoor(failed(cause)));
    if (!cityCode) {
      setPlaceDoor({ phase: "ready", door: { kind: "no_list", keys: ["no_city_code_on_journey"] } });
    } else {
      void readCityPlaces(cityCode)
        .then((door) => alive && setPlaceDoor({ phase: "ready", door }))
        .catch((cause) => alive && setPlaceDoor(failed(cause)));
    }
    return () => {
      alive = false;
    };
  }, [journeyId, cityCode, attempt]);

  const candidates =
    candidateDoor.phase === "ready" && candidateDoor.door.kind === "list"
      ? candidateDoor.door.rows
      : null;
  const places =
    placeDoor.phase === "ready" && placeDoor.door.kind === "list" ? placeDoor.door.rows : null;

  const derived = useMemo(
    () => derivePendingSet({ data, candidates, places }),
    [data, candidates, places],
  );
  const facets = useMemo(() => facetsOf(derived.rows), [derived.rows]);

  const [choice, setChoice] = useState("all");
  const [draft, setDraft] = useState("all");
  const [sheetOpen, setSheetOpen] = useState(false);

  /* A facet can vanish when a read comes back; the committed choice then falls back to 全部 rather
     than holding a filter that names nothing. */
  useEffect(() => {
    if (choice !== "all" && !facets.some((facet) => facet.token === choice)) setChoice("all");
  }, [facets, choice]);

  const shown = rowsForFilter(derived.rows, choice);
  const stillLoading = candidateDoor.phase === "loading" || placeDoor.phase === "loading";
  const candidateListAbsent =
    candidateDoor.phase === "ready" && candidateDoor.door.kind === "no_list";
  const placeListAbsent = placeDoor.phase === "ready" && placeDoor.door.kind === "no_list";
  /* Which of the two doors failed is a fact this screen keeps, because the two answers are different
     sentences: the mesh door is this trip's captured-place list, the places door only names
     categories. */
  const failedDoor =
    candidateDoor.phase === "failed" ? candidateDoor : placeDoor.phase === "failed" ? placeDoor : null;
  /* The set itself is only knowable when the captured-place door answered with a list.  Until it
     does, this screen has no number to show -- and printing a 0 there would be exactly the ORD-0263
     mistake: an unread door dressed up as an empty shelf. */
  const setKnown = candidateDoor.phase === "ready" && candidateDoor.door.kind === "list";

  return (
    <section className="cn-pending" data-testid="pending-set" aria-label={say("pending.title")}>
      <header className="cn-pending-head">
        <span className="v8-kicker">{say("pending.title")}</span>
        <p className="v8-muted">{say("pending.sub")}</p>
      </header>

      {stillLoading && (
        <p className="v8-muted" data-testid="pending-loading">
          {say("pending.loading")}
        </p>
      )}

      {!derived.hasActivePlan && !stillLoading && (
        <p className="cn-pending-note" data-testid="pending-no-plan">
          {say("pending.noPlan")}
        </p>
      )}

      {candidateListAbsent && !stillLoading && (
        <p className="cn-pending-note" role="status" data-testid="pending-candidates-absent">
          {say("pending.candidatesAbsent")}
        </p>
      )}
      {placeListAbsent && !stillLoading && (
        <p className="cn-pending-note" role="status" data-testid="pending-places-absent">
          {say("pending.placesAbsent")}
        </p>
      )}

      {failedDoor && (
        <div className="cn-pending-notice" role="alert" data-testid="pending-read-failed">
          <p>{copyFor(failedDoor.attribution, zh).body}</p>
        </div>
      )}

      {!setKnown && !stillLoading && (
        <div className="cn-pending-unknown" data-testid="pending-set-unknown">
          <p data-testid="pending-set-unknown-line">{say("pending.setUnknown")}</p>
          <button
            type="button"
            className="v8-quiet-button"
            data-testid="pending-reread"
            data-contrast-role="control"
            onClick={() => setAttempt((n) => n + 1)}
          >
            {say("pending.reread")}
          </button>
        </div>
      )}

      {setKnown && (
        <>
      <div className="cn-pending-bar">
        <p className="cn-pending-count" data-testid="pending-count">
          {choice === "all"
            ? say("pending.count", { n: shown.length })
            : say("pending.shown", { m: shown.length, n: derived.rows.length })}
        </p>
        <button
          type="button"
          className="v8-quiet-button cn-pending-filter-open"
          data-testid="pending-filter-open"
          data-contrast-role="control"
          aria-expanded={sheetOpen}
          onClick={() => {
            setDraft(choice);
            setSheetOpen(true);
          }}
        >
          {say("pending.filter")}
        </button>
      </div>

      {sheetOpen && (
        <div
          className="cn-pending-sheet"
          data-testid="pending-filter-sheet"
          role="group"
          aria-label={say("pending.filter.sheetTitle")}
        >
          <ul className="cn-pending-sheet-list">
            <li>
              <button
                type="button"
                className={draft === "all" ? "cn-pending-chip cn-pending-chip--on" : "cn-pending-chip"}
                data-testid="pending-filter-row"
                data-choice="all"
                aria-pressed={draft === "all"}
                {...(draft === "all" ? { "data-contrast-role": "state" } : {})}
                onClick={() => setDraft("all")}
              >
                <span className="cn-pending-chip-name">{say("pending.filter.all")}</span>
                <span className="cn-pending-chip-sub">{say("pending.filter.allSub")}</span>
                <span className="cn-pending-chip-count">{derived.rows.length}</span>
              </button>
            </li>
            {facets.map((facet) => (
              <li key={facet.token}>
                <button
                  type="button"
                  className={
                    draft === facet.token ? "cn-pending-chip cn-pending-chip--on" : "cn-pending-chip"
                  }
                  data-testid="pending-filter-row"
                  data-choice={facet.token}
                  aria-pressed={draft === facet.token}
                  {...(draft === facet.token ? { "data-contrast-role": "state" } : {})}
                  onClick={() => setDraft(facet.token)}
                >
                  <span className="cn-pending-chip-name">{categoryLabel(facet.token, zh)}</span>
                  <span className="cn-pending-chip-count">{facet.count}</span>
                </button>
              </li>
            ))}
          </ul>
          <div className="cn-pending-sheet-actions">
            <button
              type="button"
              className="v8-quiet-button"
              data-testid="pending-filter-reset"
              data-contrast-role="control"
              onClick={() => setDraft("all")}
            >
              {say("pending.filter.reset")}
            </button>
            <button
              type="button"
              className="v8-primary-button"
              data-testid="pending-filter-done"
              data-contrast-role="control"
              onClick={() => {
                setChoice(draft);
                setSheetOpen(false);
              }}
            >
              {say("pending.filter.done")}
            </button>
          </div>
        </div>
      )}

      {shown.length > 0 && (
        <>
          <ul className="cn-pending-list">
            {shown.map((row) => (
              <li key={row.key} className="cn-pending-row" data-testid="pending-row">
                <p className="cn-pending-row-name" data-testid="pending-row-name">
                  {row.name}
                </p>
                <p className="cn-pending-row-cause" data-testid="pending-row-cause">
                  {say("pending.noDay")}
                  {row.capturedDay ? ` · ${say("pending.capturedOn", { day: row.capturedDay })}` : ""}
                </p>
                <p className="cn-pending-row-meta" data-testid="pending-row-category">
                  {row.categoryToken === null
                    ? say("pending.rowCategoryMissing")
                    : categoryLabel(row.categoryToken, zh)}
                </p>
                <p className="cn-pending-row-facts" data-testid="pending-row-facts">
                  {zh ? "营业" : "Opening"}: {openingLabel(row, zh)} · {zh ? "费用" : "Cost"}: {costLabel(row, zh)}
                </p>
              </li>
            ))}
          </ul>
          <p className="cn-pending-rule" data-testid="pending-delete-day-rule">
            {say("pending.deleteDayRule")}
          </p>
          <p className="cn-pending-rule" data-testid="pending-no-place-door">
            {say("pending.noPlaceDoor")}
          </p>
        </>
      )}

      {shown.length === 0 && !stillLoading && (
        <div className="cn-pending-empty" data-testid="pending-empty">
          <p className="cn-pending-empty-title" data-testid="pending-empty-title">
            {derived.rows.length === 0
              ? say("pending.empty")
              : say("pending.filterEmpty", { n: derived.rows.length })}
          </p>
          {derived.rows.length === 0 && (
            <p className="cn-pending-empty-next">{say("pending.empty.next")}</p>
          )}
          <div className="cn-pending-empty-actions">
            {derived.rows.length > 0 && (
              <button
                type="button"
                className="v8-quiet-button"
                data-testid="pending-empty-reset"
                data-contrast-role="control"
                onClick={() => setChoice("all")}
              >
                {say("pending.filter.reset")}
              </button>
            )}
            {derived.rows.length === 0 && onNavigate && (
              <button
                type="button"
                className="v8-primary-button"
                data-testid="pending-empty-action"
                data-contrast-role="control"
                onClick={() => onNavigate("discover")}
              >
                {say("pending.empty.action")}
              </button>
            )}
            <button
              type="button"
              className="v8-quiet-button"
              data-testid="pending-reread"
              data-contrast-role="control"
              onClick={() => setAttempt((n) => n + 1)}
            >
              {say("pending.reread")}
            </button>
          </div>
        </div>
      )}
        </>
      )}

      <details className="cn-pending-details" data-testid="pending-details">
        <summary>{say("pending.details")}</summary>
        <p className="cn-pending-reads" data-testid="pending-reads">
          {say("pending.activities")} {derived.counts.activities_read} ·{" "}
          {say("pending.candidates")} {derived.counts.candidates_read} ·{" "}
          {say("pending.placesRows")}{" "}
          {derived.counts.places_read === null ? say("pending.notAnswered") : derived.counts.places_read} ·{" "}
          {say("pending.placed")} {derived.counts.placed_place_ids} ·{" "}
          {say("pending.candidatesPlaced")} {derived.counts.candidates_placed}
        </p>
        {derived.rows.map((row) => (
          <p key={row.key} className="cn-pending-token">
            {row.key} · {row.stateToken ?? say("pending.stateMissing")} ·{" "}
            {row.providerKey ?? "no_provider_key"} · {row.externalId ?? "no_external_id"} ·{" "}
            {row.placeId ?? "no_place_row_yet"} ·{" "}
            {row.categoryToken ?? "no_category_token"}
          </p>
        ))}
        {candidateListAbsent && (
          <p className="cn-pending-token">
            provider-mesh answered keys: {candidateDoorKeys(candidateDoor).join(", ") || "none"}
          </p>
        )}
        {failedDoor && failedDoor.facts && (
          <p className="cn-pending-facts">
            <CnxMeasuredFacts
              facts={failedDoor.facts}
              zh={zh}
              testId={
                failedDoor === candidateDoor ? "pending-mesh-facts" : "pending-places-facts"
              }
              envelope={undefined}
            />
          </p>
        )}
      </details>
    </section>
  );
}

/** A read that never answered has no measurement to show; the details face says which keys the
 *  answer did carry instead of inventing a zero. */
function candidateDoorKeys(
  state: DoorState<PendingCandidate>,
): string[] {
  return state.phase === "ready" && state.door.kind === "no_list" ? state.door.keys : [];
}
