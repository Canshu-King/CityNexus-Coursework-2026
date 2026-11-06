/**
 * ORD-0472 — the trip poster entry, and the sheet it opens.
 *
 * One quiet bar at the top of the Trips > Itinerary screen. Pressing it opens the poster sheet, which
 * shows what the picture contains -- the trip's own title, its city, the day count the panel already
 * quotes, every stop on its own day with the times the panel already prints, and the attribution line
 * the map carries. Pressing it again draws that same list onto a canvas and hands back one PNG.
 *
 * The bar renders only when a plan with at least one stop exists, so the sheet can never open onto an
 * empty poster. The list the sheet renders and the list the canvas draws are the same value, built
 * once here: a picture of something else than the traveller is reading would be a second answer.
 */
import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";

import {
  drawTripPoster,
  planPosterPages,
  savePosterBlob,
  type PosterFailureStep,
  type PosterLines,
} from "./posterCanvas";
import { buildTripPoster, posterLinesFor } from "./posterModel";
import { sharePosterLink, type PosterShareOutcome } from "./posterShare";
import type { V8Data } from "../../app/v8Types";
import "./trip-poster.css";

const POSTER_FILE_NAME = "citynexus-trip-poster.png";

/* Which sentence each non-confirmation answers, spelled out: a composed key would print
   `poster.shareErr...` on the screen if a reason ever arrived without a row here. */
const SHARE_FAILURE_KEYS = {
  CREATE_REFUSED: "poster.shareErrRefused",
  NO_TOKEN: "poster.shareErrNoToken",
  NOT_IN_SERVER_READ: "poster.shareErrNotListed",
  REVOKED_IN_SERVER_READ: "poster.shareErrRevoked",
  READ_NOT_ANSWERED: "poster.shareErrReadUnanswered",
} as const;

/* Which sentence each step answers, spelled out rather than built from the step name: `t()` returns
   the key itself when a key is missing, so a composed key could put `poster.fail.MissingTokens` on
   the screen. Written as a table, the compiler checks every step has words and none of them can leak.

   The drawing layer's own `detail` (a byte count, `null`, a pixel width) is deliberately not
   rendered and not put in the DOM at all: the line a traveller reads is the dictionary sentence and
   nothing else, which is what the Q64 arm caught me breaking -- the first version appended
   ` · null` to "长图没有生成：画布没有交出 PNG 字节。". The detail still comes back on the result
   object for whoever instruments the canvas, it just never reaches the screen. */
const FAILURE_COPY: Record<PosterFailureStep, string> = {
  NoCanvasContext: "poster.fail.noCanvas",
  NoMeasuredWidth: "poster.fail.noWidth",
  MissingTokens: "poster.fail.missingTokens",
  BlankAfterDraw: "poster.fail.blank",
  NoPngBytes: "poster.fail.noBytes",
  NoDownloadRoute: "poster.fail.noDownload",
};

type DrawState =
  | { phase: "idle" }
  | { phase: "working" }
  | { phase: "made"; bytes: number; width: number; height: number }
  | { phase: "failed"; step: PosterFailureStep };

export function TripPosterPanel({ data }: { data: V8Data }) {
  const { locale, t } = useLocale();
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<DrawState>({ phase: "idle" });
  const [share, setShare] = useState<PosterShareOutcome | null>(null);
  const [sharing, setSharing] = useState(false);
  const [page, setPage] = useState(0);
  /* The bound is expressed in CSS pixels, so the page count can only be decided once the sheet has
     a width on screen -- measured after it is in the DOM, not guessed from a breakpoint. */
  const [sheetWidth, setSheetWidth] = useState(0);
  const sheet = useRef<HTMLElement | null>(null);

  const poster = useMemo(() => buildTripPoster(data, locale), [data, locale]);

  /* One value for the screen and for the picture. Every string here is what the DOM already shows. */
  /* One builder for both ends of a share link: what the sheet lists and what the picture paints are
     the same value, and the guest's poster screen assembles it the same way. */
  const lines = useMemo<PosterLines | null>(
    () => (poster ? posterLinesFor(poster, locale, t) : null),
    [poster, locale, t],
  );

  /* Layout effect, not effect: with the plain one the first paint lists every day and the second
     collapses to page one, so a traveller watches the list jump. Measuring before the browser paints
     means the first thing on screen is already the paged thing. */
  useLayoutEffect(() => {
    if (!open || !sheet.current) return;
    setSheetWidth(sheet.current.getBoundingClientRect().width);
    setPage(0);
  }, [open, lines]);

  if (!poster || !lines) return null;

  /* One plan, read once: the panel slices by its entries and the encoder slices by the same
     entries, so "the blocks on this page" has exactly one answer. A second split recomputed from a
     page count would agree only until block heights stopped being uniform. */
  const planned = lines && sheetWidth > 0 ? planPosterPages(lines, sheetWidth) : null;
  const pages = planned && "pages" in planned ? planned.pages : null;
  const pageCount = pages ? pages.length : 1;
  const currentPage = Math.min(page, pageCount - 1);
  const slice = pages ? pages[currentPage] : null;
  const pageLines: PosterLines | null =
    lines && slice ? { ...lines, days: lines.days.slice(slice.first, slice.first + slice.count) } : null;

  const makePoster = async () => {
    setState({ phase: "working" });
    const measuredWidth = sheet.current ? sheet.current.getBoundingClientRect().width : 0;
    const result = await drawTripPoster(lines, measuredWidth, currentPage);
    /* Tested by which arm the value carries rather than by `ok`, because this project type-checks
       with `strict: false` (`tsconfig.typecheck.json`) and a boolean discriminant does not narrow
       a union here -- the failure fields would read as missing. */
    if ("step" in result) {
      setState({ phase: "failed", step: result.step });
      return;
    }
    const fileName =
      result.page_count > 1
        ? POSTER_FILE_NAME.replace(/\.png$/, `-p${result.page_index + 1}.png`)
        : POSTER_FILE_NAME;
    if (!savePosterBlob(result.blob, fileName)) {
      setState({ phase: "failed", step: "NoDownloadRoute" });
      return;
    }
    setState({
      phase: "made",
      bytes: result.blob.size,
      width: result.width,
      height: result.height,
    });
  };

  return (
    <>
      <div className="cn-poster-bar" data-testid="trip-poster-bar">
        <p className="cn-poster-bar-note">{t("poster.barNote")}</p>
        <button
          type="button"
          className="cn-poster-open"
          data-testid="poster-open"
          data-contrast-role="control"
          aria-expanded={open}
          onClick={() => setOpen((next) => !next)}
        >
          {open ? t("poster.close") : t("poster.action")}
        </button>
      </div>

      {open && (
        <section className="v8-page cn-poster" data-testid="trip-poster" ref={sheet}>
          <div className="v8-page-intro compact">
            <div>
              <p className="v8-kicker">{lines.kicker}</p>
              <h1 data-testid="poster-title">{lines.title}</h1>
              <p className="v8-lede" data-testid="poster-subtitle">
                {lines.subtitle}
              </p>
              <p className="v8-lede" data-testid="poster-progress">
                {lines.progress}
              </p>
            </div>
          </div>

          {pageCount > 1 && (
            <>
              <p className="cn-poster-paged" data-testid="poster-paged-note">
                {t("poster.paged", { pages: pageCount })}
              </p>
              <div className="cn-poster-pages" data-testid="poster-pages" role="group">
                {Array.from({ length: pageCount }, (_, index) => (
                  <button
                    key={index}
                    type="button"
                    className={index === currentPage ? "cn-poster-page is-current" : "cn-poster-page"}
                    data-testid={`poster-page-${index + 1}`}
                    data-contrast-role="control"
                    aria-pressed={index === currentPage}
                    onClick={() => {
                      setPage(index);
                      setState({ phase: "idle" });
                    }}
                  >
                    {t("poster.page", { page: index + 1 })}
                  </button>
                ))}
              </div>
            </>
          )}

          {/* The sheet lists the page it is on, not every day of the trip: a strip that pages the
              picture while the panel still shows all seven days would make the page buttons a
              caption on a picture the traveller cannot check. */}
          <ol className="cn-poster-days" data-testid="poster-days">
            {(pageLines ?? lines!).days.map((day, index) => (
              <li className="cn-poster-day" key={`${day.label}-${index}`} data-testid={`poster-day-${index + 1}`}>
                <div className="cn-poster-day-head">
                  <b>{day.label}</b>
                  <small>{day.count}</small>
                </div>
                <ul className="cn-poster-stops">
                  {day.stops.map((stop, stopIndex) => (
                    <li
                      className="cn-poster-stop"
                      key={`${day.label}-${stopIndex}`}
                      data-testid="poster-stop"
                    >
                      <time className="cn-poster-stop-time">{stop.time}</time>
                      <span className="cn-poster-stop-title">{stop.title}</span>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ol>

          <p className="cn-poster-attribution" data-testid="poster-attribution">
            {lines.attribution}
          </p>

          <button
            type="button"
            className="v8-primary-button cn-poster-download"
            data-testid="poster-download"
            data-contrast-role="control"
            disabled={state.phase === "working"}
            onClick={() => {
              void makePoster();
            }}
          >
            {state.phase === "working" ? t("poster.working") : t("poster.download")}
          </button>

          {/* ORD-0528: "share this picture" reuses the trip's own grant door -- it does not open a
              second write endpoint and does not mint a second token. The sentence below is only ever
              the server's list door agreeing that the row exists.
              ORD-0541: agreeing can happen two ways -- the list door already had a standing
              LINK_READ row, or this press wrote one and the read-back carries it. Those are
              different facts about what just happened to the traveller's trip, so they are two
              sentences, and neither is chosen by a client-side boolean: `reused` comes from which
              of the server's answers the link was taken out of. */}
          <button
            type="button"
            className="v8-quiet-button cn-poster-share"
            data-testid="poster-share"
            data-contrast-role="control"
            disabled={sharing}
            onClick={() => {
              setSharing(true);
              setShare(null);
              void sharePosterLink(data.journey.id, data.journey.traveler_id).then((outcome) => {
                setShare(outcome);
                setSharing(false);
              });
            }}
          >
            {sharing ? t("poster.sharing") : t("poster.share")}
          </button>

          {share && (
            <p
              className={share.state === "CONFIRMED" ? "cn-poster-outcome" : "cn-poster-outcome cn-poster-outcome--failed"}
              data-testid="poster-share-result"
              data-share-state={share.state}
            >
              {share.state === "CONFIRMED"
                ? t(share.reused ? "poster.shareReused" : "poster.shareDone", {
                    rows: share.rows_read,
                    grant: share.grant_id,
                  })
                : t(SHARE_FAILURE_KEYS[share.reason], { status: share.http_status })}
            </p>
          )}
          {share?.state === "CONFIRMED" && (
            <input
              className="cn-poster-link"
              data-testid="poster-share-link"
              readOnly
              value={`${window.location.origin}${share.path}`}
              aria-label={t("poster.shareLinkLabel")}
            />
          )}

          {state.phase === "made" && (
            <>
              <p className="cn-poster-outcome" data-testid="poster-result" data-state="made">
                {t("poster.made", {
                  bytes: state.bytes,
                  width: state.width,
                  height: state.height,
                })}
              </p>
              {pageCount > 1 && (
                <p className="cn-poster-outcome" data-testid="poster-page-result">
                  {t("poster.madePage", { page: currentPage + 1, pages: pageCount })}
                </p>
              )}
            </>
          )}
          {state.phase === "failed" && (
            <p
              className="cn-poster-outcome cn-poster-outcome--failed"
              data-testid="poster-result"
              data-state="failed"
            >
              {`${t("poster.failPrefix")}${t(FAILURE_COPY[state.step])}`}
            </p>
          )}
        </section>
      )}
    </>
  );
}
