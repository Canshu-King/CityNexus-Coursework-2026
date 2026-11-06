/**
 * ORD-0439: copy for the one-screen plan comparison.
 *
 * Own file, not `i18n/LocaleContext.tsx` (a contested file tonight), and the dimension names are
 * ours: 预算 / 步行时长 / 受保护锚点 / 硬可用证据段数. No competitor vocabulary is carried in -- the
 * order forbids borrowing a dimension name onto our screen.
 */
export const planRankingCopy = {
  en: {
    "rank.title": "Compare the plans this trip can choose from",
    "rank.sub": "Ranked on numbers this product computes for each plan. A dimension every plan shares is shown, but never used to break a tie.",
    "rank.dim.cost": "cost",
    "rank.dim.walk": "walking minutes",
    "rank.dim.protected": "protected stops",
    "rank.dim.evidence": "legs with hard evidence",
    "rank.version": "Plan",
    "rank.priceUnknown": "price unknown",
    "rank.walkNotMeasured": "not measured",
    "rank.walkPartial": "only partly measured",
    "rank.protectedNotMarked": "not marked",
    "rank.noFacts": "no legs",
    "rank.rankedBy": "Ranked by: {dims}.",
    "rank.tie": "These plans cannot be separated on {dims}; they are listed in the order the server wrote them.",
    "rank.onlyOne": "There is only one plan to compare here, so nothing is ranked.",
    "rank.selected": "This trip already has a selected plan; there are no other candidate plans to compare.",
    "rank.none": "No plan is available to compare right now.",
    "rank.evidenceNote": "{hard} of {total} legs on this plan rest on evidence the gate accepts as hard; the rest are {quality}.",
    "rank.loading": "Reading the plans\u2026",
    "rank.failed": "The plan list could not be read from this server. Nothing about your trip was changed.",
  },
  "zh-CN": {
    "rank.title": "把这趟能选的方案摆在一屏上比",
    "rank.sub": "排序只用这个产品自己为每一版算得出的数。所有方案都一样的那一维照实列出来，但不用它分高下。",
    "rank.dim.cost": "预算",
    "rank.dim.walk": "步行时长",
    "rank.dim.protected": "受保护锚点",
    "rank.dim.evidence": "硬可用证据段数",
    "rank.version": "方案",
    "rank.priceUnknown": "价格未知",
    "rank.walkNotMeasured": "没量到",
    "rank.walkPartial": "只量到一部分",
    "rank.protectedNotMarked": "没标受保护项",
    "rank.noFacts": "没有路段",
    "rank.rankedBy": "按这些维度排：{dims}。",
    "rank.tie": "这些方案在「{dims}」上分不开，按服务器写入的先后列出。",
    "rank.onlyOne": "这里只有一个方案可比，所以不做排序。",
    "rank.selected": "这趟行程已经选定一版方案，目前没有其他候选方案可比较。",
    "rank.none": "现在没有可比的方案。",
    "rank.evidenceNote": "这一版 {total} 段里有 {hard} 段的证据是闸门认可能当硬依据的，其余是 {quality}。",
    "rank.loading": "正在读方案\u2026",
    "rank.failed": "方案列表没有从这台服务器读到。你行程上的任何东西都没有被改动。",
  },
} as const;

export type PlanRankCopyKey = keyof (typeof planRankingCopy)["zh-CN"];

export function rankCopy(locale: string, key: PlanRankCopyKey): string {
  const table = planRankingCopy[locale.startsWith("zh") ? "zh-CN" : "en"];
  return table[key] ?? planRankingCopy.en[key] ?? key;
}

export function fillRank(template: string, params: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_, name: string) => String(params[name] ?? `{${name}}`));
}
