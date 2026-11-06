import { useQuery } from "@tanstack/react-query";
import {
  classifyContinuation,
  type ContinuationState,
  type AgentFeedEvent,
  type AgentFeedPayload,
} from "./journeyContinuation";

/**
 * ORD-0269 cell 1 — the continuation detection surface, read-only.
 *
 * The feed is already being fetched on journey load (main.tsx) and by candidateRejections.ts with
 * limit=200; this hook reuses the SAME call shape so no new route or extra request appears.  The
 * limit is load-bearing (R-6): the feed route orders ascending and caps at 200, so a smaller page
 * can silently exclude the marker's mission — this hook reports that as `unconfirmable`, never as
 * an absence.
 */

const FEED_LIMIT = 200;

async function fetchFeed(journeyId: string, signal: AbortSignal): Promise<AgentFeedPayload> {
  const response = await fetch(
    `/api/v1/journeys/${journeyId}/agent-feed?limit=${FEED_LIMIT}`,
    { headers: { Accept: "application/json" }, signal },
  );
  if (!response.ok) {
    throw new Error(`agent-feed -> HTTP ${response.status}`);
  }
  return (await response.json()) as AgentFeedPayload;
}

export function useContinuationState(journeyId: string | null): {
  state: ContinuationState;
  feedReadFailed: boolean;
} {
  const query = useQuery<AgentFeedPayload, Error>({
    queryKey: ["ord0269-continuation-feed", journeyId],
    enabled: Boolean(journeyId),
    retry: false,
    queryFn: ({ signal }) => fetchFeed(journeyId as string, signal),
    /* The marker is device-local and cheap to re-join; the feed read is the expensive part and is
       already cached by the key above.  No polling: a continuation is revealed, not chased. */
    staleTime: 30_000,
  });

  if (!journeyId) return { state: { kind: "none" }, feedReadFailed: false };
  if (query.isError) {
    return { state: { kind: "unconfirmable", reason: "feed_unreadable" }, feedReadFailed: true };
  }
  if (query.isLoading || query.data === undefined) {
    return { state: { kind: "none" }, feedReadFailed: false };
  }
  const events = (query.data as { events?: AgentFeedEvent[] } | null) ?? null;
  /* Forward what the origin actually answered.  Substituting `[]` here made every body without an
     events array look like "this page does not contain the round", which is a different claim than
     the one the screen makes -- and it put the classifier's malformed guard out of reach of any
     caller.  The join below decides which named state it is. */
  return {
    state: classifyContinuation(journeyId, events),
    feedReadFailed: false,
  };
}
