/**
 * ORD-0423 defect two: the bookings screen printed the server's ISO-8601 straight to the traveller
 * (`Nexus River Cruise Pier · 2026-10-10T15:30:00+08:00`).  The instant is rendered in the traveller's
 * own language here instead, in the device's zone, the same shape `V8Experience.tsx` already uses.
 *
 * A value we cannot read as an instant is returned as-is rather than as `pending`: the server did say
 * something about time, so calling it "时间待定" would be a false statement on the screen.
 */
const INSTANT = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/;

export function formatWhen(
  at: string | null | undefined,
  opts: { zh: boolean; pending: string },
): string {
  const raw = (at ?? "").trim();
  if (!raw) return opts.pending;
  if (!INSTANT.test(raw)) return raw;
  const when = new Date(raw);
  if (Number.isNaN(when.getTime())) return raw;
  return new Intl.DateTimeFormat(opts.zh ? "zh-CN" : "en-GB", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(when);
}
