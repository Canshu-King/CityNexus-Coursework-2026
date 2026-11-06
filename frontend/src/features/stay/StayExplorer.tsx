import { Icon } from "../../components/v8/Icon";
import { MapPanel } from "../../components/v8/MapPanel";
import type { V8Data } from "../../app/v8Types";
import { useT, useLocale } from "../../i18n/LocaleContext";
import { formatMinorMoney } from "../../lib/money";
import { evidenceSourceLabel } from "../../lib/displayVocabulary";
import { EstimationLadderPanel } from "../estimation/EstimationLadderPanel";
import type { EstimateReading } from "../estimation/estimationLadder";
import { useState } from "react";
import {
  generateStayOptions,
  stayOutcomeCopy,
  type GenerateDoor,
} from "./stayGenerationDoor";

export function StayExplorer({
  data,
  selected,
  onSelect,
  onNavigate,
  onReload,
}: {
  data: V8Data;
  selected: V8Data["options"][number] | null;
  onSelect: (id: string) => void;
  /** Ord-0455: the ladder's 给补救 rung names a door in words. It may only name one this shell can
   *  actually reach, so the shell hands the navigation capability in and its absence is honoured. */
  onNavigate?: (route: string) => void;
  /** ORD-0511: the cards on this screen are the shell's own read, so after a generation the screen
   *  asks for that read again instead of keeping a second candidate list of its own. */
  onReload?: () => void;
}) {
  const t = useT();
  const { locale } = useLocale();
  const zh = locale === "zh-CN";
  // The stay's check-in/check-out are committed instants at the stay's own place; read them in the
  // journey's zone, and name it only when that zone can actually be used.
  const stayTz = data.journey.timezone || "UTC";
  const stayTzUsable = zoneIsUsable(data.journey.timezone);
  /* ORD-0511: the empty rung used to be a dead end -- it told the traveller there was nothing and
     offered no way to find out whether the server could produce something. `door` is what this trip
     actually answered to the one call this screen can make; it is never the source of the cards. */
  const [door, setDoor] = useState<GenerateDoor | null>(null);
  const [working, setWorking] = useState(false);
  /* The shell hands this screen its one read door; if it ever stops doing so, a generation would
     really happen and the cards would still not appear. That is a fact this screen can see and must
     say, not one it may leave as a silent difference between the answer and the page. */
  const [reloadMissing, setReloadMissing] = useState(false);

  const runGeneration = async () => {
    setWorking(true);
    const answer = await generateStayOptions(data.journey.id);
    setDoor(answer);
    setWorking(false);
    if (answer.kind === "ok") {
      if (onReload) onReload();
      else setReloadMissing(true);
    }
  };

  // A city label derived from the Journey, never a hard-coded demo city.
  const cityLabel = String(data.journey.city_code || "").split("-").pop() || "";
  const protectedStations = protectedStationNames(data);
  const confirmed = data.options.find((item) => item.id === data.selected_option_id) ?? null;
  const budgetLine = stayBudgetSentence(data, confirmed, zh);
  if (!data.options.length) {
    return (
      <section className="v8-page" data-testid="v8-stay-explorer">
        <div className="v8-page-intro compact">
          <div>
            <p className="v8-kicker">{t("stay.kicker")}</p>
            <h1>{t("stay.title")}</h1>
          </div>
        </div>
        <div className="v8-empty-state" data-testid="stay-no-options">
          <b>{zh ? "还没有可用的住宿候选" : "No stay candidates yet"}</b>
          <p>
            {zh
              ? "我们还没有拿到真实住宿数据，所以不会给你看一张假的价格卡片。可以先选好行程，再回来搜索住宿。"
              : "We have no real accommodation data yet, so we will not show you an invented price card. Choose your plan first, then come back to search stays."}
          </p>
          {/* ORD-0511: one press makes the real call. Until it has been made this screen offers no
              "定下来" control at all -- there is nothing to confirm, and a button that pretended
              otherwise would be the fake this order exists to prevent. */}
          <button
            type="button"
            className="v8-button"
            data-testid="stay-generate"
            disabled={working}
            onClick={() => void runGeneration()}
          >
            {working
              ? zh
                ? "正在向这台服务器要住宿候选…"
                : "Asking this server for stays…"
              : zh
                ? "向这台服务器要一次住宿候选"
                : "Ask this server for stays"}
          </button>
          {door ? (
            <p className="v8-muted" data-testid="stay-generation-outcome">
              {door.kind === "ok"
                ? stayOutcomeCopy(String(door.outcome), zh)
                : door.kind === "refused"
                  ? refusalSentence(door, zh)
                  : zh
                    ? "这一次请求没有拿到可读的回话，所以这里没有一个关于原因的说法。"
                    : "This request came back without a readable answer, so there is nothing here to say about why."}
            </p>
          ) : null}
          {door?.kind === "ok" ? (
            <dl className="v8-detail-panel" data-testid="stay-generation-facts">
              <div>
                <dt>{zh ? "这一轮排定的候选数" : "Candidates ranked this run"}</dt>
                <dd>{door.ranked ?? "—"}</dd>
              </div>
              <div>
                <dt>{zh ? "这台服务器交回的住宿数" : "Stays this server returned"}</dt>
                <dd>{door.offers ?? "—"}</dd>
              </div>
            </dl>
          ) : null}
          {reloadMissing ? (
            <p role="alert" className="v8-muted" data-testid="stay-reload-absent">
              {zh
                ? "这一轮服务器答完了，但这一屏没有从外层拿到重新读取的能力，所以卡片不会出现在这里——这一句是本屏自己报的缺，不是服务器的答复。"
                : "This run was answered, but this screen was not handed a way to re-read, so the cards cannot appear here -- this line is the screen reporting its own missing capability, not the server's answer."}
            </p>
          ) : null}
          {/* ORD-0468: the ladder keeps its own name here instead of going with the missing cards. */}
          <EstimationLadderPanel
            estimate={emptyScreenEstimate(Boolean(onNavigate), t("nav.studio"))}
            sourceToken={null}
          />
        </div>
      </section>
    );
  }
  return <section className="v8-page" data-testid="v8-stay-explorer">
    <div className="v8-page-intro compact"><div><p className="v8-kicker">{t("stay.kicker")}</p><h1>{t("stay.title")}</h1><p className="v8-lede">{t("stay.lede")}</p></div><span className="v8-evidence-label"><span className="status-dot" />{t("stay.capturedReplay")}</span></div>
    {/* ORD-0511: what this screen may claim, and on whose authority. The confirmation sentence is
        driven by the read (`selected_option_id` and that row's own `status`), never by the click --
        so a selection that the server did not record cannot be announced here. */}
    {confirmed && confirmed.status === "SELECTED" ? (
      <p className="v8-muted" data-testid="stay-confirmed-line">
        {zh
          ? `已按这台服务器的记录排定：${confirmed.stay.property_name ?? ""}`
          : `Recorded by this server as the stay for this trip: ${confirmed.stay.property_name ?? ""}`}
      </p>
    ) : null}
    {protectedStations.length ? (
      <p className="v8-muted" data-testid="stay-protected-stations">
        {zh ? `行程里受保护的站：${protectedStations.join("、")}` : `Protected stops in the trip: ${protectedStations.join(", ")}`}
      </p>
    ) : null}
    {budgetLine ? (
      <p className="v8-muted" data-testid="stay-budget-line">
        {budgetLine}
      </p>
    ) : null}
    <div className="v8-stay-layout">
      <div className="v8-stay-list">{data.options.slice(0, 4).map((item) => <article data-testid="stay-offer-card" className={item.id === data.selected_option_id ? "v8-stay-card selected" : "v8-stay-card"} key={item.id} onClick={() => onSelect(item.id)} onKeyDown={(event) => { if (event.key === "Enter") onSelect(item.id); }} role="button" tabIndex={0}>
        <div className="v8-hotel-thumb large"><Icon name="bed" /><span>{cityLabel}</span></div>
        <div className="v8-stay-card-body"><div className="v8-stay-card-title"><div><span className="v8-rank">0{item.rank}</span><h3>{item.stay.property_name}</h3></div>{item.id === data.selected_option_id && <span className="v8-selected-label">{t("stay.selected")}</span>}</div><p>{roomLabel(item.stay.room_type, zh)} · {data.trip.party_size} {zh ? "位旅客" : "travelers"} · {item.stay.refundable ? t("stay.freeCancel") : t("stay.nonRefundable")}</p><div className="v8-stay-price">{pricePair(item.stay_total_minor, item.stay.nightly_amount_minor, item.stay.currency, locale, zh)}</div><div className="v8-stay-impact"><span>{t("stay.journeyImpact")}</span><b>{item.walking_min} {zh ? "分钟步行" : "min walk"}</b><b>{item.protected_margin_min} {zh ? "分钟余量" : "min slack"}</b><em>{modeLabel(item.leg_modes[0], zh)}</em></div></div>
      </article>)}</div>
      <div className="v8-stay-detail"><MapPanel data={data} option={selected} /><div className="v8-detail-panel"><div><span>{t("stay.rateEvidence")}</span><b>{selected?.stay.property_name}</b></div><dl>
            <div>
              <dt>{t("stay.totalCurrency")}</dt>
              <dd>{priceLabel(selected?.stay.total_amount_minor, selected?.stay.currency, locale, zh)}</dd>
            </div>
            <div>
              <dt>{t("stay.checkInOut")}</dt>
              <dd>
                {dateInZone(selected?.stay.check_in_at, locale, stayTz)} → {dateInZone(selected?.stay.check_out_at, locale, stayTz)}
                {stayTzUsable ? ` · ${t("hub.zoneAttribution", { zone: stayTz })}` : ""}
              </dd>
            </div>
            <div>
              <dt>{t("stay.cancellation")}</dt>
              <dd>{cancellationLabel(selected?.stay.cancellation_policy?.summary, zh)}</dd>
            </div>
            <div>
              <dt>{t("stay.provider")}</dt>
              <dd>{evidenceLabel(selected, zh)}</dd>
            </div>
          </dl>
          {selected ? (
            <EstimationLadderPanel
              estimate={stayEstimate(
                selected,
                data.options.length,
                locale,
                zh,
                Boolean(onNavigate),
                t("nav.studio"),
              )}
              sourceToken={selected.stay.evidence?.source ?? null}
            />
          ) : null}
        </div>
      </div>
    </div>
  </section>;
}

function money(minor: number | null | undefined, currency = "CNY", locale = "en") { return formatMinorMoney(minor, currency, locale === "zh-CN" ? "zh-CN" : "en-US"); }

/**
 * ORD-0455 -- the cells this server sent about one stay's price, reduced to what the ladder reads.
 *
 * The words for the figure come from the same `money()` the card above uses, so the ladder can never
 * quote a total that disagrees with the price it is describing. `source` is the traveller's words for
 * `stay.evidence.source`, produced by the shared display layer (ORD-0425) rather than by this file,
 * and it stays empty when the payload carried no source cell -- an absent cell is not a source that
 * happens to be called "unknown".
 */
function stayEstimate(
  option: V8Data["options"][number],
  candidates: number,
  locale: string,
  zh: boolean,
  canRecompute: boolean,
  doorLabel: string,
): EstimateReading {
  const stay = option.stay;
  const total = typeof stay.total_amount_minor === "number" ? stay.total_amount_minor : null;
  const nightly = typeof stay.nightly_amount_minor === "number" ? stay.nightly_amount_minor : null;
  const minor = (total ?? 0) > 0 ? total : (nightly ?? 0) > 0 ? nightly : null;
  const token = String(stay.evidence?.source ?? "").trim();
  const confidence = stay.evidence?.confidence;
  return {
    amountMinor: minor,
    amountLabel: minor === null ? "" : money(minor, stay.currency, locale),
    source: token ? evidenceSourceLabel(token, zh) : "",
    confidence: typeof confidence === "number" ? confidence : null,
    binding: stay.refundable === false,
    alternatives: Math.max(0, candidates - 1),
    canRecompute,
    doorLabel,
    candidates,
  };
}

/**
 * ORD-0468 -- the reading for a screen with no candidate on it.
 *
 * This is not "a stay whose price is missing": there is no stay. `candidates: 0` is the only cell
 * that carries the fact, and the ladder answers it with one named rung in the 说方法 slot -- no
 * amount, no source, no confidence and no invented zero, because a number here would be read as the
 * server having measured something.
 */
function emptyScreenEstimate(canRecompute: boolean, doorLabel: string): EstimateReading {
  return {
    amountMinor: null,
    amountLabel: "",
    source: "",
    confidence: null,
    binding: false,
    alternatives: 0,
    canRecompute,
    doorLabel,
    candidates: 0,
  };
}

/**
 * OSM carries no price, so a live stay arrives as 0.  "0" is not a price: it
 * would read as a free hotel.  A zero/absent amount is reported as unknown in
 * the traveller's own language, and a known amount keeps its currency.
 */
function pricePair(
  totalMinor: number | null | undefined,
  nightlyMinor: number | null | undefined,
  currency: string | undefined,
  locale: string,
  zh: boolean,
) {
  const known = (nightlyMinor ?? 0) > 0 || (totalMinor ?? 0) > 0;
  if (!known) {
    return (
      <strong data-testid="stay-price-unknown">
        {zh ? "价格未知" : "Price unavailable"}
        <small>{zh ? "（供应商未提供房价）" : " (no provider rate)"}</small>
      </strong>
    );
  }
  return (
    <strong data-testid="stay-price-known">
      {money(totalMinor, currency, locale)}
      <small>
        {zh ? " 合计 · " : " total · "}
        {money(nightlyMinor, currency, locale)}
        {zh ? " / 晚" : " / night"}
      </small>
    </strong>
  );
}

function roomLabel(roomType: string | undefined, zh: boolean): string {
  const value = String(roomType ?? "").trim().toLowerCase();
  if (!value) return zh ? "房型未知" : "Room type unknown";
  if (value === "hotel" || value === "hostel" || value === "guest_house" || value === "apartment") {
    return zh ? "酒店" : "Hotel";
  }
  return roomType as string;
}

function modeLabel(mode: string | undefined, zh: boolean): string {
  const table: Record<string, [string, string]> = {
    WALK: ["步行", "Walk"], BICYCLE: ["骑行", "Bicycle"], TRANSIT: ["公交地铁", "Transit"],
    AUTO: ["驾车", "Drive"], RIDE_HAIL: ["网约车", "Ride hail"],
  };
  const hit = table[String(mode ?? "").toUpperCase()];
  return hit ? (zh ? hit[0] : hit[1]) : String(mode ?? "—");
}
function date(value?: string, locale = "en") { return value ? new Intl.DateTimeFormat(locale === "zh-CN" ? "zh-CN" : "en-GB", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value)) : "—"; }

/** Is this string a zone `Intl` will accept?  Same rule the shell uses, kept local so this file
 *  does not import the shell's component module back. */
function zoneIsUsable(zone: string | undefined | null): boolean {
  if (!zone) return false;
  try { new Intl.DateTimeFormat("en-GB", { timeZone: zone }); return true; } catch { return false; }
}

/** A check-in/check-out is a committed instant at the stay's own place, so it is read in the
 *  journey's zone -- not the device's -- and says which zone when that zone can be used. */
function dateInZone(value: string | undefined, locale: string, zone: string) {
  if (!value) return "—";
  try {
    return new Intl.DateTimeFormat(locale === "zh-CN" ? "zh-CN" : "en-GB", { timeZone: zone, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
  } catch {
    return date(value, locale) + (locale === "zh-CN" ? " · 本设备时间" : " · this device's time");
  }
}

// A property with no real captured evidence must not be dressed up as an
// evidence card.  Absence is reported as absence, never as a 0% confidence
// reading that still looks like provider output.
function priceLabel(
  minor: number | null | undefined,
  currency: string | undefined,
  locale: string,
  zh: boolean,
): string {
  // A missing OR zero amount is not a price.  OSM carries no rate, so a live
  // stay arrives as 0; printing "CNY 0.00" would read as a free room.
  if (minor === null || minor === undefined || minor <= 0) {
    return zh
      ? "价格未知（供应商未提供房价）"
      : "Price unknown (no provider rate)";
  }
  return `${money(minor, currency, locale)} · ${currency || "CNY"}`;
}

function cancellationLabel(summary: string | null | undefined, zh: boolean): string {
  const text = String(summary ?? "").trim();
  if (!text) return zh ? "退改政策未知" : "Cancellation terms unknown";
  // The provider returns a machine-facing English summary; a Chinese screen
  // must not print it raw.
  if (/non-refundable/i.test(text)) return zh ? "不可取消" : "Non-refundable";
  if (/free cancellation/i.test(text)) return zh ? "入住前可免费取消" : "Free cancellation before arrival";
  return text;
}

function evidenceLabel(option: V8Data["options"][number] | null | undefined, zh: boolean): string {
  const confidence = option?.stay?.evidence?.confidence;
  if (typeof confidence !== "number" || confidence <= 0) {
    return zh ? "暂无供应商证据" : "No provider evidence";
  }
  return zh
    ? `供应商证据置信度 ${Math.round(confidence * 100)}%`
    : `Provider evidence confidence ${Math.round(confidence * 100)}%`;
}

/**
 * ORD-0511 -- the stops this trip marks as protected, said in the traveller's own words.
 *
 * The flag is the product's existing one; this only reads names out of the plan the shell already
 * holds, so 受保护 is not given a third meaning here. Identifiers are deliberately not returned: an
 * id on this screen is exactly what the order forbids.
 */
function protectedStationNames(data: V8Data): string[] {
  const names: string[] = [];
  for (const item of data.active_plan?.activities ?? []) {
    if (!item.protected && !item.locked) continue;
    const title = String(item.title ?? "").trim();
    if (title && !names.includes(title)) names.push(title);
  }
  return names;
}

/**
 * A refusal in words, keyed on the envelope's own `reason_code` rather than on the English sentence
 * the server keeps for its logs. An unnamed refusal says it is unnamed -- it does not borrow a
 * neighbour's wording to look resolved.
 */
function refusalSentence(
  door: { http_status: number; reason_code: string | null },
  zh: boolean,
): string {
  const table: Record<string, [string, string]> = {
    GENERATION_STORE_BUSY: [
      "这台服务器此刻正在写另一批住宿数据，所以这一轮没有落账，也没有生成任何候选。",
      "This server is writing another batch of stay data right now, so this run was not recorded and produced no candidates.",
    ],
    GENERATION_ABANDONED: [
      "这一轮生成被放弃了，所以它没有交回结果。",
      "This generation run was abandoned, so it returned no result.",
    ],
    STAY_OPTION_UNKNOWN: [
      "这台服务器没有过这一枚住宿候选，所以它没有被排进这趟行程。",
      "This server has no such stay candidate for this trip, so nothing was scheduled.",
    ],
    STAY_ITINERARY_CONFLICT: [
      "这家店的入住与退房时间，和你已经确认的行程不能同时成立，所以这一枚没有排进去。",
      "This property's check-in and check-out windows cannot coexist with the itinerary you already confirmed, so it was not scheduled.",
    ],
    STAY_NOT_AUTHORITATIVE: [
      "这一枚没有通过这趟行程的硬条件，所以它没有成为可用的安排。",
      "This stay did not clear this trip's hard conditions, so it did not become a usable arrangement.",
    ],
  };
  const hit = door.reason_code ? table[door.reason_code] : undefined;
  if (hit) return zh ? hit[0] : hit[1];
  return zh
    ? `这台服务器拒了这一次，但没有给这一屏一个认得出的拒绝名（回到的状态码是 ${door.http_status}）。`
    : `This server refused the request without a name this screen recognises (status ${door.http_status}).`;
}

/**
 * ORD-0511 -- what this screen may say about the lodging budget.
 *
 * Measured, not assumed: no planner leg in this product prices a stay against
 * `stay_budget_amount_minor` (a declared ¥80 budget still ranked a ¥360 stay with an empty
 * `rejections_by_reason`), so the comparison here is stated as this screen subtracting two numbers it
 * was given. The server's own budget vocabulary stays where it belongs -- in
 * `stayOutcomeCopy(STAY_BUDGET_NOT_DECLARED)` -- and this never claims a rejection the server did not
 * make. `declared` is three-state on purpose: absent is not false.
 */
function stayBudgetSentence(
  data: V8Data,
  confirmed: V8Data["options"][number] | null,
  zh: boolean,
): string | null {
  const stated = (data.trip as { stay_budget?: { declared?: boolean | null; amount_minor?: number | null; currency?: string } })
    .stay_budget;
  if (!stated || stated.declared === null || stated.declared === undefined) {
    return zh
      ? "这台服务器没有交回「这条行程是否声明过住宿预算」这一格，所以这里不提预算。"
      : "This server did not answer whether a lodging budget was declared, so no budget is claimed here.";
  }
  if (stated.declared === false) {
    return zh
      ? "这条行程没有声明住宿预算，所以这里没有一条预算界线可比。"
      : "No lodging budget was declared for this trip, so there is no line to compare against.";
  }
  const amount = typeof stated.amount_minor === "number" ? stated.amount_minor : null;
  const currency = stated.currency || "CNY";
  if (amount === null) {
    return zh
      ? "这条行程声明过住宿预算，但这台服务器没有交回那个数额。"
      : "This trip declared a lodging budget, but this server did not return the amount.";
  }
  const declaredWords = `${money(amount, currency, localeOf(zh))}`;
  if (!confirmed) {
    return zh
      ? `这条行程声明的住宿预算是 ${declaredWords}；还没有选定住宿，所以这里不做超出判断。`
      : `This trip declared a lodging budget of ${declaredWords}; nothing is selected, so no comparison is made here.`;
  }
  const total = confirmed.stay_total_minor;
  if (typeof total !== "number") {
    return zh
      ? `这条行程声明的住宿预算是 ${declaredWords}；这枚候选的合计数这台服务器没给，所以不说超出。`
      : `Declared lodging budget ${declaredWords}; this server gave no total for this stay, so nothing is called over.`;
  }
  if (total > amount) {
    const over = `${money(total - amount, currency, localeOf(zh))}`;
    return zh
      ? `这枚合计 ${money(total, currency, localeOf(zh))}，比声明的 ${declaredWords} 多 ${over}。这一句是本屏把拿到的两个数相减，不是这台服务器的判定。`
      : `This stay totals ${money(total, currency, localeOf(zh))}, which is ${over} above the declared ${declaredWords}. That subtraction is this screen's own arithmetic, not a ruling by this server.`;
  }
  return zh
    ? `这枚合计 ${money(total, currency, localeOf(zh))}，不超过声明的 ${declaredWords}。同样是本屏自己的相减，不是这台服务器的判定。`
    : `This stay totals ${money(total, currency, localeOf(zh))}, within the declared ${declaredWords} -- again this screen's subtraction, not this server's ruling.`;
}

function localeOf(zh: boolean): string {
  return zh ? "zh-CN" : "en";
}
