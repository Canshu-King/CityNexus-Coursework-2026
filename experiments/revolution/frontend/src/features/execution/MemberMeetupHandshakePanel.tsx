import { useState } from "react";
import { cityNexusApi, type JourneyGroupPresenceProjection, type MemberSideQuestProjection } from "../../api/citynexus";
import { useLocale } from "../../i18n/LocaleContext";

export function MemberMeetupHandshakePanel({
  journeyId,
  projection,
  sideQuest,
  onUpdated,
  showOwner = true,
  showGroup = true,
}: {
  journeyId: string;
  projection: JourneyGroupPresenceProjection["meetup_handshake"];
  sideQuest: MemberSideQuestProjection | null;
  onUpdated: () => Promise<void>;
  showOwner?: boolean;
  showGroup?: boolean;
}) {
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [confirmingReport, setConfirmingReport] = useState(false);
  if (
    !projection
    || (showOwner && !projection.owner && (!showGroup || (projection.incoming.length === 0 && projection.resolved.length === 0)))
    || (!showOwner && (!showGroup || (projection.incoming.length === 0 && projection.resolved.length === 0)))
  ) return null;

  async function reportArrival() {
    if (!sideQuest?.id) return;
    setBusyId("owner");
    setMessage(null);
    try {
      await cityNexusApi.reportMemberMeetupArrival(journeyId, sideQuest.id);
      setConfirmingReport(false);
      await onUpdated();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : (zh ? "暂时无法提交会合报告。" : "The meetup report could not be submitted."));
    } finally {
      setBusyId(null);
    }
  }

  async function decide(handshakeId: string, decision: "acknowledge" | "not-yet") {
    setBusyId(handshakeId);
    setMessage(null);
    try {
      await cityNexusApi.decideMemberMeetupHandshake(journeyId, handshakeId, decision);
      await onUpdated();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : (zh ? "暂时无法提交确认。" : "The confirmation could not be submitted."));
    } finally {
      setBusyId(null);
    }
  }

  const owner = projection.owner;
  const canShowReport = Boolean(
    owner?.can_report
    && sideQuest
    && ["COMPLETED", "CANCELLED"].includes(sideQuest.state),
  );

  return (
    <section className="journey-side-quest-handshake" data-testid="member-meetup-handshake">
      {showOwner && owner && (
        <div data-testid="member-meetup-owner-status" data-state={owner.state}>
          <strong>
            {owner.state === "PEER_ACKNOWLEDGED"
              ? (zh
                ? `你和${owner.responder_display_name ?? "同行者"}都报告在「${owner.rendezvous.place_name ?? "共同会合点"}」会合；你的分开行动已结束，共同行程可以继续。`
                : `You and ${owner.responder_display_name ?? "a fellow traveller"} reported meeting at ${owner.rendezvous.place_name ?? "the shared meetup"}. Your planned side trip ended; the shared Journey can continue.`)
              : owner.state === "PENDING"
                ? (zh ? "正在等待一位同行者回应会合报告" : "Waiting for a fellow traveller to respond")
                : owner.state === "NOT_CONFIRMED"
                  ? (zh ? "同行者暂时无法确认会合" : "A fellow traveller could not confirm the meetup yet")
                  : owner.state === "EXPIRED"
                    ? (zh ? "会合确认请求已过期" : "The meetup confirmation request expired")
                    : owner.state === "INVALIDATED"
                      ? (zh ? "会合确认请求已失效" : "The meetup confirmation request is no longer current")
                      : (zh ? "你可以报告已到达共同会合点" : "You can report that you reached the shared meetup")}
          </strong>
          {owner.state === "PENDING" && owner.expires_at && (
            <small>{zh ? `请求有效至 ${new Date(owner.expires_at).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })}。` : `Request expires at ${new Date(owner.expires_at).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })}.`}</small>
          )}
          {owner.blocked_reason === "R62_REJOIN_REQUIRED" && (
            <small>{zh ? "请在当前群体返场流程中确认到达；此请求不会替代该流程。" : "Confirm arrival in the active group recovery flow; this request cannot replace it."}</small>
          )}
          {owner.blocked_reason === "NO_PEER_AVAILABLE" && (
            <small>{zh ? "目前没有其他有效同行者可回应；你仍可使用原有方式结束自己的分离行动。" : "No other current member is available to respond; you can still end your own split using the existing Journey control."}</small>
          )}
          {owner.state === "INVALIDATED" && (
            <small>{zh ? "当前支线或会合点已变化；请先核对现在的行程与支线，之后再提交新的本人报告。" : "The side trip or meetup changed. Review the current Journey and side trip before making a new report."}</small>
          )}
          {canShowReport && (
            <div className="journey-side-quest-actions">
              {!confirmingReport ? (
                <button
                  type="button"
                  data-testid="member-meetup-report-arrival"
                  disabled={busyId !== null}
                  onClick={() => setConfirmingReport(true)}
                >
                  {zh ? "我已到共同会合点" : "I’m at our shared meetup"}
                </button>
              ) : (
                <section
                  role="dialog"
                  aria-modal="true"
                  aria-labelledby="member-meetup-report-confirm-title"
                  data-testid="member-meetup-report-confirmation"
                >
                  <strong id="member-meetup-report-confirm-title">
                    {zh ? "确认发送会合报告？" : "Confirm your meetup report?"}
                  </strong>
                  <p>
                    {zh
                      ? "我报告自己已到达这个共同会合点。这是我的本人报告，不是 GPS、设备定位或实际在场证明；共享行程不会改变。"
                      : "I’m reporting that I reached the exact shared rendezvous. This is my report, not GPS, device-location, or physical-presence proof; the shared Journey will not change."}
                  </p>
                  <div className="journey-side-quest-actions">
                    <button
                      type="button"
                      data-testid="member-meetup-report-confirm"
                      disabled={busyId !== null}
                      onClick={() => void reportArrival()}
                    >
                      {busyId === "owner" ? (zh ? "正在发送请求…" : "Sending request…") : (zh ? "确认并请求同行者回应" : "Confirm and ask a fellow traveller")}
                    </button>
                    <button
                      type="button"
                      data-testid="member-meetup-report-cancel"
                      disabled={busyId !== null}
                      onClick={() => setConfirmingReport(false)}
                    >
                      {zh ? "取消" : "Cancel"}
                    </button>
                  </div>
                </section>
              )}
            </div>
          )}
          {owner.state === "PEER_ACKNOWLEDGED" && (
            <small data-testid="member-meetup-no-physical-proof">
              {zh ? "这是对你本人报告的同行者回应，不是设备定位或实际在场证明；共享行程未改变。" : "This is a peer response to your report, not device-location or physical-presence proof. The shared Journey is unchanged."}
            </small>
          )}
        </div>
      )}
      {showGroup && projection.incoming.map((request) => (
        <article key={request.id} data-testid="member-meetup-incoming" data-handshake-id={request.id}>
          <strong>
            {zh
              ? `${request.owner_display_name} 报告已到达共同会合点`
              : `${request.owner_display_name} reports reaching the shared meetup`}
          </strong>
          <small>{zh ? `会合点：${request.rendezvous.place_name}` : `Meetup: ${request.rendezvous.place_name}`}</small>
          <small>{zh ? "请只根据你自己的情况回应；这不会验证设备位置。" : "Respond based on what you know; this does not verify device location."}</small>
          <div className="journey-side-quest-actions">
            <button
              type="button"
              data-testid="member-meetup-acknowledge"
              disabled={busyId !== null}
              onClick={() => void decide(request.id, "acknowledge")}
            >
              {busyId === request.id ? (zh ? "正在提交…" : "Submitting…") : (zh ? "确认已会合" : "Acknowledge meetup")}
            </button>
            <button
              type="button"
              data-testid="member-meetup-not-yet"
              disabled={busyId !== null}
              onClick={() => void decide(request.id, "not-yet")}
            >
              {zh ? "暂时无法确认" : "Can’t confirm yet"}
            </button>
          </div>
        </article>
      ))}
      {showGroup && projection.resolved.map((result) => (
        <article key={result.id} data-testid="member-meetup-resolved" data-state={result.state}>
          <strong>
            {result.viewer_acknowledged
              ? (zh
                ? `你和${result.owner_display_name}都报告在「${result.rendezvous.place_name}」会合；其分开行动已结束，共同行程可以继续。`
                : `You and ${result.owner_display_name} reported meeting at ${result.rendezvous.place_name}. Their planned side trip ended; the shared Journey can continue.`)
              : (zh
                ? `${result.owner_display_name}和${result.responder_display_name}双方报告在「${result.rendezvous.place_name}」会合；只是这两位成员的报告，其他同行者并未确认。`
                : `${result.owner_display_name} and ${result.responder_display_name} reported meeting at ${result.rendezvous.place_name}. Only those two travellers reported this meeting; other group members have not confirmed.`)}
          </strong>
          <small>
            {zh
              ? `回应者：${result.responder_display_name}。这是双方本人报告，不是设备定位或全员会合证明；共享计划未改变。`
              : `Acknowledged by ${result.responder_display_name}. These are member reports, not device-location or all-group proof; the shared plan is unchanged.`}
          </small>
        </article>
      ))}
      {message && <small role="status" data-testid="member-meetup-message">{message}</small>}
    </section>
  );
}
