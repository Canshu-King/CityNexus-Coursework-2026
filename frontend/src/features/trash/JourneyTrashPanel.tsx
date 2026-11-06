import { useCallback, useEffect, useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import { CnxMeasuredFacts } from "../../components/CnxMeasuredFacts";
import { classifyReadFailure, copyFor, type Attribution } from "../../lib/failureAttribution";
import type { RawFacts } from "../../lib/rawFacts";
import { formatV8Date } from "../../lib/v8Formatting";
import {
  binMayRestore,
  binOwnership,
  binRecovery,
  readDiscardSequence,
  readScopedDiscardedJourneys,
  refusalReasonCode,
  undeleteJourney,
  RejectionWithFacts,
  type DiscardEvent,
  type ScopedBinRow,
} from "./journeyTrash";
import { fillTrash, trashCopy, type JourneyTrashCopyKey } from "./journeyTrashCopy";
import "./journeyTrash.css";

/**
 * ORD-0402 slice 2 -- the recycle bin: one row per binned trip, one action per row.
 *
 * Mounted through the panel registry (`app/v8PanelRegistry.tsx`, surface "route", key "trash"), so
 * this file and the registry are the whole front-end change and the 4 500-line shell stays shut --
 * which is what ORD-0392 was built to make possible.
 *
 * Three things this screen may not do:
 *  - It may not infer a state from absence. A row is shown as binned only because the server's own
 *    payload says so; if the read answers without the field, the row says that instead of inventing
 *    the fact (`trash.rowStateMissing`).
 *  - It may not offer what the server cannot do. There is no permanent-delete door on this tree, so
 *    no second action appears here (ORD-0385).
 *  - It may not answer a refusal with a code. `JOURNEY_NOT_DELETED` becomes 「这条已经在你的行程列表
 *    里了」; the token itself goes on the details face, where it is quotable and not required reading.
 *
 * The read door this screen needs is ORD-0402 slice 1's and is not on the line yet -- see
 * `journeyTrash.ts` for the measurement of that claim. Until it is, the honest state is the one that
 * renders: the list could not be answered, nothing changed, and the device's own reading is below it.
 */

type Outcome =
  | { kind: "found"; title: string }
  | { kind: "already" | "gone" | "refused"; code: string | null; facts: RawFacts | null }
  | { kind: "unknown"; attribution: Attribution };

type ReadState =
  /* ORD-0531: the door this screen reads now asks about one seat, so without a seat to ask for it
     says so and shows nobody's bin. Rendering the whole-bin door here would leak exactly the rows
     this order exists to isolate. */
  | { phase: "noViewer" }
  | { phase: "loading" }
  | { phase: "ready"; rows: ScopedBinRow[] }
  | { phase: "absent"; facts: RawFacts | null; attribution: Attribution }
  | { phase: "failed"; facts: RawFacts | null; attribution: Attribution };

const OUTCOME_KEY: Record<Outcome["kind"], JourneyTrashCopyKey | null> = {
  found: "trash.found",
  already: "trash.already",
  gone: "trash.gone",
  refused: "trash.refused",
  unknown: null,
};

/** ORD-0535 -- one row's discard log, per journey, in its own slot so one trip's unreadable log does
 *  not blank the rest of the bin. */
type SequenceState =
  | { phase: "loading" }
  | { phase: "ready"; events: DiscardEvent[] }
  | { phase: "failed" };

/** What the row says about *which* discard this is. Counting is the server's order; the only
 *  inference made here is "how many `journey.deleted` entries did this trip's own log hand back". */
function sequenceText(
  state: SequenceState | undefined,
  say: (key: JourneyTrashCopyKey, params?: Record<string, string | number>) => string,
  locale: string,
): string {
  if (!state || state.phase === "loading") return say("trash.sequenceLoading");
  if (state.phase === "failed") return say("trash.sequenceUnreadable");
  const discards = state.events.filter((event) => event.event_type === "journey.deleted");
  if (discards.length === 0) return say("trash.sequenceEmpty");
  if (discards.length === 1) return say("trash.discardSequenceFirst");
  const previous = discards[discards.length - 2];
  const when = previous.deleted_at
    ? { when: formatV8Date(previous.deleted_at, locale) || previous.deleted_at }
    : { when: previous.timestamp ? formatV8Date(previous.timestamp, locale) || previous.timestamp : "" };
  return [
    say("trash.discardSequence", { n: discards.length }),
    previous.deleted_at ? say("trash.previousDiscard", when) : say("trash.previousDiscardRecorded", when),
  ].join(" · ");
}

/** The sentence a refused row owes the traveller: why this one is read-only, in their language. */
const RECOVERY_REASON_KEYS: Record<string, JourneyTrashCopyKey> = {
  RECOVERY_REFUSED_ROLE_CANNOT_RESTORE: "trash.recoveryRefusedRole",
  RECOVERY_REFUSED_NO_SEAT: "trash.recoveryRefusedNoSeat",
  RECOVERY_REFUSED_NOT_OWNER: "trash.recoveryRefusedOther",
};

/** Which copy key explains a row the traveller may not restore; null when the row said nothing. */
function recoveryReasonKey(row: ScopedBinRow): JourneyTrashCopyKey | null {
  const code = binRecovery(row);
  return code === null ? null : RECOVERY_REASON_KEYS[code] ?? null;
}

export function JourneyTrashPanel({ travelerId = null }: { travelerId?: string | null }) {
  const { locale, t } = useLocale();
  const zh = locale.startsWith("zh");
  const say = (key: JourneyTrashCopyKey, params: Record<string, string | number> = {}) =>
    fillTrash(trashCopy(locale, key), params);

  const [read, setRead] = useState<ReadState>(
    travelerId ? { phase: "loading" } : { phase: "noViewer" },
  );
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [sequences, setSequences] = useState<Record<string, SequenceState>>({});

  const load = useCallback(async () => {
    if (!travelerId) {
      /* No seat to ask about: the whole-bin door is deliberately not used as a fallback. */
      setRead({ phase: "noViewer" });
      return;
    }
    setRead({ phase: "loading" });
    try {
      setRead({ phase: "ready", rows: await readScopedDiscardedJourneys(travelerId) });
    } catch (cause) {
      const rejection =
        cause instanceof RejectionWithFacts ? cause : null;
      const attribution = classifyReadFailure(cause);
      /* 404/405 on a collection path is the door not answering, which is a different sentence from
         "the bin is empty" and a different one again from "the network failed". They are separated by
         what this device measured, not by a guess at the cause. */
      setRead({
        phase: rejection?.http_status === 404 || rejection?.http_status === 405 ? "absent" : "failed",
        facts: rejection?.facts ?? null,
        attribution,
      });
    }
  }, [travelerId]);

  useEffect(() => {
    void load();
  }, [load]);

  /* ORD-0535: which discard this is comes from the trip's own log, because the bin row carries only
     the latest moment. Asked once per row and in parallel; each row renders when its own answer
     arrives, so one trip's unreadable log says so on that row alone. The signature deliberately
     includes `deleted_at`: a trip that is dropped again while this screen stays open changes its
     moment, and a key made of ids alone would keep answering "the first time". */
  const sequenceSignature =
    read.phase === "ready"
      ? read.rows.map((row) => `${row.id}@${row.deleted_at ?? ""}`).join("|")
      : "";
  useEffect(() => {
    if (read.phase !== "ready" || !sequenceSignature) return;
    const wanted = sequenceSignature.split("|");
    let live = true;
    setSequences(() => {
      const next: Record<string, SequenceState> = {};
      for (const entry of wanted) {
        const [id] = entry.split("@");
        next[id] = { phase: "loading" };
      }
      return next;
    });
    for (const entry of wanted) {
      const [id] = entry.split("@");
      void readDiscardSequence(id)
        .then((events) => {
          if (live) setSequences((current) => ({ ...current, [id]: { phase: "ready", events } }));
        })
        .catch(() => {
          if (live) setSequences((current) => ({ ...current, [id]: { phase: "failed" } }));
        });
    }
    return () => {
      live = false;
    };
    // `read.phase` is folded in through the signature: the rows only exist once the read is ready.
  }, [sequenceSignature]);

  const bringBack = async (row: ScopedBinRow) => {
    setBusyId(row.id);
    setOutcome(null);
    try {
      const brought = await undeleteJourney(row.id);
      setOutcome({ kind: "found", title: brought.title || row.title });
      await load();
    } catch (cause) {
      const code = refusalReasonCode(cause);
      const facts = cause instanceof RejectionWithFacts ? cause.facts : null;
      if (code === "JOURNEY_NOT_DELETED") setOutcome({ kind: "already", code, facts });
      else if (code === "JOURNEY_NOT_FOUND") setOutcome({ kind: "gone", code, facts });
      else if (cause instanceof RejectionWithFacts) setOutcome({ kind: "refused", code, facts });
      else setOutcome({ kind: "unknown", attribution: classifyReadFailure(cause) });
    } finally {
      setBusyId(null);
    }
  };

  const rows = read.phase === "ready" ? read.rows : [];
  const outcomeKey = outcome ? OUTCOME_KEY[outcome.kind] : null;
  const outcomeText = outcome
    ? outcome.kind === "unknown"
      ? copyFor(outcome.attribution, zh).body
      : outcome.kind === "found"
        ? say("trash.found", { title: outcome.title })
        : outcomeKey
          ? say(outcomeKey)
          : null
    : null;

  return (
    <section
      className="cn-trash"
      data-testid="journey-trash-panel"
      {...(read.phase === "ready" ? { "data-count": rows.length } : {})}
      aria-label={say("trash.title")}
    >
      <header className="cn-trash-head">
        <span className="v8-kicker">{say("trash.title")}</span>
        <p className="v8-muted">{say("trash.sub")}</p>
      </header>

      {read.phase === "noViewer" && (
        <p className="v8-muted" data-testid="trash-no-viewer" role="status">
          {say("trash.noViewer")}
        </p>
      )}

      {read.phase === "loading" && (
        <p className="v8-muted" data-testid="trash-loading">
          {say("trash.loading")}
        </p>
      )}

      {(read.phase === "absent" || read.phase === "failed") && (
        <div className="cn-trash-notice" role="alert" data-testid="trash-read-failed">
          <p>{read.phase === "absent" ? say("trash.doorAbsent") : say("trash.transport")}</p>
          {read.facts ? (
            <details className="cn-trash-details">
              <summary>{t("agent.advancedDetail")}</summary>
              <CnxMeasuredFacts facts={read.facts} zh={zh} testId="trash-read" envelope={null} />
              <span className="cnx-measured-fact" data-testid="trash-read-message-key">
                {read.attribution.message_key}
              </span>
            </details>
          ) : null}
          <button type="button" className="v8-quiet-button" data-testid="trash-refresh" onClick={() => void load()}>
            {say("trash.refresh")}
          </button>
        </div>
      )}

      {read.phase === "ready" && rows.length === 0 && (
        <p className="v8-muted" data-testid="trash-empty">
          {say("trash.empty")}
        </p>
      )}

      {read.phase === "ready" && rows.length > 0 && (
        <>
          {/* The count is computed from the same array the rows are drawn from, so the number on
              screen cannot describe a different read. */}
          <p className="v8-muted" data-testid="trash-count">
            {say("trash.count", { n: rows.length })}
          </p>
          <ul className="cn-trash-list" data-testid="trash-list">
            {rows.map((row) => (
              <li key={row.id} className="cn-trash-row" data-testid="trash-row" data-cnx-journey={row.id}>
                <b className="cn-trash-row-title" data-testid="trash-row-title">
                  {row.title}
                </b>
                <span className="cn-trash-row-state" data-testid="trash-row-state">
                  {row.bin_state ? say("trash.rowState") : say("trash.rowStateMissing")}
                </span>
                <span className="cn-trash-row-when" data-testid="trash-row-deleted-at">
                  {row.deleted_at
                    ? say("trash.deletedAt", {
                        when: formatV8Date(row.deleted_at, locale) || row.deleted_at,
                      })
                    : say("trash.deletedAtMissing")}
                </span>
                {/* Same tier as the moment above it on purpose: this is a fact about the row, not a
                    warning, and it has to be spoken by the screen reader that reads the row. */}
                <span className="cn-trash-row-when cn-trash-row-sequence" data-testid="trash-row-sequence">
                  {sequenceText(sequences[row.id], say, locale)}
                </span>
                {/* ORD-0531: whose trip, and whether this seat may take it back -- both read out of
                    the row, both said in words. A row the server did not clear is rendered without an
                    action rather than with a button that would be refused. */}
                <span className="cn-trash-row-owner" data-testid="trash-row-owner">
                  {binOwnership(row) === "OWNED_BY_THIS_TRAVELER"
                    ? say("trash.rowOwnedByMe")
                    : binOwnership(row) === "OWNED_BY_ANOTHER_TRAVELER"
                      ? say("trash.rowOwnedByOther")
                      : binOwnership(row) === "NOT_STATED"
                        ? say("trash.rowNotStated")
                        : say("trash.rowOwnershipMissing")}
                </span>
                {binMayRestore(row) ? (
                  <button
                    type="button"
                    className="v8-quiet-button cn-trash-action"
                    data-testid="trash-bring-back"
                    disabled={busyId === row.id}
                    onClick={() => void bringBack(row)}
                  >
                    {busyId === row.id ? say("trash.busy") : say("trash.action")}
                  </button>
                ) : (
                  <p className="v8-muted" data-testid="trash-row-readonly-reason">
                    {say(recoveryReasonKey(row) ?? "trash.recoveryMissing")}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </>
      )}

      {/* A list can go stale under a traveller's finger -- arm C of the acceptance run is exactly that
          -- so reading it again has to be something they can do from this screen, not something that
          only appears when the read failed. */}
      {read.phase === "ready" && (
        <button
          type="button"
          className="v8-quiet-button"
          data-testid="trash-refresh"
          onClick={() => {
            /* An explicit re-read retires the last receipt: what the traveller asked for is now the
               list on screen, and leaving 「这条已经在你的行程列表里了」 under an empty bin would put
               two answers on one question. */
            setOutcome(null);
            void load();
          }}
        >
          {say("trash.refresh")}
        </button>
      )}

      {outcomeText && (
        /* A div, not a <p>: the disclosure below is flow content and a paragraph may not hold it. */
        <div className="cn-trash-outcome" role="status" data-testid="trash-outcome" data-cnx-outcome={outcome?.kind}>
          <span data-testid="trash-outcome-text">{outcomeText}</span>
          {outcome && "code" in outcome && outcome.code ? (
            <details className="cn-trash-details">
              <summary>{t("agent.advancedDetail")}</summary>
              {/* The server's own token, on the details face only: the traveller already has the
                  sentence above, and a code is not a diagnosis without its number. Named refusals
                  carry it too -- a screen that explains a condition it cannot quote leaves the next
                  person reading the log with nothing to grep. */}
              <span className="cnx-measured-fact" data-cnx-field="reason_code" data-cnx-value={outcome.code}>
                {outcome.code}
              </span>
              {outcome.facts ? <CnxMeasuredFacts facts={outcome.facts} zh={zh} testId="trash-action" /> : null}
            </details>
          ) : null}
        </div>
      )}
    </section>
  );
}
