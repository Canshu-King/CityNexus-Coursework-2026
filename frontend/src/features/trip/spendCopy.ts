/**
 * ORD-0504 follow-on (commander ruling 01:09, cell ③) -- the one sentence this face was missing:
 * money that is inside a currency total and on no day.
 *
 * Deliberately NOT in `i18n/LocaleContext.tsx`: another lane is editing that dictionary tonight for
 * the same screen (lane `da46954a`'s ORD-0504B branch changes `spendFacts.ts` and its copy keys), and
 * a same-key collision there silently takes whichever value landed last. `useT()` cannot reach these
 * keys -- the `t` built inside `LocaleProvider` looks keys up in its own two dictionaries only and has
 * no fallback-dictionary parameter -- so the panel reads this table directly, the same way
 * `features/studio/planVersionHistoryCopy.ts` and `features/trash/journeyTrashCopy.ts` do.
 *
 * Wording rule, and it is a measurement rather than a taste: `spendFacts.ts` puts a line into `days`
 * only `if (line.dayKey)`, while every priced line goes into its currency group. A line the server
 * answered with an amount and no instant -- a commitment with no `scheduled_start_at`, a leg with no
 * `depart_at` -- is therefore counted in 合计 and in nowhere else on this page. The sentence has to
 * say that, and may not promise a day the mechanism does not have (ORD-0385: copy may not exceed the
 * mechanism). It names the count, because "some of it" is not checkable and a number is.
 */
export const spendCopy = {
  en: {
    "spend.offDay":
      "{n} priced line is inside the totals above and on no day below: this server sent it an amount " +
      "with no instant the trip's calendar can place.",
    "spend.offDay.plural":
      "{n} priced lines are inside the totals above and on no day below: this server sent them amounts " +
      "with no instant the trip's calendar can place.",
  },
  "zh-CN": {
    "spend.offDay":
      "上面合计里有 {n} 笔不在任何一天里：服务器给了它金额，没给能落进这趟日历的时刻。",
    "spend.offDay.plural":
      "上面合计里有 {n} 笔不在任何一天里：服务器给了它们金额，没给能落进这趟日历的时刻。",
  },
} as const;

export type SpendCopyKey = keyof (typeof spendCopy)["zh-CN"];

/** This face's wording for the traveller's locale, falling back to English the same way the other
 *  screen-local dictionaries do. */
export function spendFaceCopy(locale: string, key: SpendCopyKey): string {
  const table = spendCopy[locale.startsWith("zh") ? "zh-CN" : "en"];
  return table[key];
}

/** One substitution point, filled from the model -- never from a guess. */
export function fillSpend(template: string, params: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_whole, name: string) =>
    name in params ? String(params[name]) : `{${name}}`,
  );
}

/** Singular below two, plural from two up: the count is in the sentence, so the grammar has to follow
 *  it or the face reads wrong exactly when there is more than one of these lines. */
export function offDaySentence(locale: string, count: number): string {
  const key: SpendCopyKey = count === 1 ? "spend.offDay" : "spend.offDay.plural";
  return fillSpend(spendFaceCopy(locale, key), { n: count });
}
