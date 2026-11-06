import type { paths } from "../generated/api";
import { useQuery } from "@tanstack/react-query";

// Keep the generated OpenAPI surface in the V10 client boundary.  The hand-authored
// runtime payloads below cover the newer endpoints while the contract is regenerated
// in CI whenever contracts/openapi.yaml changes.
export type GeneratedOpenApiPaths = paths;

export type ExecutionClockResponse = paths["/journeys/{journey_id}/execution-clock"]["get"]["responses"][200]["content"]["application/json"];

export type WebObservationPayload = {
  id: string;
  query: string;
  url: string;
  domain: string;
  title: string;
  source_class: string;
  provider_key: string;
  model: string;
  captured_at: string;
  evidence_fingerprint: string;
  authority_status: "NON_AUTHORITATIVE";
};

export async function fetchExecutionClock(journeyId: string): Promise<ExecutionClockResponse> {
  const response = await fetch(`/api/v1/journeys/${journeyId}/execution-clock`);
  if (!response.ok) throw new Error("Execution clock unavailable");
  return response.json() as Promise<ExecutionClockResponse>;
}

export async function fetchWebObservations(journeyId: string): Promise<{ observations: WebObservationPayload[] }> {
  const response = await fetch(`/api/v1/journeys/${journeyId}/web-observations`);
  if (!response.ok) throw new Error("Web evidence unavailable");
  return response.json() as Promise<{ observations: WebObservationPayload[] }>;
}

export function useExecutionClock(journeyId: string) {
  return useQuery({
    queryKey: ["execution-clock", journeyId],
    queryFn: () => fetchExecutionClock(journeyId),
    refetchInterval: 30_000,
  });
}

export function useWebObservations(journeyId: string) {
  return useQuery({
    queryKey: ["web-observations", journeyId],
    queryFn: () => fetchWebObservations(journeyId),
  });
}
