import { http, HttpResponse } from "msw";

export const v12ExecutionStates = [
  "NOT_STARTED",
  "EN_ROUTE",
  "ARRIVED",
  "IN_PROGRESS",
  "ACTIVE",
  "COMPLETED",
  "SKIPPED",
] as const;

export const handlers = [
  http.get("/api/v1/journeys/:journeyId/twin", ({ params }) =>
    HttpResponse.json({
      journey_id: params.journeyId,
      active_plan_version_id: "00000000-0000-4000-8000-000000000012",
      current_activity_id: "00000000-0000-4000-8000-000000000013",
      projection_status: "READY",
      context: {
        execution: {
          now_state: "IN_PROGRESS",
          next_state: "NOT_STARTED",
        },
      },
    }),
  ),
];
