/**
 * ORD-0440 - copy for the sheet that sits in front of 「停止这次准备」.
 *
 * Borrowed shape, not borrowed words (abstraction ruling `ABSORPTION_FULL_SCOPE_V1`: the shape is a
 * churn survey placed before a destructive act, whose exit keeps naming the real act). Every value
 * here is 12..70 characters on purpose so both arms enter the pairing ruler's denominator, which is
 * how "the English arm says the same tier" is measured rather than promised.
 */

export type SurveyKey =
  | "prepSurvey.title"
  | "prepSurvey.sub"
  | "prepSurvey.r1"
  | "prepSurvey.r2"
  | "prepSurvey.r3"
  | "prepSurvey.r4"
  | "prepSurvey.r5"
  | "prepSurvey.note"
  | "prepSurvey.max"
  | "prepSurvey.over"
  | "prepSurvey.stored"
  | "prepSurvey.noDoor"
  | "prepSurvey.back"
  | "prepSurvey.stopAction";

export const SURVEY_COPY: Record<"zh" | "en", Record<SurveyKey, string>> = {
  zh: {
    "prepSurvey.title": "能说说为什么现在要停止吗？",
    "prepSurvey.sub": "原因可以多选，也可以整个跳过。",
    "prepSurvey.r1": "它转得太久，我等不下去了",
    "prepSurvey.r2": "这不是我要找的那个地方",
    "prepSurvey.r3": "我改主意了，待会儿再说",
    "prepSurvey.r4": "我们没把正在做什么讲清楚",
    "prepSurvey.r5": "其他，我想自己补一句",
    "prepSurvey.note": "可以补充一句，也可以不写",
    "prepSurvey.max": "这一句最多 200 个字",
    "prepSurvey.over": "超了，先删掉几个字再说",
    "prepSurvey.stored": "这几个答案只写在这台设备上。",
    "prepSurvey.noDoor": "这台服务器还没有收这个问题的门（NOT_AVAILABLE）。",
    "prepSurvey.back": "先不停止，我再看看",
    "prepSurvey.stopAction": "停止这次准备",
  },
  en: {
    "prepSurvey.title": "Can you tell us why you're stopping now?",
    "prepSurvey.sub": "Pick any that apply, or skip them all.",
    "prepSurvey.r1": "It was taking too long for me to wait",
    "prepSurvey.r2": "This is not the place I was looking for",
    "prepSurvey.r3": "I changed my mind for now",
    "prepSurvey.r4": "We didn't make clear what it was doing",
    "prepSurvey.r5": "Something else - I'll add a line",
    "prepSurvey.note": "Add a line if you want, or leave it empty",
    "prepSurvey.max": "This line holds at most 200 characters",
    "prepSurvey.over": "Over the line - trim a few words first",
    "prepSurvey.stored": "These answers stay on this device.",
    "prepSurvey.noDoor": "This server has no door for the question (NOT_AVAILABLE).",
    "prepSurvey.back": "Don't stop yet, let me look",
    "prepSurvey.stopAction": "Stop this preparation",
  },
};

/** The reason codes are stable tokens, not the sentences: a later door can count them. */
export const SURVEY_REASONS: Array<{ code: string; key: SurveyKey }> = [
  { code: "TOO_SLOW", key: "prepSurvey.r1" },
  { code: "WRONG_PLACE", key: "prepSurvey.r2" },
  { code: "CHANGED_MIND", key: "prepSurvey.r3" },
  { code: "WE_WERE_UNCLEAR", key: "prepSurvey.r4" },
  { code: "OTHER", key: "prepSurvey.r5" },
];

/** Kept in one place so the sheet's exit and the panel's button cannot drift apart in wording. */
export const STOP_ACTION_KEY: SurveyKey = "prepSurvey.stopAction";
export const NOTE_MAX_CHARS = 200;
export const LOG_KEY = "REDACTED_FOR_PUBLIC_RELEASE";
export const LOG_CAP = 20;

export type SurveyRecord = {
  at: string;
  operation_id: string;
  reasons: string[];
  /** The traveller's own line, kept verbatim but only on this device (see the note above). */
  note: string;
  note_chars: number;
  skipped: boolean;
  /** Whether the stop the survey preceded actually landed. A survey that recorded a stop the
   *  server refused would be a report about our own widget, not about what happened. */
  stopped: boolean;
};

/**
 * The answer's destination is named, not invented: this build has no write door that accepts a
 * reason for stopping, so the record stays on the device and the sheet says exactly that. Writing it
 * to a server would be the fake this order forbids; deleting the survey would be the other fake.
 */
export function appendSurveyLog(entry: SurveyRecord): SurveyRecord[] {
  let existing: SurveyRecord[] = [];
  try {
    const raw = window.localStorage.getItem(LOG_KEY);
    existing = raw ? (JSON.parse(raw) as SurveyRecord[]) : [];
  } catch {
    existing = [];
  }
  const next = [...existing, entry].slice(-LOG_CAP);
  try {
    window.localStorage.setItem(LOG_KEY, JSON.stringify(next));
  } catch {
    /* A device that refuses storage still stops the job; the sheet already said the answers stay here. */
  }
  return next;
}
