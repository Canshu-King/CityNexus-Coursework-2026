import { useEffect, useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import { namedCodeIn } from "../../lib/rawFacts";
import { rejectionWithFacts, RejectionWithFacts } from "../../lib/rejectionFacts";
import { PoiCandidateCard } from "../../components/PoiCandidateCard";
/* ORD-0524 exit 2 borrows the container ORD-0444 built: the same two doors (the scoped list read and
   `POST /api/v1/collections/{folder_id}/items`), the same re-read-before-you-say-it rule, and the same
   shelf sentences -- so this screen does not open a third way to say 已收在, and no new write endpoint
   is invented for a batch. */
import {
  addPlaceToCollection,
  copyFor,
  readCollections,
  refusalCopy,
  shelvesHolding,
  type ReadOutcome,
} from "../collections/collections";
import { poiImportCopy, type PoiImportCopyKey } from "./poiImportCopy";
import "./poi-import.css";

/**
 * ORD-0437 — the second step of the POI entry chain: paste text or links, get places.
 *
 * Shapes are taken line by line from `D:/moodle/_apk_analysis_pi_travel/MAP/map_findings.md`
 * (the F24 chain), and the ones this build cannot honour are said out loud instead of being skipped:
 *
 *   two parallel input modes — 文本或链接识别 is wired; 截图识别 is presented and refused by name,
 *     because a screenshot needs OCR and this order ships none.
 *   the result card carries all three phases in one place — `loading`, `error` and `success` render
 *     inside the same region, and 重试 stays mounted in every one of them. A card that only shows a
 *     retry after failing is how a traveller ends up reloading the whole screen.
 *   a verification warning sits above the success list permanently — the extraction is ours, so the
 *     traveller, not the product, is the last authority on what a line meant.
 *   the automatic day is disclosed before it happens — not adding a chosen day means the plan's own
 *     first day, and the sentence says so next to the button rather than in the result.
 *   links are named, not fetched — the parse response carries `unsupported_forms` and `link_hosts`,
 *     and the panel prints both. This is the red line: extraction is our own local channel, so a
 *     小红书 or 公众号 link is reported as unread rather than quietly dropped.
 *   加入 is not a client-side colour — after the write, the record is read back from the server and
 *     only the ids that come back are called 已加入.
 *
 * ORD-0524 changes three shapes on this screen, and each one is a defect that was measured first:
 *
 *   a parse no longer ticks anything. The line below used to seed `selected` with every matched place,
 *     so a traveller who pressed 加进计划 without reading the cards wrote all of them; measured on the
 *     390 px arm at tip 1e95d106 as `已选 3 / 可加 3` with all three controls `aria-checked=true`
 *     before any press. Selecting everything is now a control the traveller presses.
 *   去处 is one radiogroup, not three independent buttons. Before, all three exits were painted at once
 *     and two of them were inert, so the screen offered a choice it did not keep. Choosing one now
 *     decides which write exists at all, and the third refuses by name instead of inventing a door.
 *   存到收藏 is wired. The sentence under it claimed 「这一版还没有收藏的门」 while `features/collections`
 *     had been shipping both doors since ORD-0444/ORD-0510 — the cell was false on its own line.
 */

type Item = {
  title: string;
  note: string | null;
  char_count: number;
  status: "RESOLVED" | "AMBIGUOUS" | "UNRESOLVED";
  place_id: string | null;
  place_name: string | null;
  latitude: number | null;
  longitude: number | null;
  coordinate_system: string | null;
  matches: string[];
  addable: boolean;
};

type ParseResponse = {
  source_channel: "LOCAL_TEXT_PARSER";
  forms: string[];
  unsupported_forms: string[];
  link_hosts: string[];
  counts: Record<string, number>;
  items: Item[];
  char_count: number;
};

type AddResponse = {
  journey_id: string;
  plan_version_id: string;
  day: string;
  day_source: string;
  added: { id: string; title: string; place_id: string }[];
  already_present: string[];
  readback_path: string;
};

type Phase = "idle" | "loading" | "error" | "success";

/** ORD-0524: the destination the screen is currently allowed to write to. Exactly one of these is
 * chosen at a time, and the other two have no write control on screen -- not a greyed suggestion, no
 * control at all. `store-only` is a state the server has no door for, so it is presented and refused. */
type Destination = "plan" | "shelf" | "store-only";

/** What a batch collect ended in, counted off the fresh list read rather than off the taps. */
type CollectReport = {
  shelfName: string;
  confirmed: number;
  already: number;
  /** The named non-success states this batch met, deduplicated: `no_folder` / `no_place` /
   * `refused` / `unreachable`. A reason code is read to pick the state, never painted. */
  states: string[];
  /** Every write answered and the shelf still holds none of them. */
  unconfirmed: boolean;
};

/* ORD-0437 r4 (commander's ruling of 11:27, section 3): the two `<small>` details below used to print
   `cause.message`, and my own `call()` built that message out of the answer's body text -- so an
   upstream sentence reached the traveller twice. The house carrier is `rejectionWithFacts`: it parses
   the body for machine tokens and the contract receipt field, keeps what this device measured, and
   drops the prose. The exposure ruler counts that as 12 cells on the line and 14 on my branch; this
   edit is what brings it back to 12 by adding none. */
const REFUSAL_CODES = [
  "JOURNEY_NOT_FOUND",
  "NO_ACTIVE_PLAN",
  "IMPORT_DAY_REQUIRED",
  "IMPORT_PLACE_UNKNOWN",
  "IMPORT_PLACE_NOT_VISITABLE",
] as const;

type RefusalCode = (typeof REFUSAL_CODES)[number];

/** What the screen may show: a named condition, and this device's own reading of the status. Never a
 * sentence that arrived in the answer. */
type Failure = { code: RefusalCode | "NO_RESPONSE_FROM_ORIGIN" | "UNNAMED_REFUSAL"; status: number | null };

function describeRejection(cause: unknown): Failure {
  if (cause instanceof RejectionWithFacts) {
    const envelope = (cause.envelope as { error?: { reason_code?: string } } | null)?.error;
    const fromEnvelope = typeof envelope?.reason_code === "string" ? envelope.reason_code : null;
    const scanned = namedCodeIn(cause.message, REFUSAL_CODES);
    // `namedCodeIn` answers `string | null`; the union is what the dictionary lookup needs.
    const code: RefusalCode | null =
      (fromEnvelope && (REFUSAL_CODES as readonly string[]).includes(fromEnvelope)
        ? (fromEnvelope as RefusalCode)
        : null) ??
      ((REFUSAL_CODES as readonly string[]).includes(scanned ?? "") ? (scanned as RefusalCode) : null);
    return { code: code ?? "UNNAMED_REFUSAL", status: cause.http_status };
  }
  return { code: "NO_RESPONSE_FROM_ORIGIN", status: null };
}

/** One sentence per named condition, all of them ours. A condition with no dictionary line does not
 * get to borrow the answer's wording -- it gets `errUnnamed`. The technical cell beside these lines
 * paints only what this device measured: a status code, or that no status was measured. */
const FAILURE_SENTENCE: Record<Failure["code"], PoiImportCopyKey> = {
  JOURNEY_NOT_FOUND: "errJourneyNotFound",
  NO_ACTIVE_PLAN: "errNoActivePlan",
  IMPORT_DAY_REQUIRED: "errDayRequired",
  IMPORT_PLACE_UNKNOWN: "errPlaceUnknown",
  IMPORT_PLACE_NOT_VISITABLE: "errPlaceNotVisitable",
  NO_RESPONSE_FROM_ORIGIN: "errNoResponse",
  UNNAMED_REFUSAL: "errUnnamed",
};

/** The answer names which channel produced the list; that name is a machine token, so it never sits
 * in the main slot. The mapping is keyed by the type the answer allows, so a second channel value
 * from the server is a compile error here rather than a token that reaches the screen unchecked. */
const CHANNEL_NAME: Record<ParseResponse["source_channel"], PoiImportCopyKey> = {
  LOCAL_TEXT_PARSER: "channelValueLocal",
};

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    throw await rejectionWithFacts(response);
  }
  return (await response.json()) as T;
}

export function PoiImportPanel({
  journeyId,
  travelerId = null,
  firstDay = null,
  onOpenPlan,
}: {
  journeyId: string;
  /** Whose shelves the 收藏 exit may ask the list door about. Absent is not "all of them": the read
   *  door requires a `traveler_id`, so the exit names that state instead of sending a call it knows
   *  the door will refuse (`features/collections/collections.ts` -- `readCollections`). */
  travelerId?: string | null;
  /** The day the plan itself starts on: what the automatic landing would pick. */
  firstDay?: string | null;
  onOpenPlan: () => void;
}) {
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const [mode, setMode] = useState<"text" | "screenshot">("text");
  const [text, setText] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [parsed, setParsed] = useState<ParseResponse | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [destination, setDestination] = useState<Destination>("plan");
  const [day, setDay] = useState("");
  const [adding, setAdding] = useState(false);
  const [added, setAdded] = useState<{ ids: string[]; day: string; daySource: string } | null>(null);
  const [alreadyPresent, setAlreadyPresent] = useState<string[]>([]);
  const [readBackFailure, setReadBackFailure] = useState<Failure | "MISMATCH" | null>(null);
  const [shelves, setShelves] = useState<ReadOutcome | null>(null);
  const [shelfId, setShelfId] = useState<string | null>(null);
  const [collecting, setCollecting] = useState(false);
  const [collected, setCollected] = useState<CollectReport | null>(null);

  const addableIds = parsed?.items.filter((i) => i.addable && i.place_id).map((i) => i.place_id as string) ?? [];
  const chosen = selected.filter((id) => addableIds.includes(id));
  const allChosen = addableIds.length > 0 && addableIds.every((id) => chosen.includes(id));
  /* Written as a row of text, not only as the value of `<input type="date">`: an input's value is not
     `innerText`, so a screen that puts the landing day only in the widget is a screen where the day
     cannot be read off the glass -- which is what ORD-0524 ③ measures. */
  const landingDay = day || firstDay || null;

  /* The shelf list is read when the exit is chosen, not when the button is painted: an empty read and
     a read that never happened are two different sentences, and this screen may not say either one
     before the traveller asks for that destination. */
  useEffect(() => {
    if (destination !== "shelf") return;
    let alive = true;
    void readCollections(travelerId).then((answer) => {
      if (!alive) return;
      setShelves(answer);
      if (answer.kind === "loaded" && answer.shelves.length && !shelfId) {
        setShelfId(answer.shelves[0].id);
      }
    });
    return () => {
      alive = false;
    };
  }, [destination, travelerId, shelfId]);

  const runParse = async () => {
    setPhase("loading");
    setFailure(null);
    setAdded(null);
    setAlreadyPresent([]);
    setReadBackFailure(null);
    setCollected(null);
    try {
      const next = await call<ParseResponse>(`/api/v1/journeys/${journeyId}/poi-import/parse`, {
        method: "POST",
        body: JSON.stringify({ text }),
      });
      setParsed(next);
      /* ORD-0524 ①. This line used to seed the selection with every matched place, which made a parse
         a silent batch write waiting for one press. Nothing is picked here; the rows arrive unticked
         and 全选可加的行 is the control that ticks them. */
      setSelected([]);
      setPhase("success");
    } catch (cause) {
      setFailure(describeRejection(cause));
      setPhase("error");
    }
  };

  const runAdd = async () => {
    setAdding(true);
    setReadBackFailure(null);
    /* Only one destination may have written during a visit to this screen: the shelf line is dropped
       when the plan write runs, so two success sentences can never sit on the glass at once. */
    setCollected(null);
    try {
      const written = await call<AddResponse>(`/api/v1/journeys/${journeyId}/poi-import/add`, {
        method: "POST",
        body: JSON.stringify({ place_ids: chosen, day: day || null }),
      });
      /* The write's own 201 is not what makes a place 已加入. The record has to come back from the
         server, so the ids are re-read from the projection the plan is actually drawn from. */
      const projected = await call<{ active_plan?: { activities?: { id: string }[] } | null }>(
        written.readback_path,
      );
      const serverIds = new Set((projected?.active_plan?.activities ?? []).map((a) => a.id));
      const confirmed = written.added.filter((a) => serverIds.has(a.id)).map((a) => a.id);
      if (written.added.length && confirmed.length !== written.added.length) {
        setReadBackFailure("MISMATCH");
        setAdded(null);
      } else {
        setAdded({ ids: confirmed, day: written.day, daySource: written.day_source });
      }
      setAlreadyPresent(written.already_present);
    } catch (cause) {
      setReadBackFailure(describeRejection(cause));
    } finally {
      setAdding(false);
    }
  };

  const toggle = (placeId: string) =>
    setSelected((prev) => (prev.includes(placeId) ? prev.filter((x) => x !== placeId) : [...prev, placeId]));

  /** ORD-0524 ①: selecting every row is the traveller's press, not the parse's side effect. */
  const toggleAll = () => setSelected(allChosen ? [] : addableIds);

  /** Choosing a destination retires the other two writes' sentences: what is on the glass is what the
   *  exit that is picked now actually did. */
  const chooseDestination = (next: Destination) => {
    setDestination(next);
    if (next !== "plan") {
      setAdded(null);
      setAlreadyPresent([]);
      setReadBackFailure(null);
    }
    if (next !== "shelf") setCollected(null);
  };

  /* ORD-0524 ②, exit 2. The batch goes through the door ORD-0510 already uses for one stop, one call
     per row, and the sentence under it is counted off a fresh `GET /api/v1/collections` -- not off the
     taps. A 409 is `already_there`, a 404 is split by its reason code into `no_folder` / `no_place`,
     and none of those reason codes is painted: the state names the line, which is the rule the
     collections face was built on. */
  const runCollect = async () => {
    if (!shelfId) return;
    setCollecting(true);
    setCollected(null);
    setAdded(null);
    setAlreadyPresent([]);
    setReadBackFailure(null);
    try {
      const answers = [];
      for (const placeId of chosen) answers.push(await addPlaceToCollection(shelfId, placeId));
      const already = answers.filter((a) => a.kind === "already_there").length;
      const states = Array.from(
        new Set(
          answers
            .filter((a) => a.kind === "no_folder" || a.kind === "no_place" || a.kind === "refused" || a.kind === "unreachable")
            .map((a) => a.kind),
        ),
      );
      const answered = answers.filter((a) => a.kind === "added").length;
      const after = await readCollections(travelerId);
      setShelves(after);
      const name = after.kind === "loaded" ? (after.shelves.find((s) => s.id === shelfId)?.name ?? "") : "";
      const confirmed =
        after.kind === "loaded"
          ? chosen.filter((placeId) => shelvesHolding(after, placeId).some((s) => s.id === shelfId)).length
          : 0;
      setCollected({ shelfName: name, confirmed, already, states, unconfirmed: answered > 0 && confirmed === 0 });
    } finally {
      setCollecting(false);
    }
  };

  const unsupportedForms = parsed?.unsupported_forms ?? [];

  return (
    <section className="v8-page" data-testid="poi-import">
      <div className="v8-page-intro compact">
        <div>
          <p className="v8-kicker">{poiImportCopy(locale, "kicker")}</p>
          <h1>{poiImportCopy(locale, "title")}</h1>
          <p className="v8-lede">{poiImportCopy(locale, "lede")}</p>
        </div>
        <span className="v8-evidence-label">
          <span className="status-dot" />
          {poiImportCopy(locale, "channelLocal")}
        </span>
      </div>

      <div className="v8-agent-composer-card">
        <div className="v8-agent-copy">
          <span className="v8-agent-mark">{poiImportCopy(locale, "brand")}</span>
          <h2>{poiImportCopy(locale, "modesTitle")}</h2>
          <p>{poiImportCopy(locale, "modesSub")}</p>
          <div className="v8-agent-boundary">
            <b>{poiImportCopy(locale, "boundary")}</b>
            <span>{poiImportCopy(locale, "boundarySub")}</span>
          </div>
        </div>
        <div className="v8-agent-input">
          <div className="v8-mode-chips" role="radiogroup" aria-label={poiImportCopy(locale, "modesTitle")}>
            <button
              type="button"
              role="radio"
              aria-checked={mode === "text"}
              className={mode === "text" ? "active" : ""}
              data-testid="poi-import-mode-text"
              onClick={() => setMode("text")}
            >
              {poiImportCopy(locale, "modeText")}
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={mode === "screenshot"}
              className={mode === "screenshot" ? "active" : ""}
              data-testid="poi-import-mode-screenshot"
              onClick={() => setMode("screenshot")}
            >
              {poiImportCopy(locale, "modeScreenshot")}
            </button>
          </div>
          {mode === "screenshot" ? (
            <p className="v8-message" role="status" data-testid="poi-import-mode-unsupported">
              {poiImportCopy(locale, "screenshotNotSupported")}
            </p>
          ) : (
            <>
              <label htmlFor="poi-import-text">{poiImportCopy(locale, "intent")}</label>
              <textarea
                id="poi-import-text"
                data-testid="poi-import-text"
                value={text}
                placeholder={poiImportCopy(locale, "placeholder")}
                onChange={(event) => setText(event.target.value)}
              />
              <p data-testid="poi-import-char-count">
                {poiImportCopy(locale, "charCount", { n: text.length })}
              </p>
              {/* The reference input box states its supported scope in the hint (map_findings.md Q3:
                  小红书笔记 / 公众号内容 / 已公开权限的飞书文档). Saying it here is what keeps an
                  unread link from reading as a failure of the traveller's paste -- and the sentence has
                  to say we do not open those pages, or the list would be a promise this build breaks. */}
              <p className="v8-sheet-note" data-testid="poi-import-scope-hint">
                {poiImportCopy(locale, "scopeHint")}
              </p>
            </>
          )}
          <div className="v8-agent-toolbar">
            <button
              className="v8-primary"
              data-testid="poi-import-parse"
              disabled={mode !== "text" || phase === "loading" || text.trim().length < 2}
              onClick={() => void runParse()}
            >
              {phase === "loading" ? poiImportCopy(locale, "parsing") : poiImportCopy(locale, "parseAction")}
            </button>
          </div>
          {/* Outside the result card on purpose: which day an import would land on has to be said
              before the traveller asks for anything, so it cannot live in a region that only exists
              after the press. ORD-0437's own gate caught it there on the first run. */}
          <p className="v8-sheet-note" data-testid="poi-import-day-disclosure">
            <b>{poiImportCopy(locale, "autoDayLabel")}</b>{" "}
            {day || firstDay || poiImportCopy(locale, "autoDayUnknown")} — {poiImportCopy(locale, "autoDayNote")}
          </p>
          {mode === "text" && text.trim().length < 2 ? (
            <p className="v8-sheet-note">{poiImportCopy(locale, "needTextHint")}</p>
          ) : null}
        </div>
      </div>

      {/* One region, three phases, and the retry control stays mounted through all of them. */}
      {phase !== "idle" ? (
        <div className="v8-intelligence-grid" data-testid="poi-import-card" data-phase={phase}>
          <article className="v8-intel-hero">
            <span className="v8-status-pill violet" data-testid="poi-import-phase">
              {phase === "loading"
                ? poiImportCopy(locale, "phaseLoading")
                : phase === "error"
                  ? poiImportCopy(locale, "phaseError")
                  : poiImportCopy(locale, "phaseSuccess")}
            </span>
            {phase === "loading" ? (
              <p data-testid="poi-import-loading-text">{poiImportCopy(locale, "loadingWhere")}</p>
            ) : null}
            {phase === "error" && failure ? (
              <p role="alert" data-testid="poi-import-error">
                {poiImportCopy(locale, "loadFailed")}
                {/* The condition is this screen's own sentence. The only value painted next to it is
                    the status this device measured -- the named code stays inside the component,
                    because it is read out of the answer's envelope and the exposure rule forbids
                    anything derived from the body reaching the DOM by any route, filtering included.
                    (r7b counted my first attempt as a cell for exactly that reason.) */}
                <span data-testid="poi-import-error-why">{poiImportCopy(locale, FAILURE_SENTENCE[failure.code])}</span>
                {failure.status ? (
                  <small data-testid="poi-import-error-detail">HTTP {failure.status}</small>
                ) : (
                  <small data-testid="poi-import-error-detail">{poiImportCopy(locale, "noStatusMeasured")}</small>
                )}
              </p>
            ) : null}
            {phase === "success" ? (
              <p data-testid="poi-import-counts">
                {poiImportCopy(locale, "counts", {
                  extracted: parsed?.counts.extracted ?? 0,
                  resolved: parsed?.counts.resolved ?? 0,
                  unresolved: parsed?.counts.unresolved ?? 0,
                  ambiguous: parsed?.counts.ambiguous ?? 0,
                })}
              </p>
            ) : null}
            <button
              className="v8-outline-button"
              data-testid="poi-import-retry"
              disabled={phase === "loading" || mode !== "text"}
              onClick={() => void runParse()}
            >
              {poiImportCopy(locale, "retry")}
            </button>
          </article>
          <div className="v8-intel-facts">
            <div data-testid="poi-import-channel-cell">
              <span>{poiImportCopy(locale, "channelLabel")}</span>
              <b data-testid="poi-import-channel-name">{poiImportCopy(locale, CHANNEL_NAME[parsed?.source_channel ?? "LOCAL_TEXT_PARSER"])}</b>
              <small>{poiImportCopy(locale, "channelNote")}</small>
              {/* The token itself stays available for diagnosis, but only inside the fold: the main
                  slot is the dictionary's words. */}
              <details data-testid="poi-import-channel-fold">
                <summary>{poiImportCopy(locale, "technicalToggle")}</summary>
                <code data-testid="poi-import-channel-token">{parsed?.source_channel ?? "LOCAL_TEXT_PARSER"}</code>
              </details>
            </div>
            {unsupportedForms.length ? (
              <div data-testid="poi-import-unsupported">
                <span>{poiImportCopy(locale, "unsupportedLabel")}</span>
                <b>{unsupportedForms.join(" · ")}</b>
                <small>
                  {parsed?.link_hosts.length
                    ? poiImportCopy(locale, "unsupportedHosts", { hosts: parsed.link_hosts.join(", ") })
                    : poiImportCopy(locale, "unsupportedNoFetch")}
                </small>
              </div>
            ) : null}
            <div data-testid="poi-import-day-result">
              <span>{poiImportCopy(locale, "autoDayLabel")}</span>
              <b>{day || firstDay || poiImportCopy(locale, "autoDayUnknown")}</b>
              <small>{poiImportCopy(locale, "autoDayNote")}</small>
            </div>
          </div>
        </div>
      ) : null}

      {phase === "success" && parsed && parsed.items.length === 0 ? (
        <p className="v8-message" role="status" data-testid="poi-import-empty">
          {poiImportCopy(locale, "emptyExtracted")}
        </p>
      ) : null}

      {phase === "success" && parsed && parsed.items.length > 0 ? (
        <>
          {/* Permanent while results are on screen: what was recognised still needs a human eye. */}
          <p className="v8-sheet-note" role="status" data-testid="poi-import-verify-warning">
            {poiImportCopy(locale, "verifyWarning")}
          </p>
          <div className="v8-stay-layout">
            <div className="v8-stay-list" data-testid="poi-import-results">
              {/* ORD-0524 ①: the row that selects every selectable row, mounted above the cards so it is
                  found before the list is scrolled through. Ticking moves nothing on the server -- the
                  destination below is what writes. */}
              {addableIds.length ? (
                <button
                  type="button"
                  role="checkbox"
                  aria-checked={allChosen}
                  className={allChosen ? "v8-pick-box checked poi-import-select-all" : "v8-pick-box poi-import-select-all"}
                  data-testid="poi-import-select-all"
                  data-picked={allChosen ? "true" : "false"}
                  onClick={toggleAll}
                >
                  <span aria-hidden="true">{allChosen ? "✓" : ""}</span>
                  <span className="v8-pick-box-text">
                    {allChosen ? poiImportCopy(locale, "selectAllOn") : poiImportCopy(locale, "selectAll")}
                  </span>
                </button>
              ) : null}
              {parsed.items.map((item, index) => (
                <PoiCandidateCard
                  key={`${item.title}-${index}`}
                  index={index + 1}
                  phrase={item.title}
                  status={item.status}
                  placeId={item.place_id}
                  placeName={item.place_name}
                  category={null}
                  coordinateSystem={item.coordinate_system}
                  note={item.note}
                  addable={item.addable}
                  selected={!!item.place_id && selected.includes(item.place_id)}
                  onToggle={item.place_id ? () => toggle(item.place_id as string) : null}
                  ambiguousNames={item.matches}
                />
              ))}
            </div>
            <aside className="v8-boundary-card">
              <b>{poiImportCopy(locale, "exitsTitle")}</b>
              <p className="v8-sheet-note" data-testid="poi-import-exits-lead">
                {poiImportCopy(locale, "exitsLead")}
              </p>
              <p data-testid="poi-import-selection-count">
                {poiImportCopy(locale, "selectedCount", { n: chosen.length, total: addableIds.length })}
              </p>
              {/* ORD-0524 ②: one radiogroup, one chosen destination, and only the chosen one carries a
                  write control. 只存不排 is selectable on purpose -- choosing it takes both doors away
                  and leaves the refusal on the glass, which is a stated exit rather than a silent one.
                  A touch screen never shows a tooltip, so the reason is painted, not titled. */}
              <div
                className="v8-exit-radiogroup"
                role="radiogroup"
                aria-label={poiImportCopy(locale, "exitsTitle")}
                data-testid="poi-import-exits"
              >
                <button
                  type="button"
                  role="radio"
                  aria-checked={destination === "plan"}
                  className={destination === "plan" ? "v8-exit chosen" : "v8-exit"}
                  data-testid="poi-import-exit-plan"
                  data-picked={destination === "plan" ? "true" : "false"}
                  onClick={() => chooseDestination("plan")}
                >
                  {poiImportCopy(locale, "addToPlan")}
                </button>
                <button
                  type="button"
                  role="radio"
                  aria-checked={destination === "shelf"}
                  className={destination === "shelf" ? "v8-exit chosen" : "v8-exit"}
                  data-testid="poi-import-exit-shelf"
                  data-picked={destination === "shelf" ? "true" : "false"}
                  onClick={() => chooseDestination("shelf")}
                >
                  {poiImportCopy(locale, "favorite")}
                </button>
                <button
                  type="button"
                  role="radio"
                  aria-checked={destination === "store-only"}
                  className={destination === "store-only" ? "v8-exit chosen" : "v8-exit"}
                  data-testid="poi-import-exit-store-only"
                  data-picked={destination === "store-only" ? "true" : "false"}
                  onClick={() => chooseDestination("store-only")}
                >
                  {poiImportCopy(locale, "exitStoreOnly")}
                </button>
              </div>
              <small className="poi-import-exit-why" data-testid="poi-import-store-only-why">
                {poiImportCopy(locale, "storeOnlyRefusal")}
              </small>

              {destination === "plan" ? (
                <div data-testid="poi-import-exit-plan-body">
                  <label htmlFor="poi-import-day">{poiImportCopy(locale, "dayLabel")}</label>
                  <input
                    id="poi-import-day"
                    type="date"
                    data-testid="poi-import-day"
                    value={day}
                    onChange={(event) => setDay(event.target.value)}
                  />
                  {/* ORD-0524 ③: the landing day as a row of text. `<input type="date">` holds a value,
                      not a readable line -- `innerText` cannot reach a widget's value, so the day the
                      write will use is painted next to it, and it says whose day it is. */}
                  <p data-testid="poi-import-landing-day">
                    {landingDay
                      ? day
                        ? poiImportCopy(locale, "landingDayChosen", { day: landingDay })
                        : poiImportCopy(locale, "landingDayFirst", { day: landingDay })
                      : poiImportCopy(locale, "landingDayNone")}
                  </p>
                  <button
                    className="v8-primary"
                    data-testid="poi-import-add"
                    disabled={adding || chosen.length === 0}
                    onClick={() => void runAdd()}
                  >
                    {adding ? poiImportCopy(locale, "adding") : poiImportCopy(locale, "addToPlan")}
                  </button>
                  {/* A replay adds nothing, so it has no right to a success sentence: this used to read
                      `added ? ...` and painted "0 places are on 2026-12-05 — read back from the server"
                      under the 本来就在 line, which is the second success the order forbids -- measured in
                      the gate's own replay arm. What a replay may say is what was already there. */}
                  {added && added.ids.length > 0 ? (
                    <p data-testid="poi-import-added">
                      {poiImportCopy(locale, "addedCount", { n: added.ids.length, day: added.day })}
                      <small data-testid="poi-import-added-source">{added.daySource}</small>
                    </p>
                  ) : null}
                  {alreadyPresent.length ? (
                    <p data-testid="poi-import-already-present">
                      {poiImportCopy(locale, "alreadyPresent", { n: alreadyPresent.length })}
                    </p>
                  ) : null}
                  {readBackFailure ? (
                    <p role="alert" data-testid="poi-import-readback-failed">
                      {readBackFailure === "MISMATCH"
                        ? poiImportCopy(locale, "readBackFailed")
                        : poiImportCopy(locale, FAILURE_SENTENCE[readBackFailure.code])}
                      {readBackFailure !== "MISMATCH" && readBackFailure.status ? (
                        <small data-testid="poi-import-readback-detail">HTTP {readBackFailure.status}</small>
                      ) : null}
                    </p>
                  ) : null}
                </div>
              ) : null}

              {destination === "shelf" ? (
                <div data-testid="poi-import-exit-shelf-body">
                  {!travelerId ? (
                    <p className="v8-message" role="status" data-testid="poi-import-shelf-no-traveler">
                      {copyFor("noTravelerYet", zh)}
                    </p>
                  ) : !shelves ? (
                    <p role="status" data-testid="poi-import-shelf-loading">
                      {copyFor("loading", zh)}
                    </p>
                  ) : shelves.kind === "refused" ? (
                    <p role="alert" data-testid="poi-import-shelf-refused">
                      {refusalCopy(zh)}
                    </p>
                  ) : shelves.kind === "unreachable" ? (
                    <p role="alert" data-testid="poi-import-shelf-unreachable">
                      {copyFor("readUnreachable", zh)}
                    </p>
                  ) : shelves.shelves.length === 0 ? (
                    <p role="status" data-testid="poi-import-shelf-empty">
                      {copyFor("noShelfToPick", zh)}
                    </p>
                  ) : (
                    <div className="v8-shelf-radiogroup" role="radiogroup" data-testid="poi-import-shelves">
                      <p className="v8-sheet-note">{copyFor("pickerTitle", zh)}</p>
                      {shelves.shelves.map((shelf) => (
                        <button
                          key={shelf.id}
                          type="button"
                          role="radio"
                          aria-checked={shelfId === shelf.id}
                          className={shelfId === shelf.id ? "v8-exit chosen" : "v8-exit"}
                          data-testid="poi-import-shelf"
                          onClick={() => {
                            setShelfId(shelf.id);
                            setCollected(null);
                          }}
                        >
                          <span className="v8-shelf-name">{shelf.name}</span>
                          <span className="v8-shelf-count">
                            {shelf.item_count} {copyFor("places", zh)}
                          </span>
                        </button>
                      ))}
                    </div>
                  )}
                  <button
                    className="v8-primary"
                    data-testid="poi-import-collect"
                    disabled={collecting || chosen.length === 0 || !shelfId}
                    onClick={() => void runCollect()}
                  >
                    {collecting ? poiImportCopy(locale, "collecting") : poiImportCopy(locale, "collectAction")}
                  </button>
                  {collected ? (
                    <>
                      {collected.confirmed > 0 ? (
                        <p data-testid="poi-import-collected">
                          {poiImportCopy(locale, "collectedCount", {
                            n: collected.confirmed,
                            shelf: collected.shelfName,
                          })}
                        </p>
                      ) : null}
                      {collected.already > 0 ? (
                        <p data-testid="poi-import-collect-already">
                          {poiImportCopy(locale, "collectAlready", {
                            n: collected.already,
                            shelf: collected.shelfName,
                          })}
                        </p>
                      ) : null}
                      {collected.unconfirmed ? (
                        <p role="alert" data-testid="poi-import-collect-unconfirmed">
                          {poiImportCopy(locale, "collectNothingConfirmed")}
                        </p>
                      ) : null}
                      {collected.states.map((state) => (
                        <p key={state} role="alert" data-testid="poi-import-collect-state">
                          {state === "no_folder"
                            ? copyFor("noFolder", zh)
                            : state === "no_place"
                              ? copyFor("noPlace", zh)
                              : state === "unreachable"
                                ? copyFor("writeUnreachable", zh)
                                : copyFor("writeRefused", zh)}
                        </p>
                      ))}
                    </>
                  ) : null}
                </div>
              ) : null}

              {destination === "store-only" ? (
                <p className="v8-message" role="status" data-testid="poi-import-store-only-refused">
                  {poiImportCopy(locale, "storeOnlyChosen")}
                </p>
              ) : null}

              <button className="v8-quiet-button" data-testid="poi-import-open-plan" onClick={onOpenPlan}>
                {poiImportCopy(locale, "openPlan")}
              </button>
            </aside>
          </div>
        </>
      ) : null}
    </section>
  );
}
