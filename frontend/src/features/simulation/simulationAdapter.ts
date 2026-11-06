import { CityNexusApiError } from "../../api/citynexus";
import { englishCountNoun } from "../../lib/plural";

/**
 * ORD-0059 (b), adapter half -- the calls the Simulation entry makes, and the words it answers
 * with when one is refused.
 *
 * Two rules shape this file.  First: every action maps onto an endpoint the product already has
 * (`cityNexusApi` wraps most of them); nothing here grows a parallel state machine or edits an
 * array client-side, because a lifecycle the server did not agree to is a fake lifecycle.  The
 * one call that is not already wrapped is written here rather than by widening a shared module,
 * and it is written *without* `occurred_at`: the server has a deterministic default for it, and a
 * client-side `new Date()` in a Simulation entry is precisely the second clock ORD-0059 forbids.
 *
 * Second: a refusal is named, not smoothed over.  The status is checked before the body is
 * trusted (fetch does not reject on 5xx), the server's own code is kept, and the Chinese screen
 * gets Chinese words -- an English `detail` string pasted into a Chinese notice is the defect
 * ORD-0055 was written about.  Where there is no local wording for a code, the line says so and
 * shows the code instead of inventing an explanation.
 */

export type SimulatedEventType = "WEATHER" | "TRAFFIC_DELAY" | "CLOSURE" | "SCHEDULE_DELAY";

export type NamedFailure = {
  code: string;
  status: number;
  /** Traveller-facing copy, in the language the screen is using. */
  zh: string;
  en: string;
};

type ErrorBody = {
  detail?: unknown;
  error?: { code?: unknown; message?: unknown; message_key?: unknown; details?: unknown };
};

function bodyOf(error: unknown): ErrorBody | null {
  if (error instanceof CityNexusApiError) {
    const raw = (error as { body?: unknown }).body;
    if (raw && typeof raw === "object") return raw as ErrorBody;
  }
  return null;
}

/** The server's own sentence, when it sent one.  `message_key` names the *kind* of refusal and
 *  `message` carries the reason; a screen that reads only the key reports "the request was invalid"
 *  to someone who was told exactly which activity has no orderable service. */
export function serverSentence(error: unknown): string {
  const nested = bodyOf(error)?.error;
  if (typeof nested?.message === "string" && nested.message.trim()) return nested.message.trim();
  const detail = bodyOf(error)?.detail;
  return typeof detail === "string" && detail.trim() ? detail.trim() : "";
}

/** The server's own name for the refusal: a code, a message key, or the HTTP status. */
export function failureCode(error: unknown): { code: string; status: number } {
  if (error instanceof CityNexusApiError) {
    const body = bodyOf(error);
    const nested = body?.error;
    const candidate =
      (typeof nested?.message_key === "string" && nested.message_key) ||
      (typeof nested?.code === "string" && nested.code) ||
      (typeof body?.detail === "string" && body.detail) ||
      "";
    return { code: candidate.trim() || `HTTP ${error.status}`, status: error.status };
  }
  if (error instanceof TypeError) return { code: "NETWORK", status: 0 };
  return { code: "UNKNOWN", status: 0 };
}

const TABLE: Record<
  string,
  { zh: (status: number, extra: string) => string; en: (status: number, extra: string) => string }
> = {
  DEMO_MODE_REQUIRED: {
    zh: () => "这台部署没有开模拟舞台（服务器要求 CITYNEXUS_DEMO_MODE=true）。它明确拒绝，并且说没有任何数据被删除。",
    en: () =>
      "This deployment is not a simulation stage (the server requires CITYNEXUS_DEMO_MODE=true). It refused and says nothing was deleted.",
  },
  /* ORD-0070's named 503, so the stage says "wait" instead of "nothing happened". */
  "generation.store_busy": {
    zh: (_s, extra) => `上一次的生成还占着存储${extra}。等它跑完再试，这一步没有失败，只是被挡住了。`,
    en: (_s, extra) =>
      `The previous generation still holds the store${extra}. Wait for it and try again -- this step was blocked, not refused.`,
  },
  GENERATION_STORE_BUSY: {
    zh: (_s, extra) => `上一次的生成还占着存储${extra}。等它跑完再试，这一步没有失败，只是被挡住了。`,
    en: (_s, extra) =>
      `The previous generation still holds the store${extra}. Wait for it and try again -- this step was blocked, not refused.`,
  },
  VALIDATION_ERROR: {
    zh: (_s, extra) => `服务器认为有字段不合法${extra}，所以这一步没有执行。`,
    en: (_s, extra) => `The server rejected one or more fields${extra}, so this step did not run.`,
  },
  NETWORK: {
    zh: () => "请求没有到达服务器（网络或进程断了）。场景状态没有变化，我们也无从得知它变了没有。",
    en: () =>
      "The request never reached the server (network or process down). Nothing about the scene is known from here.",
  },
};

function fieldsOf(error: unknown): string {
  const body = bodyOf(error);
  const errors = (body?.error?.details as { errors?: unknown } | undefined)?.errors;
  if (!Array.isArray(errors)) return "";
  const names = errors
    .map((item) => {
      const record = item as { loc?: unknown; msg?: unknown };
      const loc = Array.isArray(record.loc)
        ? record.loc.filter((part) => typeof part === "string" || typeof part === "number").join(".")
        : "";
      return loc ? String(loc) : "";
    })
    .filter(Boolean);
  return names.length ? `：${[...new Set(names)].join("、")}` : "";
}

function retryHint(error: unknown): string {
  const details = (bodyOf(error)?.error?.details ?? {}) as { retry_after_ms?: unknown };
  const ms = typeof details.retry_after_ms === "number" ? details.retry_after_ms : null;
  if (ms === null) return "（服务器没说要等多久）";
  return ms <= 0 ? "（现在就可以再试）" : `（约 ${Math.round(ms)} 毫秒后再试）`;
}

/** `POST .../replans` answers 422 with the *reason* as a bare machine token in `error.message`
 *  while `message_key` stays the generic `request.invalid` (measured on the sealed demo journey:
 *  code VALIDATION_ERROR, message MOBILITY_DATA_UNAVAILABLE, one gap whose evidence state is
 *  FACT_PRESENT_BUT_NOT_HARD_ELIGIBLE).  Reporting only the key told a traveller "your request was
 *  invalid"; quoting only the token told them nothing they could act on.  These wordings name the
 *  condition the server's own detail carries, keep its code visible, and say what did not change. */
function mobilityRefusal(error: unknown): { legs: number; states: string[] } | null {
  if (serverSentence(error) !== "MOBILITY_DATA_UNAVAILABLE") return null;
  const gaps = (bodyOf(error)?.error?.details as { gaps?: unknown } | undefined)?.gaps;
  if (!Array.isArray(gaps)) return { legs: 0, states: [] };
  const states = [
    ...new Set(
      gaps
        .map((gap) => (gap as { evidence_state?: unknown }).evidence_state)
        .filter((state): state is string => typeof state === "string" && state.length > 0),
    ),
  ];
  return { legs: gaps.length, states };
}

export function describeFailure(error: unknown): NamedFailure {
  const { code, status } = failureCode(error);
  const fields = code === "VALIDATION_ERROR" ? fieldsOf(error) : "";
  const busy = code === "generation.store_busy" || code === "GENERATION_STORE_BUSY";
  const hit = TABLE[code];
  if (hit) {
    const extra = busy ? retryHint(error) : fields;
    return { code, status, zh: hit.zh(status, extra), en: hit.en(status, extra) };
  }
  const mobility = mobilityRefusal(error);
  if (mobility) {
    const states = mobility.states.length ? mobility.states.join("、") : "服务器未给证据状态";
    const legs = mobility.legs > 0 ? ` ${mobility.legs} 段` : "";
    /* The sentence used to be `these` + a noun with no number + `have`, so it said "these leg have facts"
       for every count and named no leg at all in the zero case. The clause now carries the count and
       agrees with it: "this 1 leg has" / "these 2 legs have" / "the route evidence has". */
    const legsClause =
      mobility.legs === 1
        ? "this 1 leg has"
        : mobility.legs > 1
          ? `these ${mobility.legs} ${englishCountNoun(mobility.legs, "leg")} have`
          : "the route evidence has";
    return {
      code,
      status,
      zh: `重算这一版需要能当作硬依据的路线证据。服务器说这些路程${legs}的证据在档，但达不到可硬用的标准（它给的码：MOBILITY_DATA_UNAVAILABLE，证据状态 ${states}）。所以它没有算出任何替代方案，你现在的方案没有变化——这一步要接上真实的路线数据源才会通。`,
      en: `Recomputing this plan needs route evidence that can be treated as a hard basis. The server says ${legsClause} facts on file that do not meet that bar (its code: MOBILITY_DATA_UNAVAILABLE, evidence state ${mobility.states.join(", ") || "not given"}). It produced no alternative, and your current plan is unchanged -- this step needs a real routing data source connected.`,
    };
  }
  if (code.startsWith("INVALID_ACTIVITY_TRANSITION")) {
    /* The server's own name carries the from->to it refused, so the sentence can quote the fact
       instead of guessing which step the stage is really on. */
    return {
      code,
      status,
      zh: `服务器说这一步的顺序不对（${code.replace("INVALID_ACTIVITY_TRANSITION:", "")}），活动状态没有被改动。上面那行会跟着服务器的状态换成它允许的那一步。`,
      en: `The server refused this step's order (${code.replace("INVALID_ACTIVITY_TRANSITION:", "")}); the activity is unchanged. The row above follows the server's state to the step it allows.`,
    };
  }
  /* No local wording for this one.  Say that, name it, and quote the server's own sentence when it
     sent one -- translating it into a Chinese guess would be inventing a reason, and dropping it
     would be the named-reason-never-arrives defect again. */
  const said = serverSentence(error);
  return {
    code,
    status,
    zh: said
      ? `服务器拒绝了这个动作（HTTP ${status}，具名 ${code}）。它的原话：${said}。这条我们没有本地文案，所以照它说的给，不替它解释。`
      : `服务器拒绝了这个动作（HTTP ${status}，具名码 ${code}）。我们没有这条拒绝的本地文案，所以不替它编造原因。`,
    en: said
      ? `The server refused this action (HTTP ${status}, ${code}). Its own words: ${said}. There is no local wording for it, so the sentence is passed through rather than explained.`
      : `The server refused this action (HTTP ${status}, code ${code}). There is no local wording for it, so no reason is invented here.`,
  };
}

/**
 * ORD-0085 / the booking gate: `POST /bookings` refuses an un-bookable activity with a **sentence**
 * rather than a code (`main.py` turns `CommerceValidationError` into `HTTPException(422,
 * str(exc))`), and on the demo stage five of the six activities are of that kind -- `_service_for_
 * activity` only maps a `city_service_id` or two known `place_id`s onto a catalog service.  So a
 * row that assumes "any activity can be booked" hands the teacher a 422 on the spot, and a row that
 * pastes the English sentence onto a Chinese screen is the ORD-0055 defect again.  These wordings
 * quote the server's own reason and name the activity it refused; nothing is invented about why.
 */
const BOOKING_REFUSALS: Record<string, { zh: (t: string) => string; en: (t: string) => string }> = {
  "activity has no supported tradable commerce service": {
    zh: (t: string) => `${t} 这项活动没有可下单的服务（服务器的原话：activity has no supported tradable commerce service）。这一步没有发生，换一个活动试试。`,
    en: (t: string) => `${t} has no service that can be ordered (the server's words: "activity has no supported tradable commerce service"). Nothing was booked; try another activity.`,
  },
  "activity service is not available in the frozen catalog": {
    zh: (t: string) => `${t} 要的服务不在这台舞台的冻结目录里（服务器的原话：activity service is not available in the frozen catalog）。这一步没有发生。`,
    en: (t: string) => `${t}'s service is not in this stage's frozen catalog (the server's words: "activity service is not available in the frozen catalog"). Nothing was booked.`,
  },
  "commerce service semantic identity does not match activity": {
    zh: (t: string) => `${t} 的服务和活动指向的不是同一个地点（服务器的原话：commerce service semantic identity does not match activity），所以服务器不肯下单。`,
    en: (t: string) => `The service behind ${t} points at a different place than the activity does (the server's words: "commerce service semantic identity does not match activity"), so it will not book.`,
  },
  "commerce service belongs to another city": {
    zh: (t: string) => `${t} 的服务属于另一座城市（服务器的原话：commerce service belongs to another city），这一趟订不了。`,
    en: (t: string) => `The service behind ${t} belongs to another city (the server's words: "commerce service belongs to another city"), so this trip cannot book it.`,
  },
};

export function describeBookingRefusal(
  error: unknown,
  activityTitle: string,
): NamedFailure {
  const { code, status } = failureCode(error);
  /* The reason arrives in `error.message` while `message_key` says only "request.invalid", so this
     lookup is keyed on the sentence -- measured on a throwaway stack (`review/bookings-*.json`):
     four of the six activities on the demo plan answer 422 with exactly these words. */
  const hit = BOOKING_REFUSALS[serverSentence(error)];
  if (hit) return { code, status, zh: hit.zh(activityTitle), en: hit.en(activityTitle) };
  return describeFailure(error);
}

/**
 * "重置到场景起点".  ORD-0074 made reset run the real pipeline (`plans/generate` -> `select` ->
 * `/start`) and report what happened in `simulation_advance` -- as a JSON string, and as a
 * **recorded refusal on a 200** when a step is denied.  So `status: 200` is not the success
 * signal here; the advance report is.  Reading only the 200 would let this screen claim a
 * lifecycle the server explicitly did not grant, which is the failure ORD-0059 forbids.
 */
export type AdvanceStep = {
  step?: string;
  ms?: number;
  ok?: boolean;
  refused?: string | number;
  detail?: string;
};

export type DemoReset = {
  status: string;
  traveler_id?: string;
  journey_id?: string;
  city_code?: string;
  advance: {
    steps: AdvanceStep[];
    totalMs: number | null;
    journeyStatus: string | null;
    selectedPlanVersionId: string | null;
  } | null;
};

export async function resetStage(): Promise<DemoReset> {
  const response = await fetch("/api/v1/demo/reset", {
    method: "POST",
    headers: { Accept: "application/json" },
  });
  const text = await response.text();
  let body: Record<string, unknown> = {};
  if (text) {
    try {
      body = JSON.parse(text) as Record<string, unknown>;
    } catch {
      body = {};
    }
  }
  if (!response.ok) throw new CityNexusApiError(response.status, body, `demo/reset ${response.status}`);
  const rawAdvance = body.simulation_advance;
  let advance: DemoReset["advance"] = null;
  if (typeof rawAdvance === "string" && rawAdvance) {
    try {
      const parsed = JSON.parse(rawAdvance) as Record<string, unknown>;
      advance = {
        steps: Array.isArray(parsed.steps) ? (parsed.steps as AdvanceStep[]) : [],
        totalMs: typeof parsed.total_ms === "number" ? parsed.total_ms : null,
        journeyStatus: typeof parsed.journey_status === "string" ? parsed.journey_status : null,
        selectedPlanVersionId:
          typeof parsed.selected_plan_version_id === "string" ? parsed.selected_plan_version_id : null,
      };
    } catch {
      /* An unreadable report is reported as unreadable below, never read as a success. */
      advance = { steps: [], totalMs: null, journeyStatus: null, selectedPlanVersionId: null };
    }
  }
  return {
    status: typeof body.status === "string" ? body.status : "unknown",
    traveler_id: typeof body.traveler_id === "string" ? body.traveler_id : undefined,
    journey_id: typeof body.journey_id === "string" ? body.journey_id : undefined,
    city_code: typeof body.city_code === "string" ? body.city_code : undefined,
    advance,
  };
}
/**
 * "推进一个活动".  The server owns the activity state graph -- `NOT_STARTED|ACTIVE -> EN_ROUTE ->
 * ARRIVED -> IN_PROGRESS -> COMPLETED` -- and answers a named 409 (`INVALID_ACTIVITY_TRANSITION:
 * <from>-><to>; expected=<from…>`) for a step that is out of order, which is exactly what a panel
 * that hard-codes "complete" gets on a frozen V12/V13 plan.  So the row offers the one transition
 * the server currently allows, and the label changes because the server's state changed.
 */
export type StageStep = "depart" | "arrive" | "begin" | "complete";

export const NEXT_STEP: Record<string, { step: StageStep; zh: string; en: string } | undefined> = {
  NOT_STARTED: { step: "depart", zh: "出发", en: "depart" },
  ACTIVE: { step: "depart", zh: "出发", en: "depart" },
  EN_ROUTE: { step: "arrive", zh: "到达", en: "arrive" },
  ARRIVED: { step: "begin", zh: "开始这项活动", en: "begin" },
  IN_PROGRESS: { step: "complete", zh: "完成这项活动", en: "complete" },
};

export type TransitionTwin = {
  current_activity_id: string | null;
  projection_status: string;
  active_plan_version_id?: string | null;
};

export async function transitionStage(
  journeyId: string,
  activityId: string,
  step: StageStep,
): Promise<TransitionTwin> {
  const response = await fetch(`/api/v1/journeys/${journeyId}/activities/${activityId}/${step}`, {
    method: "POST",
    headers: { Accept: "application/json" },
  });
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  /* The envelope survives as a CityNexusApiError on purpose: the plain Error the shared helper
     builds drops the status, and a refusal whose name is dropped is the ORD-0070 defect again. */
  if (!response.ok) {
    throw new CityNexusApiError(response.status, body, `activities/${activityId}/${step} ${response.status}`);
  }
  return body as TransitionTwin;
}

/**
 * The two stage actions `65e184d` split out of reset: `prepare-scenario` runs `plans/generate`
 * then `select` and stops at PLANNED, `start-scenario` runs `/start` with all of its gates.  Both
 * answer 200 with a **report**, and a refused start is still a 200 with a named code -- so a
 * caller that reads only the HTTP status would claim a stage that never started.
 */
export type StageAction = "prepare-scenario" | "start-scenario";

export type StageReport = {
  status: string;
  journeyId: string | null;
  journeyStatus: string | null;
  planVersionId: string | null;
  code: string | null;
  advance: { steps: AdvanceStep[]; totalMs: number | null } | null;
};

export async function runStageAction(action: StageAction): Promise<StageReport> {
  const response = await fetch(`/api/v1/demo/${action}`, {
    method: "POST",
    headers: { Accept: "application/json" },
  });
  const text = await response.text();
  let body: Record<string, unknown> = {};
  if (text) {
    try {
      body = JSON.parse(text) as Record<string, unknown>;
    } catch {
      body = {};
    }
  }
  if (!response.ok) throw new CityNexusApiError(response.status, body, `demo/${action} ${response.status}`);
  const error = body.error as { code?: unknown; message_key?: unknown } | undefined;
  const advance = body.advance as { steps?: AdvanceStep[]; total_ms?: number } | undefined;
  return {
    status: typeof body.status === "string" ? body.status : `HTTP ${response.status}`,
    journeyId: typeof body.journey_id === "string" ? body.journey_id : null,
    journeyStatus: typeof body.journey_status === "string" ? body.journey_status : null,
    planVersionId: typeof body.plan_version_id === "string" ? body.plan_version_id : null,
    code: typeof error?.message_key === "string" ? error.message_key : typeof error?.code === "string" ? error.code : null,
    advance: advance
      ? {
          steps: Array.isArray(advance.steps) ? advance.steps : [],
          totalMs: typeof advance.total_ms === "number" ? advance.total_ms : null,
        }
      : null,
  };
}

/**
 * "触发一次中断".  `provenance` is omitted: the handler assigns SIMULATED for stage input and
 * rejects a client that claims trusted provenance, so claiming it here would be the fake-trust
 * shape the backend already refuses.  `occurred_at` is omitted for the same reason -- the server
 * derives it from the cursor, and a client clock must not enter this path.
 */
export async function postSimulatedEvent(
  journeyId: string,
  event: { eventType: SimulatedEventType; severity: "MINOR" | "MAJOR" | "CRITICAL"; activityId: string | null },
): Promise<{ id: string; occurred_at: string; status: string }> {
  const response = await fetch(`/api/v1/journeys/${journeyId}/events`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({
      event_type: event.eventType,
      severity: event.severity,
      affected_activity_id: event.activityId,
      payload: { source: "simulation-panel" },
    }),
  });
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!response.ok) throw new CityNexusApiError(response.status, body, `events ${response.status}`);
  return body as { id: string; occurred_at: string; status: string };
}

/**
 * ORD-0100 ①: which activity 「建一个硬承诺」 may honestly be aimed at.
 *
 * Two server reads, and no guessing in between:
 *
 *  - `GET /city-services?city_code=` names the places this city's catalogue carries an orderable
 *    service for, in the server's own type words.
 *  - `GET /city-services/{id}/booking-slots?start_at=&end_at=` is the only read that separates a
 *    service the frozen catalogue can actually take from one that is merely listed.  Measured on a
 *    throwaway stack (`review/bookable-target-shape.json`): of the demo plan's six activities, two
 *    carry an orderable type; `service-food-preorder` is listed by `/city-services` and answers
 *    **404 here**, and its booking is refused with `activity service is not available in the frozen
 *    catalog`; `service-river-cruise-booking` answers 200 with one slot and books `201 CONFIRMED`.
 *
 * So a 404 on this path is not a failure to display -- it is the answer that retires the candidate.
 * Anything else (5xx, a dropped connection) *is* unreadable, and gets said as such rather than being
 * folded into "nothing can be booked", which would be the panel claiming a server fact it did not get.
 */
export const ORDERABLE_SERVICE_TYPES: readonly string[] = [
  "bookable_experience",
  "tradable_food",
];

export type CityServiceRow = {
  id: string;
  name: string;
  service_type: string;
  city_code: string;
  place_id: string | null;
};

export async function listCityServices(
  cityCode: string,
): Promise<CityServiceRow[]> {
  const response = await fetch(
    `/api/v1/city-services?city_code=${encodeURIComponent(cityCode)}`,
    { headers: { Accept: "application/json" } },
  );
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    throw new CityNexusApiError(response.status, body, `city-services ${response.status}`);
  }
  return Array.isArray(body) ? (body as CityServiceRow[]) : [];
}

export type BookableTargets = {
  /** activity ids the server gave a slot for, kept in the plan's own order */
  targets: Array<{ activityId: string; serviceId: string; slots: number }>;
  /** how many open activities carry an orderable service type at all */
  withOrderableService: number;
  /** how many of those the server could not be read on -- not the same answer as "no slot" */
  unreadable: number;
};

export async function readBookableTargets(
  activities: Array<{ id: string; place_id: string | null; start_at: string; end_at: string }>,
  services: CityServiceRow[],
): Promise<BookableTargets> {
  const orderable = new Map<string, CityServiceRow[]>();
  for (const row of services) {
    if (!row.place_id || !ORDERABLE_SERVICE_TYPES.includes(row.service_type)) continue;
    const held = orderable.get(row.place_id) ?? [];
    held.push(row);
    orderable.set(row.place_id, held);
  }
  const result: BookableTargets = { targets: [], withOrderableService: 0, unreadable: 0 };
  for (const activity of activities) {
    const candidates = activity.place_id ? orderable.get(activity.place_id) : undefined;
    if (!candidates?.length) continue;
    result.withOrderableService += 1;
    for (const service of candidates) {
      let response: Response;
      try {
        response = await fetch(
          `/api/v1/city-services/${encodeURIComponent(service.id)}/booking-slots` +
            `?start_at=${encodeURIComponent(activity.start_at)}` +
            `&end_at=${encodeURIComponent(activity.end_at)}`,
          { headers: { Accept: "application/json" } },
        );
      } catch {
        result.unreadable += 1;
        continue;
      }
      if (response.status === 404) {
        /* The catalogue lists the service; the booking side has never heard of it.  That is an
           answer, and it is the one the frozen-catalog refusal was hiding behind. */
        continue;
      }
      if (!response.ok) {
        result.unreadable += 1;
        continue;
      }
      const slots = (await response.json().catch(() => null)) as unknown;
      const count = Array.isArray(slots) ? slots.length : 0;
      if (count > 0) {
        result.targets.push({
          activityId: activity.id,
          serviceId: service.id,
          slots: count,
        });
      }
    }
  }
  return result;
}
