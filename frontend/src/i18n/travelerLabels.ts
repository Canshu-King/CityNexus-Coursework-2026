/**
 * One shared map from internal role / category / type / status vocabulary to
 * traveller words.
 *
 * CityNexus stores structural facts in machine form — `check_out`, `MID_TRIP`,
 * `museum`, `STRUCTURED_JSON`, `manual-entry`, `CONFIRMED`.  Those tokens are
 * routing vocabulary, not traveller words, so every consumer surface
 * (TripItinerary, ActivityDetail, the inbox review, the disruption notice)
 * reads its labels from here so the vocabulary cannot drift.
 *
 * Contract:
 *   • Chinese-first copy with the English label alongside (`zh` flag).
 *   • Case-insensitive: `check_out`, `CHECK_OUT` and `Check Out` all match.
 *   • Unknown values never throw and never surface a raw SCREAMING_CASE enum:
 *     the fallback replaces separators with spaces and lowercases the token.
 */

type Label = { zh: string; en: string };

const ROLE_LABELS: Record<string, Label> = {
  ARRIVAL: { zh: "抵达", en: "Arrival" },
  DEPARTURE: { zh: "出发", en: "Departure" },
  LEAVE: { zh: "离开", en: "Leave" },
  CHECK_IN: { zh: "入住", en: "Check-in" },
  CHECK_OUT: { zh: "退房", en: "Check-out" },
  STAY: { zh: "住宿", en: "Stay" },
  OVERNIGHT: { zh: "过夜", en: "Overnight" },
  LUGGAGE_DROP: { zh: "寄存行李", en: "Luggage drop" },
  ACTIVITY: { zh: "活动", en: "Activity" },
  VISIT: { zh: "游览", en: "Visit" },
  TRANSIT: { zh: "交通", en: "Transit" },
  TRANSPORT: { zh: "交通", en: "Transport" },
  BOOKING: { zh: "预订", en: "Booking" },
  MID_TRIP: { zh: "中途", en: "Mid-trip" },
  MUSEUM: { zh: "博物馆", en: "Museum" },
  FOOD: { zh: "美食", en: "Food" },
  HOTEL: { zh: "住宿", en: "Hotel" },
  ATTRACTION: { zh: "景点", en: "Attraction" },
  SIGHT: { zh: "景点", en: "Sight" },
  PARK: { zh: "公园", en: "Park" },
  GARDEN: { zh: "园林", en: "Garden" },
  CAFE: { zh: "咖啡馆", en: "Café" },
  COFFEE: { zh: "咖啡馆", en: "Café" },
  RESTAURANT: { zh: "餐厅", en: "Restaurant" },
  GALLERY: { zh: "美术馆", en: "Gallery" },
  ARTS_CENTRE: { zh: "艺术中心", en: "Arts centre" },
  LIBRARY: { zh: "图书馆", en: "Library" },
  THEATRE: { zh: "剧院", en: "Theatre" },
  THEATER: { zh: "剧院", en: "Theatre" },
  CINEMA: { zh: "电影院", en: "Cinema" },
  HERITAGE: { zh: "历史街区", en: "Heritage" },
  VIEWPOINT: { zh: "观景点", en: "Viewpoint" },
  SHOPPING: { zh: "购物", en: "Shopping" },
  MARKET: { zh: "市场", en: "Market" },
  BAR: { zh: "酒吧", en: "Bar" },
  PUB: { zh: "酒馆", en: "Pub" },
  BEACH: { zh: "海滩", en: "Beach" },
  TEMPLE: { zh: "寺庙", en: "Temple" },
  CHURCH: { zh: "教堂", en: "Church" },
  MONUMENT: { zh: "纪念地", en: "Monument" },
  SQUARE: { zh: "广场", en: "Square" },
  STADIUM: { zh: "体育场", en: "Stadium" },
  ZOO: { zh: "动物园", en: "Zoo" },
  AQUARIUM: { zh: "水族馆", en: "Aquarium" },
  SPA: { zh: "水疗", en: "Spa" },
  SERVICE: { zh: "服务", en: "Service" },
  EXPERIENCE: { zh: "体验", en: "Experience" },
  BREAKFAST: { zh: "早餐", en: "Breakfast" },
  LUNCH: { zh: "午餐", en: "Lunch" },
  DINNER: { zh: "晚餐", en: "Dinner" },
  MEAL: { zh: "用餐", en: "Meal" },
  UNKNOWN: { zh: "未确定", en: "Not set" },
  OTHER: { zh: "其他", en: "Other" },
};

const CATEGORY_LABELS: Record<string, Label> = {
  ...ROLE_LABELS,
  UNKNOWN: { zh: "未知", en: "Unknown" },
};

const STATUS_LABELS: Record<string, Label> = {
  CONFIRMED: { zh: "已确认", en: "Confirmed" },
  PLANNED: { zh: "计划中", en: "Planned" },
  CANCELLED: { zh: "已取消", en: "Cancelled" },
  CANCELED: { zh: "已取消", en: "Cancelled" },
  COMPLETED: { zh: "已完成", en: "Completed" },
  SKIPPED: { zh: "已跳过", en: "Skipped" },
  UNKNOWN: { zh: "未知", en: "Unknown" },
  UNKNOWN_REALTIME: { zh: "暂无实时状态", en: "No live status" },
  PENDING: { zh: "待处理", en: "Pending" },
  REQUESTED: { zh: "已请求", en: "Requested" },
  PROPOSED: { zh: "已建议", en: "Proposed" },
  APPROVED: { zh: "已批准", en: "Approved" },
  REJECTED: { zh: "已拒绝", en: "Rejected" },
  APPLIED: { zh: "已应用", en: "Applied" },
  DRAFT: { zh: "草稿", en: "Draft" },
  READY: { zh: "已就绪", en: "Ready" },
  READY_FOR_CONFIRMATION: { zh: "待确认", en: "Ready to confirm" },
  NEEDS_REVIEW: { zh: "需审核", en: "Needs review" },
  NOT_STARTED: { zh: "未开始", en: "Not started" },
  IN_PROGRESS: { zh: "进行中", en: "In progress" },
  EN_ROUTE: { zh: "在路上", en: "On the way" },
  ARRIVED: { zh: "已抵达", en: "Arrived" },
  ACTIVE: { zh: "进行中", en: "Active" },
  FAILED: { zh: "失败", en: "Failed" },
  ERROR: { zh: "出错", en: "Error" },
  STOPPED: { zh: "已停止", en: "Stopped" },
  EXPIRED: { zh: "已过期", en: "Expired" },
  DELAYED: { zh: "延误", en: "Delayed" },
  RESCHEDULED: { zh: "已改期", en: "Rescheduled" },
  PROTECTED: { zh: "受保护", en: "Protected" },
  CANDIDATE: { zh: "候选", en: "Candidate" },
  PAID: { zh: "已支付", en: "Paid" },
  UNPAID: { zh: "未支付", en: "Not paid" },
  PARTIAL: { zh: "部分支付", en: "Partly paid" },
  REFUNDED: { zh: "已退款", en: "Refunded" },
  FREE: { zh: "免费", en: "Free" },
};

const ARTIFACT_LABELS: Record<string, Label> = {
  STRUCTURED_JSON: { zh: "结构化数据", en: "Structured data" },
  TEXT: { zh: "文本", en: "Text" },
  ICS: { zh: "日历", en: "Calendar" },
  IMAGE: { zh: "截图", en: "Screenshot" },
};

const EXTRACTION_LABELS: Record<string, Label> = {
  DETERMINISTIC: { zh: "本机解析", en: "Read on device" },
  DEEPSEEK_RESPONSES_TEXT: { zh: "智能解析", en: "Smart reading" },
  DEEPSEEK_RESPONSES_VISION: { zh: "图片识别", en: "Image reading" },
  MANUAL: { zh: "手动录入", en: "Entered by hand" },
};

const PAYMENT_LABELS: Record<string, Label> = {
  PAID: { zh: "已支付", en: "Paid" },
  UNPAID: { zh: "未支付", en: "Not paid" },
  PARTIAL: { zh: "部分支付", en: "Partly paid" },
  REFUNDED: { zh: "已退款", en: "Refunded" },
  UNKNOWN: { zh: "未知", en: "Unknown" },
};

const PROVIDER_LABELS: Record<string, Label> = {
  "manual-entry": { zh: "手动录入", en: "Entered by hand" },
  "pasted-confirmation": { zh: "粘贴的确认单", en: "Pasted confirmation" },
  "osm-overpass": { zh: "地图数据", en: "Map data" },
  "external-import": { zh: "导入", en: "Imported" },
  "ics-import": { zh: "日历导入", en: "Calendar import" },
  "user-confirmed-rail": { zh: "用户确认的车票", en: "Confirmed rail ticket" },
};

const DECISION_LABELS: Record<string, Label> = {
  AUTO_APPLY: { zh: "可以自动加入行程", en: "Ready to add to your trip" },
  APPLY_WITH_NOTICE: { zh: "加入时会提醒你", en: "Added with a notice" },
  REQUIRES_CONFIRMATION: { zh: "需要你先确认", en: "Needs your confirmation" },
  NEEDS_REVIEW: { zh: "需要你补充信息", en: "Needs a few more details" },
  APPLIED: { zh: "已加入行程", en: "Added to your trip" },
  STOPPED: { zh: "已暂停", en: "Paused" },
};

const DISRUPTION_LABELS: Record<string, Label> = {
  CLOSURE: { zh: "地点临时关闭", en: "Place temporarily closed" },
  WEATHER: { zh: "天气影响", en: "Weather affected" },
  TRAFFIC_DELAY: { zh: "路上拥堵", en: "Traffic delay" },
  SCHEDULE_DELAY: { zh: "行程时间变化", en: "Schedule changed" },
  SERVICE_UNAVAILABLE: { zh: "服务不可用", en: "Service unavailable" },
  USER_CHANGE: { zh: "你更新了需求", en: "You changed a requirement" },
  OTHER: { zh: "行程有新情况", en: "Something changed" },
  UNKNOWN: { zh: "行程有新情况", en: "Something changed" },
};

/** Coerce anything into a printable string without ever throwing. */
function toText(value: unknown): string {
  if (value === null || value === undefined) return "";
  const type = typeof value;
  if (type === "string") return value as string;
  if (type === "number" || type === "boolean" || type === "bigint") {
    return String(value);
  }
  return "";
}

/**
 * Safe fallback for an unmapped internal value.
 * `SOME_WEIRD_VALUE` → `some weird value`, `manual-entry` → `manual entry`.
 */
export function humanizeToken(raw: unknown): string {
  return toText(raw).replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim().toLowerCase();
}

function wantsChinese(zh?: boolean | string | null): boolean {
  if (typeof zh === "boolean") return zh;
  if (typeof zh === "string") return zh.trim().toLowerCase().startsWith("zh");
  return false;
}

function pick(
  map: Record<string, Label>,
  value: unknown,
  zh?: boolean | string | null,
): string {
  const key = toText(value).trim().replace(/[\s-]+/g, "_").toUpperCase();
  const entry = key ? map[key] : undefined;
  if (entry) return wantsChinese(zh) ? entry.zh : entry.en;
  return humanizeToken(value);
}

/** Internal role / anchor role / activity type -> traveller words. */
export function roleLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(ROLE_LABELS, value, zh);
}

/** Internal place category / service class -> traveller words. */
export function categoryLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(CATEGORY_LABELS, value, zh);
}

/** Booking / draft / execution / payment status -> traveller words. */
export function bookingStatusLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(STATUS_LABELS, value, zh);
}

/** Booking artifact type (STRUCTURED_JSON / TEXT / ICS) -> traveller words. */
export function artifactTypeLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(ARTIFACT_LABELS, value, zh);
}

/** Extraction mode (DETERMINISTIC / DEEPSEEK_...) -> traveller words. */
export function extractionModeLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(EXTRACTION_LABELS, value, zh);
}

/** Payment status -> traveller words. */
export function paymentStatusLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(PAYMENT_LABELS, value, zh);
}

/** Provider key (`manual-entry`, `osm-overpass`, ...) -> traveller words. */
export function providerLabel(value: unknown, zh?: boolean | string | null): string {
  const key = toText(value).trim();
  const entry = key ? PROVIDER_LABELS[key] ?? PROVIDER_LABELS[key.toLowerCase()] : undefined;
  if (entry) return wantsChinese(zh) ? entry.zh : entry.en;
  return humanizeToken(key);
}

/** Smart-intake decision enum -> traveller words. */
export function decisionLabel(value: unknown, zh?: boolean | string | null): string {
  return pick(DECISION_LABELS, value, zh);
}

/** Journey disruption event type -> traveller words. */
export function disruptionLabel(value: unknown, zh?: boolean | string | null): string {
  const mapped = pick(DISRUPTION_LABELS, value, zh);
  if (mapped) return mapped;
  return wantsChinese(zh) ? "行程有新情况" : "Something changed";
}

/**
 * A stop's role for the itinerary.  Prefers the stored role, then the
 * category, then the raw activity type - all mapped to traveller words.
 */
export function stopRoleLabel(
  activity: { type?: string | null; metadata?: Record<string, unknown> | null },
  zh?: boolean | string | null,
): string {
  const meta = activity.metadata ?? {};
  const raw =
    (meta.role as string | undefined) ||
    (meta.category as string | undefined) ||
    activity.type ||
    "";
  return roleLabel(raw, zh) || categoryLabel(raw, zh);
}
