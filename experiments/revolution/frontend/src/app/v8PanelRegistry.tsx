import { Fragment, type ReactNode } from "react";

import { ActivityDetail } from "../features/trip/ActivityDetail";
import { AgentComposer } from "../features/agent/AgentComposer";
import { BookingArtifactScanner } from "../features/inbox/BookingArtifactScanner";
import { ControlTowerPanel } from "../features/intelligence/ControlTowerPanel";
import { DestinationPreparationPanel } from "../features/discovery/DestinationPreparationPanel";
import { HotelsPanel } from "../features/stay/HotelsPanel";
import { JourneyInboxWorkbench } from "../features/inbox/JourneyInboxWorkbench";
import { LiveRecovery } from "../features/execution/LiveRecovery";
import { PlanComparison } from "../features/planning/PlanComparison";
import { PlanRankingPanel } from "../features/planning/PlanRankingPanel";
import { PendingSetPanel } from "../features/pending/PendingSetPanel";
import { PretripChecklistPanel } from "../features/checklist/PretripChecklistPanel";
import { PlanVersionHistoryPanel } from "../features/studio/PlanVersionHistoryPanel";
import { RealCityDiscoveryPanel } from "../features/discovery/RealCityDiscoveryPanel";
import { RecoveryRequiredPanel } from "../features/recovery/RecoveryRequiredPanel";
import { StayExplorer } from "../features/stay/StayExplorer";
import { TwinPanel } from "../features/traveler-twin/TwinPanel";
import { TripItinerary } from "../features/trip/TripItinerary";
import { JourneyTrashPanel } from "../features/trash/JourneyTrashPanel";
import { AccessPanel } from "../features/access/AccessPanel";
import { V10LiveExecutionPanel } from "../features/execution/V10LiveExecutionPanel";
import { V12AuthoritativeJourneyPanel } from "../features/execution/V12AuthoritativeJourneyPanel";
import { V12TripStudioPanel } from "../features/studio/V12TripStudioPanel";
import { V14PreventivePanel } from "../features/recovery/V14PreventivePanel";
import { V14RecoveryTruthPanel } from "../features/recovery/V14RecoveryTruthPanel";

/**
 * ORD-0392: which feature panel appears on which screen, in one table.
 *
 * Why this file exists: until now every panel was mounted by an `{route === "x" && <Panel/>}`
 * block inside `app/V8Experience.tsx`, so adding a panel meant editing a 4 494-line file that
 * three other lanes were editing the same night. Tonight's rejections were all that one file.
 * A lane that wants a panel on a screen now adds one entry here and never opens the shell.
 *
 * What is deliberately NOT here: the page shells (`Hub`, `Studio`, `Live`, `MapPage`,
 * `TripsWorkspace`, `Bookings`, `Me`, `AuthoritativeExecution`, `WalletCommitments`) and the local
 * markup inside them. They are defined in the shell file and reach the table through `slots`,
 * which is a bag of already-bound render functions. That keeps this file free of the shell's
 * state while leaving every element, attribute and sibling order exactly where it was -- the
 * acceptance for this order is "the screen does not change", so nothing here re-decides layout.
 *
 * Field notes:
 *  - `id` is the React key. It is stable and unique per surface; changing it remounts the panel.
 *  - `testId` names the element this entry puts on screen. It is documentation for a human
 *    reading the table, and the handle the screen-reading instrument counts (`[data-testid]`).
 *    An entry may render more than one testid (a wrapper plus a panel); `testId` is the one a
 *    traveller would be pointed at, or `null` when the entry has no single named element of its
 *    own -- a page shell (`hub`), local markup (the no-plan notice), or a panel that renders no
 *    testid at all (`TwinPanel`). Every non-null value here was read off the component source or
 *    off a measured screen; none of it is invented.
 *  - `when` is the whole rendering condition, verbatim from the `&&` it replaced.
 *  - `render` returns exactly the JSX that block returned before -- including the fragments,
 *    so the DOM sibling order is unchanged.
 *
 * Types are loose on purpose (`any` for the scope): this order may not change behaviour, and
 * tightening the props of 24 mount sites would be a second change hiding inside the first.
 */

export type V8PanelSurface = "route" | "trips-tab" | "authoritative" | "hub-anchor";

export type V8PanelEntry<S> = {
  id: string;
  surface: V8PanelSurface;
  /** `null` = this entry puts no named testid on screen (local markup, kept in order). */
  testId: string | null;
  when: (scope: S) => boolean;
  render: (scope: S) => ReactNode;
};

/* eslint-disable @typescript-eslint/no-explicit-any */
export type RouteScope = any;
export type TripsTabScope = any;
export type AuthoritativeScope = any;
export type HubAnchorScope = any;

/** The shell's route chain: one entry per `{route === "…" && …}` block it used to hold. */
export const routePanels: V8PanelEntry<RouteScope>[] = [
  {
    id: "hub",
    surface: "route",
    testId: null,
    when: (s) => s.route === "hub",
    render: (s) => (
      <>
        {/* Only offer the chooser while no plan is chosen; once chosen,
            switching lives in Trips so Today stays task-focused. */}
        {!s.hasChosenPlan && (
          <PlanComparison
            journeyId={s.data.journey.id}
            plans={s.authoritativePlans.data ?? []}
            budgetMinor={s.data.journey.budget.amount_minor}
            currency={s.data.journey.budget.currency}
            onSelected={() => {
              void s.load(s.data.journey.id);
              void s.authoritativePlans.refetch();
              void s.authoritativeTwin.refetch();
            }}
          />
        )}
        {s.slots.hub()}
      </>
    ),
  },
  /* ORD-0439: the comparison screen sits under Today, where the candidate list already lives. It is
     a registry entry, not a shell edit -- which is the point of this table. */
  {
    id: "plan-ranking",
    surface: "route",
    testId: "plan-ranking",
    when: (s) => s.route === "hub",
    render: (s) => (
      <PlanRankingPanel
        budgetMinor={s.data.journey.budget.amount_minor}
        currency={s.data.journey.budget.currency}
      />
    ),
  },
  {
    /* ORD-0445: 待规划 as its own data state. The shell holds no mount for it and none is added --
       the panel reads its two doors itself and takes the journey it is on from the scope this table
       already carries, the same way `plan-ranking` above does. It sits under Today because that is
       where the trip's day-by-day answer lives, and the set is defined against those days. */
    id: "pending-set",
    surface: "route",
    testId: "pending-set",
    when: (s) => s.route === "hub" && Boolean(s.data?.journey?.id),
    render: (s) => (
      <PendingSetPanel
        data={s.data}
        onNavigate={(route: string) => s.navigate(route)}
      />
    ),
  },
  {
    id: "studio",
    surface: "route",
    testId: "v8-trip-studio",
    when: (s) => s.route === "studio",
    render: (s) => <>{s.slots.studio()}</>,
  },
  {
    id: "stay",
    surface: "route",
    testId: "v8-stay-explorer",
    when: (s) => s.route === "stay",
    render: (s) => (
      <StayExplorer
        data={s.data}
        selected={s.selected}
        onSelect={(id: string) => void s.selectOption(id)}
      />
    ),
  },
  {
    id: "discover",
    surface: "route",
    testId: "prep-panel",
    when: (s) => s.route === "discover",
    render: (s) => (
      <>
        {/* ORD-0193: whether this destination can be planned, and what to do about it, in
            front of the discovery console -- both read the same server projections, and this
            one owns the "prepare / wait / go and plan" loop. */}
        <DestinationPreparationPanel
          journeyId={s.data.journey.id}
          onOpenPlan={() => s.navigate("authoritative")}
          initialQuery={s.data.journey.city_code ?? ""}
        />
        <RealCityDiscoveryPanel
          journeyId={s.data.journey.id}
          cityCode={s.data.journey.city_code}
          onComplete={() => s.refreshAuthoritativeTruth(s.data.journey.id)}
          onOpenPlan={() => s.navigate("authoritative")}
        />
      </>
    ),
  },
  {
    id: "live",
    surface: "route",
    testId: "live-recovery",
    when: (s) => s.route === "live",
    render: (s) => (
      <div
        className="live-shell"
        data-decision-active={s.disruptions.length > 0 ? "true" : "false"}
      >
        {s.slots.live()}
        <LiveRecovery
          journeyId={s.data.journey.id}
          journeyStatus={s.data.journey.status}
          handledEventIds={s.handledEventIds}
          onRefreshLocation={s.captureForegroundLocation}
          onAccepted={() => void s.load(s.data.journey.id)}
        />
        {s.slots.liveSecondary()}
        <V10LiveExecutionPanel journeyId={s.data.journey.id} plan={s.data.active_plan ?? null} />
      </div>
    ),
  },
  {
    id: "map",
    surface: "route",
    testId: "v8-maplibre-panel",
    when: (s) => s.route === "map",
    render: (s) => <>{s.slots.mapPage()}</>,
  },
  {
    id: "trips-activity-detail",
    surface: "route",
    testId: "activity-detail",
    when: (s) => s.route === "trips" && Boolean(s.detailActivity),
    render: (s) => (
      <ActivityDetail
        activity={s.detailActivity as never}
        timeZone={s.data.journey.timezone || "UTC"}
        onBack={() => s.setDetailActivityId(null)}
        onShowOnMap={() => {
          /* ORD-0390: this used to clear `detailActivityId` and then navigate to the map -- it
             threw away the very id the map needs to centre on, so 「在地图上显示」 could only ever
             land on a generic midpoint. The selection is the payload of this action, so it now
             survives it. Carried over verbatim when this mount moved into the table. */
          s.navigate("map");
        }}
        onAskCopilot={() => {
          s.setDetailActivityId(null);
          s.navigate("agent");
        }}
        nearby={s.nearbyActivities as never}
        onOpenNearby={(id: string) => s.setDetailActivityId(id)}
      />
    ),
  },
  {
    id: "trips-workspace",
    surface: "route",
    testId: "trips-tabs",
    when: (s) => s.route === "trips" && !s.detailActivity,
    render: (s) => <>{s.slots.tripsWorkspace()}</>,
  },
  {
    id: "bookings",
    surface: "route",
    testId: "inbox-pending-queue",
    when: (s) => s.route === "bookings",
    render: (s) => (
      <>
        {s.slots.bookings()}
        <div className="v8-page">
          <BookingArtifactScanner onDecoded={s.setDecodedArtifactText} />
        </div>
        <div className="v8-page">
          <JourneyInboxWorkbench
            journeyId={s.data.journey.id}
            decodedText={s.decodedArtifactText}
            importedAt={s.artifactImportedAt}
            onConfirmed={() => s.load(s.data.journey.id)}
          />
        </div>
      </>
    ),
  },
  {
    id: "intelligence",
    surface: "route",
    testId: "v17-runtime-status",
    when: (s) => s.route === "intelligence",
    render: (s) => (
      <ControlTowerPanel
        policy={s.policy}
        runtimeStatus={s.runtimeStatus}
        onActivate={s.activateAutonomy}
      />
    ),
  },
  {
    id: "twin",
    surface: "route",
    testId: null,
    when: (s) => s.route === "twin",
    render: (s) => <TwinPanel data={s.data} />,
  },
  {
    id: "authoritative",
    surface: "route",
    testId: "v12-authoritative-page",
    when: (s) => s.route === "authoritative",
    render: (s) => <>{s.slots.authoritative()}</>,
  },
  {
    id: "agent",
    surface: "route",
    testId: "v15-copilot",
    when: (s) => s.route === "agent",
    render: (s) => <AgentComposer data={s.data} busy={s.busy} onNavigate={s.navigate} />,
  },
  {
    id: "me",
    surface: "route",
    testId: "v8-me",
    when: (s) => s.route === "me",
    render: (s) => <>{s.slots.me()}</>,
  },
  {
    /* ORD-0402 slice 2: the recycle bin, on the route surface -- the shell holds no mount for it, and
       the screen owns its two server calls (see features/trash/journeyTrash.ts for which of them is
       landed and which waits on slice 1). */
    id: "trash",
    surface: "route",
    testId: "journey-trash-panel",
    when: (s) => s.route === "trash",
    render: () => <JourneyTrashPanel />,
  },
  {
    /* ORD-0436: 成员与权限. Same registry-mounted route as the bin above, so the shell stays shut for
       this screen too. The screen reads the two doors that are on the line today (the trip list and
       the per-trip action feed) and says out loud that the sharing door is not there yet. */
    id: "access",
    surface: "route",
    testId: "access-panel",
    when: (s) => s.route === "access",
    render: () => <AccessPanel />,
  },
];

/** The Trips sub-tabs, same order as the tab strip renders them. */
export const tripsTabPanels: V8PanelEntry<TripsTabScope>[] = [
  {
    id: "trips-itinerary",
    surface: "trips-tab",
    testId: "trip-itinerary",
    when: (s) => s.tab === "itinerary",
    render: (s) => (
      <TripItinerary
        data={s.data}
        onOpenCopilot={s.onOpenCopilot}
        onOpenActivity={s.onOpenActivity}
      />
    ),
  },
  {
    id: "trips-stay",
    surface: "trips-tab",
    testId: "hotels-panel",
    when: (s) => s.tab === "stay",
    render: (s) => <HotelsPanel journeyId={s.data.journey.id} onSelected={s.onRefresh} />,
  },
  {
    id: "trips-bookings",
    surface: "trips-tab",
    testId: "trips-commitments",
    when: (s) => s.tab === "bookings",
    render: (s) => (
      <>
        {/* Consumer flow first: paste a confirmation or enter it manually,
            review the parsed fields, then confirm.  The raw-JSON workbench is
            kept for developers behind an Advanced disclosure. */}
        <JourneyInboxWorkbench
          journeyId={s.data.journey.id}
          onConfirmed={async () => {
            s.onRefresh();
          }}
        />
        <div className="v8-booking-grid" data-testid="trips-commitments">
          {s.slots.walletCommitments()}
        </div>
        <details className="trips-advanced">
          <summary>{s.zh ? "高级 / 开发者" : "Advanced / developer"}</summary>
          {s.slots.bookingsAdvanced()}
        </details>
      </>
    ),
  },
  {
    id: "trips-spend",
    surface: "trips-tab",
    testId: "trips-spend",
    when: (s) => s.tab === "spend",
    render: (s) => <>{s.slots.spend()}</>,
  },
];

/**
 * The authoritative screen. The order of these six is load-bearing and was the reason the
 * version list got its own mount: `RecoveryRequiredPanel` renders before any plan panel so an
 * invalidated plan can never look healthy, and `PlanVersionHistoryPanel` renders whether or not
 * a plan was read, because a failed list read must not be answered with 「你还没有选定方案。」
 * (ORD-0386). A new panel that belongs on this screen goes in this table, not in the shell.
 */
export const authoritativePanels: V8PanelEntry<AuthoritativeScope>[] = [
  {
    id: "auth-recovery-required",
    surface: "authoritative",
    testId: "recovery-required-panel",
    when: () => true,
    render: (s) => (
      <RecoveryRequiredPanel
        journeyId={s.journeyId}
        recovery={s.recovery}
        flags={s.flags}
        onRefresh={s.onRefresh}
      />
    ),
  },
  {
    // Local markup, but it sits *between* two panels, so the order can only be kept by making
    // it an entry too. Same element, same condition, same place.
    id: "auth-no-plan",
    surface: "authoritative",
    testId: null,
    when: (s) => !s.loading && !s.plan,
    render: (s) => <>{s.slots.noPlanNotice()}</>,
  },
  {
    id: "auth-plan-version-history",
    surface: "authoritative",
    testId: "plan-version-history",
    when: () => true,
    render: (s) => <PlanVersionHistoryPanel journeyId={s.journeyId} />,
  },
  {
    id: "auth-journey",
    surface: "authoritative",
    testId: "v12-authoritative-panel",
    when: (s) => Boolean(s.plan),
    render: (s) => (
      <V12AuthoritativeJourneyPanel
        journeyId={s.journeyId}
        plan={s.plan}
        nowActivityId={s.twin?.current_activity_id}
        nextActivityId={s.nextActivityId ?? null}
        onTransition={s.onRefresh}
      />
    ),
  },
  {
    id: "auth-trip-studio",
    surface: "authoritative",
    testId: "v12-trip-studio",
    when: (s) => Boolean(s.plan),
    render: (s) => (
      <V12TripStudioPanel journeyId={s.journeyId} plan={s.plan} onAccepted={s.onRefresh} />
    ),
  },
  {
    id: "auth-preventive",
    surface: "authoritative",
    testId: "v14-preventive",
    when: (s) => Boolean(s.plan),
    render: (s) => <V14PreventivePanel journeyId={s.journeyId} refreshKey={s.plan?.id} />,
  },
  {
    id: "auth-recovery-truth",
    surface: "authoritative",
    testId: "v14-recovery-truth",
    when: (s) => Boolean(s.plan),
    render: (s) => (
      <V14RecoveryTruthPanel
        journeyId={s.journeyId}
        policy={s.policy}
        currency={s.currency}
        onAccepted={s.onRefresh}
      />
    ),
  },
];

/**
 * Panels that mount *inside* a page shell at a named anchor, rather than as a route sibling.
 *
 * This is the fourth surface, and it exists because "the page shells stay in the file that defines
 * them" (see the header of this file) otherwise left no way for a lane to add a panel *between* two
 * blocks of a shell without opening the shell.  The shell contributes one traversal at the anchor;
 * everything below it is table data.
 *
 * First entry: ORD-0384's pre-trip checklist, which the shell mounted directly under Today's
 * attention band.  Its position is load-bearing and its own comment said why, so the comment
 * travels with the entry instead of being left behind in a 4 500-line file.
 */
export const hubAnchorPanels: V8PanelEntry<HubAnchorScope>[] = [
  {
    id: "hub-pretrip-checklist",
    surface: "hub-anchor",
    testId: "pretrip-checklist",
    when: () => true,
    /* ORD-0384: the two outstanding things the contract can anchor, read from the payload this
       screen already holds -- the panel issues no second read of its own. It sits *after* the
       attention band on purpose: in the clear state the band hides its own kicker
       (design-tokens.css `.cn-attention-band[data-attention="clear"] .v8-kicker{display:none}`), so a
       bare "一切正常" rendered directly under a list of open items reads as a denial of the list above
       it. Live status answers first; the pre-trip list follows. */
    render: (s) => <PretripChecklistPanel sources={{ experience: s.data }} onNavigate={s.onNavigate} />,
  },
];

/** Renders a registry: one fragment per matching entry, keyed by the entry id. */
export function renderPanels<S>(entries: V8PanelEntry<S>[], scope: S): ReactNode {
  return entries.map((entry) =>
    entry.when(scope) ? <Fragment key={entry.id}>{entry.render(scope)}</Fragment> : null,
  );
}
