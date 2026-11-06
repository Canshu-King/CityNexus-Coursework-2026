import { setup } from "xstate";

export const journeyModeMachine = setup({
  types: {
    context: {} as { error: string | null },
    events: {} as
      | { type: "BOOTSTRAP" }
      | { type: "BOOTSTRAP_OK" }
      | { type: "EVENT_DETECTED" }
      | { type: "REPLANS_READY" }
      | { type: "ACCEPT" }
      | { type: "ACCEPTED" }
      | { type: "REJECT" }
      | { type: "REJECTED" }
      | { type: "FAIL"; message: string },
  },
}).createMachine({
  id: "journeyMode",
  initial: "idle",
  context: { error: null },
  states: {
    idle: { on: { BOOTSTRAP: "bootstrapping" } },
    bootstrapping: { on: { BOOTSTRAP_OK: "ready", FAIL: "error" } },
    ready: { on: { EVENT_DETECTED: "generatingReplan", FAIL: "error" } },
    generatingReplan: { on: { REPLANS_READY: "comparing", FAIL: "error" } },
    comparing: { on: { ACCEPT: "accepting", REJECT: "rejecting" } },
    accepting: { on: { ACCEPTED: "ready", FAIL: "error" } },
    rejecting: { on: { REJECTED: "ready", FAIL: "error" } },
    error: { on: { BOOTSTRAP: "bootstrapping" } },
  },
});
