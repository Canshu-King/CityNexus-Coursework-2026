import type { Meta, StoryObj } from "@storybook/react-vite";
import { ActivityCard, AgentAction, CommitmentCard, EdgeStatus, ImpactPreview, JourneyLeg, LiveJourney, OfflineBanner, RiskBanner } from "../features/execution/V12StatePrimitives";

const meta = { title: "V12/State primitives", parameters: { layout: "centered" } } satisfies Meta;
export default meta;
type Story = StoryObj<typeof meta>;

export const OperationsState: Story = {
  render: () => <div style={{ display: "grid", gap: 12, width: 560 }}>
    <LiveJourney now="Tianjin West Station" next="Haihe Riverside Hotel" state="IN_PROGRESS" />
    <ActivityCard title="Tianjin West Station" state="COMPLETED" locked />
    <JourneyLeg mode="TRANSIT" durationMin={18} />
    <CommitmentCard title="Arrival rail reservation" />
    <ImpactPreview passed summary="C4 protected · +8 min · CNY 0" />
    <RiskBanner risk="AT_RISK">Weather provider is degraded; frozen mobility facts remain usable.</RiskBanner>
    <AgentAction label="Reschedule the flexible museum stop" decision="AUTONOMOUS_WITHIN_ENVELOPE" />
    <OfflineBanner />
    <EdgeStatus />
  </div>,
};
