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

/**
 * Which condition refused, in the server's own token, read out of the parsed envelope.
 *
 * ORD-0465 lifted this out of `features/trash/journeyTrash.ts`, where one screen owned it, because a
 * second screen now has the same need: a door that answers `409 CONFLICT` for six different reasons
 * can only be named from `error.reason_code`, and two copies of the reading is two chances to read it
 * differently. `backend/app/main.py`'s `http_error` forwards `getattr(exc, "reason_code", null)` into
 * the envelope, which is the field this looks at.
 *
 * Returning `null` is not a failure and must never be rendered as one condition: it means this device
 * did not read a name, and the screen says that instead of picking a sentence.
 */
export function refusalReasonCode(cause: unknown): string | null {
  if (!(cause instanceof RejectionWithFacts)) return null;
  const envelope = cause.envelope;
  if (typeof envelope !== "object" || envelope === null) return null;
  const error = (envelope as Record<string, unknown>).error;
  if (typeof error === "object" && error !== null) {
    const code = (error as Record<string, unknown>).reason_code;
    if (typeof code === "string" && code.trim()) return code;
  }
  const top = (envelope as Record<string, unknown>).reason_code;
  return typeof top === "string" && top.trim() ? top : null;
}

/** The details the same envelope carries beside the name -- the server's numbers, not this device's. */
export function refusalDetails(cause: unknown): Record<string, unknown> | null {
  if (!(cause instanceof RejectionWithFacts)) return null;
  const envelope = cause.envelope;
  if (typeof envelope !== "object" || envelope === null) return null;
  const holder = (envelope as Record<string, unknown>).error;
  const source = typeof holder === "object" && holder !== null ? holder : envelope;
  const details = (source as Record<string, unknown>).details;
  return typeof details === "object" && details !== null
    ? (details as Record<string, unknown>)
    : null;
}
