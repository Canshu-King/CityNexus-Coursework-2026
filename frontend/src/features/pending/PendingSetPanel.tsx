import { useEffect, useMemo, useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import { categoryLabel } from "../../i18n/travelerLabels";
import { CnxMeasuredFacts } from "../../components/CnxMeasuredFacts";
import { classifyReadFailure, copyFor, type Attribution } from "../../lib/failureAttribution";
import type { RawFacts } from "../../lib/rawFacts";
import { RejectionWithFacts, type Door } from "./pendingSetReads";
import { readCapturedPlaces, readCityPlaces } from "./pendingSetReads";
import {
  composeInputFor,
  daysOfReadback,
  derivePendingSet,
  facetsOf,
  rowsForFilter,
  type PendingCandidate,
  type PendingPlaceRow,
  type PendingRow,
  type PendingWriteBlock,
} from "./pendingSet";
import {
  adoptComposedPlan,
  composeRefusal,
  placeOnADay,
  type ComposeFamily,
  type ComposeRefusal,
} from "./pendingPlaceWrites";
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
 *    (`backend/app/main.py`, `:7226`, `:7231` only move an activity through execution), so the
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

function failed(cause: unknown): DoorState<never> {
  const rejection = cause instanceof RejectionWithFacts ? cause : null;
  return {
    phase: "failed",
    facts: rejection?.facts ?? null,
    attribution: classifyReadFailure(cause),
  };
}

const BLOCKED_KEY: Record<Exclude<PendingWriteBlock, null>, PendingSetCopyKey> = {
  NOTHING_PICKED: "pending.blocked.NOTHING_PICKED",
  PICK_HAS_NO_PLACE_ROW: "pending.blocked.PICK_HAS_NO_PLACE_ROW",
  SET_TOO_SMALL: "pending.blocked.SET_TOO_SMALL",
  SET_TOO_LARGE: "pending.blocked.SET_TOO_LARGE",
};

const REFUSE_KEY: Record<ComposeFamily, PendingSetCopyKey> = {
  WINDOW: "pending.refuse.WINDOW",
  COMMITMENT: "pending.refuse.COMMITMENT",
  NO_PLAN: "pending.refuse.NO_PLAN",
  OTHER: "pending.refuse.other",
};

export function PendingSetPanel({
  data,
  onNavigate,
  onReload,
}: {
  data: Parameters<typeof derivePendingSet>[0]["data"];
  onNavigate?: (route: string) => void;
  /** The shell's own reload of the trip payload.  Adoption changes which plan is current, and this
   *  screen's set is defined against that plan, so the reload is part of the write -- not a courtesy. */
  onReload?: () => void;
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

  /* ORD-0450 -- the batch, and the two writes that already exist for it. */
  const [pickedKeys, setPickedKeys] = useState<string[]>([]);
  const [writeBusy, setWriteBusy] = useState(false);
  const [writeRefusal, setWriteRefusal] = useState<ComposeRefusal | null>(null);
  const [writeFacts, setWriteFacts] = useState<RawFacts | null>(null);
  const [receipt, setReceipt] = useState<{
    plan_id: string;
    version_no: number | null;
    status: string | null;
    stop_count: number;
    landed_count: number;
    days: string;
    missing_names: string[];
  } | null>(null);
  const [adoptState, setAdoptState] = useState<"idle" | "busy" | "done" | "refused">("idle");

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

  const pickedRows = derived.rows.filter((row) => pickedKeys.includes(row.key));
  const writeInput = composeInputFor({ data, picked: pickedRows });

  const clearWriteOutcome = () => {
    setReceipt(null);
    setWriteRefusal(null);
    setWriteFacts(null);
    setAdoptState("idle");
  };

  const togglePick = (row: PendingRow) => {
    setPickedKeys((current) =>
      current.includes(row.key) ? current.filter((key) => key !== row.key) : [...current, row.key],
    );
    /* The receipt describes the batch that produced it; once the ticks change it is a statement
       about a set nobody asked for.  Same rule ORD-0435 set for its own batch bar. */
    clearWriteOutcome();
  };

  const runPlace = async () => {
    if (!journeyId || !writeInput.ok || writeBusy) return;
    setWriteBusy(true);
    clearWriteOutcome();
    try {
      const placed = await placeOnADay(journeyId, writeInput.place_ids);
      const outcome = daysOfReadback(
        placed.readback.activities ?? [],
        writeInput.picked_place_ids,
        derived.timezone,
      );
      const nameByPlaceId = new Map(
        pickedRows.map((row) => [row.placeId ?? "", row.name] as const),
      );
      setReceipt({
        plan_id: placed.composed.plan_id,
        version_no: placed.composed.version_no,
        status: placed.composed.status,
        stop_count: placed.composed.stop_count,
        landed_count: outcome.landed.length,
        days: outcome.days.length
          ? outcome.days.join(zh ? "\u3001" : ", ")
          : say("pending.receiptNoDay"),
        missing_names: outcome.missing.map((id) => nameByPlaceId.get(id) ?? id),
      });
    } catch (cause) {
      setWriteRefusal(composeRefusal(cause));
      setWriteFacts(cause instanceof RejectionWithFacts ? cause.facts : null);
    } finally {
      setWriteBusy(false);
    }
  };

  const runAdopt = async () => {
    if (!journeyId || !receipt || adoptState !== "idle") return;
    setAdoptState("busy");
    try {
      await adoptComposedPlan(journeyId, receipt.plan_id);
      setAdoptState("done");
      /* Re-read both this screen's doors and let the shell re-read the trip: until the plan this
         screen measures itself against is the one the server now names, the list would be a
         description of the past. */
      onReload?.();
      setAttempt((n) => n + 1);
      setPickedKeys([]);
    } catch (cause) {
      setAdoptState("refused");
      setWriteFacts(cause instanceof RejectionWithFacts ? cause.facts : null);
      setWriteRefusal(null);
    }
  };

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
                <button
                  type="button"
                  className={
                    pickedKeys.includes(row.key)
                      ? "cn-pending-tick cn-pending-tick--on"
                      : "cn-pending-tick"
                  }
                  role="checkbox"
                  aria-checked={pickedKeys.includes(row.key)}
                  aria-label={`${row.name} · ${
                    pickedKeys.includes(row.key) ? say("pending.tickOff") : say("pending.tickOn")
                  }`}
                  data-testid="pending-tick"
                  data-contrast-role="control"
                  disabled={writeBusy || adoptState === "busy"}
                  onClick={() => togglePick(row)}
                >
                  <span className="cn-pending-tick-box" aria-hidden="true" />
                  <span className="cn-pending-row-name" data-testid="pending-row-name">
                    {row.name}
                  </span>
                </button>
                <p className="cn-pending-row-cause" data-testid="pending-row-cause">
                  {say("pending.noDay")}
                  {row.capturedDay ? ` · ${say("pending.capturedOn", { day: row.capturedDay })}` : ""}
                </p>
                <p className="cn-pending-row-meta" data-testid="pending-row-category">
                  {row.categoryToken === null
                    ? say("pending.rowCategoryMissing")
                    : categoryLabel(row.categoryToken, zh)}
                </p>
              </li>
            ))}
          </ul>
          <div className="cn-pending-batch">
            <p className="cn-pending-batch-count" data-testid="pending-selected">
              {say("pending.selected", { n: pickedRows.length })}
            </p>
            <button
              type="button"
              className="v8-primary-button"
              data-testid="pending-place-action"
              data-contrast-role="control"
              disabled={!writeInput.ok || writeBusy || adoptState === "busy"}
              onClick={() => void runPlace()}
            >
              {writeBusy ? say("pending.busy") : say("pending.action")}
            </button>
            {writeInput.ok ? (
              <p className="cn-pending-batch-line" data-testid="pending-batch-line">
                {say("pending.batchLine", {
                  planned: writeInput.planned_place_ids.length,
                  picked: writeInput.picked_place_ids.length,
                })}
              </p>
            ) : (
              /* A disabled action has to say why, in the same breath: "grey until you do something"
                 is the shape that leaves a traveller guessing which thing is missing. */
              <p className="cn-pending-batch-blocked" role="status" data-testid="pending-blocked">
                {say(
                  BLOCKED_KEY[writeInput.blocked_by ?? "NOTHING_PICKED"],
                  { max: writeInput.max_places },
                )}
              </p>
            )}
          </div>

          {writeRefusal && (
            <div className="cn-pending-notice" role="alert" data-testid="pending-write-refused">
              <p>{say(REFUSE_KEY[writeRefusal.family])}</p>
              {writeFacts && (
                <details className="cn-pending-details" data-testid="pending-write-facts-open">
                  <summary>{say("pending.details")}</summary>
                  {/* The condition gets a sentence of its own above; the code the server answered with
                      belongs down here, where it is quotable and not required reading. */}
                  <p className="cn-pending-token">
                    reason_code={writeRefusal.rejection_code ?? say("pending.noToken")}
                  </p>
                  <CnxMeasuredFacts facts={writeFacts} zh={zh} testId="pending-write-facts" />
                </details>
              )}
            </div>
          )}

          {receipt && (
            <div className="cn-pending-receipt" data-testid="pending-receipt">
              <p data-testid="pending-receipt-line">
                {say("pending.receipt", {
                  n: receipt.landed_count,
                  days: receipt.days,
                  version: receipt.version_no ?? "\u2014",
                  stops: receipt.stop_count,
                })}
              </p>
              {receipt.missing_names.length > 0 && (
                <p data-testid="pending-receipt-missing">
                  {say("pending.missing", {
                    names: receipt.missing_names.join(zh ? "\u3001" : ", "),
                  })}
                </p>
              )}
              {adoptState === "done" ? (
                <>
                  <p data-testid="pending-adopted">
                    {say("pending.adopted", { version: receipt.version_no ?? "\u2014" })}
                  </p>
                  {onNavigate && (
                    <button
                      type="button"
                      className="v8-quiet-button"
                      data-testid="pending-view-days"
                      data-contrast-role="control"
                      onClick={() => onNavigate("trips")}
                    >
                      {say("pending.viewDays")}
                    </button>
                  )}
                </>
              ) : (
                <>
                  <p data-testid="pending-not-selected">{say("pending.notSelected")}</p>
                  <p className="cn-pending-batch-line" data-testid="pending-adopt-warn">
                    {say("pending.adoptWarn")}
                  </p>
                  <button
                    type="button"
                    className="v8-quiet-button"
                    data-testid="pending-adopt"
                    data-contrast-role="control"
                    disabled={adoptState === "busy"}
                    onClick={() => void runAdopt()}
                  >
                    {adoptState === "busy" ? say("pending.busy") : say("pending.adopt")}
                  </button>
                  {adoptState === "refused" && (
                    <p role="alert" data-testid="pending-adopt-refused">
                      {say("pending.adoptRefused")}
                    </p>
                  )}
                </>
              )}
            </div>
          )}

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
            {row.providerKey ?? "no_provider_key"} · {row.placeId ?? "no_place_row_yet"} ·{" "}
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
