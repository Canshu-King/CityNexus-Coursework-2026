/**
 * ORD-0382 — the waiting state's own surface, above the router.
 *
 * The reference product makes 生成中 a screen of its own (`activity_journey_planning_waiting`) rather
 * than a thing that only exists inside whoever opened it, and gives it two doors: 查看会话 and
 * 继续生成.  This strip is the minimum that makes the same promise true here: a run started from the
 * planning sheet stays visible, stays counted, and stays continuable while the traveller is on
 * Today, on the map, or reading the session -- and the one control that stops it is inside the sheet,
 * not hidden behind this strip's ×.
 *
 * It deliberately renders no numbers it cannot prove: the seconds come from the run's own start, the
 * step words are the phases `aiGenerationRun` really awaits, and a settled outcome borrows
 * `statusCopy()` so the strip and the sheet can never disagree about what happened.
 */

import { useEffect, useState } from "react";
import { useLocale, useT } from "../../i18n/LocaleContext";
import {
  dismissRun,
  PHASES,
  unwatchRun,
  useAiRun,
  watchRun,
} from "./aiGenerationRun";
import { statusCopy } from "./PlanWithAiSheet";

export function PlanAiRunStrip({
  journeyId,
  onOpenRun,
  onViewChat,
}: {
  journeyId: string;
  /** Re-open the planning sheet on the run this strip is reporting. */
  onOpenRun: () => void;
  onViewChat?: () => void;
}) {
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const t = useT();
  const run = useAiRun();
  const [elapsed, setElapsed] = useState(0);

  const mine = run.journeyId === journeyId;
  const shown = mine && (run.running || run.unread) && !run.watched;

  useEffect(() => {
    if (!run.running) {
      setElapsed(0);
      return;
    }
    const tick = () => setElapsed(Math.max(0, Math.floor((Date.now() - run.startedAt) / 1000)));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [run.running, run.startedAt]);

  if (!shown) return null;

  const phaseRow = run.phase ? PHASES.find((item) => item.key === run.phase) : null;
  const outcome = run.result ? statusCopy(run.result.status, zh, run.result.planCount ?? 0) : null;

  return (
    <div
      className="plan-ai-run-strip"
      data-testid="plan-ai-run-strip"
      data-run-state={run.running ? "running" : "settled"}
      data-run-phase={run.phase ?? ""}
      data-run-seconds={elapsed}
      data-run-id={run.runId}
      role="status"
      aria-live="polite"
    >
      <div className="plan-ai-run-copy">
        <b>
          {run.running
            ? t("agent.aiRunStripBusy")
            : outcome
              ? outcome.title
              : t("agent.aiRunStripDone")}
        </b>
        {run.running && (
          <small data-testid="plan-ai-run-line">
            {phaseRow ? (zh ? phaseRow.zh : phaseRow.en) : t("agent.aiRunWorking")} ·{" "}
            {t("agent.aiRunStripWaited", { seconds: elapsed })}
          </small>
        )}
        {/* The closing state of an interruption is spelled out in the sheet, where the read-back
            happens -- this strip only says that something is finished and is not a number of plans. */}
      </div>
      <div className="plan-ai-run-actions">
        {onViewChat && (
          <button
            type="button"
            className="v8-outline-button"
            data-testid="plan-ai-run-chat"
            onClick={() => {
              unwatchRun();
              onViewChat();
            }}
          >
            {t("agent.aiRunViewChat")}
          </button>
        )}
        <button
          type="button"
          className="v8-primary-button"
          data-testid="plan-ai-run-resume"
          onClick={() => {
            watchRun();
            onOpenRun();
          }}
        >
          {run.running ? t("agent.aiRunContinue") : t("agent.aiRunOpenOutcome")}
        </button>
      </div>
    </div>
  );
}
