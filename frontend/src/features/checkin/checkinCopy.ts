/* ORD-0500 -- the words this screen says when the footprint door refuses.
 *
 * Deliberately NOT in `i18n/LocaleContext.tsx`: that dictionary is the shared serialisation point
 * every lane appends to, and the two orders landed tonight both had to re-cut because of it (see
 * `features/access/accessCopy.ts`, which made the same choice for the same reason).  What lives here
 * is the refusal vocabulary of one door, keyed by the `reason_code` the server answers with, so a
 * new refusal name is one row in this table and one arm in this feature's spec.
 *
 * The button's own words (打卡 / Check in and their aria forms) stay in `i18n/LocaleContext.tsx`,
 * which owns them.  What lives here is only the refusal sentences -- with one difference this lane
 * used to describe and no longer has to: the dictionary also held `checkin.duplicate` and
 * `checkin.refused` (both locales), which stopped having a reader when ORD-0500 r5 moved this screen
 * onto the table below.  The commander ruled them deleted (04:26, 裁「删」) and they are gone,
 * together with the arm of this feature's row gate that had been pinning them open.
 *
 * Two rules every row obeys:
 *  - a sentence never carries an id. The server's `details` carry `existing_checkin_id` and
 *    `activity_id` as uuids; what a traveller can act on is the MOMENT the winning stamp was made,
 *    so `duplicateAt` interpolates `existing_stamped_at` and nothing else;
 *  - an answer this screen has not learned to speak is said as a gap, with the status this device
 *    measured, rather than being folded into a generic "something went wrong" -- that is how a
 *    refusal stops being actionable.
 */

/** The refusal vocabulary, in the same {zh, en} shape `accessCopy` uses. */
export type CheckinCopyKey =
  | "duplicateAt"
  | "duplicateNoMoment"
  | "activityUnknown"
  | "activityNotInJourney"
  | "placeUnknown"
  | "targetRequired"
  | "targetAmbiguous"
  | "journeyInBin"
  | "journeyUnknown"
  | "unreachable"
  | "unexpected";

export const CHECKIN_COPY: Record<CheckinCopyKey, { zh: string; en: string }> = {
  /* The duplicate answer has to point at the stamp that won, in the only two terms a traveller can
     check: this stop, and the instant the server wrote.  No uuid. */
  duplicateAt: {
    zh: "这一站已经打过卡了（服务器记的是 {moment}），这次没有再盖一枚，行程里还是原来那一枚。",
    en: "This stop is already stamped ({moment}); nothing new was written — the trip keeps the stamp it had.",
  },
  duplicateNoMoment: {
    zh: "这一站已经打过卡了，这次没有再盖一枚；那一枚的时间这次没有随答案回来。",
    en: "This stop is already stamped and nothing new was written; the winning stamp's moment did not come back with this answer.",
  },
  activityUnknown: {
    zh: "这一站已经不在服务器上了，所以这枚打卡没有落下。请回行程列表重新打开这趟。",
    en: "That stop is no longer on the server, so this stamp was not written. Open the trip again from your trips list.",
  },
  activityNotInJourney: {
    zh: "这一站不属于这趟行程，所以这枚打卡没有落下。请在这一趟自己的行程里打卡。",
    en: "That stop belongs to another trip, so this stamp was not written. Stamp a stop from this trip's own itinerary.",
  },
  placeUnknown: {
    zh: "那个地点我们这边没有档案，所以这枚打卡没有落下。",
    en: "We have no record of that place here, so this stamp was not written.",
  },
  /* The control's own name is interpolated, never written here: the dictionary says what the button
     is called in each language, and a sentence that hard-codes one language's label points at a word
     that does not exist on the other language's screen (ORD-0500 r6 measured `打卡` inside the en row
     while the en screen reads "Check in"). */
  targetRequired: {
    zh: "这枚打卡没有说要记在哪一站，所以没有落下。请在某一站上按「{button}」。",
    en: "This stamp did not name a stop, so nothing was written. Tap {button} on one of the stops.",
  },
  targetAmbiguous: {
    zh: "这枚打卡同时指向一站和一个地点，只能记一个，所以没有落下。",
    en: "This stamp named both a stop and a place, and only one can be recorded, so nothing was written.",
  },
  journeyInBin: {
    zh: "这趟行程在回收站里，先把它找回来才谈得上打卡或读足迹。",
    en: "This trip is in the recycle bin; bring it back before stamping or reading its footprints.",
  },
  journeyUnknown: {
    zh: "这台服务器上没有这趟行程，所以这一屏的打卡没有落下。",
    en: "This server has no such trip, so this stamp was not written.",
  },
  unreachable: {
    zh: "这一问没有到达服务器，所以这里不能说打卡有没有落下。",
    en: "This request never reached the server, so nothing here can say whether the stamp was written.",
  },
  unexpected: {
    zh: "服务器收了这枚打卡但给了一个这里说不上名的答复（{status}），所以这一屏按没有打卡处理。",
    en: "The server answered this stamp with a name this screen does not recognise ({status}), so this screen treats it as not written.",
  },
};

/** `error.reason_code` -> the row that speaks it.  Anything absent falls to `unexpected`. */
export const CHECKIN_REFUSAL_COPY: Record<string, CheckinCopyKey> = {
  CHECKIN_DUPLICATE_STAMP: "duplicateAt",
  CHECKIN_ACTIVITY_UNKNOWN: "activityUnknown",
  CHECKIN_ACTIVITY_NOT_IN_JOURNEY: "activityNotInJourney",
  CHECKIN_PLACE_UNKNOWN: "placeUnknown",
  CHECKIN_TARGET_REQUIRED: "targetRequired",
  CHECKIN_TARGET_AMBIGUOUS: "targetAmbiguous",
  CHECKIN_JOURNEY_IN_RECYCLE_BIN: "journeyInBin",
  CHECKIN_JOURNEY_UNKNOWN: "journeyUnknown",
};

export function wordsFor(entry: { zh: string; en: string }, zh: boolean,
                         params?: Record<string, string>): string {
  const raw = zh ? entry.zh : entry.en;
  if (!params) return raw;
  return raw.replace(/\{(\w+)\}/g, (_all, name) =>
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : "{" + name + "}");
}
/**
 * The instant of the stamp that won, as this screen's own words render it.
 *
 * Deliberately not the shared date helper (`frontend/src/lib/v8Formatting`), which the access
 * panel and the plan-version history panel also call: the value coming in here was read out of
 * the server's response body, and ORD-0500 measured that routing a body value through that one
 * helper costs those two surfaces their exemption -- the exposure ruler follows the path, so it
 * then claims a cell at every other caller of the helper.  This module is imported by the
 * itinerary screen and by nothing else, so the path ends here.
 *
 * The option that matters is the month shape: `numeric` makes zh-CN answer `10/1`, while these
 * surfaces show `10月1日` (`short`), and the order's carrier asserts that shape -- so the assertion
 * is the thing that reddened when I first wrote `numeric`, and this renderer is what changed.
  * An instant the door did not send renders as an empty string, and the caller falls back to the
 * sentence that says so without a moment; nothing here invents a time.
 */
export function formatStampedMoment(value?: string | null, zh = false): string {
  if (!value) return "";
  const stamp = new Date(value);
  if (Number.isNaN(stamp.getTime())) return "";
  return new Intl.DateTimeFormat(zh ? "zh-CN" : "en-GB", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(stamp);
}
