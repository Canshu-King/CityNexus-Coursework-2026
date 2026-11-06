import { useEffect, useState } from "react";
import { useLocale, useT } from "../../i18n/LocaleContext";
import { JourneyLinkAction } from "./JourneyLinkAction";
import {
  classifyReadFailure,
  copyFor,
  OriginResponseUnusableError,
} from "../../lib/failureAttribution";
import { formatWhen } from "../../lib/formatWhen";
import { summaryHeldLabel } from "../../lib/displayVocabulary";
import {
  artifactTypeLabel as artifactTypeLabelShared,
  bookingStatusLabel,
  extractionModeLabel as extractionModeLabelShared,
  paymentStatusLabel as paymentStatusLabelShared,
  providerLabel,
  roleLabel,
} from "../../i18n/travelerLabels";

type Draft = {
  id: string;
  artifact_type: string;
  extracted: Record<string, unknown>;
  interpretation?: {
    anchor_role?: string;
    anchor_role_confidence?: number | null;
    semantic_target?: Record<string, unknown> | null;
    payment_status?: string | null;
    reasoning_summary?: string | null;
  };
  smart_intake?: {
    decision?: string;
    reasons?: string[];
    conflicts?: string[];
    resolved_target?: {
      status?: string;
      label?: string;
      semantic_node_key?: string;
      confidence?: number | null;
    } | null;
    role_confidence?: number | null;
  };
  unresolved_fields: string[];
  extraction_mode: string;
  status: string;
  confirmed_at?: string | null;
};

type MemoryItem = {
  id: string;
  kind: string;
  role: string;
  title: string;
  label: string;
  at?: string | null;
  status: string;
  protected: boolean;
  confidence?: number | null;
  reasoning_summary?: string;
  semantic_node_key?: string | null;
};

type Memory = { title: string; items: MemoryItem[] };

type Props = {
  journeyId: string;
  decodedText?: string;
  onConfirmed?: () => Promise<void>;
  /* ORD-0146: the structured-JSON aside on the same page can create a draft this surface has to
     review, and it is not a child of this component.  The shell bumps this token after every import
     so the queue re-reads without a remount -- otherwise "需要审核" points at a list that only
     appears if you happened to create the draft here yourself. */
  importedAt?: number;
};

type ExtractionResponse = {
  status: string;
  provider?: string;
  reason?: string;
  draft?: Draft | null;
};

type ApplyResponse = {
  status: string;
  decision?: string;
  draft?: Draft;
};

/* ORD-0323 cells T05-T13 (Q64/Q67=①).  A rejected reply used to be re-labelled with its own prose and
   handed upward as `new Error(<the reply's sentence>)`, and eight catch sites then printed that message.
   What travels now is the *measurement*: which status answered, with which content type. The wording is
   looked up, not written at the catch site -- `classifyReadFailure` / `copyFor` in lib/failureAttribution.ts
   is the channel this product already uses (`status` is read off the error, never a sentence), so this
   panel does not grow a second vocabulary for the same fact. */
class InboxRequestRejected extends Error {
  readonly status: number;
  readonly content_type: string;
  constructor(status: number, contentType: string) {
    /* Internal vocabulary lives in this message and in the code slots only; every rendered sentence on
       this surface comes from copyFor. */
    super(`inbox request rejected (${status})`);
    this.name = "InboxRequestRejected";
    this.status = status;
    this.content_type = contentType;
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  /* ORD-0323 form F1 (CMDR 04:53).  The body used to be parsed straight away, so a reply that was not
     JSON at all raised the engine's own sentence -- a string that quotes the beginning of whatever came
     back -- and that is what the catch sites printed.  The body is read once as text here, and an
     unusable one is reported as what this device observed: which status answered, with which media type,
     and in what shape the body arrived. */
  const contentType = response.headers.get("content-type") ?? "";
  const raw = await response.text().catch(() => null);
  if (!response.ok) {
    throw new InboxRequestRejected(response.status, contentType);
  }
  if (raw === null || raw.trim() === "") {
    throw new OriginResponseUnusableError(response.status, contentType || null, "empty");
  }
  try {
    return JSON.parse(raw) as T;
  } catch {
    const looksHtml = /^\s*<(?:!doctype|html|[a-z][a-z0-9-]*[\s>])/i.test(raw);
    throw new OriginResponseUnusableError(response.status, contentType || null, looksHtml ? "html" : "not_json");
  }
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  return requestJson<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function readImage(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("The image could not be read."));
    reader.readAsDataURL(file);
  });
}

function dateInput(value: unknown): string {
  return typeof value === "string" ? value.replace(/Z$/, "").slice(0, 16) : "";
}

function serverDate(value: string): string | undefined {
  if (!value) return undefined;
  return value.length === 16 ? `${value}:00+08:00` : value;
}

// Traveller-facing names for booking facts.  Internal field names
// (confirmation_ref, scheduled_departure_at, ...) and raw enum values
// (UNKNOWN_REALTIME, AUTO_APPLY) are implementation vocabulary and must not be
// shown as the primary reading of a confirmed booking.
const BOOKING_FIELDS: { key: string; zh: string; en: string }[] = [
  { key: "reservation_type", zh: "预订类型", en: "Booking type" },
  { key: "title", zh: "名称", en: "Name" },
  { key: "confirmation_ref", zh: "订单号", en: "Reference" },
  { key: "origin", zh: "出发地", en: "From" },
  { key: "destination", zh: "到达地", en: "To" },
  { key: "scheduled_departure_at", zh: "出发时间", en: "Departure" },
  { key: "scheduled_arrival_at", zh: "到达时间", en: "Arrival" },
  { key: "price_amount_minor", zh: "金额", en: "Amount" },
  { key: "currency", zh: "币种", en: "Currency" },
  { key: "provider_key", zh: "来源", en: "Source" },
];

const FIELD_LABELS: Record<string, { zh: string; en: string }> = Object.fromEntries(
  BOOKING_FIELDS.map((field) => [field.key, { zh: field.zh, en: field.en }]),
);

const TYPE_LABELS: Record<string, { zh: string; en: string }> = {
  FLIGHT: { zh: "航班", en: "Flight" },
  RAIL: { zh: "火车", en: "Rail" },
  COACH: { zh: "大巴", en: "Coach" },
  FERRY: { zh: "轮渡", en: "Ferry" },
  HOTEL: { zh: "住宿", en: "Hotel" },
  TICKET: { zh: "门票", en: "Ticket" },
  RESERVATION: { zh: "预订", en: "Reservation" },
};

const DECISION_LABELS: Record<string, { zh: string; en: string }> = {
  AUTO_APPLY: { zh: "可以自动加入行程", en: "Ready to add to your trip" },
  APPLY_WITH_NOTICE: { zh: "加入时会提醒你", en: "Will be added with a notice" },
  REQUIRES_CONFIRMATION: { zh: "需要你先确认", en: "Needs your confirmation" },
  NEEDS_REVIEW: { zh: "需要你补充信息", en: "Needs a few more details" },
  APPLIED: { zh: "已加入行程", en: "Added to your trip" },
};

// The traveller-facing reading of every internal token comes from the shared
// label module.  These thin wrappers keep the call sites short; unknown values
// fall back to the shared safe humanizer (never a raw SCREAMING_CASE enum).
function artifactTypeLabel(value: unknown, zh: boolean): string {
  return artifactTypeLabelShared(value, zh);
}

function extractionModeLabel(value: unknown, zh: boolean): string {
  return extractionModeLabelShared(value, zh);
}

function paymentStatusLabel(value: unknown, zh: boolean): string {
  return paymentStatusLabelShared(value, zh);
}

function anchorRoleLabel(value: unknown, zh: boolean): string {
  return roleLabel(value, zh);
}

function providerKeyLabel(value: unknown, zh: boolean): string {
  return providerLabel(value, zh);
}

function statusLabel(value: unknown, zh: boolean): string {
  return bookingStatusLabel(value, zh);
}

export function fieldLabel(key: string, zh: boolean): string {
  const entry = FIELD_LABELS[key];
  if (entry) return zh ? entry.zh : entry.en;
  return key.replaceAll("_", " ");
}

function decisionLabel(value: string, zh: boolean): string {
  const entry = DECISION_LABELS[String(value).toUpperCase()];
  return entry ? (zh ? entry.zh : entry.en) : zh ? "已处理" : "Handled";
}

function timeLabel(value: unknown, zh: boolean): string {
  const raw = String(value ?? "");
  if (!raw) return zh ? "待确认" : "To confirm";
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) return raw;
  const stamp = parsed.toLocaleString(zh ? "zh-CN" : "en-GB", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
  return stamp;
}

function moneyLabel(minor: unknown, currency: unknown, zh: boolean): string {
  const value = Number(minor);
  if (!Number.isFinite(value)) return zh ? "金额待确认" : "Amount to confirm";
  const major = Math.round(value / 100);
  const code = String(currency || "CNY");
  return zh ? `${code} ${major.toLocaleString("zh-CN")}` : `${code} ${major.toLocaleString("en-GB")}`;
}

function renderFact(value: unknown, zh: boolean): string {
  if (value === null || value === undefined || value === "") return zh ? "待确认" : "To confirm";
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const name = record.name ?? record.location ?? record.title;
    return name ? String(name) : zh ? "已记录" : "Recorded";
  }
  const text = String(value);
  const type = TYPE_LABELS[text.toUpperCase()];
  if (type) return zh ? type.zh : type.en;
  if (/^\d{4}-\d{2}-\d{2}T/.test(text)) return timeLabel(text, zh);
  if (text.includes("-") && text === text.toLowerCase()) return providerKeyLabel(text, zh);
  return text;
}

export function JourneyInboxWorkbench({ journeyId, decodedText, onConfirmed, importedAt }: Props) {
  const t = useT();
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const [text, setText] = useState("");
  const [image, setImage] = useState<File | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  /* ORD-0146: what the server is still holding for review.  This surface used to know only about the
     draft created inside this same mount, which is why a page could demand a review and then offer
     nowhere to do it. */
  const [pending, setPending] = useState<Draft[] | null>(null);
  const [pendingError, setPendingError] = useState<string | null>(null);
  const [confirmDetail, setConfirmDetail] = useState<string | null>(null);
  const [memory, setMemory] = useState<Memory | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [role, setRole] = useState("UNKNOWN");
  const [scheduledDeparture, setScheduledDeparture] = useState("");
  const [scheduledArrival, setScheduledArrival] = useState("");
  // Consumer manual entry: a traveller who has a booking but no text to paste
  // fills this in and goes through the same preview -> confirm path.
  const [manualOpen, setManualOpen] = useState(false);
  const [manualType, setManualType] = useState("RAIL");
  const [manualTitle, setManualTitle] = useState("");
  const [manualRef, setManualRef] = useState("");
  const [manualFrom, setManualFrom] = useState("");
  const [manualTo, setManualTo] = useState("");
  const [manualDepart, setManualDepart] = useState("");
  const [manualArrive, setManualArrive] = useState("");
  const [manualAmount, setManualAmount] = useState("");

  /* One lookup per failure, two products of it: the sentence the traveller reads (copyFor's own table)
     and the machine line the code slots show. Neither of them is read off `error.message`. */
  const deviceCopy = (error: unknown): string => copyFor(classifyReadFailure(error), zh).body;
  const deviceFields = (error: unknown): string => {
    const a = classifyReadFailure(error);
    /* The classification carries the status; the media type is this device's own header read, and the
       channel does not put it on SERVER_ERROR, so it is taken from the measurement rather than invented. */
    const ct = error instanceof InboxRequestRejected
      ? error.content_type
      : error instanceof OriginResponseUnusableError
        ? error.content_type ?? ""
        : "";
    return `http_status=${a.http_status ?? "none"} content_type=${ct || "none"} evidence=${a.evidence_kind}`;
  };

  const confidence = (value: number | null | undefined): string =>
    typeof value === "number" ? t("inbox.confidence", { n: Math.round(value * 100) }) : t("inbox.confidencePending");

  const submitManual = async () => {
    if (!manualTitle.trim()) return;
    setBusy(true);
    setMessage(null);
    try {
      const amountMinor = manualAmount.trim()
        ? Math.round(Number(manualAmount) * 100)
        : null;
      const result = await requestJson<{ draft?: Draft }>(
        `/api/v1/journeys/${journeyId}/inbox/import`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            artifact_type: "STRUCTURED_JSON",
            payload: {
              reservation: {
                reservation_type: manualType,
                title: manualTitle.trim(),
                confirmation_ref: manualRef.trim() || null,
                origin: manualFrom.trim() || null,
                destination: manualTo.trim() || null,
                scheduled_departure_at: serverDate(manualDepart) ?? null,
                scheduled_arrival_at: serverDate(manualArrive) ?? null,
                amount_minor: Number.isFinite(amountMinor) ? amountMinor : null,
                currency: "CNY",
                provider_key: "manual-entry",
              },
            },
          }),
        },
      );
      // /inbox/import returns the draft at the top level.
      const created = (result as unknown as { draft?: Draft }).draft ?? (result as unknown as Draft);
      if (created && (created as Draft).id) {
        setDraft(created as Draft);
        setManualOpen(false);
      } else {
        setMessage(t("inbox.memoryLoadError"));
      }
    } catch (error) {
      setMessage(deviceCopy(error));
    } finally {
      setBusy(false);
    }
  };

  const loadMemory = async () => {
    try {
      setMemory(await requestJson<Memory>(`/api/v1/journeys/${journeyId}/memory`));
    } catch (error) {
      /* ORD-0323 form F1's other half (CMDR 04:53): the sentence alone cannot be checked, so this line
         carries what the device measured too -- which status answered, with which media type. */
      setMessage(`${deviceCopy(error)} · ${deviceFields(error)}`);
    }
  };

  /**
   * ORD-0146: the queue is the server's list, read back -- `GET .../inbox` answers with `status` and
   * `unresolved_fields`, so "needs review" and "what is missing" arrive from one place.  A failed
   * read is named in the queue's own slot: an empty list and an unreadable list must not look alike.
   */
  const readInbox = async (): Promise<Draft[]> => {
    try {
      const body = await requestJson<{ drafts?: Draft[] }>(
        `/api/v1/journeys/${journeyId}/inbox`,
      );
      const drafts = body.drafts ?? [];
      setPending(drafts);
      setPendingError(null);
      return drafts;
    } catch (error) {
      setPending(null);
      setPendingError(deviceFields(error));
      return [];
    }
  };

  useEffect(() => {
    void loadMemory();
  }, [journeyId]);

  useEffect(() => {
    void readInbox();
    // `importedAt` is the shell's nudge after a structured import made outside this component.
  }, [journeyId, importedAt]);

  useEffect(() => {
    if (!draft) return;
    setRole(String(draft.extracted.anchor_role ?? "UNKNOWN"));
    setScheduledDeparture(dateInput(draft.extracted.scheduled_departure_at));
    setScheduledArrival(dateInput(draft.extracted.scheduled_arrival_at));
    /* Whatever put a draft in this panel -- manual entry, a paste, a scan, a queue row -- the list
       next to it has to agree with the server about it, or the queue goes stale on screen. */
    void readInbox();
  }, [draft?.id]);

  const showResult = (result: ExtractionResponse) => {
    if (result.draft) setDraft(result.draft);
    setMessage(
      result.status === "READY"
        ? t("inbox.semanticReady")
        : result.status === "NOT_CONFIGURED"
          ? t("inbox.notConfigured")
          /* ORD-0323 form F2 (CMDR 04:53): `result.reason` is the reply's own sentence, and it was being
             used as this screen's status line.  What the traveller reads now is this device's copy plus the
             reply's machine status; the sentence is not re-labelled as our explanation of it. */
          : `${t("inbox.extractionStatus")} ${result.status}`,
    );
  };

  const extractText = async (value = text) => {
    if (value.trim().length < 3) return;
    setText(value);
    setBusy(true);
    try {
      showResult(
        await postJson<ExtractionResponse>(
          `/api/v1/journeys/${journeyId}/inbox/text-extract`,
          { text: value },
        ),
      );
    } catch (error) {
      setMessage(deviceCopy(error));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (decodedText) void extractText(decodedText);
  }, [decodedText]);

  const extractImage = async () => {
    if (!image) return;
    setBusy(true);
    try {
      showResult(
        await postJson<ExtractionResponse>(
          `/api/v1/journeys/${journeyId}/inbox/vision`,
          { image_url: await readImage(image), hint: "rail, flight, hotel, or attraction confirmation" },
        ),
      );
    } catch (error) {
      setMessage(deviceCopy(error));
    } finally {
      setBusy(false);
    }
  };

  const refreshAfterApply = async (result: ApplyResponse) => {
    if (result.draft) setDraft(result.draft);
    await loadMemory();
    await onConfirmed?.();
    setMessage(
      result.status === "APPLIED"
        ? t("inbox.semanticApplied")
        : t("inbox.stoppedAt", { step: result.status }) + t("inbox.correctHint"),
    );
  };

  const smartApply = async () => {
    if (!draft) return;
    setBusy(true);
    try {
      await refreshAfterApply(
        await postJson<ApplyResponse>(
          `/api/v1/journeys/${journeyId}/inbox/${draft.id}/smart-apply`,
          {},
        ),
      );
    } catch (error) {
      setMessage(deviceCopy(error));
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    if (!draft) return;
    /* An unfinished draft is not this button's to pass -- but swallowing the click is how a traveller
       ends up pressing a grey control three times.  Say which fields the server is still missing. */
    if (draft.unresolved_fields.length) {
      setMessage(
        t("inbox.confirmNeedsFields", {
          fields: draft.unresolved_fields.map((field) => fieldLabel(field, zh)).join("、"),
        }),
      );
      return;
    }
    const draftId = draft.id;
    setBusy(true);
    let failure: string | null = null;
    try {
      await postJson(`/api/v1/journeys/${journeyId}/inbox/${draftId}/confirm`, {});
    } catch (error) {
      failure = `${deviceCopy(error)} · ${deviceFields(error)}`;
    }
    /* The verdict is read back, never written here.  `confirm_reservation_draft` is the one that
       decides, and its answer can still be "not passed" (a duplicate confirmation_ref, a field that
       only resolves against the journey window); a locally stamped `status:"CONFIRMED"` with
       `confirmed_at: new Date()` was the front end grading its own homework, which is exactly what
       ORD-0146 ① forbids.  ORD-0323: what sits in the technical fold is this device's own reading of the
       attempt (status, content type), not the reply's sentence -- `requestJson` stopped carrying that up,
       so paraphrasing it here is not what this line avoids, it never arrives in the first place. */
    const fresh = (await readInbox()).find((item) => item.id === draftId) ?? null;
    setDraft(fresh ?? draft);
    await loadMemory();
    await onConfirmed?.();
    setBusy(false);
    const passed = fresh?.status === "CONFIRMED";
    setConfirmDetail(passed ? null : failure ?? t("inbox.confirmUnreadable"));
    setMessage(
      passed
        ? t("inbox.confirmedProtected")
        : `${t("inbox.confirmRefused", {
            status: statusLabel(fresh?.status ?? draft.status, zh),
          })}${
            fresh?.unresolved_fields.length
              ? ` ${t("inbox.needsReview")} ${fresh.unresolved_fields
                  .map((field) => fieldLabel(field, zh))
                  .join("、")}`
              : ""
          }`,
    );
  };

  const saveCorrections = async () => {
    if (!draft) return;
    setBusy(true);
    try {
      const patched = await requestJson<Draft>(
        `/api/v1/journeys/${journeyId}/inbox/${draft.id}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            anchor_role: role,
            scheduled_departure_at: serverDate(scheduledDeparture),
            scheduled_arrival_at: serverDate(scheduledArrival),
          }),
        },
      );
      /* What the corrections achieved is the server's answer, not this component's hope: re-read the
         list (which is also what the queue beside the panel shows) and keep the row it reports. */
      const fresh = (await readInbox()).find((item) => item.id === patched.id) ?? patched;
      setDraft(fresh);
      setMessage(
        fresh.unresolved_fields.length
          ? t("inbox.correctionsSavedStillNeeds", {
              fields: fresh.unresolved_fields.map((field) => fieldLabel(field, zh)).join("、"),
            })
          : t("inbox.correctionsSaved"),
      );
    } catch (error) {
      setMessage(deviceCopy(error));
    } finally {
      setBusy(false);
    }
  };

  const decision = draft?.smart_intake?.decision ?? "NEEDS_REVIEW";
  const unreviewed = (pending ?? []).filter((item) => item.status !== "CONFIRMED");
  const target = draft?.interpretation?.semantic_target;
  const targetLabel = String(
    draft?.smart_intake?.resolved_target?.label ?? target?.resolved_label ?? target?.query ?? t("inbox.unboundTarget"),
  );
  const canSmartApply = Boolean(
    draft &&
      draft.status !== "CONFIRMED" &&
      !draft.unresolved_fields.length &&
      ["AUTO_APPLY", "APPLY_WITH_NOTICE"].includes(decision),
  );

  return (
    <section className="v10-inbox-workbench" aria-label={t("inbox.inboxExtraction")}>
      <div className="v10-inbox-heading">
        <div>
          <span className="v8-kicker">{t("inbox.multimodal")}</span>
          <h2>{t("inbox.bringConfirmation")}</h2>
        </div>
        <span className="v10-truth-pill">{t("inbox.semanticDraft")}</span>
      </div>
      <JourneyLinkAction journeyId={journeyId} />
      <div className="inbox-entry-tabs">
        <button
          type="button"
          className={!manualOpen ? "active" : ""}
          data-testid="inbox-tab-paste"
          onClick={() => setManualOpen(false)}
        >
          {t("inbox.tabPaste")}
        </button>
        <button
          type="button"
          className={manualOpen ? "active" : ""}
          data-testid="inbox-tab-manual"
          onClick={() => setManualOpen(true)}
        >
          {t("inbox.tabManual")}
        </button>
      </div>

      {manualOpen && (
        <form
          className="inbox-manual-form"
          data-testid="inbox-manual-form"
          onSubmit={(event) => {
            event.preventDefault();
            void submitManual();
          }}
        >
          <label>
            {t("inbox.manualType")}
            <select
              data-testid="manual-type"
              value={manualType}
              onChange={(event) => setManualType(event.target.value)}
            >
              <option value="FLIGHT">{t("inbox.typeFlight")}</option>
              <option value="RAIL">{t("inbox.typeRail")}</option>
              <option value="HOTEL">{t("inbox.typeHotel")}</option>
              <option value="TICKET">{t("inbox.typeTicket")}</option>
              <option value="RESERVATION">{t("inbox.typeReservation")}</option>
            </select>
          </label>
          <label>
            {t("inbox.manualTitle")}
            <input
              data-testid="manual-title"
              value={manualTitle}
              onChange={(event) => setManualTitle(event.target.value)}
              placeholder={t("inbox.manualTitleHint")}
              required
            />
          </label>
          <label>
            {t("inbox.manualRef")}
            <input
              data-testid="manual-ref"
              value={manualRef}
              onChange={(event) => setManualRef(event.target.value)}
            />
          </label>
          <div className="inbox-manual-row">
            <label>
              {t("inbox.manualFrom")}
              <input
                data-testid="manual-from"
                value={manualFrom}
                onChange={(event) => setManualFrom(event.target.value)}
              />
            </label>
            <label>
              {t("inbox.manualTo")}
              <input
                data-testid="manual-to"
                value={manualTo}
                onChange={(event) => setManualTo(event.target.value)}
              />
            </label>
          </div>
          <div className="inbox-manual-row">
            <label>
              {t("inbox.manualDepart")}
              <input
                type="datetime-local"
                data-testid="manual-depart"
                value={manualDepart}
                onChange={(event) => setManualDepart(event.target.value)}
              />
            </label>
            <label>
              {t("inbox.manualArrive")}
              <input
                type="datetime-local"
                data-testid="manual-arrive"
                value={manualArrive}
                onChange={(event) => setManualArrive(event.target.value)}
              />
            </label>
          </div>
          <label>
            {t("inbox.manualAmount")}
            <input
              data-testid="manual-amount"
              inputMode="decimal"
              value={manualAmount}
              onChange={(event) => setManualAmount(event.target.value)}
            />
          </label>
          <button
            type="submit"
            className="v8-primary-button"
            data-testid="manual-submit"
            disabled={busy || !manualTitle.trim()}
          >
            {t("inbox.manualPreview")}
          </button>
        </form>
      )}

      {!manualOpen && (
      <div className="v10-inbox-inputs">
        <label>
          {t("inbox.pastedConfirmation")}
          <textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder={t("inbox.pasteHint")}
          />
          <button type="button" onClick={() => void extractText()} disabled={busy || text.trim().length < 3}>
            {t("inbox.extractText")}
          </button>
        </label>
        <label>
          {t("inbox.screenshot")}
          <input type="file" accept="image/*" onChange={(event) => setImage(event.target.files?.[0] ?? null)} />
          <small>{image ? image.name : t("inbox.imageHint")}</small>
          <button type="button" onClick={() => void extractImage()} disabled={busy || !image}>
            {t("inbox.extractImage")}
          </button>
        </label>
      </div>
      )}
      {message && <p className="v10-inbox-message" role="status">{message}</p>}
      {confirmDetail && (
        <details className="v8-advanced" data-testid="inbox-confirm-detail">
          <summary>{t("inbox.technicalDetail")}</summary>
          <code>{confirmDetail}</code>
        </details>
      )}

      <section className="v13-2-memory-panel" data-testid="v13-2-memory" aria-label={t("inbox.journeyMemory")}>
        <div className="v10-inbox-heading">
          <div>
            <span className="v8-kicker">{t("inbox.authoritativeMemory")}</span>
            {/* ORD-0423 defect one: `memory.title` is prose the server writes for itself
                (semantic_intake.py:489 = "Journey Memory"), so `??` never reached the dictionary and a zh
                screen read English.  The heading is this panel's own label, so it comes from the dictionary. */}
            <h3>{t("inbox.journeyMemory")}</h3>
          </div>
          <small>{t("inbox.protectedFacts", { n: memory?.items.length ?? 0 })}</small>
        </div>
        {memory?.items.length ? (
          <ol>
            {memory.items.map((item) => (
              <li key={item.id}>
                <strong>{item.title} · {roleLabel(item.role, zh)}</strong>
                <span>{item.label} · {formatWhen(item.at, { zh, pending: t("inbox.timePending") })}</span>
                <small>{item.protected ? t("inbox.protected") : t("inbox.candidate")} · {confidence(item.confidence)}</small>
                {/* ORD-0323 forms F3/F4 (CMDR 04:53): the summary is the upstream's sentence, and printed
                    here it read as this panel's own claim.  The row keeps stating what it measured. */}
                {item.reasoning_summary ? <small data-testid="v13-2-memory-summary-held">{summaryHeldLabel(zh)}</small> : null}
              </li>
            ))}
          </ol>
        ) : (
          <p>{t("inbox.noArtifact")}</p>
        )}
      </section>

      {/* ORD-0146: the review the sentence asks for, for every draft the server is still holding --
          not just the one this mount happened to create.  The row's own data comes from
          `GET .../inbox` (`status`, `unresolved_fields`), so "需要审核" and "缺什么" are one read. */}
      <section
        className="v13-2-queue"
        data-testid="inbox-pending-queue"
        aria-label={t("inbox.pendingQueue")}
      >
        <b>{t("inbox.pendingQueue")}</b>
        {pendingError ? (
          <p className="v10-inbox-warning" role="alert" data-testid="inbox-pending-error">
            {t("inbox.pendingUnreadable")}
            <code> {pendingError}</code>{" "}
            <button type="button" data-testid="inbox-pending-retry" onClick={() => void readInbox()}>
              {t("inbox.retryQueue")}
            </button>
          </p>
        ) : unreviewed.length === 0 ? (
          <p data-testid="inbox-pending-empty">{t("inbox.queueEmpty")}</p>
        ) : null}
        {unreviewed.map((item) => (
          <div className="v13-2-queue-row" key={item.id} data-testid="inbox-pending-row">
            <span>
              {artifactTypeLabel(item.artifact_type, zh)} · {statusLabel(item.status, zh)}
            </span>
            <b>
              {String(
                item.extracted.confirmation_ref ??
                  item.extracted.title ??
                  t("inbox.unnamedArtifact"),
              )}
            </b>
            <small>
              {item.unresolved_fields.length
                ? `${t("inbox.needsReview")} ${item.unresolved_fields
                    .map((field) => fieldLabel(field, zh))
                    .join("、")}`
                : t("inbox.readyToConfirm")}
            </small>
            <button
              type="button"
              data-testid="inbox-open-review"
              onClick={() => {
                setDraft(item);
                setMessage(null);
                setConfirmDetail(null);
              }}
            >
              {t("inbox.openReview")}
            </button>
          </div>
        ))}
      </section>

      {draft && (
        <div className="v10-inbox-draft" data-testid="v10-inbox-draft">
          <div>
            <b>
              {artifactTypeLabel(draft.artifact_type, zh)} ·{" "}
              {statusLabel(draft.status, zh)}
            </b>
            <small>{extractionModeLabel(draft.extraction_mode, zh)}</small>
          </div>
          <section className="v13-2-interpretation" data-testid="v13-2-interpretation">
            <b>{t("inbox.semanticInterpretation")}</b>
            <span>
              {anchorRoleLabel(draft.interpretation?.anchor_role ?? "UNKNOWN", zh)} ·{" "}
              {confidence(draft.interpretation?.anchor_role_confidence)}
            </span>
            <span>{t("inbox.target")} {targetLabel}</span>
            <span>
              {t("inbox.payment")}{" "}
              {paymentStatusLabel(draft.interpretation?.payment_status ?? "UNKNOWN", zh)}
            </span>
            {draft.interpretation?.reasoning_summary
              ? <small data-testid="v13-2-draft-summary-held">{summaryHeldLabel(zh)}</small>
              : null}
          </section>
          <section className="v13-2-review" data-testid="v13-2-review" aria-label={t("inbox.reviewSemantic")}>
            <label>
              {t("inbox.anchorRole")}
              <select data-testid="v13-2-anchor-role" value={role} onChange={(event) => setRole(event.target.value)} disabled={draft.status === "CONFIRMED"}>
                <option value="UNKNOWN">{t("inbox.roleUnknown")}</option>
                <option value="ARRIVAL">{t("inbox.roleArrival")}</option>
                <option value="MID_TRIP">{t("inbox.roleMidTrip")}</option>
                <option value="DEPARTURE">{t("inbox.roleDeparture")}</option>
              </select>
            </label>
            <label>
              {t("inbox.scheduledDeparture")}
              <input type="datetime-local" value={scheduledDeparture} onChange={(event) => setScheduledDeparture(event.target.value)} disabled={draft.status === "CONFIRMED"} />
            </label>
            <label>
              {t("inbox.scheduledArrival")}
              <input type="datetime-local" value={scheduledArrival} onChange={(event) => setScheduledArrival(event.target.value)} disabled={draft.status === "CONFIRMED"} />
            </label>
            <button
              type="button"
              data-testid="v13-2-save-corrections"
              onClick={() => void saveCorrections()}
              disabled={busy || draft.status === "CONFIRMED"}
            >
              {t("inbox.saveCorrections")}
            </button>
          </section>
          <dl>
            {BOOKING_FIELDS.filter((field) => draft.extracted[field.key] != null)
              .map((field) => (
                <div key={field.key}>
                  <dt>{zh ? field.zh : field.en}</dt>
                  <dd>{renderFact(draft.extracted[field.key], zh)}</dd>
                </div>
              ))}
          </dl>
          <p className={draft.unresolved_fields.length ? "v10-inbox-warning" : "v10-inbox-clear"}>
            {draft.unresolved_fields.length
              ? `${t("inbox.needsReview")} ${draft.unresolved_fields
                  .map((field) => fieldLabel(field, zh))
                  .join("、")}`
              : zh
                ? `已核对：${decisionLabel(decision, zh)} · ${targetLabel}`
                : `Checked: ${decisionLabel(decision, zh)} · ${targetLabel}`}
          </p>
          <button data-testid="v13-2-smart-apply" type="button" onClick={() => void smartApply()} disabled={busy || !canSmartApply}>
            {draft.status === "CONFIRMED" ? t("inbox.applied") : canSmartApply ? t("inbox.applySmart") : t("inbox.reviewRequired")}
          </button>
          <button
            type="button"
            data-testid="v13-2-confirm"
            onClick={() => void confirm()}
            disabled={busy || Boolean(draft.unresolved_fields.length) || draft.status === "CONFIRMED"}
          >
            {draft.status === "CONFIRMED" ? t("inbox.confirmed") : t("inbox.confirmProtect")}
          </button>
          {/* Internal vocabulary lives here only: the traveller-facing surface
              above never shows a raw enum or provider key. */}
          <details className="v8-advanced" data-testid="inbox-advanced">
            <summary>{t("inbox.advancedRaw")}</summary>
            <dl className="inbox-advanced-raw">
              <div>
                <dt>{t("inbox.rawArtifact")}</dt>
                <dd>{String(draft.artifact_type ?? "")}</dd>
              </div>
              <div>
                <dt>{t("inbox.rawStatus")}</dt>
                <dd>{String(draft.status ?? "")}</dd>
              </div>
              <div>
                <dt>{t("inbox.rawExtraction")}</dt>
                <dd>{String(draft.extraction_mode ?? "")}</dd>
              </div>
              <div>
                <dt>{t("inbox.rawAnchorRole")}</dt>
                <dd>{String(draft.interpretation?.anchor_role ?? "UNKNOWN")}</dd>
              </div>
              <div>
                <dt>{t("inbox.rawPayment")}</dt>
                <dd>{String(draft.interpretation?.payment_status ?? "UNKNOWN")}</dd>
              </div>
              {draft.extracted.provider_key != null && (
                <div>
                  <dt>{t("inbox.rawProvider")}</dt>
                  <dd>{String(draft.extracted.provider_key)}</dd>
                </div>
              )}
              {draft.extracted.anchor_role != null && (
                <div>
                  <dt>{t("inbox.rawKind")}</dt>
                  <dd>{String(draft.extracted.anchor_role)}</dd>
                </div>
              )}
            </dl>
          </details>
        </div>
      )}
    </section>
  );
}
