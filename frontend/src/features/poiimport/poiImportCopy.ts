/* ORD-0524 — the words the import screen needs for its three destinations, in this feature's own table.

   Why a local table instead of `i18n/LocaleContext.tsx`: that file is the one every lane's front-end
   order edits (ORD-0435's r8 could not merge for exactly that reason), and the house answer to a
   feature's own sentences is a table inside the feature -- `features/collections/collectionCopy.ts`
   and `i18n/mapPanelCopy.ts` are the two this file copies the shape of. Nothing here is exempted from
   the exposure ruler; these are this order's sentences, not upstream prose.

   The shelf words are NOT repeated here: 收藏到… / 收到哪个架子 / 已收在 / 这一枚已经在这个架子上 are
   ORD-0444 and ORD-0510's own lines in `collectionCopy.ts`, and `PoiImportPanel` imports them from
   there. Re-typing them here would be the fourth way of saying the same thing, which is what the order
   forbids.

   Counted nouns go through `{plural:stem}` (`lib/plural.ts`, ORD-0433) rather than a per-key plural of
   this file's own -- the ORD-0467 alignment ruling says the vocabulary is shared or it is not written.
*/

import { expandPluralMarkers } from "../../lib/plural";

export type PoiImportCopyKey =
  | "exitsLead"
  | "selectAll"
  | "selectAllOn"
  | "exitStoreOnly"
  | "storeOnlyRefusal"
  | "storeOnlyChosen"
  | "landingDayChosen"
  | "landingDayFirst"
  | "landingDayNone"
  | "collectAction"
  | "collecting"
  | "collectedCount"
  | "collectAlready"
  | "collectNothingConfirmed"
  /* ORD-0545: the channel badge came over from `t("poiImport.channelLocal")`, which ORD-0533 had to
     fill because this screen's own table did not hold it.  The words are that order's, unchanged. */
  | "channelLocal"
  /* ORD-0545 slice 2: the rest of this screen's sentences came over from the shared
     dictionary, so one file owns them and a refusal sentence cannot be orphaned without the
     build noticing.  The values are moved verbatim; see review/ord0545/move_family_r1.py. */
  | "addToPlan"
  | "addedCount"
  | "adding"
  | "alreadyPresent"
  | "autoDayLabel"
  | "autoDayNote"
  | "autoDayUnknown"
  | "boundary"
  | "boundarySub"
  | "brand"
  | "channelLabel"
  | "channelNote"
  | "channelValueLocal"
  | "charCount"
  | "counts"
  | "dayLabel"
  | "emptyExtracted"
  | "errDayRequired"
  | "errJourneyNotFound"
  | "errNoActivePlan"
  | "errNoResponse"
  | "errPlaceNotVisitable"
  | "errPlaceUnknown"
  | "errUnnamed"
  | "exitsTitle"
  | "favorite"
  | "intent"
  | "kicker"
  | "lede"
  | "loadFailed"
  | "loadingWhere"
  | "modeScreenshot"
  | "modeText"
  | "modesSub"
  | "modesTitle"
  | "needTextHint"
  | "noStatusMeasured"
  | "openPlan"
  | "parseAction"
  | "parsing"
  | "phaseError"
  | "phaseLoading"
  | "phaseSuccess"
  | "placeholder"
  | "readBackFailed"
  | "retry"
  | "scopeHint"
  | "screenshotNotSupported"
  | "selectedCount"
  | "technicalToggle"
  | "title"
  | "unsupportedHosts"
  | "unsupportedLabel"
  | "unsupportedNoFetch"
  | "verifyWarning";

const COPY: Record<PoiImportCopyKey, { zh: string; en: string }> = {
  exitsLead: {
    zh: "三个去处只能选一个：选了哪一个，就只照那一个写。",
    en: "One destination at a time: whichever is chosen is the only one that writes.",
  },
  /* The row that replaces the silent pre-tick. It says what it will do to every selectable row, and
     its own word moves with the state so the control is not a glyph a screen reader alone can read. */
  selectAll: {
    zh: "全选可加的行",
    en: "Select every selectable row",
  },
  selectAllOn: {
    zh: "已全选——点一下取消全部",
    en: "All selected — tap to clear every row",
  },
  exitStoreOnly: {
    zh: "只存不排",
    en: "Keep them without a day",
  },
  /* Measured on this line, not assumed: `POST .../poi-import/add` with an empty day answers 201 with
     `day_source=JOURNEY_FIRST_DAY`, and the only other store this tree has is the shelf, which is the
     exit above. So there is no door to wire this radio to, and the honest form of the third exit is a
     refusal that names why. */
  storeOnlyRefusal: {
    zh: "这台服务器没有“存下来但不给日子”的门：加进计划必须落在某一天，而存进架子就是上面那个出口。这里不另起第四种说法，所以这一项按状态拒绝，什么都不写。",
    en: "This server has no door that keeps a place without giving it a day: 加进计划 must land on a day, and putting it on a shelf is the exit above. No fourth word is invented here, so this one refuses by state and writes nothing.",
  },
  /* The chosen state of the refused exit: the traveller picked it, so the screen owes a sentence about
     what just happened -- which is that nothing happened, and no write control exists any more. */
  storeOnlyChosen: {
    zh: "这一项选上了，但它没有可写的门：上面那句就是它的全部回答，这一屏什么都没写。",
    en: "This destination is chosen and has no door to write through: the line above is its whole answer, and nothing has been written.",
  },
  landingDayChosen: {
    zh: "你选的日子：{day}",
    en: "The day you chose: {day}",
  },
  landingDayFirst: {
    zh: "没有填日子，就落在计划的第一天：{day}",
    en: "No day typed, so this lands on the plan's first day: {day}",
  },
  landingDayNone: {
    zh: "现在还没有日子可落——先选一个日子。",
    en: "There is no day to land on yet — pick one first.",
  },
  collectAction: {
    zh: "存进这个架子",
    en: "Collect these into that shelf",
  },
  collecting: {
    zh: "正在存…",
    en: "Collecting…",
  },
  /* The number here is counted off the re-read, so the sentence cannot outlive the rows the list door
     actually returned. `{shelf}` is the shelf's own name from that read -- never its id. The Chinese
     arms carry no `{plural:}` marker at all, which is ORD-0433's rule: a marker in a zh line would
     paint an English noun on the Chinese glass. */
  collectedCount: {
    zh: "{n} 枚已收在「{shelf}」——这句是从同一扇读门重读回来的。",
    en: "{n} {plural:place} are now on {shelf} — read back from the same list door.",
  },
  collectAlready: {
    zh: "{n} 枚本来就在「{shelf}」上，没有重复写入。",
    en: "{n} {plural:place} were already on {shelf}, so nothing was written twice.",
  },
  collectNothingConfirmed: {
    zh: "写入应答了，但架子上读不回这一枚。在它读回来之前，这里不说“已收在”。",
    en: "The write answered, but the shelf did not give these back. Nothing is called collected until it does.",
  },
  /* The badge next to the screen's title.  It carries no `{...}` value because the screen states it
     the same way whatever it is showing: the parser is local and no link is opened.  Both halves are
     ORD-0533's words, moved rather than reworded -- `git diff` on this order shows the pair as one
     deletion from `LocaleContext.tsx` and one insertion here. */
  channelLocal: {
    zh: "本地解析，不打开链接",
    en: "Local parse, no links opened",
  },
  addToPlan: {
    zh: "加进计划",
    en: "Add to plan",
  },
  addedCount: {
    zh: "{n} 个地点已在 {day} ——这是从服务端读回来的结果。",
    en: "{n} places are on {day} — read back from the server.",
  },
  adding: {
    zh: "正在加…",
    en: "Adding…",
  },
  alreadyPresent: {
    zh: "{n} 个本来就在那一天，没有重复添加。",
    en: "{n} were already on that day, so nothing was added twice.",
  },
  autoDayLabel: {
    zh: "会落到哪一天",
    en: "Day it would land on",
  },
  autoDayNote: {
    zh: "不填日子就会放进这个计划的第一天。这句话现在说，不是做完再说。",
    en: "Leaving the day empty puts these places on the plan's first day. It is said now, not after.",
  },
  autoDayUnknown: {
    zh: "还没有日子——请选一个",
    en: "no day yet — choose one",
  },
  boundary: {
    zh: "不抓取任何内容",
    en: "Nothing is fetched",
  },
  boundarySub: {
    zh: "识别只跑本部署自己的文本规则和我们已有的地点行。",
    en: "Extraction runs on this deployment's own text rules and our own place rows.",
  },
  brand: {
    zh: "CityNexus",
    en: "CityNexus",
  },
  channelLabel: {
    zh: "通道",
    en: "Channel",
  },
  channelNote: {
    zh: "本地解析器；不接第三方读取服务。",
    en: "Local parser; no third-party reading service.",
  },
  channelValueLocal: {
    zh: "本机文字解析",
    en: "Text parsed on this device",
  },
  charCount: {
    zh: "{n} 字",
    en: "{n} characters",
  },
  counts: {
    zh: "读出 {extracted} 行 · 对上 {resolved} 个 · {ambiguous} 个要你选 · {unresolved} 个没找到",
    en: "{extracted} lines read · {resolved} matched · {ambiguous} need your choice · {unresolved} not found",
  },
  dayLabel: {
    zh: "落点日子",
    en: "Landing day",
  },
  emptyExtracted: {
    zh: "这段文字里没有可拆的条目。请一行写一个地点，或者把笔记正文贴进来而不是链接。",
    en: "Nothing separable came out of that text. Add one place per line, or paste the note body instead of a link.",
  },
  errDayRequired: {
    zh: "这个方案还没有自己的日子，请写下要放到哪一天。",
    en: "This plan has no day of its own; type the day you want them on.",
  },
  errJourneyNotFound: {
    zh: "这台服务器上已经没有这趟行程了。请从今日页重新选一次。",
    en: "That journey is not on this server any more. Pick it again from Today.",
  },
  errNoActivePlan: {
    zh: "还没有选定哪个方案是计划，所以没有可以放进去的那一天。",
    en: "Nothing is chosen as the plan yet, so there is no day to add places to.",
  },
  errNoResponse: {
    zh: "服务器这一问没有回答。什么都没改动。",
    en: "The server did not answer this one. Nothing was changed.",
  },
  errPlaceNotVisitable: {
    zh: "那一条是日程节点或属于别的城市，不能当作要去的地点。",
    en: "That entry is a schedule node or belongs to another city, so it cannot be visited.",
  },
  errPlaceUnknown: {
    zh: "其中一个地点已不在我们的清单里，请重新识别一次文字。",
    en: "One of the places is no longer in our list. Recognise the text again.",
  },
  errUnnamed: {
    zh: "服务器拒绝了，但没给出我们认得的理由。什么都没改动。",
    en: "The server refused without naming a reason we know. Nothing was changed.",
  },
  exitsTitle: {
    zh: "去处",
    en: "Where they go",
  },
  favorite: {
    zh: "存到收藏",
    en: "Save to favourites",
  },
  intent: {
    zh: "你的文字",
    en: "Your text",
  },
  kicker: {
    zh: "把你已经写下的东西贴进来",
    en: "Paste what you already wrote",
  },
  lede: {
    zh: "贴一段笔记、一个清单或一条链接。我们只读本地能读的文字，逐行告诉你读成了什么，写回之前由你确认。",
    en: "Paste a note, a list or a link. We read the words we can read locally, show you what each line became, and let you choose before anything is written.",
  },
  loadFailed: {
    zh: "这段文字没读出来。你写的还在原地，可以再试一次。",
    en: "The text could not be read. Your words are still here; try again.",
  },
  loadingWhere: {
    zh: "正在把每一行和这座城市我们已持有的地点对照。",
    en: "Matching each line against this city's places we already hold.",
  },
  modeScreenshot: {
    zh: "截图识别",
    en: "Recognise a screenshot",
  },
  modeText: {
    zh: "文本或链接识别",
    en: "Recognise text or links",
  },
  modesSub: {
    zh: "今天支持文本与链接；截图也列出来，让缺口有名字。",
    en: "Text or links today; screenshots are listed so the gap has a name.",
  },
  modesTitle: {
    zh: "两条入口",
    en: "Two ways in",
  },
  needTextHint: {
    zh: "至少写两个字再识别。",
    en: "Write at least a couple of characters before recognising.",
  },
  noStatusMeasured: {
    zh: "没有状态",
    en: "no status",
  },
  openPlan: {
    zh: "打开计划",
    en: "Open the plan",
  },
  parseAction: {
    zh: "识别",
    en: "Recognise",
  },
  parsing: {
    zh: "正在读…",
    en: "Reading…",
  },
  phaseError: {
    zh: "没能读出来",
    en: "Could not read it",
  },
  phaseLoading: {
    zh: "正在读你的文字",
    en: "Reading your text",
  },
  phaseSuccess: {
    zh: "已读出",
    en: "Read",
  },
  placeholder: {
    zh: "一行一个地点，或者整段笔记；链接可以混在里面。",
    en: "One place per line, or a whole note. Links may be mixed in.",
  },
  readBackFailed: {
    zh: "写入应答了，但计划没有把那条记录交回来。在它交回来之前，这里不说“已加入”。",
    en: "The write answered, but the plan did not give the record back. Nothing is called added until it does.",
  },
  retry: {
    zh: "重试",
    en: "Try again",
  },
  scopeHint: {
    zh: "这里读你粘贴的文字。小红书笔记、公众号内容、飞书文档的链接只会被点名说没有读——这一版不打开别人的页面。",
    en: "This reads the text you paste. Links to Xiaohongshu notes, WeChat Official Account posts or Feishu docs are named back as unread -- this build opens nobody else's page.",
  },
  screenshotNotSupported: {
    zh: "这一版没有截图识别。请改贴文字——这个形态是具名不支持，不是悄悄没有。",
    en: "Screenshot recognition is not in this build. Paste the text instead; the picture form is named, not silently unavailable.",
  },
  selectedCount: {
    zh: "已选 {n} / 可加 {total}",
    en: "{n} of {total} matched places selected",
  },
  technicalToggle: {
    zh: "技术细节",
    en: "Technical details",
  },
  title: {
    zh: "从文本导入地点",
    en: "Import places from text",
  },
  unsupportedHosts: {
    zh: "看到链接但没有打开：{hosts}",
    en: "Links seen but not opened: {hosts}",
  },
  unsupportedLabel: {
    zh: "未读取的形态",
    en: "Forms not read",
  },
  unsupportedNoFetch: {
    zh: "这里的链接一律不抓取；请把笔记正文贴进来。",
    en: "Links are never fetched here; paste the text of the note.",
  },
  verifyWarning: {
    zh: "这些对照是我们对你写的东西的猜测。加进去之前请逐行核对——放错的日子由你删除，不由我们藏起来。",
    en: "These matches are our guess at what you meant. Check each line before adding — a wrong place on a day is yours to remove, not ours to hide.",
  },
};

export function poiImportCopy(
  locale: string,
  key: PoiImportCopyKey,
  params?: Record<string, string | number>,
): string {
  const table = locale.startsWith("zh") ? COPY[key].zh : COPY[key].en;
  /* Two passes, in this order: `{plural:stem}` is grammar and belongs to the shared counting rule
     (ORD-0433), while `{day}` / `{shelf}` / `{n}` are values the caller measured. A placeholder with
     no value stays visible as the placeholder -- the same rule `fillAccess` keeps, so a field the
     screen never read cannot arrive as a finished sentence.

     This line is not decoration: the first candidate generation painted 「没有填日子，就落在计划的第一天：{day}」
     on the glass, and the arm that reads `innerText` is what caught it. */
  const counted = expandPluralMarkers(table, params);
  return counted.replace(/\{(\w+)\}/g, (_match, name: string) => String(params?.[name] ?? `{${name}}`));
}
