/**
 * ORD-0530 -- the footprint screen's own copy table.
 *
 * Why not `LocaleContext.tsx`: that file is the dictionary every lane touches (ORD-0479's own ruler
 * reads it, and a lane has been bounced off it twice this week), and the precedent for a surface
 * that needs sentences of its own is already in the tree -- `i18n/mapPanelCopy.ts` for the map glass
 * and `features/access/accessCopy.ts` for the access panel. The reason is not tidiness: `t(key)`
 * falls back to printing the KEY when a language arm is missing (the `dictionaries.en[key] ?? key` arm inside `const t = useCallback` in `LocaleProvider` of `src/i18n/LocaleContext.tsx`), so a
 * half-added dictionary entry puts a machine token on a Chinese screen, which is the defect class
 * this project keeps having to fix. Here both arms are one object literal, and
 * `review/ord0530/dictionary_key_parity_r1.py` fails if the two ever differ in key set.
 *
 * No reason code, status number, IANA-style token or server prose is produced by anything in this
 * file except `zonePhrase`, which names the journey's own timezone -- server data the traveller is
 * entitled to see, not a machine word about a failure.
 */

export type FootprintCopyKey =
  | "scope"
  | "dayHeading"
  | "dayZone"
  | "noZoneHeading"
  | "time"
  | "target"
  | "noteAbsent"
  | "noteLabel"
  | "targetGone"
  | "sourceLabel"
  | "sourceWeb"
  | "sourceUnknown"
  | "empty"
  | "refused"
  | "unreachable"
  | "noJourney"
  | "loading"
  | "retry"
  | "count"
  | "dayCount";

const COPY: Record<FootprintCopyKey, { zh: string; en: string }> = {
  scope: {
    zh: "这一屏只列服务器替这次行程记下的印，按天分，天内按打的先后排。",
    en: "Every row here is a stamp the server holds for this trip, grouped by day and, inside a day, in the order they were stamped.",
  },
  /* The day line. `{day}` is filled by the component from the server's own instant, read in the
     journey's timezone -- see `zonePhrase`, which says out loud which zone the split used. */
  dayHeading: { zh: "{day}", en: "{day}" },
  dayZone: { zh: "日期按这趟行程自己的时区（{zone}）分，不按这台设备的钟。",
             en: "Days are split on this trip's own timezone ({zone}), not on this device's clock." },
  noZoneHeading: { zh: "这趟行程没有给出自己的时区，所以下面的日期是这台设备的钟。",
                   en: "This trip did not carry a timezone, so the dates below are this device's clock." },
  time: { zh: "时刻", en: "time" },
  target: { zh: "地点", en: "place" },
  noteLabel: { zh: "备注", en: "note" },
  noteAbsent: { zh: "没写备注", en: "no note was written" },
  /* The server resolves the target on every read, so a stamp whose activity has left the plan
     answers null -- which is a fact about the row, and gets a sentence, not a blank cell. */
  targetGone: { zh: "那一站已经不在计划里", en: "that stop has left the plan" },
  sourceLabel: { zh: "来源", en: "from" },
  /* `source` is `FOREGROUND_WEB` in the payload. The word that reaches the screen is ours. */
  sourceWeb: { zh: "网页端打的", en: "stamped from the web" },
  sourceUnknown: { zh: "来源这一格服务器没给名字", en: "the server gave no name for the source" },
  empty: { zh: "还没有足迹。打开行程，在一站上按「打卡」。",
           en: "No footprints yet. Open the itinerary and tap Check in on a stop." },
  /* Two different failures, two sentences: the order asks the screen to say WHICH step did not
     answer. The reason code stays a value the component switches on, never a printed word. */
  refused: { zh: "服务器答了，但这次没有把足迹交出来。",
             en: "The server answered, but it would not hand the footprints over this time." },
  unreachable: { zh: "这一屏没问到服务器，所以这里既没说有事，也没说没事。",
                 en: "This screen never reached the server, so it is saying neither that there are stamps nor that there are none." },
  noJourney: { zh: "现在没有打开的行程，所以没向服务器问任何东西。",
               en: "No trip is open, so nothing was asked of the server." },
  loading: { zh: "正在读足迹…", en: "Reading the footprints…" },
  retry: { zh: "重读一次", en: "Read again" },
  count: { zh: "这次行程共有 {count} 枚足迹。", en: "This trip holds {count} footprints." },
  dayCount: { zh: "分成 {count} 天。", en: "Across {count} days." },
};

/** Same rule the map copy table uses: anything starting with `zh` takes the Chinese arm. */
export function footprintCopy(
  locale: string,
  key: FootprintCopyKey,
  slots?: Record<string, string | number>,
): string {
  let text = COPY[key][locale.startsWith("zh") ? "zh" : "en"];
  for (const [name, value] of Object.entries(slots ?? {})) {
    text = text.replace(`{${name}}`, String(value));
  }
  return text;
}

/**
 * The words for one stamp's source. Unknown values get a sentence that says the server did not name
 * it -- they do not get the raw token, which is what puts SCREAMING_SNAKE on a traveller's screen.
 */
export function footprintSourceWords(locale: string, source: string | null | undefined): string {
  if (source === "FOREGROUND_WEB") return footprintCopy(locale, "sourceWeb");
  return footprintCopy(locale, "sourceUnknown");
}

/**
 * Which day a server instant falls on, and what the traveller reads for it.
 *
 * All three take the zone explicitly. `Intl` reads a `Date` in the *device's* zone unless it is told
 * otherwise, which is exactly the guess this order forbids: the same stamp lands on 09/28 in Los
 * Angeles and 09/29 in Asia/Shanghai, and only one of those is the day the trip was on. Passing
 * `timeZone: null` is the named fallback (the trip carried no zone), and the screen says so in
 * `noZoneHeading` rather than letting the device answer silently.
 */
function zoned(locale: string, date: Date, timeZone: string | null, options: Intl.DateTimeFormatOptions): string {
  try {
    return new Intl.DateTimeFormat(locale, {
      ...(timeZone ? { timeZone } : {}),
      ...options,
    }).format(date);
  } catch {
    /* An unusable zone name is a server fact that cannot be honoured, not a reason to crash the
       screen: fall back to the device and let the heading say which clock was used. */
    return new Intl.DateTimeFormat(locale, options).format(date);
  }
}

/** `2026-10-01` -- the grouping key. Always `en-CA`, whose format is this ISO shape in any zone. */
export function footprintDayKey(date: Date, timeZone: string | null): string {
  return zoned("en-CA", date, timeZone, { year: "numeric", month: "2-digit", day: "2-digit" });
}

/** The heading: `10月1日 周四` / `Oct 1, Thu`. */
export function footprintDayPhrase(locale: string, date: Date, timeZone: string | null): string {
  const parts = zoned(locale, date, timeZone, { month: "long", day: "numeric", weekday: "short" });
  if (!locale.startsWith("zh")) return parts;
  /* Chinese `Intl` output already reads 10月1日 周四; returned unchanged so the two arms stay one call. */
  return parts;
}

/** The clock on the row: `09:17`. */
export function footprintTimePhrase(locale: string, date: Date, timeZone: string | null): string {
  return zoned(locale, date, timeZone, { hour: "2-digit", minute: "2-digit", hour12: false });
}
