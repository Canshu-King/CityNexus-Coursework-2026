/**
 * ORD-0402 slice 2 -- copy for the recycle-bin screen.
 *
 * Deliberately NOT in `i18n/LocaleContext.tsx`: that file is the shared dictionary other lanes edit
 * on the same night (the ORD-0386 screen made the same choice for the same reason, see
 * `features/studio/planVersionHistoryCopy.ts`). Only `nav.trash` went into the shared dictionary,
 * because the nav strip builds its labels as `t("nav." + key)` and has no fallback hook.
 *
 * Three wording rules, all of them from the order:
 *  - the verb is 找回 / "bring it back". 恢复 and `recover*` are autonomous recovery in this product
 *    (`recovery.*` keys, `POST /journeys/{id}/recovery/acknowledge`), and 还原 is the plan-version
 *    screen's word (the wording rule stated in that screen's own header,
 *    `features/studio/planVersionHistoryCopy.ts`). The server picked the same pair
 *    (`backend/app/main.py` -- "The word is 找回 / undelete").
 *  - a refusal is *said*, not printed as a code. The 409 `JOURNEY_NOT_DELETED` gets the sentence
 *    "这条已经在你的行程列表里了。"; the machine token stays on the details face where a screen can
 *    quote it without the traveller having to read a code to know what happened.
 *  - nothing here offers a second action on a deleted trip. There is no permanent-delete route on
 *    this server, so no sentence may imply one (ORD-0385: copy may not exceed the mechanism).
 */
export const journeyTrashCopy = {
  en: {
    "trash.title": "Recycle bin",
    "trash.sub":
      "The trips this server says are in the bin. Each row has one action: bring it back into your trip list.",
    "trash.loading": "Reading the bin…",
    "trash.empty": "The bin is empty: this server named no trip in it.",
    "trash.count": "{n} trips in the bin",
    "trash.rowState": "in the bin",
    "trash.rowStateMissing": "this server gave no bin state for this row",
    "trash.deletedAt": "put in the bin at {when}",
    "trash.deletedAtMissing": "this server gave no discard time for this trip",
    /* ORD-0535: a trip that was dropped, brought back and dropped again is still ONE row here, and
       the row's own moment is only the latest one. So the row has to say which discard this is. */
    "trash.discardSequence": "discard #{n} for this trip",
    "trash.discardSequenceFirst": "this is the first time this trip was dropped",
    "trash.previousDiscard": "the earlier one was at {when}",
    "trash.previousDiscardRecorded": "the earlier one has no discard time in the log; its log line is {when}",
    "trash.sequenceUnreadable":
      "this server did not answer this trip's discard log, so which time this is cannot be said",
    "trash.sequenceLoading": "reading this trip's discard log…",
    "trash.sequenceEmpty":
      "the discard log for this trip came back with no discard in it, so which time this is cannot be said",
    "trash.action": "Bring it back",
    "trash.busy": "Bringing it back…",
    "trash.found": "Brought back: {title}. It is in your trip list again.",
    "trash.already": "This trip is already in your trip list -- there was nothing to bring back.",
    "trash.gone": "This trip is no longer on this server, so it cannot be brought back.",
    "trash.refused": "The server refused this attempt. Your trip list is unchanged.",
    "trash.transport":
      "The bin could not be read from this server. Nothing about your trips changed because of this screen.",
    "trash.doorAbsent":
      "This server did not answer the list of binned trips, so no row can be shown yet. Nothing about your trips changed.",
    "trash.refresh": "Read the bin again",
    "trash.noViewer":
      "This screen does not know whose bin to ask for, so it is not showing anybody's. Nothing about your trips changed.",
    "trash.rowOwnershipMissing": "this server gave no owner for this row",
    "trash.rowOwnedByOther": "kept by another traveller on this server",
    "trash.rowOwnedByMe": "yours on this server",
    "trash.rowNotStated": "this server did not say whose trip this is",
    "trash.recoveryAllowed": "you may bring this one back",
    "trash.recoveryRefusedRole":
      "You hold a seat on this trip, but the role you hold may not bring it back, so it is read-only here.",
    "trash.recoveryRefusedNoSeat":
      "This trip belongs to somebody else and you hold no seat on it here, so it cannot be brought back from this screen.",
    "trash.recoveryRefusedOther":
      "This trip belongs to another traveller on this server, so it is shown without an action.",
    "trash.recoveryMissing":
      "This server gave no answer about whether you may bring this one back, so no action is offered.",
  },
  "zh-CN": {
    "trash.title": "回收站",
    "trash.sub": "这台服务器说还在回收站里的行程。每一行只有一个动作：把它找回你的行程列表。",
    "trash.loading": "正在读回收站…",
    "trash.empty": "回收站是空的：这台服务器没有报出任何在站里的行程。",
    "trash.count": "回收站里有 {n} 条行程",
    "trash.rowState": "在回收站里",
    "trash.rowStateMissing": "服务器没有给出这一行的在站状态",
    "trash.deletedAt": "丢弃于 {when}",
    "trash.deletedAtMissing": "这台服务器没有给出这条行程的丢弃时间",
    "trash.discardSequence": "这是这条行程第 {n} 次被丢弃",
    "trash.discardSequenceFirst": "这是这条行程第一次被丢弃",
    "trash.previousDiscard": "上一次丢弃于 {when}",
    "trash.previousDiscardRecorded": "上一次那笔流水里没有丢弃时间，它的记录时间是 {when}",
    "trash.sequenceUnreadable": "这台服务器没有答出这条行程的丢弃流水，所以说不清这是第几次",
    "trash.sequenceLoading": "正在读这条行程的丢弃流水…",
    "trash.sequenceEmpty": "这条行程的流水里一笔丢弃都没有，所以说不清这是第几次",
    "trash.action": "找回",
    "trash.busy": "正在找回…",
    "trash.found": "已找回：{title}。它回到你的行程列表里了。",
    "trash.already": "这条已经在你的行程列表里了，没有需要找回的东西。",
    "trash.gone": "这条已经不在这台服务器上了，找不回来。",
    "trash.refused": "服务器拒绝了这次找回。你的行程列表没有变化。",
    "trash.transport": "回收站没有从这台服务器读到。这一屏没有改动你的任何行程。",
    "trash.doorAbsent": "这台服务器没有答出「在回收站里的行程」这份列表，所以现在一行都显示不出来。你的行程没有因此变化。",
    "trash.refresh": "再读一次回收站",
    "trash.noViewer": "这一屏还不知道要问谁的回收站，所以任何人的都不显示。你的行程没有因此变化。",
    "trash.rowOwnershipMissing": "服务器没有给出这一行的归属",
    "trash.rowOwnedByOther": "由这台服务器上的另一位旅客保管",
    "trash.rowOwnedByMe": "在这台服务器上属于你",
    "trash.rowNotStated": "这台服务器没说这条行程是谁的",
    "trash.recoveryAllowed": "这条你可以找回",
    "trash.recoveryRefusedRole": "你在这条行程上有席位，但你那个角色不能把它找回来，所以这里只读。",
    "trash.recoveryRefusedNoSeat": "这条属于别人，而且你在这台服务器上没有它的席位，所以这一屏找不回来。",
    "trash.recoveryRefusedOther": "这条在这台服务器上属于另一位旅客，所以这里显示但不给动作。",
    "trash.recoveryMissing": "服务器没有回答这条能不能找回，所以这里不给动作。",
  },
} as const;

export type JourneyTrashCopyKey = keyof (typeof journeyTrashCopy)["zh-CN"];

/** Same fallback rule as every other lane copy module: a key missing in one language must never
 *  reach the traveller as a raw key. */
export function trashCopy(locale: string, key: JourneyTrashCopyKey): string {
  const table = journeyTrashCopy[locale.startsWith("zh") ? "zh-CN" : "en"];
  return table[key] ?? journeyTrashCopy.en[key] ?? key;
}

/** `{n}` and `{title}` come from the read and the response, never from a number this file knows. */
export function fillTrash(template: string, params: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_, name: string) => String(params[name] ?? `{${name}}`));
}
