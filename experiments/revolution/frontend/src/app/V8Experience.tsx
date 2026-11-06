import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cityCodeLabel,
  commitmentKindLabel,
  commitmentLevelLabel,
  commitmentStatusLabel,
} from "../lib/displayVocabulary";
import { Icon } from "../components/v8/Icon";
import { activityCoordinate, MapPanel, tripHasNoCoordinates } from "../components/v8/MapPanel";
import { mapCopy } from "../i18n/mapPanelCopy";
import { AgentComposer } from "../features/agent/AgentComposer";
import { CopilotDock } from "../features/agent/CopilotDock";
import { PlanWithAiSheet } from "../features/planning/PlanWithAiSheet";
import { PlanAiRunStrip } from "../features/planning/PlanAiRunStrip";
import { TripItinerary } from "../features/trip/TripItinerary";
import { ActivityDetail } from "../features/trip/ActivityDetail";
import { HotelsPanel } from "../features/stay/HotelsPanel";
import { PlanComparison } from "../features/planning/PlanComparison";
import { LiveRecovery } from "../features/execution/LiveRecovery";
import { JourneyMembershipPanel } from "../features/execution/JourneyMembershipPanel";
import { liveActivityWindow } from "../lib/executionWindow";
import { installVisualViewportVars } from "../lib/visualViewport";
import {
  classifyHealthProbe,
  classifyReadFailure,
  OriginResponseUnusableError,
  clearJourneyReferences,
  copyFor,
  isOriginResponseUnusable,
  recordAttribution,
  withCachedCopyFact,
  type Attribution,
  type OriginProbeFact,
} from "../lib/failureAttribution";
import {
  fetchWithReadDeadline,
  isReadDeadlineMissed,
  READ_DEADLINE_MS,
} from "../lib/readDeadline";
import { beginNonIdempotentRequest } from "../lib/inFlightWrites";
import { storeTravelerBearer, travelerAuthHeaders } from "../lib/travelerSession";
import {
  clearPendingContinuation,
  continuePendingPlan,
  type ContinuationOutcome,
} from "../api/journeyContinuation";
import { useElapsedSeconds } from "../lib/waitClock";
import { readChoice, setThemeChoice, type ThemeChoice } from "../theme";
import { MobilitySheet } from "../features/mobility/MobilitySheet";
import { StayExplorer } from "../features/stay/StayExplorer";
import { ControlTowerPanel } from "../features/intelligence/ControlTowerPanel";
import { V10LiveExecutionPanel } from "../features/execution/V10LiveExecutionPanel";
import { BookingArtifactScanner } from "../features/inbox/BookingArtifactScanner";
import { JourneyInboxWorkbench, fieldLabel } from "../features/inbox/JourneyInboxWorkbench";
import { JourneyLinkAction } from "../features/inbox/JourneyLinkAction";
import { PushCapabilityCard } from "../features/notifications/PushCapabilityCard";
import { TwinPanel } from "../features/traveler-twin/TwinPanel";
import { SortableActivityList } from "../features/studio/SortableActivityList";
import { V12AuthoritativeJourneyPanel } from "../features/execution/V12AuthoritativeJourneyPanel";
import { SimulationEntry } from "../features/simulation/SimulationPanel";
import { useDemoScene } from "../lib/sceneClock";
import { V14RecoveryTruthPanel } from "../features/recovery/V14RecoveryTruthPanel";
import { V14PreventivePanel } from "../features/recovery/V14PreventivePanel";
import { RecoveryRequiredPanel } from "../features/recovery/RecoveryRequiredPanel";
import { V12TripStudioPanel } from "../features/studio/V12TripStudioPanel";
import { PlanVersionHistoryPanel } from "../features/studio/PlanVersionHistoryPanel";
import {
  authoritativePanels,
  hubAnchorPanels,
  renderPanels,
  routePanels,
  tripsTabPanels,
} from "./v8PanelRegistry";
import { RealCityDiscoveryPanel } from "../features/discovery/RealCityDiscoveryPanel";
import { BootstrapDiscoveryPanel } from "../features/discovery/BootstrapDiscoveryPanel";
import { BootstrapJourneyStartPanel } from "../features/discovery/BootstrapJourneyStartPanel";
import { DestinationPreparationPanel } from "../features/discovery/DestinationPreparationPanel";
import {
  JourneyTrialSetup,
  localDateTimeInZoneToIso,
  type JourneyTrialDraft,
  type TrialPhase,
} from "../features/setup/JourneyTrialSetup";
import { cityNexusApi, type AgentRuntimeStatus } from "../api/citynexus";
import {
  useAuthoritativePlans,
  useAuthoritativeTwin,
  type JourneyTwin,
  type PlanVersion,
} from "../api/v12Authoritative";
import { useCandidateRejections } from "../api/candidateRejections";
import { useContinuationState } from "../api/useContinuationState";
import { syncStatedInterests } from "../api/travelerPreferences";
import { desktopRoutes, mobileRoutes, navGroupKey } from "../routes/v8Routes";
import {
  readOfflineJourney,
  saveOfflineJourneyVerified,
  type OfflineJourney,
} from "../lib/offlineJourney";
import { useT, useLocale } from "../i18n/LocaleContext";
import {
  currencyExponent,
  currencyMinorFactor,
  formatMinorMoney,
  majorToMinor,
} from "../lib/money";
import { withViewTransition } from "../lib/viewTransition";
import { isDisruptionOpen } from "../lib/disruptionState";
import { JOURNEY_ID_SEGMENT, v8FirstSegment } from "../lib/journeyLink";
import { measureRawFacts, type RawFacts } from "../lib/rawFacts";
import { RawFactsPanel } from "../components/RawFactsPanel";
import {
  budgetEvidenceCoverage,
  budgetVerdictAllowed as budgetVerdictAllowedFor,
} from "../lib/budgetEvidence";
import {
  v8Modes,
  type RecoveryState,
  type V8Data,
  type V8Mode,
} from "./v8Types";

type Policy = {
  mode: "ADVISORY" | "ASSISTED" | "AUTONOMOUS";
  auto_accept_replan: boolean;
  version: number;
  max_auto_replan_cost_delta_minor: number;
  max_auto_loss_minor: number;
  max_auto_schedule_drift_min: number;
};
type Wallet = {
  reservations: Array<{
    id: string;
    type: string;
    confirmation_ref: string;
    scheduled_departure_at: string;
    scheduled_arrival_at: string;
    realtime_status: string;
    /* `_reservation_payload` (main.py:3527) links a reservation to the commitment it became.  This is
       the only honest source for a "受保护" badge: without it the card was asserting a promise the
       server had not made (ORD-0146 ③). */
    commitment_id?: string | null;
    price: {
      amount_minor: number | null;
      currency: string | null;
      status: string;
    };
  }>;
  commitments: Array<{
    id: string;
    level: string;
    kind: string;
    external_ref: string | null;
    status: string;
    /* `_commitment_payload` (main.py:5311) puts the money under `monetary_value` and says how much
       is actually known in `monetary_value_status`; the schedule it protects is under `policy`.
       Declaring less than this is how a committed booking ended up with no honest way to render. */
    monetary_value?: {
      amount_minor?: number | null;
      currency?: string | null;
    } | null;
    monetary_value_status?: string | null;
    policy?: {
      semantic_key?: string | null;
      scheduled_start_at?: string | null;
      scheduled_end_at?: string | null;
      fixed_schedule?: boolean | null;
    } & Record<string, unknown>;
  }>;
  offline_ready: boolean;
};

/**
 * The traveller-facing reason behind a rejected request.
 *
 * A4-1 froze the envelope as `error.code = VALIDATION_ERROR` with
 * `error.details.errors[].loc / msg`.  Reading only `error.message` yields the constant
 * "Request validation failed", which names nothing the traveller can go and fix, so the
 * per-field `loc: msg` pairs are appended -- backend words, no invented translation.
 *
 * Those backend words are this error's own `message`, and since Q64=1 they stop there: the value is
 * for telemetry and for a `data-*` attribute, never for the rendered copy.  A traveller sentence and
 * this device's measured fields (RawFactsPanel) are what a screen shows, because an upstream
 * sentence in the DOM also carries whatever the answer happened to contain (ORD-0325).
 */
function rejectionReason(body: any, status: number): string {
  const errors: unknown[] = Array.isArray(body?.error?.details?.errors)
    ? body.error.details.errors
    : [];
  const fields = errors
    .map((entry) => {
      const item = entry as { loc?: unknown[]; msg?: unknown };
      const loc = Array.isArray(item?.loc)
        ? item.loc.filter(Boolean).join(".")
        : "";
      const msg = typeof item?.msg === "string" ? item.msg : "";
      return loc || msg ? `${loc ? `${loc}: ` : ""}${msg}` : "";
    })
    .filter(Boolean)
    .slice(0, 4)
    .join(" / ");
  const headline =
    (typeof body?.detail === "string" ? body.detail : null) ??
    (typeof body?.error?.message === "string" ? body.error.message : null) ??
    `Request failed (${status})`;
  return fields ? `${headline} — ${fields}` : headline;
}

/**
 * A rejection with the two facts the screen needs to be honest: the status the server answered with
 * and its named code.  Before this, every failure arrived as a bare `Error` and the boot path could
 * only ask "was it a TypeError?" -- which is why a 404 for a deleted journey and a dead network both
 * ended up printed as "网络不可用" (ORD-0126 R3-4/R3-6).
 */
class V8Rejection extends Error {
  readonly status: number;
  readonly code: string | null;
  /** What this device measured on the refusing answer, carried with the refusal so the screen that
   *  shows the rejection can show *that* instead of the server's sentence (Q64=1). */
  readonly facts: RawFacts;
  constructor(
    message: string,
    status: number,
    code: string | null,
    facts: RawFacts,
  ) {
    super(message);
    this.name = "V8Rejection";
    this.status = status;
    this.code = code;
    this.facts = facts;
  }
}

/**
 * The traveller sentence for a step that failed, composed from the machine facts this device holds:
 * whether an answer came at all, and with what status.  An upstream sentence is never the wording,
 * and `error.message` is not a safe stand-in for "our own words" -- whichever helper threw decides
 * what that string carries, and since Q64=1 that includes whatever the server's body happened to say.
 */
function refusalSentence(error: unknown, zh: boolean, fallback: string): string {
  if (error instanceof V8Rejection) {
    return zh
      ? `服务器拒绝了这一步（HTTP ${error.status}）。你的行程没有因此改变，可以检查后重试。`
      : `The server refused this step (HTTP ${error.status}). Your trip is unchanged; check the details and try again.`;
  }
  if (isOriginResponseUnusable(error)) {
    return zh
      ? `服务器答了 HTTP ${error.status}，可带回来的不是这一屏能用的数据（Content-Type: ${error.content_type ?? "未知"}）。`
      : `The server answered HTTP ${error.status}, but not with data this screen can use (Content-Type: ${error.content_type ?? "unknown"}).`;
  }
  if (isReadDeadlineMissed(error) || error instanceof TypeError) {
    return zh
      ? "连不上服务器，请重试。"
      : "Could not reach the server. Try again.";
  }
  return fallback;
}

/** The reading a failure carries, if any: only an answered HTTP refusal has one. */
function refusalFacts(error: unknown): RawFacts | null {
  return error instanceof V8Rejection ? error.facts : null;
}

async function v8Request<T>(path: string, init?: RequestInit): Promise<T> {
  const releaseInFlight = beginNonIdempotentRequest(init?.method);
  try {
    return await v8RequestInner<T>(path, init);
  } finally {
    releaseInFlight();
  }
}

async function v8RequestInner<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetchWithReadDeadline(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  /* Parsed by hand rather than `response.json().catch(() => null)`: that one line turned three
     different answers -- zero bytes, an HTML document, a plain sentence -- into the same `null`, and
     the caller then threw a TypeError on it, which the classifier filed under "nothing came back".
     The status branch below is unchanged. */
  const raw = await response.text();
  const emptyBody = raw.trim() === "";
  let body: any = null;
  if (!emptyBody) {
    try {
      body = JSON.parse(raw);
    } catch {
      body = null;
    }
  }
  const contentType = response.headers.get("content-type");
  if (!response.ok) {
    throw new V8Rejection(
      rejectionReason(body, response.status),
      response.status,
      typeof body?.error?.code === "string" ? body.error.code : null,
      measureRawFacts(response, raw),
    );
  }
  /* ORD-0263 fifth state: a 2xx that reached us and still gives this screen nothing is an answered
     request, and it has to be classified as one. */
  if (body === null) {
    throw new OriginResponseUnusableError(
      response.status,
      contentType,
      (contentType ?? "").includes("html") ? "html" : emptyBody ? "empty" : "not_json",
    );
  }
  return body as T;
}

/**
 * "Did the origin answer, and how?" as four separate facts -- not a boolean.
 *
 * `navigator.onLine` stays out of this (it is true on a captive portal and true while a backend is
 * dead), and so does "was it a 2xx?" alone: ORD-0126 needed "no 网络不可用 sentence may stand while a
 * same-page probe is healthy", which is a *positive* reading.  ORD-0263 needed the other half -- an
 * origin that answered HTTP 500 and an origin that never answered are different states, and a single
 * `response.ok` could not tell them apart, so one sentence covered both.  `timed_out` says which kind
 * of silence this was: our own deadline expired, as opposed to a connection that was refused outright.
 */
async function probeHealth(): Promise<OriginProbeFact> {
  const controller = new AbortController();
  let deadlineFired = false;
  const timer = setTimeout(() => {
    deadlineFired = true;
    controller.abort();
  }, 4_000);
  try {
    const response = await fetch("/api/v1/health", { signal: controller.signal });
    return { answered: true, status: response.status, ok: response.ok, timed_out: false };
  } catch {
    return { answered: false, status: null, ok: false, timed_out: deadlineFired };
  } finally {
    clearTimeout(timer);
  }
}

const money = formatMinorMoney;

/** Waiting, found, empty and refused are four different faces and must not share one. */
type StaySearchState = "idle" | "searching" | "found" | "empty" | "failed";

/**
 * Today's explanation for an empty search, taken from the `generation.outcome` the server
 * reported.  Anything unrecognised -- including no outcome at all -- keeps the generic
 * sentence: naming a cause the server did not give would be the fabrication the frozen list
 * forbids, in the same shape as a made-up progress step.
 */
function stayOutcomeLine(outcome: string | null, zh: boolean): string {
  switch (outcome) {
    case "NO_OFFERS_FROM_PROVIDER":
      return zh
        ? "提供方这一轮没有返回任何可比较的住宿。这种失败多在提供方一侧，再查一次通常能拿到结果。"
        : "The providers returned nothing comparable this round. That failure is on their side; searching again usually brings results.";
    case "ALL_CANDIDATES_REJECTED":
      return zh
        ? "有候选住宿，但没有一个满足这趟行程的硬性条件（预算、时间或可达性）。"
        : "There were candidates, but none of them met this trip's hard constraints (budget, timing or reachability).";
    case "NOT_APPLICABLE_ZERO_NIGHTS":
      return zh
        ? "这趟行程不过夜，所以没有住宿可搜。"
        : "This trip has no overnight stay, so there is nothing to search for.";
    default:
      return zh
        ? "行程已经创建，日期和地点都还在，只是这一次搜索没有返回任何可比较的住宿，服务器也没有说明原因。我们不会编造酒店。"
        : "Your trip is created and its dates are intact — this search returned nothing to compare, and the server gave no reason. We will not invent hotels.";
  }
}

/** A fetch the traveller stopped is an AbortError; a network failure is a TypeError. */
function isAbort(cause: unknown): boolean {
  return cause instanceof Error && cause.name === "AbortError";
}

/** Ruling ④: three choices, and the word on the button is the choice you have now. */
const THEME_WORD: Record<ThemeChoice, [string, string]> = {
  system: ["跟随系统", "System"],
  light: ["亮色", "Light"],
  dark: ["暗色", "Dark"],
};

const THEME_NEXT: Record<ThemeChoice, ThemeChoice> = {
  system: "light",
  light: "dark",
  dark: "system",
};

/**
 * Wall-clock helpers that read an ISO instant in the *journey's* time zone.
 *
 * Slicing the raw ISO string (``start_at.slice(11, 16)``) shows UTC wall time
 * for a UTC-offset instant.  A 10:00 Xiamen arrival is stored as
 * ``...T02:00:00+00:00``, so the raw slice rendered "02:00" on Today while the
 * itinerary (which uses Intl with the journey zone) rendered "10:00" — two
 * contradictory readings of the same stop.  Always format instants through the
 * journey time zone.
 */
function clockInZone(value: string, timeZone: string): string {
  try {
    return new Intl.DateTimeFormat("en-GB", {
      timeZone,
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).format(new Date(value));
  } catch {
    return value.slice(11, 16);
  }
}

function dateKeyInZone(value: string, timeZone: string): string {
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date(value));
  } catch {
    return value.slice(0, 10);
  }
}

function date(value?: string, locale = "en-GB") {
  return value
    ? new Intl.DateTimeFormat(locale === "zh-CN" ? "zh-CN" : "en-GB", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(new Date(value))
    : "—";
}

/** Same as `date`, but rendered in the journey's time zone, not the device's. */
function dateInZone(
  value: string | undefined,
  locale: string,
  timeZone: string,
) {
  if (!value) return "—";
  try {
    return new Intl.DateTimeFormat(locale === "zh-CN" ? "zh-CN" : "en-GB", {
      timeZone,
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(new Date(value));
  } catch {
    return date(value, locale);
  }
}

// Alternate paths people share or type.  A deep link that lands on an unknown
// key used to render an empty content area, so refresh/share produced a blank
// page.  Map the known aliases onto real routes; anything else falls back to
// Today rather than nothing.
//
// An alias must never shadow a route the product itself navigates to.  "stay"
// and "bookings" are real desktop routes, so aliasing them to "trips" made a
// hard refresh silently swap the surface: the traveller opened 住宿选择, then
// reloading showed the Trips workspace instead.
const ROUTE_ALIASES: Record<string, string> = {
  today: "hub",
  copilot: "agent",
  wallet: "trips",
  itinerary: "trips",
  stays: "trips",
  home: "hub",
};

const KNOWN_ROUTES = new Set<string>([
  ...desktopRoutes.map((r) => r.key),
  ...mobileRoutes.map((r) => r.key),
]);

/**
 * ORD-0404: what the address asks for, and which of four things it was.
 *
 * `routeFromPath` could already name the screen -- the drawer proves that, because clicking into
 * 城市探索 puts `/v8/discover` in the address and a walking instrument reads it back. What was
 * missing was the other half: nothing *kept* that answer across the demo gate, which ended by
 * navigating to Today, and an unrecognised path fell through to Today without ever saying so.
 * Those two need three distinguishable outcomes, not one string:
 *   screen   -- the address named a screen, and this session must go there once it can;
 *   journey  -- `/v8/<uuid>`: ORD-0351's address, which names a journey and *no* screen, so it
 *              must land on Today silently. Calling it "no screen" would be a new lie built on
 *              top of the one being fixed;
 *   unknown  -- a path that names neither, and has to be answered honestly;
 *   bare     -- `/v8/` itself, which is Today and nothing was asked.
 */
type DeepLink =
  | { kind: "screen"; route: string }
  | { kind: "journey" }
  | { kind: "unknown"; segment: string }
  | { kind: "bare" };

function deepLinkFromPath(): DeepLink {
  // Route keys and aliases are lower-case; the raw case is kept for the unknown-segment reading,
  // because what the traveller has to recognise is the address they were given, not its normalised form.
  const segment = v8FirstSegment(window.location.pathname);
  if (!segment) return { kind: "bare" };
  const key = segment.toLowerCase();
  // A real route wins over an alias, otherwise refresh changes the screen.
  if (KNOWN_ROUTES.has(key)) return { kind: "screen", route: key };
  const alias = ROUTE_ALIASES[key];
  if (alias) return { kind: "screen", route: alias };
  if (JOURNEY_ID_SEGMENT.test(key)) return { kind: "journey" };
  return { kind: "unknown", segment };
}

function routeFromPath() {
  const link = deepLinkFromPath();
  // Never render an empty shell: anything that names no screen is Today.
  return link.kind === "screen" ? link.route : "hub";
}

const drawerRoutes = [
  ...desktopRoutes,
  ...mobileRoutes.filter(
    (item) => !desktopRoutes.some((entry) => entry.key === item.key),
  ),
];

function greetingFor(hour: number, t: (key: string) => string) {
  if (hour < 5) return t("hub.greeting.night");
  if (hour < 12) return t("hub.greeting.morning");
  if (hour < 18) return t("hub.greeting.afternoon");
  return t("hub.greeting.evening");
}

function cityFromCode(code: string) {
  const segments = code.split("-");
  const raw = (
    segments.length > 1 ? segments.slice(1).join(" ") : code
  ).replace(/_/g, " ");
  return raw
    .split(" ")
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(" ");
}

function initialsFrom(name: string) {
  const words = name.split(/\s+/).filter(Boolean);
  const initials =
    words.length > 1
      ? words
          .slice(0, 2)
          .map((word) => word.charAt(0))
          .join("")
      : name.replace(/[^A-Za-z]/g, "").slice(0, 2);
  return (initials || "CN").toUpperCase();
}

// The resolver lives in src/lib/executionWindow.ts so every surface that states "where am
// I now" reads one implementation.  Two copies of a three-state rule is how Live and the
// V10 panel ended up disagreeing about the same journey.

// The authoritative current step is current_activity_id; falling back to a
// fixed index would show a stale "next" once the journey has advanced.
function nextActivityOf(plan: V8Data["active_plan"]) {
  return liveActivityWindow(plan).next;
}

export function V8Experience() {
  const [route, setRoute] = useState(routeFromPath);
  /* ORD-0404: what the address asked for, read once at mount -- before anything this session does
     to the address. Both the sealed-demo gate and the create-trip flow navigate, and navigating to
     Today is precisely what erased the screen the traveller had asked for, so the answer has to be
     held somewhere a later navigate cannot overwrite. */
  const [deepLink] = useState(deepLinkFromPath);
  const [data, setData] = useState<V8Data | null>(null);
  const [recentlyJoinedJourneyId, setRecentlyJoinedJourneyId] = useState<string | null>(null);
  const [mode, setMode] = useState<V8Mode>("TRANSIT");
  const [comparison, setComparison] = useState<V8Data["mobility_facts"]>([]);
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [runtimeStatus, setRuntimeStatus] = useState<AgentRuntimeStatus | null>(
    null,
  );
  const [busy, setBusy] = useState(true);
  const [banner, setBanner] = useState<{ text: string | null; facts: RawFacts | null }>({
    text: null,
    facts: null,
  });
  /**
   * The banner's text and its measured fields are one decision, so they are set by one function: a
   * sentence from a later step must never stay on screen next to the fields this step read.  The
   * name is kept as `setMessage`, so the 13 sites that announce something without a reading beside it
   * continue to mean exactly what they meant and clear the fields on their own.
   */
  const message = banner.text;
  const setMessage = (text: string | null, facts: RawFacts | null = null) =>
    setBanner({ text, facts });
  /* ORD-0146: the structured-JSON aside and the review queue are siblings, and the sentence the
     aside prints asks for a review the queue has to show.  Bumping this token is how the shell tells
     the queue "the server has a new row" without either component pretending to know the other's
     business. */
  const [artifactImportedAt, setArtifactImportedAt] = useState(0);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [alertsOpen, setAlertsOpen] = useState(false);
  const [wallet, setWallet] = useState<Wallet | null>(null);
  const [offline, setOffline] = useState<OfflineJourney | null>(null);
  // Event ids whose disruption the traveller has already resolved by accepting
  // a replan.  Read from /replans (which is authoritative for proposal status)
  // as well as from the event status, so the two can never disagree.
  const [handledEventIds, setHandledEventIds] = useState<string[]>([]);
  const [decodedArtifactText, setDecodedArtifactText] = useState<
    string | undefined
  >();
  const [setupOpen, setSetupOpen] = useState(false);
  const [bootstrapJourney, setBootstrapJourney] = useState<{
    id: string;
    cityCode: string;
    cityName?: string;
  } | null>(null);
  const [bootstrapPlan, setBootstrapPlan] = useState<{
    journeyId: string;
    placeId: string;
  } | null>(null);
  const [planAiOpen, setPlanAiOpen] = useState(false);
  // Reference screen 08: the dedicated activity detail surface.
  const [detailActivityId, setDetailActivityId] = useState<string | null>(null);
  /* R14 / ORD-0169: one classification per failure instance.  `failure` holds the Attribution that
     the screen copy, the aria-live announcement, the telemetry record and the recovery button all
     read; the three states it replaced (journeyGone / loadFailure / unreachable) each decided their
     own cause, which is how a 404 became 网络不可用. */
  const [failure, setFailure] = useState<{ attribution: Attribution; journeyId: string | null } | null>(
    null,
  );
  /* The offline classification's recovery action has to lead somewhere: this flag renders the cached
     read-only view on demand instead of reloading into the same failure screen. */
  const [showCachedCopy, setShowCachedCopy] = useState(false);
  /* ORD-0178: whether this device actually holds a readable read-only copy.  Set from the verified
     save on the write path and from the read-back on the offline path, and the only thing that may
     turn a "saved" sentence or a "read the saved copy" button on -- in-memory snapshot state is not
     evidence that anything is on the device. */
  const [cachedCopySaved, setCachedCopySaved] = useState(false);
  const [setupBusy, setSetupBusy] = useState(false);
  /* ORD-0047 — a cold city's first stay search measured 114.98s through the production
     preview, during which this screen showed nothing at all.  The shell is the only party
     that knows which request it is awaiting, so it owns the phase, the clock and the
     controller the traveller's "stop waiting" aborts.  `journeyCreated` keeps the failure
     notice from denying a trip whose row is already committed. */
  const [createPhase, setCreatePhase] = useState<TrialPhase | null>(null);
  const [createStartedAt, setCreateStartedAt] = useState<number | null>(null);
  const [journeyCreated, setJourneyCreated] = useState(false);
  const [stoppedWaiting, setStoppedWaiting] = useState(false);
  const createAbortRef = useRef<AbortController | null>(null);
  // "empty" is a completed search with nothing in it: neither a spinner nor an error.
  const [staySearch, setStaySearch] = useState<StaySearchState>("idle");
  // Whatever `generation.outcome` the server actually reported; null means it said nothing.
  const [stayOutcome, setStayOutcome] = useState<string | null>(null);
  const [stayAttempts, setStayAttempts] = useState(0);
  // Ruling ④ asked for a choice, not just a contract: the root attribute and persistence
  // were already in place, but nothing on screen let anyone pick.
  const [themeChoice, setThemeChoiceState] = useState<ThemeChoice>(() =>
    readChoice(),
  );
  const { locale, switchLocale } = useLocale();
  const zh = locale.startsWith("zh");
  const t = useT();
  // "Needs attention" means an event the traveller has NOT already handled.
  // Acknowledgement is deliberately NOT resolution in this product: an
  // ACKNOWLEDGED trigger means the traveller has *seen* the change, not that a
  // recovery was chosen.  Only RESOLVED, IGNORED, or an accepted replan linked
  // to this event closes it (T3 / docs/ACK_RESOLUTION_CONTRACT.md).  Counting a
  // merely-acknowledged event as handled let Today and the notification badge
  // go quiet while the disruption was still unresolved.
  const allDisruptions = useMemo(
    () => data?.active_plan?.disruptions ?? [],
    [data],
  );
  const isHandledDisruption = useCallback(
    (
      item: V8Data["active_plan"] extends null
        ? never
        : NonNullable<V8Data["active_plan"]>["disruptions"][number],
    ) => {
      const acceptedTriggerIds: Set<string> = new Set(handledEventIds);
      return !isDisruptionOpen(item, acceptedTriggerIds);
    },
    [handledEventIds],
  );
  const disruptions = useMemo(
    () => allDisruptions.filter((item) => !isHandledDisruption(item)),
    [allDisruptions, isHandledDisruption],
  );
  /** Disruptions the traveller has already acted on; explained, not escalated. */
  const handledDisruptions = useMemo(
    () => allDisruptions.filter((item) => isHandledDisruption(item)),
    [allDisruptions, isHandledDisruption],
  );
  const cityName = useMemo(
    () => (data ? cityFromCode(data.journey.city_code) : ""),
    [data],
  );
  const authoritativePlans = useAuthoritativePlans(data?.journey.id ?? null);
  const authoritativeTwin = useAuthoritativeTwin(data?.journey.id ?? null);
  const authoritativePlan =
    authoritativePlans.data?.find(
      (item) => item.status === "ACTIVE" || item.status === "SELECTED",
    ) ??
    authoritativePlans.data?.[0] ??
    null;
  // Distinct from ``authoritativePlan``: that one falls back to the first plan
  // for display, so it cannot answer "has the traveller actually chosen one?".
  const hasChosenPlan = Boolean(
    authoritativePlans.data?.some(
      (item) => item.status === "ACTIVE" || item.status === "SELECTED",
    ),
  );

  const load = async (journeyId: string) => {
    const experience = await v8Request<V8Data>(
      `/api/v1/journeys/${journeyId}/experience`,
    );
    setData(experience);
    const selected =
      experience.options.find(
        (item) => item.id === experience.selected_option_id,
      ) ?? experience.options[0];
    const firstLeg = selected?.leg_mode_map[0];
    if (firstLeg)
      setComparison(
        experience.mobility_facts.filter(
          (item) =>
            item.from_key === firstLeg.from_key &&
            item.to_key === firstLeg.to_key,
        ),
      );
    // Which disruptions already have an accepted proposal behind them?
    try {
      const replans = await v8Request<
        Array<{ trigger_event_id?: string | null; status?: string }>
      >(`/api/v1/journeys/${journeyId}/replans`);
      setHandledEventIds(
        (Array.isArray(replans) ? replans : [])
          .filter(
            (item) => String(item?.status ?? "").toUpperCase() === "ACCEPTED",
          )
          .map((item) => String(item?.trigger_event_id ?? ""))
          .filter(Boolean),
      );
    } catch {
      setHandledEventIds([]);
    }
    const [nextPolicy, nextWallet, nextOffline, nextRuntimeStatus] =
      await Promise.all([
        v8Request<Policy>(
          `/api/v1/travelers/${experience.journey.traveler_id}/agent-policy`,
        ),
        v8Request<Wallet>(`/api/v1/journeys/${journeyId}/wallet`),
        v8Request<OfflineJourney>(
          `/api/v1/journeys/${journeyId}/offline-snapshot`,
        ),
        v8Request<AgentRuntimeStatus>(`/api/v1/agent/runtime-status`),
      ]);
    setPolicy(nextPolicy);
    setRuntimeStatus(nextRuntimeStatus);
    setWallet(nextWallet);
    setOffline(nextOffline);
    /* ORD-0178 ③: whether a copy may be offered is a measurement, not an assumption. */
    setCachedCopySaved(await saveOfflineJourneyVerified(nextOffline));
  };

  const detectBootstrapJourney = async (journeyId: string): Promise<boolean> => {
    try {
      const journey = await v8Request<{
        id: string;
        city_code: string;
        status: string;
        active_plan_version_id?: string | null;
      }>(`/api/v1/journeys/${journeyId}`);
      if (
        String(journey.status).toUpperCase() !== "DRAFT" ||
        journey.active_plan_version_id
      ) {
        return false;
      }
      const places = await v8Request<Array<{ id: string }>>(
        `/api/v1/places?city_code=${encodeURIComponent(journey.city_code)}`,
      );
      if (places.length !== 0) return false;
      setBootstrapJourney({ id: journey.id, cityCode: journey.city_code });
      setSetupOpen(false);
      return true;
    } catch {
      return false;
    }
  };

  // Authoritative V12 mutations refetch PlanVersion/JourneyTwin through
  // TanStack Query.  Keep the browser's read-only Dexie cache on the same
  // server snapshot immediately after those mutations as well; it must never
  // become a second, lagging execution source of truth.
  const refreshAuthoritativeTruth = async (journeyId: string) => {
    await Promise.all([
      authoritativePlans.refetch(),
      authoritativeTwin.refetch(),
    ]);
    const nextOffline = await v8Request<OfflineJourney>(
      `/api/v1/journeys/${journeyId}/offline-snapshot`,
    );
    setOffline(nextOffline);
    setCachedCopySaved(await saveOfflineJourneyVerified(nextOffline));
  };

  useEffect(() => {
    let cancelled = false;
    async function boot() {
      setBusy(true);
      try {
        let journeyId = localStorage.getItem("citynexus.v8.journey_id");
        if (journeyId) {
          if (await detectBootstrapJourney(journeyId)) {
            if (!cancelled) setBusy(false);
            return;
          }
          try {
            await load(journeyId);
          } catch (error) {
            /* R14-S/R14-P: classify once, from the strongest discriminating evidence, and let every
               consumer read that.  An HTTP answer -- any status -- proves the request reached the
               server, so it can never be reported as a network failure; only a transport exception
               may say that.  The health probe exists to *rule the network out* when the read failed
               without a response, not to overwrite a response that already arrived. */
            let attribution = classifyReadFailure(error);
            if (attribution.evidence_kind === "TRANSPORT") {
              const fact = await probeHealth();
              if (cancelled) return;
              if (!fact.ok) attribution = classifyHealthProbe(fact);
            }
            /* ORD-0263: the origin answering badly and the origin never answering both have the same
               legitimate second answer -- the copy this device holds.  Whether it holds one is a
               measurement, so it is folded into the one classification object here rather than guessed
               by the copy table or by a branch that writes its own sentence (R14-P). */
            if (attribution.reason === "NETWORK_UNAVAILABLE" || attribution.reason === "SERVER_ERROR"
                || attribution.reason === "ORIGIN_INVALID_RESPONSE") {
              const saved = await readOfflineJourney(journeyId);
              if (cancelled) return;
              setOffline(saved);
              setCachedCopySaved(Boolean(saved));
              attribution = withCachedCopyFact(attribution, Boolean(saved));
            }
            recordAttribution(attribution, "shell.boot.journeyRead");
            if (cancelled) return;
            if (attribution.reason === "JOURNEY_NOT_FOUND") {
              /* R14-S: the stored reference is invalid, so it is cleared (the whole family of keys,
                 in one place) and the traveller is offered the picker -- never a silent fall back to
                 the demo journey, never left on a stale read-only snapshot. */
              const cleared = clearJourneyReferences();
              recordAttribution(attribution, `shell.boot.cleared:${cleared.join("|") || "none"}`);
              setOffline(null);
            }
            if (!cancelled) {
              setFailure({ attribution, journeyId });
              setBusy(false);
            }
            return;
          }
        }
        if (!journeyId) {
          if (!cancelled) setSetupOpen(true);
          if (!cancelled) setBusy(false);
          return;
        }
        if (!cancelled) setMessage(null);
      } catch (error) {
        /* Reached when something outside the journey read itself threw (storage, the probe).  It goes
           through the same classifier as everything else: R14-P forbids a broad catch that picks its
           own prose, and this branch used to print the raw error message as if it were a diagnosis. */
        const attribution = classifyReadFailure(error);
        recordAttribution(attribution, "shell.boot.unexpected");
        if (!cancelled) {
          setFailure({
            attribution,
            journeyId: localStorage.getItem("citynexus.v8.journey_id"),
          });
        }
      }
      if (!cancelled) setBusy(false);
    }
    void boot();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const onPopState = () => setRoute(routeFromPath());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  /* ORD-0134 ①: bottom-anchored surfaces are bounded by what is actually visible, not by `vh`.
     Installed once here because every overlay that owns a footer needs the same numbers. */
  useEffect(() => installVisualViewportVars(), []);

  useEffect(() => {
    if (!navOpen && !alertsOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setNavOpen(false);
      setAlertsOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [navOpen, alertsOpen]);

  // `selected` is what to preview: the traveller's own choice, or the best
  // candidate so the screens are never empty.  `chosenOption` is what the
  // traveller actually committed to, and is the only thing that may be called
  // "your stay" or counted as spend.
  const selected =
    data?.options.find((item) => item.id === data.selected_option_id) ??
    data?.options[0] ??
    null;
  const chosenOption =
    data?.options.find((item) => item.id === data.selected_option_id) ??
    data?.options.find((item) => item.status === "SELECTED") ??
    null;
  const cycleTheme = () => {
    const next = THEME_NEXT[themeChoice];
    setThemeChoice(next);
    setThemeChoiceState(next);
  };
  /* ORD-0404: an address that names no screen used to be answered by silently showing Today, which
     reads to the traveller as "the link I was given is broken" with nothing saying which side
     failed. Today is still the right surface to land on -- nothing here invents a screen -- but the
     reason has to be said. A `/v8/<uuid>` journey address is deliberately not covered: it names a
     journey, and ORD-0351 already brings that traveller to Today on purpose.
     It is its own surface rather than the shared banner because the banner is cleared by the boot
     read that follows (`load` ends with `setMessage(null)` on success): the measured reading was
     `/v8/nosuchscreen` coming out byte-identical to `/v8/hub` -- 445 chars both -- the sentence set
     and then wiped in the same boot. A verdict about an address belongs to the address, not to
     whichever step last wrote the banner.
     Derived from `route`, not from state written beside it: `navigate` runs its writes inside
     `startViewTransition`, which defers them, so a clear-then-reassert pair written in the demo
     door's own handler came out of React in the other order. Measured on the door path: the
     sentence still did not mount (10 cells, `route_nosuchscreen` the only failing one, verdict
     absent at body=445). Deriving it means the only writer is the navigation that is already
     happening, and the sentence retires exactly when the traveller goes somewhere else. */
  const addressNoScreen =
    deepLink.kind === "unknown" && route === "hub" ? deepLink.segment : null;
  const navigate = (next: string) => {
    const value = next || "hub";
    withViewTransition(() => {
      window.history.pushState({}, "", `/v8/${value}`);
      setRoute(value);
      setNavOpen(false);
      setAlertsOpen(false);
    });
  };
  const openJoinedJourney = async (journeyId: string) => {
    localStorage.setItem("citynexus.v8.journey_id", journeyId);
    setRecentlyJoinedJourneyId(journeyId);
    setFailure(null);
    setShowCachedCopy(false);
    await load(journeyId);
    setSetupOpen(false);
    navigate("live");
  };
  /* ORD-0209 (arbitration `5772122180`): the demo entry addresses the sealed demo journey; it does
     not create one.  The previous body POSTed a traveller and a journey on every press -- HARNESS
     measured 6/6 clicks creating rows.  Now: read the sealed id from the server-owned /demo/scene
     (never a hard-coded UUID), navigate to it, and on any failure show a named fail-closed state.
     The POST path is gone, not commented; the entry either addresses or it says why it cannot.
     "Entry read-only" does not touch the demo-owned actions *inside* the journey (advance, booking,
     interruption, replan) -- those keep their existing ownership constraints, which this order
     explicitly leaves alone. */
  const [sealedDemoUnavailable, setSealedDemoUnavailable] = useState<string | null>(null);
  /* ORD-0273: the technical shape of the sealed-demo read failure (raw message + error name).
     Rendered only inside data-* on the wizard's alert -- a diagnosable fact, not user copy. */
  const [sealedDemoDetail, setSealedDemoDetail] = useState<{ message: string; name: string } | null>(null);
  const enterSealedDemo = async () => {
    setSetupBusy(true);
    setMessage(null);
    setSealedDemoUnavailable(null);
    try {
      const scene = await v8Request<{
        demo_mode?: boolean;
        journey_id?: string | null;
        journey_status?: string | null;
      }>("/api/v1/demo/scene");
      const sealedId = scene.journey_id ?? null;
      if (!scene.demo_mode || !sealedId) {
        /* Fail closed, with the reason the server gave: no silent POST fallback. */
        setSealedDemoUnavailable(
          !scene.demo_mode
            ? locale === "zh-CN"
              ? "这台部署不是演示舞台（demo_mode=false），所以没有可进入的封存演示。"
              : "This deployment is not a demo stage (demo_mode=false), so there is no sealed demo to enter."
            : locale === "zh-CN"
              ? "服务器当前没有给出可用的封存演示行程，所以这里进不去。稍后再试，或新建自己的行程。"
              : "The server currently names no usable sealed demo journey, so this entry is closed. Try again later, or create your own trip.",
        );
        return;
      }
      localStorage.setItem("citynexus.v8.journey_id", sealedId);
      await load(sealedId);
      setSetupOpen(false);
      /* ORD-0404: the gate is a detour, not a destination. Someone who opened `/v8/stay` from a
         chat, or reloaded the page while on 恢复与执行, asked for that screen; passing through the
         sealed-demo door has to put them where they asked, not back on Today. A journey address
         (`/v8/<uuid>`, ORD-0351) names no screen and still belongs on Today, unchanged. */
      navigate(deepLink.kind === "screen" ? deepLink.route : "hub");
    } catch (error) {
      /* ORD-0273-TEXT: the user sentence comes from an i18n key -- the raw error.message (with its
         MIME/content-type and error shapes) must not reach the main copy.
         ORD-0341 (CMDR 15:3x, 甲): ORD-0339's Q71 removed the `data-*` carriers that used to hold
         these facts, and nothing replaced them -- the gate measured 0 records on the attribution
         channel for this failure, so the trip was undiagnosable off the screen.  The facts go to the
         channel that was never about the DOM, classified from the error this read actually raised
         rather than re-worded here, and `sealedDemoDetail` stays as the state the wizard no longer
         renders (removing it is a separate edit with its own blast radius).
         ORD-0349 (HJ-P3): one sentence was covering two different legs.  A leg that never connected
         was told with "the server answered just now", which asserts something that did not happen,
         and the two fates came out as pixel-identical screenshots.  The legs are separated by the
         evidence this device already records -- `evidence_kind`, TRANSPORT versus HTTP_RESPONSE --
         not by a guess at the cause.
         The two orders read the same classification, so one call serves both: ORD-0341 named the
         sink `wizard.sealedDemo.enter` and its shipped gate quotes that string, so ORD-0349's first
         cut borrows the name rather than writing the same record twice under two contexts. */
      const attribution = classifyReadFailure(error);
      setSealedDemoUnavailable(
        attribution.evidence_kind === "HTTP_RESPONSE"
          ? t("trial.sealedUnreadable")
          : t("trial.sealedNoAnswer"),
      );
      recordAttribution(attribution, "wizard.sealedDemo.enter");
      setSealedDemoDetail(
        error instanceof Error
          ? { message: error.message, name: error.name }
          : { message: String(error), name: "Unknown" },
      );
    } finally {
      setSetupBusy(false);
    }
  };
  /**
   * One stay search, one honest outcome.  Cold cities measured 114.98s here and came back
   * with `top_k: []`, so the count -- not the 200 -- is what decides the next screen, and a
   * zero result gets its own state instead of Today's ordinary face.
   */
  const searchStayOptions = async (
    journeyId: string,
    query: string,
    signal: AbortSignal,
  ) => {
    setCreatePhase("stay-options");
    setStaySearch("searching");
    setStayAttempts((current) => current + 1);
    try {
      const generated = await v8Request<{
        top_k?: unknown;
        generation?: { outcome?: string };
      }>(`/api/v1/journeys/${journeyId}/stay-options/generate`, {
        method: "POST",
        body: JSON.stringify({ query }),
        signal,
      });
      const count = Array.isArray(generated?.top_k)
        ? generated.top_k.length
        : 0;
      // An absent outcome stays unknown: the reason on screen must be one the server gave.
      setStayOutcome(
        typeof generated?.generation?.outcome === "string"
          ? generated.generation.outcome
          : null,
      );
      setStaySearch(count === 0 ? "empty" : "found");
      return count;
    } catch (cause) {
      if (!isAbort(cause)) setStaySearch("failed");
      throw cause;
    }
  };

  /** Abandon the wait, not the trip: the server keeps whatever it is doing. */
  const stopWaiting = () => {
    setStoppedWaiting(true);
    createAbortRef.current?.abort();
  };

  /** Today's "look again": the trip is already written, only the search is repeated. */
  const retryStaySearch = async () => {
    if (!data) return;
    const controller = new AbortController();
    createAbortRef.current = controller;
    setStoppedWaiting(false);
    setCreateStartedAt(Date.now());
    try {
      await searchStayOptions(
        data.journey.id,
        `${data.journey.city_code} stay-aware journey`,
        controller.signal,
      );
      await load(data.journey.id);
    } catch (cause) {
      if (!isAbort(cause)) {
        setMessage(
          refusalSentence(cause, locale.startsWith("zh"), t("shell.loadError")),
          refusalFacts(cause),
        );
      }
    } finally {
      createAbortRef.current = null;
      setCreatePhase(null);
      setCreateStartedAt(null);
    }
  };

  const createJourney = async (draft: JourneyTrialDraft) => {
    setSetupBusy(true);
    setMessage(null);
    setStoppedWaiting(false);
    setJourneyCreated(false);
    setStaySearch("idle");
    setStayOutcome(null);
    setStayAttempts(0);
    setCreateStartedAt(Date.now());
    const controller = new AbortController();
    createAbortRef.current = controller;
    // Read from here, not from `journeyCreated`: this closure holds the value from the
    // render it was created in, which is always false during the attempt.
    let tripWritten = false;
    try {
      const cityName = draft.cityName.trim();
      // The real-city discovery provider materialises places under a latin
      // city code (CN-XIAMEN).  Using the raw Chinese name produced CN-厦门,
      // which matched no places and silently yielded zero candidate plans.
      const cityCode = `${draft.countryCode.trim() || "CN"}-${cityCodeSlug(cityName)}`;
      const startAt = localDateTimeInZoneToIso(
        draft.startAt,
        draft.timezone.trim(),
      );
      const endAt = localDateTimeInZoneToIso(
        draft.endAt,
        draft.timezone.trim(),
      );
      setCreatePhase("traveler");
      const traveler = await v8Request<{ id: string; access_token?: string }>("/api/v1/travelers", {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({
          display_name: "City traveler",
          locale: locale === "zh-CN" ? "zh-CN" : "en-GB",
          timezone: draft.timezone.trim(),
        }),
      });
      if (traveler.access_token) storeTravelerBearer(traveler.access_token);
      setCreatePhase("journey");
      const journey = await v8Request<{ id: string }>("/api/v1/journeys", {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({
          traveler_id: traveler.id,
          title: `${cityName} · journey`,
          city_code: cityCode,
          timezone: draft.timezone.trim(),
          start_at: startAt,
          end_at: endAt,
          budget: {
            amount_minor: majorToMinor(draft.budgetMajor, draft.currency),
            currency: draft.currency,
          },
          stay_budget: {
            amount_minor: majorToMinor(draft.stayBudgetMajor, draft.currency),
            currency: draft.currency,
          },
          party_size: draft.partySize,
          occupancy: { adults: draft.partySize, rooms: draft.rooms },
          arrival_anchor: {
            at: startAt,
            location: draft.arrivalLocation.trim(),
          },
          departure_anchor: {
            at: endAt,
            location: draft.departureLocation.trim(),
          },
        }),
      });
      tripWritten = true;
      setJourneyCreated(true);
      localStorage.setItem("citynexus.v8.journey_id", journey.id);
      /* ORD-0238: her words get stored in the contract that already exists.  A failure here is not
         announced, and that is not a cover-up: no sentence on this screen claims the plan was built
         from her interests, so there is nothing to unsay -- what the server actually holds is what
         the gate reads back.  It must not block creating the trip either: the journey is real. */
      try {
        await syncStatedInterests(traveler.id, draft.interests ?? []);
      } catch {
        /* left un-stated on purpose; see the gate `ord0238-2-…` */
      }
      const initialPlaces = await v8Request<Array<{ id: string }>>(
        `/api/v1/places?city_code=${encodeURIComponent(cityCode)}`,
        { signal: controller.signal },
      );
      if (initialPlaces.length === 0) {
        // A new Journey with no catalogue must stop at source-linked discovery;
        // stay search and plan generation would otherwise manufacture a dead end.
        setCreatePhase("experience");
        setSetupOpen(false);
        setBootstrapJourney({ id: journey.id, cityCode, cityName });
        try {
          await load(journey.id);
        } catch {
          setData(null);
        }
        return;
      }
      const stayCount = await searchStayOptions(
        journey.id,
        `${cityName} stay-aware journey`,
        controller.signal,
      );
      setCreatePhase("experience");
      await load(journey.id);
      setSetupOpen(false);
      // Land on Today and open the planning entry: the next thing a traveller
      // wants after creating a trip is a plan, not a discovery console.
      navigate("hub");
      if (stayCount === 0) {
        /* An empty search is not a success to announce: Today carries the empty block with
           its own retry, and opening the planner on top of nothing is the same lie. */
        setMessage(null);
      } else {
        setPlanAiOpen(true);
        setMessage(
          locale.startsWith("zh")
            ? "行程已创建。告诉 AI 你想怎么玩，它会生成可选方案。"
            : "Trip created. Tell the AI how you like to travel and it will build candidate plans.",
        );
      }
    } catch (error) {
      if (isAbort(error)) {
        // No endpoint cancels this search, so say what the traveller gave up on instead.
        setMessage(
          locale.startsWith("zh")
            ? tripWritten
              ? "已停止等待。行程已经创建，住宿搜索可能仍在服务器上运行，稍后回到 Today 查看结果。"
              : "已停止等待，行程还没有创建。"
            : tripWritten
              ? "Stopped waiting. The trip is created and the stay search may still be running server-side — come back to Today for the result."
              : "Stopped waiting — no journey was created.",
        );
      } else {
        setMessage(
          refusalSentence(error, locale.startsWith("zh"), t("shell.loadError")),
          refusalFacts(error),
        );
      }
    } finally {
      setSetupBusy(false);
      setCreatePhase(null);
      createAbortRef.current = null;
    }
  };

  const completeBootstrap = async (placeId: string) => {
    const pending = bootstrapJourney;
    if (!pending) return;
    setBootstrapJourney(null);
    setBootstrapPlan({ journeyId: pending.id, placeId });
    setBusy(true);
    try {
      await load(pending.id);
      navigate("hub");
      setMessage(
        locale.startsWith("zh")
          ? `已添加有来源的公共地点（${placeId}）。确认价格未知后即可生成原生行程。`
          : `A source-linked public place was added (${placeId}). Confirm the unknown price to generate the native Journey.`,
      );
    } catch (error) {
      setBootstrapPlan(null);
      setMessage(
        refusalSentence(error, locale.startsWith("zh"), t("shell.loadError")),
        refusalFacts(error),
      );
    } finally {
      setBusy(false);
    }
  };

  const selectOption = async (id: string) => {
    if (!data) return;
    setBusy(true);
    try {
      await v8Request(
        `/api/v1/journeys/${data.journey.id}/stay-options/${id}/select`,
        { method: "POST" },
      );
      await load(data.journey.id);
      setMessage(t("shell.staySelected"));
    } finally {
      setBusy(false);
    }
  };
  const changeMode = async (next: V8Mode) => {
    if (!data || !selected) return;
    setMode(next);
    setBusy(true);
    try {
      await v8Request(
        `/api/v1/journeys/${data.journey.id}/stay-options/${selected.id}/mode`,
        { method: "POST", body: JSON.stringify({ mode: next }) },
      );
      const response = await v8Request<{ facts: V8Data["mobility_facts"] }>(
        `/api/v1/journeys/${data.journey.id}/mobility/compare`,
        { method: "POST", body: JSON.stringify({ mode: next }) },
      );
      const firstLeg = selected.leg_mode_map[0];
      setComparison(
        firstLeg
          ? response.facts.filter(
              (item) =>
                item.from_key === firstLeg.from_key &&
                item.to_key === firstLeg.to_key,
            )
          : response.facts,
      );
      await load(data.journey.id);
    } finally {
      setBusy(false);
    }
  };
  const triggerRain = async () => {
    if (!data || !selected) return;
    setBusy(true);
    try {
      const result = await v8Request<{
        decision: string;
        executed: boolean;
        after_mode: string;
        reason_codes: string[];
      }>(
        `/api/v1/journeys/${data.journey.id}/stay-options/${selected.id}/autopilot/recover`,
        { method: "POST", body: JSON.stringify({ state: "RAIN" }) },
      );
      await load(data.journey.id);
      setMessage(
        result.executed
          ? t("shell.autopilotRecovered", {
              mode: result.after_mode.toLowerCase(),
            })
          : t("shell.autopilotPaused", {
              reasons: result.reason_codes.join(" · "),
            }),
      );
    } finally {
      setBusy(false);
    }
  };
  const activateAutonomy = async () => {
    if (!data) return;
    setBusy(true);
    try {
      const next = await v8Request<Policy>(
        `/api/v1/travelers/${data.journey.traveler_id}/agent-policy`,
        {
          method: "PUT",
          body: JSON.stringify({
            mode: "AUTONOMOUS",
            auto_accept_replan: true,
            max_auto_replan_cost_delta_minor: 5000,
            max_auto_loss_minor: 0,
            max_auto_schedule_drift_min: 20,
            allowed_activity_types: ["transport", "visit"],
            allowed_booking_types: [],
            protected_commitment_levels: ["C3_Guaranteed", "C4_Anchored"],
            allow_auto_commitment_management: false,
            allow_auto_when_provider_degraded: false,
            allow_auto_on_critical_event: false,
          }),
        },
      );
      setPolicy(next);
      setMessage(t("shell.autonomyEnabled"));
    } finally {
      setBusy(false);
    }
  };
  const importReservation = async (reservation: Record<string, unknown>) => {
    if (!data) return;
    setBusy(true);
    try {
      const draft = await v8Request<{
        id: string;
        status: string;
        unresolved_fields?: string[];
      }>(`/api/v1/journeys/${data.journey.id}/inbox/import`, {
        method: "POST",
        body: JSON.stringify({
          artifact_type: "STRUCTURED_JSON",
          payload: { reservation },
        }),
      });
      if (draft.status === "READY_FOR_CONFIRMATION")
        await v8Request(
          `/api/v1/journeys/${data.journey.id}/inbox/${draft.id}/confirm`,
          { method: "POST" },
        );
      await load(data.journey.id);
      setArtifactImportedAt(Date.now());
      /* "需要审核" on its own is an order with no address.  The server already named what it could
         not resolve, so the sentence says that, and says where the review actually happens. */
      setMessage(
        draft.status === "READY_FOR_CONFIRMATION"
          ? t("shell.reservationConfirmed")
          : t("shell.reservationReviewFields", {
              fields: (draft.unresolved_fields ?? [])
                .map((field) => fieldLabel(field, zh))
                .join("、"),
            }),
      );
    } finally {
      setBusy(false);
    }
  };
  const captureForegroundLocation = async (
    maximumAgeMs = 30_000,
    privateHandoffId?: string,
    signal?: AbortSignal,
  ): Promise<string | null> => {
    if (!data || !navigator.geolocation) return null;
    return await new Promise((resolve) => {
      let settled = false;
      let geolocationWatchdog: number | null = null;
      const finish = (observationId: string | null) => {
        if (settled) return;
        settled = true;
        if (geolocationWatchdog !== null) window.clearTimeout(geolocationWatchdog);
        signal?.removeEventListener("abort", onAbort);
        resolve(observationId);
      };
      const onAbort = () => finish(null);
      if (signal?.aborted) {
        finish(null);
        return;
      }
      signal?.addEventListener("abort", onAbort, { once: true });
      // The browser geolocation timeout below is authoritative; this watchdog
      // also releases the consent UI if a browser never calls either callback.
      geolocationWatchdog = window.setTimeout(() => finish(null), 20_000);
      try {
        navigator.geolocation.getCurrentPosition(
        async (position) => {
          if (settled || signal?.aborted) {
            finish(null);
            return;
          }
          if (geolocationWatchdog !== null) {
            window.clearTimeout(geolocationWatchdog);
            geolocationWatchdog = null;
          }
          try {
            const locationPayload = {
              latitude: position.coords.latitude,
              longitude: position.coords.longitude,
              accuracy_m: position.coords.accuracy,
              captured_at: new Date(position.timestamp).toISOString(),
            };
            if (privateHandoffId) {
              if (signal?.aborted) {
                finish(null);
                return;
              }
              const observation = await cityNexusApi.captureRendezvousBuddyPrivateRouteLocation(
                data.journey.id,
                privateHandoffId,
                locationPayload,
                signal,
              );
              finish(signal?.aborted ? null : observation.location_observation_id);
            } else {
              const observation = await v8Request<{ id: string }>(
                `/api/v1/journeys/${data.journey.id}/location`,
                {
                  method: "POST",
                  headers: travelerAuthHeaders(),
                  body: JSON.stringify(locationPayload),
                },
              );
              await load(data.journey.id);
              setMessage(
                t("shell.locationUpdated", {
                  n: Math.round(position.coords.accuracy),
                }),
              );
              finish(observation.id);
            }
          } catch {
            if (!signal?.aborted && !privateHandoffId) setMessage(t("shell.locationDenied"));
            finish(null);
          }
        },
        () => {
          if (!signal?.aborted && !privateHandoffId) setMessage(t("shell.locationDenied"));
          finish(null);
        },
        { enableHighAccuracy: true, maximumAge: maximumAgeMs, timeout: 15_000 },
        );
      } catch {
        if (!signal?.aborted && !privateHandoffId) setMessage(t("shell.locationDenied"));
        finish(null);
      }
    });
  };
  const shareForegroundLocation = async () => {
    await captureForegroundLocation();
  };

  // Derived for the activity-detail surface (reference 08).
  const allActivities = (data?.active_plan?.activities ??
    []) as unknown as Array<{
    id: string;
    title: string;
    place_id?: string | null;
  }>;
  const detailActivity = detailActivityId
    ? (allActivities.find((item) => item.id === detailActivityId) ?? null)
    : null;
  // Nearby suggestions are other real stops on the same plan that the backend
  // actually returned; nothing is invented.
  const nearbyActivities = detailActivity
    ? allActivities.filter((item) => item.id !== detailActivity.id)
    : [];

  /* R14-P / ORD-0169: one failure view, driven by the classification that was made at the evidence
     boundary.  The kicker, the headline, the body, the announcement and the recovery button are all
     read off `failure.attribution`, so a 404 can no longer be shown as a network failure by a branch
     that wrote its own sentence, and the map is not hidden unless the classified state says so. */
  if (failure && !data && !showCachedCopy) {
    const copy = copyFor(failure.attribution, locale.startsWith("zh"), {
      journeyId: failure.journeyId ?? undefined,
      hasCachedCopy: cachedCopySaved,
    });
    const action =
      failure.attribution.recovery_action === "read-only-cached" && !cachedCopySaved
        ? "retry"
        : failure.attribution.recovery_action;
    const recovery =
      action === "choose-journey"
        ? {
            testId: "failure-choose-journey",
            label: locale.startsWith("zh") ? "选一趟行程" : "Choose a trip",
            run: () => {
              setFailure(null);
              setSetupOpen(true);
            },
          }
        : action === "sign-in"
          ? {
              testId: "failure-sign-in",
              label: locale.startsWith("zh") ? "重新登录" : "Sign in again",
              run: () => {
                setFailure(null);
                window.location.reload();
              },
            }
          : action === "read-only-cached"
            ? {
                testId: "failure-read-cached",
                label: locale.startsWith("zh") ? "看已保存的只读副本" : "Read the saved copy",
                run: () => {
                  setFailure(null);
                  setShowCachedCopy(true);
                },
              }
            : {
                testId: "failure-retry",
                label: locale.startsWith("zh") ? "重试" : "Try again",
                run: () => {
                  setFailure(null);
                  setBusy(true);
                  window.location.reload();
                },
              };
    return (
      <section
        className="v8-page"
        data-testid="failure-view"
        data-attribution-reason={failure.attribution.reason}
        data-evidence-kind={failure.attribution.evidence_kind}
        data-http-status={failure.attribution.http_status ?? ""}
        data-source-layer={failure.attribution.source_layer}
        data-recovery-action={action}
        role="alert"
        aria-live="assertive"
      >
        <div className="v8-page-intro compact">
          <div>
            <p className="v8-kicker">{copy.kicker}</p>
            <h1>{copy.title}</h1>
            <p className="v8-lede" data-testid="failure-reason">
              {copy.body}
            </p>
          </div>
        </div>
        <div className="v8-sheet-primary">
          <button
            type="button"
            className="v8-primary-button"
            data-testid={recovery.testId}
            onClick={recovery.run}
          >
            {recovery.label}
          </button>{" "}
          {action !== "retry" && (
            <button
              type="button"
              className="v8-outline-button"
              data-testid="failure-retry"
              onClick={() => {
                setFailure(null);
                setBusy(true);
                window.location.reload();
              }}
            >
              {locale.startsWith("zh") ? "重试" : "Try again"}
            </button>
          )}
        </div>
        {/* The record the classifier actually produced, kept on screen for the same reason telemetry
            keeps it: a false cause has to be discoverable without a screenshot. */}
        <details className="v8-advanced" data-testid="failure-attribution">
          <summary>{locale.startsWith("zh") ? "归因详情" : "Attribution detail"}</summary>
          <code data-testid="failure-attribution-line">{failure.attribution.telemetry}</code>
        </details>
      </section>
    );
  }

  // Intentional, and ruled so (Amendment F #1): this return sits outside
  // `.v8-product-shell`, so the pre-Journey onboarding screen deliberately has no bottom
  // primary navigation - there is no journey to navigate yet, and a focused first gate is
  // the product decision, not a scoping miss.  Do not "fix" it by moving the return inside
  // the shell: the visual ground for these screens is the onboarding wrapper's job, and a
  // shell migration for one screen is a much larger change than the wording it would buy.
  if (bootstrapJourney)
    return (
      <BootstrapDiscoveryPanel
        journeyId={bootstrapJourney.id}
        cityCode={bootstrapJourney.cityCode}
        cityName={bootstrapJourney.cityName}
        onCompleted={completeBootstrap}
        onCancel={() => {
          setBootstrapJourney(null);
          setMessage(
            locale.startsWith("zh")
              ? "已取消首地点发现；尚未向公共提供方发起请求。"
              : "First-place discovery was cancelled; no public-provider request was made.",
          );
          if (data) navigate("hub");
          else setSetupOpen(true);
        }}
      />
    );
  if (bootstrapPlan)
    return (
      <BootstrapJourneyStartPanel
        journeyId={bootstrapPlan.journeyId}
        placeId={bootstrapPlan.placeId}
        onStarted={async () => {
          setBootstrapPlan(null);
          setBusy(true);
          try {
            await load(bootstrapPlan.journeyId);
            navigate("hub");
            setMessage(
              locale.startsWith("zh")
                ? "原生行程已选择并启动；未知门票价格仍未计入，也未发生预订或扣款。"
                : "The native Journey is selected and active; the unknown admission price remains excluded and no booking or charge was made.",
            );
          } catch (error) {
            setMessage(
              refusalSentence(error, locale.startsWith("zh"), t("shell.loadError")),
              refusalFacts(error),
            );
          } finally {
            setBusy(false);
          }
        }}
        onCancel={() => {
          setBootstrapPlan(null);
          setMessage(
            locale.startsWith("zh")
              ? "已取消原生行程启动；已选地点仍保留，未生成计划或产生预订。"
              : "Native Journey start was cancelled; the selected place remains and no plan or booking was created.",
          );
          navigate("hub");
        }}
      />
    );
  if (setupOpen)
    return (
      <>
        <JourneyTrialSetup
          onCreate={createJourney}
          onUseSealedDemo={enterSealedDemo}
          sealedDemoUnavailable={sealedDemoUnavailable}
          sealedDemoDetail={sealedDemoDetail}
          onCancel={data ? () => setSetupOpen(false) : undefined}
          busy={setupBusy}
          /* Both lanes landed this same line independently: this comment came from ORD-0034's
           remainder (the wizard returns before the shell renders `message`, so a named
           backend rejection died in shell state), and ca65483 passed it again as part of
           ORD-0044's field-level 422 work. Keeping it because the block below the confirm
           button is gated on the wizard's own createAttemptFailed - without this prop the
           reason never arrives, and a stale message from an earlier step still cannot. */
          message={message}
          /* ORD-0353: the same banner already carries this device's readings for the shell's own
             message; the wizard's create failure is that same read, so the fold under it shows the
             measurement rather than a sentence about one. */
          createFacts={banner.facts}
          phase={createPhase}
          startedAt={createStartedAt}
          journeyCreated={journeyCreated}
          stoppedWaiting={stoppedWaiting}
          onStopWaiting={stopWaiting}
        />
        <JourneyMembershipPanel
          journeyId={null}
          recentlyJoinedJourneyId={recentlyJoinedJourneyId}
          onJourneyJoined={openJoinedJourney}
        />
        {/* ORD-0081 ①: a teacher who has never created a journey is standing on this screen, and
            `demo/reset` is the only action that puts a scenario on the stage.  The stage control
            used to exist only inside Hub, which renders when a journey already does -- so the one
            state that needs it had no way to reach it, and measured wizard -> wizard.  Collapsed
            here so the wizard keeps the screen. */}
        <SimulationEntry defaultOpen={false} />
      </>
    );
  if (busy && !data)
    return (
      <div className="v8-loading">
        <div className="v8-brand-mark">CN</div>
        <p>{t("shell.preparing")}</p>
        <span>{t("shell.preparingSub")}</span>
      </div>
    );
  if (!data && offline && showCachedCopy && cachedCopySaved)
    return (
      <OfflineJourneyView
        value={offline}
        originAnswered={failure?.attribution.evidence_kind === "HTTP_RESPONSE"}
        message={
          message
          ?? (failure
            ? copyFor(failure.attribution, locale.startsWith("zh"), {
                journeyId: failure.journeyId ?? undefined,
                hasCachedCopy: cachedCopySaved,
              }).body
            : null)
        }
      />
    );
  if (!data)
    return (
      <div className="v8-join-entry">
        <div className="v8-loading">
          <div className="v8-brand-mark">CN</div>
          <p>{message ?? t("shell.noJourney")}</p>
        </div>
        <JourneyMembershipPanel
          journeyId={null}
          recentlyJoinedJourneyId={recentlyJoinedJourneyId}
          onJourneyJoined={openJoinedJourney}
        />
      </div>
    );

  const routeModes = new Set<string>();
  data.options.forEach((item) => {
    item.leg_modes.filter(Boolean).forEach((itemMode) => routeModes.add(itemMode));
  });
  data.active_plan?.legs.forEach((leg) => {
    if (leg.mode) routeModes.add(leg.mode);
  });
  const routeModeCount = routeModes.size;
  const hasKnownRouteMode = routeModeCount > 0;

  /* ORD-0392: the fourteen `{route === "x" && <Panel/>}` blocks that used to sit in the content
     area are now one table in `app/v8PanelRegistry.tsx`. This file keeps only what it owns: the
     page components defined here and the state they already read, handed to the table as `slots`.
     Adding a panel to a screen is one entry in that table -- no edit in this file. The JSX below
     is the same element tree, moved verbatim; `renderPanels` keys each entry by its id. */
  const routeSlots = {
    hub: () => (
      <Hub
        data={data}
        twin={authoritativeTwin.data ?? null}
        selected={selected}
        hasChosenPlan={hasChosenPlan}
        isHandledDisruption={isHandledDisruption}
        onNavigate={navigate}
        onNewJourney={() => setSetupOpen(true)}
        onPlanWithAi={() => setPlanAiOpen(true)}
        staySearch={staySearch}
        stayOutcome={stayOutcome}
        stayAttempts={stayAttempts}
        waitStartedAt={createStartedAt}
        onRetryStays={() => void retryStaySearch()}
        onStageChanged={() => {
          void load(data.journey.id);
          void authoritativeTwin.refetch();
        }}
      />
    ),
    studio: () => (
      <Studio
        data={data}
        selected={selected}
        chosen={chosenOption}
        plan={authoritativePlan}
        onNavigate={navigate}
        onRain={triggerRain}
      />
    ),
    live: () => (
      <Live
        data={data}
        selected={selected}
        mode={mode}
        onRain={triggerRain}
        onLocation={shareForegroundLocation}
        offline={offline}
      />
    ),
    liveSecondary: () => (
      <LiveSecondary
        data={data}
        selected={selected}
        mode={mode}
        canChangeMode={Boolean(selected)}
        comparison={comparison}
        isHandledDisruption={isHandledDisruption}
        onMode={changeMode}
        onSheet={() => setSheetOpen(true)}
        onRain={triggerRain}
        onLocation={shareForegroundLocation}
      />
    ),
    mapPage: () => (
      <MapPage
        data={data}
        selected={selected}
        comparison={comparison}
        onSheet={() => setSheetOpen(true)}
        selectedActivityId={detailActivityId}
        onSelectActivity={setDetailActivityId}
      />
    ),
    tripsWorkspace: () => (
      <TripsWorkspace
        data={data}
        selected={selected}
        chosen={chosenOption}
        plan={authoritativePlan}
        wallet={wallet}
        onNavigate={navigate}
        onRain={triggerRain}
        onImport={importReservation}
        busy={busy}
        onOpenCopilot={() => setPlanAiOpen(true)}
        onOpenActivity={setDetailActivityId}
        onRefresh={() => void load(data.journey.id)}
      />
    ),
    bookings: () => (
      <Bookings
        data={data}
        selected={selected}
        chosen={chosenOption}
        wallet={wallet}
        onImport={importReservation}
        busy={busy}
        onNavigate={navigate}
      />
    ),
    me: () => (
      <Me data={data} policy={policy} onActivate={activateAutonomy} wallet={wallet} />
    ),
    authoritative: () => (
      <AuthoritativeExecution
        journeyId={data.journey.id}
        plan={authoritativePlan}
        twin={authoritativeTwin.data ?? null}
        policy={policy}
        loading={
          authoritativePlans.isLoading || authoritativeTwin.isLoading
        }
        recovery={data.recovery}
        flags={data.experience_flags}
        currency={data.journey.budget.currency}
        onRefresh={() => void refreshAuthoritativeTruth(data.journey.id)}
      />
    ),
  };
  const routeScope = {
    route,
    data,
    selected,
    hasChosenPlan,
    authoritativePlans,
    authoritativeTwin,
    policy,
    wallet,
    busy,
    mode,
    comparison,
    offline,
    disruptions,
    handledEventIds,
    detailActivity,
    nearbyActivities,
    decodedArtifactText,
    artifactImportedAt,
    runtimeStatus,
    staySearch,
    stayOutcome,
    stayAttempts,
    createStartedAt,
    navigate,
    load,
    refreshAuthoritativeTruth,
    selectOption,
    triggerRain,
    shareForegroundLocation,
    captureForegroundLocation,
    changeMode,
    setSheetOpen,
    setPlanAiOpen,
    setSetupOpen,
    setDetailActivityId,
    setDecodedArtifactText,
    importReservation,
    retryStaySearch,
    activateAutonomy,
    slots: routeSlots,
  };

  return (
    <div className="v8-product-shell">
      <aside className="v8-sidebar">
        <div className="v8-brand">
          <div className="v8-brand-mark">CN</div>
          <div>
            <strong>{t("shell.brand")}</strong>
            <span>{t("shell.tagline")}</span>
          </div>
        </div>
        <div className="v8-workspace-label">
          {t("shell.workspace")} <span>{t("shell.workspaceSub")}</span>
        </div>
        <nav className="v8-nav" aria-label="Product navigation">
          {desktopRoutes.map((item, index) => (
            <div key={item.key}>
              {(index === 0 ||
                desktopRoutes[index - 1].group !== item.group) && (
                <span className="v8-nav-group">{t(navGroupKey(item.group))}</span>
              )}
              <button
                className={route === item.key ? "active" : ""}
                data-contrast-role={route === item.key ? "state" : undefined}
                onClick={() => navigate(item.key)}
              >
                <Icon name={item.icon} />
                <span>{t("nav." + item.key)}</span>
                {item.key === "live" && <i />}
              </button>
            </div>
          ))}
        </nav>
        <div className="v8-sidebar-bottom">
          <div className="v8-sync">
            <span className="status-dot" />
            {t("shell.factsSynced")}
            <div>
              {t(hasKnownRouteMode ? "shell.plansModes" : "shell.plansModesPending", {
                plans: data.options.length || (data.active_plan ? 1 : 0),
                modes: routeModeCount,
              })}
            </div>
          </div>
          <button className="v8-user" onClick={() => navigate("me")}>
            <span>{initialsFrom(cityName)}</span>
            <div>
              <b>{cityName}</b>
              <small>{t("shell.travelers", { n: data.trip.party_size })}</small>
            </div>
            <em>•••</em>
          </button>
        </div>
      </aside>
      <main className="v8-main">
        <header className="v8-topbar">
          <button
            className="v8-mobile-menu"
            aria-label={t("shell.openNav")}
            aria-expanded={navOpen}
            aria-controls="v8-nav-drawer"
            onClick={() => setNavOpen(true)}
          >
            {t("shell.menu")}
          </button>
          <div className="v8-breadcrumb">
            <span>{t("shell.journeys")}</span>
            <b>/</b>
            <strong>
              {t(
                "nav." +
                  (route in
                  {
                    hub: 1,
                    trips: 1,
                    studio: 1,
                    stay: 1,
                    discover: 1,
                    live: 1,
                    bookings: 1,
                    intelligence: 1,
                    twin: 1,
                    agent: 1,
                    authoritative: 1,
                    map: 1,
                    me: 1,
                  }
                    ? route
                    : "hub"),
              )}
            </strong>
          </div>
          <div className="v8-top-actions">
            <span className="v8-runtime">
              <span className="status-dot" />
              {policy?.mode === "AUTONOMOUS"
                ? t("shell.autonomyActive")
                : t("shell.factsReady")}
            </span>
            <button
              className="v9-language"
              data-testid="topbar-theme"
              data-theme-choice={themeChoice}
              data-contrast-role="control"
              aria-label={
                themeChoice === "system"
                  ? "Theme: following the system. Choose light, dark, or back to system."
                  : themeChoice === "light"
                    ? "Theme: pinned to light. Switch to dark or back to system."
                    : "Theme: pinned to dark. Switch back to system or to light."
              }
              onClick={cycleTheme}
            >
              {zh ? THEME_WORD[themeChoice][0] : THEME_WORD[themeChoice][1]}
            </button>
            <button
              className="v9-language"
              data-testid="topbar-language"
              data-contrast-role="control"
              aria-label="Switch language"
              onClick={switchLocale}
            >
              {t("nav.language")}
            </button>
            <div className="v8-alerts">
              <button
                className="v8-icon-button"
                aria-label={
                  disruptions.length
                    ? t("shell.notifActive", { n: disruptions.length })
                    : t("shell.notifNone")
                }
                aria-expanded={alertsOpen}
                aria-haspopup="dialog"
                onClick={() => setAlertsOpen((open) => !open)}
              >
                ◌
                {disruptions.length > 0 && (
                  <i className="v8-alert-badge">{disruptions.length}</i>
                )}
              </button>
              {alertsOpen && (
                <>
                  <button
                    type="button"
                    className="v8-pop-backdrop"
                    aria-label={t("shell.closeNotif")}
                    onClick={() => setAlertsOpen(false)}
                  />
                  <div
                    className="v8-alert-pop"
                    role="dialog"
                    aria-label={t("shell.notifTitle")}
                  >
                    <span className="v8-kicker">{t("shell.signals")}</span>
                    {disruptions.length ? (
                      disruptions.map((item) => (
                        <article key={item.id}>
                          <b>{item.severity}</b>
                          <span>
                            {item.type.replaceAll("_", " ").toLowerCase()}
                          </span>
                          <small>{date(item.occurred_at, locale)}</small>
                        </article>
                      ))
                    ) : (
                      <p className="v8-muted">{t("shell.noDisruptions")}</p>
                    )}
                  </div>
                </>
              )}
            </div>
            <button className="v8-avatar" onClick={() => navigate("me")}>
              {initialsFrom(cityName)}
            </button>
          </div>
        </header>
        {/* ORD-0404: the address verdict. Its own element, not the shared banner, and no dismiss
            button: a sentence about an address is retired by going somewhere, not by hiding it. */}
        {addressNoScreen !== null && (
          <p className="v8-message" role="status" data-testid="address-no-screen">
            {t("shell.addressNoScreen", { segment: addressNoScreen })}
          </p>
        )}
        {message && (
          <div className="v8-message" role="status">
            {message}
            {banner.facts && (
              <RawFactsPanel
                facts={banner.facts}
                zh={locale.startsWith("zh")}
                testId="shell-message-raw-facts"
              />
            )}
            <button onClick={() => setMessage(null)}>
              {t("shell.dismiss")}
            </button>
          </div>
        )}
        <div className="v8-content">
          {(route === "hub" || route === "live") && (
            <JourneyMembershipPanel
              journeyId={data.journey.id}
              journeyStatus={data.journey.status}
              recentlyJoinedJourneyId={recentlyJoinedJourneyId}
              onRefreshLocation={(privateHandoffId, signal) => captureForegroundLocation(0, privateHandoffId, signal)}
              onJourneyJoined={openJoinedJourney}
            />
          )}
          {renderPanels(routePanels, routeScope)}
        </div>
      </main>
      <CopilotDock data={data} onNavigate={navigate} onJourneyUpdated={() => load(data.journey.id)} />
      {/* ORD-0382: the run is no longer owned by whichever surface started it.  This strip is what the
          traveller still sees after closing the sheet -- same seconds, same step words, and the same
          two doors the reference product names (查看会话 / 继续生成). */}
      <PlanAiRunStrip
        journeyId={data.journey.id}
        onOpenRun={() => setPlanAiOpen(true)}
        /* No chat door while the session page is the page on screen -- the strip would be offering to
           walk someone to where they already are. */
        onViewChat={route === "agent" ? undefined : () => navigate("agent")}
      />
      {planAiOpen && (
        <PlanWithAiSheet
          journeyId={data.journey.id}
          cityCode={data.journey.city_code}
          onClose={() => setPlanAiOpen(false)}
          onPlanned={() => void load(data.journey.id)}
          onChooseAnotherCity={() => setSetupOpen(true)}
          onViewChat={() => {
            setPlanAiOpen(false);
            navigate("agent");
          }}
        />
      )}
      <nav className="v8-mobile-nav" aria-label="Mobile navigation">
        {mobileRoutes.map((item) => (
          <button
            key={item.key}
            data-testid={`nav-${item.key}`}
            data-contrast-role={
              route === item.key || (item.key === "map" && route === "live")
                ? "state"
                : undefined
            }
            className={
              route === item.key || (item.key === "map" && route === "live")
                ? "active"
                : ""
            }
            onClick={() => navigate(item.key)}
          >
            <Icon name={item.icon} />
            <span>
              {t(
                (
                  {
                    hub: "mobile.today",
                    trips: "mobile.trips",
                    agent: "mobile.copilot",
                    bookings: "mobile.wallet",
                  } as Record<string, string>
                )[item.key] ?? "nav." + item.key,
              )}
            </span>
          </button>
        ))}
      </nav>
      {navOpen && (
        <>
          <button
            type="button"
            className="v8-drawer-backdrop"
            aria-label={t("shell.closeNav")}
            onClick={() => setNavOpen(false)}
          />
          <nav id="v8-nav-drawer" aria-label="Product navigation">
            <div className="v8-nav-drawer-head">
              <span className="v8-kicker">{t("shell.allRoutes")}</span>
              <button
                type="button"
                className="v8-quiet-button"
                onClick={() => setNavOpen(false)}
              >
                {t("shell.close")}
              </button>
            </div>
            {drawerRoutes.map((item, index) => (
              <div key={item.key}>
                {"group" in item &&
                  (index === 0 ||
                    !("group" in drawerRoutes[index - 1]) ||
                    (drawerRoutes[index - 1] as { group?: string }).group !==
                      item.group) && (
                    <span className="v8-nav-group">
                      {"group" in item ? t(navGroupKey(item.group)) : t("nav.groupOther")}
                    </span>
                  )}
                <button
                  className={route === item.key ? "active" : ""}
                  onClick={() => navigate(item.key)}
                >
                  <Icon name={item.icon} />
                  <span>{t("nav." + item.key)}</span>
                </button>
              </div>
            ))}
          </nav>
        </>
      )}
      {sheetOpen && (
        <MobilitySheet
          facts={comparison.length ? comparison : data.mobility_facts}
          selectedMode={mode}
          canChoose={Boolean(selected && (comparison.length || data.mobility_facts.length))}
          onSelect={(next) => void changeMode(next)}
          onClose={() => setSheetOpen(false)}
        />
      )}
    </div>
  );
}

/**
 * Trips workspace.
 *
 * The previous version stacked itinerary + studio + booking JSON + authority
 * text into one long page.  Travellers answer different questions here, so the
 * screen is split into sub-tabs and the default tab is useful within one
 * phone screen.
 */
function TripsWorkspace({
  data,
  selected,
  chosen,
  plan,
  wallet,
  onNavigate,
  onRain,
  onImport,
  busy,
  onOpenCopilot,
  onOpenActivity,
  onRefresh,
}: {
  data: V8Data;
  selected: V8Data["options"][number] | null;
  chosen: V8Data["options"][number] | null;
  plan: PlanVersion | null;
  wallet: Wallet | null;
  onNavigate: (route: string) => void;
  onRain: () => Promise<void>;
  onImport: (reservation: Record<string, unknown>) => Promise<void>;
  busy: boolean;
  onOpenCopilot: () => void;
  onOpenActivity: (activityId: string) => void;
  onRefresh: () => void;
}) {
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const [tab, setTab] = useState<"itinerary" | "stay" | "bookings" | "spend">(
    "itinerary",
  );
  const tabs = [
    { key: "itinerary" as const, zh: "行程", en: "Itinerary" },
    { key: "stay" as const, zh: "住宿", en: "Stay" },
    { key: "bookings" as const, zh: "预订", en: "Bookings" },
    { key: "spend" as const, zh: "花费", en: "Spend" },
  ];
  const activityCount = data.active_plan?.activities.length ?? 0;
  // Only a *chosen* stay is a committed cost.  Summing every candidate option
  // would invent a spend the traveller never agreed to.
  const chosenStayOption = chosen;
  const chosenStayPriceKnown = Boolean(
    chosenStayOption &&
    ((chosenStayOption.stay?.nightly_amount_minor ?? 0) > 0 ||
      (chosenStayOption.stay_total_minor ?? 0) > 0),
  );
  const chosenStayTotalMinor = chosenStayPriceKnown
    ? chosenStayOption!.stay_total_minor
    : null;
  /* ORD-0392: the tab bodies live in `app/v8PanelRegistry.tsx` now; this file keeps the local
     markup the Spend tab is made of and the `Bookings` / `WalletCommitments` shells it defines.
     A new panel on any of these four tabs is one entry in that table. */
  const tripsSlots = {
    walletCommitments: () => (
      <WalletCommitments wallet={wallet} timezone={data.journey.timezone || undefined} />
    ),
    bookingsAdvanced: () => (
      <Bookings
        showCommitments={false}
        data={data}
        selected={selected}
        chosen={chosen}
        wallet={wallet}
        onImport={onImport}
        busy={busy}
        onNavigate={onNavigate}
      />
    ),
    spend: () => (
      <section className="v8-page" data-testid="trips-spend">
        <div className="v8-page-intro compact">
          <div>
            <p className="v8-kicker">{zh ? "花费" : "Spend"}</p>
            <h1>{zh ? "这趟旅行花多少" : "What this trip costs"}</h1>
            <p className="v8-lede">
              {zh
                ? `预算 ${data.journey.budget.currency} ${Math.round(data.journey.budget.amount_minor / 100)}；当前方案 ${activityCount} 个活动。`
                : `Budget ${data.journey.budget.currency} ${Math.round(data.journey.budget.amount_minor / 100)}; the active plan has ${activityCount} stops.`}
            </p>
          </div>
        </div>
        <dl className="trial-review">
          <div>
            <dt>{zh ? "总预算" : "Total budget"}</dt>
            <dd>
              {data.journey.budget.currency}{" "}
              {Math.round(data.journey.budget.amount_minor / 100)}
            </dd>
          </div>
          <div>
            <dt>{zh ? "住宿合计（已选）" : "Stay total (selected)"}</dt>
            <dd data-testid="spend-stay-total">
              {!chosenStayOption
                ? zh
                  ? "还没有选定住宿"
                  : "No stay chosen yet"
                : chosenStayTotalMinor == null
                  ? zh
                    ? "价格未知（供应商未提供房价）"
                    : "Price unknown (no provider rate)"
                  : `${chosenStayOption.stay.currency} ${Math.round(chosenStayTotalMinor / 100)}`}
            </dd>
          </div>
          <div>
            <dt>{zh ? "活动数量" : "Stops"}</dt>
            <dd>{activityCount}</dd>
          </div>
        </dl>
        <Studio
          data={data}
          selected={selected}
          chosen={chosen}
          plan={plan}
          onNavigate={onNavigate}
          onRain={onRain}
        />
      </section>
    ),
  };
  const tripsTabScope = {
    tab,
    data,
    onOpenCopilot,
    onOpenActivity,
    onRefresh,
    zh,
    slots: tripsSlots,
  };

  return (
    <>
      <nav
        className="trips-tabs"
        data-testid="trips-tabs"
        aria-label={zh ? "行程分区" : "Trip sections"}
      >
        {tabs.map((item) => (
          <button
            key={item.key}
            type="button"
            className={tab === item.key ? "active" : ""}
            data-testid={`trips-tab-${item.key}`}
            onClick={() => setTab(item.key)}
          >
            {zh ? item.zh : item.en}
          </button>
        ))}
      </nav>

      {renderPanels(tripsTabPanels, tripsTabScope)}
    </>
  );
}

/**
 * A truthful, deliberately approximate budget sanity signal.
 *
 * It never claims an exact trip cost, and it no longer computes anything: the backend
 * divides the traveller's declared budget by the authoritative trip shape
 * (`backend/app/core/temporal.py`).  This function used to round the hours between the
 * anchors into days and add one, which turned a twelve-hour same-day trip into "2 天" — and
 * then divided the budget by that invented second day (ORD-0063).
 *
 * The money is shown in the journey's own currency (ORD-0064).  `¥` was hard-coded here,
 * and `¥` is also JPY's sign, so a symbol could not be trusted at all; the ISO code can be.
 */
const CURRENCY_CODE = /^[A-Z]{3}$/;

/** `budgetMoney` rather than `money`: this module already aliases `money` to formatMinorMoney. */
function budgetMoney(
  minorUnits: number,
  currency: string,
  locale: string,
): string {
  if (!CURRENCY_CODE.test(currency)) return "";
  try {
    return new Intl.NumberFormat(locale, {
      style: "currency",
      currency,
      currencyDisplay: "code",
      maximumFractionDigits: currencyExponent(currency),
    }).format(minorUnits / currencyMinorFactor(currency));
  } catch {
    // An unsupported code must not fall back to a plausible symbol.
    return "";
  }
}

/* ORD-0226 (red team arm E1): a word table cannot pin down what a caveat says, because paraphrase is
   unbounded.  Both sentences are frozen and `e2e/qoder-budget-advisory-tiers.spec.ts` asserts them
   character for character -- so changing the copy means editing that gate in the same commit.  The
   English arm is pinned too because it was measured on screen: `resolveLocale` falls through to
   `navigator.language`, so a non-Chinese browser renders English by default. */
const TIGHT_CAVEAT_ZH =
  "按这个预算，住宿、交通和吃饭很可能超出计划。可以先调整预算，或你确认继续也可以。";
const TIGHT_CAVEAT_EN =
  "At this budget, lodging, transport and food are likely to exceed the plan. Adjust the budget, or continue intentionally.";

function budgetAdvisory(
  reference: V8Data["trip"]["budget_reference"],
  zh: boolean,
  locale: string,
): { tight: boolean; text: string; hint: string } | null {
  if (!reference || !reference.available) return null;
  const days = reference.day_count;
  const people = reference.party_size;
  const perDay = reference.per_day_minor;
  const perPersonPerDay = reference.per_person_per_day_minor;
  const currency = reference.currency;
  if (
    days === null ||
    people === null ||
    perDay === null ||
    perPersonPerDay === null ||
    !currency ||
    !budgetMoney(perPersonPerDay, currency, locale)
  )
    return null;
  // `floor_comparable` is the backend saying "this CNY price level applies to your money".
  // For a JPY or SGD budget it is false, and calling the trip tight would be a verdict
  // reached by comparing yen against yuan.
  const tight =
    Boolean(reference.floor_comparable) &&
    perPersonPerDay < (reference.floor_minor ?? 0);
  const amount = budgetMoney(perPersonPerDay, currency, locale);
  return {
    tight,
    text: zh
      ? `${days} 天 · ${people} 人 · 每人每天约 ${amount}`
      : `${days} days · ${people} people · about ${amount} per person per day`,
    hint: tight
      ? zh
        ? TIGHT_CAVEAT_ZH
        : TIGHT_CAVEAT_EN
      : reference.floor_comparable
        ? zh
          ? "这是按你的预算平均下来的参考值，不是最终花费；真实价格以供应商为准。"
          : "This is your own budget averaged out, not a final cost. Real prices come from providers."
        : zh
          ? `这是按你的预算平均下来的参考值（以 ${currency} 计），不是最终花费；真实价格以供应商为准。`
          : `This is your own budget averaged out, in ${currency}, not a final cost. Real prices come from providers.`,
  };
}

function Hub({
  data,
  twin,
  selected,
  hasChosenPlan,
  isHandledDisruption,
  onNavigate,
  onNewJourney,
  onPlanWithAi,
  staySearch,
  stayOutcome,
  stayAttempts,
  waitStartedAt,
  onRetryStays,
  onStageChanged,
}: {
  data: V8Data;
  /** ORD-0082 ②: carries the server's own NOW/NEXT projection (`context.execution`) -- the one
      rule for "what is now / what is next".  Live already reads it; Today deriving a second rule
      out of activity order was the defect. */
  twin: JourneyTwin | null;
  selected: V8Data["options"][number] | null;
  /** True only when the traveller actually adopted a plan. */
  hasChosenPlan: boolean;
  /** Whether a disruption has already been dealt with (accepted replan). */
  isHandledDisruption: (
    item: NonNullable<V8Data["active_plan"]>["disruptions"][number],
  ) => boolean;
  onNavigate: (route: string) => void;
  onNewJourney: () => void;
  onPlanWithAi: () => void;
  /* ORD-0047: a stay search that answered "200, nothing" must not wear Today's ordinary
     face, and one that is running must be countable. */
  staySearch: StaySearchState;
  stayOutcome: string | null;
  stayAttempts: number;
  waitStartedAt: number | null;
  onRetryStays: () => void;
  /** ORD-0082: after the stage moves, Today must re-read the journey it is showing.  The panel
      names which action ran; which journey to re-read stays this screen's own fact. */
  onStageChanged: () => void;
}) {
  const t = useT();
  const { locale, switchLocale } = useLocale();
  const zh = locale.startsWith("zh");
  const activePlanHasKnownMode = Boolean(
    data.active_plan?.legs.some((leg) => Boolean(leg.mode)),
  );
  /* ORD-0232: "fewer stops than I asked for" needs its reason on the same screen that shows the
     stops.  An empty list is also what a still-running or failed read gives: no rows, no claims. */
  const dropped = useCandidateRejections(data.journey.id, data.journey.city_code).data ?? [];
  /* ORD-0269 cell 1: the continuation reveal is derived from a device-local marker joined
     against the real agent-feed events.  Its coverage boundary is named, not hidden. */
  const { state: continuation, feedReadFailed: continuationFeedFailed } = useContinuationState(
    data.journey.id,
  );
  /* ORD-0278 -- the one deliberate continue press.  `resumingRef` is synchronous and ahead of every
     await because a rendered `disabled` cannot be the re-entry guard (two clicks in one tick both
     arrive while it still reads enabled).  The transport key is minted INSIDE that guard, so the
     refused second click consumes no key and nothing is stored between presses: the key identifies
     this intent, it is not a session token or a cooldown. */
  const resumingRef = useRef(false);
  const [resuming, setResuming] = useState(false);
  const [resumeOutcome, setResumeOutcome] = useState<ContinuationOutcome | null>(null);
  const resumePendingPlan = async (missionId: string) => {
    if (resumingRef.current) return;
    resumingRef.current = true;
    setResuming(true);
    setResumeOutcome(null);
    const outcome = await continuePendingPlan({
      journeyId: data.journey.id,
      missionId,
      idempotencyKey: crypto.randomUUID(),
      /* The hub's own read deadline -- the same single clock ORD-0263 established, not a fourth
         waiting constant.  A resume that could not answer inside it says so instead of hanging. */
      deadlineMs: READ_DEADLINE_MS,
    });
    /* The marker leaves only after the server accepted this leg; a refusal, a timeout or a dead
       transport keeps it, because "I could not finish" is still the truth the traveller deserves.
       Clause 5 in the other direction: nothing here clears the marker before a request goes out. */
    if (outcome.kind === "accepted") clearPendingContinuation(data.journey.id);
    setResumeOutcome(outcome);
    if (outcome.kind === "refused") {
      /* ORD-0344-b: the server's sentence left the traveller's line, so the diagnosis goes through
         the channel that was never about the DOM -- `classifyReadFailure` + `recordAttribution`,
         the same sink ORD-0273's technical facts use.  It is classified from the status this device
         saw; when the answer sent no code the record stays unclassified, which is the honest state
         rather than a guessed cause.  `copyFor` is deliberately not painted here: its title for a
         409 is 「这一次没读到」, while this paragraph says the attempt was refused and a shipped gate
         holds that exact sentence. */
      recordAttribution(
        classifyReadFailure(null, { status: outcome.httpStatus, code: null, message: null }),
        "hub.continuation.resume.refused",
      );
    }
    setResuming(false);
    resumingRef.current = false;
    if (outcome.kind === "accepted" && outcome.planIds.length > 0) onStageChanged();
  };

  const stayWaited = useElapsedSeconds(
    staySearch === "searching" ? waitStartedAt : null,
  );
  // The traveller may intentionally proceed after seeing a tight-budget warning.
  const [advisoryDismissed, setAdvisoryDismissed] = useState(false);

  // The journey carries a city code (CN-XIAMEN); derive a traveller-facing name
  // from it rather than showing the raw code.
  const cityLabel = (() => {
    const code = String(data.journey.city_code || "");
    const tail = code.includes("-") ? code.split("-").slice(1).join("-") : code;
    if (!tail) return "";
    const cleaned = tail.replaceAll("_", " ");
    // The product creates latin slugs (CN-XIAMEN).  A Chinese traveller must
    // read the city name in Chinese, not a romanised code, so map the known
    // slugs back to their local name.
    const localName = CITY_NAME_BY_SLUG[cleaned.toUpperCase()];
    if (localName) return locale.startsWith("zh") ? localName.zh : localName.en;
    if (/[一-鿿]/.test(cleaned)) return cleaned;
    return cleaned
      .toLowerCase()
      .split(/\s+/)
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(" ");
  })();

  // ORD-0063: the day count is projected by the backend, not derived here.  The removed line
  // rounded the span between the anchors into whole days and added one, which called a
  // 08:00→20:00 trip "2 天" and then divided the budget by that invented second day.  A null
  // means the stored window cannot be ordered at all, which shows as unknown, never as 1.
  const tripDayCount = data.journey.temporal_shape?.day_count ?? null;
  /* ORD-0082 ④: until the map reports, fall back on the data-level predicate; once it reports,
      take *its* answer.  The predicate alone was wrong in one direction -- a trip with route
      geometry but no located activity is perfectly placeable, and attention was calling that
      "没有可信的坐标" while the map drew the route.  Converging on the map's decision removes
      both directions of the contradiction without new wording. */
  const [mapReportsNoLocation, setMapReportsNoLocation] = useState<
    boolean | null
  >(null);
  const tripUnlocatable = mapReportsNoLocation ?? tripHasNoCoordinates(data);
  const advisory = budgetAdvisory(data.trip.budget_reference, zh, locale);
  const activities = (data.active_plan?.activities ?? []) as unknown as Array<{
    id: string;
    title: string;
    start_at: string;
    end_at: string;
    type?: string | null;
  }>;
  /* ORD-0082: "now" on this screen is the stage's, not the device's.  Hub used to pick Now/Next
     and its calendar day from `Date.now()`, so after the server had walked a scenario past a
     stop, this screen still advertised it as upcoming -- one part of the same viewport saying
     "现在 = 12:00" while the line under it said "接下来 · 09:00 Old Town Gate".  Parsing the
     server's instant is not a second clock; reading the device is.  The device is only used when
     there is no readable scenario, and which of the two is in play is said out loud below
     (`today-clock-source`) -- a silent fallback is exactly how two clocks pass for one truth. */
  const stage = useDemoScene();
  const stageInstant = stage.scene?.effectiveNow ?? null;
  const stageMs = stageInstant ? Date.parse(stageInstant) : null;
  const usingDeviceClock = stageMs === null;
  const nowMs = stageMs ?? Date.now();
  const nextStop =
    activities.find((item) => Date.parse(item.end_at) >= nowMs) ??
    activities[0] ??
    null;
  // "Today" is a calendar day in the city the traveller is in, not on the
  // device that happens to be open — and the clock must be the city's clock.
  const journeyTz = data.journey.timezone || "UTC";
  const todayKey = dateKeyInZone(
    stageInstant ?? new Date().toISOString(),
    journeyTz,
  );
  const todayStops = activities.filter(
    (item) => dateKeyInZone(item.start_at, journeyTz) === todayKey,
  );
  const chosenStay =
    data.options.find((option) => option.status === "SELECTED") ?? null;
  const planDisruptions = data.active_plan?.disruptions ?? [];
  // A handled event is history, not an open problem.  An ACKNOWLEDGED trigger
  // stays in the plan payload (acknowledgement is not resolution), so it must
  // not keep Today in a permanent "needs attention" state.
  const disruptions = planDisruptions.filter(
    (item) => !isHandledDisruption(item),
  );
  const handledDisruptions = planDisruptions.filter((item) =>
    isHandledDisruption(item),
  );
  // Single-focus hero: "Now" only when something is genuinely in progress, and
  // "Next" as the one upcoming action. No empty current slot is ever fabricated.
  /* ORD-0082 ②: one rule, and it is the server's.  The twin publishes `context.execution` -- now,
     next, and whether its own cursor can be trusted -- off the persisted activity state machine,
     and it is the same block Live reads.  Picking "next" here by activity order grew a second rule
     that could disagree with the first while both were internally correct, which is exactly what
     this order was opened against.  A cursor the server itself refuses (`STALE`: it names an
     activity that is not on this plan) is a degraded screen rather than a station choice, so that
     one case keeps the scheduled-window reading and says so in the line below. */
  const execution = (
    twin?.context as
      | {
          execution?: {
            now_activity_id?: string | null;
            next_activity_id?: string | null;
            cursor_status?: string | null;
          };
        }
      | undefined
  )?.execution;
  const cursorStatus =
    execution?.cursor_status ?? data.active_plan?.cursor_status ?? null;
  const byStage = !usingDeviceClock && Boolean(execution);
  const useProjection = byStage && cursorStatus !== "STALE";
  const projectedCurrent = useProjection
    ? (activities.find((item) => item.id === execution?.now_activity_id) ??
      null)
    : null;
  const projectedNext = useProjection
    ? (activities.find((item) => item.id === execution?.next_activity_id) ??
      null)
    : null;
  const windowCurrentStop = byStage
    ? null
    : (activities.find(
        (item) =>
          Date.parse(item.start_at) <= nowMs && Date.parse(item.end_at) >= nowMs,
      ) ?? null);
  const currentStop = useProjection
    ? projectedCurrent
    : windowCurrentStop;
  const nextUp = useProjection
    ? projectedNext
    : windowCurrentStop
      ? (activities.find(
          (item) =>
            Date.parse(item.start_at) > Date.parse(windowCurrentStop.end_at),
        ) ?? null)
      : nextStop;
  const heroClock = nextUp ? clockInZone(nextUp.start_at, journeyTz) : null;
  /* ORD-0082 (a): how many stops the server still has after the advertised one, read off its own
     sequence.  "暂无安排" may only be said when that number is actually zero. */
  const remainingAfterNext = (() => {
    const planned = data.active_plan?.activities ?? [];
    const advertised = nextUp ? planned.find((item) => item.id === nextUp.id) ?? null : null;
    if (!advertised) return 0;
    return planned.filter(
      (item) =>
        item.sequence_no > advertised.sequence_no &&
        !["COMPLETED", "SKIPPED", "CANCELLED"].includes(item.status),
    ).length;
  })();

  return (
    <section className="v8-page">
      <div className="v8-page-intro">
        <div>
          <p className="v8-kicker">{greetingFor(new Date().getHours(), t)}</p>
          {/* Today answers the traveller's four questions within one viewport:
              where am I going, when, what is next, and what needs attention. */}
          <h1 data-testid="today-heading">
            {cityLabel
              ? zh
                ? `${cityLabel}之旅`
                : `Trip to ${cityLabel}`
              : data.journey.title}
          </h1>
          {staySearch === "searching" && (
            <div
              className="hub-stay-searching"
              data-testid="hub-stay-searching"
              data-stay-search="searching"
            >
              <strong>
                {zh ? "正在搜索住宿" : "Looking for stays"} ·{" "}
                <span data-testid="hub-stay-elapsed">
                  {zh ? `已等待 ${stayWaited} 秒` : `waiting ${stayWaited}s`}
                </span>
              </strong>
              <span>
                {zh
                  ? "服务器不会中途汇报进度，这里只有真实等待时间。第一次搜索一个城市可能要一两分钟。"
                  : "The server reports nothing until it finishes, so this is measured waiting time. A city's first search can take a minute or two."}
              </span>
            </div>
          )}
          {staySearch === "failed" && (
            <div
              className="hub-stay-refused"
              data-testid="hub-stay-refused"
              data-stay-search="failed"
              role="alert"
            >
              <strong>
                {zh
                  ? "住宿搜索失败了，行程本身还在。"
                  : "The stay search failed — your trip is untouched."}
              </strong>
              <button
                type="button"
                className="v8-outline-button"
                data-testid="hub-stay-retry"
                onClick={onRetryStays}
              >
                {zh ? "再查一次" : "Search again"}
              </button>
            </div>
          )}
          {staySearch === "empty" && data.options.length === 0 && (
            /* A 200 whose top_k is empty is a completed search with no answer in it. Rendering
               Today as if it had found stays is the same defect as showing a spinner forever:
               the traveller cannot tell an empty result from a working one. */
            <section
              className="hub-stay-empty"
              data-testid="hub-no-stays"
              data-stay-search="empty"
              data-stay-outcome={stayOutcome ?? "unknown"}
              role="status"
            >
              <strong>
                {zh
                  ? "这次没找到可推荐的住宿"
                  : "No stay options came back this time"}
              </strong>
              <p>{stayOutcomeLine(stayOutcome, zh)}</p>
              <div className="hub-stay-empty-actions">
                <button
                  type="button"
                  className="v8-primary-button"
                  data-testid="hub-stay-retry"
                  onClick={onRetryStays}
                >
                  {zh ? "再查一次" : "Search again"}
                </button>
                <button
                  type="button"
                  className="v8-quiet-button"
                  onClick={() => onNavigate("stay")}
                >
                  {zh ? "去住宿页看看" : "Open the stay page"}
                </button>
              </div>
              {stayAttempts > 1 && (
                <small data-testid="hub-stay-attempts">
                  {zh ? `第 ${stayAttempts} 次查询` : `attempt ${stayAttempts}`}
                </small>
              )}
            </section>
          )}
          {advisory && advisory.tight && !advisoryDismissed && (
            /* ORD-0212 (user, 15:00): the hub keeps only the `tight` verdict -- it carries real
               information and an action.  The averaged-reference tier (text + hint without tight)
               is gone from the hub entirely; its paired text+hint still lives on the budget-review
               surface.  Invariant: `每人每天约 X` never appears without its caveat, so when this
               block is absent the number is absent with it. */
            <div
              className={`today-advisory${advisory.tight ? " tight" : ""}`}
              data-testid="budget-advisory"
              data-tight={advisory.tight ? "true" : "false"}
            >
              <strong>{advisory.text}</strong>
              <span data-testid="budget-advisory-caveat">{advisory.hint}</span>
              <div className="today-advisory-actions">
                <button
                  type="button"
                  className="v8-outline-button"
                  data-testid="budget-advisory-edit"
                  onClick={onNewJourney}
                >
                  {zh ? "调整预算" : "Adjust budget"}
                </button>
                <button
                  type="button"
                  className="v8-quiet-button"
                  data-testid="budget-advisory-continue"
                  onClick={() => setAdvisoryDismissed(true)}
                >
                  {zh ? "我知道了，继续" : "Got it, continue"}
                </button>
              </div>
            </div>
          )}
          {continuation.kind === "pending" && (
            /* ORD-0269 cell 1: an interrupted plan is a durable fact, not a silence.  The marker is
               device-local, so the sentence itself names the boundary ("on this device").  The resume
               gate is MANUAL_RESUME / NOT-EXACTLY-ONCE until ORD-0272 gives the continuation a durable
               server-side identity -- no marker clearing happens before any request is sent. */
            <section className="today-continuation" data-testid="continuation-pending">
              <strong data-testid="continuation-pending-heading">
                {t("hub.continuationPending")}
              </strong>
              <button
                type="button"
                className="v8-primary"
                data-testid="continuation-resume"
                disabled={resuming}
                onClick={() => void resumePendingPlan(continuation.missionId)}
              >
                {resuming ? t("hub.continuationResuming") : t("hub.continuationResume")}
              </button>
            </section>
          )}
          {/* The outcome stands OUTSIDE the card on purpose: an accepted resume clears the marker,
              which unmounts the card, and a sentence living inside it would vanish with it -- a
              successful continue would then be the one outcome the screen says nothing about. */}
          {resumeOutcome && (
            <>
              <p
                className="today-continuation-resume-outcome"
                data-testid="continuation-resume-outcome"
                data-outcome={resumeOutcome.kind}
              >
                {resumeOutcome.kind === "accepted"
                  ? resumeOutcome.planIds.length > 0
                    ? t("hub.continuationResumed")
                    : t("hub.continuationResumedEmpty")
                  : null}
                {resumeOutcome.kind === "refused" && (
                  <>
                    {t("hub.continuationResumeRefused")}
                    {!resumeOutcome.serverReason && (
                      /* The label the traveller must not be lied to about: an HTTP status this screen
                         wrote itself is not "the server's reason".  Q68 says the same line from the
                         other side -- no number reached us, so the screen names that instead of
                         borrowing one. */
                      <span data-testid="continuation-resume-no-server-reason">
                        {zh
                          ? "服务器没有给出原因文字（这条是本机说的，不是服务器说的）。"
                          : "The server sent no reason text -- this sentence is ours, not the server's."}
                      </span>
                    )}
                  </>
                )}
                {resumeOutcome.kind === "deadline" && t("hub.continuationResumeDeadline")}
                {resumeOutcome.kind === "transport" && t("hub.continuationResumeTransport")}
              </p>
              {resumeOutcome.kind === "refused" && resumeOutcome.serverReason && (
                /* ORD-0344-b (Q64): the server's own words stay attributed but leave the
                   traveller's line -- they sit in the technical-detail fold, the same home ORD-0323
                   accepted for the plan sheet.  The fold is a sibling of the paragraph, not a child:
                   a `<details>` inside a `<p>` makes the browser close the paragraph early, and then
                   the sentence this screen wrote and the sentence the server wrote no longer share
                   one element for a gate to read. */
                <details className="today-continuation-technical" data-testid="continuation-technical-details">
                  <summary data-testid="continuation-technical-toggle">
                    {zh ? "技术详情" : "Technical detail"}
                  </summary>
                  <span data-testid="continuation-resume-server-reason">
                    {`${zh ? "服务器给出的原因：" : "Server's own reason: "}`}
                    {resumeOutcome.serverReason}
                  </span>
                  {/* Q70: the field this device measured, under its contract name, on the element
                      carrying it. */}
                  <span
                    className="today-continuation-technical-reading"
                    data-testid="continuation-resume-http-status"
                    data-cnx-field="http_status"
                    data-cnx-value={String(resumeOutcome.httpStatus)}
                  >
                    {zh
                      ? `这台设备看到的回读：HTTP ${resumeOutcome.httpStatus}`
                      : `This device's reading: HTTP ${resumeOutcome.httpStatus}`}
                  </span>
                  {/* Q68: the number a traveller could repeat is the one thing this refusal did not
                      send -- `continuePendingPlan` hands back a status and a sentence, no code.  The
                      screen says that instead of leaving the slot empty or dressing the status up
                      as a code.  No anchor, because there is no value to anchor. */}
                  <span
                    className="today-continuation-technical-nocode"
                    data-testid="continuation-resume-no-request-id"
                  >
                    {zh
                      ? "这一屏没有拿到可复述的编号（这条读取没把它送过来）。"
                      : "This screen holds no repeatable code -- this read did not carry one."}
                  </span>
                </details>
              )}
            </>
          )}
          {continuation.kind === "unconfirmable" && (
            /* ORD-0269 cell 3: a marker exists but the feed cannot confirm the mission (read failed
               or the ascending capped page excluded it).  Named state, never silence. */
            <p className="today-continuation-unconfirmable" data-testid="continuation-unconfirmable">
              {t("hub.continuationUnconfirmable")}
            </p>
          )}
          {dropped.length > 0 && (
            /* ORD-0232: the number of stops the traveller asked for and the number the plan can
               defend are different, and the difference is a server fact with a named cause.  The raw
               id and the raw code live in data-* only -- what reaches the eye, and what a screen
               reader speaks, is a place name and a condition a person can read. */
            <section
              className="today-dropped"
              data-testid="dropped-candidates"
              data-count={dropped.length}
            >
              <strong data-testid="dropped-candidates-heading">
                {t("hub.droppedCandidates", { count: dropped.length })}
              </strong>
              <ul>
                {dropped.map((row) => (
                  <li
                    key={row.key}
                    data-testid="dropped-candidate-row"
                    data-place-id={row.placeId ?? "unresolved"}
                    data-rule-code={row.ruleCode ?? "unresolved"}
                  >
                    {t("hub.droppedCandidateRow", {
                      place: row.placeName ?? t("hub.droppedCandidateUnknownPlace"),
                      rule: hardRuleLabel(
                        row.ruleCode,
                        zh,
                        t("hub.droppedCandidateUnknownRule"),
                      ),
                    })}
                  </li>
                ))}
              </ul>
            </section>
          )}
          <p className="v8-lede" data-testid="today-when">
            {dateInZone(data.journey.start_at, locale, journeyTz)} —{" "}
            {dateInZone(data.journey.end_at, locale, journeyTz)}
            {" · "}
            {t("shell.travelers", { n: data.trip.party_size })}
            {" · "}
            {tripDayCount === null
              ? zh
                ? "天数未知"
                : "length unknown"
              : zh
                ? `${tripDayCount} 天`
                : `${tripDayCount} days`}
          </p>
        </div>
        {/* Pass-8 ruling #1/#2: with an active journey the hero's
            "查看实时行程" is the ONLY primary action on this screen. These are
            path-switching and authoring utilities, so they read as one line of
            quiet text links below the summary instead of four stacked
            full-width buttons competing with the hero. The duplicated language
            toggle is gone — the topbar already owns that control. */}
        <div className="v8-hub-actions" data-testid="hub-secondary-actions">
          <button
            type="button"
            className="v8-quiet-button"
            data-testid="hub-plan-with-ai"
            onClick={onPlanWithAi}
          >
            {locale.startsWith("zh") ? "AI 帮我规划" : "Plan my trip with AI"}
          </button>
          <button
            type="button"
            className="v8-quiet-button"
            onClick={onNewJourney}
          >
            {t("trial.newJourney")}
          </button>
          <button
            type="button"
            className="v8-quiet-button"
            onClick={() => onNavigate("studio")}
          >
            {t("hub.openStudio")}
          </button>
          {/* ORD-0366: Today is the screen a traveller is actually on when they want to hand the
              trip to someone, so the link action lives here too -- in this same quiet row, not as a
              second primary button competing with the hero (Pass-8 ruling #1/#2 above stays intact). */}
          <JourneyLinkAction journeyId={data.journey.id} variant="quiet" />
        </div>
      </div>
      {/* ORD-0059: the Simulation entry belongs on Today, because Today is the screen whose
          "now" is being staged.  Whether this deployment is a stage is the server's answer
          (`GET /demo/scene` -> demo_mode), never a path this file guesses at. */}
      {/* ORD-0082 ②: the callback has to reach the panel, or "the stage moved" never arrives on
          this screen -- a hero that is not told to re-read cannot follow anything. */}
      <SimulationEntry
        viewingJourneyId={data.journey.id}
        onStageChanged={onStageChanged}
      />
      {/* ORD-0406: this band used to sit below the map, which on a phone puts it at y=1037
          while the fixed bar starts at 724 -- measured on 360x800, so the row a traveller
          has to hit was under the bar before any scrolling. Moved above the hero grid, which
          keeps the hero/map order intact and costs everything below one band height. */}
      {/* Attention is its own band, not a tile squeezed beside "Next"/"Stay", and an all-clear is a
          quiet line, not a card. The `today-attention` element is always rendered (same logic and
          strings as before); only its presentation escalates via data-attention.
          The "loudest thing after Now/Next" placement this file used to argue for is superseded by
          the ORD-0406 measurement above: below the map it is not reachable at all on a phone. */}
      <section
        className="cn-attention-band"
        data-testid="today-attention-band"
        data-attention={
          disruptions.length
            ? "open"
            : handledDisruptions.length
              ? "handled"
              : "clear"
        }
        aria-label={zh ? "需要关注" : "Needs attention"}
      >
        <div className="cn-attention-head">
          <span className="v8-kicker">{zh ? "需要关注" : "Attention"}</span>
          <strong
            className={disruptions.length || tripUnlocatable ? "warn" : "green"}
            data-testid="today-attention"
            data-no-coordinates={tripUnlocatable ? "true" : "false"}
          >
            {disruptions.length
              ? zh
                ? `${disruptions.length} 项变化`
                : `${disruptions.length} change(s)`
              : handledDisruptions.length
                ? zh
                  ? "已处理"
                  : "Handled"
                : tripUnlocatable
                  ? /* Not a new word: the map panel owns this condition's sentence
                       (MapPanel.tsx, "这个行程还没有可信的坐标…"), and the attention line quotes its
                       own opening clause so the two surfaces on one screen cannot disagree. */
                    zh
                    ? "这个行程还没有可信的坐标"
                    : "This trip has no trustworthy coordinates yet"
                  : zh
                    ? "一切正常"
                    : "All clear"}
          </strong>
        </div>
        {disruptions.length > 0 || handledDisruptions.length > 0 ? (
          <p className="cn-attention-hint">
            {disruptions.length
              ? zh
                ? "打开「实时行程」查看并处理这些变化。"
                : "Open Live to review these changes."
              : zh
                ? "刚才的变化已经处理好。"
                : "The earlier change has been handled."}
          </p>
        ) : null}
        {disruptions.length > 0 && (
          /* Pass-8 ruling #2, second instance: this pointed at exactly the same
             destination as the hero's "Open Live", so a screen with a real
             disruption showed two primary buttons side by side. The hero owns
             the primary; the attention band keeps its own quiet link. */
          <button
            type="button"
            className="v8-quiet-button cn-attention-cta"
            onClick={() => onNavigate("live")}
          >
            {zh ? "查看实时行程" : "Review in Live"}
          </button>
        )}
      </section>
      <div className="v8-hero-grid">
        <article
          className="v8-trip-hero cn-now-next-hero"
          data-testid="today-hero"
          data-now={currentStop ? "true" : "false"}
        >
          {currentStop ? (
            <div className="cn-hero-now" data-testid="today-now">
              <span className="cn-hero-label">{zh ? "现在" : "Now"}</span>
              <strong className="cn-hero-now-place">{currentStop.title}</strong>
            </div>
          ) : null}
          <div className="cn-hero-next" data-testid="today-next">
            <span className="cn-hero-label">
              {nextUp ? (zh ? "接下来" : "Next") : zh ? "今天" : "Today"}
            </span>
            <strong className="cn-hero-next-row">
              {nextUp
                ? `${heroClock ?? ""} ${nextUp.title}`.trim()
                : zh
                  ? "还没有安排"
                  : "Nothing scheduled"}
            </strong>
            {nextUp ? (
              <small className="cn-hero-meta">
                {todayStops.length
                  ? zh
                    ? `今天还有 ${todayStops.length} 项安排`
                    : `${todayStops.length} stops today`
                  : /* An empty calendar day says nothing about whether further stops exist -- only
                       the server's sequence does.  "暂无安排" beside a real later stop was the same
                       contradiction in a smaller font. */
                    remainingAfterNext > 0
                      ? zh
                        ? `后面还有 ${remainingAfterNext} 站`
                        : `${remainingAfterNext} stops after this`
                      : zh
                        ? "这一趟到下一站就结束了"
                        : "This trip ends at the next stop"}
              </small>
            ) : null}
          </div>
          {nextUp ? (
            <button
              type="button"
              className="v8-primary cn-hero-cta"
              data-testid="today-hero-cta"
              data-contrast-role="control"
              onClick={() => onNavigate("live")}
            >
              {zh ? "查看实时行程" : "Open Live"}
            </button>
          ) : null}
          {/* ORD-0082 ①: the fallback is allowed, silence is not.  Whichever clock chose these
              two lines is named here, so a device reading is never mistaken for the stage's. */}
          <small
            className="cn-clock-source"
            data-testid="today-clock-source"
            data-clock-source={usingDeviceClock ? "DEVICE" : "SCENE"}
            data-cursor-status={cursorStatus ?? "UNKNOWN"}
            data-next-source={
              useProjection
                ? "SERVER_PROJECTION"
                : byStage
                  ? "PROJECTION_UNUSABLE"
                  : usingDeviceClock
                    ? "DEVICE_WINDOW"
                    : // The twin gave no projection but the stage gave a scene minute, and the time
                      // window below is measured against *that* minute (nowMs = stageMs), not against
                      // this box's clock -- which is why this arm is not called DEVICE_WINDOW.
                      "SCENE_WINDOW"
            }
          >
            {(() => {
              if (!usingDeviceClock) {
                const basis =
                  stage.scene?.basis === "CURSOR"
                    ? zh
                      ? "执行游标"
                      : "execution cursor"
                    : zh
                      ? "行程起点"
                      : "journey start";
                /* ORD-0208 1: this sentence had three defects measured on a phone -- the internal
                   enum (`游标状态 ABSENT_FALLBACK`) printed as prose, the instant shown as bare UTC
                   (`2026-09-21T02:00:00+00:00`) to someone just told the trip runs on China Standard
                   Time, and a self-contradiction ("取自服务器场景" beside a status that says ABSENT).
                   A non-VALID cursor is now the plain sentence the order suggests; the enum stays in
                   the `data-cursor-status` attribute, where it is inspectable without being prose. */
                const cursorNote =
                  cursorStatus && cursorStatus !== "VALID"
                    ? zh
                      ? "。服务器没有给出这一分钟的执行游标，这一屏的时间按行程的时间窗来读"
                      : ". The server gave no execution cursor for this minute, so this screen reads time by the journey's window"
                    : "";
                const stationNote = useProjection
                  ? zh
                    ? " · 现在/接下来由服务器的执行投影给出"
                    : " · now/next come from the server's execution projection"
                  : cursorStatus === "STALE"
                    ? zh
                      ? " · 服务器不认它自己的游标，这一屏的站位按时间窗读，不是按游标"
                      : " · the server does not trust its own cursor, so this screen reads the stations by time window, not by cursor"
                    : zh
                      ? " · 服务器的执行投影还没到，这一屏没有按游标定位"
                      : " · the server's execution projection has not arrived, so no station is claimed from the cursor";
                return zh
                  ? `“现在”取自服务器场景（${basis}${cursorNote}）：${dateInZone(stageInstant ?? undefined, locale, data.journey.timezone || "UTC")}${stationNote}`
                  : `"now" comes from the server's scenario (${basis}${cursorNote}): ${dateInZone(stageInstant ?? undefined, locale, data.journey.timezone || "UTC")}${stationNote}`;
              }
              if (stage.phase === "loading")
                return zh
                  ? "还在读服务器场景，这一屏用的是这台设备自己的时间"
                  : "The scenario is still being read; this screen uses this device's own time";
              if (stage.phase === "unavailable")
                return zh
                  ? `读不到服务器场景（${stage.reason ?? "未知"}），这一屏用的是这台设备自己的时间`
                  : `The scenario could not be read (${stage.reason ?? "unknown"}); this screen uses this device's own time`;
              return zh
                ? "台上没有场景，这一屏用的是这台设备自己的时间"
                : "There is no scenario on stage; this screen uses this device's own time";
            })()}
          </small>
        </article>
        <MapPanel
          data={data}
          option={selected}
          onNoLocationChange={setMapReportsNoLocation}
        />
      </div>
      {/* Stay / booking steps OUT of the hero into a compact strip: the hero owns
          one action, lodging is secondary context (GPT Hero rules). */}
      <div className="cn-hero-strip" data-testid="today-strip">
        <div className="cn-strip-item">
          <span>{zh ? "住宿" : "Stay"}</span>
          <strong data-testid="today-stay">
            {chosenStay
              ? chosenStay.stay.property_name
              : zh
                ? "还没有选定"
                : "Not chosen yet"}
          </strong>
        </div>
        <div className="cn-strip-item">
          <span>{t("hub.arrival")}</span>
          <b>{dateInZone(data.trip.arrival_anchor.at, locale, journeyTz)}</b>
        </div>
        <div className="cn-strip-item">
          <span>{t("hub.departure")}</span>
          <b>{dateInZone(data.trip.departure_anchor.at, locale, journeyTz)}</b>
        </div>
      </div>
      {/* ORD-0392: this anchor is a table now (`hubAnchorPanels` in app/v8PanelRegistry.tsx), so a
          lane that wants something under Today's attention band adds an entry there and never opens
          this file. ORD-0384's own comment about *why* it sits here travelled with the entry. */}
      {renderPanels(hubAnchorPanels, { data, onNavigate })}
      <div className="v8-section-heading">
        <div>
          <p className="v8-kicker">{t("hub.planGlance")}</p>
          <h2>{t("hub.everyDay")}</h2>
        </div>
        <button className="v8-text-button" onClick={() => onNavigate("stay")}>
          {t("hub.compareStays")}
        </button>
      </div>
      {/* The day cards belong to a stay *option*, not to a chosen plan.  Until
          the traveller has actually chosen a plan they are a preview of one
          candidate stay, so they must say so — otherwise an option's day plan
          reads as the trip the traveller is on. */}
      {!hasChosenPlan && selected && (
        <p className="v8-muted" data-testid="hub-glance-caveat">
          {zh
            ? `以下只是其中一个候选方案的预览，你还没有采用它。`
            : `Preview of one candidate option — you have not chosen a plan yet.`}
        </p>
      )}
      {!selected && data.active_plan ? (
        <div className="v8-empty-state" data-testid="hub-active-plan">
          <b>{zh ? "已选行程" : "Selected plan"}</b>
          <p>
            {zh
              ? "下面是已采用的真实行程；住宿方案仍未选择。"
              : "This is the selected real itinerary; no stay option has been chosen."}
          </p>
          <p data-testid="hub-active-plan-mode">
            {activePlanHasKnownMode
              ? t("hub.routeModeReady")
              : t("hub.routeModePending")}
          </p>
          <p data-testid="hub-active-plan-stops">
            {activities
              .map(
                (item) =>
                  `${clockInZone(item.start_at, data.journey.timezone || "UTC")} ${item.title}`,
              )
              .join(" · ")}
          </p>
        </div>
      ) : !selected ? (
        <div className="v8-empty-state" data-testid="hub-glance-empty">
          <b>{zh ? "还没有可以预览的行程" : "Nothing to preview yet"}</b>
          <p>
            {zh
              ? "选定方案后，这里会显示每天的安排。"
              : "Choose a plan and the day-by-day plan appears here."}
          </p>
        </div>
      ) : null}
      <div className="v8-day-row">
        {selected?.days.map((day) => (
          <button
            key={day.day}
            className="v8-day-card"
            onClick={() => onNavigate(hasChosenPlan ? "studio" : "hub")}
          >
            <span>{t("hub.day", { n: day.day })}</span>
            <strong>{dayHeading(day, zh)}</strong>
            <p>{day.activities.join(" · ")}</p>
            <small>
              {/* ORD-0222 1: the *other* day-card path (the detail list at :2768 was fixed under
                  ORD-0208 ④).  `day.mode` is an enum and `hub.anchor` is a template, so
                  interpolating the enum produced 「TRANSIT锚点」 / "ARRIVAL anchor" on a Chinese
                  screen.  The template stays; its parameter is now an already translated word. */}
              <span className="mode-mark">{modeLabel(day.mode, zh)}</span>{" "}
              {t("hub.anchor", { type: anchorLabel(day.anchor, zh) })}
            </small>
          </button>
        ))}
      </div>
    </section>
  );
}

/** A chosen stay with no provider rate must not print as 0.00. */
function stayTotalLabel(
  option: NonNullable<V8Data["options"][number]>,
  t: (key: string, params?: Record<string, string | number>) => string,
  locale: string,
): string {
  const known =
    (option.stay.nightly_amount_minor ?? 0) > 0 ||
    (option.stay_total_minor ?? 0) > 0;
  if (!known) return t("studio.priceUnknown");
  return money(option.stay.total_amount_minor, option.stay.currency, locale);
}

function Studio({
  data,
  selected,
  chosen,
  plan,
  onNavigate,
  onRain,
}: {
  data: V8Data;
  /**
   * The option being previewed.  `selected` falls back to the first candidate
   * when the traveller has not chosen anything yet, so it is a *preview* and
   * must never be presented as the trip the traveller is on.
   */
  selected: V8Data["options"][number] | null;
  /** The option the traveller actually committed to, or null. */
  chosen: V8Data["options"][number] | null;
  /** The authoritative plan, whose activities carry the cost evidence. */
  plan: PlanVersion | null;
  onNavigate: (route: string) => void;
  onRain: () => Promise<void>;
}) {
  const t = useT();
  const { locale } = useLocale();
  const [draftImpact, setDraftImpact] = useState<{
    day: number;
    count: number;
  } | null>(null);
  // How much of the budget is actually evidenced?  A single priced stop does
  // not verify a total, and a stop with no provider price is not a free stop.
  // The evidence lives on the authoritative plan's activities (`cost_status`),
  // not on the /experience projection, so it is read from `plan`
  // (T4 / docs/BUDGET_EVIDENCE_CONTRACT.md).
  const planActivities = plan?.activities ?? [];
  const costEvidence = budgetEvidenceCoverage(planActivities);
  const pricedStops = costEvidence.knownCount;
  const budgetVerdictAllowed = budgetVerdictAllowedFor(costEvidence);
  return (
    <section className="v8-page" data-testid="v8-trip-studio">
      <div className="v8-page-intro compact">
        <div>
          <p className="v8-kicker">{t("studio.kicker")}</p>
          <h1>{t("studio.title")}</h1>
          <p className="v8-lede">{t("studio.lede")}</p>
        </div>
        <button className="v8-outline-button" onClick={() => void onRain()}>
          {t("studio.reportRain")}
        </button>
      </div>
      <div className="v8-studio-layout">
        <div className="v8-days-column">
          {!chosen && selected && (
            <p className="v8-muted" data-testid="studio-preview-caveat">
              {locale.startsWith("zh")
                ? "以下只是候选方案的预览，你还没有采用它。"
                : "This is a preview of a candidate option - you have not chosen a plan yet."}
            </p>
          )}
          {selected?.days.map((day) => (
            <article className="v8-day-detail" key={day.day}>
              <div className="v8-day-detail-title">
                <span>0{day.day}</span>
                <div>
                  <b>
                    {dayHeading(day, locale.startsWith("zh"))}
                    {day.date ? ` · ${day.date.slice(5)}` : ""}
                  </b>
                  <small>
                    {/* ORD-0208 4: this used to print the raw enums -- 「ARRIVAL anchor · TRANSIT」
                        in a Chinese interface.  Both words have humanizer tables; the day card just
                        never used them. */}
                    {anchorLabel(day.anchor, locale.startsWith("zh"))} ·{" "}
                    {modeLabel(day.mode, locale.startsWith("zh"))}
                  </small>
                </div>
              </div>
              <div className="v8-activity-list">
                <SortableActivityList
                  activities={day.activities}
                  protectedFirst={day.anchor !== "STAY"}
                  onDraft={(draft) =>
                    setDraftImpact({ day: day.day, count: draft.length })
                  }
                />
              </div>
            </article>
          ))}
        </div>
        {draftImpact && (
          <div
            className="v8-impact-drawer"
            role="status"
            data-testid="v12-impact-preview"
          >
            <b>{t("studio.draftPreview")}</b>
            <span>
              {t("studio.dayActivities", {
                day: draftImpact.day,
                count: draftImpact.count,
              })}
            </span>
            <small>{t("studio.draftCheck")}</small>
          </div>
        )}
        <aside className="v8-studio-aside">
          <div className="v8-aside-heading">
            <span>
              {chosen ? t("studio.selectedStay") : t("studio.noStaySelected")}
            </span>
            <button onClick={() => onNavigate("stay")}>
              {chosen ? t("studio.change") : t("studio.chooseStay")}
            </button>
          </div>
          {chosen && (
            <div className="v8-selected-stay">
              <div className="v8-hotel-thumb">
                <Icon name="bed" />
              </div>
              <div>
                <b>{chosen.stay.property_name}</b>
                <span>{chosen.stay.room_type}</span>
                <strong>
                  {stayTotalLabel(chosen, t, locale)}{" "}
                  <small>{t("studio.total")}</small>
                </strong>
              </div>
            </div>
          )}
          <div className="v8-cost-lines">
            <div>
              <span>{t("studio.stayTotal")}</span>
              {/* A total is only known once a stay is chosen, and OSM carries
                  no price: unknown must read as unknown, never as ¥0.00. */}
              <b data-testid="studio-stay-total">
                {!chosen
                  ? t("studio.notChosen")
                  : (chosen.stay.nightly_amount_minor ?? 0) > 0 ||
                      (chosen.stay_total_minor ?? 0) > 0
                    ? money(
                        chosen.stay_total_minor,
                        chosen.stay.currency ?? data.trip.stay_budget.currency,
                        locale,
                      )
                    : t("studio.priceUnknown")}
              </b>
            </div>
            <div>
              <span>{t("studio.dailyMovement")}</span>
              <b>
                {chosen
                  ? money(
                      chosen.daily_travel_minor,
                      chosen.stay.currency ?? data.journey.budget.currency,
                      locale,
                    )
                  : t("studio.notChosen")}
              </b>
            </div>
            <div>
              <span>{t("studio.walkingExposure")}</span>
              <b data-testid="studio-walking">
                {chosen
                  ? t("studio.walkingMinutes", { minutes: chosen.walking_min })
                  : t("studio.notChosen")}
              </b>
            </div>
            <div className="total">
              <span>{t("studio.planHealth")}</span>
              <b className={data.active_plan ? "green" : ""}>
                {data.active_plan
                  ? t("studio.protected", {
                      n: chosen?.protected_margin_min ?? 0,
                    })
                  : t("studio.notChosen")}
              </b>
            </div>
          </div>
          {/* The plan gate has nothing to validate before a plan exists, and
              it cannot verify a budget it has no prices for: a stop with no
              provider price is not a free stop.  Claiming "budget verified"
              over an all-unknown itinerary is a fabricated fact, and one known
              price beside many unknown ones is only partial evidence — never a
              total (T4 / docs/BUDGET_EVIDENCE_CONTRACT.md). */}
          {data.active_plan ? (
            <div className="v8-feasibility" data-testid="studio-feasibility">
              <span className="status-dot" />
              <div>
                <b>
                  {budgetVerdictAllowed
                    ? t("studio.feasibilityPassed")
                    : t("studio.feasibilityPassedPartial")}
                </b>
                <small>
                  {budgetVerdictAllowed
                    ? t("studio.feasibilitySub")
                    : costEvidence.state === "PARTIAL"
                      ? t("studio.feasibilityPartialSub", {
                          n: costEvidence.unverifiedCount,
                        })
                      : t("studio.feasibilityNoPrice")}
                </small>
              </div>
            </div>
          ) : (
            <div
              className="v8-feasibility pending"
              data-testid="studio-no-plan"
            >
              <span className="status-dot" />
              <div>
                <b>{t("studio.noPlanChecked")}</b>
                <small>{t("studio.noPlanCheckedSub")}</small>
              </div>
            </div>
          )}
        </aside>
      </div>
    </section>
  );
}

function AuthoritativeExecution({
  journeyId,
  plan,
  twin,
  policy,
  loading,
  recovery,
  flags,
  currency,
  onRefresh,
}: {
  journeyId: string;
  plan: PlanVersion | null;
  twin: JourneyTwin | null;
  policy: Policy | null;
  loading: boolean;
  recovery?: RecoveryState | null;
  flags?: V8Data["experience_flags"];
  currency: string;
  onRefresh: () => void;
}) {
  const t = useT();
  const execution = (
    twin?.context as
      { execution?: { next_activity_id?: string | null } } | undefined
  )?.execution;
  return (
    <section className="v8-page" data-testid="v12-authoritative-page">
      <div className="v8-page-intro compact">
        <div>
          <p className="v8-kicker">{t("auth.kicker")}</p>
          <h1>{t("auth.title")}</h1>
          <p className="v8-lede">{t("auth.lede")}</p>
        </div>
        <button className="v8-outline-button" type="button" onClick={onRefresh}>
          {t("auth.refresh")}
        </button>
      </div>
      {loading && <p className="v8-muted">{t("auth.loading")}</p>}
      {/* ORD-0392: the children of this screen are the `authoritative` rows of
          `app/v8PanelRegistry.tsx`, in table order. The comment that used to sit here -- why the
          recovery notice comes before any plan panel, and why the version list is mounted outside
          `plan &&` (ORD-0386) -- now lives on those entries. */}
      {renderPanels(
        authoritativePanels,
        {
          journeyId,
          plan,
          twin,
          policy,
          loading,
          recovery,
          flags,
          currency,
          onRefresh,
          nextActivityId: execution?.next_activity_id ?? null,
          slots: {
            noPlanNotice: () => (
              <div className="v8-feasibility">
                <span className="status-dot" />
                <div>
                  <b>{t("auth.noPlan")}</b>
                  <small>{t("auth.noPlanSub")}</small>
                </div>
              </div>
            ),
          },
        },
      )}
    </section>
  );
}

function Live({
  data,
  selected,
  mode,
  onRain,
  onLocation,
  offline,
}: {
  data: V8Data;
  selected: V8Data["options"][number] | null;
  mode: V8Mode;
  onRain: () => Promise<void>;
  onLocation: () => Promise<void>;
  offline: OfflineJourney | null;
}) {
  const t = useT();
  const { locale } = useLocale();
  const journeyCompleted = String(data.journey.status ?? "").toUpperCase() === "COMPLETED";
  const {
    now,
    next,
    nextLeg,
    unresolved: cursorUnresolved,
    cursorStatus,
  } = liveActivityWindow(data.active_plan ?? null);
  return (
    <section className="v8-page" data-testid="v8-live-journey">
      <div className="v8-page-intro compact">
        <div>
          <p className="v8-kicker">
            {t("live.kicker")}{" "}
            {offline
              ? t("live.saved", { date: date(offline.updated_at, locale) })
              : t("live.syncing")}
          </p>
          <h1>{journeyCompleted ? t("live.completedTitle") : t("live.title")}</h1>
          <p className="v8-lede">
            {journeyCompleted ? t("live.completedLede") : t("live.lede")}
          </p>
        </div>
      </div>
      <div className="v8-live-main">
        <div className="v8-live-now">
          <span className="v8-live-label">
            <span className="status-dot pulse" />
            {t("live.now")}
          </span>
          <div>
            <h2
              data-testid="live-journey-current"
              // Same value domain as the backend's cursor_status, on purpose: this
              // attribute used to publish a fourth-looking "ABSENT" that the projection
              // never emits, so a reader could not tell a vocabulary difference from a
              // contract. UNKNOWN is a fetch state (no plan served yet), not an
              // execution state.
              data-cursor-status={cursorStatus ?? "UNKNOWN"}
            >
              {journeyCompleted
                ? t("live.completedStatus")
                : cursorUnresolved
                ? t("live.unresolvedStep")
                : now
                  ? now.title
                  : data.active_plan
                    ? t("live.nothingDue")
                    : t("live.awaitingPlan")}
            </h2>
            <p>
              {journeyCompleted
                ? t("live.completedAdvisory")
                : cursorUnresolved
                ? t("live.unresolvedStepHint")
                : next
                  ? t("live.nextLeg", {
                      title: next.title,
                      mode: modeLabel(
                        nextLeg?.mode,
                        locale.startsWith("zh"),
                      ),
                      duration:
                        nextLeg?.co_located
                          ? t("live.routeCoLocated")
                          : nextLeg?.duration_min ?? t("live.durationPending"),
                      unit: nextLeg?.co_located ? "" : nextLeg ? " min" : "",
                    })
                  : data.active_plan
                    ? // A plan that is spent is not a plan that is missing, and the
                      // sub-line used to claim the latter.
                      t("live.nothingDue")
                    : t("live.nextSelect")}
            </p>
          </div>
          <strong>
            {journeyCompleted
              ? t("live.completedStatusUpper")
              : now && !cursorUnresolved
              ? clockInZone(now.start_at, data.journey.timezone || "UTC")
              : cursorUnresolved
                ? "—"
                : t("live.awaitingPlanUpper")}
          </strong>
        </div>
        <MapPanel data={data} option={selected} />
      </div>
    </section>
  );
}

function LiveSecondary({
  data,
  selected,
  mode,
  canChangeMode,
  comparison,
  isHandledDisruption,
  onMode,
  onSheet,
  onRain,
  onLocation,
}: {
  data: V8Data;
  selected: V8Data["options"][number] | null;
  mode: V8Mode;
  canChangeMode: boolean;
  comparison: V8Data["mobility_facts"];
  isHandledDisruption: (
    item: NonNullable<V8Data["active_plan"]>["disruptions"][number],
  ) => boolean;
  onMode: (mode: V8Mode) => Promise<void>;
  onSheet: () => void;
  onRain: () => Promise<void>;
  onLocation: () => Promise<void>;
}) {
  const t = useT();
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const journeyCompleted = String(data.journey.status ?? "").toUpperCase() === "COMPLETED";
  const { next, nextLeg } = liveActivityWindow(data.active_plan ?? null);
  const planDisruptions = data.active_plan?.disruptions ?? [];
  const disruptions = planDisruptions.filter(
    (item) => !isHandledDisruption(item),
  );
  /* ORD-0394: the link target is the same joined coordinate the map paints, so the pin and the
     external navigation app cannot disagree about where a stop is.  A route mode is appended only
     when the selected plan's own mobility fact supplied one; a missing mode opens the destination
     marker instead of silently turning absent evidence into walking. */
  const nextAt = next ? activityCoordinate(next) : null;
  const amapMode = nextLeg?.mode
    ? ({
        WALK: "walk",
        BICYCLE: "ride",
        TRANSIT: "bus",
        AUTO: "car",
        RIDE_HAIL: "car",
      } as Record<string, string>)[nextLeg.mode.toUpperCase()] ?? null
    : null;
  const destination = nextAt != null
      ? data.journey.city_code.startsWith("CN-")
        ? amapMode
          ? `https://uri.amap.com/navigation?to=${nextAt.longitude},${nextAt.latitude},${encodeURIComponent(next.title)}&mode=${amapMode}&coordinate=wgs84&callnative=1`
          : `https://uri.amap.com/marker?position=${nextAt.longitude},${nextAt.latitude}&name=${encodeURIComponent(next.title)}&coordinate=wgs84&callnative=1`
        : `https://www.google.com/maps/dir/?api=1&destination=${nextAt.latitude},${nextAt.longitude}`
      : null;
  const destinationLabel = nextLeg?.mode
    ? t("live.navigate")
    : zh
      ? "打开目的地 →"
      : "Open destination →";
  return (
    <section className="v8-page" data-testid="v8-live-secondary">
      {/* Pass-8 ruling #3: Live reads Current -> Map -> Disruption/Replan ->
          these utilities -> mode/evidence. They are location and weather
          reporting, not decisions, so they no longer sit above "Current" as two
          full-width buttons of equal visual weight. */}
      <div className="cn-live-toolbar" data-testid="live-toolbar-actions">
        <button
          type="button"
          className="v8-quiet-button"
          data-testid="live-update-location"
          onClick={() => void onLocation()}
        >
          {t("live.updateLocation")}
        </button>
        <button
          type="button"
          className="v8-quiet-button"
          data-testid="live-report-rain"
          aria-label="Rain detected"
          onClick={() => void onRain()}
        >
          {t("live.reportRain")}
        </button>
      </div>
      <div className="cn-live-secondary">
        <div className="v8-mode-chooser">
          <div>
            <span>{t("live.modeForLeg")}</span>
            <b>{canChangeMode ? t("live.chooseRoute") : t("live.modeUnavailable")}</b>
          </div>
          <div className="v8-mode-chips">
            {v8Modes.map((item) => (
              <button
                type="button"
                data-testid={`mode-${item.toLowerCase()}`}
                key={item}
                className={canChangeMode && mode === item ? "active" : ""}
                data-contrast-role={canChangeMode && mode === item ? "state" : "control"}
                disabled={!canChangeMode}
                aria-disabled={!canChangeMode}
                title={!canChangeMode ? t("live.modeUnavailable") : undefined}
                onClick={() => void onMode(item)}
              >
                {modeLabel(item, zh)}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="v8-compare-button"
            disabled={!canChangeMode}
            aria-disabled={!canChangeMode}
            title={!canChangeMode ? t("live.compareUnavailable") : undefined}
            onClick={onSheet}
          >
            {canChangeMode ? t("live.compareAll") : t("live.compareUnavailable")}
          </button>
        </div>
        <div className="v8-live-side">
          <div className="v8-next-card">
            <span>{t("live.leaveBy")}</span>
            <h3>{journeyCompleted ? t("live.completedStatus") : next?.title ?? t("live.awaitingPlan")}</h3>
            <p data-testid="live-leave-by">
              {journeyCompleted
                ? t("live.completedAdvisory")
                : next
                ? t("live.arriveBy", {
                    date: dateInZone(
                      next.start_at,
                      locale,
                      data.journey.timezone || "UTC",
                    ),
                  })
                : t("live.selectPlan")}{" "}
              · {t("live.slack", { minutes: selected?.protected_margin_min ?? 0 })}
            </p>
            <div className="v8-eta">
              <strong>
                {journeyCompleted
                  ? "—"
                  : next
                  ? clockInZone(next.start_at, data.journey.timezone || "UTC")
                  : "—"}
              </strong>
              <span>
                {journeyCompleted
                  ? t("live.completedStatus")
                  : t("live.eta", { mode: modeLabel(nextLeg?.mode, zh) })}
              </span>
            </div>
            {destination && (
              <a
                className="v9-navigate"
                href={destination}
                target="_blank"
                rel="noreferrer"
              >
                {destinationLabel}
              </a>
            )}
            <div className="v9-risk">
              <b>{t("live.risk")}</b>
              <span>
                {journeyCompleted
                  ? t("live.completedRisk")
                  : disruptions.length
                  ? t("live.guardianMonitoring", {
                      severity: disruptions[0].severity,
                    })
                  : t("live.routeCurrent")}
              </span>
            </div>
          </div>
          <details className="v8-comparison v9-evidence-drawer">
            <summary className="v9-evidence-summary">
              <span>{t("live.evidence")}</span>
              <small>{t("live.evidenceSub")}</small>
            </summary>
            <div className="v8-aside-heading">
              <span>{t("live.routeEvidence")}</span>
              <button onClick={onSheet}>{t("live.compare")}</button>
            </div>
            {comparison.length
              ? comparison.slice(0, 2).map((fact) => (
                  <div
                    className="v8-route-row"
                    key={`${fact.from_key}-${fact.to_key}-${fact.mode}`}
                  >
                    {/* ORD-0222 1/②: `fact.mode` is a provider enum and the duration words were
                        hardcoded English, so a Chinese traveller read "TRANSIT 12 min · 5 min walk"
                        on the same row. */}
                    <b>{modeLabel(fact.mode, zh)}</b>
                    <span>
                      {t("live.routeDuration", {
                        duration: fact.duration_min,
                        walking: fact.walking_min,
                      })}
                    </span>
                    <strong>
                      {fact.cost_amount_minor == null
                        ? t("mobility.costUnknown")
                        : money(
                            fact.cost_amount_minor,
                            fact.currency ?? "CNY",
                            locale,
                          )}
                    </strong>
                  </div>
                ))
              : nextLeg && (
                  <div
                    className="v8-route-row"
                    data-testid="live-frozen-route-evidence"
                  >
                    <b>{modeLabel(nextLeg.mode, zh)}</b>
                    <span>
                      {nextLeg.co_located
                        ? t("live.routeCoLocated")
                        : t("live.routeDurationOnly", {
                            duration: nextLeg.duration_min,
                          })}
                    </span>
                    <strong>{t("mobility.costUnknown")}</strong>
                  </div>
                )}
            <small>
              {comparison.length
                ? t("live.providerFacts")
                : nextLeg
                  ? t("live.frozenRouteEvidence", {
                      provider: nextLeg.provider_key ?? "unknown",
                      provenance: nextLeg.provenance ?? "unknown",
                    })
                  : t("live.providerFacts")}
            </small>
          </details>
        </div>
      </div>
    </section>
  );
}

function MapPage({
  data,
  selected,
  comparison,
  onSheet,
  selectedActivityId,
  onSelectActivity,
}: {
  data: V8Data;
  selected: V8Data["options"][number] | null;
  comparison: V8Data["mobility_facts"];
  onSheet: () => void;
  /** ORD-0390: the shell's one and only selection variable, threaded through rather than
      duplicated — a second "selected stop" state here is how two surfaces start disagreeing. */
  selectedActivityId: string | null;
  onSelectActivity: (id: string | null) => void;
}) {
  const t = useT();
  const { locale } = useLocale();
  /* Only stops the map can actually stand on are offered as rows — the same coordinate test
     `MapPanel` uses to build its markers, so a row can never name a stop with no pin. */
  const planStops = (data.active_plan?.activities ?? []).filter(
    (item) => activityCoordinate(item) != null,
  );
  const coLocatedLegs = (data.active_plan?.legs ?? []).filter((leg) => leg.co_located);
  const { nextLeg } = liveActivityWindow(data.active_plan ?? null);
  const selectedActivity =
    planStops.find((item) => item.id === selectedActivityId) ?? null;
  /* ORD-0390 ①: selection has to be readable on both halves of the screen, and on a phone the card
     sits below the map.  Selecting a pin therefore brings its own card into view — `block:"nearest"`
     so an already-visible card does not jump.  This is the other half of "reachable by hit-test":
     a card that exists but is off-screen cannot be hit, and the gate measures hits, not presence. */
  const selectedHeadingRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!selectedActivityId) return;
    /* `center`, not `nearest`: the floating Copilot pill is anchored bottom-right and covers the
       bottom of this screen on a phone, so a card that merely "fits" at the bottom edge is still
       unhittable there — measured, a hit-test at the card's centre returned the pill. */
    selectedHeadingRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [selectedActivityId]);
  return (
    <section className="v8-page">
      <div className="v8-page-intro compact">
        <div>
          <p className="v8-kicker">{t("map.kicker")}</p>
          <h1>{t("map.title")}</h1>
          <p className="v8-lede">{t("map.lede")}</p>
        </div>
        <button className="v8-primary" onClick={onSheet}>
          {t("map.compareModes")}
        </button>
      </div>
      <div className="v8-map-page-grid">
        <MapPanel
          data={data}
          option={selected}
          selectedActivityId={selectedActivityId}
          onSelectActivity={onSelectActivity}
        />
        <div className="v8-map-facts">
          <span className="v8-kicker">{t("map.nextLeg")}</span>
          {/* ORD-0390: the heading used to name the next leg no matter what the traveller had just
              tapped, so selecting a stop on the map changed the pin and nothing else — the screen
              could not show that a selection had happened.  It now reads the same
              `detailActivityId` the trips list writes, and falls back to the next leg when
              nothing is selected. */}
          <h2 data-testid="map-selected-stop" ref={selectedHeadingRef as never}>
            {selectedActivity
              ? `${mapCopy(locale, "stopSelectedLead")} · ${selectedActivity.title}`
              : nextActivityOf(data.active_plan)?.title ?? t("map.awaitingPlan")}
          </h2>
          {/* `type` is what this payload calls the category — `Activity.activity_type` in
              `api/citynexus.ts:16` is a different shape, and the typechecker caught the guess. */}
          {selectedActivity?.type ? (
            <p className="v8-map-selected-type" data-testid="map-selected-type">
              {selectedActivity.type}
            </p>
          ) : null}
          {/* The reverse half: a row here selects the stop, which pans the map and presses its
              marker.  Name only — times and money stay off the map affordance. */}
          {planStops.length > 0 && (
            <div className="v8-map-stops" data-testid="map-stops">
              <span className="v8-kicker">{mapCopy(locale, "stopsKicker")}</span>
              {planStops.map((stop) => (
                <button
                  className={stop.id === selectedActivityId ? "v8-map-stop selected" : "v8-map-stop"}
                  key={stop.id}
                  type="button"
                  data-testid={`map-stop-${stop.id}`}
                  aria-label={mapCopy(locale, "stopLocateAria").replace("%s", stop.title)}
                  aria-pressed={stop.id === selectedActivityId}
                  onClick={() => onSelectActivity(stop.id)}
                >
                  {stop.title}
                </button>
              ))}
            </div>
          )}
          {coLocatedLegs.length > 0 && (
            <p className="v8-muted" data-testid="map-co-located-evidence">
              {t("live.routeCoLocatedSummary", { count: coLocatedLegs.length })}
            </p>
          )}
          {comparison.slice(0, 3).map((fact) => (
            <div className="v8-route-row" key={fact.mode}>
              {/* ORD-0222 1/②: same row, same two leaks, on the map surface. */}
              <b>{modeLabel(fact.mode, locale.startsWith("zh"))}</b>
              <span>
                {t("live.routeDuration", {
                  duration: fact.duration_min,
                  walking: fact.walking_min,
                })}
              </span>
              <strong>
                {fact.cost_amount_minor == null
                  ? "—"
                  : money(
                      fact.cost_amount_minor,
                      fact.currency ?? "CNY",
                      locale,
                    )}
              </strong>
            </div>
          ))}
          {!comparison.length && nextLeg && (
            <div className="v8-route-row" data-testid="map-frozen-route-evidence">
              <b>{modeLabel(nextLeg.mode, locale.startsWith("zh"))}</b>
              <span>
                {nextLeg.co_located
                  ? t("live.routeCoLocated")
                  : t("live.routeDurationOnly", { duration: nextLeg.duration_min })}
              </span>
              <strong>{t("mobility.costUnknown")}</strong>
            </div>
          )}
          <small>
            {!comparison.length && nextLeg
              ? t("live.frozenRouteEvidence", {
                  provider: nextLeg.provider_key ?? "unknown",
                  provenance: nextLeg.provenance ?? "unknown",
                })
              : t("map.unknownCosts")}
          </small>
        </div>
      </div>
    </section>
  );
}

// Booking vocabulary in traveller language.  Internal codes such as
// UNKNOWN_REALTIME, STRUCTURED_JSON and C4 must never be the primary reading.
const BOOKING_TYPES: Record<string, { zh: string; en: string }> = {
  FLIGHT: { zh: "航班", en: "Flight" },
  RAIL: { zh: "火车", en: "Rail" },
  COACH: { zh: "大巴", en: "Coach" },
  FERRY: { zh: "轮渡", en: "Ferry" },
  HOTEL: { zh: "住宿", en: "Hotel" },
  TICKET: { zh: "门票", en: "Ticket" },
  RESERVATION: { zh: "预订", en: "Reservation" },
};

function bookingTypeLabel(value: string | null | undefined): string {
  const key = String(value ?? "").toUpperCase();
  return BOOKING_TYPES[key]?.zh ?? key.replaceAll("_", " ").toLowerCase();
}

function realtimeLabel(value: string | null | undefined, zh: boolean): string {
  const key = String(value ?? "").toUpperCase();
  if (key === "ON_TIME" || key === "NORMAL") return zh ? "准点" : "On time";
  if (key === "DELAYED") return zh ? "延误" : "Delayed";
  if (key === "CANCELLED") return zh ? "已取消" : "Cancelled";
  if (key === "UNKNOWN_REALTIME" || key === "UNKNOWN" || !key) {
    return zh ? "暂无实时状态" : "No live status";
  }
  return zh ? "状态未知" : "Status unknown";
}

/**
 * ORD-0087 / ORD-0084: `commitments` was declared on this file's own `Wallet` type and drawn
 * nowhere, so a commitment the server had confirmed could not reach the screen and the truth it
 * carries (is this mine, is it real, when is it due) died in the payload.  Every word here comes
 * from a field: the simulated label only from an `external_ref` the mock provider issued, the
 * amount only when the server says it is KNOWN, and nothing at all when there are no commitments --
 * an empty list is not a mock.
 *
 * Its own component because the phone's Trips -> 预订 tab kept the whole `Bookings` surface inside a
 * disclosure labelled 高级 / 开发者, which is where a confirmed promise does not belong.
 */
function WalletCommitments({
  wallet,
  timezone,
}: {
  wallet: Wallet | null;
  timezone?: string;
}) {
  const t = useT();
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  return (
    <>
      {(wallet?.commitments ?? []).map((item) => {
        const simulated = String(item.external_ref ?? "").startsWith("mock-");
        const known = item.monetary_value_status === "KNOWN";
        const amountMinor = item.monetary_value?.amount_minor;
        const start = item.policy?.scheduled_start_at ?? null;
        const end = item.policy?.scheduled_end_at ?? null;
        const tz = timezone || undefined;
        return (
          <article
            className="v8-booking-card"
            key={item.id}
            data-testid="wallet-commitment"
            data-simulated={simulated ? "true" : "false"}
            data-monetary-status={item.monetary_value_status ?? "UNSTATED"}
          >
            <div className="v8-booking-icon muted">
              <Icon name="ticket" />
            </div>
            <div>
              <span className="v8-selected-label">
                {commitmentKindLabel(item.kind, zh)} · {commitmentLevelLabel(item.level, zh)} ·{" "}
                {commitmentStatusLabel(item.status, zh)}
                {simulated && (
                  <b data-testid="wallet-commitment-simulated">
                    {" · "}
                    {zh ? "模拟承诺" : "simulated commitment"}
                  </b>
                )}
              </span>
              <h2>
                {item.policy?.semantic_key ||
                  `${zh ? "承诺" : "Commitment"} ${item.id.slice(0, 8)}`}
              </h2>
              <p data-testid="wallet-commitment-window">
                {start && end
                  ? `${dateInZone(start, locale, tz ?? "UTC")} → ${dateInZone(end, locale, tz ?? "UTC")}`
                  : zh
                    ? "服务器没有给这项承诺的时间窗"
                    : "The server gave no time window for this commitment"}
              </p>
              <strong data-testid="wallet-commitment-amount">
                {known && amountMinor != null
                  ? money(amountMinor, item.monetary_value?.currency ?? "CNY", locale)
                  : zh
                    ? "价格未知"
                    : t("bookings.priceUnknown")}
              </strong>
            </div>
            <span className="v8-booking-state safe">
              {item.policy?.fixed_schedule
                ? zh
                  ? "时间已锁定"
                  : "Schedule fixed"
                : zh
                  ? "时间可调"
                  : "Schedule flexible"}
            </span>
          </article>
        );
      })}
    </>
  );
}

function Bookings({
  data,
  selected,
  chosen,
  wallet,
  onImport,
  busy,
  onNavigate,
  showCommitments = true,
}: {
  data: V8Data;
  selected: V8Data["options"][number] | null;
  chosen: V8Data["options"][number] | null;
  wallet: Wallet | null;
  onImport: (reservation: Record<string, unknown>) => Promise<void>;
  busy: boolean;
  /* ORD-0407 ②: this screen's "no stay yet" row used to state the condition and stop. The screen that
     owns stays exists (`/v8/stay`, named 住宿选择 / Stay Explorer), so the row opens it. */
  onNavigate: (route: string) => void;
  /* False where the caller draws `WalletCommitments` itself, which is what the phone's Trips tab
     does -- the alternative was a confirmed promise reachable only under 高级 / 开发者. */
  showCommitments?: boolean;
}) {
  const t = useT();
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const [input, setInput] = useState("");
  const [parseError, setParseError] = useState<string | null>(null);
  const submit = async () => {
    // A traveller pastes a ticket, not JSON.  Failing to parse must produce a
    // visible, localised message instead of an unhandled exception.
    let value: Record<string, unknown>;
    try {
      const parsed = JSON.parse(input);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("not an object");
      }
      value = parsed as Record<string, unknown>;
    } catch {
      setParseError(
        input.trim().startsWith("{")
          ? zh
            ? "这段内容不是有效的预订数据。请检查格式，或改用上面的粘贴/手动方式。"
            : "That is not valid booking data. Check the format, or use the paste/manual flow above."
          : zh
            ? "这里只接受结构化数据。普通文字请用「粘贴确认单」或「手动录入」。"
            : "Only structured data is accepted here. For normal text use Paste or Manual entry above.",
      );
      return;
    }
    setParseError(null);
    await onImport(value);
    setInput("");
  };
  return (
    <section className="v8-page" data-testid="v8-bookings">
      <div className="v8-page-intro compact">
        <div>
          <p className="v8-kicker">{t("bookings.kicker")}</p>
          <h1>{t("bookings.title")}</h1>
          <p className="v8-lede">{t("bookings.lede")}</p>
        </div>
      </div>
      <div className="v9-wallet-layout">
        <div className="v8-booking-grid">
          {selected ? (
            <article className="v8-booking-card selected">
              <div className="v8-booking-icon">
                <Icon name="bed" />
              </div>
              <div>
                <span className="v8-selected-label">
                  {chosen
                    ? t("bookings.stayEvidence")
                    : t("bookings.stayPreview")}
                </span>
                <h2>{selected.stay.property_name}</h2>
                <p>
                  {selected.stay.room_type} ·{" "}
                  {selected.stay.refundable
                    ? t("bookings.freeCancel")
                    : t("bookings.nonRefundable")}
                </p>
                <strong data-testid="bookings-stay-total">
                  {stayTotalLabel(selected, t, locale)} {t("bookings.total")}
                </strong>
              </div>
              <span className="v8-booking-state">
                {chosen ? t("bookings.notBooked") : t("bookings.notChosen")}
              </span>
            </article>
          ) : (
            <article className="v8-booking-card" data-testid="bookings-no-stay">
              <div className="v8-booking-icon">
                <Icon name="bed" />
              </div>
              <div>
                <span className="v8-selected-label">
                  {t("bookings.stayEvidence")}
                </span>
                <h2>{t("bookings.noStay")}</h2>
                <p>{t("bookings.noStaySub")}</p>
                {/* The action names where it goes and promises nothing about what is there: the stay
                    screen answers for itself, including when it has no offers to show. */}
                <button
                  type="button"
                  className="v8-quiet-button"
                  data-testid="bookings-no-stay-action"
                  onClick={() => onNavigate("stay")}
                >
                  {t("bookings.noStayAction")}
                </button>
              </div>
            </article>
          )}
          {wallet?.reservations.map((item) => (
            <article
              className="v8-booking-card"
              key={item.id}
              data-testid="wallet-reservation"
            >
              <div className="v8-booking-icon muted">
                <Icon name="ticket" />
              </div>
              <div>
                <span className="v8-selected-label neutral">
                  {zh ? bookingTypeLabel(item.type) : item.type} ·{" "}
                  {realtimeLabel(item.realtime_status, zh)}
                </span>
                <h2>{item.confirmation_ref}</h2>
                <p>
                  {date(item.scheduled_departure_at, locale)} →{" "}
                  {date(item.scheduled_arrival_at, locale)}
                </p>
                <strong>
                  {item.price.amount_minor == null
                    ? t("bookings.priceUnknown")
                    : money(
                        item.price.amount_minor,
                        item.price.currency ?? "CNY",
                        locale,
                      )}
                </strong>
              </div>
              <span className={`v8-booking-state${item.commitment_id ? " safe" : ""}`}>
                {item.commitment_id
                  ? zh
                    ? "已确认 · 受保护"
                    : "Confirmed · protected"
                  : zh
                    ? "已确认 · 未成承诺"
                    : "Confirmed · no commitment"}
              </span>
            </article>
          ))}
          {/* The cards live in their own component so the phone's Trips -> 预订 tab can draw them
              above its 高级 / 开发者 disclosure instead of buried inside it. */}
          {showCommitments ? (
            <WalletCommitments wallet={wallet} timezone={data.journey.timezone || undefined} />
          ) : null}
        </div>
        <aside className="v9-inbox">
          <span className="v8-kicker">
            {zh ? "导入预订" : "Import a booking"}
          </span>
          <p className="v8-lede">
            {zh
              ? "结构化导入在这里：展开下方「高级」粘贴 JSON 确认单。确认单文本与截图走行程收件箱。"
              : "Structured import lives here - expand Advanced below to paste a JSON confirmation. Confirmation text and screenshots go through the journey inbox."}
          </p>
          <details className="v8-advanced" data-testid="bookings-advanced">
            <summary>
              {zh ? "高级：结构化数据" : "Advanced: structured data"}
            </summary>
            <label htmlFor="reservation-json">
              {t("bookings.reservationJson")}
            </label>
            <textarea
              id="reservation-json"
              data-testid="bookings-json-input"
              value={input}
              onChange={(event) => {
                setInput(event.target.value);
                if (parseError) setParseError(null);
              }}
              placeholder={
                '{"reservation_type":"RAIL", "confirmation_ref":"…"}'
              }
            />
            <button
              className="v8-primary"
              disabled={busy || input.trim().length < 2}
              onClick={() => void submit()}
            >
              {t("bookings.reviewProtect")}
            </button>
            {parseError && (
              <p
                className="role-alert"
                role="alert"
                data-testid="bookings-parse-error"
              >
                {parseError}
              </p>
            )}
            <small>{t("bookings.neverInvented")}</small>
          </details>
        </aside>
      </div>
      <div className="v8-no-payment">
        <b>{t("bookings.authorityExplicit")}</b>
        <span>{t("bookings.authoritySub")}</span>
      </div>
    </section>
  );
}

function OfflineJourneyView({
  value,
  message,
  originAnswered = false,
}: {
  value: OfflineJourney;
  message: string | null;
  /** Was this copy reached because the origin answered badly (HTTP_RESPONSE), or never answered? */
  originAnswered?: boolean;
}) {
  const t = useT();
  const { locale } = useLocale();
  const stateKey = (s: string) =>
    (
      ({
        EN_ROUTE: "exec.state.enRoute",
        ARRIVED: "exec.state.arrived",
        IN_PROGRESS: "exec.state.inProgress",
        ACTIVE: "exec.state.active",
        COMPLETED: "exec.state.completed",
        SKIPPED: "exec.state.skipped",
      }) as Record<string, string>
    )[s] ?? null;
  return (
    <main className="v9-offline">
      <div className="v8-brand-mark">CN</div>
      <span className="v8-status-pill">{t(originAnswered ? "offline.readOnlySnapshot" : "offline.readOnly")}</span>
      <h1>{t("offline.saved24h")}</h1>
      <p>{message}</p>
      {/* R3-4: this screen was measured with zero visible buttons and it never healed on its own.
          A cached view still has one honest action -- ask the server again. */}
      <button
        type="button"
        className="v8-primary-button"
        data-testid="offline-retry"
        onClick={() => window.location.reload()}
      >
        {t("offline.retry")}
      </button>
      {value.snapshot.execution && (
        <div className="v12-offline-now-next">
          <article>
            <span>
              {t("exec.now")} ·{" "}
              {stateKey(value.snapshot.execution.now_state)
                ? t(stateKey(value.snapshot.execution.now_state)!)
                : value.snapshot.execution.now_state.replaceAll("_", " ")}
            </span>
            <b>
              {value.snapshot.execution.now?.title ??
                t("offline.journeyComplete")}
            </b>
          </article>
          <article>
            <span>
              {t("exec.next")} ·{" "}
              {value.snapshot.execution.next_state
                ? stateKey(value.snapshot.execution.next_state)
                  ? t(stateKey(value.snapshot.execution.next_state)!)
                  : value.snapshot.execution.next_state.replaceAll("_", " ")
                : "—"}
            </span>
            <b>
              {value.snapshot.execution.next?.title ?? t("offline.noNextStep")}
            </b>
          </article>
        </div>
      )}
      <div>
        {value.snapshot.next_24h.map((item) => (
          <article key={item.id}>
            <b>{item.title}</b>
            <span>
              {date(item.start_at, locale)} → {date(item.end_at, locale)}
            </span>
          </article>
        ))}
      </div>
      <small>
        {t("offline.providerRefresh", {
          date: value.provider_freshness.latest_provider_capture_at
            ? date(value.provider_freshness.latest_provider_capture_at, locale)
            : t("offline.beforeSnapshot"),
        })}
      </small>
      <small data-testid="offline-snapshot-age">
        {t("offline.snapshotSavedAt", {
          date: value.updated_at ? date(value.updated_at, locale) : t("offline.beforeSnapshot"),
        })}
      </small>
    </main>
  );
}

function Me({
  data,
  policy,
  onActivate,
  wallet,
}: {
  data: V8Data;
  policy: Policy | null;
  onActivate: () => Promise<void>;
  wallet?: Wallet | null;
}) {
  const t = useT();
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");

  // The traveller reads a human sentence, not the internal delegation enum.
  const freedomLabel = (mode: string | null | undefined): string => {
    switch (String(mode ?? "").toUpperCase()) {
      case "AUTONOMOUS":
        return zh ? "自动执行已开启" : "Automatic changes are on";
      case "ASSISTED":
        return zh ? "重要改动先问你" : "I ask before important changes";
      default:
        return zh ? "仅提供建议" : "Suggestions only";
    }
  };
  const citySlug = String(data.journey.city_code || "")
    .split("-")
    .pop()
    ?.toUpperCase() ?? "";
  const cityLabel =
    CITY_NAME_BY_SLUG[citySlug]?.[zh ? "zh" : "en"] ?? cityCodeLabel(data.journey.city_code, zh);

  const bookings = wallet?.reservations ?? [];
  const stay =
    (data.options ?? []).find((o) => o.id === data.selected_option_id) ?? null;
  const [showAdvanced, setShowAdvanced] = useState(false);

  return (
    <section className="v8-page" data-testid="v8-me">
      <div className="v8-page-intro compact">
        <div>
          <p className="v8-kicker">{zh ? "我的" : "Me"}</p>
          <h1>{zh ? "我的行程与偏好" : "My trip and preferences"}</h1>
          <p className="v8-lede">
            {zh
              ? "这里是你这趟行程的事实：同行人数、预算、住宿和已确认的预订。"
              : "The facts of this trip: party, budget, stay and confirmed bookings."}
          </p>
        </div>
      </div>

      <div className="v8-me-grid">
        <article className="v8-me-card">
          <span className="v8-kicker">{zh ? "这趟行程" : "This trip"}</span>
          <strong>{cityLabel}</strong>
          <p>
            {t("shell.travelers", { n: data.trip.party_size })} ·{" "}
            {money(
              data.journey.budget.amount_minor,
              data.journey.budget.currency,
              locale,
            )}
          </p>
        </article>

        <article className="v8-me-card">
          <span className="v8-kicker">{zh ? "住宿" : "Stay"}</span>
          <strong>
            {stay
              ? stay.stay.property_name
              : zh
                ? "还没有选定"
                : "Not chosen yet"}
          </strong>
          <p>
            {stay
              ? zh
                ? "已确认的住宿"
                : "Confirmed stay"
              : zh
                ? "在「行程 · 住宿」里挑选"
                : "Choose it in Trips · Stay"}
          </p>
        </article>

        <article className="v8-me-card">
          <span className="v8-kicker">
            {zh ? "已确认的预订" : "Confirmed bookings"}
          </span>
          <strong>
            {bookings.length
              ? zh
                ? `${bookings.length} 项`
                : `${bookings.length}`
              : zh
                ? "还没有"
                : "None yet"}
          </strong>
          <p>
            {bookings.length
              ? zh
                ? "受保护的预订，不会被自动改动。"
                : "Protected bookings; they are never changed automatically."
              : zh
                ? "在「行程 · 预订」里导入你的票或确认单。"
                : "Import a ticket or confirmation in Trips · Bookings."}
          </p>
        </article>

        <article className="v8-me-card">
          <span className="v8-kicker">
            {zh ? "改动偏好" : "Change preferences"}
          </span>
          <strong>{freedomLabel(policy?.mode)}</strong>
          <p>
            {zh
              ? "任何会影响已确认预订或受保护安排的改动，都会先征得你同意。"
              : "Anything affecting a confirmed booking or protected plan asks you first."}
          </p>
          <label className="me-mode-row">
            <span>{zh ? "开启自动执行" : "Allow automatic changes"}</span>
            <input
              type="checkbox"
              data-testid="me-mode-toggle"
              checked={
                String(policy?.mode ?? "").toUpperCase() === "AUTONOMOUS"
              }
              onChange={() => void onActivate()}
            />
          </label>
        </article>

        <PushCapabilityCard travelerId={data.journey.traveler_id} />
      </div>

      <details className="v8-advanced" data-testid="me-advanced">
        <summary>{zh ? "高级详情" : "Advanced detail"}</summary>
        <div className="v8-me-grid">
          <article className="v8-me-card">
            <span className="v8-kicker">
              {zh ? "委派范围" : "Delegation scope"}
            </span>
            <ul>
              <li>
                {t("me.maxCost")}{" "}
                {money(
                  policy?.max_auto_replan_cost_delta_minor ?? 5000,
                  data.journey.budget.currency,
                  locale,
                )}
              </li>
              <li>
                {t("me.maxLoss")}{" "}
                {money(
                  policy?.max_auto_loss_minor ?? 0,
                  data.journey.budget.currency,
                  locale,
                )}
              </li>
              <li>
                {t("me.scheduleDrift")}{" "}
                {policy?.max_auto_schedule_drift_min ?? 20} {t("me.minutes")}
              </li>
              <li>{t("me.protectedMoments")}</li>
            </ul>
          </article>
          <article className="v8-me-card">
            <span className="v8-kicker">
              {zh ? "技术标识" : "Technical identifiers"}
            </span>
            <ul className="me-identifiers">
              <li>
                <b>mode</b> <code>{policy?.mode ?? "ADVISORY"}</code>
              </li>
              <li>
                <b>version</b> <code>{policy?.version ?? "—"}</code>
              </li>
              <li>
                <b>city_code</b> <code>{data.journey.city_code}</code>
              </li>
            </ul>
            <button
              type="button"
              className="v8-outline-button"
              onClick={() => setShowAdvanced(!showAdvanced)}
            >
              {showAdvanced
                ? zh
                  ? "收起"
                  : "Hide"
                : zh
                  ? "查看策略详情"
                  : "View policy detail"}
            </button>
            {showAdvanced && (
              <pre className="me-policy-json" data-testid="me-policy-json">
                {JSON.stringify(policy ?? {}, null, 2)}
              </pre>
            )}
          </article>
        </div>
      </details>
    </section>
  );
}

/** Latin city-code slug for a city name, matching the discovery provider.
 *
 * Chinese city names are mapped to their standard pinyin code so the journey's
 * city_code agrees with the places the provider materialises.  Unknown names
 * fall back to a normalised latin slug.
 */
// Reverse of CITY_CODE_SLUGS: a created Journey stores the latin slug, but the
// traveller reads the city in their own language.
// Travel modes are internal enum values (WALK, RIDE_HAIL).  The traveller
// reads a normal word for how they will move.
/**
 * ORD-0232: the audit fact names the constraint as `"<RULE>_ACCESS_UNAVAILABLE"`.  A rule this file
 * does not know gets the caller's "unnamed condition" wording rather than a humanised code -- the
 * `modeLabel` leak this lane already fixed was exactly "an unmapped enum, prettified and printed".
 */
function hardRuleLabel(code: string | null, zh: boolean, unknown: string): string {
  const labels: Record<string, { zh: string; en: string }> = {
    STROLLER: { zh: "婴儿车通行", en: "stroller access" },
    LUGGAGE: { zh: "大件行李", en: "large luggage" },
  };
  const key = String(code ?? "")
    .toUpperCase()
    .replace(/_ACCESS_UNAVAILABLE$/, "");
  const entry = labels[key];
  return entry ? (zh ? entry.zh : entry.en) : unknown;
}

function modeLabel(mode: string | null | undefined, zh: boolean): string {
  if (!mode) return zh ? "未提供出行方式" : "Mode not provided";
  const key = String(mode).toUpperCase();
  const labels: Record<string, { zh: string; en: string }> = {
    WALK: { zh: "步行", en: "Walk" },
    BICYCLE: { zh: "骑行", en: "Cycle" },
    TRANSIT: { zh: "公交地铁", en: "Transit" },
    AUTO: { zh: "驾车", en: "Drive" },
    RIDE_HAIL: { zh: "网约车", en: "Ride-hail" },
  };
  const entry = labels[key];
  /* ORD-0222: an unmapped value must not reach a Chinese screen as a latin token.  The old
     fallback humanised `RIDE_SHARE_NEW` into "ride share_new" and printed it in zh anyway, which
     is the same leak this order is fixing, just one enum later. */
  return entry
    ? zh
      ? entry.zh
      : entry.en
    : zh
      ? "未识别的出行方式"
      : key.replaceAll("_", " ").toLowerCase();
}

/** ORD-0208 4: structural anchor roles reached the day card as raw enums (「ARRIVAL anchor」). */
function anchorLabel(anchor: string, zh: boolean): string {
  const key = String(anchor).toUpperCase();
  const labels: Record<string, { zh: string; en: string }> = {
    ARRIVAL: { zh: "抵达", en: "Arrival" },
    DEPARTURE: { zh: "离开", en: "Departure" },
    STAY: { zh: "住宿", en: "Stay" },
    CHECK_IN: { zh: "入住", en: "Check-in" },
    CHECK_OUT: { zh: "退房", en: "Check-out" },
    ANCHOR: { zh: "锚点", en: "Anchor" },
  };
  const entry = labels[key];
  /* ORD-0222: same rule as modeLabel -- unknown means unknown, in the traveller's language. */
  return entry
    ? zh
      ? entry.zh
      : entry.en
    : zh
      ? "未识别"
      : key.replaceAll("_", " ").toLowerCase();
}

/**
 * ORD-0222 ②: the day card's heading printed the server's own `label`, whose first segment is an
 * English weekday ("Friday · 09 Oct"), so a Chinese screen showed English it did not need to.  The
 * payload already carries the ISO `date` for that day, so the weekday is a form, not content: it is
 * rendered in the traveller's language from the same date, and the server's text stays the fallback
 * when there is no date to read.  The activity titles on the next line are a different case -- those
 * are server-authored content and are handed off, not rewritten here.
 */
function dayHeading(day: { label: string; date?: string }, zh: boolean): string {
  const iso = String(day.date ?? "");
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) {
    return new Intl.DateTimeFormat(zh ? "zh-CN" : "en-GB", {
      weekday: "long",
      timeZone: "UTC",
    }).format(new Date(`${iso}T00:00:00Z`));
  }
  return day.label.split(" · ")[0];
}

const CITY_NAME_BY_SLUG: Record<string, { zh: string; en: string }> = {
  XIAMEN: { zh: "厦门", en: "Xiamen" },
  BEIJING: { zh: "北京", en: "Beijing" },
  SHANGHAI: { zh: "上海", en: "Shanghai" },
  TIANJIN: { zh: "天津", en: "Tianjin" },
  FUZHOU: { zh: "福州", en: "Fuzhou" },
  GUANGZHOU: { zh: "广州", en: "Guangzhou" },
  SHENZHEN: { zh: "深圳", en: "Shenzhen" },
  CHENGDU: { zh: "成都", en: "Chengdu" },
  HANGZHOU: { zh: "杭州", en: "Hangzhou" },
  TOKYO: { zh: "东京", en: "Tokyo" },
  SINGAPORE: { zh: "新加坡", en: "Singapore" },
};

const CITY_CODE_SLUGS: Record<string, string> = {
  厦门: "XIAMEN",
  北京: "BEIJING",
  上海: "SHANGHAI",
  天津: "TIANJIN",
  福州: "FUZHOU",
  广州: "GUANGZHOU",
  深圳: "SHENZHEN",
  成都: "CHENGDU",
  杭州: "HANGZHOU",
  东京: "TOKYO",
  新加坡: "SINGAPORE",
};

function cityCodeSlug(name: string): string {
  const trimmed = name.trim();
  if (CITY_CODE_SLUGS[trimmed]) return CITY_CODE_SLUGS[trimmed];
  const lower = trimmed.toLowerCase();
  if (CITY_CODE_SLUGS[lower]) return CITY_CODE_SLUGS[lower];
  return trimmed.toUpperCase().replace(/\s+/g, "-");
}
