import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { rejectionWithFacts } from "../lib/rejectionFacts";
import type { components, paths } from "../generated/api";

export type AuthoritativeComposeRequest =
  components["schemas"]["AuthoritativeComposeRequest"];
export type AuthoritativeComposeResponse =
  components["schemas"]["AuthoritativeComposeResponse"];
export type PlanVersion = components["schemas"]["PlanVersion"];
export type TripStudioPreviewRequest =
  components["schemas"]["TripStudioPreviewRequest"];
export type TripStudioPreview = components["schemas"]["TripStudioPreview"];
export type TripStudioAcceptRequest =
  components["schemas"]["TripStudioAcceptRequest"];
export type TripStudioAcceptResponse =
  components["schemas"]["TripStudioAcceptResponse"];
export type JourneyTwin = components["schemas"]["JourneyTwin"];

type PreviewResponse = paths["/journeys/{journey_id}/trip-studio/preview"]["post"]["responses"][200]["content"]["application/json"];

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    /* ORD-0369: this used to be `throw new Error(body?.detail ?? body?.error?.message ?? ...)`, so
       the backend's sentence travelled as `error.message` and the panel that rendered it put the
       server's prose on the traveller's line. The refusal now travels as what this device measured
       (`lib/rejectionFacts`), and the text is kept only to match machine tokens against. */
    throw await rejectionWithFacts(response);
  }
  return (await response.json()) as T;
}

export function useAuthoritativePlans(journeyId: string | null) {
  return useQuery({
    queryKey: ["v12-authoritative-plans", journeyId],
    enabled: Boolean(journeyId),
    queryFn: () => request<PlanVersion[]>(`/journeys/${journeyId}/plans`),
  });
}

export function useAuthoritativeTwin(journeyId: string | null) {
  return useQuery({
    queryKey: ["v12-authoritative-twin", journeyId],
    enabled: Boolean(journeyId),
    queryFn: () => request<JourneyTwin>(`/journeys/${journeyId}/twin`),
    refetchInterval: 15_000,
  });
}

export function useTripStudioPreview(journeyId: string | null) {
  return useMutation<PreviewResponse, Error, TripStudioPreviewRequest>({
    mutationFn: (payload) =>
      request<PreviewResponse>(`/journeys/${journeyId}/trip-studio/preview`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
  });
}

export function useAcceptTripStudioEdit(journeyId: string | null) {
  const queryClient = useQueryClient();
  return useMutation<TripStudioAcceptResponse, Error, TripStudioAcceptRequest>({
    mutationFn: (payload) =>
      request<TripStudioAcceptResponse>(`/journeys/${journeyId}/trip-studio/accept`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["v12-authoritative-plans", journeyId] });
    },
  });
}

export async function composeAuthoritativeJourney(
  journeyId: string,
  payload: AuthoritativeComposeRequest,
): Promise<AuthoritativeComposeResponse> {
  return request<AuthoritativeComposeResponse>(
    `/journeys/${journeyId}/plans/authoritative-compose`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export async function transitionActivity(
  journeyId: string,
  activityId: string,
  transition: "depart" | "arrive" | "begin" | "complete" | "skip",
): Promise<JourneyTwin> {
  return request<JourneyTwin>(
    `/journeys/${journeyId}/activities/${activityId}/${transition}`,
    { method: "POST" },
  );
}
