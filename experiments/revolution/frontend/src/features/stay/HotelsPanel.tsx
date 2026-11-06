/**
 * Hotel comparison and selection (reference 09).
 *
 * Rules honoured:
 *  - only provider-backed facts are shown: name, area/coordinates, walking
 *    burden and the relationship to the itinerary. **Price is shown as
 *    "price unavailable" when the provider does not supply one** — OSM has no
 *    rates, so inventing a nightly price is forbidden;
 *  - selecting a stay calls the authoritative stay-option select endpoint so
 *    the Trip view reflects the chosen property;
 *  - an honest empty state when no provider-backed property exists.
 */

import { useEffect, useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import { namedCodeIn, readResponseFacts, type RawFacts } from "../../lib/rawFacts";
import { RawFactsPanel } from "../../components/RawFactsPanel";

type StayOption = {
  id: string;
  rank: number;
  status: string;
  stay_total_minor: number;
  walking_min: number;
  protected_margin_min: number;
  arrival_fit: number;
  departure_fit: number;
  cancellation_score: number;
  hard_constraints_passed: boolean;
  stay: {
    property_name: string;
    room_type: string;
    total_amount_minor: number;
    nightly_amount_minor: number;
    currency: string;
    refundable: boolean;
    location: { latitude: number; longitude: number; crs?: string };
    evidence?: { source?: string; confidence?: number };
  };
};

/** The refusals this screen can actually explain to a traveller, named by the token the endpoint
 *  answers with.  A code outside this list gets the general refusal: this screen has no wording for
 *  a condition it was never told about, and quoting the server's sentence for one is what put an
 *  upstream body into the DOM (Q64=1). */
const EXPLAINABLE_REFUSALS = ["STAY_ITINERARY_CONFLICT", "STAY_PINNED_CONFLICT"];

export function HotelsPanel({ journeyId, onSelected }: { journeyId: string; onSelected?: () => void }) {
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const [options, setOptions] = useState<StayOption[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [banner, setBanner] = useState<{ text: string | null; facts: RawFacts | null }>({
    text: null,
    facts: null,
  });
  /** The sentence and the reading are announced together, so a later attempt can never leave the
   *  previous attempt's fields on screen beside it. */
  const error = banner.text;
  const setError = (text: string | null, facts: RawFacts | null = null) =>
    setBanner({ text, facts });
  const [chosen, setChosen] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        // Read first: a previous capture may already have produced options.
        const read = async () => {
          const r = await fetch(`/api/v1/journeys/${journeyId}/experience`);
          const body = await r.json();
          return (body?.options ?? []) as StayOption[];
        };
        let list = await read();
        if (!list.length) {
          // Acquisition is a live provider call and can be slow; only pay for
          // it when nothing has been captured yet.
          try {
            await fetch(`/api/v1/journeys/${journeyId}/stays/offers/capture`, { method: "POST" });
            await fetch(`/api/v1/journeys/${journeyId}/stay-options/generate`, { method: "POST" });
            list = await read();
          } catch {
            /* fall through to the honest empty state */
          }
        }
        if (cancelled) return;
        setOptions(list);
        const sel = list.find((o) => o.status === "SELECTED");
        setChosen(sel ? sel.id : null);
      } catch {
        if (!cancelled) setOptions([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [journeyId]);

  const select = async (optionId: string) => {
    setBusy(optionId);
    setError(null);
    try {
      const r = await fetch(
        `/api/v1/journeys/${journeyId}/stay-options/${optionId}/select`,
        { method: "POST" },
      );
      if (!r.ok) {
        /* A hotel whose check-in / check-out windows cannot coexist with the confirmed itinerary is a
           real conflict, and the endpoint names it in the answer's leading token
           (`detail: "STAY_ITINERARY_CONFLICT: STAY_PINNED_CONFLICT:..."`).  The token is what this
           screen decides on; the sentence around it is dropped, because since Q64=1 no upstream body
           may reach the DOM and a traveller-facing refusal is ours to word.  What stays visible is
           this device's own reading of the answer, below. */
        const { text, facts } = await readResponseFacts(r);
        const isConflict =
          r.status === 422 && namedCodeIn(text, EXPLAINABLE_REFUSALS) !== null;
        setError(
          isConflict
            ? zh
              ? "这家住宿的入住/退房时间和你已确认的行程冲突，无法在不改动行程的情况下采用。换一家试试。"
              : "This stay's check-in / check-out windows clash with your confirmed itinerary, so it cannot be used without changing your trip. Try another one."
            : zh
              ? "暂时无法选择这家住宿。"
              : "Could not choose this stay.",
          facts,
        );
        return;
      }
      setChosen(optionId);
      onSelected?.();
    } catch {
      setError(zh ? "连不上服务器，请重试。" : "Could not reach the server. Try again.");
    } finally {
      setBusy(null);
    }
  };

  if (options === null) {
    return (
      <section className="v8-page" data-testid="hotels-panel">
        <p className="v8-muted">{zh ? "正在获取住宿…" : "Loading stays…"}</p>
      </section>
    );
  }

  if (options.length === 0) {
    return (
      <section className="v8-page" data-testid="hotels-empty">
        <div className="v8-page-intro compact">
          <div>
            <p className="v8-kicker">{zh ? "住宿" : "Stay"}</p>
            <h1>{zh ? "暂时没有可比较的住宿" : "No stays to compare yet"}</h1>
            <p className="v8-lede">
              {zh
                ? "这个城市还没有拿到可用的住宿数据。换个城市或稍后再试——我们不会编造酒店。"
                : "No provider-backed accommodation is available for this city yet. Try again later — we will not invent hotels."}
            </p>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="v8-page" data-testid="hotels-panel">
      <div className="v8-page-intro compact">
        <div>
          <p className="v8-kicker">{zh ? "住宿" : "Stay"}</p>
          <h1>{zh ? "选一家住的地方" : "Choose where to stay"}</h1>
          <p className="v8-lede">
            {zh
              ? `按与行程的关系排序的 ${options.length} 家住宿。价格未知时我们如实说明。`
              : `${options.length} stays ranked by how they fit your itinerary. Where price is unknown we say so.`}
          </p>
        </div>
      </div>

      <div className="hotel-list">
        {options.map((option) => {
          const stay = option.stay;
          const priceKnown = (stay.nightly_amount_minor ?? 0) > 0;
          const isChosen = chosen === option.id;
          return (
            <article
              key={option.id}
              className={`hotel-card${isChosen ? " chosen" : ""}`}
              data-testid="hotel-card"
              data-property={stay.property_name}
            >
              <header>
                <b data-testid="hotel-name">{stay.property_name}</b>
                {isChosen && (
                  <span className="hotel-badge" data-testid="hotel-chosen">
                    {zh ? "已选择" : "Selected"}
                  </span>
                )}
              </header>
              <dl>
                <div>
                  <dt>{zh ? "价格" : "Price"}</dt>
                  <dd data-testid="hotel-price">
                    {priceKnown
                      ? `${stay.currency} ${Math.round(stay.nightly_amount_minor / 100)} / ${zh ? "晚" : "night"}`
                      : zh
                        ? "暂无价格"
                        : "Price unavailable"}
                  </dd>
                </div>
                <div>
                  <dt>{zh ? "步行" : "Walking"}</dt>
                  <dd>{option.walking_min}{zh ? " 分钟" : " min"}</dd>
                </div>
                <div>
                  <dt>{zh ? "到达/离开衔接" : "Arrival / departure fit"}</dt>
                  <dd>{option.arrival_fit}% / {option.departure_fit}%</dd>
                </div>
                <div>
                  <dt>{zh ? "退改" : "Cancellation"}</dt>
                  <dd>{stay.refundable ? (zh ? "可取消" : "Refundable") : zh ? "不可取消" : "Non-refundable"}</dd>
                </div>
              </dl>
              <p className="hotel-reason">
                {zh ? "推荐理由：" : "Why: "}
                {option.hard_constraints_passed
                  ? zh
                    ? `与行程冲突最少，步行负担 ${option.walking_min} 分钟，预留缓冲 ${option.protected_margin_min} 分钟。`
                    : `Fewest itinerary conflicts, ${option.walking_min} min walking, ${option.protected_margin_min} min protected margin.`
                  : zh
                    ? "与当前硬约束存在冲突，仅供参考。"
                    : "Conflicts with current hard constraints; shown for reference only."}
              </p>
              <button
                type="button"
                className="v8-primary-button"
                data-testid="hotel-choose"
                disabled={busy !== null || isChosen}
                onClick={() => void select(option.id)}
              >
                {isChosen
                  ? zh ? "已选择这家" : "Selected"
                  : busy === option.id
                    ? zh ? "选择中…" : "Choosing…"
                    : zh ? "选择这家住宿" : "Choose this stay"}
              </button>
            </article>
          );
        })}
      </div>
      {error && (
        <p className="role-alert" data-testid="hotel-error" role="alert">{error}</p>
      )}
      {banner.facts && (
        <RawFactsPanel facts={banner.facts} zh={zh} testId="hotel-raw-facts" />
      )}
    </section>
  );
}
