import { useQuery, type QueryClient } from "@tanstack/react-query";

/**
 * ORD-0059 (a) -- the one place the frontend learns "what time is it for this journey", and it
 * learns it from the server.
 *
 * There is no `Date.now()` in this file, on purpose.  Today used to read the wall clock while
 * Live read the cursor, which is two answers to one question; the moment a Simulation entry
 * appears, a third invented clock would be the worst of the three, because a traveller could not
 * tell which screen was lying.  `GET /api/v1/demo/scene` is the single source -- including the
 * answer "there is no scenario", which the server gives as nulls plus `evidence_state`
 * "NO_SCENARIO" rather than as today's date.  Reporting an unknown scene as "now" is the
 * unknown-as-known failure this project keeps being charged for, so nothing here fills it in.
 *
 * The shape is validated, not assumed: the live envelope carries ten keys (`plan_version_id`
 * included).  A response this code does not recognise is reported as *unavailable*, never as a
 * half-populated scene.
 */

export type SceneState = {
  demoMode: boolean;
  journeyId: string | null;
  journeyStatus: string | null;
  sceneStartAt: string | null;
  sceneEndAt: string | null;
  effectiveNow: string | null;
  basis: string | null;
  evidenceExpiresAt: string | null;
  evidenceState: string | null;
  planVersionId: string | null;
};

export type ScenePhase = "loading" | "ready" | "unavailable";

export type SceneRead = {
  phase: ScenePhase;
  scene: SceneState | null;
  /** Why the scene could not be read, in the server's own words or a named code. */
  reason: string | null;
  /** ORD-0086: when a reading actually landed (`null` = none yet).  A refusal that was only true of
      an older reading has to be retireable, and that needs to know a newer one arrived. */
  updatedAt: number | null;
  refresh: () => void;
};

export class SceneReadError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string) {
    super(`demo/scene failed (${status}): ${code}`);
    this.status = status;
    this.code = code;
  }
}

export const SCENE_QUERY_KEY = ["demo-scene"] as const;

const NULLABLE_KEYS = [
  "journey_id",
  "journey_status",
  "scene_start_at",
  "scene_end_at",
  "effective_now",
  "basis",
  "evidence_expires_at",
  "evidence_state",
  "plan_version_id",
] as const;

function rejectReason(body: unknown, status: number): string {
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    const error = record.error as Record<string, unknown> | undefined;
    const code = error?.code ?? record.detail;
    if (typeof code === "string" && code.trim()) return code;
  }
  return `HTTP ${status}`;
}

/** Strict on purpose: an absent or mistyped key means this is not the contract we consume. */
export function parseSceneEnvelope(value: unknown): SceneState | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  if (typeof raw.demo_mode !== "boolean") return null;
  const read: Record<(typeof NULLABLE_KEYS)[number], string | null> = {
    journey_id: null,
    journey_status: null,
    scene_start_at: null,
    scene_end_at: null,
    effective_now: null,
    basis: null,
    evidence_expires_at: null,
    evidence_state: null,
    plan_version_id: null,
  };
  for (const key of NULLABLE_KEYS) {
    const value = raw[key];
    if (value !== null && typeof value !== "string") return null;
    read[key] = value === null ? null : (value as string);
  }
  return {
    demoMode: raw.demo_mode,
    journeyId: read.journey_id,
    journeyStatus: read.journey_status,
    sceneStartAt: read.scene_start_at,
    sceneEndAt: read.scene_end_at,
    effectiveNow: read.effective_now,
    basis: read.basis,
    evidenceExpiresAt: read.evidence_expires_at,
    evidenceState: read.evidence_state,
    planVersionId: read.plan_version_id,
  };
}

export async function readDemoScene(signal?: AbortSignal): Promise<SceneState> {
  const response = await fetch("/api/v1/demo/scene", {
    headers: { Accept: "application/json" },
    signal,
  });
  /* `fetch` does not reject on 5xx, and a refused read drawn as an empty scene is the exact
     defect ORD-0070 was opened for -- so the status is checked before the body is trusted. */
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) throw new SceneReadError(response.status, rejectReason(body, response.status));
  const parsed = parseSceneEnvelope(body);
  if (!parsed) throw new SceneReadError(response.status, "SCENE_ENVELOPE_UNRECOGNISED");
  return parsed;
}

export function useDemoScene(): SceneRead {
  const query = useQuery({
    queryKey: SCENE_QUERY_KEY,
    queryFn: ({ signal }) => readDemoScene(signal),
    retry: false,
  });
  const updatedAt = query.dataUpdatedAt || null;
  if (query.isPending)
    return {
      phase: "loading",
      scene: null,
      reason: null,
      updatedAt,
      refresh: () => void query.refetch(),
    };
  if (query.isError) {
    const error = query.error;
    return {
      phase: "unavailable",
      scene: null,
      reason: error instanceof SceneReadError ? error.code : "SCENE_READ_FAILED",
      updatedAt,
      refresh: () => void query.refetch(),
    };
  }
  return {
    phase: "ready",
    scene: query.data ?? null,
    reason: null,
    updatedAt,
    refresh: () => void query.refetch(),
  };
}

export function invalidateScene(client: QueryClient): void {
  void client.invalidateQueries({ queryKey: SCENE_QUERY_KEY });
}

/** The server's words, mapped to what a traveller can act on.  Unknown stays unknown. */
export function basisWord(basis: string | null, zh: boolean): string {
  if (basis === "CURSOR") return zh ? "执行游标" : "the execution cursor";
  if (basis === "JOURNEY_START") return zh ? "行程起点" : "the journey's start";
  return zh ? "服务器没有说明依据" : "the server gave no basis";
}

export function evidenceWord(state: string | null, zh: boolean): string {
  if (state === "REFERENCED")
    return zh ? "这批证据带冻结期" : "the frozen evidence carries an expiry";
  /* The state `924461e` added, and the one a naive reading gets wrong: sealed pairs exist and are
     used by a replan -- only the expiry is missing.  Showing this as "没有数据" tells a traveller
     the trip has no travel data when the server said it has 56 pairs. */
  if (state === "FROZEN_WITHOUT_TTL")
    return zh
      ? "这趟的出行数据是封存的，没有到期时间"
      : "this trip's travel data is sealed, with no expiry";
  if (state === "EVIDENCE_REFERENCE_INCONSISTENT")
    return zh
      ? "证据的引用对不上（服务器说有封存记录，但引用不一致）"
      : "the evidence reference does not line up (sealed rows exist, but the reference is inconsistent)";
  if (state === "NO_FROZEN_EVIDENCE")
    return zh ? "这趟没有冻结证据" : "no frozen evidence on this trip";
  if (state === "NO_SCENARIO") return zh ? "台上没有场景" : "no scenario on stage";
  return zh ? `服务器报的是 ${state}，我们不替它解释` : `the server reported ${state}; we do not interpret it`;
}

/**
 * Sliced, never parsed: running the instant through `new Date()` and the browser's zone would
 * put a second clock back into the display path.  The offset the server sent is part of the fact.
 */
export function sceneInstant(iso: string | null): string {
  if (!iso) return "—";
  const body = iso.slice(0, 16).replace("T", " ");
  const offset = iso.match(/(?:Z|[+-]\d{2}:\d{2})$/)?.[0];
  return offset ? `${body} ${offset}` : body;
}
