import { parseJsonOrNull, readResponseFacts, type RawFacts } from "./rawFacts";

/**
 * ORD-0345 (Q64=1): a rejection a panel cannot name used to be printed as it arrived, so an upstream
 * sentence reached the traveller. This is the one carrier those panels share: it keeps what this
 * device measured and the parsed envelope, and it keeps the sentence only as an internal string for
 * the machine-token branches to match on -- never as what gets rendered.
 *
 * Deliberately not a second classification channel: it decides nothing about the failure. Naming the
 * condition stays `lib/failureAttribution`'s job, and the fields a screen may show come from
 * `components/CnxMeasuredFacts`.
 */
export class RejectionWithFacts extends Error {
  readonly facts: RawFacts;
  readonly envelope: unknown;
  readonly http_status: number;

  constructor(facts: RawFacts, envelope: unknown, status: number) {
    super(
      typeof envelope === "object" && envelope !== null
        ? String(
            (envelope as Record<string, unknown>).error ??
              (envelope as Record<string, unknown>).detail ??
              `HTTP ${status}`,
          )
        : `HTTP ${status}`,
    );
    this.name = "RejectionWithFacts";
    this.facts = facts;
    this.envelope = envelope;
    this.http_status = status;
  }
}

/**
 * The refusal half of a response, read once: the answer's text is parsed for machine tokens and for
 * the contract receipt field, and is then dropped -- the byte count is what survives, not the prose.
 */
export async function rejectionWithFacts(response: Response): Promise<RejectionWithFacts> {
  const { text, facts } = await readResponseFacts(response);
  return new RejectionWithFacts(facts, parseJsonOrNull(text), response.status);
}
