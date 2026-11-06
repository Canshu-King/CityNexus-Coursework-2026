import { useCallback, useEffect, useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import { CnxMeasuredFacts } from "../../components/CnxMeasuredFacts";
import { classifyReadFailure, copyFor, type Attribution } from "../../lib/failureAttribution";
import type { RawFacts } from "../../lib/rawFacts";
import { formatV8Date } from "../../lib/v8Formatting";
import {
  readDiscardedJourneys,
  refusalReasonCode,
  undeleteJourney,
  RejectionWithFacts,
  type DiscardedJourney,
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
  | { phase: "loading" }
  | { phase: "ready"; rows: DiscardedJourney[] }
  | { phase: "absent"; facts: RawFacts | null; attribution: Attribution }
  | { phase: "failed"; facts: RawFacts | null; attribution: Attribution };

const OUTCOME_KEY: Record<Outcome["kind"], JourneyTrashCopyKey | null> = {
  found: "trash.found",
  already: "trash.already",
  gone: "trash.gone",
  refused: "trash.refused",
  unknown: null,
};

export function JourneyTrashPanel() {
  const { locale, t } = useLocale();
  const zh = locale.startsWith("zh");
  const say = (key: JourneyTrashCopyKey, params: Record<string, string | number> = {}) =>
    fillTrash(trashCopy(locale, key), params);

  const [read, setRead] = useState<ReadState>({ phase: "loading" });
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setRead({ phase: "loading" });
    try {
      setRead({ phase: "ready", rows: await readDiscardedJourneys() });
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
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const bringBack = async (row: DiscardedJourney) => {
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
                <button
                  type="button"
                  className="v8-quiet-button cn-trash-action"
                  data-testid="trash-bring-back"
                  disabled={busyId === row.id}
                  onClick={() => void bringBack(row)}
                >
                  {busyId === row.id ? say("trash.busy") : say("trash.action")}
                </button>
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
