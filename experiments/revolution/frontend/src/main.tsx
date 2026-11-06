import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { createActor } from "xstate";
import { citynexusQueryClient } from "./lib/queryClient";
import { formatMinorMoney } from "./lib/money";
import { hasNonIdempotentRequestInFlight, onNonIdempotentRequestsQuiet } from "./lib/inFlightWrites";
import { JOURNEY_ID_SEGMENT, v8FirstSegment } from "./lib/journeyLink";
import "./v10.css";
import {
  CityNexusApiError,
  cityNexusApi,
  type Activity,
  type AcceptReplanResponse,
  type AgentFeedEntry,
  type AgentMission,
  type AgentPolicy,
  type AgentRun,
  type GuardianStatus,
  type ProviderMeshResponse,
  type PlaceRecord,
  type PlanVersion,
  type ResilienceSnapshot,
  type ReflectSummary,
  type RouteEvidence,
  type TravelerTwinEvidence,
} from "./api/citynexus";
import type { Commitment, JourneyTwin } from "./contracts/journeyTwin";
import type { ReplanProposal } from "./contracts/replan";
import { testIds } from "./contracts/testIds";
import {
  bootstrapGoldenScenario,
  injectClosureAndLoadCandidates,
  reconnectGoldenScenario,
  type GoldenScenarioSession,
} from "./demo/goldenScenario";
import { journeyModeMachine } from "./state/journeyModeMachine";
import { V8Experience } from "./app/V8Experience";
import { ProductErrorBoundary } from "./components/ProductErrorBoundary";
import { LocaleProvider } from "./i18n/LocaleContext";
import { applyTheme, followSystemTheme } from "./theme";
import "./styles.css";
import "./design-tokens.css";

/* Resolve the theme root contract before the first paint.  The stylesheets key their dark
   tables off `:root[data-theme]`, so the attribute must exist by the time anything is
   painted; doing it here rather than in a component keeps the first render correct and
   avoids a second copy of the OS check. */
applyTheme();
followSystemTheme();

const journeyStorageKey = "citynexus.demo.journey_id";
const agentRunStorageKey = "citynexus.demo.agent_run_id";
const closureOccurredAt = "2026-10-10T13:25:00+08:00";
const defaultAgentCommand =
  "下午海河沿线有交通管制，我不想淋雨，游船和晚餐别动，最多多花50元。你自己重新安排。";

// Keep MapLibre and the recovery map out of the non-map first paint. The
// route chunk is loaded only when this legacy recovery surface renders.
const JourneyMap = lazy(() =>
  import("./components/JourneyMap").then(({ JourneyMap: component }) => ({ default: component })),
);

function preferCompletedPredictiveRun(status: GuardianStatus): GuardianStatus {
  const predictive = status.runs.find(
    (run) => run.trigger_kind === "PREDICTIVE" && run.status === "COMPLETED",
  );
  return predictive ? { ...status, latest: predictive } : status;
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function guardianWatchCopy(state: string | undefined): string {
  switch (state) {
    case "INCIDENT_ACTIVE":
      return "A live issue needs closer attention, so Guardian is checking more often.";
    case "APPROACHING":
      return "Your next activity is within two hours, so Guardian is checking more often.";
    case "TODAY":
      return "You have an activity later today, so Guardian is keeping a closer watch.";
    case "DAY_AHEAD":
      return "An activity is planned for tomorrow, so Guardian is checking periodically.";
    default:
      return "Nothing important is near, so Guardian is checking at a calmer pace.";
  }
}

function formatAgentOutcome(outcome: AgentRun["outcome"]): string {
  if (!outcome) return "";
  if (typeof outcome === "string") return outcome;
  const parts = [
    outcome.status,
    outcome.reason_code,
    outcome.user_summary,
    outcome.recommended_action ? `Next: ${outcome.recommended_action}` : null,
  ].filter((part): part is string => Boolean(part));
  return parts.join(" · ");
}

function formatMoney(amountMinor: number, currency: string): string {
  return formatMinorMoney(amountMinor, currency, "zh-CN");
}

function activityForId(plan: PlanVersion | null, activityId: string | null): Activity | null {
  if (!plan || !activityId) return null;
  return plan.activities.find((item) => item.id === activityId) ?? null;
}

function commitmentActivity(
  plan: PlanVersion | null,
  commitment: Commitment,
): Activity | null {
  return activityForId(plan, commitment.activity_id);
}

function semanticTitle(key: string, plan: PlanVersion | null): string {
  const activity = plan?.activities.find(
    (item) => item.place_id === key || item.city_service_id === key,
  );
  return activity?.title ?? key;
}

function proposalDiffText(proposal: ReplanProposal, plan: PlanVersion): string {
  const removed = proposal.diff.removed.map((key) => semanticTitle(key, plan));
  const added = proposal.diff.added.map((key) => semanticTitle(key, plan));
  const preserved = proposal.diff.preserved_protected.map((key) => semanticTitle(key, plan));
  return (
    "Removed " +
    (removed.join(", ") || "none") +
    " · added " +
    (added.join(", ") || "none") +
    " · protected preserved " +
    (preserved.join(", ") || "none")
  );
}

function proposalDeltaText(proposal: ReplanProposal, currency: string): string {
  const diff = proposal.diff;
  return (
    "Cost Δ " +
    formatMoney(diff.cost_delta_minor, currency) +
    " · travel Δ " +
    (diff.travel_delta_min >= 0 ? "+" : "") +
    diff.travel_delta_min +
    " min · end Δ " +
    (diff.end_time_delta_min >= 0 ? "+" : "") +
    diff.end_time_delta_min +
    " min"
  );
}

/** Q64=1: a screen may say only what this device measured, in this device's own words.  The
 *  server's sentence and the exception's message are both out -- and Q71 closes the side door that
 *  used to make them look safe: moving them into a `data-*` puts them in the DOM anyway. */
type ErrorNote = { note: string; http_status: number | null };

function errorNote(error: unknown): ErrorNote {
  const raw = (error as { status?: unknown } | null)?.status;
  const status =
    error instanceof CityNexusApiError
      ? error.status
      : typeof raw === "number"
        ? raw
        : null;
  return {
    note: status === null
      ? "CityNexus request failed"
      : `CityNexus request refused (HTTP ${status})`,
    http_status: status,
  };
}

function LegacyApp() {
  const actor = useMemo(() => createActor(journeyModeMachine), []);
  const [workflowState, setWorkflowState] = useState(() =>
    String(actor.getSnapshot().value),
  );
  const [session, setSession] = useState<GoldenScenarioSession | null>(null);
  const [places, setPlaces] = useState<PlaceRecord[]>([]);
  const [agentPolicy, setAgentPolicy] = useState<AgentPolicy | null>(null);
  const agentPolicyRef = useRef<AgentPolicy | null>(null);
  const policyUpdateChain = useRef(Promise.resolve());
  const [agentFeed, setAgentFeed] = useState<AgentFeedEntry[]>([]);
  const [agentCommand, setAgentCommand] = useState(defaultAgentCommand);
  const [requestedAutonomy, setRequestedAutonomy] = useState<
    "PLAN_ONLY" | "EXECUTE_WITHIN_BOUNDS"
  >("EXECUTE_WITHIN_BOUNDS");
  const [mission, setMission] = useState<AgentMission | null>(null);
  const [agentRun, setAgentRun] = useState<AgentRun | null>(null);
  const [guardianStatus, setGuardianStatus] = useState<GuardianStatus | null>(null);
  const [resilience, setResilience] = useState<ResilienceSnapshot | null>(null);
  const [providerMesh, setProviderMesh] = useState<ProviderMeshResponse | null>(null);
  const [serverDecision, setServerDecision] = useState<string | null>(null);
  const [confirmationRequired, setConfirmationRequired] = useState(false);
  const [approvalId, setApprovalId] = useState<string | null>(null);
  const [routeEvidence, setRouteEvidence] = useState<RouteEvidence | null>(null);
  const [reflectSummary, setReflectSummary] = useState<ReflectSummary | null>(null);
  const [travelerTwin, setTravelerTwin] = useState<TravelerTwinEvidence | null>(null);
  const [proposals, setProposals] = useState<ReplanProposal[]>([]);
  const [selectedProposalId, setSelectedProposalId] = useState<string | null>(null);
  const [eventId, setEventId] = useState<string | null>(null);
  const [simulatorOpen, setSimulatorOpen] = useState(false);
  const [replanRejected, setReplanRejected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ErrorNote | null>(null);
  const [lastAction, setLastAction] = useState<string | null>(null);
  const [liveOperations, setLiveOperations] = useState<
    Array<{ event_type: string; summary: string; timestamp: string }>
  >([]);

  useEffect(() => {
    const subscription = actor.subscribe((snapshot) => {
      setWorkflowState(String(snapshot.value));
    });
    actor.start();
    actor.send({ type: "BOOTSTRAP" });

    let cancelled = false;
    async function loadJourney() {
      setLoading(true);
      setError(null);
      const storedJourneyId = localStorage.getItem(journeyStorageKey);
      const storedAgentRunId = localStorage.getItem(agentRunStorageKey);
      try {
        let nextSession: GoldenScenarioSession;
        if (storedJourneyId) {
          try {
            nextSession = await reconnectGoldenScenario(storedJourneyId);
          } catch (reconnectError) {
            if (!(reconnectError instanceof CityNexusApiError) || reconnectError.status !== 404) {
              throw reconnectError;
            }
            localStorage.removeItem(journeyStorageKey);
            nextSession = await bootstrapGoldenScenario();
          }
        } else {
          nextSession = await bootstrapGoldenScenario();
        }
        if (cancelled) return;
        localStorage.setItem(journeyStorageKey, nextSession.journeyId);
        setSession(nextSession);
        const [nextPolicy, nextPlaces, nextFeed, nextProposals, nextEvents, nextReflect, nextGuardian, nextResilience, nextMesh, nextTravelerTwin] = await Promise.all([
          cityNexusApi.getAgentPolicy(nextSession.travelerId),
          cityNexusApi.listPlaces(nextSession.cityCode),
          cityNexusApi.getAgentFeed(nextSession.journeyId),
          cityNexusApi.listReplans(nextSession.journeyId),
          cityNexusApi.listEvents(nextSession.journeyId),
          cityNexusApi.getReflect(nextSession.journeyId),
          cityNexusApi.getGuardianStatus(nextSession.journeyId),
          cityNexusApi.getResilience(nextSession.journeyId),
          cityNexusApi.getProviderMesh(nextSession.journeyId),
          cityNexusApi.getTravelerTwin(nextSession.travelerId),
        ]);
        setAgentPolicy(nextPolicy);
        agentPolicyRef.current = nextPolicy;
        if (storedAgentRunId) {
          try {
            const persistedRun = await cityNexusApi.getAgentRun(
              nextSession.journeyId,
              storedAgentRunId,
            );
            setAgentRun(persistedRun);
            setMission(persistedRun.mission ?? null);
          } catch (runError) {
            if (runError instanceof CityNexusApiError && runError.status === 404) {
              localStorage.removeItem(agentRunStorageKey);
            } else {
              throw runError;
            }
          }
        }
        setPlaces(nextPlaces);
        setAgentFeed(nextFeed.events);
        setReflectSummary(nextReflect);
        setGuardianStatus(preferCompletedPredictiveRun(nextGuardian));
        setResilience(nextResilience);
        setProviderMesh(nextMesh);
        setTravelerTwin(nextTravelerTwin);
        const persistedProposals = nextProposals.filter(
          (item) => item.status === "PRESENTED" || item.status === "ACCEPTED",
        );
        setProposals(persistedProposals);
        setSelectedProposalId(persistedProposals[0]?.id ?? null);
        setEventId(nextEvents[nextEvents.length - 1]?.id ?? null);
        const latestDecision = [...nextFeed.events]
          .reverse()
          .find((item) => item.decision)?.decision;
        setServerDecision(latestDecision ?? null);
        setConfirmationRequired(
          nextFeed.events.some((item) => item.event_type === "replan_confirmation_required") &&
            !nextFeed.events.some((item) =>
              ["replan_auto_accepted", "replan_user_confirmed", "replan_user_approved"].includes(item.event_type),
            ),
        );
        const pendingApproval = [...nextFeed.events]
          .reverse()
          .find(
            (item) =>
              typeof item.facts.approval_id === "string" &&
              item.facts.approval_status !== "CONSUMED",
          );
        setApprovalId(
          pendingApproval && typeof pendingApproval.facts.approval_id === "string"
            ? pendingApproval.facts.approval_id
            : null,
        );
        if (persistedProposals[0]) {
          setRouteEvidence(
            await cityNexusApi.getRouteEvidence(
              nextSession.journeyId,
              persistedProposals[0].id,
            ),
          );
        } else {
          setRouteEvidence(await cityNexusApi.getRouteEvidence(nextSession.journeyId));
        }
        actor.send({ type: "BOOTSTRAP_OK" });
      } catch (loadError) {
        if (cancelled) return;
        setError(errorNote(loadError));
        actor.send({ type: "FAIL", message: errorNote(loadError).note });
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void loadJourney();

    return () => {
      cancelled = true;
      subscription.unsubscribe();
      actor.stop();
    };
  }, [actor]);

  const plan = session?.plan ?? null;
  const twin: JourneyTwin | null = session?.twin ?? null;
  const current = activityForId(plan, twin?.current_activity_id ?? null);
  const currentIndex = current
    ? plan?.activities.findIndex((item) => item.id === current.id) ?? -1
    : -1;
  const next = currentIndex >= 0 ? plan?.activities[currentIndex + 1] ?? null : null;
  const envelope = twin?.context.feasibility_envelope;
  const opportunity = twin?.context.opportunity_window;
  const degradedProviders = twin?.context.degraded_providers ?? [];
  const c4Commitment = twin?.upcoming_commitments.find(
    (item) => item.level === "C4_Anchored",
  );
  const c4Activity = c4Commitment ? commitmentActivity(plan, c4Commitment) : null;
  const selectedProposal =
    proposals.find((item) => item.id === selectedProposalId) ?? proposals[0] ?? null;
  const families = Array.from(
    new Set(proposals.map((item) => String(item.diff.candidate_family))),
  );
  const planCurrency = plan?.total_cost.currency ?? "CNY";
  const impactResolution = selectedProposal?.diff.verified_evidence?.impact_resolution as
    | {
        status?: string;
        maturity_stage?: string;
        selected_activity_id?: string | null;
        provider_key?: string;
        model_key?: string;
        reason_codes?: string[];
      }
    | undefined;

  async function refreshAuthoritativeFeed(journeyId = session?.journeyId) {
    if (!journeyId) return;
    const feed = await cityNexusApi.getAgentFeed(journeyId);
    setAgentFeed(feed.events);
    const pendingApproval = [...feed.events]
      .reverse()
      .find(
        (item) =>
          typeof item.facts.approval_id === "string" &&
          item.facts.approval_status !== "CONSUMED",
      );
    setApprovalId(
      pendingApproval && typeof pendingApproval.facts.approval_id === "string"
        ? pendingApproval.facts.approval_id
        : null,
    );
    setReflectSummary(await cityNexusApi.getReflect(journeyId));
    if (session?.travelerId) {
      setTravelerTwin(await cityNexusApi.getTravelerTwin(session.travelerId));
    }
  }

  async function clearLearnedTravelerTwin() {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      setTravelerTwin(await cityNexusApi.clearTravelerTwinLearned(session.travelerId));
      setLastAction("TravelerTwin learned soft evidence cleared · explicit preferences retained");
    } catch (twinError) {
      setError(errorNote(twinError));
    } finally {
      setBusy(false);
    }
  }

  async function loadRouteEvidence(
    journeyId = session?.journeyId,
    proposalId = selectedProposal?.id,
  ) {
    if (!journeyId) return;
    const evidence = await cityNexusApi.getRouteEvidence(journeyId, proposalId);
    setRouteEvidence(evidence);
  }

  async function refreshJourneyProjection(journeyId = session?.journeyId) {
    if (!journeyId || !session) return;
    const refreshedTwin = await cityNexusApi.getTwin(journeyId);
    const refreshedPlan = refreshedTwin.active_plan_version_id
      ? await cityNexusApi.getPlan(refreshedTwin.active_plan_version_id)
      : session.plan;
    const refreshedRiverCruise = refreshedPlan.activities.find(
      (item) => item.place_id === "place-river-cruise",
    );
    if (!refreshedRiverCruise) {
      throw new Error("Backend JourneyTwin has no protected River Cruise activity");
    }
    setSession({
      ...session,
      plan: refreshedPlan,
      techArt:
        refreshedPlan.activities.find((item) => item.place_id === "place-tech-art") ?? null,
      riverCruise: refreshedRiverCruise,
      twin: refreshedTwin,
    });
    return refreshedTwin;
  }

  async function refreshPersistedProposals(journeyId = session?.journeyId) {
    if (!journeyId) return [];
    const nextProposals = (await cityNexusApi.listReplans(journeyId)).filter(
      (item) => item.status === "PRESENTED" || item.status === "ACCEPTED",
    );
    setProposals(nextProposals);
    setSelectedProposalId(nextProposals[0]?.id ?? null);
    return nextProposals;
  }

  async function refreshAutopilotTruth(journeyId = session?.journeyId) {
    if (!journeyId) return null;
    const [nextStatus, nextEvents, nextResilience, nextMesh] = await Promise.all([
      cityNexusApi.getGuardianStatus(journeyId),
      cityNexusApi.listEvents(journeyId),
      cityNexusApi.getResilience(journeyId),
      cityNexusApi.getProviderMesh(journeyId),
    ]);
    setGuardianStatus(preferCompletedPredictiveRun(nextStatus));
    setResilience(nextResilience);
    setProviderMesh(nextMesh);
    setMission(nextStatus.standing_mission);
    setEventId(nextEvents[nextEvents.length - 1]?.id ?? null);
    await refreshJourneyProjection(journeyId);
    const nextProposals = await refreshPersistedProposals(journeyId);
    await refreshAuthoritativeFeed(journeyId);
    await loadRouteEvidence(journeyId, nextStatus.latest?.proposal_id ?? nextProposals[0]?.id);
    return nextStatus;
  }

  useEffect(() => {
    const journeyId = session?.journeyId;
    if (!journeyId) return;
    let active = true;
    let refreshInFlight: Promise<void> | null = null;
    let refreshQueued = false;
    let refreshTimer: ReturnType<typeof setTimeout> | null = null;
    let settleTimer: ReturnType<typeof setTimeout> | null = null;
    const source = new EventSource(
      "/api/v1/journeys/" + journeyId + "/operations/stream?timeout_seconds=30",
    );
    const eventTypes = [
      "city_signal_received",
      "incident_updated",
      "guardian_wake",
      "guardian_sleep",
      "mission_compiled",
      "agent_run_started",
      "agent_tool_called",
      "impact_resolved",
      "solver_candidates_ready",
      "policy_decision",
      "domain_mutation",
      "twin_refreshed",
      "replan_auto_accepted",
      "replan_confirmation_required",
    ];
    const truthRefreshEventTypes = new Set([
      "city_signal_received",
      "guardian_wake",
      "mission_compiled",
      "policy_decision",
      "domain_mutation",
      "twin_refreshed",
      "replan_auto_accepted",
      "replan_confirmation_required",
    ]);
    const scheduleTruthRefresh = () => {
      if (!active) return;
      if (refreshTimer !== null) clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => {
        refreshTimer = null;
        if (!active) return;
        if (refreshInFlight) {
          refreshQueued = true;
          return;
        }
        refreshInFlight = refreshAutopilotTruth(journeyId)
          .then((status) => {
            const latestState = status?.latest?.status;
            if (
              active &&
              (latestState === "PLANNING" || latestState === "RUNNING") &&
              settleTimer === null
            ) {
              settleTimer = setTimeout(() => {
                settleTimer = null;
                if (active) scheduleTruthRefresh();
              }, 250);
            }
          })
          .catch(() => undefined)
          .finally(() => {
            refreshInFlight = null;
            if (active && refreshQueued) {
              refreshQueued = false;
              scheduleTruthRefresh();
            }
        });
      }, 120);
    };
    const closeOperationsStream = () => {
      active = false;
      refreshQueued = false;
      if (refreshTimer !== null) {
        clearTimeout(refreshTimer);
        refreshTimer = null;
      }
      if (settleTimer !== null) {
        clearTimeout(settleTimer);
        settleTimer = null;
      }
      source.close();
    };
    const handleEvent = (event: Event) => {
      const message = event as MessageEvent<string>;
      let eventType: string | undefined;
      try {
        const payload = JSON.parse(message.data) as {
          event_type?: string;
          summary?: string;
          timestamp?: string;
        };
        eventType = payload.event_type;
        setLiveOperations((current) => [
          ...current.slice(-19),
          {
            event_type: payload.event_type ?? "operation",
            summary: payload.summary ?? "Persisted operation update",
            timestamp: payload.timestamp ?? new Date().toISOString(),
          },
        ]);
      } catch {
        // The stream's REST projection remains the source of truth.
      }
      if (eventType && truthRefreshEventTypes.has(eventType)) scheduleTruthRefresh();
    };
    for (const eventType of eventTypes) source.addEventListener(eventType, handleEvent);
    source.onmessage = handleEvent;
    source.onerror = () => source.close();
    window.addEventListener("pagehide", closeOperationsStream);
    return () => {
      window.removeEventListener("pagehide", closeOperationsStream);
      closeOperationsStream();
    };
  }, [session?.journeyId]);

  async function runAgentCommand() {
    if (!session || !agentCommand.trim()) return;
    setBusy(true);
    setError(null);
    setLastAction(null);
    try {
      const response = await cityNexusApi.runAgentCommand(
        session.journeyId,
        agentCommand.trim(),
        requestedAutonomy,
      );
      setMission(response.mission);
      setAgentRun(response.run);
      if (response.run.run_id) {
        localStorage.setItem(agentRunStorageKey, response.run.run_id);
      } else {
        localStorage.removeItem(agentRunStorageKey);
      }
      setEventId(response.event_id);
      setReplanRejected(false);
      const decisionStep = [...response.run.tool_steps]
        .reverse()
        .find((step) => Boolean(step.decision));
      setServerDecision(decisionStep?.decision ?? null);
      setConfirmationRequired(
        decisionStep?.decision === "REQUIRE_CONFIRMATION" ||
          decisionStep?.decision === "REQUIRE_STEP_UP",
      );
      setApprovalId(
        typeof decisionStep?.result.approval_id === "string"
          ? decisionStep.result.approval_id
          : null,
      );
      await refreshJourneyProjection(session.journeyId);
      const nextProposals = await refreshPersistedProposals(session.journeyId);
      await refreshAuthoritativeFeed(session.journeyId);
      await loadRouteEvidence(session.journeyId, nextProposals[0]?.id);
      setLastAction(
        "Agent run " + response.run.status + " · " + response.run.tool_steps.length + " tool calls persisted",
      );
    } catch (commandError) {
      setError(errorNote(commandError));
    } finally {
      setBusy(false);
    }
  }

  async function activateGuardianMission() {
    if (!session || !agentCommand.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const standing = await cityNexusApi.putGuardianMission(
        session.journeyId,
        agentCommand.trim(),
        requestedAutonomy,
      );
      setMission(standing);
      setGuardianStatus(await cityNexusApi.getGuardianStatus(session.journeyId));
      setLastAction("Standing mission activated · Guardian is watching persisted city signals");
    } catch (guardianError) {
      setError(errorNote(guardianError));
    } finally {
      setBusy(false);
    }
  }

  async function startLiveAutopilot() {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      if (!guardianStatus?.standing_mission) {
        const standing = await cityNexusApi.putGuardianMission(
          session.journeyId,
          agentCommand.trim(),
          requestedAutonomy,
        );
        setMission(standing);
      }
      await cityNexusApi.setGuardianProviderFixture(session.journeyId, "NORMAL");
      await cityNexusApi.startGuardianAutopilot(session.journeyId, 0.25);
      await refreshAutopilotTruth(session.journeyId);
      setLastAction("Live Journey Autopilot started · provider fixture is NORMAL");
    } catch (autopilotError) {
      setError(errorNote(autopilotError));
    } finally {
      setBusy(false);
    }
  }

  async function stopLiveAutopilot() {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      await cityNexusApi.stopGuardianAutopilot(session.journeyId);
      await refreshAutopilotTruth(session.journeyId);
      setLastAction("Guardian Watch stopped");
    } catch (autopilotError) {
      setError(errorNote(autopilotError));
    } finally {
      setBusy(false);
    }
  }

  async function transitionProviderFixture(state: "NORMAL" | "DISRUPTED" | "OUTAGE") {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      await cityNexusApi.setGuardianProviderFixture(session.journeyId, state, {
        scenario: state === "DISRUPTED" ? "heavy-rain-and-route-delay" : "demo-provider",
      });
      setLastAction("Provider fixture transitioned to " + state + " · waiting for server pulse");
    } catch (fixtureError) {
      setError(errorNote(fixtureError));
    } finally {
      setBusy(false);
    }
  }

  async function runPredictiveAutopilotScenario() {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      if (!guardianStatus?.standing_mission) {
        const standing = await cityNexusApi.putGuardianMission(
          session.journeyId,
          agentCommand.trim(),
          requestedAutonomy,
        );
        setMission(standing);
      }
      if (!guardianStatus?.autopilot?.enabled) {
        await cityNexusApi.startGuardianAutopilot(session.journeyId, 0.2);
      }
      await cityNexusApi.discoverRealCity(session.journeyId, "indoor", "replay");
      await cityNexusApi.setGuardianProviderFixture(session.journeyId, "RISK_FORECAST", {
        risk_score: 88,
        forecast_horizon_min: 45,
        expected_route_delay_min: 18,
        scenario: "tianjin-real-city-predictive-replay",
      });
      for (let attempt = 0; attempt < 40; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 150));
        const nextStatus = await cityNexusApi.getGuardianStatus(session.journeyId);
        setGuardianStatus(nextStatus);
        if (nextStatus.latest?.trigger_kind === "PREDICTIVE" && nextStatus.latest.status === "COMPLETED") {
          break;
        }
      }
      await refreshAutopilotTruth(session.journeyId);
      setLastAction("Predictive Real-City Twin · weakness detected and preventive recovery evaluated");
    } catch (predictiveError) {
      setError(errorNote(predictiveError));
    } finally {
      setBusy(false);
    }
  }

  async function runProactiveGuardianScenario() {
    if (!session?.techArt) return;
    setBusy(true);
    setError(null);
    try {
      if (!guardianStatus?.standing_mission) {
        await cityNexusApi.putGuardianMission(
          session.journeyId,
          agentCommand.trim(),
          requestedAutonomy,
        );
      }
      const weather = await cityNexusApi.createJourneyEvent(
        session.journeyId,
        "WEATHER",
        "MAJOR",
        session.techArt.id,
        closureOccurredAt,
        { description: "Heavy rain threatens Tech Art access", affected_place_id: session.techArt.place_id },
      );
      await cityNexusApi.createJourneyEvent(
        session.journeyId,
        "TRAFFIC_DELAY",
        "MINOR",
        session.riverCruise.id,
        "2026-10-10T13:26:00+08:00",
        { description: "River corridor congestion", extra_minutes: 20 },
      );
      setEventId(weather.id);
      const guardianRun = await cityNexusApi.scanGuardian(session.journeyId);
      const nextStatus = await cityNexusApi.getGuardianStatus(session.journeyId);
      setGuardianStatus(nextStatus);
      setMission(nextStatus.standing_mission);
      setServerDecision(
        String(guardianRun.evidence.policy?.decision ?? guardianRun.action_status),
      );
      setConfirmationRequired(guardianRun.action_status === "REQUIRE_CONFIRMATION");
      await refreshJourneyProjection(session.journeyId);
      const nextProposals = await refreshPersistedProposals(session.journeyId);
      await refreshAuthoritativeFeed(session.journeyId);
      await loadRouteEvidence(session.journeyId, guardianRun.proposal_id ?? nextProposals[0]?.id);
      setLastAction(
        "Guardian " + guardianRun.decision + " · " + guardianRun.action_status +
          " · persisted proactive run " + guardianRun.id.slice(0, 8),
      );
    } catch (guardianError) {
      setError(errorNote(guardianError));
    } finally {
      setBusy(false);
    }
  }

  async function applyAcceptedResponse(accepted: AcceptReplanResponse) {
    if (!session || !accepted.twin.active_plan_version_id) {
      throw new Error("Accepted backend response has no active plan");
    }
    const acceptedPlan = await cityNexusApi.getPlan(accepted.twin.active_plan_version_id);
    const riverCruise = acceptedPlan.activities.find(
      (item) => item.place_id === "place-river-cruise",
    );
    if (!riverCruise) throw new Error("Accepted plan lost the protected River Cruise activity");
    setSession({
      ...session,
      plan: acceptedPlan,
      techArt:
        acceptedPlan.activities.find((item) => item.place_id === "place-tech-art") ?? null,
      riverCruise,
      twin: accepted.twin,
    });
    setProposals((existing) =>
      existing.map((item) => (item.id === accepted.proposal.id ? accepted.proposal : item)),
    );
    setConfirmationRequired(false);
    setApprovalId(null);
    await refreshAuthoritativeFeed(session.journeyId);
    await loadRouteEvidence(session.journeyId, accepted.proposal.id);
    setLastAction("Activated PlanVersion " + accepted.twin.active_plan_version_id);
    actor.send({ type: "ACCEPTED" });
  }

  async function updateAgentPolicy(
    patch: Partial<Pick<
      AgentPolicy,
      | "mode"
      | "auto_accept_replan"
      | "max_auto_replan_cost_delta_minor"
      | "max_auto_loss_minor"
      | "max_auto_schedule_drift_min"
      | "allowed_activity_types"
      | "allowed_booking_types"
      | "protected_commitment_levels"
      | "allow_auto_commitment_management"
      | "allow_auto_when_provider_degraded"
      | "allow_auto_on_critical_event"
    >>,
  ) {
    const currentPolicy = agentPolicyRef.current ?? agentPolicy;
    if (!session || !currentPolicy) return;
    setError(null);
    const nextPolicy = { ...currentPolicy, ...patch };
    agentPolicyRef.current = nextPolicy;
    // Keep controlled inputs responsive while the authoritative PUT is in flight.
    // The server response below remains the source of truth after persistence.
    setAgentPolicy(nextPolicy);
    const request = policyUpdateChain.current.then(async () => {
      const updated = await cityNexusApi.updateAgentPolicy(session.travelerId, {
        mode: nextPolicy.mode,
        auto_accept_replan: nextPolicy.auto_accept_replan,
        max_auto_replan_cost_delta_minor: nextPolicy.max_auto_replan_cost_delta_minor,
        max_auto_loss_minor: nextPolicy.max_auto_loss_minor,
        max_auto_schedule_drift_min: nextPolicy.max_auto_schedule_drift_min,
        allowed_activity_types: nextPolicy.allowed_activity_types,
        allowed_booking_types: nextPolicy.allowed_booking_types,
        protected_commitment_levels: nextPolicy.protected_commitment_levels,
        allow_auto_commitment_management: nextPolicy.allow_auto_commitment_management,
        allow_auto_when_provider_degraded: nextPolicy.allow_auto_when_provider_degraded,
        allow_auto_on_critical_event: nextPolicy.allow_auto_on_critical_event,
      });
      if (agentPolicyRef.current === nextPolicy) {
        agentPolicyRef.current = updated;
        setAgentPolicy(updated);
      }
      await refreshAuthoritativeFeed(session.journeyId);
    });
    policyUpdateChain.current = request.catch(() => undefined);
    try {
      await request;
    } catch (policyError) {
      setError(errorNote(policyError));
    }
  }

  async function runClosure() {
    if (!session?.techArt) return;
    setBusy(true);
    setError(null);
    setLastAction(null);
    actor.send({ type: "EVENT_DETECTED" });
    try {
      const result = await injectClosureAndLoadCandidates(session, closureOccurredAt);
      const refreshedTwin = await cityNexusApi.getTwin(session.journeyId, closureOccurredAt);
      setSession({ ...session, twin: refreshedTwin });
      setEventId(result.eventId);
      setProposals(result.proposals);
      setSelectedProposalId(result.proposals[0]?.id ?? null);
      setReplanRejected(false);
      setServerDecision(result.agentRequest.decision);
      setConfirmationRequired(result.agentRequest.decision === "REQUIRE_CONFIRMATION");
      await refreshAuthoritativeFeed(session.journeyId);
      await loadRouteEvidence(session.journeyId, result.proposals[0]?.id);
      if (
        result.proposals[0] &&
        agentPolicy.mode !== "ADVISORY" &&
        agentPolicy.auto_accept_replan
      ) {
        const autoAccept = await cityNexusApi.callAgentTool("accept_replan", {
          request_id: "frontend-agent-auto-accept-" + result.proposals[0].id,
          agent_id: "frontend-agent",
          journey_id: session.journeyId,
          proposal_id: result.proposals[0].id,
        });
        setServerDecision(autoAccept.decision);
        setConfirmationRequired(
          autoAccept.decision === "REQUIRE_CONFIRMATION" ||
            autoAccept.decision === "REQUIRE_STEP_UP",
        );
        setApprovalId(autoAccept.approval_id ?? null);
        if (autoAccept.executed && autoAccept.result) {
          await applyAcceptedResponse(autoAccept.result);
        }
        await refreshAuthoritativeFeed(session.journeyId);
      }
      setLastAction("Closure event and backend replan proposals loaded");
      actor.send({ type: "REPLANS_READY" });
    } catch (runError) {
      setError(errorNote(runError));
      actor.send({ type: "FAIL", message: errorNote(runError).note });
    } finally {
      setBusy(false);
    }
  }

  async function acceptSelected() {
    if (!selectedProposal || !session) return;
    setBusy(true);
    setError(null);
    actor.send({ type: "ACCEPT" });
    try {
      if (agentPolicy?.mode === "ADVISORY") {
        const accepted = await cityNexusApi.acceptReplan(
          selectedProposal.id,
          "frontend-replan-accept-" + selectedProposal.id,
        );
        await applyAcceptedResponse(accepted);
      } else {
        let approvedId = approvalId;
        if (confirmationRequired && approvedId) {
          const approved = await cityNexusApi.approveAgentAction(session.journeyId, approvedId);
          approvedId = approved.approval_id;
        }
        const accepted = await cityNexusApi.callAgentTool("accept_replan", {
          request_id:
            "frontend-agent-accept-" + selectedProposal.id + "-" + String(Date.now()),
          agent_id: "frontend-agent",
          journey_id: session.journeyId,
          proposal_id: selectedProposal.id,
          approval_id: approvedId ?? undefined,
        });
        setServerDecision(accepted.decision);
        if (accepted.executed && accepted.result) {
          await applyAcceptedResponse(accepted.result);
        } else if (
          accepted.decision === "REQUIRE_CONFIRMATION" ||
          accepted.decision === "REQUIRE_STEP_UP"
        ) {
          setConfirmationRequired(true);
          setApprovalId(accepted.approval_id ?? null);
          setLastAction("Server requires confirmation before accepting this proposal");
          await refreshAuthoritativeFeed(session.journeyId);
        } else {
          setLastAction(
            "Server denied proposal acceptance · " + accepted.reason_codes.join(", "),
          );
          await refreshAuthoritativeFeed(session.journeyId);
        }
      }
    } catch (acceptError) {
      setError(errorNote(acceptError));
      actor.send({ type: "FAIL", message: errorNote(acceptError).note });
    } finally {
      setBusy(false);
    }
  }

  async function rejectSelected() {
    if (!selectedProposal) return;
    setBusy(true);
    setError(null);
    actor.send({ type: "REJECT" });
    try {
      const rejected = await cityNexusApi.rejectReplan(selectedProposal.id);
      setProposals((existing) =>
        existing.map((item) => (item.id === rejected.id ? rejected : item)),
      );
      setReplanRejected(true);
      await refreshAuthoritativeFeed(session?.journeyId);
      setLastAction("Rejected backend proposal " + rejected.id);
      actor.send({ type: "REJECTED" });
    } catch (rejectError) {
      setError(errorNote(rejectError));
      actor.send({ type: "FAIL", message: errorNote(rejectError).note });
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return (
      <main className="shell loading-shell">
        <p className="eyebrow">CityNexus · deterministic journey mode</p>
        <h1>Connecting to the JourneyTwin</h1>
        <p data-testid={testIds.journeyStatus}>BOOTSTRAPPING · backend</p>
      </main>
    );
  }

  if (error && !twin) {
    return (
      <main className="shell loading-shell">
        <p className="eyebrow">CityNexus · deterministic journey mode</p>
        <h1>Journey connection unavailable</h1>
        <p
          data-testid={testIds.journeyStatus}
          {...(error.http_status === null
            ? {}
            : { "data-cnx-field": "http_status", "data-cnx-value": String(error.http_status) })}
        >
          ERROR · {error.note}
        </p>
        <button onClick={() => window.location.reload()}>Retry backend connection</button>
      </main>
    );
  }

  return (
    <main className="shell">
      <header>
        <p className="eyebrow">CityNexus · deterministic journey mode</p>
        <h1>Commitment-aware recovery</h1>
        <span
          data-testid={testIds.journeyStatus}
          data-source="backend"
          data-journey-id={session?.journeyId}
          className="status"
        >
          {twin?.context.journey_status ?? "UNKNOWN"} · backend truth
        </span>
        {replanRejected && <span className="status muted-status"> · proposal rejected</span>}
        <p className="workflow-state">
          UI workflow · {workflowState}
          {lastAction ? " · " + lastAction : ""}
        </p>
        {error && (
          <p
            className="error-note"
            {...(error.http_status === null
              ? {}
              : {
                  "data-cnx-field": "http_status",
                  "data-cnx-value": String(error.http_status),
                })}
          >
            {error.note}
          </p>
        )}
      </header>
      <section className="command-center" data-testid="agent-command-center">
        <div className="command-copy">
          <p className="card-kicker">Control Tower · Delegated Autonomous Journey Agent</p>
          <h2>Tell CityNexus what changed</h2>
          <p>
            DeepSeek interprets the city signal and your intent. Hermes-style tool orchestration,
            policy, and deterministic feasibility gates decide what can actually change.
          </p>
          <p className="command-boundary">
            Ordinary reversible Activities can run inside the envelope; protected Commitments stay
            intact unless an explicitly authorized stronger action passes the Gateway.
          </p>
        </div>
        <div className="command-controls">
          <label className="field-label" htmlFor="agent-command">
            Natural-language mission
          </label>
          <textarea
            id="agent-command"
            data-testid="agent-command"
            value={agentCommand}
            onChange={(event) => setAgentCommand(event.target.value)}
            rows={4}
            disabled={busy}
          />
          <div className="command-toolbar">
            <label htmlFor="agent-autonomy">Execution envelope</label>
            <select
              id="agent-autonomy"
              data-testid="agent-autonomy"
              value={requestedAutonomy}
              onChange={(event) =>
                setRequestedAutonomy(
                  event.target.value as "PLAN_ONLY" | "EXECUTE_WITHIN_BOUNDS",
                )
              }
              disabled={busy}
            >
              <option value="EXECUTE_WITHIN_BOUNDS">Execute within policy bounds</option>
              <option value="PLAN_ONLY">Plan only · no mutation</option>
            </select>
            <button
              data-testid="run-agent-command"
              onClick={() => void runAgentCommand()}
              disabled={busy || agentCommand.trim().length < 8}
            >
              {busy ? "Agent is working…" : "Run Agent mission"}
            </button>
          </div>
        </div>
        {mission && agentRun && (
          <div className="mission-result" data-testid="agent-mission-result">
            <div className="mission-heading">
              <div>
                <p className="card-kicker">Persisted mission · {mission.id.slice(0, 8)}</p>
                <h3>{agentRun.status} · {agentRun.provider_key}/{agentRun.model_key}</h3>
              </div>
              <span className={agentRun.status === "COMPLETED" ? "run-badge run-complete" : "run-badge"}>
                {mission.status}
              </span>
            </div>
            <p className="mission-text">“{mission.user_text}”</p>
            <div className="mission-metrics">
              <span>Objectives · {mission.objective_codes.join(" · ") || "general recovery"}</span>
              <span>Budget Δ · {formatMoney(mission.max_cost_delta_minor, planCurrency)}</span>
              <span>Loss ceiling · {formatMoney(mission.max_loss_minor, planCurrency)}</span>
              <span>Schedule drift · {mission.max_schedule_drift_min} min</span>
              <span>Protected Activities · {mission.protected_activity_ids.length}</span>
              {agentRun.completion_contract?.kind && (
                <span>Server result · {agentRun.completion_contract.kind}</span>
              )}
            </div>
            {agentRun.outcome && (
              <p className="mission-outcome" data-testid="agent-run-outcome">
                {formatAgentOutcome(agentRun.outcome)}
              </p>
            )}
            {agentRun.failure_reason && <p className="mission-failure">{agentRun.failure_reason}</p>}
            <div className="run-steps">
              <strong>Persisted tool trace · {agentRun.tool_steps.length} calls</strong>
              {agentRun.tool_steps.length > 0 ? (
                <ol>
                  {agentRun.tool_steps.map((step) => (
                    <li key={step.id ?? step.sequence_no}>
                      <span className="step-index">{step.sequence_no}</span>
                      <span className="step-tool">{step.tool_name}</span>
                      <span className="step-result">
                        {typeof step.result.error === "string"
                          ? step.result.error
                          : step.decision ?? step.status}
                      </span>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="note">No external model calls were available; the mission boundary was persisted safely.</p>
              )}
            </div>
          </div>
        )}
      </section>
      <section className="guardian-card" data-testid="guardian-operations">
        <div className="guardian-heading">
          <div>
            <p className="card-kicker">Proactive Journey Guardian · persistent control plane</p>
            <h2>Journey Operations</h2>
          </div>
          <span className="guardian-decision" data-testid="guardian-decision">
            {guardianStatus?.latest?.decision ?? "WATCHING"}
          </span>
        </div>
        <p className="guardian-summary">
          {guardianStatus?.latest?.reasoning_summary ??
            "Activate a standing mission once; subsequent city signals can launch recovery without a new chat message."}
        </p>
          <div className="guardian-actions">
          <button
            className="secondary-button"
            data-testid="activate-guardian-mission"
            onClick={() => void activateGuardianMission()}
            disabled={busy || agentCommand.trim().length < 8}
          >
            Activate standing mission
          </button>
          <button
            data-testid="guardian-flagship-scan"
            onClick={() => void runProactiveGuardianScenario()}
            disabled={busy || !session?.techArt}
          >
            Inject city signal + proactive scan
          </button>
          <button
            className="secondary-button"
            data-testid="guardian-autopilot-start"
            onClick={() => void startLiveAutopilot()}
            disabled={busy || !session?.techArt}
          >
            Start live Autopilot
          </button>
          <button
            className="secondary-button"
            data-testid="guardian-autopilot-stop"
            onClick={() => void stopLiveAutopilot()}
            disabled={busy || !guardianStatus?.autopilot?.enabled}
          >
            Stop Guardian Watch
          </button>
          <button
            className="secondary-button"
            data-testid="guardian-provider-disrupted"
            onClick={() => void transitionProviderFixture("DISRUPTED")}
            disabled={busy || !guardianStatus?.autopilot?.enabled}
          >
            Provider: heavy rain + route delay
          </button>
          <button
            className="secondary-button predictive-button"
            data-testid="guardian-provider-risk-forecast"
            onClick={() => void runPredictiveAutopilotScenario()}
            disabled={busy || !session?.techArt}
          >
            Run Predictive Real-City Twin
          </button>
          <button
            className="secondary-button"
            data-testid="guardian-provider-normal"
            onClick={() => void transitionProviderFixture("NORMAL")}
            disabled={busy}
          >
            Provider: normal
          </button>
        </div>
        <p data-testid="guardian-autopilot-status" className="policy-note">
          Autopilot · {guardianStatus?.autopilot?.enabled ? "RUNNING" : "STOPPED"} · provider fixture ·{" "}
          {guardianStatus?.signal_fixture?.state ?? "NORMAL"}
        </p>
        {guardianStatus?.autopilot?.enabled && guardianStatus.autopilot.watch && (
          <div className="guardian-watch-panel" data-testid="guardian-watch-status">
            <strong>Guardian Watch is on</strong>
            <span>{guardianWatchCopy(guardianStatus.autopilot.watch.watch_state)}</span>
            <span data-testid="guardian-watch-next-check">
              Next check around {formatTime(guardianStatus.autopilot.watch.next_due_at)}
            </span>
            {guardianStatus.autopilot.watch.next_move?.copy && (
              <span data-testid="guardian-next-move">
                {guardianStatus.autopilot.watch.next_move.copy}
              </span>
            )}
          </div>
        )}
        <div className="predictive-panel" data-testid="guardian-predictive-resilience">
          <div>
            <strong>Predictive resilience</strong>
            <span className="predictive-score">
              {resilience ? resilience.risk_score + "/100 weakness" : "—"}
            </span>
          </div>
          <span>{resilience?.status ?? "WAITING_FOR_PROVIDER_EVIDENCE"}</span>
          <span>
            {resilience?.dominant_failure_modes.join(" · ") || "No dominant failure mode"}
          </span>
          <span>
            Mesh · {providerMesh?.providers.length ?? 0} observations · {providerMesh?.real_place_candidates.length ?? 0} real-city POIs
          </span>
          {guardianStatus?.latest?.trigger_kind === "PREDICTIVE" && (
            <span data-testid="guardian-predictive-result">
              {guardianStatus.latest.action_status} · {guardianStatus.latest.reasoning_summary}
            </span>
          )}
        </div>
        <div className="provenance-lanes" data-testid="guardian-provenance">
          <span><b>Observation</b>{String(guardianStatus?.latest?.evidence.observation?.open_event_ids ?? "—")}</span>
          <span><b>Inference</b>{String(guardianStatus?.latest?.evidence.inference?.decision ?? "—")}</span>
          <span><b>Solver</b>{String(guardianStatus?.latest?.evidence.solver?.kind ?? "—")}</span>
          <span><b>Policy</b>{String(guardianStatus?.latest?.evidence.policy?.decision ?? "—")}</span>
          <span><b>Mutation</b>{guardianStatus?.latest?.action_status ?? "—"}</span>
        </div>
        {guardianStatus?.standing_mission && (
          <div className="standing-mission" data-testid="guardian-standing-mission">
            <strong>Standing Mission · {guardianStatus.standing_mission.status}</strong>
            <span>{guardianStatus.standing_mission.objective_codes.join(" · ")}</span>
            <span>
              Protected {guardianStatus.standing_mission.protected_activity_ids.length} · budget Δ {formatMoney(
                Number(guardianStatus.standing_mission.resolved_intent.max_cost_delta_minor ?? 0),
                planCurrency,
              )}
            </span>
            <span>
              Compiler {guardianStatus.standing_mission.compiler.provider_key}/
              {guardianStatus.standing_mission.compiler.model_key}
            </span>
          </div>
        )}
        {guardianStatus?.latest?.tool_steps.length ? (
          <ol className="guardian-trace" data-testid="guardian-trace">
            {guardianStatus.latest.tool_steps.map((step) => (
              <li key={step.sequence_no}>
                <span>{step.sequence_no}</span>
                <code>{step.tool_name}</code>
                <b>{step.decision ?? step.status}</b>
              </li>
            ))}
          </ol>
        ) : null}
        {liveOperations.length > 0 && (
          <ol className="guardian-trace" data-testid="guardian-live-operations">
            {liveOperations.map((item, index) => (
              <li key={item.timestamp + item.event_type + index}>
                <span>{item.event_type}</span>
                <b>{item.summary}</b>
              </li>
            ))}
          </ol>
        )}
      </section>
      {agentPolicy && (
        <section className="agent-grid" aria-label="Agent controls and activity">
          <div className="agent-card">
            <p className="card-kicker">Delegation boundary</p>
            <h2>Agent Mode</h2>
            <label className="field-label" htmlFor="agent-mode">
              Operating mode
            </label>
            <select
              id="agent-mode"
              data-testid="agent-mode"
              value={agentPolicy.mode}
              onChange={(event) =>
                void updateAgentPolicy({
                  mode: event.target.value as AgentPolicy["mode"],
                })
              }
            >
              <option value="ADVISORY">建议模式 · always ask</option>
              <option value="ASSISTED">协助模式 · bounded auto-accept</option>
              <option value="AUTONOMOUS">自主模式 · policy-gated</option>
            </select>
            <label className="toggle-row">
              <input
                data-testid="agent-auto-accept"
                type="checkbox"
                checked={agentPolicy.auto_accept_replan}
                onChange={(event) =>
                  void updateAgentPolicy({
                    auto_accept_replan: event.target.checked,
                  })
                }
              />
              Auto-accept envelope-safe replans
            </label>
            <label className="field-label" htmlFor="agent-cost-limit">
              Max automatic cost increase (minor units)
            </label>
            <input
              id="agent-cost-limit"
              type="number"
              min="0"
              value={agentPolicy.max_auto_replan_cost_delta_minor}
              onChange={(event) =>
                void updateAgentPolicy({
                  max_auto_replan_cost_delta_minor: Math.max(0, Number(event.target.value) || 0),
                })
              }
            />
            <label className="field-label" htmlFor="agent-loss-limit">
              Max automatic acceptable loss (minor units)
            </label>
            <input
              id="agent-loss-limit"
              type="number"
              min="0"
              value={agentPolicy.max_auto_loss_minor}
              onChange={(event) =>
                void updateAgentPolicy({
                  max_auto_loss_minor: Math.max(0, Number(event.target.value) || 0),
                })
              }
            />
            <label className="field-label" htmlFor="agent-drift-limit">
              Max automatic schedule drift (minutes)
            </label>
            <input
              id="agent-drift-limit"
              type="number"
              min="0"
              value={agentPolicy.max_auto_schedule_drift_min}
              onChange={(event) =>
                void updateAgentPolicy({
                  max_auto_schedule_drift_min: Math.max(0, Number(event.target.value) || 0),
                })
              }
            />
            <label className="field-label" htmlFor="agent-activity-types">
              Allowed automatic Activity types (comma separated; blank = ordinary types)
            </label>
            <input
              id="agent-activity-types"
              value={agentPolicy.allowed_activity_types.join(", ")}
              onChange={(event) =>
                void updateAgentPolicy({
                  allowed_activity_types: event.target.value
                    .split(",")
                    .map((item) => item.trim().toLowerCase())
                    .filter(Boolean),
                })
              }
            />
            <label className="field-label" htmlFor="agent-booking-types">
              Allowed automatic Booking types (comma separated; blank = all)
            </label>
            <input
              id="agent-booking-types"
              value={agentPolicy.allowed_booking_types.join(", ")}
              onChange={(event) =>
                void updateAgentPolicy({
                  allowed_booking_types: event.target.value
                    .split(",")
                    .map((item) => item.trim().toLowerCase())
                    .filter(Boolean),
                })
              }
            />
            <label className="toggle-row">
              <input
                type="checkbox"
                checked={agentPolicy.allow_auto_commitment_management}
                onChange={(event) =>
                  void updateAgentPolicy({
                    allow_auto_commitment_management: event.target.checked,
                  })
                }
              />
              Pre-authorize commitment management within the envelope
            </label>
            <p className="policy-note">
              Policy v{agentPolicy.version} · protected commitment levels {agentPolicy.protected_commitment_levels.join(", ")}
              remain server-owned.
            </p>
          </div>
          <div className="agent-card" data-testid="agent-feed">
            <p className="card-kicker">Explainable execution</p>
            <h2>Agent Activity Feed</h2>
            <ol className="agent-feed-list">
              {agentFeed.map((item) => (
                <li
                  key={item.id}
                  data-event-type={item.event_type}
                  data-actor-type={item.actor_type}
                  data-tool={item.tool ?? undefined}
                  data-decision={item.decision ?? undefined}
                >
                  {item.summary} · {item.event_type}
                </li>
              ))}
            </ol>
            <p data-testid="agent-policy-decision" className="impact-summary">
              Server decision · {serverDecision ?? "not evaluated"}
              {confirmationRequired ? " · confirmation required" : ""}
            </p>
            {impactResolution && (
              <p className="impact-summary">
                Impact evidence · {impactResolution.status ?? "unknown"} ·{" "}
                {impactResolution.provider_key ?? "deterministic"} ·{" "}
                {impactResolution.selected_activity_id
                  ? (impactResolution.maturity_stage ?? "validated Activity")
                  : "no actionable target"}
              </p>
            )}
          </div>
        </section>
      )}
      {reflectSummary && (
        <section className="agent-card" data-testid="reflect-summary">
          <p className="card-kicker">Recover → Reflect</p>
          <h2>Journey history</h2>
          <p className="impact-summary">
            Plan versions · {reflectSummary.plan_version_chain.length} · replans ·{" "}
            {reflectSummary.replan_history.length} · disruptions ·{" "}
            {reflectSummary.provider_disruptions.length}
          </p>
          <p className="impact-summary">
            Estimated cost · {formatMoney(reflectSummary.cost_summary.final_estimated_minor, reflectSummary.cost_summary.currency)}
            {reflectSummary.cost_summary.estimated_delta_minor !== 0
              ? " · Δ " + formatMoney(reflectSummary.cost_summary.estimated_delta_minor, reflectSummary.cost_summary.currency)
              : ""}
          </p>
          <p className="policy-note">{reflectSummary.timeline.actual_time_note}</p>
        </section>
      )}
      {travelerTwin && (
        <section className="agent-card" data-testid={testIds.travelerTwinPanel}>
          <p className="card-kicker">TravelerTwin Control Tower</p>
          <h2>Inspectable cross-Journey learning</h2>
          <p className="impact-summary">
            Explicit {travelerTwin.explicit_preferences.length} · learned soft {travelerTwin.learned_soft_preferences.length} ·
            confidence {Math.round(travelerTwin.confidence * 100)}% · evidence {travelerTwin.evidence_count}
          </p>
          <div className="upcoming-list">
            <strong>Explicit preferences</strong>
            {travelerTwin.explicit_preferences.length === 0 ? (
              <span>None recorded.</span>
            ) : (
              travelerTwin.explicit_preferences.map((item, index) => (
                <span key={(String(item.dimension ?? "explicit")) + index}>
                  {String(item.dimension ?? item.category ?? "preference")} · {String(item.explanation ?? "user-provided")}
                </span>
              ))
            )}
          </div>
          <div data-testid={testIds.travelerTwinLearned} className="upcoming-list">
            {travelerTwin.learned_soft_preferences.length === 0 ? (
              <span>No learned soft evidence yet.</span>
            ) : (
              travelerTwin.learned_soft_preferences.map((item, index) => (
                <span key={(item.dimension ?? "learned") + (item.direction ?? "") + index}>
                  {item.direction ?? item.dimension ?? "preference"} · strength {Math.round((item.strength ?? 0) * 100)}% ·
                  evidence {item.evidence_count ?? 0} · observed {item.last_evidence_at?.slice(0, 10) ?? "unknown"} · {item.explanation ?? "outcome evidence"}
                </span>
              ))
            )}
          </div>
          <div className="upcoming-list">
            <strong>Conflicts and resolution</strong>
            {travelerTwin.conflicts.length === 0 ? (
              <span>No active conflicts.</span>
            ) : (
              travelerTwin.conflicts.map((item, index) => (
                <span key={String(item.dimension ?? "conflict") + index}>
                  {String(item.dimension ?? "preference")} · {Array.isArray(item.directions) ? item.directions.join(" vs ") : "conflict"} · {String(item.resolution ?? "explicit wins")}
                </span>
              ))
            )}
          </div>
          <p className="policy-note">
            Learned evidence is soft planning input only; it never expands budget, payment, Commitment, or autonomy authority.
          </p>
          <button
            data-testid={testIds.travelerTwinClear}
            onClick={() => void clearLearnedTravelerTwin()}
            disabled={busy || travelerTwin.learned_soft_preferences.length === 0}
          >
            Clear learned soft evidence
          </button>
        </section>
      )}
      <section className="grid">
         <div data-testid={testIds.journeyMap} className="map-card" data-basemap-provider="openfreemap">
          <div className="map-title">Journey map · backend route evidence</div>
          <div className="map-canvas map-canvas-live" aria-label="Journey map markers and route">
            {places.length > 0 && plan && twin ? (
              <Suspense fallback={<div className="map-fallback">Loading map evidence…</div>}>
                <JourneyMap
                  plan={plan}
                  twin={twin}
                  places={places}
                  eventActive={Boolean(eventId)}
                  proposals={proposals}
                  routeEvidence={routeEvidence}
                />
              </Suspense>
            ) : (
              <div className="map-fallback">Place geometry unavailable · offline evidence mode</div>
            )}
            <span data-testid={testIds.mapCurrentMarker} className="map-marker current-marker">
              {current ? current.title : "Current"}
            </span>
            {eventId && (
              <span data-testid={testIds.mapEventMarker} className="map-marker event-marker">
                Closure event
              </span>
            )}
            {c4Activity && (
              <span data-testid={testIds.mapProtectedMarker} className="map-marker protected-marker">
                C4
              </span>
            )}
            <span data-testid={testIds.mapOldRoute} className="map-legend old-geometry">
              Old plan geometry
            </span>
            {proposals.length > 0 && (
              <span data-testid={testIds.mapNewRoute} className="map-legend new-geometry">
                Active proposal geometry
              </span>
            )}
          </div>
          <p data-testid={testIds.currentActivity}>
            Current: {current?.title ?? "not reported"}
          </p>
          <p>Next: {next?.title ?? "no upcoming activity"}</p>
        </div>
        <aside className="evidence-card">
          <h2 data-testid={testIds.protectedCommitment}>
            🔒 {c4Activity?.title ?? "No active C4"} ·{" "}
            {c4Commitment ? formatTime(c4Commitment.policy.scheduled_start_at) : "—"} C4
          </h2>
          <p data-testid={testIds.nextCommitment}>
            Next activity · {next?.title ?? c4Activity?.title ?? "—"}
          </p>
          <p data-testid={testIds.nextCommitmentBuffer}>
            Next commitment buffer ·{" "}
            {envelope?.next_commitment_buffer_min == null
              ? "not available"
              : envelope.next_commitment_buffer_min + " min"}
          </p>
          <p data-testid={testIds.budgetRemaining}>
            Budget remaining ·{" "}
            {envelope ? formatMoney(envelope.budget_remaining_minor, planCurrency) : "—"}
          </p>
          <p data-testid={testIds.opportunityWindow}>
            Opportunity window ·{" "}
            {opportunity
              ? formatTime(opportunity.opens_at) +
                "–" +
                formatTime(opportunity.must_depart_by) +
                " · " +
                opportunity.available_minutes +
                " min available"
              : "not available"}
          </p>
          <p data-testid={testIds.feasibilityEnvelope}>
            Feasibility envelope · {envelope?.robust ? "robust" : "review"} · persisted backend
            evidence
          </p>
          <p
            data-testid={testIds.providerDegradation}
            className={degradedProviders.length ? "" : "provider-ready"}
          >
            Provider status ·{" "}
            {degradedProviders.length ? "degraded: " + degradedProviders.join(", ") : "ready"}
          </p>
          <div data-testid={testIds.upcomingCommitments} className="upcoming-list">
            <strong>Upcoming commitments from JourneyTwin</strong>
            {twin?.upcoming_commitments.map((commitment) => {
              const linked = commitmentActivity(plan, commitment);
              return (
                <span key={commitment.id}>
                  {linked?.title ?? commitment.policy.semantic_key ?? commitment.id} ·{" "}
                  {formatTime(commitment.policy.scheduled_start_at)} · {commitment.level}
                </span>
              );
            })}
          </div>
          <button
            data-testid={testIds.eventSimulator}
            onClick={() => setSimulatorOpen((value) => !value)}
            disabled={busy || !session?.techArt}
          >
            Demo Event Simulator
          </button>
          {simulatorOpen && session?.techArt && (
            <button
              data-testid={testIds.eventClosureButton}
              onClick={() => void runClosure()}
              disabled={busy}
            >
              Inject Tech Art closure · {formatTime(closureOccurredAt)}
            </button>
          )}
        </aside>
      </section>
      <section data-testid={testIds.journeyTimeline} className="timeline-card">
        <strong>Journey timeline · active PlanVersion</strong>
        <div className="timeline-track">
          {plan?.activities.map((activity) => {
            const isCurrent = activity.id === twin?.current_activity_id;
            const isProtected = activity.id === c4Activity?.id;
            return (
              <span
                key={activity.id}
                className={
                  "timeline-item" +
                  (isCurrent ? " active" : "") +
                  (isProtected ? " protected" : "")
                }
              >
                {formatTime(activity.start_at)} · {activity.title}
                {isProtected ? " · C4" : ""}
              </span>
            );
          })}
        </div>
      </section>
      {selectedProposal && plan && (
        <section data-testid={testIds.replanPanel} className="replan-card">
          <h2>Replan comparison · persisted proposals</h2>
          <div data-testid={testIds.replanCandidate} className="candidate">
            {selectedProposal.diff.candidate_family} ·{" "}
            {selectedProposal.hard_constraints_passed ? "verified feasible" : "not feasible"}
          </div>
          <div data-testid={testIds.replanCandidateFamilies} className="candidate-families">
            {families.map((family) => (
              <button
                key={family}
                className={
                  family === selectedProposal.diff.candidate_family
                    ? "family-chip selected"
                    : "family-chip"
                }
                onClick={() => {
                  const candidate = proposals.find(
                    (item) => item.diff.candidate_family === family,
                  );
                  setSelectedProposalId(candidate?.id ?? null);
                }}
                disabled={busy}
              >
                {family}
              </button>
            ))}
          </div>
          <p data-testid={testIds.replanScore}>
            Utility score · {selectedProposal.utility_score.toFixed(3)} ·{" "}
            {Object.entries(selectedProposal.score_breakdown)
              .filter(([key]) => key !== "total")
              .slice(0, 3)
              .map(([key, value]) => key + " " + Number(value).toFixed(2))
              .join(" · ")}
          </p>
          <p data-testid={testIds.replanEvidence}>
            Evidence · hard constraints{" "}
            {selectedProposal.hard_constraints_passed ? "pass" : "fail"} · C4{" "}
            {selectedProposal.locked_commitments_preserved ? "preserved" : "not preserved"} ·{" "}
            {selectedProposal.diff.verified_evidence
              ? "verified evidence attached"
              : "evidence unavailable"}
          </p>
          <p data-testid={testIds.replanDiff}>{proposalDiffText(selectedProposal, plan)}</p>
          <p data-testid={testIds.replanDeltas}>
            {proposalDeltaText(selectedProposal, planCurrency)} · protected commitments unchanged
          </p>
          <div className="replan-actions">
            <button
              data-testid={testIds.acceptReplan}
              onClick={() => void acceptSelected()}
              disabled={busy}
            >
              {confirmationRequired ? "Confirm server-approved replan" : "Accept backend replan"}
            </button>
            <button
              data-testid={testIds.rejectReplan}
              className="secondary-button"
              onClick={() => void rejectSelected()}
              disabled={busy}
            >
              Reject
            </button>
          </div>
        </section>
      )}
      <p data-testid={testIds.aiExplanation} className="note">
        Explanations are limited to verified JourneyTwin and ReplanDiff evidence.
      </p>
    </main>
  );
}

/* ORD-0351: `/v8/<journey id>` was an address nothing read.  `routeFromPath` (app/V8Experience.tsx:461)
   maps an unknown first segment to Today, and boot reads only `localStorage`, so opening a link to a
   saved trip rendered whatever *else* this device held -- or the wizard -- while the address bar went on
   naming a journey no request had ever asked about.  Adopting the reference here, before the first
   render, hands it to the one read path that already classifies a failed journey reference: the shell's
   boot, which is where the leg-by-leg attribution, the measured offline-copy offer and the picker live.
   This file adds no state vocabulary and no second failure surface on purpose -- duplicating that
   surface is how two screens start disagreeing about one answer. */
const v8JourneyReferenceKey = "REDACTED_FOR_PUBLIC_RELEASE";
/** A canonical server-issued UUID, and nothing else: no route key or alias in routes/v8Routes.ts is.
    The shape is shared with ORD-0404's deep-link reading (`lib/journeyLink.ts`), because a segment
    that names a journey must not be called a segment that names no screen. */
const journeyIdSegmentPattern = JOURNEY_ID_SEGMENT;

function journeyReferenceFromPath(pathname: string): string | null {
  const first = v8FirstSegment(pathname);
  return journeyIdSegmentPattern.test(first) ? first : null;
}

function adoptJourneyReferenceFromUrl(): void {
  if (window.location.pathname.startsWith("/legacy")) return;
  const referenced = journeyReferenceFromPath(window.location.pathname);
  if (!referenced) return;
  try {
    localStorage.setItem(v8JourneyReferenceKey, referenced);
  } catch {
    /* Blocked storage means the reference cannot be adopted, so this document will describe the
       journey this device holds rather than the one the address names.  Left unnamed on purpose:
       closing it needs the boot surface, which is not this file's to edit. */
  }
}

function App() {
  // The default entry is the modern traveler product.  LegacyApp is retained
  // for internal/back-compat access only, behind an explicit /legacy path, so a
  // first-time user never lands on the legacy surface (see PHASE 2).
  const path = window.location.pathname;
  if (path.startsWith("/legacy")) return <LegacyApp />;
  return (
    <ProductErrorBoundary>
      <V8Experience />
    </ProductErrorBoundary>
  );
}

/* Once per document, before anything mounts: the reference has to exist by the time the shell's boot
   chooses which journey to read, and a render-phase write would re-run on every re-render. */
adoptJourneyReferenceFromUrl();

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={citynexusQueryClient}>
    <LocaleProvider>
      <App />
    </LocaleProvider>
  </QueryClientProvider>,
);

const serviceWorkerReloadMarkerKey = "citynexus.sw.controller-transition";
const serviceWorkerPendingHandoverKey = "citynexus.sw.generation-handover-pending";
const MAX_AUTOMATIC_CONVERGENCE_PER_SESSION = 2;

/**
 * Caches the previous strategies left behind under names nothing owns any more.
 * Deliberately not a prefix sweep: the versioned `citynexus-pages-*` / `citynexus-static-*`
 * caches belong to the generated worker, and wiping them from the page would recreate the
 * "refresh shows the old shell" bug from the other side.
 */
async function removeLegacyServiceWorkerCaches() {
  if (!("caches" in window)) return;
  const legacyCacheNames = new Set([
    "citynexus-trip-operations-v1",
    "citynexus-pages",
    "citynexus-static",
  ]);
  const cacheNames = await caches.keys();
  await Promise.all(
    cacheNames
      .filter(
        (name) =>
          legacyCacheNames.has(name) || name.startsWith("citynexus-trip-operations-v"),
      )
      .map((name) => caches.delete(name)),
  );
}

/** A production worker left running by a same-origin preview must not drive Vite/HMR. */
async function unregisterProductionServiceWorkers() {
  const registrations = await navigator.serviceWorker.getRegistrations();
  await Promise.all(
    registrations
      .filter((registration) => (registration.active?.scriptURL ?? registration.installing?.scriptURL ?? "").includes("/v10-sw.js"))
      .map((registration) => registration.unregister()),
  );
}

async function registerProductionServiceWorker() {
  /* Identity, not URL. A generated worker is always served from /v10-sw.js, so `before === after` was
     true for every real generation change and this handler returned before it could reload anything:
     measured across two generations on one origin, `controllerchange` fired once, the document's load
     count stayed at 1, no marker was ever written, and the previous bundle kept executing until a
     SECOND navigation. A ServiceWorker object does change when a new generation takes over.

     Tracked across events, not read once. A first visitor is not controlled when this runs, so a single
     `controllerBefore` captured here stayed null for the whole document: the claim arrived as the first
     `controllerchange`, and every later generation change was bailed out by that same null. Measured on the B2b arm -
     two controllerchange events, no marker written, load count 1, and the old bundle still executing after
     the new generation had taken over. The exclusion that is actually wanted is "the first time this
     document was ever controlled", which is a property of the transition, not of registration time. */
  let lastController = navigator.serviceWorker.controller ?? null;

  navigator.serviceWorker.addEventListener("controllerchange", () => {
    const controllerAfter = navigator.serviceWorker.controller;
    if (!controllerAfter || controllerAfter === lastController) return;
    const wasControlledBeforeThisEvent = lastController !== null;
    lastController = controllerAfter;
    // A document that was never controlled has no old shell to converge; this is the claim, not a generation change.
    if (!wasControlledBeforeThisEvent) return;

    // Bounded, so a worker that keeps re-taking control cannot turn into a reload loop.
    const switchesTaken = Number(sessionStorage.getItem(serviceWorkerReloadMarkerKey) || "0");
    if (switchesTaken >= MAX_AUTOMATIC_CONVERGENCE_PER_SESSION) return;

    // Reload so an old shell cannot keep executing against a new asset graph after an auto-update.
    sessionStorage.setItem(serviceWorkerReloadMarkerKey, String(switchesTaken + 1));
    /* B2c: a write that has already left the client is not mine to abandon. If one is in flight, name it
       as a pending handover and reload at the next quiet point instead of mid-request; with nothing in
       flight the quiet point is now, so no user action is ever added. */
    if (hasNonIdempotentRequestInFlight()) {
      sessionStorage.setItem(serviceWorkerPendingHandoverKey, "pending");
      window.dispatchEvent(new CustomEvent("citynexus:generation-handover-pending"));
    }
    onNonIdempotentRequestsQuiet(() => {
      sessionStorage.removeItem(serviceWorkerPendingHandoverKey);
      window.location.reload();
    });
  });

  const registration = await navigator.serviceWorker.register("/v10-sw.js", {
    scope: "/",
    updateViaCache: "none",
  });
  await registration.update();
  await navigator.serviceWorker.ready;
  await removeLegacyServiceWorkerCaches();
}

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    if (import.meta.env.DEV) {
      /* Development registers only the checked-in recovery/dev worker -- never the
         generated production one, whose precache graph is not the code being edited -- and
         drops a production worker a previous same-origin preview left in control. */
      void unregisterProductionServiceWorkers()
        .then(() => navigator.serviceWorker.register("/sw.js", { scope: "/" }))
        .catch((error) => {
          console.warn("CityNexus could not set up the development service worker", error);
        });
      return;
    }

    void registerProductionServiceWorker().catch((error) => {
      // A worker failing to register must not stop the application from booting.
      console.warn("CityNexus service worker registration failed", error);
    });
  });
}
