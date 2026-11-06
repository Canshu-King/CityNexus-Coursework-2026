import type { ReactNode } from "react";

export type AgentRailStep = {
  id: string;
  label: string;
  status: "pending" | "active" | "done" | "approval" | "failed";
  /* This screen's own wording, built by the caller.  It used to be called `detail`, which is the
     name an upstream answer uses for its sentence, and a rail that renders a slot by that name
     cannot be told apart from one forwarding that sentence (Q64/Q71). */
  runMeta?: ReactNode;
};

export function AgentRunRail({ steps }: { steps: AgentRailStep[] }) {
  return (
    <ol className="cn-agent-rail" aria-label="CityNexus agent progress" aria-live="polite">
      {steps.map((step) => (
        <li key={step.id} className={`cn-agent-step ${step.status}`}>
          <span className="cn-agent-step-mark" aria-hidden="true" />
          <div>
            <b>{step.label}</b>
            {step.runMeta && <small>{step.runMeta}</small>}
          </div>
        </li>
      ))}
    </ol>
  );
}
