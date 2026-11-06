/**
 * ORD-0193 -- preparing a destination, as a flow the traveller can see on a phone.
 *
 * Every number and every word here comes from the server's own projections:
 *   `GET /destinations/capability` -> { capability, reason_code, recommended_action, coverage,
 *                                       missing_categories, candidates, provider_status? }
 *   `POST/GET /catalog-hydrations` -> { status, phase, outcome, terminal, elapsed_ms, retryable,
 *                                       error{code,message_key,reason_code,message} }
 *
 * Three things this screen is forbidden to do, all measured elsewhere in this project:
 *
 *  1. **Invent a denominator.**  The job record has no `progress` field on purpose (the backend's
 *     docstring says a percentage would be a claim about a queue this process cannot see), so this
 *     screen shows `phase` + `elapsed_ms` and never a bar.  An invented progress step was a defect
 *     here once already.
 *  2. **Say "network unavailable" about a structural state.**  `NO_MATCH`, `AMBIGUOUS` and an
 *     unavailable provider are server-known facts, not connectivity (R14-P, reverse face).  The
 *     copy for those comes from `reason_code`, never from a fetch failure.
 *  3. **Close silently or do nothing.**  Every terminal state ends in a real action -- prepare,
 *     choose another destination, retry, or go and plan -- and the action comes from the server's
 *     `recommended_action` where it exists.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useT } from "../../i18n/LocaleContext";
import { deviceReadout } from "../../lib/failureAttribution";
import { PrepAbandonSurvey } from "./PrepAbandonSurvey";
import { failureSentence, nameTheFailure } from "./prepFailureCopy";
import { STOP_ACTION_KEY, SURVEY_COPY, appendSurveyLog } from "./prepAbandonSurveyCopy";
import {
  cityNexusApi,
  type CatalogHydrationOperation,
  type DestinationCapability,
} from "../../api/citynexus";

type Props = {
  journeyId?: string | null;
  /** Hand the traveller on to planning once the destination is ready. */
  onOpenPlan?: () => void;
  initialQuery?: string;
};

/** The server's structural verdicts.  Anything unrecognised is shown as itself, not guessed at. */
const CAPABILITY_READY = "READY";

export function DestinationPreparationPanel({ journeyId, onOpenPlan, initialQuery = "" }: Props) {
  const t = useT();
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");

  const [query, setQuery] = useState(initialQuery);
  const [capability, setCapability] = useState<DestinationCapability | null>(null);
  const [capabilityError, setCapabilityError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [job, setJob] = useState<CatalogHydrationOperation | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [surveyOpen, setSurveyOpen] = useState(false);

  /* A survey belongs to the job it was opened for, so a *new* operation closes it. It does not close
     when the job terminalises underneath: the probe measured the server answering 云和县 ~4 s after
     the sheet opened, and dropping the sheet there would throw away an answer half-written. The stop
     request is idempotent (`POST /catalog-hydrations/{id}/abandon`), so finishing the answer is safe. */
  useEffect(() => {
    setSurveyOpen(false);
  }, [job?.operation_id]);
  const pollTimer = useRef<number | null>(null);

  const stopPolling = useCallback(() => {
    if (pollTimer.current !== null) {
      window.clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  /* The job's own record is the only source while it runs.  Polling stops the moment the server
     says `terminal`, so a finished job is never "still working" on screen. */
  const watch = useCallback(
    (operationId: string) => {
      stopPolling();
      const tick = async () => {
        try {
          const row = await cityNexusApi.getCatalogHydration(operationId);
          setJob(row);
          if (!row.terminal) {
            const wait = Math.max(500, Number(row.retry_after_ms || 0) || 1500);
            pollTimer.current = window.setTimeout(() => void tick(), wait);
          }
        } catch (error) {
          /* A read that failed is not a job that failed: name it and keep the last real reading. */
          setActionError(failureSentence("read", nameTheFailure(error), zh));
        }
      };
      void tick();
    },
    [stopPolling, zh],
  );

  useEffect(() => stopPolling, [stopPolling]);

  const check = async () => {
    const wanted = query.trim();
    if (!wanted) return;
    setChecking(true);
    setCapabilityError(null);
    setJob(null);
    setActionError(null);
    stopPolling();
    try {
      setCapability(await cityNexusApi.getDestinationCapability(wanted));
    } catch (error) {
      setCapability(null);
      setCapabilityError(failureSentence("check", nameTheFailure(error), zh));
    } finally {
      setChecking(false);
    }
  };

  const prepare = async () => {
    const wanted = (capability?.query ?? query).trim();
    if (!wanted) return;
    setBusy(true);
    setActionError(null);
    try {
      const started = await cityNexusApi.startCatalogHydration(wanted, { journeyId: journeyId ?? null });
      setJob(started);
      /* `reused` means an identical job already covers this request -- the server says so rather
         than answering 409, so the screen says "already preparing" instead of pretending to start. */
      if (!started.terminal) watch(started.operation_id);
    } catch (error) {
      setActionError(failureSentence("start", nameTheFailure(error), zh));
    } finally {
      setBusy(false);
    }
  };

  const abandon = async (): Promise<boolean> => {
    if (!job) return false;
    setBusy(true);
    setActionError(null);
    try {
      const stopped = await cityNexusApi.abandonCatalogHydration(job.operation_id);
      stopPolling();
      setJob(stopped);
      return true;
    } catch (error) {
      setActionError(failureSentence("stop", nameTheFailure(error), zh));
      return false;
    } finally {
      setBusy(false);
    }
  };

  /* ORD-0440: the survey sits in front of the stop, and the stop is still the stop. The record is
     written after the server answered, because an answer recorded against a stop that never landed
     would be a report about our own widget. */
  const stopAfterSurvey = async (reasons: string[], note: string) => {
    const operationId = job?.operation_id ?? "";
    const stopped = await abandon();
    setSurveyOpen(false);
    if (!operationId) return;
    appendSurveyLog({
      at: new Date().toISOString(),
      operation_id: operationId,
      reasons,
      note,
      note_chars: note.length,
      skipped: reasons.length === 0 && note.trim() === "",
      stopped,
    });
  };

  const reasonCode = capability?.reason_code ?? "";
  const verdict = capability?.capability ?? "";
  const missing = capability?.missing_categories ?? [];
  const candidates = capability?.candidates ?? [];

  /** The verdict in the traveller's words, keyed by the server's own reason code. */
  const verdictCopy = (): { title: string; body: string } => {
    if (!capability) return { title: "", body: "" };
    switch (reasonCode) {
      case "CATALOG_CURRENT":
        return {
          title: zh ? "这个目的地已经准备好了" : "This destination is ready",
          body: zh
            ? "服务器说这座城需要的事实都在，而且是新的，可以直接规划。"
            : "The server says the facts this city needs are present and current, so planning can start.",
        };
      case "COVERAGE_INCOMPLETE":
        return {
          title: zh ? "这个目的地还没准备过" : "This destination has not been prepared",
          body: zh
            ? `服务器说还缺 ${missing.length} 类事实（${missing.join("、")}），所以现在直接规划会缺依据。可以先准备。`
            : `The server reports ${missing.length} categories missing (${missing.join(", ")}), so planning now would lack evidence. You can prepare it first.`,
        };
      case "CATALOG_STALE":
        return {
          title: zh ? "这个目的地的事实过期了" : "This destination's facts have aged",
          body: zh
            ? "服务器说需要的事实都在，但已经不新了。直接规划会用到过季的资料，建议先刷新。"
            : "The server says nothing is missing but what it holds has aged; planning would use last season's facts. Refreshing first is the honest route.",
        };
      case "DESTINATION_AMBIGUOUS":
        return {
          title: zh ? "这个名字对不上一个地方" : "That name does not name one place",
          body: zh
            ? `服务器说「${capability.query}」能对上不止一个候选，它没有替你选。请从下面挑一个。`
            : `The server says "${capability.query}" matches more than one candidate and did not choose for you. Pick one below.`,
        };
      case "NO_CANONICAL_IDENTITY":
        return {
          title: zh ? "这个目的地这个名字认不出来" : "This destination cannot be named from a traceable source",
          body: zh
            ? `服务器说「${capability.pending_destination_label ?? capability.query}」没有任何可追溯来源能把它定成一个目的地。这不是网络问题——服务器刚才答得出来。换个写法，或另选一个地方。`
            : `The server says "${capability.pending_destination_label ?? capability.query}" cannot be resolved to a destination from any traceable source. This is not a network problem: the server just answered. Try another spelling, or choose another place.`,
        };
      case "PROVIDER_RATE_LIMITED":
      case "PROVIDER_TIMEOUT":
      case "PROVIDER_NETWORK_FAILURE":
      case "PROVIDER_SERVER_ERROR":
      case "CATALOG_PREPARING":
        return {
          title: zh ? "提供方这次没能回答" : "A provider could not answer this time",
          body: zh
            ? `服务器说这是提供方一侧的一次临时失败（${reasonCode}），可以重试；这不是「这个地方不存在」。`
            : `The server reports a transient failure on a provider's side (${reasonCode}); retrying is right, and this is not "the place does not exist".`,
        };
      case "DESTINATION_RESOLVER_UNAVAILABLE":
        /* ORD-0203: the fourth cell.  Measured live (resolver on, Photon unreachable):
           capability=NOT_ANSWERED, reason=DESTINATION_RESOLVER_UNAVAILABLE, action=RETRY_LATER,
           retryable=true, provider_status=[{status:"UNREACHABLE", answered:false}].
           R11 froze 「可规划性 ≠ 覆盖完整度」, so this says nobody answered -- NOT that the city
           needs data poured in (that would be a claim about the place) and NOT that it cannot be
           planned (that is the UNAVAILABLE cell's claim, which the server did not make). */
        return {
          title: zh ? "没人回答我们" : "Nobody answered us",
          body: zh
            ? `服务器说这一问没有拿到答复（${reasonCode}）——是它去问的那一头没答话，不是「这个地方不能规划」，也不是「这城市还要灌数据」。这两句服务器都没说。可以稍后重试。`
            : `The server reports that nothing answered this request (${reasonCode}). That is not "this place cannot be planned" and not "this city still needs data loaded" -- the server said neither. Trying again later is the route.`,
        };
      default:
        return {
          title: zh ? `服务器判定：${verdict}` : `Server verdict: ${verdict}`,
          body: zh
            ? `服务器给了这个结论（${reasonCode}），本地还没有对应文案，所以照它说的给，不替它解释。`
            : `The server returned this verdict (${reasonCode}); there is no local copy for it, so it is shown as-is rather than explained away.`,
        };
    }
  };

  /** The job, in the server's vocabulary.  `phase` and `elapsed_ms` -- never a percentage. */
  const jobCopy = (): { title: string; body: string } => {
    if (!job) return { title: "", body: "" };
    if (!job.terminal) {
      const elapsedCell = deviceReadout({ elapsed_ms: job.elapsed_ms }, ["elapsed_ms"]);
      return {
        title: job.reused
          ? zh
            ? "这件事已经在准备了"
            : "This is already being prepared"
          : zh
            ? "正在准备这个目的地"
            : "Preparing this destination",
        body: zh
          ? `服务器说现在在「${job.phase}」这一步，已经跑了 ${Math.round(job.elapsed_ms / 1000)} 秒（${elapsedCell}）。进度不按百分比报——服务器不给这个数，因为队列它自己看不到。`
          : `The server reports it is inside "${job.phase}", running ${Math.round(job.elapsed_ms / 1000)}s (${elapsedCell}). No percentage is shown because the server does not publish one -- it cannot see the queue.`,
      };
    }
    switch (String(job.outcome ?? "")) {
      case "CATALOG_PREPARED":
        return {
          title: zh ? "准备好了，可以去规划" : "Prepared -- you can go and plan",
          body: zh
            ? "服务器说这座城需要的事实已经落库。现在规划会用到它们。"
            : "The server says the facts this city needs are now stored, so planning will use them.",
        };
      case "ALREADY_CURRENT":
        return {
          title: zh ? "本来就是新的，没有改动" : "Already current -- nothing was changed",
          body: zh
            ? "服务器说这份资料已经是新的，所以这次准备什么也没做。这不是失败。"
            : "The server says this material was already current, so this run changed nothing. That is not a failure.",
        };
      case "NO_REGISTERED_SOURCE":
        return {
          title: zh ? "这个目的地没有可追溯的来源" : "No traceable source for this destination",
          body: zh
            ? "服务器说这个目的地它找不到任何可追溯的数据来源，所以准备不了。这是关于这个地方的事实，不是网络或服务器故障。换个目的地吧。"
            : "The server says it has no traceable source for this destination, so it cannot prepare it. That is a fact about the place, not a network or server fault. Choose another destination.",
        };
      case "ABANDONED_BY_TRAVELLER":
        return {
          title: zh ? "你已经停止这次准备" : "You stopped this preparation",
          body: zh ? "服务器记下了你的停止，它不再用这次的结果。" : "The server recorded the stop and will not use this run's result.",
        };
      case "RESOLVER_RUNTIME_FAILURE":
        return {
          title: zh ? "解析这次运行失败了" : "The resolver failed at runtime",
          body: zh
            ? "服务器说这是它那边的一次运行性失败（可以重试），不是「这个地方不存在」。"
            : "The server reports a runtime failure on its side (retryable), not that the place does not exist.",
        };
      case "WORKER_LOST":
        return {
          title: zh ? "这次准备中途丢了" : "This preparation was lost mid-run",
          body: zh ? "服务器说执行它的工作进程没了，这次不算数。可以再来一次。" : "The server says the worker running it disappeared, so this attempt does not count. Starting again is the route.",
        };
      default:
        return {
          title: zh ? `准备结束：${String(job.status)}` : `Preparation ended: ${String(job.status)}`,
          body: zh
            ? `服务器给出的结局是 ${String(job.outcome ?? "—")}，本地没有对应文案，照它说的给。`
            : `The server's outcome is ${String(job.outcome ?? "—")}; there is no local copy for it, so it is shown as-is.`,
        };
    }
  };

  /* ORD-0440 second half: this cell used to print `job.error.message`, i.e. whatever sentence the
     server happened to answer with. The refusal is still said - by the token the server owns
     (`code` / `reason_code` / `message_key`), which chooses a sentence written here. The prose itself
     is dropped, and it does not ride into a `data-*` either: `error_receipt_id` and friends are not
     among the six fields a slot may name. */
  const serverError = job?.error ?? null;
  const serverSentence = serverError
    ? failureSentence("read", nameTheFailure(serverError), zh)
    : "";
  /* The action comes from the server.  `retryable` is the pair the fourth cell is obliged to keep
     (`NOT_ANSWERED => retryable is true and a provider_status exists`), so the retry action reads it
     rather than deciding for itself that retrying is reasonable. */
  const action = capability?.recommended_action ?? "";
  const retryable = Boolean(capability?.retryable);

  /* ORD-0440: once the traveller has asked to stop, the sheet is the screen. With the page intro and
     the verdict still stacked above it, the walk measured the exit at centre y=1145 in a 390x844
     viewport - past the 768 first-screen line and past the viewport itself, which a tap that auto-
     scrolls would never have shown. `review/n3_0440_evidence_r1/walk/walk_zh.json` carries that
     reading; this branch is the fix it asked for.
     The poll is held while the sheet is open: the shell re-measures by ~10 px on every tick
     (`review/ord0440/_shift_source_probe.cjs`, kicker top 93 <-> 103 with a stable 66 px topbar), so a
     row under the finger moved each time the server answered. Stopping the read for the length of one
     answer is ours to do; the header's own re-measure is named in the delivery for its owner. */
  const openSurvey = () => {
    stopPolling();
    setSurveyOpen(true);
  };

  const closeSurvey = () => {
    setSurveyOpen(false);
    if (job && !job.terminal) watch(job.operation_id);
  };

  if (surveyOpen && job) {
    return (
      <section
        className="v8-page"
        data-testid="prep-panel"
        data-job-id={job.operation_id}
        data-focus="stop-survey"
      >
        <p className="v8-kicker" data-testid="prep-survey-context">
          {zh ? "准备进度" : "Preparation"} · {job.status}
        </p>
        <PrepAbandonSurvey
          zh={zh}
          operationId={job.operation_id}
          busy={busy}
          onExit={(reasons, note) => void stopAfterSurvey(reasons, note)}
          onBack={closeSurvey}
        />
      </section>
    );
  }

  return (
    <section
      className="v8-page"
      data-testid="prep-panel"
      data-verdict={verdict}
      data-reason={reasonCode}
      data-action={action}
      data-retryable={String(retryable)}
      data-job-id={job?.operation_id ?? ""}
    >
      <div className="v8-page-intro compact">
        <div>
          <p className="v8-kicker">{zh ? "准备目的地" : "Prepare a destination"}</p>
          <h1>{zh ? "先看这座城能不能规划" : "First, whether this city can be planned"}</h1>
          <p className="v8-lede">
            {zh
              ? "输入一个目的地，服务器会告诉我们它现在能规划到什么程度；不能的话，缺什么、下一步是什么，都由服务器说了算。"
              : "Type a destination and the server says how far it can plan it right now; if it cannot, what is missing and what to do next come from the server."}
          </p>
        </div>
      </div>

      <div className="prep-form">
        <label>
          <span>{zh ? "目的地" : "Destination"}</span>
          <input
            data-testid="prep-query"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={zh ? "例如：天津、贡山独龙族怒族自治县" : "e.g. Tianjin"}
          />
        </label>
        <button
          type="button"
          className="v8-primary-button"
          data-testid="prep-check"
          disabled={checking || busy || query.trim().length === 0}
          onClick={() => void check()}
        >
          {checking ? (zh ? "正在问服务器…" : "Asking the server…") : zh ? "看看能不能规划" : "Check this destination"}
        </button>
      </div>

      {capabilityError && (
        <p className="role-alert" role="alert" data-testid="prep-check-error">
          {capabilityError}
        </p>
      )}

      {capability && (
        <div className="prep-verdict" data-testid="prep-verdict" data-capability={verdict}>
          <span className="v8-kicker">
            {zh ? "服务器判定" : "Server verdict"} · {verdict}
          </span>
          <h2 data-testid="prep-verdict-title">{verdictCopy().title}</h2>
          <p data-testid="prep-verdict-body">{verdictCopy().body}</p>

          {/* What is missing, category by category, from the server's coverage map. */}
          {missing.length > 0 && (
            <ul className="prep-coverage" data-testid="prep-missing">
              {missing.map((key) => (
                <li key={key}>
                  {key} · {String(capability.coverage[key] ?? "")}
                </li>
              ))}
            </ul>
          )}

          {/* An observation exists only if something looked; absence is left absent. */}
          {capability.provider_status && capability.provider_status.length > 0 && (
            <ul className="prep-providers" data-testid="prep-providers">
              {capability.provider_status.map((item, index) => (
                <li key={`${item.provider ?? index}`}>
                  {item.provider ?? t("discovery.providerUnnamed")} · {item.status ?? ""}
                  {item.message ? ` · ${item.message}` : ""}
                </li>
              ))}
            </ul>
          )}

          {candidates.length > 0 && (
            <div className="prep-candidates" data-testid="prep-candidates">
              <b>{zh ? "服务器说这些都对得上，请挑一个" : "The server says these all match -- pick one"}</b>
              <ul>
                {candidates.map((candidate, index) => (
                  <li key={String(candidate.id ?? index)}>
                    {String(candidate.label ?? candidate.name ?? candidate.id ?? index)}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Every verdict ends in a real action.  No silent close, no no-op.  The action is the
              server's `recommended_action`, not a local rule: measured on the fourth cell, the
              server answers RETRY_LATER and the old "anything not READY may prepare" branch offered
              开始准备 for a destination nobody had named -- the exact defect `anywhere_capability.py`
              names in its own comment. */}
          <div className="prep-actions">
            {action === "ACCEPT_PARTIAL_PLAN" ? (
              <button
                type="button"
                className="v8-primary-button"
                data-testid="prep-go-plan"
                onClick={() => onOpenPlan?.()}
                disabled={!onOpenPlan}
              >
                {zh ? "去规划" : "Go and plan"}
              </button>
            ) : action === "RETRY_LATER" ? (
              <button
                type="button"
                className="v8-primary-button"
                data-testid="prep-retry-later"
                disabled={checking || !retryable}
                onClick={() => void check()}
              >
                {checking ? (zh ? "正在重试…" : "Trying again…") : zh ? "稍后重试" : "Try again later"}
              </button>
            ) : action === "START_HYDRATION" ? (
              <button
                type="button"
                className="v8-primary-button"
                data-testid="prep-start"
                disabled={busy || Boolean(job && !job.terminal)}
                onClick={() => void prepare()}
              >
                {busy ? (zh ? "正在开始…" : "Starting…") : zh ? "开始准备" : "Prepare this destination"}
              </button>
            ) : null}
            {/* CHOOSE_ANOTHER_DESTINATION names no primary action of its own: the server's advice for
                a destination it cannot name is to pick a different one, which is the button below.
                Named explicitly so no future action string silently inherits "prepare this". */}
            <button
              type="button"
              className="v8-outline-button"
              data-testid="prep-choose-another"
              onClick={() => {
                stopPolling();
                setCapability(null);
                setJob(null);
                setQuery("");
                setActionError(null);
              }}
            >
              {zh ? "换一个目的地" : "Choose another destination"}
            </button>
          </div>
        </div>
      )}

      {job && (
        <div
          className={`prep-job ${job.terminal ? "terminal" : "running"}`}
          data-testid="prep-job"
          data-status={job.status}
          data-phase={job.phase}
          data-terminal={job.terminal ? "true" : "false"}
          data-outcome={String(job.outcome ?? "")}
          data-elapsed-ms={Math.round(job.elapsed_ms)}
          role="status"
        >
          <span className="v8-kicker">{zh ? "准备进度" : "Preparation"} · {job.status}</span>
          <h3 data-testid="prep-job-title">{jobCopy().title}</h3>
          <p data-testid="prep-job-body">{jobCopy().body}</p>
          {serverSentence && (
            <p className="prep-server" data-testid="prep-server-message">
              {serverSentence}
            </p>
          )}
          <div className="prep-actions">
            {!job.terminal && !surveyOpen && (
              <button
                type="button"
                className="v8-outline-button"
                data-testid="prep-abandon"
                disabled={busy}
                onClick={openSurvey}
              >
                {zh ? SURVEY_COPY.zh[STOP_ACTION_KEY] : SURVEY_COPY.en[STOP_ACTION_KEY]}
              </button>
            )}
            {job.terminal && job.retryable && (
              <button
                type="button"
                className="v8-primary-button"
                data-testid="prep-retry"
                disabled={busy}
                onClick={() => void prepare()}
              >
                {zh ? "重试" : "Try again"}
              </button>
            )}
            {job.terminal && String(job.outcome ?? "") === "CATALOG_PREPARED" && onOpenPlan && (
              <button
                type="button"
                className="v8-primary-button"
                data-testid="prep-go-plan"
                onClick={() => onOpenPlan()}
              >
                {zh ? "去规划" : "Go and plan"}
              </button>
            )}
            {job.terminal && (
              <button
                type="button"
                className="v8-outline-button"
                data-testid="prep-recheck"
                disabled={busy || checking}
                onClick={() => void check()}
              >
                {zh ? "重新判定" : "Check again"}
              </button>
            )}
          </div>
        </div>
      )}

      {actionError && (
        <p className="role-alert" role="alert" data-testid="prep-action-error">
          {actionError}
        </p>
      )}
    </section>
  );
}
