import { Icon } from "../../components/v8/Icon";
import type { AgentRuntimeStatus } from "../../api/citynexus";
import { useLocale, useT } from "../../i18n/LocaleContext";
import { policyModeLabel, runtimeIdentityLabel } from "../../lib/displayVocabulary";

type Policy = { mode: "ADVISORY" | "ASSISTED" | "AUTONOMOUS"; version: number } | null;

export function ControlTowerPanel({
  policy,
  runtimeStatus,
  onActivate,
}: {
  policy: Policy;
  runtimeStatus: AgentRuntimeStatus | null;
  onActivate: () => Promise<void>;
}) {
  const t = useT();
  const { locale } = useLocale();
  const runtimeChain = runtimeStatus?.runtime_identity === "HERMES_NATIVE_MCP"
    ? [t("intel.deepseek"), t("intel.hermes"), "MCP", t("intel.gateway"), t("intel.domain")]
    : runtimeStatus?.runtime_identity === "NATIVE_DEEPSEEK_TOOL_LOOP"
      ? [t("intel.deepseek"), "CityNexus Direct", t("intel.gateway"), t("intel.domain")]
      : ["Deterministic fallback", "CityNexus Direct", t("intel.gateway")];
  return (
    <section className="v8-page">
      <div className="v8-page-intro compact">
        <div>
          <p className="v8-kicker">{t("intel.kicker")}</p>
          <h1>{t("intel.title")}</h1>
          <p className="v8-lede">{t("intel.lede")}</p>
        </div>
        <button className="v8-primary" onClick={() => void onActivate()}>
          {policy?.mode === "AUTONOMOUS" ? t("intel.autonomyActive") : t("intel.enableRecovery")}
        </button>
      </div>
      <div className="v8-intelligence-grid">
        <article className="v8-intel-hero">
          <span className="v8-status-pill violet">
            <Icon name="spark" />
            {t("intel.guardianReady")}
          </span>
          <h2>{t("intel.recoveryTitle")}</h2>
          <p>{t("intel.recoverySub")}</p>
          <div className="v8-chain">
            {runtimeChain.map((item, index) => (
              <span key={`${item}-${index}`}>{item}{index < runtimeChain.length - 1 && <b className="v8-chain-arrow">→</b>}</span>
            ))}
          </div>
          <div className="v8-runtime-card" data-testid="v17-runtime-status">
            <span>{t("intel.runtimeSource")}</span>
            <b>{runtimeStatus ? runtimeIdentityLabel(runtimeStatus.runtime_identity, locale) : t("intel.runtimeUnknown")}</b>
            <small>
              {runtimeStatus?.native_mcp_proven
                ? t("intel.nativeMcpProven")
                : t("intel.nativeMcpNotProven")}
            </small>
          </div>
        </article>
        <div className="v8-intel-facts">
          <div>
            <span>{t("intel.policy")}</span>
            <b>{policy ? policyModeLabel(policy.mode, locale) : t("intel.advisory")}</b>
            <small>{t("intel.versionPrefix")} {policy?.version ?? "—"} {t("intel.versionSuffix")}</small>
          </div>
          <div>
            <span>{t("intel.postActionGate")}</span>
            <b>{t("intel.feasible")}</b>
            <small>{t("intel.gateSub")}</small>
          </div>
          <div>
            <span>{t("intel.commitment")}</span>
            <b>{t("intel.protected")}</b>
            <small>{t("intel.commitmentSub")}</small>
          </div>
        </div>
      </div>
    </section>
  );
}
