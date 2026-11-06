import { useQuery } from "@tanstack/react-query";

/**
 * ORD-0232: a candidate the feasibility gate drops now leaves a durable audit fact, and this module
 * is the only place that reads it for the traveller-facing screen.
 *
 * Shape comes from B's contract (`agent_feed_rejection_contract_for_D.json`, blob
 * 30c721d0716b32028a6a1e7a9a3080fd71275d68), read off a real response:
 *   GET /journeys/{id}/agent-feed?limit=200 -> { events, journey_id, source }
 *   one event per dropped candidate, `event_type === "plans.candidate_rejected"`,
 *   `facts.failures[]` entries are `"<RULE>_ACCESS_UNAVAILABLE:<place_id>"`.
 *
 * Deliberately free of prose: this file turns bytes into facts.  What a fact *says* belongs to the
 * screen and its i18n dictionary, so the gate can hold one literal copy and compare it exactly.
 */

export const CANDIDATE_REJECTION_EVENT = "plans.candidate_rejected";

/** One dropped candidate, as far as the evidence lets us know it. */
export type DroppedCandidate = {
  /** The audit row's own id.  `facts.candidate_sequence` is a template index, not a key. */
  key: string;
  /** e.g. "STROLLER_ACCESS_UNAVAILABLE"; null when the row does not carry a parseable code. */
  ruleCode: string | null;
  placeId: string | null;
  /** null means "the places directory did not answer for this id" -- never a fallback to the raw id. */
  placeName: string | null;
};

type AgentFeedEvent = {
  id?: unknown;
  event_type?: unknown;
  facts?: { failures?: unknown } | null;
};

async function request<T>(path: string): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    headers: { "Content-Type": "application/json" },
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }
  return body as T;
}

/**
 * `"<CODE>:<place_id>"`, split at the **first** colon only: a code is `[A-Z0-9_]+` and an id may
 * itself contain one, so `partition`-style splitting is the shape that cannot silently mis-parse.
 */
export function parseFailure(failure: unknown): { ruleCode: string | null; placeId: string | null } {
  const text = typeof failure === "string" ? failure.trim() : "";
  if (!text) return { ruleCode: null, placeId: null };
  const separator = text.indexOf(":");
  if (separator < 0) return { ruleCode: text, placeId: null };
  const code = text.slice(0, separator).trim();
  const id = text.slice(separator + 1).trim();
  return { ruleCode: code || null, placeId: id || null };
}

export function readRejections(events: unknown): DroppedCandidate[] {
  const list = Array.isArray(events) ? (events as AgentFeedEvent[]) : [];
  const rows: DroppedCandidate[] = [];
  list.forEach((event, index) => {
    if (String(event?.event_type ?? "") !== CANDIDATE_REJECTION_EVENT) return;
    const failures = Array.isArray(event?.facts?.failures)
      ? (event.facts?.failures as unknown[])
      : [];
    const { ruleCode, placeId } = parseFailure(failures[0]);
    const id = typeof event?.id === "string" && event.id ? event.id : `row-${index}`;
    rows.push({ key: id, ruleCode, placeId, placeName: null });
  });
  return rows;
}

/**
 * Feed + directory in one query.  The directory is only asked for when a rejection actually names a
 * place, so the ordinary screen pays for exactly one extra request.
 */
export function useCandidateRejections(
  journeyId: string | null,
  cityCode: string | null,
) {
  return useQuery({
    queryKey: ["candidate-rejections", journeyId, cityCode],
    enabled: Boolean(journeyId && cityCode),
    queryFn: async (): Promise<DroppedCandidate[]> => {
      const feed = await request<{ events?: unknown }>(
        `/journeys/${journeyId}/agent-feed?limit=200`,
      );
      const rows = readRejections(feed?.events);
      const wanted = new Set(
        rows.map((row) => row.placeId).filter((id): id is string => Boolean(id)),
      );
      if (!wanted.size) return rows;
      const places = await request<unknown>(
        `/places?city_code=${encodeURIComponent(String(cityCode))}`,
      );
      const names = new Map<string, string>();
      (Array.isArray(places) ? places : []).forEach((place) => {
        const record = place as { id?: unknown; name?: unknown };
        if (typeof record?.id === "string" && typeof record?.name === "string" && record.name) {
          names.set(record.id, record.name);
        }
      });
      return rows.map((row) => ({
        ...row,
        placeName: row.placeId ? (names.get(row.placeId) ?? null) : null,
      }));
    },
  });
}
