import { Icon } from "../../components/v8/Icon";
import { MapPanel } from "../../components/v8/MapPanel";
import type { V8Data } from "../../app/v8Types";
import { useT, useLocale } from "../../i18n/LocaleContext";
import { formatMinorMoney } from "../../lib/money";

export function StayExplorer({ data, selected, onSelect }: { data: V8Data; selected: V8Data["options"][number] | null; onSelect: (id: string) => void }) {
  const t = useT();
  const { locale } = useLocale();
  const zh = locale === "zh-CN";
  // A city label derived from the Journey, never a hard-coded demo city.
  const cityLabel = String(data.journey.city_code || "").split("-").pop() || "";
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
        </div>
      </section>
    );
  }
  return <section className="v8-page" data-testid="v8-stay-explorer">
    <div className="v8-page-intro compact"><div><p className="v8-kicker">{t("stay.kicker")}</p><h1>{t("stay.title")}</h1><p className="v8-lede">{t("stay.lede")}</p></div><span className="v8-evidence-label"><span className="status-dot" />{t("stay.capturedReplay")}</span></div>
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
              <dd>{date(selected?.stay.check_in_at, locale)} → {date(selected?.stay.check_out_at, locale)}</dd>
            </div>
            <div>
              <dt>{t("stay.cancellation")}</dt>
              <dd>{cancellationLabel(selected?.stay.cancellation_policy?.summary, zh)}</dd>
            </div>
            <div>
              <dt>{t("stay.provider")}</dt>
              <dd>{evidenceLabel(selected, zh)}</dd>
            </div>
          </dl></div></div>
    </div>
  </section>;
}

function money(minor: number | null | undefined, currency = "CNY", locale = "en") { return formatMinorMoney(minor, currency, locale === "zh-CN" ? "zh-CN" : "en-US"); }

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
