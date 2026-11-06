/**
 * ORD-0384 -- the pre-trip checklist, narrowed to what the contract can actually anchor.
 *
 * The first version derived nine candidate items from response fields. Measured against
 * `contracts/openapi.yaml`, only two of those fields are declared anywhere: the experience route types
 * `trip` and `journey` as bare objects, and the wallet route answers its 200 as `{type: object}` with no
 * properties, so seven of the nine names existed only in the bytes this one server happened to send
 * today. The order was re-scoped on that measurement: an item may appear only if the contract names its
 * field, and the classes that cannot be anchored must not reach the screen. They are recorded, with what
 * a real anchor would take, in `review/n3_0384_unanchored_list_r1.py` and the file it generates.
 *
 * So this module now holds exactly two rules, both reading the payload the screen already has:
 *   plan_feasibility_open    <- experience_flags.feasibility_failures  (contracts/openapi.yaml:1393)
 *   recovery_needs_decision  <- recovery.requires_user_decision       (contracts/openapi.yaml:1923)
 * Those two numbers are pinned against contract blob 177f3ad34f4198e87f9d8353b10d9724a816c55d; when
 * the document grows they go stale silently, which is what e2e case Y4 exists to catch.
 * It stores nothing and writes nothing. Completion is not a checkbox: the item disappears when the
 * server stops saying so, which is why there is no "mark done" control.
 *
 * Copy lives here on purpose -- `LocaleContext.tsx` is being edited by two other lanes.
 */

export type ChecklistBasis = {
  /** The endpoint whose response carries the field. `{journeyId}` is the placeholder the app fills. */
  endpoint: string;
  /** Dotted path inside that response, named as the contract names it. */
  field: string;
  /** The contract line that declares that field. An item without one is not allowed in this list. */
  anchor: { file: string; line: number };
};

export type ChecklistItem = {
  key: string;
  title: { zh: string; en: string };
  /** Why this is listed, phrased as the fact the server stated -- not an instruction invented here. */
  why: { zh: string; en: string };
  basis: ChecklistBasis;
  /** An existing screen that can act on it. This order adds no screen of its own. */
  target: { route: string; label: { zh: string; en: string } };
};

export type PretripSources = { experience: unknown };

const EXPERIENCE = "/api/v1/journeys/{journeyId}/experience";
const CONTRACT = "contracts/openapi.yaml";

type Bag = Record<string, any>;

const bag = (v: unknown): Bag | null => (v && typeof v === "object" ? (v as Bag) : null);

/** The only keys this module may ever produce; the gate asserts nothing else reaches the screen. */
export const ANCHORED_CHECKLIST_KEYS = ["plan_feasibility_open", "recovery_needs_decision"] as const;

/**
 * Each rule fires only when the field it names is in that state in the payload it was handed. A payload
 * that was never read yields no items, because "we could not look" must not render as "nothing is
 * missing" -- and must not render as an invented gap either.
 */
export function derivePretripChecklist(sources: PretripSources): ChecklistItem[] {
  const exp = bag(sources.experience);
  if (!exp) return [];

  const items: ChecklistItem[] = [];
  const flags = bag(exp.experience_flags) ?? {};
  const recovery = bag(exp.recovery);

  const failures = Array.isArray(flags.feasibility_failures) ? flags.feasibility_failures : [];
  if (failures.length > 0) {
    items.push({
      key: "plan_feasibility_open",
      title: { zh: "方案有未处理的可行性失败", en: "The plan has open feasibility failures" },
      why: {
        /* The server's own strings, counted and quoted, never rewritten into a cause we did not measure. */
        zh: `服务端在这一代方案上列出了 ${failures.length} 条可行性失败，最新一条是「${String(failures[failures.length - 1])}」。`,
        en: `The server lists ${failures.length} feasibility failure(s) on this plan generation; the latest reads "${String(failures[failures.length - 1])}".`,
      },
      basis: { endpoint: EXPERIENCE, field: "experience_flags.feasibility_failures",
               anchor: { file: CONTRACT, line: 1393 } },
      target: { route: "studio", label: { zh: "去看方案", en: "Review the plan" } },
    });
  }

  if (recovery?.requires_user_decision === true) {
    items.push({
      key: "recovery_needs_decision",
      title: { zh: "恢复方案等你决定", en: "A recovery is waiting on your decision" },
      why: {
        zh: "服务端把这一趟标为需要用户决定（`requires_user_decision` 为真），在你决定之前方案不会自己改。",
        en: "The server flags this journey as needing a user decision (`requires_user_decision`), and the plan will not change itself before you choose.",
      },
      basis: { endpoint: EXPERIENCE, field: "recovery.requires_user_decision",
               anchor: { file: CONTRACT, line: 1923 } },
      target: { route: "live", label: { zh: "去看恢复选项", en: "See the recovery options" } },
    });
  }

  return items;
}

/**
 * What the panel says when the derivation returns nothing.
 *
 * ORD-0401 ①: the empty state is one row -- the panel's own title above it, this sentence, and the
 * action. It used to be two sentences (62 zh characters) under a three-sentence note, measured at
 * 325 px tall on a 390x844 screen whose *hittable* region ends at 768 px (the fixed `v8-mobile-nav`
 * covers 768..844) and whose checklist anchor starts at 885 px. The row now stands at 143 px.
 *
 * The bounded claim survives: the subject of this sentence is the heading above it, 「服务端报出的两类
 * 待办」/ "The two things the server reports as outstanding", so an empty list still reads as "these
 * two, and only these two, came back clear" and never as "everything about your trip was checked and
 * passed". What was dropped is the restatement of that scope, which the heading already carries, and
 * the naming of the two classes -- both are still on screen in the note whenever at least one of them
 * is open, which is when the traveller needs to know which.
 */
export function emptyStateCopy(zh: boolean) {
  return zh ? "这一代都没有报出。" : "Neither one is open on this generation.";
}

/** The action the empty state carries. An empty list must still be something the traveller can do. */
export const EMPTY_STATE_ACTION = {
  route: "studio",
  label: { zh: "去看这一代方案", en: "Review this plan generation" },
} as const;
