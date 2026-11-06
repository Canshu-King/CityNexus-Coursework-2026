/**
 * "AI 帮我规划" — natural-language trip planning entry.
 *
 * Design rules honoured here:
 *  - the traveller types plain language; no internal vocabulary is shown;
 *  - progress shows only what this component can prove.  `POST /agent/commands`
 *    is synchronous and the backend reports no intermediate phase, so the wait is
 *    shown as elapsed seconds and an honest "no stage signal", never as a step that
 *    ticked by on a timer (ORD-0038 §2: a fake progress bar is worse than none,
 *    because it makes people believe the system is working);
 *  - the steps that ARE listed are the ones this file actually awaits -- baseline
 *    read, request, verification re-count, and the fallback re-plan when it really
 *    runs;
 *  - internal action enums (REQUEST_REPLAN, ACCEPT_REPLAN, …) and mission
 *    internals are never rendered;
 *  - a provider failure is shown honestly with a retry, never faked as success
 *    (master mission Phase 10), and a cancellation is neither.
 *
 * ORD-0382: the run itself moved to `aiGenerationRun.ts`, because a run owned by a modal dies with
 * the modal.  This file is now the *select* and *result* stages of the three-stage shape, and it can
 * be closed mid-wait without stopping anything: the two exits the traveller gets are 去看会话 and
 * 继续生成, named the way the reference product names them.
 */

import { useEffect, useRef, useState } from "react";
import {
  catalogueCountSentence,
  readCityCatalogue,
  type CatalogueReadback,
} from "./catalogueReadback";
import { useLocale, useT } from "../../i18n/LocaleContext";
import { type AgentRuntimeStatus } from "../../api/citynexus";
import {
  beginAiRun,
  getRunSnapshot,
  PHASES,
  stopWaiting,
  unwatchRun,
  useAiRun,
  watchRun,
  type Aftermath,
} from "./aiGenerationRun";

/* ORD-0250: this floor used to be a bare `8` inside submit() while the button next to it refused
   silently -- so an empty box looked like a button that did nothing.  Named once, read by the
   refusal, the control's aria-disabled and the reason we now paint on screen. */
const REQUEST_MIN_LENGTH = 8;

/*
 * ORD-0104 ①: an example has to be about *this* trip, or there is no example.
 *
 * What used to sit here were two hardcoded Xiamen itineraries ("我和朋友两个人去厦门…鼓浪屿、环岛路、
 * 沙茶面"), shown as the placeholder and as two chips -- so a traveller on a Tianjin or Beijing trip
 * was handed another city's concrete facts as their own suggestion.  That is the same class of defect
 * as a fabricated progress bar: the screen states something it did not learn.
 *
 * Now the sentences are built from place names the server actually returned for this journey's city,
 * and when it returns fewer than two there is nothing to show -- an absent example is honest, an
 * invented one is not.
 */
const NEUTRAL_PLACEHOLDER = {
  zh: "用你自己的话说：这趟想怎么玩、想吃什么、哪儿一定要去。",
  en: "In your own words: how you want this trip to go, what to eat, what you must not miss.",
};

/* ORD-0250: what the screen says when a press is refused for want of a request.  It names the grey
   example as an example, because that is exactly what makes an empty box look answerable.  No number
   here: the length floor lives in one place in code, and prose that repeats it goes stale. */
const REQUEST_TOO_SHORT = {
  zh: "还差你自己的一句话：上面灰色的例子只是提示，不算你说的需求，所以我们没有东西可以拿去排。",
  en: "We still need it in your own words. The grey example above is a hint, not a request we can plan from.",
};

function examplesFor(
  placeNames: string[],
  zh: boolean,
): Array<{ zh: string; en: string }> {
  /* Ordered by name rather than by the order the read happened to return, so this cannot become a
     second `slice(0, 8)` in disguise (R7): which two places appear in an *example sentence* must not
     be decided by a provider's row order, because that order is not a claim about the trip. */
  const picked = placeNames
    .filter((name) => name.trim().length > 0)
    .sort((a, b) => a.localeCompare(b, zh ? "zh-CN" : "en"))
    .slice(0, 2);
  if (picked.length < 2) return [];
  const [first, second] = picked;
  return [
    {
      zh: `我想去${first}和${second}，节奏轻松一点，再找点当地吃的。`,
      en: `I would like to fit in ${first} and ${second} at an easy pace, with somewhere local to eat.`,
    },
    {
      zh: `${first}排在下午，${second}放在傍晚，别排太满。`,
      en: `Put ${first} in the afternoon and ${second} in the early evening, and leave the rest open.`,
    },
  ];
}

/**
 * The server's own names for "why there is nothing to plan with", and the one next step each of them
 * earns.  Keyed by `error.code` / `error.reason_code` and **never by an English sentence** -- a
 * substring match on provider wording is how a screen ends up telling a traveller to "adjust the
 * budget" when the real cause was an empty catalogue (ORD-0107).  The codes are the ones the backend
 * lanes are putting names on (ORD-0101/0108: `services.py/:902/:932`, `CATALOG_EMPTY` at the
 * compose exit); anything the table does not know is shown as unknown, with the server's own sentence
 * under 技术详情, because inventing a paraphrase of a code we have not read is a lie waiting to happen.
 */
/**
 * ORD-0504: the refusals that are *about the catalogue*.  These are the only ones that earn both a
 * re-read of the server's place count and a door into the paste-import screen, because they are the
 * ones whose sentence can be false by the time a traveller reads it - the store may already hold
 * places, and the run that wrote this status did not look again.
 */
const CATALOGUE_SHAPE_CODES = ["CATALOG_EMPTY", "CATALOG_INSUFFICIENT", "PLACES_NOT_IN_CATALOG"];

const BLOCKED_BY_CODE: Record<
  string,
  { zh: string; en: string; action: "another-city" | "retry" | "none" }
> = {
  CATALOG_EMPTY: {
    zh: "这座城我们还没有任何地点数据，所以不是你描述得不对——是没有东西可排。",
    en: "We hold no place data for this city yet, so nothing about your wording failed — there was nothing to assemble a trip from.",
    action: "another-city",
  },
  CATALOG_INSUFFICIENT: {
    zh: "这座城我们手上的地点太少，配不平一天的行程。",
    en: "We have too few places for this city to balance a day's itinerary.",
    action: "another-city",
  },
  PLACES_NOT_IN_CATALOG: {
    zh: "要排的地点不在我们的目录里，所以排不出行程。",
    en: "The places this was asked to plan are not in our catalogue, so no itinerary could be built.",
    action: "another-city",
  },
  PLACE_COST_UNVERIFIED: {
    zh: "地点有了，但费用我们没能核实，所以没有给出带价格的方案。",
    en: "The places were found but their costs could not be verified, so no priced plan was offered.",
    action: "retry",
  },
  PLACE_OUTSIDE_JOURNEY_WINDOW: {
    zh: "这些地点的开放时间和你这趟的日期对不上。",
    en: "These places' opening hours do not fit the dates of this trip.",
    action: "another-city",
  },
  MOBILITY_DATA_UNAVAILABLE: {
    zh: "这趟的出行数据缺了一段（站与站之间的时间没读到），所以没排出来。",
    en: "Part of this trip's mobility data is missing, so nothing could be scheduled.",
    action: "retry",
  },
};

/**
 * ORD-0280/0342: `FailureNote`, `RawReading` and the envelope readers now sit with the legs that
 * produce them (`aiGenerationRun.ts`), so there is one definition of "who spoke" and the run can be
 * driven from a surface that is not this sheet.
 */

/** Map internal mission status -> traveller language. */
export function statusCopy(
  status: string,
  zh: boolean,
  planCount = 0,
): {
  title: string;
  body: string;
  tone: "ok" | "warn" | "busy" | "neutral";
  action?: "another-city" | "retry";
} {
  const s = (status || "").toUpperCase();
  /* A named block from the server's own envelope.  The code decides the words; nothing here is
     inferred from how a provider phrased its sentence. */
  if (s.startsWith("BLOCKED:")) {
    const code = s.slice("BLOCKED:".length);
    const known = BLOCKED_BY_CODE[code];
    return {
      title: zh ? "这次排不出方案：数据不够，不是你说的不对" : "No plan this time: the data is short, not your wording",
      body: known
        ? zh
          ? `${known.zh}（服务器给的原因编号：${code}）`
          : `${known.en} (server reason code: ${code})`
        : zh
          ? `服务器回的原因编号是 ${code || "（没给编号）"}，这条我们还没有本地文案，所以不替你解释它——原话在"技术详情"里。`
          : `The server's reason code is ${code || "(none given)"} and we have no wording for it, so we quote rather than explain — the sentence is under "Technical detail".`,
      tone: "warn",
      action: known ? known.action === "another-city" ? "another-city" : "retry" : "retry",
    };
  }
  if (s === "COMPLETED" || s === "SUCCEEDED") {
    return {
      title: zh ? `已生成 ${planCount} 个可选方案` : `${planCount} candidate plans are ready`,
      body: zh
        ? "去“今天”对比这几个方案，选一个再继续。"
        : "Compare them on Today, then choose one to continue.",
      tone: "ok",
    };
  }
  if (s === "CANCELLED") {
    return {
      title: zh ? "已取消" : "Cancelled",
      body: zh
        ? "请求已中止，你的行程没有被改动。可以改个说法再来一次。"
        : "The request was aborted. Your trip is untouched — rephrase and try again.",
      tone: "neutral",
    };
  }
  if (s === "CANCELLED_AFTER_SEND") {
    return {
      title: zh ? "已中止后续核对" : "Stopped before verification",
      body: zh
        ? "请求本身已经发出去了，可能已经生成方案。请先刷新确认，不要重复提交。"
        : "The request had already been sent, so it may have produced plans. Refresh and check before submitting again.",
      tone: "warn",
    };
  }
  if (s === "UNKNOWN_KEYLESS_WRITE_DEADLINE") {
    /* ORD-0310 (Q54=①).  Two clauses are mandatory and one word is forbidden: the result of this write
       is UNKNOWN (which is not the statement "it did not happen"), and this one cannot be re-asked with
       the same key because the server never took a key here.  「已取消」/「没有发生」/「再点一次」 are the
       counter-examples this branch exists to refuse. */
    return {
      title: zh ? "这一笔的结果未知（不是没发生）" : "This one's result is unknown (not: it did not happen)",
      body: zh
        ? "这台设备停止等待了，而请求已经发出去，服务器那边可能还在做。这条路线不收幂等键，所以这一笔既没有可回读的凭条，也不能靠同一把键补问。先刷新核对结果，不要重复提交。"
        : "This device stopped waiting, after the request had been sent -- the server may still be working. This route never took an idempotency key, so there is no receipt to read back and no way to re-ask with the same key. Refresh and check the result before submitting again.",
      tone: "warn",
    };
  }
  if (s === "NETWORK") {
    return {
      title: zh ? "连不上服务端" : "The server could not be reached",
      body: zh
        ? "网络或后端没有响应，这与你的需求内容无关。你的行程没有被改动，检查网络后可以重试。"
        : "Neither the network nor the backend answered — this is not about what you typed. Your trip is unchanged; retry once you are connected.",
      tone: "warn",
    };
  }
  if (s === "HTTP_422") {
    return {
      title: zh ? "这个要求被明确拒绝" : "The request was refused",
      body: zh
        ? "服务端说明了原因（见“技术详情”）。你的行程没有被改动，按原因调整条件或换个说法再试。"
        : "The backend gave a reason (see “Technical detail”). Your trip is unchanged — adjust to that reason and retry.",
      tone: "warn",
    };
  }
  if (s.startsWith("HTTP_5")) {
    return {
      title: zh ? "服务端处理失败" : "The server failed to finish",
      body: zh
        ? "这次是服务端出错，不是你的需求有问题。行程未被改动，可稍后重试；反复出现请保留“技术详情”。"
        : "The server errored out — not your request. Your trip is unchanged; retry later and keep the technical detail if it repeats.",
      tone: "warn",
    };
  }
  if (s === "PLANS_WITHOUT_AI") {
    return {
      title: zh
        ? `AI 没有参与这次规划（生成了 ${planCount} 个备选）`
        : `AI did not take part — ${planCount} options were still assembled`,
      body: zh
        ? "这些方案是按已经找到的城市地点重新排的，不代表系统理解了你刚才那段话。想按你的要求排，请重试；原因在“技术详情”里。"
        : "These were assembled from places already found for this city; they do not show that your request was understood. Retry to plan from what you typed — the reason is under “Technical detail”.",
      tone: "warn",
    };
  }
  if (s === "BASELINE_UNAVAILABLE") {
    return {
      title: zh ? "无法确认这次的结果" : "We could not confirm this run",
      body: zh
        ? "无法确认本次是否生成了新方案，请重试或刷新页面。"
        : "We could not confirm whether this run produced new plans. Please retry or refresh.",
      tone: "warn",
    };
  }
  if (s === "NO_PLANS_PRODUCED") {
    return {
      title: zh ? "没有生成可行方案" : "No usable plans were produced",
      body: zh
        ? "服务器说这次没排出可行方案，但没有给出具体原因编号，所以我们不替你猜是预算、措辞还是数据的问题。你的行程没有被改动；原因原文在“技术详情”里，可以再试一次或换一座城市。"
        : "The server said no feasible plan came out but gave no reason code, so we will not guess whether that was your budget, your wording or our data. Your trip is unchanged; its sentence is under “Technical detail”. You can retry or try another city.",
      tone: "warn",
    };
  }
  if (s.includes("SKIPPED_EXTERNAL_DEPENDENCY") || s.includes("EXTERNAL")) {
    return {
      title: zh ? "AI 暂时不可用" : "AI is unavailable right now",
      body: zh
        ? "规划助手这次没能连上。你的行程没有被改动，可以稍后重试。"
        : "The planning assistant could not be reached. Your trip is unchanged; you can retry.",
      tone: "warn",
    };
  }
  if (s === "RUNNING" || s === "PENDING" || s === "QUEUED") {
    return {
      title: zh ? "正在规划…" : "Planning…",
      body: zh ? "正在处理你的需求。" : "Working on your request.",
      tone: "busy",
    };
  }
  return {
    title: zh ? "暂时无法完成规划" : "Could not finish planning",
    body: zh
      ? "这次没能生成方案。你的行程没有被改动，可以重试或换个说法。"
      : "We could not produce plans this time. Your trip is unchanged; retry or rephrase.",
    tone: "warn",
  };
}

/**
 * ORD-0359: the only place this screen turns the server's word into a state.
 *
 * The server now names three different facts (`ai_state`) instead of folding them into one
 * boolean, and the sentences below must not collapse that back: an operator who left the switch
 * off, a store with no credential, and a provider that looked at the request and said no are
 * three different things, and none of them is the traveller's fault.  `deepseek_configured` is
 * still read so a server without the new field keeps answering the way it did.
 */
export type AiPreflightState =
  | "checking"
  | "configured_unverified"
  | "not_configured"
  | "disabled_by_ops"
  | "provider_refused"
  | "unknown";

/** The tone a state is painted with. `styles.css` is held by other lanes today, so the three new
 * states reuse the class the old `unconfigured` state already had rather than adding one. */
export function aiPreflightClass(state: AiPreflightState): string {
  return state === "configured_unverified" ? "ready" : state === "checking" ? "checking" : state === "unknown" ? "unknown" : "unconfigured";
}

/* ORD-0387: the input is the contract type, not an ad-hoc shape.  Naming the field here as
   `unknown` is what let this mapping drift from `contracts/openapi.yaml` unnoticed; now a
   contract rename of `ai_state` is a compile error on this line's parameter, not a silent
   fall-through to "unknown".  The runtime guards stay, because an older server really can
   answer without the field. */
export function aiStateFromServer(body: AgentRuntimeStatus | null): AiPreflightState {
  if (!body) return "unknown";
  switch (typeof body.ai_state === "string" ? body.ai_state : "") {
    case "CONFIGURED_UNVERIFIED":
      return "configured_unverified";
    case "NOT_CONFIGURED":
      return "not_configured";
    case "DISABLED_BY_OPS":
      return "disabled_by_ops";
    case "PROVIDER_REFUSED":
      return "provider_refused";
    default:
      break;
  }
  if (typeof body.deepseek_configured === "boolean") {
    return body.deepseek_configured ? "configured_unverified" : "not_configured";
  }
  return "unknown";
}

/** A dictionary sentence: which key, with which measured numbers plugged in. */
type CopyLine = { key: string; params?: Record<string, string | number> };

/**
 * ORD-0382 ③: what an interruption left behind, composed *only* out of what the read-back answered.
 *
 * Three questions have to be on screen after a stop, and each one is answered by a measured value or
 * not at all: where it stopped (`stoppedAt`, the same step words the waiting list just showed),
 * whether anything produced survived (`landedNewPlanIds`, from a fresh `GET /plans`), and whether the
 * trip moved (`currentBefore` vs `currentAfter`, both read off rows the server sent).  When the
 * confirming read did not answer, the last two are simply absent from this list -- 「行程没有被改动」 is
 * never emitted here as a comfort, because that is the sentence this order exists to earn.
 */
function aftermathLines(aftermath: Aftermath, stepName: string | null): CopyLine[] {
  const lines: CopyLine[] = [];
  if (stepName) lines.push({ key: "agent.aiRunStoppedAt", params: { step: stepName } });
  if (!aftermath.requestSent) {
    lines.push({ key: "agent.aiRunStoppedBeforeSend" });
    return lines;
  }
  if (aftermath.read === "checking") {
    lines.push({ key: "agent.aiRunChecking" });
    return lines;
  }
  if (aftermath.read === "failed") {
    lines.push({ key: "agent.aiRunUnreadable" });
    return lines;
  }
  lines.push(
    aftermath.landedNewPlanIds.length > 0
      ? { key: "agent.aiRunLanded", params: { n: aftermath.landedNewPlanIds.length } }
      : { key: "agent.aiRunNothingLanded" },
  );
  const { currentBefore, currentAfter } = aftermath;
  if (currentBefore && currentAfter) {
    const same = currentBefore.id === currentAfter.id;
    const versioned = currentBefore.versionNo !== null && currentAfter.versionNo !== null;
    lines.push(
      same
        ? versioned
          ? { key: "agent.aiRunTripUnmoved", params: { version: currentAfter.versionNo as number } }
          : { key: "agent.aiRunSamePlan" }
        : versioned
          ? {
              key: "agent.aiRunTripMoved",
              params: {
                before: currentBefore.versionNo as number,
                after: currentAfter.versionNo as number,
              },
            }
          : { key: "agent.aiRunOtherPlan" },
    );
  } else if (!currentBefore && currentAfter) {
    lines.push({
      key: currentAfter.versionNo === null ? "agent.aiRunSamePlan" : "agent.aiRunPlanAppeared",
      params: currentAfter.versionNo === null ? undefined : { version: currentAfter.versionNo },
    });
  } else if (!currentAfter) {
    /* The read answered and named nothing in force: all rows came back SUPERSEDED.  That is its own
       fact, and "your trip is unchanged" is not it. */
    lines.push({ key: "agent.aiRunNoCurrentPlan" });
  }
  return lines;
}

export function PlanWithAiSheet({
  journeyId,
  cityCode,
  onClose,
  onPlanned,
  onChooseAnotherCity,
  onViewChat,
  onImportPlaces,
}: {
  journeyId: string;
  cityCode?: string;
  onClose: () => void;
  /** Where the "换一座城市" next step goes.  Optional because the sheet is also mounted from paths
      that have no city picker to open; without it the refusal still reads in full, it just has no
      button to offer. */
  onChooseAnotherCity?: () => void;
  onPlanned?: () => void;
  /**
   * ORD-0382 exit 1: leave the wait and go read the session.  Optional because a mount with no
   * session surface to go to still has to offer exit 2 -- the wait itself is never a cage.
   */
  onViewChat?: () => void;
  /**
   * ORD-0504: the way out of "this city has no places" that is not "go to another city".  The paste
   * door already exists on this tree (`/api/v1/journeys/{id}/poi-import/*`, mounted at the import
   * route); it simply had no entry from this screen, so the refusal was a dead end for a traveller
   * who could supply the places themselves.
   */
  onImportPlaces?: () => void;
}) {
  const { locale } = useLocale();
  const t = useT();
  const zh = locale.startsWith("zh");
  const run = useAiRun();
  const mine = run.journeyId === journeyId;
  const busy = mine && run.running;
  const phase = busy ? run.phase : null;
  const result = mine ? run.result : null;
  const aftermath = mine ? run.aftermath : null;
  const [text, setText] = useState("");
  /* The destination's own words, from the same read the fallback uses.  Read once when the sheet
     opens; a failure leaves `[]`, which means "no examples", not "use the old ones". */
  const [placeNames, setPlaceNames] = useState<string[]>([]);
  /* Whether the read that decides the suggestions has answered at all.  Without it the area under the
     input means "this city has nothing to suggest" and "we are still looking" at once, and a traveller
     watching suggestions appear a second later has no way to tell which happened. */
  const [namesRead, setNamesRead] = useState(false);
  /* Set when a press was refused because there is nothing to plan from; the reason is painted, not
     announced only to a screen reader.  Cleared by the same gate once the words are long enough. */
  const [requestTooShort, setRequestTooShort] = useState(false);
  const [aiState, setAiState] = useState<AiPreflightState>("checking");
  /* The seconds, derived from the run's own start so leaving and coming back shows a bigger number
     rather than a reset one. */
  const [elapsed, setElapsed] = useState(0);
  /* ORD-0382: pressing the close control while a run is going no longer silently aborts it.  It asks,
     and the two answers are the pair the reference product names: 去看会话 / 继续生成. */
  const [leaveChoice, setLeaveChoice] = useState(false);

  /* ORD-0504: the count under a catalogue-shaped refusal is read from this server on every mount of
     that refusal, never carried over from the run that wrote the status.  Places imported while
     this sheet was closed therefore change the sentence on the next open, and a door that did not
     answer produces "we could not read it" rather than the remembered number or a silence that
     reads as zero. */
  const blockedCode = (result?.status || "").startsWith("BLOCKED:")
    ? (result?.status || "").slice("BLOCKED:".length)
    : "";
  /* `PLACES_HTTP_*` is in as well: that name is written by the run when the catalogue door itself
     refused, which is exactly the case where a traveller would otherwise be left with a number the
     device never read. */
  const catalogueShaped =
    CATALOGUE_SHAPE_CODES.includes(blockedCode) || blockedCode.startsWith("PLACES_HTTP");
  const [catalogue, setCatalogue] = useState<CatalogueReadback | null>(null);
  useEffect(() => {
    if (!catalogueShaped) {
      setCatalogue(null);
      return;
    }
    const controller = new AbortController();
    void readCityCatalogue(cityCode, controller.signal).then((read) => {
      if (!controller.signal.aborted) setCatalogue(read);
    });
    return () => controller.abort();
  }, [catalogueShaped, cityCode, result?.status]);

  useEffect(() => {
    if (!busy) {
      setElapsed(0);
      return;
    }
    const tick = () => setElapsed(Math.max(0, Math.floor((Date.now() - run.startedAt) / 1000)));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [busy, run.startedAt]);

  /* Mounting this sheet *is* watching the run; unmounting is walking away from it.  Neither stops
     anything -- `stopWaiting` is the only control that does, which is the whole point of ORD-0382.
     Scoped to `mine` so opening this journey's sheet cannot mark another journey's unread outcome
     as read. */
  useEffect(() => {
    if (!mine) return;
    watchRun();
    return () => unwatchRun();
  }, [mine]);

  /* The sheet used to call `onPlanned` inline at the end of submit(); the run can now finish while
     this component is not on screen, so the refresh rides the outcome instead.  One call per run. */
  const reportedRef = useRef(0);
  useEffect(() => {
    if (!result || reportedRef.current === run.runId) return;
    reportedRef.current = run.runId;
    if (result.status === "COMPLETED") onPlanned?.();
  }, [result, run.runId, onPlanned]);

  /* ORD-0104 ①: the examples are read from this journey's own city.  A non-2xx or a short list leaves
     the list empty, and empty means "show no examples" -- the fallback is silence, not the old Xiamen
     sentences. */
  useEffect(() => {
    let stale = false;
    const controller = new AbortController();
    void fetch(`/api/v1/places?city_code=${encodeURIComponent(cityCode ?? "")}`, {
      headers: { Accept: "application/json" },
      signal: controller.signal,
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((body) => {
        if (stale) return;
        setNamesRead(true);
        if (!Array.isArray(body)) return;
        setPlaceNames(
          body
            .map((item: { name?: string }) => (typeof item?.name === "string" ? item.name : ""))
            .filter((name: string) => name.trim().length > 0),
        );
      })
      .catch(() => {
        /* aborted or unreachable: nothing to suggest, which is the honest state -- and it is settled,
           not pending. */
        if (!stale) setNamesRead(true);
      });
    return () => {
      stale = true;
      controller.abort();
    };
  }, [cityCode]);

  useEffect(() => {
    let stale = false;
    void fetch("/api/v1/agent/runtime-status", { headers: { Accept: "application/json" } })
      .then((response) => (response.ok ? response.json() : null))
      .then((body) => {
        if (stale) return;
        setAiState(aiStateFromServer(body as Parameters<typeof aiStateFromServer>[0]));
      })
      .catch(() => {
        /* A failed pre-flight is reported as unknown, never as "ready". */
        if (!stale) setAiState("unknown");
      });
    return () => {
      stale = true;
    };
  }, []);

  /* A run that outlived this sheet comes back carrying its own words: the request the traveller typed
     lives on the run, so re-opening mid-flight shows *what* is being waited for, and 「照原话再试一次」
     still has a 原话 behind it after a detach -- which it did not, the first time this was driven on a
     real screen.  Only fills an empty box; it never overwrites what someone is typing now. */
  useEffect(() => {
    if (!mine || !run.command) return;
    setText((current) => (current.trim().length === 0 ? run.command : current));
  }, [mine, run.command]);

  const submit = (override?: string) => {
    const command = (override ?? text).trim();
    if (command.length < REQUEST_MIN_LENGTH) {
      /* ORD-0250: this `return` used to be silent -- no request, no word, nothing to read.  The press
         is still refused (there is genuinely nothing to plan from) but it now leaves a reason on the
         screen, and the placeholder example is not treated as if the traveller had said it. */
      setRequestTooShort(true);
      return;
    }
    setRequestTooShort(false);
    /* Synchronous, ahead of every await: the second click of a same-tick double tap reaches this
       function while the button is still rendered as enabled, so the guard has to read live module
       state, not this render's snapshot.  Checked before the key is minted, so a refused burst never
       gets one (ORD-0253). */
    if (getRunSnapshot().running) return;
    setLeaveChoice(false);
    void beginAiRun({
      journeyId,
      cityCode,
      command,
      idempotencyKey: crypto.randomUUID(),
    });
  };

  /** Exit 1.  The run keeps going; the strip is what carries it from here. */
  const goWatchSession = () => {
    unwatchRun();
    setLeaveChoice(false);
    if (onViewChat) onViewChat();
    else onClose();
  };

  /** Exit 2, from the leave prompt: stay, and keep watching the same run. */
  const keepWaiting = () => {
    setLeaveChoice(false);
    watchRun();
  };

  /** 「停止等待」 -- the only control that aborts, and it stays honest about what it stops. */
  const stopWaitingNow = () => {
    void stopWaiting();
  };

  const leave = () => {
    if (busy) {
      setLeaveChoice(true);
      return;
    }
    onClose();
  };

  /* The prompt is the answer to a press, so it has to be where the eyes already are.  Measured on the
     first 390px run: the sheet's own scroll region grew past it, and the two doors sat below the fold
     -- a confirmation nobody can see is the same dead end as a button that does nothing (ORD-0250). */
  const leaveRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (leaveChoice) leaveRef.current?.scrollIntoView({ block: "nearest" });
  }, [leaveChoice]);

  /* Measured on the live chain at 390x844: with the example chips and the pre-flight line stacked
     above it, the closing card's last sentence fell below the sheet's own scroll fold -- so the one
     line that answers 「行程有没有被改」 was the line a traveller could not see without thinking to
     scroll.  Same treatment as the leave prompt above. */
  const resultRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    /* `nearest` was the first attempt and it does nothing when the element top is already on screen,
       which is exactly the case here -- the card opens with its head visible and its tail under the
       fold.  `end` is what brings the last line up. */
    if (result) resultRef.current?.scrollIntoView({ block: "end" });
    /* `aftermath` is in the dependency list because the card GROWS after it lands: the settle that
       carries CANCELLED_AFTER_SEND happens first with two lines, then the confirming read adds the
       「当前生效的还是第 N 版」 line that was the whole reason for scrolling.  Scrolling once at settle
       is what left that last line under the fold in both earlier attempts. */
  }, [result, aftermath, run.runId]);

  const copy = result ? statusCopy(result.status, zh, result.planCount ?? 0) : null;
  /* Q70, one-way by construction: an anchor may only name a code the traveller can actually read on the
     line it sits on.  Derived from `result.status` -- the very string `statusCopy` was handed -- so the
     attribute and the sentence it labels cannot disagree, and a state that shows no code shows no anchor. */
  const reasonAnchor =
    result?.reasonCode && result.status === `BLOCKED:${result.reasonCode.code}` ? result.reasonCode : null;
  const examples = examplesFor(placeNames, zh);
  const here = phase ? PHASES.findIndex((item) => item.key === phase) : -1;
  const currentPhase = here >= 0 ? PHASES[here] : null;
  const phaseLabel = currentPhase
    ? zh
      ? currentPhase.zh
      : currentPhase.en
    : zh
      ? "正在处理"
      : "Working";
  /* Where the interruption landed, named with the same step words the waiting list just showed --
     one vocabulary, so a traveller can point at the row they were watching. */
  const stoppedLabel = aftermath?.stoppedAt
    ? (() => {
        const row = PHASES.find((item) => item.key === aftermath.stoppedAt);
        return row ? (zh ? row.zh : row.en) : null;
      })()
    : null;

  return (
    <div className="v8-sheet-backdrop" role="dialog" aria-modal="true" data-testid="plan-ai-sheet">
      <section className="plan-ai-sheet">
        <header className="plan-ai-head">
          <div>
            <b>{zh ? "AI 帮我规划" : "Plan my trip with AI"}</b>
            <small>
              {zh
                ? "用你自己的话描述这趟旅行，越具体越好。"
                : "Describe the trip in your own words — the more specific the better."}
            </small>
          </div>
          <button type="button" className="plan-ai-close" aria-label={zh ? "关闭" : "Close"} onClick={leave}>
            ×
          </button>
        </header>

        {/* Everything that may grow lives in one scroll region between two pinned ends. */}
        <div className="plan-ai-scroll" data-testid="plan-ai-scroll">
        <label className="plan-ai-field">
          <span className="v8-sr-only">{zh ? "旅行需求" : "Trip request"}</span>
          <textarea
            data-testid="plan-ai-input"
            rows={5}
            value={text}
            disabled={busy}
            aria-describedby={
              requestTooShort && text.trim().length < REQUEST_MIN_LENGTH ? "plan-ai-request-hint" : undefined
            }
            onChange={(event) => setText(event.target.value)}
            placeholder={
              examples.length
                ? zh
                  ? examples[0].zh
                  : examples[0].en
                : zh
                  ? NEUTRAL_PLACEHOLDER.zh
                  : NEUTRAL_PLACEHOLDER.en
            }
          />
        </label>

        {/* ORD-0250: the example sentence sits in the box as a placeholder, so a traveller who presses
            开始规划 with nothing typed sees a filled-looking field and an inert button.  The refusal
            stays (there is nothing to plan from) but it now says what is missing, in painted text --
            and it says the box is still empty, which is the part the placeholder hides. */}
        {requestTooShort && text.trim().length < REQUEST_MIN_LENGTH && (
          <p
            className="plan-ai-field-hint"
            id="plan-ai-request-hint"
            data-testid="plan-ai-empty-request"
            data-request-length={text.trim().length}
            role="status"
          >
            {zh ? REQUEST_TOO_SHORT.zh : REQUEST_TOO_SHORT.en}
          </p>
        )}

        {/* No chips at all when this city has fewer than two catalogued places -- an absent
            suggestion is honest, an invented one is a claim about a trip we know nothing about. */}
        <div
          className="plan-ai-examples"
          data-testid="plan-ai-examples"
          data-example-count={examples.length}
          data-examples-state={!namesRead ? "reading" : examples.length ? "suggested" : "none"}
        >
          {examples.map((item) => (
            <button
              key={item.en}
              type="button"
              disabled={busy}
              onClick={() => setText(zh ? item.zh : item.en)}
            >
              {/* Measured on the live chain at 320/360/390/414: the label was the first 18 characters
                  plus an ellipsis while the click put the whole sentence in the box, so the chip
                  promised one thing and submitted another -- and no amount of CSS would have made the
                  hidden half readable.  The chip now speaks exactly what it submits. */}
              {zh ? item.zh : item.en}
            </button>
          ))}
        </div>

        {aiState !== "checking" && (
          <p
            className={`plan-ai-preflight ${aiPreflightClass(aiState)}`}
            data-testid="plan-ai-preflight"
            data-ai-state={aiState}
          >
            {aiState === "configured_unverified" &&
              (zh
                ? "这台服务配了一个 AI 凭据。这里只说明\"配了\"，没有验证过它能用；这一句不代表你下一次提交会成功。"
                : "This server has an AI credential configured. That says something is set, not that it works — it is no promise that your next submission succeeds.")}
            {aiState === "not_configured" &&
              (zh
                ? "这台服务没有配置 AI 凭据。这是服务器少了一样东西，与你写了什么、你的预算或你的网络都无关；行程不会被改动。"
                : "This server has no AI credential configured. Something is missing on the server — not in what you wrote, not your budget, not your connection. Your trip is unchanged.")}
            {aiState === "disabled_by_ops" &&
              (zh
                ? "这台服务的 AI 开关处于关闭状态，这是运营侧的设置，不是你的操作结果。开关本身不代表某次失败的原因。"
                : "The AI switch on this server is switched off. That is an operator setting, not something you did. It names the switch, not the cause of any one failure.")}
            {aiState === "provider_refused" &&
              (zh
                ? "模型这一侧答复了请求但拒绝了这次生成；原文在“技术详情”里，逐字是服务器给的。这不是你的措辞问题。"
                : "The model side answered and refused this generation; its own words are under “Technical detail”, verbatim from the server. This is not about how you phrased it.")}
            {aiState === "unknown" &&
              (zh
                ? "读不到 AI 状态（状态接口没有应答）。这只说明我们不知道，不代表它可用或不可用。"
                : "The AI status endpoint did not answer. That means we do not know — not that it works or that it is broken.")}
          </p>
        )}

        {busy && (
          <div
            className="plan-ai-live"
            data-testid="plan-ai-live"
            data-elapsed-seconds={elapsed}
            data-phase={phase ?? ""}
            data-watched={run.watched ? "sheet" : "strip"}
          >
            <p className="plan-ai-live-line" data-testid="plan-ai-elapsed">
              <span className="plan-ai-pulse" aria-hidden="true" />
              <span>
                {phaseLabel} · {zh ? `已等待 ${elapsed} 秒` : `waiting ${elapsed}s`}
              </span>
            </p>
            <p className="plan-ai-live-note">
              {zh
                ? "后端不会中途汇报进度，所以这里只有真实等待时间，没有百分比，也没有猜。"
                : "The backend reports nothing until it finishes, so this is measured waiting time — no percentage, no guessing."}
            </p>
            {/* Whose ask this is, quoted back from the run rather than from the box: the traveller who
                walked away and came back has to be able to tell two rounds apart. */}
            {run.command && (
              <p className="plan-ai-asked" data-testid="plan-ai-asked">
                {t("agent.aiRunAsked", { asked: run.command })}
              </p>
            )}
            {/* ORD-0382 ②: the units a traveller can check by eye, read from the plan row this run
                already fetched, plus the one unit this screen is NOT allowed to show.  The reference
                product reports 正在更新第 N 天; our backend emits no per-day signal at all, so saying
                "this trip is 2 days" and "we cannot tell you which day" is the honest half of that,
                and inventing the counter would be the same lie wearing a better label. */}
            {run.current && (
              <div className="plan-ai-scope" data-testid="plan-ai-scope">
                <p>
                  {t("agent.aiRunScope", {
                    days: run.current.days,
                    activities: run.current.activities,
                  })}
                </p>
                <p>{t("agent.aiRunNoDaySignal")}</p>
              </div>
            )}
            <ol className="plan-ai-steps" data-testid="plan-ai-steps">
              {PHASES.map((item, index) => {
                if (item.key === "fallback" && phase !== "fallback") return null;
                const state = index < here ? "done" : index === here ? "active" : "";
                return (
                  <li
                    key={item.key}
                    className={state}
                    data-phase={item.key}
                    data-phase-state={state || "pending"}
                  >
                    <span className="plan-ai-dot" aria-hidden="true">
                      {state === "done" ? "✓" : state === "active" ? "●" : "○"}
                    </span>
                    <span>{zh ? item.zh : item.en}</span>
                  </li>
                );
              })}
            </ol>
            <div className="plan-ai-wait-exits">
              <button
                type="button"
                className="plan-ai-cancel v8-outline-button"
                data-testid="plan-ai-cancel"
                onClick={stopWaitingNow}
              >
                {t("agent.aiRunStop")}
              </button>
              {/* ORD-0382 ①: leaving is no longer the same act as cancelling.  Until now the only way
                  out of this panel was to kill the request, so "I want to look at something else"
                  cost the whole run. */}
              {onViewChat && !leaveChoice && (
                <button
                  type="button"
                  className="v8-secondary plan-ai-view-chat"
                  data-testid="plan-ai-view-chat"
                  onClick={goWatchSession}
                >
                  {t("agent.aiRunViewChat")}
                </button>
              )}
            </div>
          </div>
        )}

        {/* The named pair, shown when a leave press would otherwise have destroyed a live run.
            Kept in one place so 去看会话 is never on screen twice and cannot be read as two doors. */}
        {busy && leaveChoice && (
          <div
            className="plan-ai-leave-choice"
            ref={leaveRef}
            data-testid="plan-ai-leave-choice"
            role="group"
            aria-label={t("agent.aiRunLeavePrompt")}
          >
            <p data-testid="plan-ai-leave-prompt">{t("agent.aiRunLeavePrompt")}</p>
            <div className="plan-ai-leave-actions">
              {onViewChat && (
                <button
                  type="button"
                  className="v8-outline-button"
                  data-testid="plan-ai-view-chat"
                  onClick={goWatchSession}
                >
                  {t("agent.aiRunViewChat")}
                </button>
              )}
              <button
                type="button"
                className="v8-primary-button"
                data-testid="plan-ai-continue"
                onClick={keepWaiting}
              >
                {t("agent.aiRunContinue")}
              </button>
            </div>
          </div>
        )}

        {copy && (
          <div
            className={`plan-ai-result ${copy.tone}`}
            ref={resultRef}
            data-testid="plan-ai-result"
            data-result-status={result?.status ?? ""}
            data-result-tone={copy.tone}
            /* ORD-0251: `-1` is how this component knows "we could not tell" (indeterminate baseline or
               an unread re-count).  That is a real state; it is not a number of plans, and publishing it
               on an attribute a gate reads made "unknowable" indistinguishable from a count. */
            data-plan-count={
              result?.planCount !== undefined && result.planCount >= 0 ? result.planCount : "unknown"
            }
            data-new-plan-ids={(result?.newPlanIds ?? []).join(",")}
            role="status"
          >
            <b>{copy.title}</b>
            {/* ORD-0342 (Q70): this line names a 原因编号, so the element carrying it says which envelope
                field that number was read out of.  The pair is emitted only when a real code arrived --
                `CATALOG_*` / `PLACES_HTTP_n` are this screen's own readings and get no anchor (无量则不贴),
                and Q68's「没给编号」branch has nothing to name either.  No rendered word is added: the
                attributes carry the code the sentence already shows, so anchor and sentence cannot drift. */}
            <p
              {...(reasonAnchor
                ? {
                    "data-cnx-field": reasonAnchor.field,
                    "data-cnx-value": reasonAnchor.code,
                  }
                : {})}
            >
              {copy.body}
            </p>
            {/* ORD-0382 ②: the result counted in units the traveller can go and check, summed over the
                rows the compose leg actually returned.  Absent, not zero, when this run composed
                nothing -- a 0 here would be read as "empty plans" rather than "no plans from us". */}
            {result?.units && (
              <p className="plan-ai-units" data-testid="plan-ai-units">
                {t("agent.aiRunUnits", {
                  landed: result.units.landed,
                  activities: result.units.activities,
                  days: result.units.days,
                })}
              </p>
            )}
            {/* ORD-0382 ③: the closing state of an interruption.  Every line below is a read-back, and
                nothing is emitted for a question the read-back could not answer. */}
            {aftermath && (
              <div
                className="plan-ai-aftermath"
                data-testid="plan-ai-aftermath"
                data-aftermath-read={aftermath.read}
                data-aftermath-stopped-at={aftermath.stoppedAt ?? ""}
                data-aftermath-landed={aftermath.landedNewPlanIds.length}
                data-aftermath-http-status={aftermath.httpStatus ?? ""}
                {...(aftermath.attribution
                  ? { "data-aftermath-cause": aftermath.attribution.reason }
                  : {})}
              >
                {aftermathLines(aftermath, stoppedLabel).map((line) => (
                  <p key={line.key}>{t(line.key, line.params)}</p>
                ))}
                {/* The shared channel's verdict stays in `data-aftermath-cause` above: an
                    Attribution reason is this app's machine vocabulary, and the design rule at the top
                    of this file forbids putting any of it on the traveller's screen. */}
              </div>
            )}
            {(result?.failureReason || result?.summary || result?.rawReading) && (
              <details className="plan-ai-detail">
                <summary data-testid="plan-ai-detail-toggle">
                  {zh ? "技术详情" : "Technical detail"}
                </summary>
                {result?.failureReason && (
                  <>
                    {/* ORD-0280: the label is chosen by who spoke, not by whichever branch ran.  A
                        server-sent sentence keeps 「服务器给出的原因」; anything this screen composed says
                        so, because an HTTP number we invented is not the backend's statement -- the
                        screen used to print our guess under the attribution label on all three refusal
                        legs.  `data-reason-origin` is here so a gate can read the same fact the
                        traveller reads instead of parsing prose. */}
                    <span
                      className="plan-ai-detail-label"
                      data-testid="plan-ai-detail-kind"
                      data-reason-origin={result.failureReason.from}
                    >
                      {result.failureReason.from === "server"
                        ? zh
                          ? "服务器给出的原因"
                          : "Reason the server gave"
                        : zh
                          ? "这台设备自己的读数（服务器没给句子）"
                          : "This device's own reading (the server sent no sentence)"}
                    </span>
                    {/* ORD-0342 (Q70/Q71): 技术详情 may quote the answer, and the element quoting it now
                        says WHICH envelope field the words are -- `error.message`, `detail` or
                        `mission.failure_reason`.  The anchor carries the name only: a `data-cnx-value`
                        holding the sentence would move upstream words into an attribute, and Q71 counts
                        attributes as the screen.  A line this device composed has no field to name, so it
                        gets no anchor at all rather than a name borrowed from a response. */}
                    <code
                      data-testid="plan-ai-reason"
                      {...(result.failureReason.field
                        ? { "data-cnx-field": result.failureReason.field }
                        : {})}
                    >{result.failureReason.text}</code>
                  </>
                )}
                {result?.rawReading && (
                  <>
                    {/* Q64=① whitelist: only what this device measured -- status, content-type, byte
                        count.  No fragment of the upstream body is forwarded into the DOM, so a
                        screenshot or an evidence file cannot carry a credential that arrived here. */}
                    <span className="plan-ai-detail-label" data-testid="plan-ai-raw-label">
                      {zh ? "这台设备自己量到的回读字段" : "Fields this device measured"}
                    </span>
                    <pre
                      className="plan-ai-raw-reading"
                      data-testid="plan-ai-raw-reading"
                      data-raw-http-status={result.rawReading.http_status}
                      data-raw-content-type={result.rawReading.content_type}
                      data-raw-bytes={result.rawReading.bytes ?? ""}
                    >
                      {`http_status=${result.rawReading.http_status}\n`}
                      {`content-type=${result.rawReading.content_type}\n`}
                      {`bytes=${result.rawReading.bytes ?? ""}\n`}
                    </pre>
                  </>
                )}
                {result?.summary && (
                  <>
                    {/* ORD-0240: this is the model's own sentence, not this screen's wording.  It is
                        neither translated nor added to a dictionary -- dynamic prose put through a
                        static table would be claimed as "already translated", which is the hole this
                        order closes.  Unlabelled in the main flow, a Chinese traveller read it as our
                        promise. */}
                    <span
                      className="plan-ai-detail-label"
                      data-testid="plan-ai-model-label"
                      data-summary-origin={result?.summaryOrigin ?? "not_stated"}
                    >
                      {(result?.summaryOrigin ?? "not_stated") === "no_model"
                        ? zh
                          ? "服务器给这句总结（这台部署的 AI 开关是关的，所以它不是模型写的）"
                          : "The server's own summary (the AI switch on this deployment is off, so no model wrote this)"
                        : zh
                          ? "模型原始输出（未翻译，不代表本界面的结论）"
                          : "Model's own words (not translated, not this screen's conclusion)"}
                    </span>
                    <code data-testid="plan-ai-model-output">{result.summary}</code>
                  </>
                )}
              </details>
            )}
            {/* A refusal the traveller cannot act on is a dead end, so the reason carries exactly one
                next step -- and it is the step the server's own code earns, not a generic "try again". */}
            {catalogueShaped && (
              <p data-testid="plan-ai-catalogue-count" className="plan-ai-catalogue-count">
                {catalogueCountSentence(catalogue, zh)}
              </p>
            )}
            {catalogueShaped && onImportPlaces && (
              <button
                type="button"
                /* The product's own secondary style, deliberately: ORD-0375's narrow-screen gate
                   counts strikeable `.v8-primary-button` and this refusal already carries one
                   (换一座有数据的城市). A second primary here would be a quiet-row violation, not a
                   help. */
                className="v8-outline-button plan-ai-import-entry"
                data-testid="plan-ai-import-entry"
                data-next-step="import-places"
                onClick={() => {
                  onClose();
                  onImportPlaces();
                }}
              >
                {zh ? "粘贴你自己的地点，导入这趟行程" : "Paste your own places and import them into this trip"}
              </button>
            )}
            {copy?.action === "another-city" && onChooseAnotherCity && (
              <button
                type="button"
                className="v8-primary-button plan-ai-next-step"
                data-testid="plan-ai-next-step"
                data-next-step="another-city"
                onClick={() => {
                  onClose();
                  onChooseAnotherCity();
                }}
              >
                {zh ? "换一座有数据的城市" : "Try a city we have data for"}
              </button>
            )}
            {copy?.action === "retry" && (
              <button
                type="button"
                className="v8-primary-button plan-ai-next-step"
                data-testid="plan-ai-next-step"
                data-next-step="retry"
                /* The 原话 comes off the run, not off this mount: after a detach the box was empty and
                   this button pressed into nothing. */
                onClick={() => submit(run.command || text)}
              >
                {zh ? "照原话再试一次" : "Try again with the same words"}
              </button>
            )}
          </div>
        )}

        </div>

        <div className="plan-ai-actions">
          <button
            type="button"
            className="v8-outline-button"
            data-testid="plan-ai-dismiss"
            onClick={leave}
          >
            {/* This control used to say 「取消并关闭」 while a run was going, and did exactly that: one
                press destroyed the run and the panel together.  It now only leaves, and says so. */}
            {busy ? t("agent.aiRunLeaveNow") : t("agent.aiRunLater")}
          </button>
          <button
            type="button"
            className="v8-primary-button"
            data-testid="plan-ai-submit"
            /* Busy is a real `disabled` (the control means something else while a run is going).  A
               missing request is `aria-disabled` instead -- the ORD-0066 (甲) shape -- so the press
               reaches submit() and the traveller reads why it did not advance, instead of tapping a
               dead surface.  The existing `[aria-disabled="true"]` rule keeps it looking unwalkable. */
            disabled={busy}
            aria-disabled={text.trim().length < REQUEST_MIN_LENGTH || undefined}
            onClick={() => submit()}
          >
            {busy ? (zh ? "规划中…" : "Planning…") : zh ? "开始规划" : "Plan my trip"}
          </button>
        </div>
      </section>
    </div>
  );
}
