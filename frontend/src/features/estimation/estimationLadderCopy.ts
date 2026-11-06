/**
 * ORD-0455 -- the wording of the four estimation rungs.
 *
 * Kept out of `i18n/LocaleContext.tsx` on purpose, the way `features/access/accessCopy.ts`,
 * `features/trash/journeyTrashCopy.ts` and `i18n/placeProvenanceCopy.ts` are: that dictionary is the
 * file several lanes edit the same night, and the current line has already aborted an absorption over
 * a collision in it. The one word that does come from the shared dictionary is the door -- the host
 * passes `{door}` as `t("nav.studio")`, so a remedy cannot point at a screen under one name and open
 * another.
 *
 * Four rules these strings serve:
 *  - Copy may not exceed the mechanism. Nothing here promises a re-price, a refund window or a
 *    provider the server did not answer with, and `{confidence}` only ever arrives when
 *    `estimationLadder.ts` measured a confidence to put in it.
 *  - A warning is never left without a door. `ladder.remedy.*` discharges `ladder.pre.*`, and where
 *    no door exists the copy says so instead of naming a place.
 *  - The chip and the sentence never say the same thing twice. The first run on a live screen painted
 *    "下单前请核对" beside "下单前请向供应商核对…", which reads as one stutter rather than two facts,
 *    so the chips are short nouns and the sentences carry the verbs.
 *  - The registers stay in one voice: Chinese says 核对, English says check, and neither side borrows
 *    the other's machine words -- `CAPTURED_REPLAY` reaches a traveller only through
 *    `lib/displayVocabulary.ts`, never through these strings.
 *  - A placeholder with no value stays visible as `{source}` rather than vanishing (`fillLadder`), so
 *    a cell the server did not send reads as missing instead of arriving as a finished sentence.
 *  - ORD-0468's empty reading carries no placeholder and no digit at all. "0 家候选" would put a count
 *    on the screen that the empty state never asked for, and this product's own rule is that an
 *    absence is not a zero (`ladder.pre.noAmount` says exactly that about a price).
 */

export const estimationLadderCopy = {
  en: {
    "ladder.tier.method": "How it was made",
    "ladder.tier.pre": "Before you compare",
    "ladder.tier.verify": "Verify",
    "ladder.tier.remedy": "What you can change",
    "ladder.method.sourced":
      "{source}: {amount} at {confidence}% confidence. It is a quote handed back to us, not a settlement -- the provider's own final price is what you pay.",
    "ladder.method.noConfidence":
      "{source}: {amount}. This server sent no confidence with that answer.",
    "ladder.method.noSource": "This server gave only {amount}: it wrote no source for it.",
    "ladder.method.noCandidates":
      "There is no method to report at this rung: this server returned no stay candidate this time, so there is no figure here to explain.",
    "ladder.pre.noAmount":
      "There is no rate here to compare against yet. We do not print 0 as if it were a price.",
    "ladder.pre.noConfidence":
      "Do not weigh this against another stay yet: the figure carries no provider confidence.",
    "ladder.verify.open":
      "Check this rate and its cancellation terms with the provider before you book.",
    "ladder.verify.binding":
      "Non-refundable -- check the final price and the cancellation terms with the provider before you book.",
    "ladder.remedy.switch":
      "To get a number you can compare: switch to another of the {n} candidates on this screen, or open {door} to change the hotel or one leg's mode -- the same hard commitments are re-checked there.",
    "ladder.remedy.recompute":
      "To get a number you can compare: open {door} and change the hotel or one leg's mode -- the same hard commitments are re-checked there.",
    "ladder.remedy.absent":
      "No door on this screen changes this number today: there is no second candidate here, and {door} did not open for this server.",
  },
  "zh-CN": {
    "ladder.tier.method": "怎么来的",
    "ladder.tier.pre": "比较之前",
    "ladder.tier.verify": "核对",
    "ladder.tier.remedy": "你能改的",
    "ladder.method.sourced":
      "{source}：{amount}，置信度 {confidence}%。这是报回来的一条价，不是结算单，实付以供应商的最终价格为准。",
    "ladder.method.noConfidence": "{source}：{amount}。这台服务器没有随这条回答给出置信度。",
    "ladder.method.noSource": "这台服务器只给了 {amount}：没有写它是从哪来的。",
    "ladder.method.noCandidates": "这一级没有可报的方法：这台服务器这次没有交回任何住宿候选，所以这里没有一个数可解释。",
    "ladder.pre.noAmount": "这里还没有能拿去比价的房价。我们不会把 0 写成一个价。",
    "ladder.pre.noConfidence": "先别拿它和别家比：这个数背后没有供应商置信度。",
    "ladder.verify.open": "下单前请向供应商核对这个价格与取消条款。",
    "ladder.verify.binding": "这一档不可取消 —— 下单前请向供应商核对最终价格与取消条款。",
    "ladder.remedy.switch":
      "想拿到能比价的数：可以在这里换另一家（共 {n} 家候选），或打开{door}更换酒店、改某一段的出行方式，那里会按同一套硬约束重算。",
    "ladder.remedy.recompute":
      "想拿到能比价的数：打开{door}更换酒店、或改某一段的出行方式，那里会按同一套硬约束重算。",
    "ladder.remedy.absent":
      "今天这块屏上没有能改变这个数的入口：既没有第二家候选，{door}那一扇这台服务器也没让我们打开。",
  },
} as const;

export type LadderCopyKey = keyof (typeof estimationLadderCopy)["zh-CN"];

/** A key missing on one side must never reach a traveller as a raw key. */
export function ladderCopyFor(locale: string, key: LadderCopyKey): string {
  const table = estimationLadderCopy[locale.startsWith("zh") ? "zh-CN" : "en"];
  return table[key] ?? estimationLadderCopy.en[key] ?? key;
}

export function fillLadder(template: string, params: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_, name: string) => String(params[name] ?? `{${name}}`));
}
