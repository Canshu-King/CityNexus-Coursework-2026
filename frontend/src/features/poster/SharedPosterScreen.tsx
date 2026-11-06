/**
 * ORD-0528 — the other end of a shared poster link.
 *
 * A share link can now ask for one of two shapes: the trip (the screen ORD-0465 built, where a guest
 * reads the journey, its map and its plan line) or this poster (one block per day, and a picture that
 * can be saved). The address tail picks the shape; the *authorization* is still and only the token,
 * read at the same door this order did not touch — `GET /api/v1/shared/journeys/{token}`. No second
 * door, no second token, and no claim here that a poster is a different kind of access: it is a
 * different rendering of the same grant, so revoking the grant closes both shapes at once.
 *
 * Two rules this screen is built under:
 *
 *  1. Every door state keeps its own sentence, and this order adds no second copy of one. The loading,
 *     no-token, read-failure, refusal and empty-plan lines all come from the `shared.*` keys
 *     `SharedJourneyPanel.tsx` already owns, and the nine refusal names come from its exported table.
 *     A shape difference is a layout difference, not a different fact about the server.
 *  2. What a guest may not do here is act. There is no map, no plan-source line, no advance/replan
 *     affordance and no token painted — the same rule the trip screen states for screenshots. The
 *     only thing this page hands over is a PNG of the days the grant let the guest read.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import { CnxMeasuredFacts } from "../../components/CnxMeasuredFacts";
import { copyFor } from "../../lib/failureAttribution";
import { REFUSAL_KEY } from "../shared/SharedJourneyPanel";
import { askSharedJourney, sharedStopsOf, type SharedScreenRead } from "../shared/sharedJourneyDoors";
import { buildPosterFromShared, posterLinesFor } from "./posterModel";
import { drawTripPoster, planPosterPages, savePosterBlob } from "./posterCanvas";
/* The notice/head/details styles this screen's four non-ready arms reuse are ORD-0465's, and it
   imports its own sheet: a poster page is a different surface, and it should not render only because
   some other module happened to pull the same stylesheet into the bundle. */
import "../shared/sharedJourney.css";
import "./trip-poster.css";

const SHARED_POSTER_FILE_NAME = "citynexus-shared-poster.png";

/* The drawing layer's `detail` is again not rendered (Q64): the picture either came out or the line
   says which step of our own drawing failed, in dictionary words. */
const DRAWN_KEY = { made: "sharedPoster.made", failed: "sharedPoster.failed" } as const;

export function SharedPosterScreen({ token }: { token: string }) {
  const { locale, t, switchLocale } = useLocale();
  const zh = locale.startsWith("zh");
  const trimmed = token.trim();
  const [read, setRead] = useState<SharedScreenRead>(
    trimmed ? { state: "loading" } : { state: "no_token_in_address" },
  );
  const [attempt, setAttempt] = useState(0);
  const [note, setNote] = useState<"idle" | "working" | "made" | "failed">("idle");

  useEffect(() => {
    if (!trimmed) {
      setRead({ state: "no_token_in_address" });
      return;
    }
    let retired = false;
    setRead({ state: "loading" });
    void askSharedJourney(trimmed).then((next) => {
      if (!retired) setRead(next);
    });
    return () => {
      retired = true;
    };
  }, [trimmed, attempt]);

  const answer = read.state === "ready" ? read.answer : null;
  const timeZone = answer?.timezone || "UTC";
  const stops = answer ? sharedStopsOf(answer) : [];

  /* The grouping the traveller's own itinerary and the traveller's own poster use, so the guest looks
     at the trip's days rather than at a second calendar this file invented. `dayTotal` stays null on
     this side -- the guest door answers no `temporal_shape` (it is not in its contract whitelist,
     `frontend/src/generated/api.ts`) -- but ORD-0542 caught what the old wording did with that:
     the subtitle said "trip length unknown" while the line under it printed a counted "2 days". The
     count is the subtitle now, and the line under it names the count's provenance. Neither one fills
     in a projection the door did not send. */
  const poster = useMemo(
    () => buildPosterFromShared({ answer, stops, timeZone, locale, t }),
    [answer, stops, timeZone, locale, t],
  );
  const lines = useMemo(
    () => (poster ? posterLinesFor(poster, locale, t) : null),
    [poster, locale, t],
  );

  const refusalKey =
    read.state === "refused" ? REFUSAL_KEY[read.refusal] ?? REFUSAL_KEY.unnamed : null;

  /* The guest's page has to obey the same bound the traveller's sheet obeys, and the only honest way
     to do that is to use the same splitter with the same inputs: this page's own measured width, not
     a number typed into this file. A hardcoded 360 here would draw a different picture on a desktop
     than the one the guest is looking at, and would silently clip a trip long enough to exceed the
     bound -- which is the exact defect ORD-0515 closed on the traveller's side. */
  const list = useRef<HTMLOListElement | null>(null);
  const [listWidth, setListWidth] = useState(0);
  const [page, setPage] = useState(0);
  useLayoutEffect(() => {
    if (list.current) setListWidth(list.current.getBoundingClientRect().width);
  }, [lines]);

  const planned = lines && listWidth > 0 ? planPosterPages(lines, listWidth) : null;
  const pages = planned && "pages" in planned ? planned.pages : null;
  const pageCount = pages ? pages.length : 1;
  const currentPage = Math.min(page, pageCount - 1);
  const slice = pages ? pages[currentPage] : null;
  const pageLines =
    lines && slice ? { ...lines, days: lines.days.slice(slice.first, slice.first + slice.count) } : null;

  const download = useCallback(async () => {
    if (!lines) return;
    setNote("working");
    const result = await drawTripPoster(lines, listWidth, currentPage);
    if (!("blob" in result) || !result.blob) {
      setNote("failed");
      return;
    }
    const fileName =
      result.page_count > 1
        ? SHARED_POSTER_FILE_NAME.replace(/\.png$/, `-p${result.page_index + 1}.png`)
        : SHARED_POSTER_FILE_NAME;
    setNote(savePosterBlob(result.blob, fileName) ? "made" : "failed");
  }, [lines, listWidth, currentPage]);

  const shell = (state: string, body: ReactNode) => (
    <div className="cn-shared-poster" data-testid="shared-poster-screen" data-poster-state={state}>
      <header className="cn-shared-head">
        <span className="v8-kicker" data-testid="shared-poster-kicker">
          {t("sharedPoster.kicker")}
        </span>
        <button
          type="button"
          className="v8-quiet-button cn-shared-language"
          data-testid="shared-poster-language"
          data-contrast-role="control"
          onClick={switchLocale}
        >
          {t("nav.language")}
        </button>
      </header>
      {body}
    </div>
  );

  if (read.state === "loading") {
    return shell(
      read.state,
      <p className="v8-muted" role="status" data-testid="shared-poster-loading">
        {t("shared.loading")}
      </p>,
    );
  }

  if (read.state === "no_token_in_address") {
    return shell(
      read.state,
      <div className="cn-shared-notice" role="alert" data-testid="shared-poster-no-token">
        <b>{t("shared.noTokenTitle")}</b>
        <p>{t("shared.noTokenInAddress")}</p>
        <p className="v8-muted">{t("shared.noTokenWhy")}</p>
      </div>,
    );
  }

  if (read.state === "read_failed") {
    return shell(
      read.state,
      <div className="cn-shared-notice" role="alert" data-testid="shared-poster-read-failed">
        <p>{copyFor(read.attribution, zh).body}</p>
        {read.facts ? (
          <details className="cn-shared-details" open>
            <summary>{t("agent.advancedDetail")}</summary>
            <CnxMeasuredFacts facts={read.facts} zh={zh} testId="shared-poster-read-failed" envelope={read.envelope} />
          </details>
        ) : null}
        <button
          type="button"
          className="v8-quiet-button"
          data-testid="shared-poster-refresh"
          data-contrast-role="control"
          onClick={() => setAttempt((n) => n + 1)}
        >
          {t("shared.refresh")}
        </button>
      </div>,
    );
  }

  if (read.state === "refused" && refusalKey) {
    return shell(
      read.state,
      <div
        className="cn-shared-notice"
        role="alert"
        data-testid="shared-poster-refused"
        data-shared-code={read.reason_code ?? undefined}
      >
        <b data-testid="shared-poster-refusal-title">{t(refusalKey)}</b>
        <details className="cn-shared-details" open>
          <summary>{t("agent.advancedDetail")}</summary>
          <p className="cn-shared-code-label">{t("shared.refusalCodeLabel")}</p>
          <code data-testid="shared-poster-refusal-code">
            {read.reason_code ?? t("shared.refusalCodeNone")}
          </code>
          {read.facts ? (
            <CnxMeasuredFacts facts={read.facts} zh={zh} testId="shared-poster-refused" envelope={read.envelope} />
          ) : null}
        </details>
        <button
          type="button"
          className="v8-quiet-button"
          data-testid="shared-poster-refresh"
          data-contrast-role="control"
          onClick={() => setAttempt((n) => n + 1)}
        >
          {t("shared.refresh")}
        </button>
      </div>,
    );
  }

  /* A read that answered 200 can still have nothing to draw, and the three ways are three facts. They
     are the trip screen's three sentences, reused: this shape changes what a page looks like, not what
     the server said. */
  if (!lines) {
    const emptyKey =
      answer?.plan_state === "PLAN_ROW_MISSING"
        ? "shared.planRowMissing"
        : answer?.plan_state === "NO_PLAN" || !answer
          ? "shared.noPlan"
          : null;
    if (emptyKey) {
      return shell(
        "ready_no_plan",
        <div className="cn-shared-notice" role="status" data-testid="shared-poster-no-plan">
          <p>{t(emptyKey)}</p>
        </div>,
      );
    }
    return shell(
      "ready_empty",
      <div className="cn-shared-notice" role="status" data-testid="shared-poster-plan-empty">
        <b>{t("shared.planEmptyTitle")}</b>
        <p>{t("shared.planEmptyBody")}</p>
      </div>,
    );
  }

  return shell(
    read.state,
    <>
      <h1 className="cn-shared-title" data-testid="shared-poster-title">
        {lines.title}
      </h1>
      <p className="v8-muted" data-testid="shared-poster-subtitle">
        {lines.subtitle}
      </p>
      {/* The picture carries this line, so the page has to show it too: ORD-0542 is precisely the
          finding that a line can be true in the drawing and absent from the screen. */}
      <p className="v8-muted" data-testid="shared-poster-counted-why">
        {lines.progress}
      </p>
      {/* The line the trip screen cannot carry, and the reason the two shapes do not read alike: this
          page says out loud that it is the picture, and that the trip itself is elsewhere. */}
      <p className="cn-poster-form-note" data-testid="shared-poster-form-note">
        {t("sharedPoster.formNote")}
      </p>

      {/* The same bound sentence the traveller's sheet uses -- one owner for the sentence that says
          "this is too tall for one picture", because the bound is a product rule and not a per-screen
          opinion. */}
      {pageCount > 1 && (
        <>
          <p className="cn-poster-paged" data-testid="shared-poster-paged-note">
            {t("poster.paged", { pages: pageCount })}
          </p>
          <div className="cn-poster-pages" data-testid="shared-poster-pages" role="group">
            {Array.from({ length: pageCount }, (_, index) => (
              <button
                key={index}
                type="button"
                className={index === currentPage ? "cn-poster-page is-current" : "cn-poster-page"}
                data-testid={`shared-poster-page-${index + 1}`}
                data-contrast-role="control"
                aria-pressed={index === currentPage}
                onClick={() => {
                  setPage(index);
                  setNote("idle");
                }}
              >
                {t("poster.page", { page: index + 1 })}
              </button>
            ))}
          </div>
        </>
      )}

      <ol className="cn-poster-days" data-testid="shared-poster-days" ref={list}>
        {(pageLines ?? lines).days.map((day, index) => (
          <li className="cn-poster-day" key={`${day.label}-${index}`} data-testid={`shared-poster-day-${index + 1}`}>
            <div className="cn-poster-day-head">
              <b>{day.label}</b>
              <small>{day.count}</small>
            </div>
            <ul className="cn-poster-stops">
              {day.stops.map((stop, stopIndex) => (
                <li
                  className="cn-poster-stop"
                  key={`${day.label}-${stopIndex}`}
                  data-testid="shared-poster-stop"
                >
                  <time className="cn-poster-stop-time">{stop.time}</time>
                  <span className="cn-poster-stop-title">{stop.title}</span>
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ol>

      <p className="cn-poster-attribution" data-testid="shared-poster-attribution">
        {lines.attribution}
      </p>

      <button
        type="button"
        className="v8-primary-button cn-poster-download"
        data-testid="shared-poster-download"
        data-contrast-role="control"
        disabled={note === "working"}
        onClick={() => {
          void download();
        }}
      >
        {t("sharedPoster.download")}
      </button>
      {note !== "idle" && note !== "working" && (
        <p className="cn-poster-outcome" data-testid="shared-poster-note" data-state={note}>
          {t(DRAWN_KEY[note])}
        </p>
      )}
      {note === "made" && pageCount > 1 && (
        <p className="cn-poster-outcome" data-testid="shared-poster-page-result">
          {t("poster.madePage", { page: currentPage + 1, pages: pageCount })}
        </p>
      )}
    </>,
  );
}
