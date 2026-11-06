/**
 * ORD-0382 — the AI generation run lives here, not inside the sheet that started it.
 *
 * The defect this closes: `PlanWithAiSheet` owned the `AbortController` in a ref and aborted on
 * unmount (`useEffect(() => () => abortRef.current?.abort(), [])`).  So "生成中" was only survivable
 * while one modal stayed open -- closing it to look at something else killed the request, and
 * re-opening it showed a blank form.  Pi Travel splits the same flow into three screens for exactly
 * this reason (`activity_journey_planning_select` -> `_waiting` -> `_result`, with 查看会话 /
 * 继续生成 as the waiting state's two exits); this module is the *waiting* half of that shape, held
 * above the router so the run outlives the surface that started it.
 *
 * What may be reported as progress, and what may not:
 *  - the phase this file itself awaits, because this file is the one awaiting it;
 *  - the seconds since this device sent, measured from `startedAt`;
 *  - per-plan / per-day / per-leg UNITS derived from rows this run already fetched (`planStamp`);
 *  - NOT a day-by-day 「正在处理第 N 天」: the backend emits no per-day signal.  Measured on the live
 *    journey's own `agent-feed` (95 events, `review/evidence/ord0382-live-readings.json`), the fact
 *    keys are `deterministic_gate`, `leg_count`, `locked_commitments_preserved`, `option_id`,
 *    `source`, `mission_id`, `run_id`, `model`, `outcome`, `status`, `failure_reason`, `sequence_no`,
 *    `tool`, `decision`, `proposal_id`, `request_id`, `trigger_event_id` and the `trace.span` set --
 *    none names a day, and the only per-day-shaped quantity (`leg_count`) sits on
 *    `v9.plan.materialized`, which carries no `mission_id` and so cannot be attributed to this run
 *    from the event alone.  Reported as `needs_backend_field`, never invented here.
 *
 * Durability boundary, stated rather than implied: the run survives leaving the sheet and moving
 * between routes *within this page*.  A reload destroys the in-flight fetch; what survives a reload
 * is already `api/journeyContinuation`'s marker, which this module neither duplicates nor claims.
 */

import { useSyncExternalStore } from "react";
import { markPendingContinuation } from "../../api/journeyContinuation";
import { classifyReadFailure, type Attribution } from "../../lib/failureAttribution";
import {
  fetchWithKeylessWriteDeadline,
  isKeylessWriteDeadlineMissed,
  keylessWriteUnknownReading,
} from "../../lib/keylessWriteDeadline";

/**
 * ORD-0369: who actually wrote `reasoning_summary`.
 *
 * Measured against the running backend with the AI switch off: `POST .../agent/commands` still
 * answers HTTP 200 with `mission.status = "COMPLETED"` and a populated `reasoning_summary`, and the
 * mission itself carries `ai_state = "DISABLED_BY_OPS"`. Mounting that sentence under ORD-0240's
 * 「模型原始输出」 label credits a model for text the deterministic compiler wrote -- the mirror
 * image of the defect ORD-0240 closed.
 *
 * The vocabulary is the one `backend/app/agent/ai_availability.py:36-44` produces after ORD-0359,
 * which renamed `READY` away because "a key exists" was promising that it works. So
 * `CONFIGURED_UNVERIFIED` deliberately does **not** mean a model wrote this -- mapping it to
 * "model" would re-open 0359's hole one level up. Only `PROVIDER_REFUSED` states that the provider
 * was actually reached and answered. Everything else, including a missing state, keeps today's
 * wording, because "we were not told" is not evidence that nothing ran either.
 */
type SummaryOrigin = "model" | "no_model" | "not_stated";

export function summaryOriginOf(mission: unknown): SummaryOrigin {
  if (!mission || typeof mission !== "object") return "not_stated";
  const stated = mission as { ai_state?: unknown; deepseek_configured?: unknown };
  const state = typeof stated.ai_state === "string" ? stated.ai_state : "";
  if (state === "DISABLED_BY_OPS" || state === "NOT_CONFIGURED") return "no_model";
  if (state === "PROVIDER_REFUSED") return "model";
  if (state === "" && stated.deepseek_configured === false) return "no_model";
  return "not_stated";
}

/** The phases this driver itself enters, in the order it can enter them. */
export type PhaseKey = "baseline" | "request" | "verify" | "fallback";

export const PHASES: { key: PhaseKey; zh: string; en: string }[] = [
  { key: "baseline", zh: "读取已有方案作为基线", en: "Reading existing plans as a baseline" },
  { key: "request", zh: "发出请求，等待模型返回", en: "Request sent, waiting for the model" },
  { key: "verify", zh: "核对本次是否真的产出了新方案", en: "Checking whether this run produced plans" },
  {
    key: "fallback",
    zh: "改用已发现的城市地点重新排一版",
    en: "Re-planning from the places already discovered",
  },
];

/**
 * Which mouth a failure sentence came from, carried with the sentence (ORD-0280).
 * Moved here with the legs that produce it so there is exactly one definition of "who spoke".
 */
export type FailureNote = {
  text: string;
  from: "server" | "device";
  /** Q70: the contract field the words were read out of.  Absent when this device composed them. */
  field?: string;
};

/** What this device measured on the leg that answered, before it tried to understand any of it. */
export type RawReading = {
  http_status: number;
  content_type: string;
  bytes: number | null;
};

export type RunResult = {
  status: string;
  failureReason: FailureNote | null;
  summary: string | null;
  /** ORD-0369: whose sentence `summary` is, taken from the mission's own `ai_state` -- never from
   *  whether this run happened to reach the success branch. */
  summaryOrigin?: SummaryOrigin;
  rawReading?: RawReading | null;
  planCount?: number;
  newPlanIds?: string[];
  reasonCode?: { code: string; field: string } | null;
  units?: RunUnits | null;
};

/**
 * One plan row reduced to the quantities a traveller can check by eye: how many arrangements, over
 * how many calendar days.  Read off the row the server already sent.
 *
 * Transport legs are deliberately absent.  `mobility_evidence.facts` is not a plan's leg list:
 * measured on the two scenarios this lane holds bytes for, the CNX-DEMO plan (6 arrangements, 1 day)
 * carries 56 fact rows whose keys are 8 origins x 8 destinations = 56 ordered pairs -- a complete OD
 * matrix -- while the sealed Guangzhou plan (5 arrangements) carried 4, which is why counting rows
 * there looked plausible and is wrong either way.  Printing that count told a traveller their one-day
 * trip had 56 transport legs.  The only real per-plan leg figure is
 * `v9.plan.materialized.facts.leg_count`, joinable by `entity_id` but costing an extra `agent-feed`
 * read, so it is named as a `needs_backend_field` (a `leg_count` on the plan row) not approximated.
 */
export type PlanStamp = {
  id: string;
  versionNo: number | null;
  activities: number;
  days: number;
};

/** The units this run produced, summed over the plans attributed to it. */
export type RunUnits = { landed: number; activities: number; days: number };

/**
 * What an interruption left behind, measured by a fresh read AFTER the abort.
 *
 * This is the order's third ask: closing the panel used to be the end of the story, so nobody could
 * tell where it stopped, whether anything produced survived, or whether the trip moved.  `read`
 * stays `failed` when the confirming read itself did not answer, and then nothing below it is known
 * -- which the screen says as unknown instead of falling back to 「行程没有被改动」.
 */
export type Aftermath = {
  stoppedAt: PhaseKey | null;
  /** Did anything ever leave this device?  Decides "clean stop" from "stopped mid-flight". */
  requestSent: boolean;
  read: "not_needed" | "checking" | "answered" | "failed";
  httpStatus: number | null;
  landedNewPlanIds: string[];
  currentBefore: PlanStamp | null;
  currentAfter: PlanStamp | null;
  /** Only set when the confirming read failed: the shared channel's own verdict on why. */
  attribution: Attribution | null;
};

export type RunSnapshot = {
  /** Increments per submission, so a surface can tell two runs of one journey apart. */
  runId: number;
  journeyId: string | null;
  running: boolean;
  /** Whether the sheet is open on this run.  False = the traveller left and the run kept going. */
  watched: boolean;
  startedAt: number;
  phase: PhaseKey | null;
  result: RunResult | null;
  /** A settled outcome nobody has read yet -- what keeps the strip alive after the run finishes. */
  unread: boolean;
  /**
   * The traveller's own words for this run.  The sheet holds the textarea in local state, so a run
   * that outlives the sheet would otherwise come back to an empty box -- and 「照原话再试一次」 on a
   * sheet with no 原话 is a button that cannot do the one thing its label promises.
   */
  command: string;
  aftermath: Aftermath | null;
  /**
   * Which plan was in force at this run's own baseline read, and how big it is.  The waiting surface
   * reports it as the scope the traveller is waiting inside (「这趟 2 天」 is checkable against their
   * own trip); the closing state compares against it to answer "did the trip move".
   */
  current: PlanStamp | null;
};

const EMPTY: RunSnapshot = {
  runId: 0,
  journeyId: null,
  running: false,
  watched: true,
  startedAt: 0,
  phase: null,
  result: null,
  unread: false,
  command: "",
  aftermath: null,
  current: null,
};

let snapshot: RunSnapshot = { ...EMPTY };
const listeners = new Set<() => void>();
let controller: AbortController | null = null;
/** The plan ids present before this run, or `null` when the baseline was indeterminate. */
let baselinePlanIds: Set<string> | null = null;
/** Which plan was in force at the baseline, so "did the trip move" has a before-side. */
let baselineCurrentStamp: PlanStamp | null = null;

function emit(patch: Partial<RunSnapshot>): void {
  snapshot = { ...snapshot, ...patch };
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useAiRun(): RunSnapshot {
  return useSyncExternalStore(subscribe, getRunSnapshot);
}

export function getRunSnapshot(): RunSnapshot {
  return snapshot;
}

/** The sheet mounted on this run, so its outcome counts as read. */
export function watchRun(): void {
  emit({ watched: true, unread: false });
}

/** The sheet closed while the run is still going: keep driving, stop rendering a modal. */
export function unwatchRun(): void {
  emit({ watched: false });
}

/** The traveller is done with a settled outcome; the strip has nothing left to say. */
export function dismissRun(): void {
  if (snapshot.running) return;
  controller = null;
  emit({ ...EMPTY, runId: snapshot.runId });
}

/**
 * 「停止等待」: abort what this device is holding, then -- only if something had already left --
 * read the store back so the closing state is a measurement rather than a reassurance.
 *
 * The confirming read uses its OWN controller, not the run's: the run's signal is the one just
 * aborted, so reusing it would make the check fail for the traveller's own decision and report "we
 * could not check" when nothing prevented the check.
 */
export async function stopWaiting(): Promise<void> {
  const inFlight = controller;
  const journeyId = snapshot.journeyId;
  const stoppedAt = snapshot.phase;
  /* `request` is entered immediately before the command leg is issued, so "request or later" is the
     same fact the sheet used a closure-local `reached` for: the point past which stopping no longer
     guarantees nothing happened. */
  const requestSent = stoppedAt === "request" || stoppedAt === "verify" || stoppedAt === "fallback";
  if (!requestSent) {
    inFlight?.abort();
    emit({ aftermath: { stoppedAt, requestSent: false, read: "not_needed", httpStatus: null,
      landedNewPlanIds: [], currentBefore: baselineCurrentStamp, currentAfter: null, attribution: null } });
    return;
  }
  emit({
    aftermath: { stoppedAt, requestSent: true, read: "checking", httpStatus: null,
      landedNewPlanIds: [], currentBefore: baselineCurrentStamp, currentAfter: null, attribution: null },
  });
  inFlight?.abort();
  const confirmed = await confirmAfterInterruption(journeyId, stoppedAt);
  /* Only still reportable if this run is still the one on screen. */
  emit({ aftermath: confirmed });
}

async function confirmAfterInterruption(
  journeyId: string | null,
  stoppedAt: PhaseKey | null,
): Promise<Aftermath> {
  const base = {
    stoppedAt,
    requestSent: true,
    currentBefore: baselineCurrentStamp,
    currentAfter: null as PlanStamp | null,
    landedNewPlanIds: [] as string[],
    httpStatus: null as number | null,
    attribution: null as Attribution | null,
  };
  if (!journeyId) return { ...base, read: "failed" as const };
  const probe = new AbortController();
  const timer = window.setTimeout(() => probe.abort(), CONFIRM_READ_MS);
  try {
    const response = await fetch(`/api/v1/journeys/${journeyId}/plans`, {
      headers: { Accept: "application/json" },
      signal: probe.signal,
    });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      let parsed: unknown = null;
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = null;
      }
      const envelope = parsed as { error?: { code?: unknown; message?: unknown } } | null;
      return {
        ...base,
        read: "failed",
        httpStatus: response.status,
        /* The existing channel, not a second taxonomy: this screen does not decide on its own what
           a 403 or a dead origin means (R14). */
        attribution: classifyReadFailure(null, {
          status: response.status,
          code: typeof envelope?.error?.code === "string" ? envelope.error.code : null,
          message: typeof envelope?.error?.message === "string" ? envelope.error.message : null,
        }),
      };
    }
    const rows = await response.json().catch(() => null);
    if (!Array.isArray(rows)) {
      return {
        ...base,
        read: "failed",
        httpStatus: response.status,
        attribution: classifyReadFailure(null, { status: response.status, code: null, message: null }),
      };
    }
    const ids = rows
      .map((item: { id?: unknown }) => item?.id)
      .filter((id: unknown): id is string => typeof id === "string" && id.length > 0);
    return {
      ...base,
      read: "answered",
      httpStatus: response.status,
      landedNewPlanIds: baselinePlanIds === null ? [] : ids.filter((id) => !baselinePlanIds?.has(id)),
      currentAfter: currentStampOf(rows),
    };
  } catch (cause) {
    return { ...base, read: "failed", attribution: classifyReadFailure(cause, null) };
  } finally {
    window.clearTimeout(timer);
  }
}

/** The read-back's own ceiling: 12 s, the same floor every other read in this app carries. */
export const CONFIRM_READ_MS = 12_000;

/** The three envelope keys this server puts a machine name in (`main.py:497-502`). */
const REASON_CODE_KEYS = ["reason_code", "code", "message_key"] as const;

export function serverReasonCode(body: unknown): { code: string; field: string } | null {
  const error = (body as { error?: Record<string, unknown> } | null)?.error;
  if (!error || typeof error !== "object") return null;
  for (const key of REASON_CODE_KEYS) {
    const value = error[key];
    if (typeof value === "string" && value.trim()) return { code: value.trim().toUpperCase(), field: key };
  }
  return null;
}

export function serverReasonSentence(body: unknown): { text: string; field: string } | null {
  const error = (body as { error?: Record<string, unknown> } | null)?.error;
  if (error && typeof error.message === "string" && error.message.trim())
    return { text: error.message, field: "error.message" };
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail.trim()) return { text: detail, field: "detail" };
  return null;
}

export function failureNote(
  server: { text: string; field: string } | null,
  deviceText: string,
): FailureNote {
  return server
    ? { text: server.text, from: "server", field: server.field }
    : { text: deviceText, from: "device" };
}

function isAbort(cause: unknown): boolean {
  return cause instanceof Error && cause.name === "AbortError";
}

function planStamp(row: unknown): PlanStamp | null {
  const id = (row as { id?: unknown } | null)?.id;
  if (typeof id !== "string" || id.length === 0) return null;
  const dates = activityDates(row);
  return {
    id,
    versionNo:
      typeof (row as { version_no?: unknown }).version_no === "number"
        ? (row as { version_no: number }).version_no
        : null,
    activities: Array.isArray((row as { activities?: unknown }).activities)
      ? (row as { activities: unknown[] }).activities.length
      : 0,
    days: dates.size,
  };
}

/** The calendar days this row's own arrangements fall on, as the server spelled them. */
function activityDates(row: unknown): Set<string> {
  const activities = Array.isArray((row as { activities?: unknown }).activities)
    ? ((row as { activities: unknown[] }).activities)
    : [];
  return new Set(
    activities
      .map((item) => (item as { start_at?: unknown })?.start_at)
      .filter((value): value is string => typeof value === "string" && value.length >= 10)
      .map((value) => value.slice(0, 10)),
  );
}

function currentStampOf(rows: unknown[]): PlanStamp | null {
  for (const row of rows) {
    const plan = row as { is_current?: unknown; status?: unknown };
    if (plan?.is_current === true || String(plan?.status ?? "").toUpperCase() === "ACTIVE") {
      const stamp = planStamp(row);
      if (stamp) return stamp;
    }
  }
  return null;
}

/** One `/plans` read reduced to the two things this driver answers for: which ids, what is in force. */
async function readPlanRows(
  journeyId: string,
  signal: AbortSignal,
): Promise<{ ok: true; ids: string[]; current: PlanStamp | null } | { ok: false }> {
  try {
    const response = await fetch(`/api/v1/journeys/${journeyId}/plans`, {
      headers: { Accept: "application/json" },
      signal,
    });
    if (!response.ok) return { ok: false };
    const rows = await response.json();
    if (!Array.isArray(rows)) return { ok: false };
    return {
      ok: true,
      ids: rows
        .map((item: { id?: unknown }) => item?.id)
        .filter((id: unknown): id is string => typeof id === "string" && id.length > 0),
      current: currentStampOf(rows),
    };
  } catch (cause) {
    if (isAbort(cause)) throw cause;
    return { ok: false };
  }
}

/**
 * Sum the traveller-checkable units over the plans this run produced.
 *
 * `rows` is the response the compose leg already received -- never a second request bought for a
 * prettier number.  `null` when this run composed nothing, which the screen renders as "no units to
 * report" rather than as zeros.
 */
function unitsFromRows(rows: unknown[] | null, landedIds: string[]): RunUnits | null {
  if (!rows || rows.length === 0) return null;
  const wanted = new Set(landedIds);
  const landed = rows.filter((row) => wanted.has(String((row as { id?: unknown })?.id ?? "")));
  const stamps = landed.map(planStamp).filter((stamp): stamp is PlanStamp => stamp !== null);
  if (stamps.length === 0) return null;
  const dates = new Set<string>();
  for (const row of landed) for (const date of activityDates(row)) dates.add(date);
  return {
    landed: stamps.length,
    activities: stamps.reduce((sum, item) => sum + item.activities, 0),
    days: dates.size,
  };
}

/**
 * The whole submission, moved out of the sheet.
 *
 * Every status string, branch and reason code below is the one `PlanWithAiSheet` already produced --
 * those states are pinned by `e2e/qoder-plan-ai-thinking.spec.ts` and
 * `e2e/qoder-ai-sheet-blocked.spec.ts`.  What changed is *where the state lives*, not what the run
 * decides.
 */
export async function beginAiRun(args: {
  journeyId: string;
  cityCode?: string;
  command: string;
  /** Minted by the caller from the submission intent, exactly as the sheet did. */
  idempotencyKey: string;
}): Promise<RunResult | null> {
  const { journeyId, cityCode, command, idempotencyKey } = args;
  if (snapshot.running) return null;
  const abort = new AbortController();
  const signal = abort.signal;
  controller = abort;
  baselinePlanIds = null;
  baselineCurrentStamp = null;
  emit({
    runId: snapshot.runId + 1,
    journeyId,
    running: true,
    watched: true,
    startedAt: Date.now(),
    phase: "baseline",
    result: null,
    unread: false,
    command,
    aftermath: null,
    current: null,
  });

  let reached: PhaseKey = "baseline";
  const enter = (next: PhaseKey) => {
    reached = next;
    emit({ phase: next });
  };
  const settle = (result: RunResult): RunResult => {
    controller = null;
    emit({ running: false, phase: null, result, unread: !snapshot.watched });
    return result;
  };

  try {
    const baseline = await readPlanRows(journeyId, signal);
    if (baseline.ok) {
      baselinePlanIds = new Set(baseline.ids);
      baselineCurrentStamp = baseline.current;
      emit({ current: baseline.current });
    }
    enter("request");
    const response = await fetchWithKeylessWriteDeadline(
      `/api/v1/journeys/${journeyId}/agent/commands`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify({ command }),
        signal,
      },
    );
    const raw = await response.text().catch(() => null);
    let body: unknown = null;
    if (raw !== null) {
      try {
        body = JSON.parse(raw);
      } catch {
        body = null;
      }
    }
    const rawReading: RawReading = {
      http_status: response.status,
      content_type: response.headers.get("content-type") ?? "",
      bytes: raw === null ? null : new TextEncoder().encode(raw).length,
    };
    const mission = (
      body as {
        mission?: {
          id?: string;
          status?: string;
          failure_reason?: string | null;
          reasoning_summary?: string | null;
        };
      } | null
    )?.mission ?? null;
    if (mission?.id) markPendingContinuation(journeyId, String(mission.id));
    if (!response.ok) {
      return settle({
        status: `HTTP_${response.status}`,
        failureReason: failureNote(
          serverReasonSentence(body),
          `agent/commands -> HTTP ${response.status}`,
        ),
        summary: null,
        rawReading,
        planCount: -1,
        newPlanIds: [],
      });
    }

    const isNewPlanId = (id: string | undefined): id is string =>
      typeof id === "string" && id.length > 0 && baselinePlanIds !== null && !baselinePlanIds.has(id);
    const recount = async (): Promise<string[] | null> => {
      if (baselinePlanIds === null) return null;
      const read = await readPlanRows(journeyId, signal);
      return read.ok ? read.ids.filter(isNewPlanId) : null;
    };

    enter("verify");
    const counted = await recount();
    let newPlanIds: string[] = counted ?? [];
    let planCount = counted === null ? -1 : newPlanIds.length;
    let blockedCode = "";
    let blockedReasonCode: { code: string; field: string } | null = null;
    let blockedSentence: FailureNote | null = null;
    let composedRows: unknown[] | null = null;

    if (baselinePlanIds !== null && planCount === 0) {
      enter("fallback");
      try {
        const placesResponse = await fetch(
          `/api/v1/places?city_code=${encodeURIComponent(cityCode ?? "")}`,
          { headers: { Accept: "application/json" }, signal },
        );
        if (!placesResponse.ok) {
          const unreadable = await placesResponse.json().catch(() => null);
          const refusedCode = serverReasonCode(unreadable);
          return settle({
            status: `BLOCKED:${refusedCode ? refusedCode.code : `PLACES_HTTP_${placesResponse.status}`}`,
            reasonCode: refusedCode,
            failureReason: failureNote(
              serverReasonSentence(unreadable),
              `GET /places -> HTTP ${placesResponse.status}`,
            ),
            summary: null,
            planCount: -1,
            newPlanIds: [],
          });
        }
        const catalogue = await placesResponse.json();
        const knownIds = new Set(
          (Array.isArray(catalogue) ? catalogue : [])
            .map((item: { id?: string }) => item?.id)
            .filter((id: unknown): id is string => typeof id === "string" && id.length > 0),
        );
        if (knownIds.size < 2) {
          return settle({
            status: knownIds.size === 0 ? "BLOCKED:CATALOG_EMPTY" : "BLOCKED:CATALOG_INSUFFICIENT",
            failureReason: {
              text: `GET /places?city_code=${cityCode} -> ${knownIds.size} places`,
              from: "device",
            },
            summary: null,
            planCount: -1,
            newPlanIds: [],
          });
        }
        const generate = await fetch(`/api/v1/journeys/${journeyId}/plans/generate`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
            "Idempotency-Key": idempotencyKey,
          },
          body: JSON.stringify({ candidate_count: 3 }),
          signal,
        });
        if (generate.ok) {
          const created = await generate.json();
          composedRows = Array.isArray(created) ? created : null;
          newPlanIds = (Array.isArray(created) ? created : [])
            .map((item: { id?: string }) => item?.id)
            .filter(isNewPlanId);
          if (newPlanIds.length === 0) {
            const recounted = await recount();
            if (recounted !== null) newPlanIds = recounted;
          }
          planCount = newPlanIds.length;
        } else {
          const refused = await generate.json().catch(() => null);
          blockedReasonCode = serverReasonCode(refused);
          blockedCode = blockedReasonCode ? blockedReasonCode.code : "";
          blockedSentence = failureNote(
            serverReasonSentence(refused),
            `POST /plans/generate -> HTTP ${generate.status}`,
          );
        }
      } catch (cause) {
        if (isAbort(cause)) throw cause;
        /* keep planCount at 0 and report honestly */
      }
    }

    const missionStatus = String(mission?.status ?? "").toUpperCase();
    const missionCompleted = missionStatus === "COMPLETED" || missionStatus === "SUCCEEDED";
    let status: string;
    if (baselinePlanIds === null) {
      status = "BASELINE_UNAVAILABLE";
      planCount = -1;
      newPlanIds = [];
    } else if (planCount > 0 && missionCompleted) {
      status = "COMPLETED";
    } else if (planCount > 0) {
      status = "PLANS_WITHOUT_AI";
    } else if (blockedCode) {
      status = `BLOCKED:${blockedCode}`;
    } else if (missionStatus.includes("EXTERNAL") || missionStatus.includes("SKIPPED")) {
      status = "SKIPPED_EXTERNAL_DEPENDENCY";
    } else {
      status = "NO_PLANS_PRODUCED";
    }
    const units = unitsFromRows(
      status === "COMPLETED" || status === "PLANS_WITHOUT_AI" ? composedRows : null,
      newPlanIds,
    );
    return settle({
      status,
      newPlanIds,
      reasonCode: blockedReasonCode,
      units,
      failureReason: mission?.failure_reason
        ? {
            text: mission.failure_reason,
            from: "server" as const,
            field: "mission.failure_reason",
          }
        : blockedSentence,
      summary: mission?.reasoning_summary ?? null,
      summaryOrigin: summaryOriginOf(mission ?? null),
      rawReading,
      planCount,
    });
  } catch (cause) {
    if (isKeylessWriteDeadlineMissed(cause)) {
      return settle({
        status: "UNKNOWN_KEYLESS_WRITE_DEADLINE",
        failureReason: keylessWriteUnknownReading(cause.method, cause.path, cause.deadlineMs),
        summary: null,
        planCount: -1,
        newPlanIds: [],
      });
    }
    if (isAbort(cause)) {
      return settle({
        status: reached === "baseline" ? "CANCELLED" : "CANCELLED_AFTER_SEND",
        failureReason: null,
        summary: null,
        planCount: -1,
        newPlanIds: [],
      });
    }
    return settle({
      status: "NETWORK",
      failureReason: failureNote(null, "no readable answer reached this device (network or origin)"),
      summary: null,
    });
  }
}
