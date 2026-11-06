const SESSION_KEY = "citynexus.traveler.bearer";
const CHANGE_EVENT = "citynexus.traveler.bearer.changed";

/**
 * The browser stores only the opaque signed bearer. Member identity is always
 * resolved by the server from that token; no member_id or role in web storage
 * is treated as authority.
 */
export function readTravelerBearer(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
}

export function storeTravelerBearer(token: string): void {
  if (typeof window === "undefined") return;
  window.sessionStorage.setItem(SESSION_KEY, token);
  window.dispatchEvent(new CustomEvent(CHANGE_EVENT));
}

export function clearTravelerBearer(): void {
  if (typeof window === "undefined") return;
  window.sessionStorage.removeItem(SESSION_KEY);
  window.dispatchEvent(new CustomEvent(CHANGE_EVENT));
}

/**
 * Same-tab sessionStorage writes do not emit the browser storage event. Poll
 * the opaque bearer as a small privacy boundary and also listen for the
 * explicit events emitted by the session helpers.
 */
export function subscribeTravelerBearer(listener: (bearer: string | null) => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  let previous = readTravelerBearer();
  const check = () => {
    const next = readTravelerBearer();
    if (next === previous) return;
    previous = next;
    listener(next);
  };
  const onSignal = () => check();
  window.addEventListener(CHANGE_EVENT, onSignal);
  window.addEventListener("storage", onSignal);
  const interval = window.setInterval(check, 100);
  return () => {
    window.removeEventListener(CHANGE_EVENT, onSignal);
    window.removeEventListener("storage", onSignal);
    window.clearInterval(interval);
  };
}

export function travelerAuthHeaders(): Record<string, string> {
  const token = readTravelerBearer();
  return token ? { Authorization: `Bearer ${token}` } : {};
}
