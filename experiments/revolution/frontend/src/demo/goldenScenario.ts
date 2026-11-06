import {
  cityNexusApi,
  type Activity,
  type AgentToolResponse,
  type PlanVersion,
} from "../api/citynexus";
import type { JourneyTwin } from "../contracts/journeyTwin";
import type { ReplanProposal } from "../contracts/replan";

export interface GoldenScenarioSession {
  travelerId: string;
  cityCode: string;
  journeyId: string;
  plan: PlanVersion;
  techArt: Activity | null;
  riverCruise: Activity;
  twin: JourneyTwin;
}

function scenarioActivities(plan: PlanVersion): {
  techArt: Activity | null;
  riverCruise: Activity;
} {
  const riverCruise = plan.activities.find((item) => item.place_id === "place-river-cruise");
  if (!riverCruise) throw new Error("Golden Scenario River Cruise activity is missing");
  return {
    riverCruise,
    techArt: plan.activities.find((item) => item.place_id === "place-tech-art") ?? null,
  };
}

export async function bootstrapGoldenScenario(): Promise<GoldenScenarioSession> {
  const ids = await cityNexusApi.resetDemo();
  const plans = await cityNexusApi.generatePlans(ids.journey_id, 1);
  const plan = plans[0];
  if (!plan) throw new Error("No deterministic demo plan was generated");
  await cityNexusApi.selectPlan(ids.journey_id, plan.id);

  const { techArt, riverCruise } = scenarioActivities(plan);
  if (!techArt) throw new Error("Golden Scenario Tech Art activity is missing");
  const booking = await cityNexusApi.createBooking(ids.journey_id, riverCruise.id);
  await cityNexusApi.captureBooking(
    ids.journey_id,
    booking,
    "frontend-golden-capture-" + booking.id,
  );
  let twin = await cityNexusApi.startJourney(ids.journey_id);

  // Put the deterministic demo at the actual disruption point. A closure at
  // 13:25 is not a meaningful continuity demo if the pointer is still at 09:00.
  for (const activity of plan.activities) {
    if (activity.id === techArt.id) break;
    if (twin.current_activity_id !== activity.id) {
      throw new Error("Journey execution pointer drifted before the Tech Art disruption");
    }
    twin = await cityNexusApi.completeActivity(ids.journey_id, activity.id);
  }
  if (twin.current_activity_id !== techArt.id) {
    throw new Error("Golden Scenario did not progress to Tech Art before disruption");
  }
  return {
    travelerId: ids.traveler_id,
    cityCode: ids.city_code,
    journeyId: ids.journey_id,
    plan,
    techArt,
    riverCruise,
    twin,
  };
}

export async function reconnectGoldenScenario(
  journeyId: string,
): Promise<GoldenScenarioSession> {
  const twin = await cityNexusApi.getTwin(journeyId);
  const journey = await cityNexusApi.getJourney(journeyId);
  if (!twin.active_plan_version_id) throw new Error("JourneyTwin has no active plan");
  const plan = await cityNexusApi.getPlan(twin.active_plan_version_id);
  const { techArt, riverCruise } = scenarioActivities(plan);
  return {
    travelerId: journey.traveler_id,
    cityCode: journey.city_code,
    journeyId,
    plan,
    techArt,
    riverCruise,
    twin,
  };
}

export async function injectClosureAndLoadCandidates(
  session: GoldenScenarioSession,
  occurredAt = "2026-10-10T13:25:00+08:00",
): Promise<{ proposals: ReplanProposal[]; eventId: string; agentRequest: AgentToolResponse }> {
  if (!session.techArt) throw new Error("Tech Art activity is no longer on the active plan");
  const event = await cityNexusApi.createClosureEvent(
    session.journeyId,
    session.techArt.id,
    occurredAt,
  );
  const agentRequest = await cityNexusApi.callAgentTool("request_replan", {
    request_id: "frontend-agent-request-" + event.id,
    agent_id: "frontend-agent",
    journey_id: session.journeyId,
    trigger_event_id: event.id,
    candidate_count: 3,
  });
  const all = await cityNexusApi.listReplans(session.journeyId);
  return {
    eventId: event.id,
    agentRequest,
    proposals: all.filter(
      (item) => item.trigger_event_id === event.id && item.status === "PRESENTED",
    ),
  };
}
