/**
 * ORD-0520 -- copy for 中止.
 *
 * Deliberately NOT in `i18n/LocaleContext.tsx`: that dictionary is the shared file several lanes edit
 * on the same night, and `features/trash/journeyTrashCopy.ts` and `features/studio/
 * planVersionHistoryCopy.ts` set the precedent for keeping one screen's words beside that screen.
 *
 * Three rules the order states and this file is the only place they are written down:
 *  - the sentence has to say what happens AFTER the press (it leaves 实时行程, it stays in the trip
 *    list, the reason is stored and read back) and whether the traveller can come BACK (this server
 *    has no un-abort door, so no row here may promise one);
 *  - the bin's vocabulary stays the bin's: 找回 and 丢弃 name `undeleteJourney` and `deleteJourney`,
 *    还原 is plan version history's word, and 恢复 with `recover*` is autonomous recovery.  Nothing
 *    in this file uses any of them for 中止, and nothing here offers a second action on a trip that
 *    has been dropped;
 *  - a refusal is *said*.  Each `error.reason_code` gets its own sentence, because each names a
 *    different condition; the machine token itself goes on the 技术详情 face, where it is quotable
 *    and not required reading (same rule as the recycle-bin screen).
 */
export const journeyAbortCopy = {
  en: {
    "abort.title": "Abort this trip",
    "abort.explains":
      "After you abort it: this trip leaves “Live trips”, it stays in your trip list -- aborting is " +
      "not discarding, and the recycle bin is the only place that discards -- and the reason you " +
      "write is stored with the trip, so any later screen can read it back. There is no way back: " +
      "this server has no door that un-aborts a trip, so travelling it again means starting a new one.",
    "abort.reasonLabel": "Why you are calling it off (stored with the trip)",
    "abort.reasonPlaceholder": "e.g. something came up at home and we are not going this time",
    "abort.action": "Abort this trip",
    "abort.busy": "Aborting…",
    "abort.done":
      "Aborted: {title}. The reason is stored with the trip, it is no longer on “Live trips”, and it " +
      "is still in your trip list.",
    "abort.stateOnScreen": "aborted",
    "abort.alreadyAborted":
      "This trip is already aborted -- at {when}, for this reason: “{reason}”. The server did not " +
      "write it a second time.",
    "abort.storedReason": "Reason you gave: {reason}",
    "abort.storedAt": "Aborted at {when}",
    "abort.readbackMissing":
      "this server did not read back the abort moment or the reason for this trip",
    "abort.liveExcluded":
      "This trip was aborted, so “Live trips” does not show it. That decision came from the server's " +
      "own field, not from this device filtering the status.",

    "abort.inBin":
      "This trip is in the recycle bin, so it cannot be aborted. Aborting is not discarding: bring it " +
      "back into your trip list first, then abort it.",
    "abort.notYours":
      "The traveller this request named does not own that trip, so nothing was written.",
    "abort.gone":
      "This trip is no longer on this server, so there is nothing here to abort. Nothing was written.",
    "abort.contested":
      "This trip changed while the request was running, so nothing was written. Nothing on screen is " +
      "claiming otherwise.",
    "abort.emptyReason":
      "There has to be a reason. An empty box is refused instead of storing a trip that ended for no " +
      "stated reason.",
    "abort.refused":
      "The server refused this attempt. Your trip is unchanged -- it is still live and still in your " +
      "trip list.",
    "abort.unnamed":
      "The server refused this attempt without naming the condition it refused. Your trip is " +
      "unchanged.",
    "abort.transport":
      "The server did not answer, so nothing about your trip changed because of this screen.",
    "abort.details": "Technical details",
  },
  "zh-CN": {
    "abort.title": "中止这趟行程",
    "abort.explains":
      "中止之后：这趟不再进入「实时行程」；它仍留在你的行程列表里——中止不等于丢弃，只有回收站那两扇门管丢弃；你写下的原因会和这趟行程一起存下来，之后任何一屏都能读回。不能回头：这台服务器没有把中止撤回的门，想再走这条路只能新开一趟。",
    "abort.reasonLabel": "中止的原因（会随行程一起存下）",
    "abort.reasonPlaceholder": "例如：家里临时有事，这趟先不去了",
    "abort.action": "中止这趟行程",
    "abort.busy": "正在中止…",
    "abort.done": "已中止：{title}。原因已随这趟行程存下，它不再出现在「实时行程」里，但仍留在你的行程列表中。",
    "abort.stateOnScreen": "已中止",
    "abort.alreadyAborted": "这趟已经被中止：{when}，原因是「{reason}」。服务器没有把它再写一次。",
    "abort.storedReason": "你当时的原因：{reason}",
    "abort.storedAt": "中止于 {when}",
    "abort.readbackMissing": "这台服务器没有回读这趟行程的中止时间或原因",
    "abort.liveExcluded":
      "这趟行程已被中止，所以「实时行程」不展示它。这个判定来自服务器给出的字段，不是这台设备自己按状态筛的。",

    "abort.inBin":
      "这趟在回收站里，所以不能中止。中止不等于丢弃：先把它找回你的行程列表，再中止。",
    "abort.notYours": "这次请求点名的身份不是这趟行程的主人，所以服务器没有写入。",
    "abort.gone": "这趟已经不在这台服务器上了，这里没有可中止的东西。服务器没有写入。",
    "abort.contested":
      "请求在跑的时候这趟行程被改动过，所以这次没有写入。屏上也不会装作它成功了。",
    "abort.emptyReason": "必须写原因。空着会被拒绝，而不是存下一趟「不知道为什么就结束」的行程。",
    "abort.refused":
      "服务器拒绝了这次中止。你的行程没有变化：它仍在实时行程里，也仍在你的行程列表里。",
    "abort.unnamed":
      "服务器拒绝了这次中止，但没有说出拒绝的是哪一种情况。你的行程没有变化。",
    "abort.transport": "这台服务器没有应答。这一屏没有改动你的任何行程。",
    "abort.details": "技术详情",
  },
} as const;

export type JourneyAbortCopyKey = keyof (typeof journeyAbortCopy)["zh-CN"];

/** A key missing in one language must never reach the traveller as a raw key. */
export function abortCopy(locale: string, key: JourneyAbortCopyKey): string {
  const table = journeyAbortCopy[locale.startsWith("zh") ? "zh-CN" : "en"];
  return table[key] ?? journeyAbortCopy.en[key] ?? key;
}

/** `{when}` and `{reason}` come from the read or the refusal's own details, never from this file. */
export function fillAbort(template: string, params: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_, name: string) => String(params[name] ?? `{${name}}`));
}

/**
 * The server's named refusal, in one table: each `error.reason_code` this door can answer gets its
 * own sentence, because each is a different fact.  A code that is not here is reported as unnamed --
 * the screen must not pick a condition it did not read.
 */
export const ABORT_REFUSAL_COPY: Record<string, JourneyAbortCopyKey> = {
  JOURNEY_ALREADY_ABORTED: "abort.alreadyAborted",
  ABORT_JOURNEY_IN_RECYCLE_BIN: "abort.inBin",
  ABORT_ACTOR_NOT_OWNER: "abort.notYours",
  ABORT_WRITE_CONTESTED: "abort.contested",
  JOURNEY_NOT_FOUND: "abort.gone",
};
