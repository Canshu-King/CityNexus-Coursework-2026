/* ORD-0525 -- the calendar door's client: one tap on the itinerary, one real .ics on disk.
 *
 * This is the sibling of `../checkin/checkins.ts`, and it keeps that module's two rules because they
 * are the ones this project had to learn the hard way:
 *
 * 1. The path carries the `/api/v1` prefix the server actually mounts, not the contract's own path.
 *    Fetching `contracts/openapi.yaml`'s `/journeys/{journey_id}/calendar.ics` would hit the SPA
 *    fallback and return a 200 whose body is HTML -- which is exactly how ORD-0263 turned a refused
 *    connection into "网络不可用" on a phone.  A 200 is therefore not enough here: the content type is
 *    checked, and anything that is not `text/calendar` is refused by name rather than downloaded.
 * 2. A refusal's machine name is switched on, never rendered.  `CALENDAR_JOURNEY_IN_RECYCLE_BIN`
 *    reaching a Chinese screen would be the exposure defect this order's own acceptance forbids, so
 *    the reason code leaves this module only as a key the screen looks up in its own dictionary.
 *
 * The event count is read out of the bytes that came back (`BEGIN:VEVENT` occurrences), not out of a
 * response header.  `X-CityNexus-Ics-Events` exists and says the same thing, but a screen that quotes
 * a header while showing a body has two authorities for one fact -- and the body is the artefact the
 * traveller keeps.
 */

/** The door this module calls.  One place, so a path typo cannot become a silent HTML download. */
export const CALENDAR_PATH = (journeyId: string) =>
  `/api/v1/journeys/${encodeURIComponent(journeyId)}/calendar.ics`;

/** The media type the contract promises for this door, and the only one a download may start from. */
export const CALENDAR_MEDIA_TYPE = "text/calendar";

/**
 * What can go wrong between a tap and a file, named.  The first three are the server's own refusal
 * names (`contracts/openapi.yaml`, the `calendar.ics` operation); the last two are facts about the
 * *response* this client can see and the server cannot: bytes that are not a calendar, and a body
 * whose refusal it could not name.
 */
export type CalendarRefusal =
  | "CALENDAR_JOURNEY_IN_RECYCLE_BIN"
  | "JOURNEY_NOT_FOUND"
  | "CALENDAR_SOURCE_UNREADABLE"
  | "NOT_CALENDAR_BYTES"
  | "UNRECOGNISED";

export type CalendarOutcome =
  | { kind: "exported"; events: number; bytes: number; fileName: string }
  | { kind: "refused"; refusal: CalendarRefusal; status: number }
  | { kind: "unreachable" };

/** The dictionary key for a refusal, so the screen never has to know what a reason code looks like. */
export function calendarRefusalKey(refusal: CalendarRefusal): string {
  const keys: Record<CalendarRefusal, string> = {
    CALENDAR_JOURNEY_IN_RECYCLE_BIN: "calendar.refused.bin",
    JOURNEY_NOT_FOUND: "calendar.refused.not_found",
    CALENDAR_SOURCE_UNREADABLE: "calendar.refused.unreadable",
    NOT_CALENDAR_BYTES: "calendar.refused.not_calendar",
    UNRECOGNISED: "calendar.refused.unknown",
  };
  return keys[refusal];
}

const KNOWN_REFUSALS: CalendarRefusal[] = [
  "CALENDAR_JOURNEY_IN_RECYCLE_BIN",
  "JOURNEY_NOT_FOUND",
  "CALENDAR_SOURCE_UNREADABLE",
];

/** VEVENTs in the object, counted from the bytes.  No header is consulted. */
export function countCalendarEvents(body: string): number {
  return (body.match(/BEGIN:VEVENT/g) ?? []).length;
}

/** The name the server asked for, from `filename*=UTF-8''...`; absent means no name was offered. */
export function fileNameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const extended = /filename\*=(?:UTF-8|utf-8)''([^;]+)/.exec(header);
  if (extended) {
    try {
      return decodeURIComponent(extended[1].trim());
    } catch {
      /* A percent-escape the server did not mean to send is not a reason to lose the download; fall
         through to the plain form rather than guessing a name here. */
    }
  }
  const plain = /filename="?([^";]+)"?/.exec(header);
  return plain ? plain[1].trim() : null;
}

/**
 * Ask the server for this trip's calendar and hand the bytes to the browser as a download.
 *
 * Nothing is composed locally: the file that lands on disk is the byte sequence this door returned,
 * and the screen is told how many events those bytes actually hold.  A caller that wants to claim
 * "the export worked" has to be able to count VEVENTs in what it wrote, which is why the count is
 * derived here from `body` and not from a header.
 */
export async function exportJourneyCalendar(journeyId: string): Promise<CalendarOutcome> {
  let response: Response;
  try {
    response = await fetch(CALENDAR_PATH(journeyId));
  } catch {
    // A refused connection has exactly one honest sentence, and it is not a server refusal (ORD-0263).
    return { kind: "unreachable" };
  }

  if (!response.ok) {
    let refusal: CalendarRefusal = "UNRECOGNISED";
    try {
      const body = (await response.json()) as { error?: { reason_code?: unknown } };
      const code = body?.error?.reason_code;
      if (typeof code === "string" && (KNOWN_REFUSALS as string[]).includes(code)) {
        refusal = code as CalendarRefusal;
      }
    } catch {
      refusal = "UNRECOGNISED";
    }
    return { kind: "refused", refusal, status: response.status };
  }

  const media = (response.headers.get("content-type") ?? "").toLowerCase();
  if (!media.startsWith(CALENDAR_MEDIA_TYPE)) {
    // A 200 that is not a calendar is the SPA-fallback shape; it must never reach the traveller's file.
    return { kind: "refused", refusal: "NOT_CALENDAR_BYTES", status: response.status };
  }

  const buffer = await response.arrayBuffer();
  const text = new TextDecoder("utf-8").decode(buffer);
  if (!text.startsWith("BEGIN:VCALENDAR")) {
    return { kind: "refused", refusal: "NOT_CALENDAR_BYTES", status: response.status };
  }

  const fileName =
    fileNameFromDisposition(response.headers.get("content-disposition")) ?? "calendar.ics";
  const blob = new Blob([buffer], { type: CALENDAR_MEDIA_TYPE });
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = fileName;
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    // Revoked on the next tick: releasing the URL before the click lands cancels the download.
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  return { kind: "exported", events: countCalendarEvents(text), bytes: buffer.byteLength, fileName };
}
