/**
 * ORD-0511 -- the stay acquisition door, read the way the traveller meets it.
 *
 * Why this is a module and not three lines in the panel: the door answers in its own vocabulary
 * (`generation.outcome`), and a screen that printed that word would be putting a machine name on a
 * traveller's page. The table below is the one place that turns each name the server can answer with
 * into a sentence, and anything not in the table is reported as unfamiliar rather than translated by
 * guesswork -- a new server word must not arrive on this screen wearing someone's best guess.
 *
 * Nothing here invents a second candidate list: the panel re-reads the shell's own data after a
 * successful call, so the cards a traveller sees are the ones the read door answered with.
 */

export type StayOutcome =
  | "OPTIONS_FOUND"
  | "NOT_APPLICABLE_ZERO_NIGHTS"
  | "NO_OFFERS_FROM_PROVIDER"
  | "STAY_BUDGET_NOT_DECLARED"
  | "ALL_CANDIDATES_REJECTED";

/** What the server said, in the traveller's language, per outcome name. */
export function stayOutcomeCopy(code: string, zh: boolean): string {
  const table: Record<string, [string, string]> = {
    OPTIONS_FOUND: [
      "这一次拿到了可以选的住宿候选。",
      "This run produced stays you can choose from.",
    ],
    NOT_APPLICABLE_ZERO_NIGHTS: [
      "这趟行程在同一天里开始和结束，没有一个夜晚需要住宿，所以这里没有候选可列。",
      "This trip starts and ends inside one day, so there is no night to house and no candidate to list.",
    ],
    NO_OFFERS_FROM_PROVIDER: [
      "这台服务器这次没有交回任何住宿数据，所以这里没有一个候选可列。",
      "This server returned no accommodation data this time, so there is no candidate to list.",
    ],
    STAY_BUDGET_NOT_DECLARED: [
      "这条行程没有声明住宿预算，所以这台服务器没有可对住宿做取舍的界线。",
      "No lodging budget was declared for this trip, so this server has no line to judge stays against.",
    ],
    ALL_CANDIDATES_REJECTED: [
      "有住宿数据，但这一轮没有一枚满足这趟行程的硬条件。",
      "Accommodation data came back, but no stay met this trip's hard conditions in this run.",
    ],
  };
  const hit = table[code];
  if (!hit) {
    return zh
      ? "这台服务器报回一个这一屏还没有对应词形的结果名，所以我不替它编一句解释。"
      : "This server answered with a result name this screen has no wording for, so it is not going to get an invented explanation here.";
  }
  return zh ? hit[0] : hit[1];
}

/** True when the outcome is one this screen may offer a choice on. `OPTIONS_FOUND` is the only one. */
export function outcomeOffersChoice(code: string | null | undefined): boolean {
  return code === "OPTIONS_FOUND";
}

export type GenerateDoor =
  | { kind: "ok"; outcome: string | null; ranked: number | null; offers: number | null }
  /** A refusal this screen can name: the envelope's own `reason_code`, never an identifier. */
  | { kind: "refused"; http_status: number; reason_code: string | null; message_key: string | null }
  /** The request never reached an answer -- connection refused, timeout, proxy's own 5xx. */
  | { kind: "unreadable"; http_status: number | null };

/**
 * Ask this trip for stay candidates and report what came back.
 *
 * `offers` and `ranked` are read out of `generation`, which is where this door actually puts them --
 * measured against a live stack rather than assumed from the payload's top level.
 */
export async function generateStayOptions(journeyId: string): Promise<GenerateDoor> {
  let response: Response;
  try {
    response = await fetch(`/api/v1/journeys/${journeyId}/stay-options/generate`, { method: "POST" });
  } catch {
    return { kind: "unreadable", http_status: null };
  }
  const text = await response.text();
  let body: unknown = null;
  try {
    body = JSON.parse(text);
  } catch {
    body = null;
  }
  if (!response.ok) {
    const error = (body as { error?: Record<string, unknown> } | null)?.error ?? null;
    return {
      kind: "refused",
      http_status: response.status,
      reason_code: typeof error?.reason_code === "string" ? error.reason_code : null,
      message_key: typeof error?.message_key === "string" ? error.message_key : null,
    };
  }
  const generation = (body as { generation?: Record<string, unknown> } | null)?.generation ?? null;
  const cell = (key: string) => (generation ? generation[key] : undefined);
  const outcome = cell("outcome");
  const ranked = cell("options_ranked");
  const offers = cell("offers_from_provider");
  return {
    kind: "ok",
    outcome: typeof outcome === "string" ? outcome : null,
    ranked: typeof ranked === "number" ? ranked : null,
    offers: typeof offers === "number" ? offers : null,
  };
}
