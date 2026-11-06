/**
 * R14 / ORD-0169 -- one classification, four consumers.
 *
 * The defect this exists to prevent was measured on the production bundle at 390x844:
 * `localStorage["citynexus.v8.journey_id"]` held `ca0f1ed1-…`, a journey the server does not have,
 * so `GET /journeys/ca0f1ed1-…/experience` answered **404** -- and the screen said
 * 「网络不可用 · 正在显示只读的已保存行程」.  The banner, the read-only fallback and the missing map
 * were all downstream of one misread: an HTTP response was treated as a transport failure.
 *
 * R14's rule is that the classification happens **once**, at the boundary where the discriminating
 * evidence is available, and is then propagated.  So this module owns the only mapping from evidence
 * to reason, and the screen copy, the `aria-live` announcement, the telemetry record and the
 * recovery action all read the same object.  Nothing downstream may invent a cause.
 *
 * Evidence precedence (R14 §3), strongest first:
 *   1. the server's own structured code on a response  (HTTP status + body);
 *   2. the HTTP status of that request;
 *   3. a transport exception/timeout for that request;
 *   4. browser heuristics -- and only when nothing above exists.
 * `navigator.onLine` is deliberately **not** consulted here: it is true on a captive portal and true
 * while a backend is dead, so it cannot distinguish anything this module is asked to distinguish.
 */

import { isReadDeadlineMissed, READ_DEADLINE_SECONDS } from "./readDeadline";

export type AttributionReason =
  | "JOURNEY_NOT_FOUND"
  | "NETWORK_UNAVAILABLE"
  | "SERVER_ERROR"
  | "UNAUTHORIZED"
  /* ORD-0126 补项: 403 is not 401.  A 403 means the server knows who is asking and still refuses
     *this step* -- telling that traveller to sign in again promises a fix that cannot arrive, which
     is the same "rewrite a named HTTP answer into a different cause" defect this module exists to
     stop. */
  | "FORBIDDEN"
  /* ORD-0263 fifth state: the origin answered 2xx and what came back was not this screen's data --
   * an HTML document, a plain sentence, zero bytes.  Neither "the network is down" nor "nobody
   * answered" describes it, because an answer arrived and was discarded. */
  | "ORIGIN_INVALID_RESPONSE"
  /* ORD-0457: the two answers that were being filed as "unknown" and then told to retry.  A 429 is
     not a failure to read, it is the server asking for a slower pace, and a 409 on a state column is
     the work having already happened.  Both said "原因还没有分类" before this, which is a screen
     admitting it cannot read an answer it did receive. */
  | "RATE_LIMITED"
  | "STATE_ALREADY_MOVED"
  | "UNKNOWN_FAILURE";

export type EvidenceKind = "HTTP_RESPONSE" | "TRANSPORT" | "HEALTH_PROBE";

export type SourceLayer = "RESOURCE_STATE" | "TRANSPORT" | "RUNTIME";

/** What the traveller can actually do about it.  One action per cause -- R14 §5. */
export type RecoveryAction =
  | "choose-journey"
  | "retry"
  | "read-only-cached"
  | "sign-in"
  /* ORD-0457: one action per cause (R14 §5). "Later" is not "again now", and looking at what a
     finished state is is not retrying the act that produced it. */
  | "retry-later"
  | "view-current-state";

export type Attribution = {
  reason: AttributionReason;
  /** Which evidence produced `reason`.  Telemetry must record this, not just the conclusion. */
  evidence_kind: EvidenceKind;
  http_status: number | null;
  source_layer: SourceLayer;
  retryable: boolean;
  recovery_action: RecoveryAction;
  /** Stable key for the copy table, so a message is looked up rather than written at a catch site. */
  message_key: string;
  /** The server's own code, when it gave one.  A refusal must be quotable, not paraphrased. */
  server_code?: string | null;
  /** Measured, not assumed: did this device actually hold a copy this read could fall back to? */
  cached_copy_present?: boolean;
  /** What the origin actually sent, for the state where the answer's media type is the whole story. */
  content_type?: string | null;
  /** ORD-0457: the wait a 429 carried, in the server's own milliseconds. Absent means it never said. */
  retry_after_ms?: number | null;
  /** Short, greppable line for production logs.  Shape follows R14 §8. */
  telemetry: string;
};

/** The server's structured error body, as `v8Request` reads it. */
export type ServerErrorShape = {
  status?: number;
  code?: string | null;
  message?: string | null;
  /** The wait the answer carried, in milliseconds. Absent means it was never sent, not zero. */
  retry_after_ms?: number | null;
};

/** The server's own wait, or null. Never a number this screen picked. */
function retryAfterMs(error: unknown, server?: ServerErrorShape | null): number | null {
  const candidates = [
    server?.retry_after_ms,
    error instanceof Error && "retry_after_ms" in error
      ? (error as { retry_after_ms?: unknown }).retry_after_ms
      : null,
  ];
  for (const value of candidates) {
    const n = typeof value === "string" ? Number(value) : value;
    if (typeof n === "number" && Number.isFinite(n) && n > 0) return Math.round(n);
  }
  return null;
}

/**
 * ORD-0263 fifth state: the response arrived and was 2xx, but its body is not this screen's data.
 *
 * Before this existed, both request channels swallowed that fact in different ways -- `v8Request`
 * returned `null` (so a later property read threw a TypeError and the failure was filed under
 * "nothing came back"), `requestJson` returned the document as a string (so nothing threw at all) --
 * and the one discriminating fact, `the origin answered HTTP 200`, was lost either way.  This error
 * carries it, so no branch downstream may rename it a transport failure.
 */
export class OriginResponseUnusableError extends Error {
  readonly status: number;
  readonly content_type: string | null;
  readonly body_kind: "html" | "not_json" | "empty";
  constructor(status: number, contentType: string | null, bodyKind: "html" | "not_json" | "empty") {
    super(`origin answered ${status} with an unusable body (${contentType ?? "content-type unknown"})`);
    this.name = "OriginResponseUnusableError";
    this.status = status;
    this.content_type = contentType;
    this.body_kind = bodyKind;
  }
}

/** Matched by name, the same way the read deadline is, so a bundling pass cannot break the fact. */
export function isOriginResponseUnusable(error: unknown): error is OriginResponseUnusableError {
  return error instanceof Error && error.name === "OriginResponseUnusableError";
}

const NOT_FOUND_CODES = ["NOT_FOUND", "RESOURCE_NOT_FOUND", "JOURNEY_NOT_FOUND"];

function make(
  reason: AttributionReason,
  evidence_kind: EvidenceKind,
  http_status: number | null,
  source_layer: SourceLayer,
  retryable: boolean,
  recovery_action: RecoveryAction,
  message_key: string,
  server_code?: string | null,
  content_type?: string | null,
): Attribution {
  return {
    reason,
    evidence_kind,
    http_status,
    source_layer,
    retryable,
    recovery_action,
    message_key,
    ...(server_code ? { server_code } : {}),
    ...(content_type ? { content_type } : {}),
    telemetry: [
      `attribution_reason=${reason}`,
      `evidence_kind=${evidence_kind}`,
      `http_status=${http_status ?? "none"}`,
      `source_layer=${source_layer}`,
      `retryable=${retryable ? "true" : "false"}`,
      `recovery_action=${recovery_action}`,
      ...(server_code ? [`server_code=${server_code}`] : []),
      /* Emitted last on purpose: every existing column keeps its position in the line, and a reader
         that regexes for `key=` cannot be disturbed by a value that legitimately contains a space. */
      ...(content_type ? [`content_type=${content_type}`] : []),
    ].join(" "),
  };
}

/**
 * Classify a failed journey read.  The order of the branches *is* the evidence precedence: a
 * response that arrived outranks any transport guess, because a response is proof the request
 * reached the server.
 */
export function classifyReadFailure(error: unknown, server?: ServerErrorShape | null): Attribution {
  const status = server?.status ?? (error instanceof Error && "status" in error ? Number((error as { status?: number }).status) : null);
  const code = (server?.code ?? (error instanceof Error && "code" in error ? (error as { code?: string | null }).code : null)) ?? null;

  /* ORD-0263 fifth state, decided *first*: this error carries a 2xx status on purpose, and letting the
     status branch below see it would file "answered, body unusable" as an unnamed 200 failure.  It also
     has to precede the TypeError branch, because that is what a discarded 200 body turns into two
     frames later -- and `error instanceof TypeError` is the one place allowed to say 网络不可用. */
  if (isOriginResponseUnusable(error)) {
    return make("ORIGIN_INVALID_RESPONSE", "HTTP_RESPONSE", error.status, "RUNTIME", true,
      "retry", "failure.originInvalidResponse", null, error.content_type ?? `empty ${error.body_kind} body`);
  }

  /* 1 + 2: the server answered, so this is a resource/state fact, never a transport one. */
  if (typeof status === "number" && status > 0) {
    if (status === 404 || status === 410 || (code !== null && NOT_FOUND_CODES.includes(code))) {
      return make(
        "JOURNEY_NOT_FOUND",
        "HTTP_RESPONSE",
        status,
        "RESOURCE_STATE",
        false,
        "choose-journey",
        "failure.journeyNotFound",
      );
    }
    if (status === 401) {
      return make("UNAUTHORIZED", "HTTP_RESPONSE", status, "RESOURCE_STATE", false, "sign-in", "failure.unauthorized", code);
    }
    if (status === 403) {
      return make("FORBIDDEN", "HTTP_RESPONSE", status, "RESOURCE_STATE", false, "choose-journey", "failure.forbidden", code);
    }
    if (status === 429) {
      /* The wait is the server's own number or nothing at all: this screen has never been allowed to
         invent a delay, and a retry offered without one still says what was measured. */
      const wait = retryAfterMs(error, server);
      return {
        ...make(
          "RATE_LIMITED",
          "HTTP_RESPONSE",
          status,
          "RUNTIME",
          true,
          wait === null ? "retry" : "retry-later",
          wait === null ? "failure.rateLimitedNoWait" : "failure.rateLimited",
          code,
        ),
        retry_after_ms: wait,
      };
    }
    if (status === 409) {
      return make(
        "STATE_ALREADY_MOVED",
        "HTTP_RESPONSE",
        status,
        "RESOURCE_STATE",
        false,
        "view-current-state",
        "failure.stateAlreadyMoved",
        code,
      );
    }
    if (status >= 500) {
      return make("SERVER_ERROR", "HTTP_RESPONSE", status, "RUNTIME", true, "retry", "failure.serverError");
    }
    return make("UNKNOWN_FAILURE", "HTTP_RESPONSE", status, "RESOURCE_STATE", false, "retry", "failure.unknown");
  }

  /* 3a: our own per-read deadline expired (ORD-0263).  Same *fact class* as a transport failure --
     nothing ever came back -- but not the same sentence: a request that was abandoned at a deadline
     may well have travelled and been swallowed, so the copy says "no answer arrived", which is what
     was actually measured, and leaves "the network is down", which was not. */
  if (isReadDeadlineMissed(error)) {
    return make("NETWORK_UNAVAILABLE", "TRANSPORT", null, "TRANSPORT", true, "read-only-cached",
      "failure.noAnswer");
  }

  /* 3b: no response at all -- the request itself failed to travel.  This is the *only* branch that
     may say the network is unavailable, which is what keeps the reverse direction honest. */
  if (error instanceof TypeError) {
    return make("NETWORK_UNAVAILABLE", "TRANSPORT", null, "TRANSPORT", true, "read-only-cached", "failure.networkUnavailable");
  }

  /* 4: nothing discriminating.  Say so instead of borrowing a cause. */
  return make("UNKNOWN_FAILURE", "TRANSPORT", null, "TRANSPORT", true, "retry", "failure.unknown");
}

/**
 * What a same-origin probe actually observed.  Four separate facts, because the old shape -- a boolean
 * that meant "was it a 2xx?" -- collapsed "the origin answered 500" with "the origin never answered",
 * and one sentence then covered both states (ORD-0263's V1/V3).
 */
export type OriginProbeFact = {
  answered: boolean;
  status: number | null;
  ok: boolean;
  timed_out: boolean;
};

/**
 * The health probe's verdict folded into the same vocabulary, for the case where the journey read
 * failed and the question is whether the origin answers at all.  A 2xx probe means the network is
 * fine and the failure was something else -- the reading that proves a 网络不可用 banner is over-broad.
 * An answer of any status still means *something answered*, so it may never be called a network
 * failure either: that is the merge this replaces.
 */
export function classifyHealthProbe(fact: OriginProbeFact): Attribution {
  if (fact.ok) {
    return make("UNKNOWN_FAILURE", "HEALTH_PROBE", fact.status, "TRANSPORT", true, "retry",
      "failure.unknown");
  }
  if (fact.answered) {
    return make("SERVER_ERROR", "HEALTH_PROBE", fact.status, "RUNTIME", true, "retry",
      "failure.serverError");
  }
  return make("NETWORK_UNAVAILABLE", "HEALTH_PROBE", null, "TRANSPORT", true, "read-only-cached",
    fact.timed_out ? "failure.noAnswer" : "failure.networkUnavailable");
}

/**
 * Fold in the one fact the read itself cannot know: whether this device actually holds a copy.  It is
 * still the same classification object -- nothing downstream picks its own cause -- and it only ever
 * changes the *offer*, for the two classes where a local copy is a legitimate answer.
 */
export function withCachedCopyFact(attribution: Attribution, hasCachedCopy: boolean): Attribution {
  const mayOffer = hasCachedCopy
    && (attribution.reason === "SERVER_ERROR" || attribution.reason === "NETWORK_UNAVAILABLE"
        || attribution.reason === "ORIGIN_INVALID_RESPONSE");
  return {
    ...attribution,
    recovery_action: mayOffer ? "read-only-cached" : attribution.recovery_action,
    cached_copy_present: hasCachedCopy,
    telemetry: `${attribution.telemetry} cached_copy_present=${hasCachedCopy}`,
  };
}

/** What this device did not measure. Not `0`, not an empty cell, not a prose "unknown":
    a reader has to be able to tell "no reading exists" from "the reading is zero". */
export const NOT_MEASURED = "NOT_MEASURED";

/* The exposure whitelist, spelled exactly as the contract spells it. Nothing outside this
   list may be produced here -- a new field reaching a "technical details" surface is a
   exposure-gate question, not a convenience. */
export type ReadoutField =
  | "http_status"
  | "content_type"
  | "body_bytes"
  | "response_received"
  | "elapsed_ms"
  | "request_id";

const READOUT_ORDER: ReadoutField[] = [
  "http_status", "content_type", "body_bytes", "response_received", "elapsed_ms", "request_id",
];

/**
 * The one place this family of numbers is spelled out. Call sites name which of their own
 * readings they hold (`only`) and pass the measured values; they never write the
 * `key=value` text themselves, so the key names, the rounding and the not-measured word
 * cannot drift screen by screen.
 */
export function deviceReadout(
  fields: Partial<Record<ReadoutField, string | number | boolean | null | undefined>>,
  only?: ReadoutField[],
): string {
  return (only ?? READOUT_ORDER).map((field) => {
    const value = fields[field];
    if (value === null || value === undefined || value === "") return `${field}=${NOT_MEASURED}`;
    const shown = typeof value === "number" ? Math.round(value) : value;
    return `${field}=${shown}`;
  }).join(" ");
}

/**
 * Telemetry sink.  R14 §8: the record must say what the classifier decided and on what evidence, so
 * a false cause is discoverable in production rather than only in a screenshot.  There is no
 * analytics backend in this build, so the record is kept where a test (and a future reporter) can
 * read it -- emitting `network_unavailable` for an HTTP 404 is exactly the line this prevents.
 */
export type AttributionRecord = Attribution & { at: string; context: string };

const TELEMETRY_LIMIT = 50;
const telemetry: AttributionRecord[] = [];

declare global {
  interface Window {
    citynexusAttribution?: AttributionRecord[];
  }
}

export function recordAttribution(attribution: Attribution, context: string): AttributionRecord {
  const record: AttributionRecord = { ...attribution, at: new Date().toISOString(), context };
  telemetry.push(record);
  if (telemetry.length > TELEMETRY_LIMIT) telemetry.shift();
  if (typeof window !== "undefined") {
    window.citynexusAttribution = [...telemetry];
    window.dispatchEvent(new CustomEvent("citynexus:attribution", { detail: record }));
  }
  return record;
}

export function readAttribution(): AttributionRecord[] {
  return [...telemetry];
}

/**
 * The stored references that point at "the journey this device is on".  R14-S requires the invalid
 * one to be cleared or quarantined once a stale-journey classification has been made, and this is
 * the one place that knows which keys those are -- a `removeItem` per catch site is how one of them
 * gets forgotten and the refresh walks into the same 404 again.
 */
export const JOURNEY_REFERENCE_KEYS = [
  "citynexus.v8.journey_id",
  "citynexus.demo.journey_id",
  "citynexus.demo.agent_run_id",
] as const;

/** Returns the keys it actually removed, so a gate can assert on the clearing rather than assume it. */
export function clearJourneyReferences(): string[] {
  const cleared: string[] = [];
  for (const key of JOURNEY_REFERENCE_KEYS) {
    try {
      if (localStorage.getItem(key) !== null) {
        localStorage.removeItem(key);
        cleared.push(key);
      }
    } catch {
      /* Blocked storage: the reference is not persisted anyway, so there is nothing to clear. */
    }
  }
  return cleared;
}

/**
 * The four consumers, derived from the one classification.  Keeping them in this file is the point:
 * screen copy, announcement, recovery action and telemetry cannot drift apart if they are functions
 * of the same object.
 */
export function copyFor(
  attribution: Attribution,
  zh: boolean,
  detail: { journeyId?: string; hasCachedCopy?: boolean } = {},
): {
  kicker: string;
  title: string;
  body: string;
} {
  const id = detail.journeyId ? `${detail.journeyId.slice(0, 8)}…` : "";
  switch (attribution.message_key) {
    case "failure.journeyNotFound":
      return {
        kicker: zh ? "连接正常" : "Connection is fine",
        title: zh ? "这台服务器上没有这一趟行程了" : "This trip is not on this server",
        body: zh
          ? `这台设备还记得行程 ${id}，但服务器明确回答它不存在（http_status=${attribution.http_status}）。这不是网络问题：服务器刚才答得出来。你的其它行程没有受影响。`
          : `This device remembered trip ${id}, and the server answered plainly that it does not exist (http_status=${attribution.http_status}). This is not a network problem: the server just answered. Your other trips are untouched.`,
      };
    case "failure.networkUnavailable":
      return {
        kicker: zh ? "连接" : "Connection",
        title: zh ? "网络不可用" : "Network unavailable",
        body: detail.hasCachedCopy
          ? zh
            ? "这一发请求没能出去，也没有收到任何回答，所以这是网络问题。你的行程数据没有丢失，已保存的只读副本还能看。请检查网络后重试。"
            : "This request could not travel and nothing came back, so this is a network problem. Your trip data has not been lost and the saved read-only copy is still available. Check your connection and try again."
          : zh
            ? "这一发请求没能出去，也没有收到任何回答，所以这是网络问题。这台设备上还没有可看的离线副本，所以现在没有可显示的内容。请检查网络后重试。"
            : "This request could not travel and nothing came back, so this is a network problem. This device holds no offline copy to show you, so there is nothing to display right now. Check your connection and try again.",
      };
    case "failure.serverError":
      return detail.hasCachedCopy
        ? {
            kicker: zh ? "服务器" : "Server",
            title: zh ? "服务暂时不可用" : "The service is temporarily unavailable",
            body: zh
              ? `服务器刚才答了你，但它自己报错了（http_status=${attribution.http_status}）。这是服务暂时不可用，不是你填错了什么。这台设备上还留着你已保存的行程，用下面的按钮可以只读查看这份本地快照；也可以稍后重试。`
              : `The server did answer, and answered with an error of its own (http_status=${attribution.http_status}). The service is unavailable for now — nothing you typed caused it. This device still holds your saved trip; the button below opens that read-only snapshot. Retrying is safe.`,
          }
        : {
            kicker: zh ? "服务器" : "Server",
            title: zh ? "服务器这次没能把行程读出来" : "The server could not read your trip",
            body: zh
              ? `服务器自己报错了（http_status=${attribution.http_status}）。这是它那边的问题，不是你填错了什么。可以重试；如果一直这样，稍后再来。`
              : `The server reported an error of its own (http_status=${attribution.http_status}). That is on its side, not something you typed. Retry; if it persists, come back later.`,
          };
    /* ORD-0263 fifth state: an answer arrived, so the sentence may not claim one did not.  The status
       and media type quoted here are the ones this response actually carried. */
    case "failure.originInvalidResponse":
      return detail.hasCachedCopy
        ? {
            kicker: zh ? "服务器" : "The origin",
            title: zh
              ? `服务器答了 http_status=${attribution.http_status ?? "2xx"}，可带回来的不是这一屏能用的数据`
              : `The origin answered http_status=${attribution.http_status ?? "2xx"}, but not with data this screen can use`,
            body: zh
              ? `它确实回了话（http_status=${attribution.http_status ?? "2xx"}，content_type=${attribution.content_type ?? "未知"}），只是那不是一个可解析的行程响应。这属于服务暂时不可用的一种，不是你填错了什么。这台设备上还留着你已保存的行程，用下面的按钮可以只读查看这份本地快照；也可以稍后重试。`
              : `It did reply (http_status=${attribution.http_status ?? "2xx"}, content_type=${attribution.content_type ?? "unknown"}) — the answer simply was not a journey response that could be parsed. The service is unavailable for now; nothing you typed caused it. This device still holds your saved trip, and the button below opens that read-only snapshot. Retrying is safe.`,
          }
        : {
            kicker: zh ? "服务器" : "The origin",
            title: zh
              ? `服务器答了 http_status=${attribution.http_status ?? "2xx"}，可带回来的不是这一屏能用的数据`
              : `The origin answered http_status=${attribution.http_status ?? "2xx"}, but not with data this screen can use`,
            body: zh
              ? `它确实回了话（http_status=${attribution.http_status ?? "2xx"}，content_type=${attribution.content_type ?? "未知"}），只是那不是一个可解析的行程响应。这属于服务暂时不可用的一种，不是你填错了什么。这台设备上还没有可只读查看的本地副本，所以现在没有内容可显示；可以稍后重试。`
              : `It did reply (http_status=${attribution.http_status ?? "2xx"}, content_type=${attribution.content_type ?? "unknown"}) — the answer simply was not a journey response that could be parsed. The service is unavailable for now; nothing you typed caused it. This device holds no local copy to show either, so there is nothing to display right now. Retrying is safe.`,
          };
    /* ORD-0457: a refusal to be asked again so soon is not a failed read, and the wait is only ever
       the server's own. The retry control stays on screen either way - "later" is a pace, not a
       refusal to act. */
    case "failure.rateLimited":
      return {
        kicker: zh ? "问得太快" : "Asked too quickly",
        title: zh ? "服务器请我们慢一点" : "The server asked us to slow down",
        body: zh
          ? `这一发被限流了（http_status=${attribution.http_status}）。服务器给了等待时间：约 ${Math.round(
              (attribution as { retry_after_ms?: number }).retry_after_ms ?? 0,
            )} 毫秒后再问。数据没有丢，下面那个按钮一直在，等一会儿再按它就行。`
          : `This request was rate-limited (http_status=${attribution.http_status}). The server named its own wait: ask again in about ${Math.round(
              (attribution as { retry_after_ms?: number }).retry_after_ms ?? 0,
            )} milliseconds. Nothing was lost; the button below stays, so wait a moment and use it.`,
      };
    case "failure.rateLimitedNoWait":
      return {
        kicker: zh ? "问得太快" : "Asked too quickly",
        title: zh ? "服务器请我们慢一点" : "The server asked us to slow down",
        body: zh
          ? `这一发被限流了（http_status=${attribution.http_status}），这一次它没有说等多久。数据没有丢，稍后再按下面那个按钮。`
          : `This request was rate-limited (http_status=${attribution.http_status}) and no wait came with it this time. Nothing was lost - press the button below again shortly.`,
      };
    case "failure.stateAlreadyMoved":
      return {
        kicker: zh ? "已经办过了" : "Already done",
        title: zh ? "这件事的状态已经变了，不是没读到" : "The state already moved - this was not a failed read",
        body: zh
          ? `服务器接了这发，回答的是「现在已经是这样了」（http_status=${attribution.http_status}）。所以这句不是失败：再按一次只会重新读一遍现在的样子。它给的名字在下面的归因详情里。`
          : `The server took this request and answered that it is already so (http_status=${attribution.http_status}). That is not a failure: asking again just re-reads the current state. Its own name for the condition is in the attribution detail below.`,
      };
    /* ORD-0263 V2: a read that hit its own deadline.  The measured fact is "no answer arrived";
       whether the request travelled is not something this screen knows, so the sentence does not
       claim it.  The deadline's own value comes from the mechanism, not from a second number. */
    case "failure.noAnswer":
      return detail.hasCachedCopy
        ? {
            kicker: zh ? "没有回音" : "No reply",
            title: zh ? `等了 ${READ_DEADLINE_SECONDS} 秒，这台服务器一个字也没回答` : `No answer from this server in ${READ_DEADLINE_SECONDS} seconds`,
            body: zh
              ? `这一发读取到了期限仍然没有任何回答：服务器既没有拒绝，也没有答应，它只是没有说话。你的行程数据没有丢，这台设备上有已保存的行程，用下面的按钮可以只读查看这份本地快照；也可以再试一次。`
              : `This read reached its deadline with no answer at all: the server neither refused nor agreed, it simply said nothing. Your trip has not been lost — this device holds your saved trip, and the button below opens that read-only snapshot. Retrying is safe.`,
          }
        : {
            kicker: zh ? "没有回音" : "No reply",
            title: zh ? `等了 ${READ_DEADLINE_SECONDS} 秒，这台服务器一个字也没回答` : `No answer from this server in ${READ_DEADLINE_SECONDS} seconds`,
            body: zh
              ? `这一发读取到了期限仍然没有任何回答：服务器既没有拒绝，也没有答应。这台设备上也没有可只读查看的本地副本，所以现在没有内容可显示。可以再试一次。`
              : `This read reached its deadline with no answer at all: the server neither refused nor agreed. This device holds no local copy to show either, so there is nothing to display right now. Retrying is safe.`,
          };
    case "failure.unauthorized":
      return {
        kicker: zh ? "身份" : "Identity",
        title: zh ? "这一趟需要重新登录才能读" : "This trip needs a fresh sign-in",
        body: zh
          ? `服务器拒绝了这次读取（http_status=${attribution.http_status}）。登录之后这一趟还在。`
          : `The server refused this read (http_status=${attribution.http_status}). Your trip is still there once you sign in.`,
      };
    case "failure.forbidden":
      return {
        kicker: zh ? "权限" : "Permission",
        title: zh ? "这一步这台服务器不允许" : "This server will not allow this step",
        body: zh
          ? `服务器认得你是谁，但明确拒绝了这一步（http_status=${attribution.http_status}${attribution.server_code ? `：${attribution.server_code}` : ""}）。这不是网络问题，也不是登录问题——重新登录不会改变它的回答。你可以换一趟行程，或回到选择页。`
          : `The server knows who you are and still refused this step (http_status=${attribution.http_status}${attribution.server_code ? `: ${attribution.server_code}` : ""}). This is not a network problem and not a sign-in problem -- signing in again will not change its answer. Choose another trip, or go back to the picker.`,
      };
    default:
      return {
        kicker: zh ? "读取" : "Read",
        title: zh ? "这一次没读到" : "This read failed",
        body: zh
          ? "服务器接上了，但这次读取没有成功，原因还没有分类。可以重试。"
          : "The server answered, but this read did not succeed and the cause is not yet classified. Retrying is safe.",
      };
  }
}
