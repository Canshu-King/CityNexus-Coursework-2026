import type { Meta, StoryObj } from "@storybook/react-vite";
import { V12AuthoritativeJourneyPanel, type ExecutionState } from "../features/execution/V12AuthoritativeJourneyPanel";

const states: ExecutionState[] = [
  "NOT_STARTED",
  "EN_ROUTE",
  "ARRIVED",
  "IN_PROGRESS",
  "ACTIVE",
  "COMPLETED",
  "SKIPPED",
];

function fixture(state: ExecutionState) {
  return {
    id: "00000000-0000-4000-8000-000000000012",
    mobility_snapshot_id: "v12-story-frozen-evidence",
    activities: [
      {
        id: "00000000-0000-4000-8000-000000000013",
        sequence_no: 1,
        title: "Tianjin West Station",
        activity_type: "arrival",
        start_at: "2026-10-01T08:00:00+08:00",
        end_at: "2026-10-01T08:20:00+08:00",
        estimated_cost: { amount_minor: 0, currency: "CNY" },
        status: state,
        locked: true,
      },
      {
        id: "00000000-0000-4000-8000-000000000014",
        sequence_no: 2,
        title: "Haihe Riverside Hotel",
        activity_type: "check_in",
        start_at: "2026-10-01T14:00:00+08:00",
        end_at: "2026-10-01T14:20:00+08:00",
        estimated_cost: { amount_minor: 0, currency: "CNY" },
        status: "NOT_STARTED" as const,
        locked: true,
      },
    ],
  };
}

const meta = {
  title: "V12/Authoritative execution",
  component: V12AuthoritativeJourneyPanel,
  parameters: { layout: "fullscreen" },
} satisfies Meta<typeof V12AuthoritativeJourneyPanel>;

export default meta;
type Story = StoryObj<typeof meta>;

export const StateMatrix: Story = {
  args: { plan: fixture(states[0]) },
  render: () => (
    <div className="v12-story-grid">
      {states.map((state) => {
        const plan = fixture(state);
        return (
          <V12AuthoritativeJourneyPanel
            key={state}
            plan={plan}
            nowActivityId={plan.activities[0].id}
            nextActivityId={plan.activities[1].id}
          />
        );
      })}
    </div>
  ),
};

export const OfflineReadOnly: Story = {
  args: {
    plan: fixture("ARRIVED"),
    nowActivityId: "00000000-0000-4000-8000-000000000013",
    nextActivityId: "00000000-0000-4000-8000-000000000014",
    offline: true,
  },
};
