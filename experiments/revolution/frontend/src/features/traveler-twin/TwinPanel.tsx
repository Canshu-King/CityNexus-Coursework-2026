import { Icon } from "../../components/v8/Icon";
import type { V8Data } from "../../app/v8Types";
import type { TravelerCapability } from "../../api/citynexus";
import { useT } from "../../i18n/LocaleContext";

function capabilityLabel(item: TravelerCapability, codeLabel: (code: string) => string): string {
  const params = item.params ?? {};
  const suffix = typeof params.max_m === "number" ? ` · ${params.max_m} m` : typeof params.max_min === "number" ? ` · ${params.max_min} min` : "";
  return `${codeLabel(item.code)}${suffix}`;
}

/**
 * A dictionary lookup for a server-side token, with two rules the raw renders used to break:
 * the traveller reads their own language, and a code the product has no name for says so
 * instead of silently showing the enum. `t()` falls back to the key itself when a lookup
 * misses (LocaleContext.tsx:1644), so comparing the answer against the question is what
 * keeps a missing key off the screen.
 */
function useTokenLabels() {
  const t = useT();
  return (prefix: string, unknownKey: string) => (code: string) => {
    const key = `${prefix}${code}`;
    const value = t(key);
    return value === key ? t(unknownKey, { code }) : value;
  };
}

function CapabilityList({ items }: { items: TravelerCapability[] }) {
  const t = useT();
  const label = useTokenLabels();
  const capabilityLabelOf = label("twin.cap.", "twin.capUnknown");
  const severityLabel = label("twin.sev.", "twin.sevUnknown");
  if (!items.length) return <p className="v8-empty-note">{t("twin.noCapabilityFacts")}</p>;
  return <ul className="v8-capability-list">{items.map((item) => <li key={`${item.scope}:${item.code}`}><strong>{capabilityLabel(item, capabilityLabelOf)}</strong><span>{severityLabel(item.severity)}</span><small>{item.confirmed ? t("twin.confirmedByTraveler") : t("twin.statedNeedsConfirm")}</small></li>)}</ul>;
}

export function TwinPanel({ data }: { data: V8Data }) {
  const t = useT();
  /* A dimension that happens to be a capability code (the server puts confirmed capabilities into
     `explicit_dimensions` too -- backend/app/execution/traveler_twin.py:349) reads with its capability
     name rather than as unregistered. Only a token in neither vocabulary says so out loud. */
  const dimensionLabel = (code: string) => {
    const asDimension = t(`twin.dim.${code}`);
    if (asDimension !== `twin.dim.${code}`) return asDimension;
    const asCapability = t(`twin.cap.${code}`);
    if (asCapability !== `twin.cap.${code}`) return asCapability;
    return t("twin.dimUnknown", { code });
  };
  const explicit = data.traveler_twin?.explicit_dimensions ?? [];
  const learned = data.traveler_twin?.learned_soft_dimensions ?? [];
  const capabilities = data.traveler_twin?.explicit_capabilities ?? [];
  const hard = capabilities.filter((item) => item.severity === "HARD");
  const softer = capabilities.filter((item) => item.severity !== "HARD");
  return <section className="v8-page"><div className="v8-page-intro compact"><div><p className="v8-kicker">{t("twin.kicker")}</p><h1>{t("twin.title")}</h1><p className="v8-lede">{t("twin.lede")}</p></div></div><div className="v8-twin-grid"><article className="v8-twin-card"><span className="v8-twin-label explicit">{t("twin.explicit")}</span><h2>{explicit.length ? explicit.map(dimensionLabel).join(" · ") : t("twin.noExplicitPrefs")}</h2><p>{t("twin.explicitFrom")}</p><small>{t("twin.explicitSub")}</small><h3 className="v8-capability-heading">{t("twin.capabilityNeeds")}</h3><CapabilityList items={capabilities} /></article><article className="v8-twin-card"><span className="v8-twin-label learned">{t("twin.learned")}</span><h2>{learned.length ? learned.map(dimensionLabel).join(" · ") : t("twin.noLearnedSignals")}</h2><p>{t("twin.learnedFrom")}</p><small>{t("twin.learnedSub")}</small><h3 className="v8-capability-heading">{t("twin.learnedCapabilityState")}</h3><p>{hard.length ? t("twin.hardNeedsRemain", { n: hard.length }) : t("twin.hardNeverInferred")}</p>{softer.length ? <CapabilityList items={softer} /> : null}</article><aside className="v8-boundary-card"><Icon name="spark" /><b>{t("twin.authorityBoundary")}</b><p>{t("twin.boundarySub")}</p><span>{t("twin.provenance")}</span><small className="v8-capability-disclosure">{t("twin.unknownEvidenceNote")}</small></aside></div></section>;
}
