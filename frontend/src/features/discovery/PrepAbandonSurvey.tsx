import "./prepAbandonSurvey.css";
import { useEffect, useState } from "react";
import {
  NOTE_MAX_CHARS,
  STOP_ACTION_KEY,
  SURVEY_COPY,
  SURVEY_REASONS,
  type SurveyKey,
} from "./prepAbandonSurveyCopy";

type Props = {
  zh: boolean;
  operationId: string;
  busy: boolean;
  /** The traveller went through with the stop. The panel still owns the write. */
  onExit: (reasons: string[], note: string) => void;
  onBack: () => void;
};

const ZH_COPY = SURVEY_COPY.zh;
const copy = (zh: boolean, key: SurveyKey): string =>
  (zh ? ZH_COPY : SURVEY_COPY.en)[key];

/**
 * ORD-0440 - the sheet that sits in front of stopping a preparation.
 *
 * Two things it deliberately does not do: it never renames the act (the exit carries the panel's own
 * `prepSurvey.stopAction` string, so "button does not change its mouth" is structural rather than a
 * claim), and it never blocks the act - zero reasons and an empty note are answers the job still
 * stops on. An optional survey that gatekeeps a destructive action would be a second, sneakier
 * confirmation dialog, which is not what was borrowed.
 */
export function PrepAbandonSurvey({ zh, operationId, busy, onExit, onBack }: Props) {
  const [picked, setPicked] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const over = note.length > NOTE_MAX_CHARS;

  /* The sheet replaces content the traveller was already scrolled past, so on arrival the page can
     be left with its top above the viewport - the English arm measured box top = -272 at 390x844,
     which means a finger would see the bottom of a survey it cannot read from the start. */
  useEffect(() => {
    window.scrollTo(0, 0);
  }, []);

  const toggle = (code: string) =>
    setPicked((current) =>
      current.includes(code) ? current.filter((item) => item !== code) : [...current, code],
    );

  return (
    <div className="cn-prep-survey" data-testid="prep-survey" data-operation-id={operationId}>
      <p className="cn-prep-survey-title" data-testid="prep-survey-title">
        {copy(zh, "prepSurvey.title")}
      </p>
      <p className="cn-prep-survey-sub" data-testid="prep-survey-sub">
        {copy(zh, "prepSurvey.sub")}
      </p>
      <ul className="cn-prep-survey-reasons">
        {SURVEY_REASONS.map((reason) => (
          <li key={reason.code}>
            <label>
              <input
                type="checkbox"
                data-testid={`prep-survey-reason-${reason.code}`}
                checked={picked.includes(reason.code)}
                onChange={() => toggle(reason.code)}
              />
              <span data-testid={`prep-survey-reason-label-${reason.code}`}>
                {copy(zh, reason.key)}
              </span>
            </label>
          </li>
        ))}
      </ul>
      <label className="cn-prep-survey-note-label" data-testid="prep-survey-note-label">
        {copy(zh, "prepSurvey.note")}
      </label>
      <textarea
        className="cn-prep-survey-note"
        data-testid="prep-survey-note"
        rows={1}
        value={note}
        onChange={(event) => setNote(event.target.value)}
      />
      <p className="cn-prep-survey-count" data-testid="prep-survey-count">
        {`${note.length} / ${NOTE_MAX_CHARS}`} · {copy(zh, "prepSurvey.max")}
      </p>
      {over && (
        <p className="cn-prep-survey-over" data-testid="prep-survey-over" role="status">
          {copy(zh, "prepSurvey.over")}
        </p>
      )}
      <p className="cn-prep-survey-where" data-testid="prep-survey-stored">
        {copy(zh, "prepSurvey.stored")}
      </p>
      <p className="cn-prep-survey-door" data-testid="prep-survey-no-door">
        {copy(zh, "prepSurvey.noDoor")}
      </p>
      <div className="cn-prep-survey-actions">
        <button
          type="button"
          className="v8-quiet-button"
          data-testid="prep-survey-back"
          disabled={busy}
          onClick={onBack}
        >
          {copy(zh, "prepSurvey.back")}
        </button>
        <button
          type="button"
          className="v8-outline-button"
          data-testid="prep-survey-exit"
          disabled={busy}
          onClick={() => onExit(picked, note)}
        >
          {copy(zh, STOP_ACTION_KEY)}
        </button>
      </div>
    </div>
  );
}
