/**
 * ORD-0425: internal enum tokens -> traveller words, in one place.
 *
 * Display layer only. The tokens themselves (`NOT_CONFIGURED`, `ADVISORY`, `PLANNED`, `INITIAL`,
 * `Asia/Shanghai`, ...) stay exactly as the contract and ORD-0359 define them -- nothing here
 * rewrites a value, a route or a test; it only decides what a traveller is shown for one.
 *
 * A token this table does not know keeps its own raw value on screen, labelled as the server's word,
 * so a missing entry reads as a gap instead of silently becoming an invented claim.
 */

type Label = { zh: string; en: string };

/* CMDR 10-03 13:5x: the intelligence runtime card prints this value (`ControlTowerPanel.tsx`), and
   the table carried one of the four names `backend/app/agent/runtime_identity.py` can write. The
   other three went down `pick`'s labelled-raw branch, so the traveller read
   「这台服务器写的：NATIVE_DEEPSEEK_TOOL_LOOP」. Each wording below states the condition that branch
   actually tested (`runtime_identity.py`) -- no wording claims a verified gateway where only a
   configured endpoint exists, and none repeats the server's own name. */
const RUNTIME_IDENTITY: Record<string, Label> = {
  HERMES_NATIVE_MCP: {
    zh: "本机自带的原生网关正在应答",
    en: "the native gateway installed on this machine is answering",
  },
  HERMES_COMPAT_ENDPOINT: {
    zh: "这台部署配的兼容端点，原生网关没有验到在应答",
    en: "the compatibility endpoint this deployment configured; no verified native gateway",
  },
  NATIVE_DEEPSEEK_TOOL_LOOP: {
    zh: "这个服务自己直连模型商的那条环",
    en: "this service's own loop straight to the model provider",
  },
  NOT_CONFIGURED: { zh: "未配置", en: "Not configured" },
};

const POLICY_MODE: Record<string, Label> = {
  ADVISORY: { zh: "建议", en: "Advisory" },
  ASSISTED: { zh: "协助", en: "Assisted" },
  AUTONOMOUS: { zh: "自主", en: "Autonomous" },
};

/** Only the states the traveller-facing dictionary does not already name. Where a screen has an
 *  `exec.state.*` key, that key stays authoritative -- this table is the fallback, not a rival. */
/* ORD-0553: the status Literal in `backend/app/schemas.py` can also answer READY and CANCELLED for an
   activity. Neither had a wording here nor an `exec.state.*` key, so both reached the screen through
   `pick`'s labelled-raw branch and painted their own name. CANCELLED reuses the pair this file already
   carries for that same server value (`COMMITMENT_STATUS`), so it is a second use of one reading. */
const EXECUTION_STATE: Record<string, Label> = {
  PLANNED: { zh: "计划中", en: "Planned" },
  READY: { zh: "可以开始了", en: "Ready to start" },
  CANCELLED: { zh: "已取消", en: "Cancelled" },
};

const PLAN_SOURCE: Record<string, Label> = {
  INITIAL: { zh: "首次生成", en: "First generation" },
  REPLAN: { zh: "来自一次重规划", en: "From a replan" },
  MANUAL: { zh: "来自一次人工改动", en: "From a manual change" },
};

/**
 * ORD-0551: what the two extraction routes (`inbox/text-extract`, `inbox/vision`) answer as their
 * top-level `status`. Before this family existed the fallback branch of the workbench printed the
 * reply's own token, so a supplier failure reached a Chinese screen as 「提取状态： FAILED_EXTERNAL」.
 * The three names here are the whole set those routes can write, and the lookup below is closed: a status
 * that is not listed is answered with a gap sentence, never with the word it was handed. This family does
 * not take the labelled-raw channel, because its input is the reply's own `status` -- an upstream read
 * reaching the shared `pick` would condemn every other panel that imports a `pick`-based label, since the
 * module graph answers a helper's return from all of its call sites (Q64). No invented states: these
 * sentences describe what happened, they do not promise a retry policy or rename a refusal the contract
 * already decides elsewhere.
 */
const EXTRACTION_KEYS = ["READY", "NOT_CONFIGURED", "FAILED_EXTERNAL"] as const;
type ExtractionStatus = (typeof EXTRACTION_KEYS)[number];
const EXTRACTION_STATUS: Record<ExtractionStatus, Label> = {
  READY: { zh: "读出来了，等你过目", en: "read out, waiting for you to check it" },
  NOT_CONFIGURED: { zh: "这台服务器没接读取服务", en: "this server has no reader connected" },
  FAILED_EXTERNAL: {
    zh: "外部服务这次没给回话，这条没读出来",
    en: "the outside service did not reply this time, so nothing was read out of it",
  },
};

/** ORD-0432: the words `feasibility_envelope.risk_reason_codes` can hold. A trip with an open
 *  MAJOR or CRITICAL event is not "nothing to see", and the reason has to be readable. */
const RISK_REASON: Record<string, Label> = {
  PROVIDER_DEGRADED: { zh: "数据源降级", en: "Provider degraded" },
  DISRUPTION_MAJOR: { zh: "有重大变动没处理", en: "Major disruption open" },
  DISRUPTION_CRITICAL: { zh: "有严重变动没处理", en: "Critical disruption open" },
};

const TIME_ZONE: Record<string, Label> = {
  "Asia/Shanghai": { zh: "上海时间", en: "Shanghai time" },
  "Asia/Singapore": { zh: "新加坡时间", en: "Singapore time" },
  "Asia/Tokyo": { zh: "东京时间", en: "Tokyo time" },
  UTC: { zh: "协调世界时", en: "Coordinated universal time" },
};

const PLAN_VERSION_NOUN: Label = { zh: "方案版本", en: "Plan version" };

/* ORD-0446: the bookings card used to print the three internal identifiers straight from the wallet
   payload. The value sets below are not invented here -- `kind` is a contract Literal
   (`backend/app/schemas.py`), `level` is the frozen strength tuple
   (`backend/app/commerce/strengths_generated.py`), and `status` is what the server actually writes
   (`backend/app/commerce/service_impl.py`, `:442`, `backend/app/operations/v9.py`, `:646`).
   Anything outside them still falls to the labelled raw channel, so a new server value shows up as a
   gap rather than being quietly renamed into a claim. */
const COMMITMENT_KIND: Record<string, Label> = {
  BOOKING: { zh: "预订", en: "Booking" },
  ORDER: { zh: "订单", en: "Order" },
  PAYMENT: { zh: "付款", en: "Payment" },
  APPOINTMENT: { zh: "预约", en: "Appointment" },
  OTHER: { zh: "其他凭据", en: "Other evidence" },
};

const COMMITMENT_LEVEL: Record<string, Label> = {
  C0_FLEXIBLE: { zh: "可灵活改动", en: "Flexible" },
  C1_PREFERRED: { zh: "倾向保留", en: "Preferred" },
  C2_RESERVED: { zh: "已预留", en: "Reserved" },
  C3_GUARANTEED: { zh: "有保证", en: "Guaranteed" },
  C4_ANCHORED: { zh: "受保护锚点", en: "Protected anchor" },
};

const COMMITMENT_STATUS: Record<string, Label> = {
  ACTIVE: { zh: "生效中", en: "Active" },
  CONFIRMED: { zh: "已确认", en: "Confirmed" },
  CANDIDATE: { zh: "待确认", en: "Awaiting confirmation" },
  PLANNED: { zh: "已排入计划", en: "Planned" },
  /* A retired anchor is a state this server writes (operations/v9.py, superseded-active-anchor), so
     it is named here in the words `travelerLabels.ts` already uses for it -- the row is a copy of that
     entry, not a second reading of the fact. */
  CANCELLED: { zh: "已取消", en: "Cancelled" },
  CANCELED: { zh: "已取消", en: "Cancelled" },
};

/* The demo deployment is not a city a traveller can name. `backend/app/anywhere_capability.py`
   already carries traveller-facing labels for this very code, so this says the same thing in words
   instead of painting the code's last segment. */
const CITY_CODE: Record<string, Label> = {
  CNX_DEMO: { zh: "演示城市", en: "Demo city" },
};

/* ORD-0455: the estimation ladder's 说方法 rung has to name where a figure came from, and the cell
   it reads is `stay.evidence_json.source`, which `backend/app/stay_mobility.py` writes as
   `provider.provenance`. The three values below are the provenances that module's own provider
   classes declare (`:111` CAPTURED_REPLAY, `:136` UNAVAILABLE, `:1323` SYNTHETIC_FIXTURE) -- nothing
   else is listed, so a fourth provenance reaches the screen through the labelled raw channel and
   reads as a gap in this table rather than as a claim we invented about a provider. */
const EVIDENCE_SOURCE: Record<string, Label> = {
  CAPTURED_REPLAY: { zh: "一次采回来的报价回放", en: "a captured replay of a provider quote" },
  UNAVAILABLE: { zh: "这台服务器说没有可用来源", en: "the server says no source is available" },
  SYNTHETIC_FIXTURE: { zh: "演示夹具造的数，不是供应商给的", en: "a demo fixture value, not a provider rate" },
};

const SUMMARY_HELD: Label = {
  zh: "上游那句摘要的原文不在这一屏",
  en: "The upstream summary's own words are not on this screen",
};

/* 路段证据的档位（`fact_quality`）也走这一层：三个名字逐枚来自
   `backend/app/replanning/provider_snapshots.py`，第四个值一旦出现就退回上面那两条通道
   （`pick` 的 labelled-raw 那一支），屏上说的是「这台服务器写的：X」而不是我们替它编的人话。 */
const MOBILITY_FACT_QUALITY: Record<string, Label> = {
  EXPLICIT_FROZEN_FIXTURE: { zh: "冻结快照里写明的一条", en: "a value written out in the frozen snapshot" },
  LIVE_PROVIDER_CAPTURE: { zh: "供应商这次真给的一条", en: "a value a provider actually returned" },
  SYNTHETIC_DEFAULT: { zh: "合成默认值，不是供应商给的", en: "a synthetic default, not a provider reading" },
};

function wantsChinese(zh?: boolean | string | null): boolean {
  if (typeof zh === "boolean") return zh;
  if (typeof zh === "string") return zh.trim().toLowerCase().startsWith("zh");
  return false;
}

function keyOf(value: unknown): string {
  return String(value ?? "").trim().replace(/[\s-]+/g, "_").toUpperCase();
}

function rawChannel(value: unknown, zh?: boolean | string | null): string {
  const raw = String(value ?? "").trim();
  if (!raw) return wantsChinese(zh) ? "这台服务器没写值" : "this server wrote no value";
  return wantsChinese(zh) ? `这台服务器写的：${raw}` : `as this server wrote it: ${raw}`;
}

function pick(map: Record<string, Label>, value: unknown, zh?: boolean | string | null): string {
  const entry = map[keyOf(value)];
  if (entry) return wantsChinese(zh) ? entry.zh : entry.en;
  return rawChannel(value, zh);
}

export function runtimeIdentityLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(RUNTIME_IDENTITY, value, zh);
}

export function mobilityFactQualityLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(MOBILITY_FACT_QUALITY, value, zh);
}

export function policyModeLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(POLICY_MODE, value, zh);
}

export function executionStateLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(EXECUTION_STATE, value, zh);
}

export function planSourceLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(PLAN_SOURCE, value, zh);
}

/** Closed lookup: only a name in `EXTRACTION_KEYS` can be answered with, so the value handed here reaches
 *  the screen only as one of this file's own sentences. */
export function extractionStatusLabel(value: unknown, zh?: boolean | string | null): string {
  const key = EXTRACTION_KEYS.find((name) => value === name);
  if (!key) return wantsChinese(zh) ? "这台服务器写的读取状态不在这张表里"
    : "the read status this server wrote is not in this table";
  const entry = EXTRACTION_STATUS[key];
  return wantsChinese(zh) ? entry.zh : entry.en;
}

export function riskReasonLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(RISK_REASON, value, zh);
}

/** ORD-0425 r3, on the commander's ruling: a zone this table knows is shown in traveller words only.
 *  The IANA id keeps its exact value -- it is what the backend and contract use -- but it no longer
 *  reaches the screen. A zone the table does NOT know still shows its own value, labelled as the
 *  server's word, because naming an unlisted zone in human words would be an invention. */
export function clockZoneLabel(value: unknown, zh?: boolean | string | null): string {
  const raw = String(value ?? "").trim();
  const entry = TIME_ZONE[raw] || TIME_ZONE[keyOf(raw)];
  if (!entry || !raw) return rawChannel(value, zh);
  return wantsChinese(zh) ? entry.zh : entry.en;
}

export function planVersionNoun(zh?: boolean | string | null): string {
  return wantsChinese(zh) ? PLAN_VERSION_NOUN.zh : PLAN_VERSION_NOUN.en;
}

export function commitmentKindLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(COMMITMENT_KIND, value, zh);
}

export function commitmentLevelLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(COMMITMENT_LEVEL, value, zh);
}

export function commitmentStatusLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(COMMITMENT_STATUS, value, zh);
}

/** A city code the display table does not know keeps the old behaviour: the code's own tail on screen.
 *  Only the demo deployment's code, which names no city at all, gets words. */
export function cityCodeLabel(value: unknown, zh?: boolean | string | null): string {
  const key = keyOf(value);
  const entry = CITY_CODE[key];
  if (!entry) {
    const raw = String(value ?? "").trim();
    const tail = raw.includes("-") ? raw.split("-").slice(1).join("-") : raw;
    return tail || raw;
  }
  return wantsChinese(zh) ? entry.zh : entry.en;
}

export function summaryHeldLabel(zh?: boolean | string | null): string {
  return wantsChinese(zh) ? SUMMARY_HELD.zh : SUMMARY_HELD.en;
}

/** Words for `stay.evidence_json.source` (ORD-0455). An unlisted provenance keeps its own value,
 *  labelled as the server's word: a method we cannot name is not a method we may describe. */
export function evidenceSourceLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(EVIDENCE_SOURCE, value, zh);
}
