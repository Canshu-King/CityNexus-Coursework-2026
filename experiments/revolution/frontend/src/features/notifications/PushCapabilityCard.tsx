import { useEffect, useState } from "react";
import { useT } from "../../i18n/LocaleContext";

type Props = { travelerId: string };
type PushConfig = { capability: string; public_key: string | null; record_fallback: boolean };

/* ORD-0251: this card used to render the server's own label (`RECORD_ONLY`) straight into the
   traveller's screen.  The vocabulary is `push_capability()` in backend/app/integrations/push.py --
   three answers today -- and an answer this file does not know is reported as unknown, never echoed
   back as if it were prose.  The raw value stays in data-capability for gates and bug reports. */
const CAPABILITY_KEYS: Record<string, { state: string; help: string }> = {
  RECORD_ONLY: { state: "push.capRecordOnly", help: "push.capRecordOnlyHelp" },
  DEPENDENCY_MISSING: { state: "push.capDependencyMissing", help: "push.capDependencyMissingHelp" },
  WEB_PUSH_CONFIGURED: { state: "push.capWebPushConfigured", help: "push.capWebPushConfiguredHelp" },
};
const UNKNOWN_CAPABILITY = { state: "push.capUnknown", help: "push.capUnknownHelp" };

function decodeKey(value: string): Uint8Array<ArrayBuffer> {
  const padded = `${value}${"=".repeat((4 - (value.length % 4)) % 4)}`;
  const binary = window.atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

export function PushCapabilityCard({ travelerId }: Props) {
  const t = useT();
  const [config, setConfig] = useState<PushConfig | null>(null);
  /* Only an action writes this; the standing line is derived from what the read answered, so a
     capability we have already learned is never described as "still checking". */
  const [message, setMessage] = useState<string | null>(null);
  /* A failed read is not a read still in flight: without this the status line sat on "CHECKING"
     forever after the fetch threw, which is the same class of untruth ORD-0251 was filed for. */
  const [unreadable, setUnreadable] = useState(false);
  const keys = config ? CAPABILITY_KEYS[config.capability] ?? UNKNOWN_CAPABILITY : null;

  useEffect(() => {
    void fetch(`/api/v1/travelers/${travelerId}/push-config`)
      .then((response) => response.json() as Promise<PushConfig>)
      .then(setConfig)
      .catch(() => {
        setUnreadable(true);
        setMessage(t("push.unavailable"));
      });
  }, [travelerId]);

  const enable = async () => {
    if (!config?.public_key || !("serviceWorker" in navigator) || !("PushManager" in window)) {
      setMessage(t("push.noKey"));
      return;
    }
    try {
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: decodeKey(config.public_key),
      });
      const response = await fetch(`/api/v1/travelers/${travelerId}/push-subscriptions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(subscription.toJSON()),
      });
      if (!response.ok) throw new Error("subscription rejected");
      setMessage(t("push.saved"));
    } catch {
      setMessage(t("push.notEnabled"));
    }
  };

  return (
    <article
      className="v8-me-card"
      data-testid="push-capability"
      data-capability={config?.capability ?? ""}
    >
      <span className="v8-kicker">{t("push.kicker")}</span>
      <strong>
        {keys ? t(keys.state) : unreadable ? t("push.capUnread") : t("push.checkingState")}
      </strong>
      <p>{message ?? t(keys ? keys.help : "push.checking")}</p>
      <button className="v8-secondary" type="button" onClick={() => void enable()}>
        {t("push.enable")}
      </button>
    </article>
  );
}
