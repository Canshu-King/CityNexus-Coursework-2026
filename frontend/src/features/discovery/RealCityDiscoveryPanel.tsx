import { useEffect, useState } from "react";
import { useT, useLocale } from "../../i18n/LocaleContext";
import { formatMinorMoney } from "../../lib/money";
import { namedCodeIn, type RawFacts } from "../../lib/rawFacts";
import { rejectionWithFacts, RejectionWithFacts } from "../../lib/rejectionFacts";
import { OriginResponseUnusableError } from "../../lib/failureAttribution";
import { CnxMeasuredFacts } from "../../components/CnxMeasuredFacts";
import { PlaceProvenance } from "../../components/PlaceProvenance";
/* ORD-0435: the batch bar and the per-card tick live in a sheet this panel imports, not in
 * `src/styles.css`, which is a hot file under this order. See the header of that file for why the
 * two `[data-picked]` rules do not depend on injection order. */
import "./discovery-batch.css";

type Candidate = {
  id: string;
  name: string;
  category: string;
  provider_key?: string;
  latitude: number;
  longitude: number;
  /* ORD-0435: the discovery run materialises each candidate into a `places` row and says so. The
     write path needs that row's identity, not the model's, so the field is now part of what this
     screen reads. Measured on this tree: `id` and `materialized_place_id` carry the same value for
     all three candidates of a sealed replay (`real-place-2a59f43244feca0eeced4bfa` and friends),
     and the compose door accepts those ids as `place_ids`. */
  materialized_place_id?: string;
  coordinate_system?: string | null;
  source_coordinate_system?: string | null;
  opening_hours?: { status?: string; open?: string; close?: string; source_urls?: string[] };
  cost_evidence?: { status?: string; amount_minor?: number | null; currency?: string };
};

type DiscoveryResponse = {
  intent: {
    provider: string;
    model?: string | null;
    anchor_key: string;
    explanation: string;
    queries: Array<{ query: string; radius_m: number; limit: number }>;
  };
  anchor?: { source?: string; location?: string; latitude: number; longitude: number } | null;
  discovery: {
    provenance: string;
    mode: string;
    provider_status: Record<string, unknown>;
    provider_evidence?: {
      mobility?: {
        provider?: string;
        captured_at?: string;
        supported_modes?: string[];
        route_provenance?: string;
        cost_provenance?: string;
      };
      weather?: { provider?: string; time_basis?: string };
    };
    candidates: Candidate[];
  };
  curation: {
    provider: string;
    semantic_fallback?: string;
    rejected_candidate_ids: string[];
    ranked: Array<{ candidate_id: string; semantic_fit: number; reason: string }>;
  };
  feasibility: {
    passed: boolean;
    authority: string;
    eligible: Candidate[];
    rejected: Array<{ candidate_id: string; reason: string }>;
  };
  replay: { sealed: boolean; provenance: string; provider_status: Record<string, unknown> };
  selected_plan?: { id: string; status: string; version_no: number };
  require_fully_grounded?: boolean;
  /* ORD-0435: which places the server itself put into the set it composed. It is not the traveller's
     batch - it holds the protected commitment (`place-river-cruise` on the sealed demo) alongside at
     most one pick - but it is the only place on this response where that commitment is named by its
     row identity, and the compose door refuses a batch without it
     (`V13_C4_ANCHOR_NOT_IN_INPUT:place-river-cruise`, measured). */
  selected_place_ids?: string[];
};

/* ORD-0435: what the batch write path answers with, and what this screen reads back afterwards.
   Kept structural rather than imported from src/generated/api.ts because the panel talks to the
   route through its own `request()` - the one that turns a refusal into measured facts - and a
   generated type would suggest the response is trusted further than the two fields used here. */
type ComposedPlanActivity = {
  id: string;
  sequence_no: number;
  title: string;
  activity_type?: string;
  place_id?: string | null;
  start_at: string;
  end_at: string;
};
type ComposedPlan = {
  id: string;
  version_no: number;
  status: string;
  activities?: ComposedPlanActivity[];
};
type ComposeResponse = {
  optimizer: string;
  complete_plan_count: number;
  top_k: ComposedPlan[];
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  const contentType = response.headers.get("content-type");
  if (!response.ok) {
    /* Only a refusal is read as text, and only two things survive from it: what this device measured,
       and the contract field the receipt number is read out of. The answer's own sentence stops here --
       it is what the traveller used to be shown. */
    throw await rejectionWithFacts(response);
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (body === null) {
    /* Answered 2xx with a body that is not this screen's data: a bare `null` used to become a
       TypeError two frames later and be reported as no network (ORD-0263's fifth state). The status
       and the media type the answer really carried ride on the error instead. */
    throw new OriginResponseUnusableError(
      response.status,
      contentType,
      (contentType ?? "").includes("html") ? "html" : "not_json",
    );
  }
  return body as T;
}

/**
 * ORD-0435: the machine tokens a refusal actually carried, read from the whole envelope.
 *
 * `RejectionWithFacts` builds its `message` from `String(envelope.error ?? envelope.detail)`, and on
 * this contract `error` is an object -- so the message is the literal `[object Object]` and every
 * `namedCodeIn(message, …)` branch on this screen has been falling through to the generic sentence.
 * Measured twice on this tree's own store: a `V13_FULL_GROUNDING_MISSING_MOBILITY_PAIR` and a
 * `V13_ACTIVITY_OUTSIDE_DEPARTURE_WINDOW` both arrived on screen as 没能组合探索结果.
 *
 * This is not a second classification channel and it renders nothing: `namedCodeIn` answers only for
 * tokens from the caller's enumerated list, so the answer's own prose still never reaches a
 * traveller (Q64 names the token, Q71 keeps the sentence off the screen).
 */
function refusalTokens(cause: unknown): string {
  const parts: string[] = [cause instanceof Error ? cause.message : ""];
  const envelope = cause instanceof RejectionWithFacts ? cause.envelope : null;
  if (envelope && typeof envelope === "object") {
    for (const value of Object.values(envelope as Record<string, unknown>)) {
      if (typeof value === "string") parts.push(value);
      else if (value && typeof value === "object") {
        for (const inner of Object.values(value as Record<string, unknown>)) {
          if (typeof inner === "string") parts.push(inner);
        }
      }
    }
  }
  return parts.join(" ");
}

export function RealCityDiscoveryPanel({
  journeyId,
  cityCode,
  onComplete,
  onOpenPlan,
}: {
  journeyId: string;
  cityCode: string;
  onComplete: () => Promise<void>;
  onOpenPlan: () => void;
}) {
  const t = useT();
  const { locale } = useLocale();
  const supportsTianjinReplay = ["CN-TIANJIN", "TIANJIN", "TSN", "CNX-DEMO"].includes(cityCode.toUpperCase().replaceAll("_", "-"));
  /* ORD-0422: this used to be a hardcoded English sentence sitting in a Chinese screen's input
     box. The sentence was already in the dictionary under the key `discovery.defaultGoal` (one definition per
     language arm in `src/i18n/LocaleContext.tsx`, named by key because line numbers there move every generation) -- the panel just never looked it up, so the Chinese screen got English.
     It now follows the language the screen opened in. `useState` evaluates once, so switching
     language mid-visit deliberately leaves their
     in-progress text alone rather than overwriting what they typed. */
  const [goal, setGoal] = useState(t("discovery.defaultGoal"));
  const [mode, setMode] = useState<"captured_replay" | "live" | "replay">(supportsTianjinReplay ? "captured_replay" : "live");
  const [result, setResult] = useState<DiscoveryResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [failureFacts, setFailureFacts] = useState<RawFacts | null>(null);
  const [failureEnvelope, setFailureEnvelope] = useState<unknown>(null);
  /* ORD-0435 -- the batch. `picked` holds candidate row identities in the order the cards are
     rendered, so the count on screen is a count of these and nothing else. It is deliberately NOT
     stored under the `selected` class the feasibility gate already owns: on this tree's own screen
     read three cards were on screen and three carried `.selected`, which is the server's verdict on
     each place, not the traveller's tick. A second meaning on the same attribute would make the two
     states indistinguishable to an instrument and to a person. */
  const [picked, setPicked] = useState<string[]>([]);
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchError, setBatchError] = useState<string | null>(null);
  const [batchFacts, setBatchFacts] = useState<RawFacts | null>(null);
  const [batchEnvelope, setBatchEnvelope] = useState<unknown>(null);
  /* Only ever set from a second, separate GET of the plan the server just wrote -- see `finishBatch`.
     Nothing on screen claims a place "landed on a day" from the POST's own body. */
  const [batchReceipt, setBatchReceipt] = useState<{
    plan_id: string;
    version_no: number;
    days: string[];
    landed: Array<{ activity_id: string; title: string; start_at: string }>;
    missing: string[];
  } | null>(null);

  useEffect(() => {
    setMode(supportsTianjinReplay ? "captured_replay" : "live");
  }, [supportsTianjinReplay]);

  const money = (value: number | null | undefined, currency = "CNY") =>
    value == null
      ? t("mobility.costUnknown")
      : formatMinorMoney(value, currency, locale === "zh-CN" ? "zh-CN" : "en-US");

  const runDiscovery = async () => {
    setBusy(true);
    setError(null);
    setFailureFacts(null);
    setFailureEnvelope(null);
    /* A new exploration replaces the cards, so it has to replace the batch too: ticks are candidate
       identities, and leaving last run's in state would put a count on screen that no card answers. */
    setPicked([]);
    setBatchReceipt(null);
    setBatchError(null);
    setBatchFacts(null);
    setBatchEnvelope(null);
    try {
      const next = await request<DiscoveryResponse>(`/api/v1/journeys/${journeyId}/real-city/intelligence`, {
        method: "POST",
        body: JSON.stringify({ goal, mode, radius_m: 3200, limit: 8, candidate_count: 2, require_fully_grounded: mode === "captured_replay" }),
      });
      setResult(next);
      await onComplete();
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "";
      /* Q64: the machine token is what is matched, the sentence is what is dropped. `namedCodeIn`
         scans the text instead of assuming one path, so a re-shaped envelope cannot turn a named
         refusal back into a generic one -- and the branch that matches nothing prints this screen's
         own sentence, never what the answer happened to say. */
      setFailureFacts(cause instanceof RejectionWithFacts ? cause.facts : null);
      setFailureEnvelope(cause instanceof RejectionWithFacts ? cause.envelope : null);
      const code = namedCodeIn(refusalTokens(cause), [
        "V13_DISCOVERY_NO_FEASIBLE_PLACE_SET",
        "V13_FULL_GROUNDING_MISSING_MOBILITY_PAIR",
      ]);
      if (code === "V13_DISCOVERY_NO_FEASIBLE_PLACE_SET" || code === "V13_FULL_GROUNDING_MISSING_MOBILITY_PAIR") {
        setError(t("discovery.noGroundedCandidates"));
      } else if (message.startsWith("DeepSeek discovery failed")) {
        setError(t("discovery.aiUnavailable"));
      } else {
        setError(t("discovery.composeError"));
      }
    } finally {
      setBusy(false);
    }
  };

  /* ORD-0435 -- one tick per card, and the batch is exactly the set of ticks. */
  const togglePick = (candidateId: string) => {
    setPicked((current) =>
      current.includes(candidateId)
        ? current.filter((item) => item !== candidateId)
        : [...current, candidateId],
    );
    /* The receipt describes the batch that produced it. Once the batch changes, the receipt is a
       statement about a set nobody asked for, so it goes away rather than staying on screen. */
    setBatchReceipt(null);
    setBatchError(null);
    setBatchFacts(null);
    setBatchEnvelope(null);
  };

  const startNewBatch = () => {
    setPicked([]);
    setBatchReceipt(null);
    setBatchError(null);
    setBatchFacts(null);
    setBatchEnvelope(null);
  };

  /* ORD-0435 -- "done" writes through the route the V13.4 discovery path already uses internally
     (`POST /journeys/{id}/plans/authoritative-compose`, contracts/openapi.yaml) and then reads
     the answer back through a second, unrelated one (`GET /plans/{id}`). The second call is the
     point of the order: a tick that only lives in this component's state would look identical on
     screen and mean nothing, so the screen shows what the row store returned -- including the case
     where it returned less than what was asked for (`missing` below). */
  const finishBatch = async () => {
    if (!result || picked.length === 0 || batchBusy) return;
    setBatchBusy(true);
    setBatchError(null);
    setBatchFacts(null);
    setBatchEnvelope(null);
    setBatchReceipt(null);
    try {
      const cardIds = result.discovery.candidates.map((item) => item.id);
      const pickedPlaces = picked.flatMap((candidateId) => {
        const candidate = result.discovery.candidates.find((item) => item.id === candidateId);
        if (!candidate) return [];
        return [String(candidate.materialized_place_id ?? candidate.id)];
      });
      /* The protected commitment travels with the batch because the planner refuses without it; it is
         not counted as a pick, and the screen says so separately. */
      const commitments = (result.selected_place_ids ?? []).filter((id) => !cardIds.includes(id));
      const placeIds = Array.from(new Set([...commitments, ...pickedPlaces]));
      const composed = await request<ComposeResponse>(
        `/api/v1/journeys/${journeyId}/plans/authoritative-compose`,
        { method: "POST", body: JSON.stringify({ place_ids: placeIds, candidate_count: 1 }) },
      );
      const proposed = composed.top_k?.[0];
      if (!proposed?.id) throw new Error("V13_DISCOVERY_COMPOSE_EMPTY");
      const readback = await request<ComposedPlan>(`/api/v1/plans/${proposed.id}`);
      const written = readback.activities ?? [];
      const landed = written
        .filter((item) => item.place_id && pickedPlaces.includes(String(item.place_id)))
        .map((item) => ({ activity_id: item.id, title: item.title, start_at: item.start_at }));
      const missing = pickedPlaces.filter(
        (placeId) => !written.some((item) => String(item.place_id) === placeId),
      );
      setBatchReceipt({
        plan_id: readback.id,
        version_no: readback.version_no,
        days: Array.from(new Set(landed.map((item) => item.start_at.slice(0, 10)))).sort(),
        landed,
        missing,
      });
      await onComplete();
    } catch (cause) {
      setBatchFacts(cause instanceof RejectionWithFacts ? cause.facts : null);
      setBatchEnvelope(cause instanceof RejectionWithFacts ? cause.envelope : null);
      const code = namedCodeIn(refusalTokens(cause), [
        "V13_ACTIVITY_OUTSIDE_DEPARTURE_WINDOW",
        "V13_C4_ANCHOR_NOT_IN_INPUT",
        "V13_DISCOVERY_COMPOSE_EMPTY",
      ]);
      if (code === "V13_ACTIVITY_OUTSIDE_DEPARTURE_WINDOW") {
        setBatchError(t("discovery.batchOutsideDay"));
      } else if (code === "V13_C4_ANCHOR_NOT_IN_INPUT") {
        setBatchError(t("discovery.batchMissingCommitment"));
      } else if (code === "V13_DISCOVERY_COMPOSE_EMPTY") {
        setBatchError(t("discovery.batchNoPlan"));
      } else {
        setBatchError(t("discovery.composeError"));
      }
    } finally {
      setBatchBusy(false);
    }
  };

  return (
    <section className="v8-page" data-testid="v13-discovery">
      <div className="v8-page-intro compact">
        <div>
            <p className="v8-kicker">{t("discovery.kicker")}</p>
          <h1>{t("discovery.title")}</h1>
          <p className="v8-lede">{t("discovery.lede")}</p>
        </div>
        <span className="v8-evidence-label"><span className="status-dot" />{mode === "captured_replay" ? t("discovery.sealedRealReplay") : mode === "live" ? t("discovery.liveProviderMode") : t("discovery.demoReplay")}</span>
      </div>
      <div className="v8-agent-composer-card">
        <div className="v8-agent-copy">
          <span className="v8-agent-mark">{t("discovery.brand")}</span>
          <h2>{t("discovery.shapeCity")}</h2>
          <p>{t("discovery.shapeSub")}</p>
          <div className="v8-agent-boundary"><b>{t("discovery.boundary")}</b><span>{t("discovery.boundarySub")}</span></div>
        </div>
        <div className="v8-agent-input">
          <label htmlFor="v13-discovery-goal">{t("discovery.intent")}</label>
          <textarea id="v13-discovery-goal" data-testid="v13-discovery-goal" value={goal} onChange={(event) => setGoal(event.target.value)} />
          <div className="v8-agent-toolbar">
            <label>{t("discovery.evidenceMode")} <select aria-label={t("discovery.evidenceMode")} value={mode} onChange={(event) => setMode(event.target.value as typeof mode)}>{supportsTianjinReplay && <option value="captured_replay">{t("discovery.sealedRealReplay")}</option>}<option value="live">{t("discovery.liveProviders")}</option>{supportsTianjinReplay && <option value="replay">{t("discovery.syntheticDemo")}</option>}</select></label>
            <button className="v8-primary" data-testid="v13-discover-submit" disabled={busy || goal.trim().length < 3} onClick={() => void runDiscovery()}>{busy ? t("discovery.composing") : t("discovery.discoverCompose")}</button>
          </div>
          {!supportsTianjinReplay && <p className="v8-sheet-note">{t("discovery.genericCityLiveNote")}</p>}
        </div>
      </div>
      {error && (
        <>
          <div className="v8-message" role="alert" data-testid="v13-discovery-error">
            {error}
          </div>
          {/* Q64=1: what stays diagnosable is this device's own reading, in a technical-detail fold --
              one element per contract field (Q70), and no fragment of the answer's text anywhere,
              attribute included (Q71). With no measurement there is no anchor to mount. */}
          {failureFacts && (
            <details className="v8-advanced" data-testid="v13-discovery-technical">
              <summary data-testid="v13-discovery-technical-toggle">
                {locale.startsWith("zh") ? "技术详情" : "Technical detail"}
              </summary>
              <CnxMeasuredFacts
                facts={failureFacts}
                zh={locale.startsWith("zh")}
                testId="v13-discovery-fact"
                envelope={failureEnvelope}
              />
            </details>
          )}
        </>
      )}
      {result && (
        <div className="v8-intelligence-grid" data-testid="v13-discovery-result">
          <article className="v8-intel-hero">
            <span className="v8-status-pill violet" data-testid="v13-discovery-provider">{result.intent.provider}</span>
            <h2>{result.intent.explanation || t("discovery.groundedStop")}</h2>
            <p>{t("discovery.anchor")} <b>{result.intent.anchor_key}</b>{result.anchor?.location ? ` · ${result.anchor.location}` : ""}</p>
            <p>{t("discovery.query")} {result.intent.queries[0]?.query} · {t("discovery.radius", { r: result.intent.queries[0]?.radius_m, max: result.intent.queries[0]?.limit })}</p>
            <div className="v8-chain"><span>{t("discovery.memory")}</span><b>→</b><span>{t("intel.deepseek")}</span><b>→</b><span>{t("discovery.providers")}</span><b>→</b><span>{t("discovery.detGate")}</span></div>
          </article>
          <div className="v8-intel-facts">
            <div><span>{t("discovery.provenance")}</span><b>{result.replay.provenance === "SEALED_REAL_REPLAY" ? t("discovery.sealedRealReplay") : result.replay.sealed ? t("discovery.sealed") : result.discovery.provenance}</b><small>{result.discovery.mode} · {String(result.discovery.provider_status.overpass ?? t("discovery.providerCaptured"))}</small></div>
            <div><span>{t("discovery.mobility")}</span><b>{result.discovery.provider_evidence?.mobility?.provider ?? t("discovery.explicitFallback")}</b><small>{result.discovery.provider_evidence?.mobility?.supported_modes?.join(" + ") ?? t("discovery.factFallback")} · {result.discovery.provider_evidence?.mobility?.route_provenance ?? t("discovery.syntheticFallback")}</small></div>
            <div><span>{t("discovery.grounding")}</span><b>{result.require_fully_grounded ? t("discovery.fullyGrounded") : t("discovery.mixedDegraded")}</b><small>{result.discovery.provider_evidence?.mobility?.cost_provenance ?? t("discovery.costSeparate")}</small></div>
            <div><span>{t("discovery.feasibility")}</span><b>{result.feasibility.passed ? t("discovery.passed") : t("discovery.blocked")}</b><small>{result.feasibility.authority}</small></div>
            <div data-testid="v13-discovery-selected-plan"><span>{t("discovery.planversion")}</span><b>{result.selected_plan?.status ?? t("discovery.notSelected")}</b><small>{result.selected_plan ? `v${result.selected_plan.version_no} · ${result.selected_plan.id}` : t("discovery.noPlan")}</small></div>
          </div>
        </div>
      )}
      {result && <div className="v8-stay-layout">
        <div className="v8-stay-list">
          {/* ORD-0435 checkpoint 2 and 4: the batch names the container it will be written into, and
              an empty batch says so instead of offering a dead "done".  Checkpoint 5: 完成 and 新建
              sit in the same row at every width, so finishing one batch always opens the next.
              Neither takes `.v8-primary`: this screen's one primary action is the aside's 打开权威方案
              (Pass-8), and a second primary here would be a screen that shouts twice. */}
          <div className="v8-batch-bar" data-testid="v13-discovery-batch-bar">
            <b className="v8-batch-count" data-testid="v13-discovery-picked-count">
              {t("discovery.pickedCount", { n: picked.length })}
            </b>
            <span className="v8-batch-target" data-testid="v13-discovery-batch-target">
              {!batchReceipt
                ? t("discovery.batchNotPlaced")
                : batchReceipt.landed.length > 0
                ? t("discovery.batchLanded", { n: batchReceipt.landed.length, day: batchReceipt.days.join(" · ") })
                : t("discovery.batchNothingLanded")}
            </span>
            {batchReceipt && (
              <span className="v8-batch-plan" data-testid="v13-discovery-batch-plan">
                {t("discovery.batchPlanVersion", { v: batchReceipt.version_no })}
              </span>
            )}
            <span className="v8-batch-actions">
              <button
                type="button"
                className="v8-quiet-button"
                data-testid="v13-discovery-new-batch"
                disabled={batchBusy}
                onClick={startNewBatch}
              >
                {t("discovery.newBatch")}
              </button>
              <button
                type="button"
                className="v8-outline-button"
                data-testid="v13-discovery-finish-batch"
                disabled={batchBusy || picked.length === 0}
                onClick={() => void finishBatch()}
              >
                {batchBusy ? t("discovery.batchBusy") : t("discovery.finishBatch")}
              </button>
            </span>
          </div>
          {picked.length === 0 ? (
            <p className="v8-batch-empty" data-testid="v13-discovery-batch-empty">
              {t("discovery.batchEmpty")}
            </p>
          ) : (
            <p className="v8-batch-note" data-testid="v13-discovery-batch-note">
              {t("discovery.batchCommitmentNote", {
                n: (result.selected_place_ids ?? []).filter(
                  (id) => !result.discovery.candidates.some((item) => item.id === id),
                ).length,
              })}
            </p>
          )}
          {result.discovery.candidates.slice(0, 4).map((candidate) => {
            const rank = result.curation.ranked.find((item) => item.candidate_id === candidate.id);
            const eligible = result.feasibility.eligible.some((item) => item.id === candidate.id);
            const sourceUrl = candidate.opening_hours?.source_urls?.[0];
            const isPicked = picked.includes(candidate.id);
            return <article className={eligible ? "v8-stay-card selected" : "v8-stay-card"} key={candidate.id} data-picked={isPicked ? "true" : "false"}>
              <div className="v8-stay-card-body"><div className="v8-stay-card-title"><div>
                {/* ORD-0435: the tick is its own control, not the card. The card carries a `<details>`
                    (来源与坐标系) and a whole-card click would fight it; `role="checkbox"` +
                    `aria-checked` gives the selected state a twin instead of a class that only ever
                    appears one way. `.selected` stays the server's feasibility verdict -- on this
                    screen's own before-read all three rendered cards carried it. */}
                <button
                  type="button"
                  role="checkbox"
                  aria-checked={isPicked}
                  className={isPicked ? "v8-pick-box checked" : "v8-pick-box"}
                  data-testid="v13-pick-candidate"
                  data-picked={isPicked ? "true" : "false"}
                  onClick={() => togglePick(candidate.id)}
                >
                  <span aria-hidden="true">{isPicked ? "✓" : ""}</span>
                  <span className="v8-pick-box-text">{t(isPicked ? "discovery.unpick" : "discovery.pick")}</span>
                </button>
                <span className="v8-rank">{eligible ? "✓" : "—"}</span><h3>{candidate.name}</h3></div><span className="v8-selected-label">{eligible ? t("discovery.eligible") : t("discovery.rejected")}</span></div>
                <PlaceProvenance
                  name={candidate.name}
                  category={candidate.category}
                  providerKey={candidate.provider_key}
                  coordinateSystem={candidate.coordinate_system}
                  sourceCoordinateSystem={candidate.source_coordinate_system}
                  host={sourceUrl ? new URL(sourceUrl).hostname : null}
                />
                <div className="v8-stay-impact"><span>{candidate.opening_hours?.status ?? t("discovery.hoursUnknown")}</span><b>{money(candidate.cost_evidence?.amount_minor, candidate.cost_evidence?.currency)}</b><em>{rank ? t("discovery.semanticFit", { n: Math.round(rank.semantic_fit * 100) }) : t("discovery.notRanked")}</em></div>
                {rank?.reason && <small>{rank.reason}</small>}
              </div>
            </article>;
          })}
          {batchReceipt && (
            <div className="v8-batch-receipt" data-testid="v13-discovery-batch-receipt">
              <b>{t("discovery.batchReadbackTitle")}</b>
              <ul className="v8-batch-receipt-rows" data-testid="v13-discovery-batch-rows">
                {batchReceipt.landed.map((row) => (
                  <li key={row.activity_id} data-testid="v13-discovery-batch-row">
                    <span>{row.start_at.slice(0, 16).replace("T", " ")}</span>
                    <b>{row.title}</b>
                    <small>{row.activity_id}</small>
                  </li>
                ))}
              </ul>
              {batchReceipt.missing.length > 0 && (
                <p className="v8-batch-refused" data-testid="v13-discovery-batch-refused">
                  {t("discovery.batchRefusedSome", { n: batchReceipt.missing.length })}
                </p>
              )}
              <small className="v8-batch-receipt-plan">
                {t("discovery.batchPlanId", { id: batchReceipt.plan_id })}
              </small>
            </div>
          )}
          {batchError && (
            <>
              <div className="v8-message" role="alert" data-testid="v13-discovery-batch-error">
                {batchError}
              </div>
              {batchFacts && (
                <details className="v8-advanced" data-testid="v13-discovery-batch-technical">
                  <summary data-testid="v13-discovery-batch-technical-toggle">
                    {t("recovery.technicalDetail")}
                  </summary>
                  <CnxMeasuredFacts
                    facts={batchFacts}
                    zh={locale.startsWith("zh")}
                    testId="v13-discovery-batch-fact"
                    envelope={batchEnvelope}
                  />
                </details>
              )}
            </>
          )}
        </div>
        <aside className="v8-boundary-card"><b>{t("discovery.authoritativeNext")}</b><p>{t("discovery.authoritativeSub")}</p><button className="v8-primary" onClick={onOpenPlan}>{t("discovery.openAuthoritative")}</button>{result.curation.rejected_candidate_ids.length > 0 && <small>{t("discovery.rejectedIds")} {result.curation.rejected_candidate_ids.join(", ")}</small>}</aside>
      </div>}
    </section>
  );
}
