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

const RUNTIME_IDENTITY: Record<string, Label> = {
  NOT_CONFIGURED: { zh: "未配置", en: "Not configured" },
};

const POLICY_MODE: Record<string, Label> = {
  ADVISORY: { zh: "建议", en: "Advisory" },
  ASSISTED: { zh: "协助", en: "Assisted" },
  AUTONOMOUS: { zh: "自主", en: "Autonomous" },
};

/** Only the states the traveller-facing dictionary does not already name. Where a screen has an
 *  `exec.state.*` key, that key stays authoritative -- this table is the fallback, not a rival. */
const EXECUTION_STATE: Record<string, Label> = {
  PLANNED: { zh: "计划中", en: "Planned" },
};

const PLAN_SOURCE: Record<string, Label> = {
  INITIAL: { zh: "首次生成", en: "First generation" },
  REPLAN: { zh: "来自一次重规划", en: "From a replan" },
  MANUAL: { zh: "来自一次人工改动", en: "From a manual change" },
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
   (`backend/app/schemas.py:772`), `level` is the frozen strength tuple
   (`backend/app/commerce/strengths_generated.py:6-12`), and `status` is what the server actually writes
   (`backend/app/commerce/service_impl.py:306`, `:442`, `backend/app/operations/v9.py:579`, `:646`).
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
};

/* The demo deployment is not a city a traveller can name. `backend/app/anywhere_capability.py:233`
   already carries traveller-facing labels for this very code, so this says the same thing in words
   instead of painting the code's last segment. */
const CITY_CODE: Record<string, Label> = {
  CNX_DEMO: { zh: "演示城市", en: "Demo city" },
};

const SUMMARY_HELD: Label = {
  zh: "上游那句摘要的原文不在这一屏",
  en: "The upstream summary's own words are not on this screen",
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

export function policyModeLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(POLICY_MODE, value, zh);
}

export function executionStateLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(EXECUTION_STATE, value, zh);
}

export function planSourceLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(PLAN_SOURCE, value, zh);
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
