import { useState } from "react";
import {
  CityNexusApiError,
  cityNexusApi,
  type MemberPlaceInvitationFeasibilityPreviewResult,
  type MemberPlaceGroupSharePreviewResult,
  type MemberPlaceInvitationOwnProgress,
  type MemberPlaceInvitationProgressParticipant,
  type MemberPlaceInvitationProjection,
  type MemberPlacePromotionProjection,
  type SharedStopCoPlanningGrantProjection,
} from "../../api/citynexus";
import { useLocale } from "../../i18n/LocaleContext";
import { buildOpenStreetMapPinUrl } from "./memberSideQuestNavigationLinks";

export function MemberPlaceInvitationPanel({
  journeyId,
  projection,
  onRefreshLocation,
  onUpdated,
}: {
  journeyId: string;
  projection: MemberPlaceInvitationProjection | null | undefined;
  onRefreshLocation?: () => Promise<string | null>;
  onUpdated: () => Promise<void>;
}) {
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [pinConsentId, setPinConsentId] = useState<string | null>(null);
  const [routeConsentId, setRouteConsentId] = useState<string | null>(null);
  const [progressConsent, setProgressConsent] = useState<{
    invitationId: string;
    placeName: string;
    action: "SHARE_ARRIVAL" | "SHARE_HEADING_BACK";
    shareAudienceToken: string;
    audienceNames: string[];
  } | null>(null);
  const [groupSharePreviewById, setGroupSharePreviewById] = useState<
    Record<string, MemberPlaceGroupSharePreviewResult | null>
  >({});
  const [feasibilityById, setFeasibilityById] = useState<Record<string, MemberPlaceInvitationFeasibilityPreviewResult | null>>({});
  const [dwellById, setDwellById] = useState<Record<string, 10 | 20 | 30>>({});
  const [promotionDwellById, setPromotionDwellById] = useState<Record<string, 10 | 20 | 30>>({});
  const [coPlanningSelectionByInvitation, setCoPlanningSelectionByInvitation] = useState<Record<string, string>>({});
  const invitations = projection?.incoming ?? [];
  const sentInvitations = projection?.sent ?? [];
  const joinedInvitations = projection?.joined ?? [];
  const promotions = projection?.promotions ?? [];
  const coPlanningGrants = projection?.co_planning_grants ?? [];
  if (!invitations.length && !sentInvitations.length && !joinedInvitations.length && !promotions.length && !coPlanningGrants.length) return null;

  const respond = async (
    invitationId: string,
    decision: "ACCEPT" | "DECLINE",
    feasibility?: { token?: string; acknowledgeUnknown?: boolean; acknowledgeRisk?: boolean },
  ) => {
    if (busyId) return;
    setBusyId(invitationId);
    setMessage(null);
    try {
      await cityNexusApi.respondToMemberPlaceInvitation(journeyId, invitationId, decision, feasibility);
      await onUpdated();
      setMessage(decision === "ACCEPT"
        ? (zh ? "已为你创建独立支线；共享行程未改变，也没有自动打开地图。" : "Your own side trip is ready. The shared plan is unchanged and no map was opened.")
        : (zh ? "已谢绝这个地点邀请。" : "Place invitation declined."));
    } catch {
      setMessage(zh
        ? "邀请已失效、你的行程状态有冲突，或暂时无法完成；没有覆盖你现有的支线。刷新后可查看状态。"
        : "The invitation may be stale or your current trip state conflicts. Your existing side trip was not replaced; refresh to review the status.");
      await onUpdated();
    } finally {
      setBusyId(null);
    }
  };

  const checkFeasibility = async (invitationId: string) => {
    if (busyId) return;
    setBusyId(invitationId);
    setRouteConsentId(null);
    setMessage(null);
    try {
      const observationId = onRefreshLocation ? await onRefreshLocation() : null;
      const result = await cityNexusApi.previewMemberPlaceInvitationFeasibility(
        journeyId,
        invitationId,
        observationId,
        dwellById[invitationId] ?? 20,
      );
      setFeasibilityById((current) => ({ ...current, [invitationId]: result }));
    } catch {
      setFeasibilityById((current) => {
        const next = { ...current };
        delete next[invitationId];
        return next;
      });
      setMessage(zh
        ? "路线检查未完成，不能把请求失败当作已确认的“时间未核实”结果。请刷新重试；如不愿共享位置，可在同意弹窗中主动选择跳过。"
        : "The route check failed; this is not a verified UNKNOWN preview. Refresh and retry, or explicitly skip location sharing in the consent dialog.");
    } finally {
      setBusyId(null);
    }
  };

  const updateProgress = async (
    invitationId: string,
    action: "SHARE_ARRIVAL" | "WITHDRAW_ARRIVAL" | "SHARE_HEADING_BACK" | "WITHDRAW_HEADING_BACK",
    shareAudienceToken?: string,
  ) => {
    if (busyId) return;
    setBusyId(invitationId);
    setMessage(null);
    try {
      await cityNexusApi.updateMemberPlaceInvitationProgress(
        journeyId,
        invitationId,
        action,
        shareAudienceToken,
      );
      await onUpdated();
      setMessage(action.startsWith("SHARE_")
        ? (zh ? "已向本次邀请中已加入的同行者分享你的自述；它不是 GPS 验证。" : "Your self-report is shared with members who joined this invitation; it is not GPS verified.")
        : (zh ? "已停止在后续视图中分享；此前已看见的信息无法撤回。" : "Future views no longer show this report; information already seen cannot be recalled."));
    } catch (error) {
      const body = error instanceof CityNexusApiError && error.body && typeof error.body === "object"
        ? error.body as { detail?: unknown; error?: { message?: unknown } }
        : null;
      const detail = String(body?.detail ?? body?.error?.message ?? "");
      if (detail === "PLACE_INVITATION_PROGRESS_AUDIENCE_CHANGED") {
        setProgressConsent(null);
        setMessage(zh
          ? "邀请中的成员名单刚有变化。为避免把自述分享给你尚未确认的对象，本次分享已取消；请查看刷新后的名单并重新确认。"
          : "The invitation audience changed. This share was cancelled so you can review the updated members and confirm again.");
        await onUpdated();
        return;
      }
      setMessage(zh
        ? "无法更新分享状态。你的行程没有被更改；刷新后可查看当前进度。"
        : "The sharing status could not be updated. Your Journey was not changed; refresh to review progress.");
      await onUpdated();
    } finally {
      setBusyId(null);
    }
  };

  const createPromotion = async (invitationId: string) => {
    if (busyId) return;
    setBusyId(invitationId);
    setMessage(null);
    try {
      await cityNexusApi.createMemberPlacePromotion(
        journeyId,
        invitationId,
        promotionDwellById[invitationId] ?? 20,
        `r73-create-${crypto.randomUUID()}`,
      );
      await onUpdated();
      setMessage(zh
        ? "已提交共享站点提议；只有路线与承诺核验通过、所有当前成员各自同意后，才可由成员应用到共享行程。"
        : "Shared-stop proposal submitted. It can be applied only after route and commitment checks pass and every current member approves independently.");
    } catch {
      setMessage(zh
        ? "提议未能进入可执行状态。共享行程未改变；请刷新查看具体路线证据或当前恢复状态。"
        : "The proposal did not become executable. The shared Journey is unchanged; refresh to review route evidence or an active recovery.");
      await onUpdated();
    } finally {
      setBusyId(null);
    }
  };

  const previewGroupShare = async (invitationId: string) => {
    if (busyId) return;
    setBusyId(invitationId);
    setMessage(null);
    try {
      const preview = await cityNexusApi.previewMemberPlaceGroupShare(journeyId, invitationId);
      setGroupSharePreviewById((current) => ({ ...current, [invitationId]: preview }));
    } catch (error) {
      const body = error instanceof CityNexusApiError && error.body && typeof error.body === "object"
        ? error.body as { detail?: unknown; error?: { message?: unknown } }
        : null;
      const code = String(body?.detail ?? body?.error?.message ?? "");
      setMessage(code === "AUDIENCE_CHANGED"
        ? (zh ? "同行成员名单发生变化；没有扩大分享。请刷新后重新查看名单。" : "The Journey audience changed. Nothing was shared; refresh and review the current list.")
        : code === "GROUP_SHARE_REQUIRES_ACCEPTED_INVITEE"
          ? (zh ? "先等待至少一位受邀同行者接受，再发起全组分享。" : "Wait for an invited companion to accept before sharing with the whole Journey.")
          : (zh ? "无法创建当前分享预览；地点仍只对原邀请对象可见。" : "The current sharing preview is unavailable; the Place remains visible only to the original invitees."));
      await onUpdated();
    } finally {
      setBusyId(null);
    }
  };

  const confirmGroupShare = async (
    invitationId: string,
    preview: MemberPlaceGroupSharePreviewResult,
  ) => {
    if (busyId) return;
    setBusyId(invitationId);
    setMessage(null);
    try {
      await cityNexusApi.confirmMemberPlaceGroupShare(journeyId, invitationId, preview);
      setGroupSharePreviewById((current) => ({ ...current, [invitationId]: null }));
      await onUpdated();
      setMessage(zh
        ? "已向预览中的当前同行者分享这个公开地点；改变共同计划仍需他们逐人另行决定。"
        : "The public Place was shared with the reviewed Journey audience. Any shared-plan change still needs separate decisions from every required member.");
    } catch (error) {
      const body = error instanceof CityNexusApiError && error.body && typeof error.body === "object"
        ? error.body as { detail?: unknown; error?: { message?: unknown } }
        : null;
      const code = String(body?.detail ?? body?.error?.message ?? "");
      setGroupSharePreviewById((current) => ({ ...current, [invitationId]: null }));
      setMessage(code === "AUDIENCE_CHANGED"
        ? (zh ? "名单在确认前变化；没有分享。请重新打开预览核对新名单。" : "The audience changed before confirmation; nothing was shared. Reopen the preview to review the new list.")
        : code === "PLAN_CHANGED"
          ? (zh ? "共享计划或当前执行点已变化；本次分享没有生效。" : "The shared plan or current execution cursor changed; this share was not applied.")
          : code === "INVITER_SPLIT_STALE"
            ? (zh ? "你的独立探索或会合信息已变化；本次分享没有生效。" : "Your private split or rendezvous context changed; this share was not applied.")
            : (zh ? "分享未生效；原行程和成员权限没有改变。" : "The share was not applied; the Journey plan and member roles are unchanged."));
      await onUpdated();
    } finally {
      setBusyId(null);
    }
  };

  const withdrawGroupShare = async (invitationId: string) => {
    if (busyId) return;
    setBusyId(invitationId);
    setMessage(null);
    try {
      await cityNexusApi.withdrawMemberPlaceGroupShare(journeyId, invitationId);
      await onUpdated();
      setMessage(zh
        ? "已停止后续投影和新提议；此前已经看到地点的成员无法被当作从未见过。"
        : "Future projections and new proposals are disabled. Members who already saw the Place cannot be treated as if they never saw it.");
    } catch {
      setMessage(zh ? "无法撤回全组分享；刷新查看当前状态。" : "The group share could not be withdrawn; refresh to review its current state.");
      await onUpdated();
    } finally {
      setBusyId(null);
    }
  };

  const offerCoPlanning = async (invitationId: string, memberId: string) => {
    if (busyId || !memberId) return;
    setBusyId(invitationId);
    setMessage(null);
    try {
      await cityNexusApi.offerSharedStopCoPlanningGrant(
        journeyId,
        invitationId,
        memberId,
        `r74-coplan-${crypto.randomUUID()}`,
      );
      await onUpdated();
      setMessage(zh
        ? "已发出仅限这个公开地点的一次共同规划授权；对方必须用自己的签名身份接受。"
        : "A one-stop co-planning offer was sent. The collaborator must accept it with their own signed identity.");
    } catch {
      setMessage(zh
        ? "授权未发出；行程和权限没有变化。请刷新检查邀请、当前计划和成员状态。"
        : "The offer was not issued; Journey and role permissions are unchanged. Refresh to review the invitation, plan, and member state.");
      await onUpdated();
    } finally {
      setBusyId(null);
    }
  };

  const respondCoPlanning = async (
    grant: SharedStopCoPlanningGrantProjection,
    decision: "ACCEPT" | "DECLINE",
  ) => {
    if (busyId) return;
    setBusyId(grant.grant_id);
    setMessage(null);
    try {
      await cityNexusApi.respondSharedStopCoPlanningGrant(
        journeyId,
        grant.invitation_id,
        grant.grant_id,
        decision,
      );
      await onUpdated();
      setMessage(decision === "ACCEPT"
        ? (zh ? "已接受这个地点的一次共同规划；你的全局角色权限没有改变。" : "You accepted one co-planning round for this Place. Your global role permissions did not change.")
        : (zh ? "已拒绝这次共同规划授权。" : "You declined this co-planning offer."));
    } catch {
      setMessage(zh
        ? "授权邀请已过期或当前计划已变化；没有扩大你的权限。请刷新查看。"
        : "The offer expired or the active plan changed. No broader permission was granted; refresh to review.");
      await onUpdated();
    } finally {
      setBusyId(null);
    }
  };

  const revokeCoPlanning = async (grant: SharedStopCoPlanningGrantProjection) => {
    if (busyId) return;
    setBusyId(grant.grant_id);
    try {
      await cityNexusApi.revokeSharedStopCoPlanningGrant(
        journeyId,
        grant.invitation_id,
        grant.grant_id,
      );
      await onUpdated();
    } catch {
      setMessage(zh ? "授权无法撤回或已不再有效；请刷新查看。" : "The grant could not be revoked or is no longer active; refresh to review.");
      await onUpdated();
    } finally {
      setBusyId(null);
    }
  };

  const answerPromotion = async (
    promotion: MemberPlacePromotionProjection,
    decision: "APPROVE" | "DECLINE",
  ) => {
    if (busyId || !promotion.clarification_id) return;
    setBusyId(promotion.promotion_id);
    setMessage(null);
    try {
      await cityNexusApi.respondToMemberPlacePromotion(
        journeyId,
        promotion.promotion_id,
        promotion.clarification_id,
        decision,
      );
      await onUpdated();
    } catch {
      setMessage(zh
        ? "决策轮可能已经过期或行程事实发生变化；共享计划未被本次操作切换。请刷新查看新状态。"
        : "This decision round may be stale or Journey facts may have changed. This action did not switch the shared plan; refresh to review its status.");
      await onUpdated();
    } finally {
      setBusyId(null);
    }
  };

  const applyPromotion = async (promotion: MemberPlacePromotionProjection) => {
    if (busyId) return;
    setBusyId(promotion.promotion_id);
    setMessage(null);
    try {
      await cityNexusApi.applyMemberPlacePromotion(
        journeyId,
        promotion.promotion_id,
        `r73-apply-${crypto.randomUUID()}`,
      );
      await onUpdated();
      setMessage(zh ? "已通过集体授权应用新的共享 PlanVersion。" : "The new shared PlanVersion was applied after collective authorization.");
    } catch {
      setMessage(zh
        ? "应用前复核发现计划、成员、路线或恢复状态已变化；没有应用过期提案。请刷新并重新检查。"
        : "The plan, membership, route, or recovery state changed before apply. The stale proposal was not applied; refresh and review it.");
      await onUpdated();
    } finally {
      setBusyId(null);
    }
  };

  const renderPromotionOffer = (
    invitationId: string,
    placeName: string,
    rendezvousTitle: string,
    current: boolean,
    canPropose: boolean,
    canUpdate: boolean,
    groupShareStatus: string,
    coPlanningOptions: MemberPlaceInvitationProjection["sent"][number]["co_planning_options"],
  ) => {
    const coPlanningReady = coPlanningOptions.every((item) => item.state === "ACCEPTED");
    const selectable = coPlanningOptions.filter((item) => !["OFFERED", "ACCEPTED"].includes(item.state));
    const selectedMemberId = coPlanningSelectionByInvitation[invitationId] ?? selectable[0]?.member_id ?? "";
    const stateLabel = (state: string) => ({
      NOT_OFFERED: zh ? "尚未邀请" : "not offered",
      OFFERED: zh ? "等待成员接受" : "awaiting acceptance",
      ACCEPTED: zh ? "已接受，可共同决策" : "accepted for this stop",
      DECLINED: zh ? "已拒绝" : "declined",
      REVOKED: zh ? "已撤销" : "revoked",
      EXPIRED: zh ? "已过期" : "expired",
      BOUND: zh ? "已绑定到上一决策轮" : "bound to a prior round",
      STALE: zh ? "已失效" : "stale",
    } as Record<string, string>)[state] ?? state;
    return (
    <section data-testid="member-place-promotion-offer" data-invitation-id={invitationId}>
      <strong>{zh ? "把发现加入共同旅程？" : "Promote this discovery to the shared Journey?"}</strong>
      <small>
        {zh
          ? "这是新的集体计划提议。系统会用服务器掌握的公开地点、路线与承诺证据重新核验；所有当前成员（包括未加入临时小队的成员）都要分别同意。原计划只会在全体同意后由成员明确应用时切换。"
          : "This is a collective plan proposal. The server rechecks the canonical public Place, route, and protected commitments. Every current Journey member, including people outside this micro-group, must approve independently. The plan switches only after unanimous approval and an explicit member apply."}
      </small>
      {canPropose && coPlanningOptions.length > 0 && (
        <section data-testid="member-place-coplanning-manager">
          <strong>{zh ? "仅限这个地点的共同规划授权" : "Co-planning authority for this Place only"}</strong>
          {coPlanningOptions.map((item) => (
            <small key={item.member_id} data-testid="member-place-coplanning-status">
              {item.display_name}: {stateLabel(item.state)}
            </small>
          ))}
          {selectable.length > 0 && (
            <div className="journey-side-quest-actions">
              <label htmlFor={`member-place-coplanning-${invitationId}`}>
                {zh ? "选择同行者" : "Choose a collaborator"}
              </label>
              <select
                id={`member-place-coplanning-${invitationId}`}
                data-testid="member-place-coplanning-member"
                value={selectedMemberId}
                disabled={busyId !== null}
                onChange={(event) => setCoPlanningSelectionByInvitation((values) => ({
                  ...values,
                  [invitationId]: event.target.value,
                }))}
              >
                {selectable.map((item) => (
                  <option key={item.member_id} value={item.member_id}>{item.display_name}</option>
                ))}
              </select>
              <button
                type="button"
                data-testid="member-place-coplanning-offer"
                disabled={!current || busyId !== null || !selectedMemberId}
                onClick={() => void offerCoPlanning(invitationId, selectedMemberId)}
              >
                {zh ? "邀请共同规划这一站" : "Offer co-planning for this stop"}
              </button>
            </div>
          )}
          {!coPlanningReady && (
            <small data-testid="member-place-coplanning-pending">
              {zh ? "每位普通协作者都需先用自己的身份接受；未接受前不创建候选计划。" : "Each read-only collaborator must accept with their own identity before a candidate plan is created."}
            </small>
          )}
        </section>
      )}
      <label htmlFor={`member-place-promotion-dwell-${invitationId}`}>
        {zh ? "计划停留时间" : "Planned dwell time"}
      </label>
      <select
        id={`member-place-promotion-dwell-${invitationId}`}
        data-testid="member-place-promotion-dwell"
        value={promotionDwellById[invitationId] ?? 20}
        disabled={!current || !canPropose || !coPlanningReady || busyId !== null}
        onChange={(event) => setPromotionDwellById((values) => ({
          ...values,
          [invitationId]: Number(event.target.value) as 10 | 20 | 30,
        }))}
      >
        <option value={10}>{zh ? "10 分钟" : "10 minutes"}</option>
        <option value={20}>{zh ? "20 分钟" : "20 minutes"}</option>
        <option value={30}>{zh ? "30 分钟" : "30 minutes"}</option>
      </select>
      <small>{zh ? `地点：${placeName}；下一会合：${rendezvousTitle}。` : `Place: ${placeName}; next rendezvous: ${rendezvousTitle}.`}</small>
      {!canUpdate && (
        <small data-testid="member-place-promotion-role-blocked">
          {zh
            ? "你当前的 Journey 成员角色没有 JOURNEY_UPDATE 权限，因此不能提交共享计划变更提议。请由有权限的成员发起；系统不会借用其他人的身份。"
            : "Your current Journey role lacks JOURNEY_UPDATE authority, so you cannot submit a shared-plan change. A member with this authority must propose it; CityNexus will not act as another member."}
        </small>
      )}
      {canUpdate && !canPropose && (
        <small data-testid="member-place-promotion-share-blocked">
          {zh
            ? `原地点分享者尚未向全组授权，或授权已失效（${groupShareStatus}）；请由分享者确认后再提议。`
            : `The original Place sharer has not authorized full-Journey disclosure, or that consent is stale (${groupShareStatus}). The sharer must confirm before a proposal can be sponsored.`}
        </small>
      )}
      <button
        type="button"
        data-testid="member-place-promotion-create"
        disabled={!current || !canPropose || busyId !== null}
        onClick={() => void createPromotion(invitationId)}
      >
        {busyId === invitationId
          ? (zh ? "正在核验并提议…" : "Checking and proposing…")
          : (!coPlanningReady
            ? (zh ? "等待同行者接受共同规划" : "Waiting for co-planning acceptance")
            : (zh ? "提议加入共同旅程" : "Propose as a shared stop"))}
      </button>
    </section>
    );
  };

  const renderGroupShareControl = (
    invitationId: string,
    placeName: string,
    status: string,
    canManage: boolean,
  ) => {
    const preview = groupSharePreviewById[invitationId];
    const statusText: Record<string, string> = {
      SHARED_TO_GROUP: zh ? "已由你明确分享给当前全组。" : "You explicitly shared this Place with the current Journey audience.",
      SHARED_BY_ORIGINAL_INVITATION: zh ? "创建原邀请时已选择当前全组；不需要重复确认。" : "The original invitation already named every current Journey member.",
      NOT_SHARED_TO_GROUP: zh ? "目前只有原邀请对象能看到这个地点。" : "Only the original invitees can currently see this Place.",
      AUDIENCE_CHANGED: zh ? "成员名单变化已使之前的分享失效。" : "A membership change invalidated the previous sharing snapshot.",
      PLAN_CHANGED: zh ? "共享计划或执行点变化已使之前的分享失效。" : "A plan or execution-cursor change invalidated the previous sharing snapshot.",
      INVITER_SPLIT_STALE: zh ? "独立探索或会合上下文变化已使分享失效。" : "The inviter's split or rendezvous context invalidated the previous share.",
      INVITER_CONSENT_WITHDRAWN: zh ? "你已撤回全组分享；已经看过的人无法被视为从未见过。" : "You withdrew full-Journey sharing; people who already saw the Place cannot be made to unsee it.",
      INVITER_CONSENT_EXPIRED: zh ? "全组分享已过期；需要重新核对并确认。" : "The group-share consent expired and needs a fresh review.",
    };
    return (
      <section data-testid="member-place-group-share" data-state={status}>
        {canManage ? (
          <>
            <strong>{zh ? "把这个发现分享给全体同行者" : "Share this discovery with the whole Journey"}</strong>
            <small>{statusText[status] ?? statusText.NOT_SHARED_TO_GROUP}</small>
            {status === "SHARED_TO_GROUP" ? (
              <button type="button" data-testid="member-place-group-share-withdraw" disabled={busyId !== null} onClick={() => void withdrawGroupShare(invitationId)}>
                {zh ? "撤回后续全组分享" : "Withdraw future group sharing"}
              </button>
            ) : (
              <button type="button" data-testid="member-place-group-share-preview" disabled={busyId !== null} onClick={() => void previewGroupShare(invitationId)}>
                {zh ? "查看分享名单与内容…" : "Review audience and shared details…"}
              </button>
            )}
            {preview && (
              <section data-testid="member-place-group-share-confirmation">
                <strong>{preview.place.name} · {preview.audience.length}{zh ? " 位同行者" : " companions"}</strong>
                <ul>
                  {preview.audience.map((person) => <li key={person.member_id}>{person.display_name}</li>)}
                </ul>
                <small>{preview.disclosure}</small>
                <small>{zh ? `下一会合点：${preview.rendezvous.title}。计划变更还需全体逐人决策。` : `Next rendezvous: ${preview.rendezvous.title}. A plan change still requires each member's separate decision.`}</small>
                <button type="button" data-testid="member-place-group-share-confirm" disabled={busyId !== null} onClick={() => void confirmGroupShare(invitationId, preview)}>
                  {zh ? "确认向以上成员分享地点" : "Confirm sharing this Place with these members"}
                </button>
                <button type="button" disabled={busyId !== null} onClick={() => setGroupSharePreviewById((current) => ({ ...current, [invitationId]: null }))}>
                  {zh ? "取消" : "Cancel"}
                </button>
              </section>
            )}
          </>
        ) : status !== "SHARED_TO_GROUP" && status !== "SHARED_BY_ORIGINAL_INVITATION" ? (
          <small data-testid="member-place-group-share-waiting">{statusText[status] ?? statusText.NOT_SHARED_TO_GROUP}</small>
        ) : null}
      </section>
    );
  };

  const renderPromotion = (promotion: MemberPlacePromotionProjection) => {
    const stateText = promotion.state === "PENDING_ROUTE"
      ? (zh ? "正在检查路线证据" : "Checking route evidence")
      : promotion.state === "WAITING_FOR_MEMBERS"
        ? (zh ? "等待所有当前成员分别决策" : "Waiting for each current member")
        : promotion.state === "AUTHORIZED"
          ? (zh ? "所有必要成员已同意；等待成员明确应用" : "All required members approved; awaiting explicit apply")
          : promotion.state === "APPLIED"
            ? (zh ? "已加入共享行程" : "Added to the shared Journey")
            : promotion.state === "DECLINED"
              ? (zh ? "有成员不同意；共享计划保持原样" : "A member declined; the shared plan stayed unchanged")
              : promotion.state === "STALE"
                ? (zh ? "计划或成员事实已变化；需要重新提议" : "Plan or membership changed; a new proposal is needed")
                : (zh ? "当前证据不足，不能切换共享计划" : "Current evidence is insufficient; the shared plan cannot change");
    return (
      <article key={promotion.promotion_id} data-testid="member-place-promotion" data-state={promotion.state}>
        <span className="v8-kicker">{zh ? "共享站点决策" : "Shared-stop decision"}</span>
        <strong>{promotion.place.name} · {promotion.dwell_minutes}{zh ? " 分钟" : " min"}</strong>
        <small>{zh ? `下一会合点：${promotion.rendezvous.title} · ${promotion.rendezvous.place_name}` : `Next rendezvous: ${promotion.rendezvous.title} · ${promotion.rendezvous.place_name}`}</small>
        <small data-testid="member-place-promotion-state">{stateText}</small>
        {promotion.tradeoff && (
          <section data-testid="member-place-promotion-tradeoff">
            <strong>{zh ? "决定前请核对共享行程变化" : "Review the shared itinerary changes before deciding"}</strong>
            <small>
              {zh
                ? `新增 ${promotion.tradeoff.added_count} 站、移除 ${promotion.tradeoff.removed_count} 站、移动 ${promotion.tradeoff.moved_count} 站。`
                : `${promotion.tradeoff.added_count} added, ${promotion.tradeoff.removed_count} removed, ${promotion.tradeoff.moved_count} moved stop(s).`}
            </small>
            <small>
              {zh
                ? `行程结束时间变化：${promotion.tradeoff.end_time_delta_min === null ? "未知" : `${promotion.tradeoff.end_time_delta_min >= 0 ? "+" : ""}${promotion.tradeoff.end_time_delta_min} 分钟`}；路程时间变化：${promotion.tradeoff.travel_delta_min === null ? "未知" : `${promotion.tradeoff.travel_delta_min >= 0 ? "+" : ""}${promotion.tradeoff.travel_delta_min} 分钟`}。`
                : `Trip end delta: ${promotion.tradeoff.end_time_delta_min === null ? "unknown" : `${promotion.tradeoff.end_time_delta_min >= 0 ? "+" : ""}${promotion.tradeoff.end_time_delta_min} min`}; travel delta: ${promotion.tradeoff.travel_delta_min === null ? "unknown" : `${promotion.tradeoff.travel_delta_min >= 0 ? "+" : ""}${promotion.tradeoff.travel_delta_min} min`}.`}
            </small>
            <small>
              {zh
                ? `预算变化：${promotion.tradeoff.cost_delta_minor === null ? "未知" : `${promotion.tradeoff.cost_delta_minor >= 0 ? "+" : ""}${promotion.tradeoff.cost_delta_minor} ${promotion.tradeoff.currency} 最小货币单位`}。受保护承诺：${promotion.tradeoff.protected_commitments_preserved ? "候选计划中保留" : "未确认保留"}。`
                : `Budget delta: ${promotion.tradeoff.cost_delta_minor === null ? "unknown" : `${promotion.tradeoff.cost_delta_minor >= 0 ? "+" : ""}${promotion.tradeoff.cost_delta_minor} minor units (${promotion.tradeoff.currency})`}. Protected commitments: ${promotion.tradeoff.protected_commitments_preserved ? "retained in candidate" : "not confirmed retained"}.`}
            </small>
            <small>
              {zh
                ? `路线证据来源：${promotion.tradeoff.route_provider}；证据类别：${promotion.tradeoff.route_fact_qualities.join(", ") || "未标注"}。这是候选估计，不是实时到场证明，应用前仍会复核。`
                : `Route evidence: ${promotion.tradeoff.route_provider}; fact classes: ${promotion.tradeoff.route_fact_qualities.join(", ") || "unspecified"}. These are candidate estimates, not physical-arrival proof; the server rechecks before apply.`}
            </small>
          </section>
        )}
        {promotion.state === "BLOCKED" && promotion.state_detail.feasibility === "FEASIBILITY_UNKNOWN" && (
          <small data-testid="member-place-promotion-unknown">
            {zh
              ? `没有足够的服务器路线/时间证据（${promotion.state_detail.code ?? "UNKNOWN"}）；状态为 NEEDS_ROUTE_EVIDENCE，不创建绿色候选，原计划不变。`
              : `The server has no qualifying route/timing evidence (${promotion.state_detail.code ?? "UNKNOWN"}). Status is NEEDS_ROUTE_EVIDENCE; no green candidate was created and the current plan remains unchanged.`}
          </small>
        )}
        {promotion.responses.length > 0 && (
          <ul data-testid="member-place-promotion-responses">
            {promotion.responses.map((response) => (
              <li key={response.member_id}>
                {response.display_name}: {response.decision === "APPROVE"
                  ? (zh ? "已同意" : "approved")
                  : response.decision === "DECLINE"
                    ? (zh ? "不同意" : "declined")
                    : (zh ? "待决策" : "waiting")}
              </li>
            ))}
          </ul>
        )}
        {promotion.viewer_decision && (
          <small>{zh ? `你已${promotion.viewer_decision === "APPROVE" ? "同意" : "拒绝"}；本轮不能替其他成员决策。` : `You ${promotion.viewer_decision.toLowerCase()}; this does not decide for anyone else.`}</small>
        )}
        {promotion.viewer_scoped_authority_expires_at && (
          <small data-testid="member-place-promotion-scoped-authority">
            {zh
              ? `你仅可对这一轮共享站点决策投票；授权于 ${new Date(promotion.viewer_scoped_authority_expires_at).toLocaleString(locale)} 到期。`
              : `Your authority covers only this shared-stop decision and expires ${new Date(promotion.viewer_scoped_authority_expires_at).toLocaleString(locale)}.`}
          </small>
        )}
        {promotion.viewer_can_decide && (
          <div className="journey-side-quest-actions">
            <button type="button" data-testid="member-place-promotion-approve" disabled={busyId !== null} onClick={() => void answerPromotion(promotion, "APPROVE")}>
              {zh ? "我同意这个共享站点" : "I approve this shared stop"}
            </button>
            <button type="button" data-testid="member-place-promotion-decline" disabled={busyId !== null} onClick={() => void answerPromotion(promotion, "DECLINE")}>
              {zh ? "不同意，保留原计划" : "Decline and keep the current plan"}
            </button>
          </div>
        )}
        {promotion.viewer_can_apply && promotion.state === "AUTHORIZED" && (
          <button type="button" data-testid="member-place-promotion-apply" disabled={busyId !== null} onClick={() => void applyPromotion(promotion)}>
            {zh ? "应用已获全体同意的新计划" : "Apply the unanimously approved plan"}
          </button>
        )}
        {promotion.state !== "APPLIED" && promotion.state !== "BLOCKED" && promotion.state !== "DECLINED" && (
          <small>{zh ? "共享计划在成功应用前保持不变；本流程不会预订、下单或付款。" : "The shared plan remains unchanged until apply succeeds. This flow does not book, order, or pay."}</small>
        )}
      </article>
    );
  };

  const requestProgressConsent = (
    invitationId: string,
    placeName: string,
    action: "SHARE_ARRIVAL" | "SHARE_HEADING_BACK",
    participants: MemberPlaceInvitationProgressParticipant[],
    ownProgress: MemberPlaceInvitationOwnProgress,
  ) => {
    const shareAudienceToken = ownProgress.share_audience_token;
    if (!shareAudienceToken) {
      setMessage(zh
        ? "无法取得当前邀请成员名单。请刷新后再查看和确认分享对象。"
        : "The current invitation audience is unavailable. Refresh before reviewing and confirming who will see this report.");
      return;
    }
    setMessage(null);
    setProgressConsent({
      invitationId,
      placeName,
      action,
      shareAudienceToken,
      audienceNames: participants.map((participant) =>
        `${participant.display_name} (${participant.membership === "INVITER" ? (zh ? "发起者" : "inviter") : (zh ? "已加入" : "joined")})`,
      ),
    });
  };

  const renderProgressBoard = (
    invitationId: string,
    placeName: string,
    rendezvousTitle: string,
    participants: MemberPlaceInvitationProgressParticipant[],
    ownProgress: MemberPlaceInvitationOwnProgress,
    title: string,
    parentState?: string,
  ) => (
    <section className="journey-member-place-progress" data-testid="member-place-progress-board">
      <strong>{title} · {placeName}</strong>
      <small>
        {zh
          ? `共同会合点：${rendezvousTitle}。停留时长只是计划估计；本进度板不验证路线或准时情况，时间按未核实处理。`
          : `Shared rendezvous: ${rendezvousTitle}. Visit duration is a plan estimate; this board does not verify route or arrival timing, so timing remains unverified here.`}
      </small>
      <small>{zh ? "仅显示已加入者主动分享的粗粒度状态；所有报告均为成员自述，未经 GPS 验证。" : "Only coarse statuses explicitly shared by joined members appear here. Every status is member-reported and not GPS verified."}</small>
      {parentState && parentState !== "OPEN" && (
        <small>
          {zh
            ? "邀请已过期、撤回或失效，不再接受新加入；已加入成员的独立支线与历史自愿报告保留。"
            : "This invitation is expired, withdrawn, or stale and cannot accept new members; joined members’ own trips and prior voluntary reports remain."}
        </small>
      )}
      <ul>
        {participants.map((participant) => (
          <li key={participant.member_id} data-testid="member-place-progress-participant">
            <strong>
              {participant.display_name}{" "}
              <span>{participant.membership === "INVITER" ? (zh ? "· 发起者" : "· inviter") : (zh ? "· 已加入" : "· joined")}</span>
            </strong>
            {participant.at_invited_place ? (
              <small>
                {zh ? "报告已到达 · " : "Reported here · "}
                {new Date(participant.at_invited_place.reported_at).toLocaleString(locale)}
                {zh ? " · 自述，非 GPS 验证" : " · self-reported, not GPS verified"}
              </small>
            ) : (
              <small>{zh ? "尚无已分享的到达报告" : "No arrival report shared"}</small>
            )}
            {participant.heading_back && (
              <small>
                {zh ? "报告正在返回 · " : "Reported heading back · "}
                {new Date(participant.heading_back.reported_at).toLocaleString(locale)}
                {zh ? " · 自述，非 GPS 验证" : " · self-reported, not GPS verified"}
              </small>
            )}
          </li>
        ))}
      </ul>
      <div className="journey-side-quest-actions">
        {ownProgress.can_share_arrival && !ownProgress.arrival_shared && (
          <button
            type="button"
            data-testid="member-place-progress-share-arrival"
            disabled={busyId !== null}
            onClick={() => requestProgressConsent(invitationId, placeName, "SHARE_ARRIVAL", participants, ownProgress)}
          >
            {zh ? "分享我报告已到达…" : "Share that I reported arriving…"}
          </button>
        )}
        {ownProgress.arrival_shared && (
          <button
            type="button"
            data-testid="member-place-progress-withdraw-arrival"
            disabled={busyId !== null}
            onClick={() => void updateProgress(invitationId, "WITHDRAW_ARRIVAL")}
          >
            {zh ? "停止分享我的到达状态" : "Stop sharing my arrival status"}
          </button>
        )}
        {ownProgress.can_share_heading_back && !ownProgress.heading_back_shared && (
          <button
            type="button"
            data-testid="member-place-progress-share-heading-back"
            disabled={busyId !== null}
            onClick={() => requestProgressConsent(invitationId, placeName, "SHARE_HEADING_BACK", participants, ownProgress)}
          >
            {zh ? "分享我报告正在返回…" : "Share that I reported heading back…"}
          </button>
        )}
        {ownProgress.heading_back_shared && (
          <button
            type="button"
            data-testid="member-place-progress-withdraw-heading-back"
            disabled={busyId !== null}
            onClick={() => void updateProgress(invitationId, "WITHDRAW_HEADING_BACK")}
          >
            {zh ? "停止分享我的返程状态" : "Stop sharing my return status"}
          </button>
        )}
      </div>
    </section>
  );

  const openPlacePin = (invitationId: string) => {
    const invitation = invitations.find((row) => row.invitation_id === invitationId);
    if (!invitation) return;
    const popup = window.open("about:blank", "_blank");
    if (!popup) {
      setMessage(zh ? "浏览器阻止了新窗口；允许此站点打开新窗口后再试。" : "The browser blocked the new tab. Allow popups for this site and try again.");
      return;
    }
    popup.opener = null;
    try {
      popup.location.href = buildOpenStreetMapPinUrl(invitation.place);
      setPinConsentId(null);
    } catch {
      popup.close();
      setMessage(zh ? "这个公开地点暂时没有有效地图坐标。" : "This public Place does not have a usable exact map pin.");
    }
  };

  return (
    <div className="journey-member-place-invitations" data-testid="member-place-invitations">
      {sentInvitations.map((invitation) => (
        <article key={invitation.invitation_id} data-testid="member-place-progress-sent-invitation">
          <span className="v8-kicker">{zh ? "你发起的临时同行" : "Your invited micro-adventure"}</span>
          {renderGroupShareControl(
            invitation.invitation_id,
            invitation.place.name,
            invitation.group_share_status,
            invitation.viewer_can_manage_group_share,
          )}
          {renderProgressBoard(
            invitation.invitation_id,
            invitation.place.name,
            invitation.rendezvous.title,
            invitation.participants,
            invitation.own_progress,
            zh ? "同行进度" : "Companion progress",
            invitation.state,
          )}
          {renderPromotionOffer(
            invitation.invitation_id,
            invitation.place.name,
            invitation.rendezvous.title,
            invitation.state === "OPEN",
            invitation.viewer_can_propose,
            invitation.viewer_has_journey_update,
            invitation.group_share_status,
            invitation.co_planning_options,
          )}
        </article>
      ))}
      {joinedInvitations.map((invitation) => (
        <article key={invitation.invitation_id} data-testid="member-place-invitation-joined">
          <span className="v8-kicker">{zh ? "你已加入的临时同行" : "An invited micro-adventure you joined"}</span>
          <strong>{invitation.inviter_display_name} · {invitation.place.name}</strong>
          {renderGroupShareControl(
            invitation.invitation_id,
            invitation.place.name,
            invitation.group_share_status,
            invitation.viewer_can_manage_group_share,
          )}
          {renderProgressBoard(
            invitation.invitation_id,
            invitation.place.name,
            invitation.rendezvous.title,
            invitation.participants,
            invitation.own_progress,
            zh ? "同行进度" : "Companion progress",
            invitation.state,
          )}
          {renderPromotionOffer(
            invitation.invitation_id,
            invitation.place.name,
            invitation.rendezvous.title,
            invitation.state === "OPEN",
            invitation.viewer_can_propose,
            invitation.viewer_has_journey_update,
            invitation.group_share_status,
            invitation.co_planning_options,
          )}
        </article>
      ))}
      {promotions.length > 0 && (
        <section data-testid="member-place-promotions">
          <span className="v8-kicker">{zh ? "全体同行者共享站点决策" : "Shared-stop decisions for the whole Journey"}</span>
          {promotions.map(renderPromotion)}
        </section>
      )}
      {coPlanningGrants.length > 0 && (
        <section data-testid="member-place-coplanning-grants">
          <span className="v8-kicker">{zh ? "单站共同规划授权" : "One-stop co-planning offers"}</span>
          {coPlanningGrants.map((grant) => (
            <article key={grant.grant_id} data-testid="member-place-coplanning-grant" data-state={grant.state}>
              <strong>{grant.place.name}</strong>
              <small>
                {zh
                  ? `${grant.grantor_name} 邀请 ${grant.grantee_name} 共同规划这一站。授权只绑定此地点、当前计划与一次集体决策，24 小时后过期；不会增加 JOURNEY_UPDATE 或预订权限。`
                  : `${grant.grantor_name} invited ${grant.grantee_name} to co-plan this one stop. The grant binds this Place, the current plan, and one collective decision; it expires after 24 hours and adds no JOURNEY_UPDATE or booking permission.`}
              </small>
              <small>{zh ? `状态：${grant.state}；会合点：${grant.rendezvous.title}。` : `State: ${grant.state}; rendezvous: ${grant.rendezvous.title}.`}</small>
              {grant.viewer_can_respond && (
                <div className="journey-side-quest-actions">
                  <button type="button" data-testid="member-place-coplanning-accept" disabled={busyId !== null} onClick={() => void respondCoPlanning(grant, "ACCEPT")}>
                    {zh ? "接受这次共同规划" : "Accept co-planning for this one stop"}
                  </button>
                  <button type="button" data-testid="member-place-coplanning-decline" disabled={busyId !== null} onClick={() => void respondCoPlanning(grant, "DECLINE")}>
                    {zh ? "拒绝" : "Decline"}
                  </button>
                </div>
              )}
              {grant.viewer_can_revoke && ["OFFERED", "ACCEPTED", "BOUND"].includes(grant.state) && (
                <button type="button" data-testid="member-place-coplanning-revoke" disabled={busyId !== null} onClick={() => void revokeCoPlanning(grant)}>
                  {zh ? "撤回这次授权" : "Revoke this one-stop grant"}
                </button>
              )}
            </article>
          ))}
        </section>
      )}
      {invitations.length > 0 && (
      <div data-testid="member-place-invitations-incoming">
      {invitations.map((invitation) => {
        const canRespond = invitation.can_respond;
        const completing = ["ACCEPTANCE_NEEDS_COMPLETION", "ACCEPTANCE_IN_PROGRESS"].includes(invitation.state);
        const hasFeasibility = Object.prototype.hasOwnProperty.call(feasibilityById, invitation.invitation_id);
        const feasibility = feasibilityById[invitation.invitation_id];
        const canAccept = completing || (hasFeasibility && feasibility?.state !== "BLOCKED");
        return (
          <article key={invitation.invitation_id} data-testid="member-place-invitation" data-state={invitation.state}>
            <span className="v8-kicker">{zh ? "同行者分享的公开地点" : "A companion shared a public place"}</span>
            <strong>{invitation.inviter_display_name} · {invitation.place.name}</strong>
            <small>
              {zh
                ? `公开精确坐标：${invitation.place.latitude.toFixed(5)}, ${invitation.place.longitude.toFixed(5)}（${invitation.place.coordinate_system}）`
                : `Exact public pin: ${invitation.place.latitude.toFixed(5)}, ${invitation.place.longitude.toFixed(5)} (${invitation.place.coordinate_system})`}
            </small>
            <small>
              {zh
                ? `来源：${invitation.place.source}。共同会合点：「${invitation.rendezvous.title}」· ${invitation.rendezvous.place_name}${invitation.rendezvous.scheduled_at ? ` · ${new Date(invitation.rendezvous.scheduled_at).toLocaleString(locale)}` : " · 时间未知"}。`
                : `Source: ${invitation.place.source}. Shared rendezvous: ${invitation.rendezvous.title} at ${invitation.rendezvous.place_name}${invitation.rendezvous.scheduled_at ? ` · ${new Date(invitation.rendezvous.scheduled_at).toLocaleString(locale)}` : " · time unknown"}.`}
            </small>
            <small>
              {zh
                ? "先检查能否赶上共同会合；没有可靠证据时不会显示绿色结论。接受后只创建你的独立支线，共享行程保持不变。"
                : "Check whether the detour fits before accepting. Missing evidence never appears green. Acceptance creates only your own side trip; the shared Journey stays unchanged."}
            </small>
            <small>
              {zh
                ? "路线检查会单独征求同意：获取一次前台位置并记录为本人 Journey 位置观察，用于路线估算。它可能更新其他成员可见的粗粒度团队状态，但不会把精确坐标直接显示给同行者。地图导航仍需另行确认。"
                : "A route check asks separately: it records one foreground location observation on your Journey for route estimation. This may update coarse group-presence status seen by companions, but does not show them your exact coordinates. Map navigation needs separate consent."}
            </small>
            {canRespond && !completing && (
              <button
                type="button"
                data-testid="member-place-invitation-check-feasibility"
                disabled={busyId !== null}
                onClick={() => setRouteConsentId(invitation.invitation_id)}
              >
                {hasFeasibility
                  ? (zh ? "重新检查能否赶上" : "Check feasibility again")
                  : (zh ? "检查能否赶上会合…" : "Check if I can make it…")}
              </button>
            )}
            {hasFeasibility && (
              <section
                data-testid="member-place-invitation-feasibility"
                data-state={feasibility?.state ?? "UNKNOWN"}
              >
                <strong>
                  {feasibility?.state === "CAN_MAKE_IT"
                    ? (zh ? "现有路线证据显示可以赶上" : "Current route evidence fits the rendezvous")
                    : feasibility?.state === "AT_RISK"
                      ? (zh ? "时间缓冲较紧" : "Timing is at risk")
                      : feasibility?.state === "BLOCKED"
                        ? (zh ? "当前安排不允许安全接受" : "Current commitments block a safe acceptance")
                        : (zh ? "时间尚未核实" : "Timing is unverified")}
                </strong>
                {feasibility?.travel_duration_min !== null && feasibility?.travel_duration_min !== undefined && (
                  <small>
                    {zh
                      ? `路线、${feasibility.dwell_minutes} 分钟停留与返程合计约 ${feasibility.travel_duration_min} 分钟；缓冲 ${feasibility.slack_min ?? "未知"} 分钟。`
                      : `Outbound route, ${feasibility.dwell_minutes} minute visit, and return total ${feasibility.travel_duration_min} minutes; ${feasibility.slack_min ?? "unknown"} minutes of slack.`}
                  </small>
                )}
                {feasibility?.evidence.captured_at && (
                  <small>
                    {zh ? "路线证据：" : "Route evidence: "}
                    {feasibility.evidence.source_type ?? (zh ? "来源未知" : "source unknown")}
                    {feasibility.evidence.provider ? ` · ${feasibility.evidence.provider}` : ""}
                    {` · ${new Date(feasibility.evidence.captured_at).toLocaleString(locale)}`}
                    {feasibility.evidence.age_seconds !== null ? ` · ${feasibility.evidence.age_seconds}s` : ""}
                  </small>
                )}
                <small>
                  {feasibility?.state === "UNKNOWN"
                    ? (zh ? "没有新鲜本人位置或可靠路线证据；没有估算到达时间。你可以在明确知情后继续。" : "There is no fresh personal location or qualifying route evidence, so no arrival time is estimated. You may continue only after acknowledging that timing is unverified.")
                    : feasibility?.state === "BLOCKED"
                      ? (zh ? "受保护安排或你现有的支线与此选择冲突；共享计划未改变。" : "A protected commitment or your existing side trip conflicts with this choice; the shared plan is unchanged.")
                      : (zh ? "这是路线建议，不是实际到达证明；预览不会改动共享计划。" : "This is route guidance, not proof of physical arrival. The preview does not change the shared plan.")}
                </small>
              </section>
            )}
            <button
              type="button"
              data-testid="member-place-invitation-view-pin"
              disabled={busyId !== null}
              onClick={() => setPinConsentId(invitation.invitation_id)}
            >
              {zh ? "查看地点地图标记…" : "Review place pin…"}
            </button>
            {canRespond ? (
              <div className="journey-side-quest-actions">
                {canAccept && (
                  <button
                    type="button"
                    data-testid="member-place-invitation-accept"
                    disabled={busyId !== null}
                    onClick={() => void respond(
                      invitation.invitation_id,
                      "ACCEPT",
                      completing
                        ? undefined
                        : feasibility?.state === "CAN_MAKE_IT"
                          ? { token: feasibility.feasibility_token }
                          : feasibility?.state === "AT_RISK"
                            ? { token: feasibility.feasibility_token, acknowledgeRisk: true }
                            : { token: feasibility?.feasibility_token, acknowledgeUnknown: true },
                    )}
                  >
                    {busyId === invitation.invitation_id
                      ? (zh ? "正在准备你的支线…" : "Preparing your side trip…")
                      : completing
                        ? (zh ? "继续完成我的支线" : "Finish setting up my side trip")
                        : feasibility?.state === "AT_RISK"
                          ? (zh ? "我了解时间风险，仍要接受" : "Accept despite the timing risk")
                          : feasibility?.state === "UNKNOWN"
                            ? (zh ? "接受，且知晓时间未核实" : "Accept knowing timing is unverified")
                            : (zh ? "接受并添加到我的私人支线" : "Accept and add to my side trip")}
                  </button>
                )}
                {feasibility?.state === "BLOCKED" && (
                  <small>{zh ? "无法安全接受；请保留现有安排或先解决冲突。" : "Acceptance is blocked. Keep your current plans or resolve the conflict first."}</small>
                )}
                {!completing && (
                  <button
                    type="button"
                    data-testid="member-place-invitation-decline"
                    disabled={busyId !== null}
                    onClick={() => void respond(invitation.invitation_id, "DECLINE")}
                  >
                    {zh ? "谢绝" : "Decline"}
                  </button>
                )}
              </div>
            ) : (
              <small>
                {invitation.state === "EXPIRED"
                  ? (zh ? "邀请已过期。" : "This invitation expired.")
                  : invitation.state === "STALE"
                    ? (zh ? "来源支线或共同会合安排已变化，不能再接受。" : "The source stop or shared rendezvous changed; this invitation can no longer be accepted.")
                    : invitation.state === "WITHDRAWN"
                      ? (zh ? "邀请已撤回。" : "This invitation was withdrawn.")
                      : (zh ? "当前无法响应此邀请。" : "This invitation can no longer be answered.")}
              </small>
            )}
          </article>
        );
      })}
      </div>
      )}
      {routeConsentId && invitations.some((row) => row.invitation_id === routeConsentId) && (
        <section
          role="dialog"
          aria-modal="true"
          aria-labelledby="member-place-invitation-route-title"
          data-testid="member-place-invitation-route-consent"
        >
          <strong id="member-place-invitation-route-title">
            {zh ? "为这次路线检查分享一次当前位置？" : "Share one current location for this route check?"}
          </strong>
          <p>
            {zh
              ? "同意后，CityNexus 会记录一条与你的身份绑定的 Journey 前台位置观察，并把起点、公开地点和会合点交给已配置的路线服务估算。其他成员不会在团队视图中看到你的精确坐标或私有路线，但可能看到更新后的粗粒度团队状态。不会改动共享计划；拒绝定位或缺少路线证据时结果是“时间未核实”。"
              : "If you agree, CityNexus records one identity-bound foreground location observation on your Journey and sends your origin, the public Place, and rendezvous to the configured route service for this estimate. Companions do not see your exact coordinates or private route in the group view, but their coarse group-presence status may update. The shared plan is unchanged. Denied location or missing route evidence means unverified timing."}
          </p>
          <label htmlFor="member-place-invitation-dwell">
            {zh ? "你计划在这里停留多久？" : "How long would you stay?"}
          </label>
          <select
            id="member-place-invitation-dwell"
            data-testid="member-place-invitation-dwell"
            value={dwellById[routeConsentId] ?? 20}
            disabled={busyId !== null}
            onChange={(event) => {
              const minutes = Number(event.target.value) as 10 | 20 | 30;
              setDwellById((current) => ({ ...current, [routeConsentId]: minutes }));
              setFeasibilityById((current) => {
                const next = { ...current };
                delete next[routeConsentId];
                return next;
              });
            }}
          >
            <option value={10}>{zh ? "10 分钟" : "10 minutes"}</option>
            <option value={20}>{zh ? "20 分钟" : "20 minutes"}</option>
            <option value={30}>{zh ? "30 分钟" : "30 minutes"}</option>
          </select>
          <div className="journey-side-quest-actions">
            <button
              type="button"
              data-testid="member-place-invitation-route-confirm"
              disabled={busyId !== null}
              onClick={() => void checkFeasibility(routeConsentId)}
            >
              {zh ? "同意并检查一次" : "Agree and check once"}
            </button>
            <button
              type="button"
              data-testid="member-place-invitation-route-skip"
              onClick={() => {
                setFeasibilityById((current) => ({ ...current, [routeConsentId]: null }));
                setRouteConsentId(null);
              }}
            >
              {zh ? "不分享位置，按未核实继续…" : "Skip location sharing; continue unverified…"}
            </button>
            <button type="button" onClick={() => setRouteConsentId(null)}>
              {zh ? "取消" : "Cancel"}
            </button>
          </div>
        </section>
      )}
      {pinConsentId && invitations.some((row) => row.invitation_id === pinConsentId) && (
        <section
          role="dialog"
          aria-modal="true"
          aria-labelledby="member-place-invitation-pin-title"
          data-testid="member-place-invitation-pin-consent"
        >
          <strong id="member-place-invitation-pin-title">{zh ? "将公开地点坐标发送给 OpenStreetMap？" : "Send the public Place coordinates to OpenStreetMap?"}</strong>
          <p>
            {zh
              ? "只会打开已分享地点的精确地图标记；不会发送你的起点或设备位置。OpenStreetMap 可能按浏览器自己的权限访问位置。此操作不会接受邀请或报告到达。"
              : "Only the shared Place’s exact pin is opened. Your origin and device location are not sent by CityNexus. OpenStreetMap may use location under your browser’s own permissions. This does not accept the invitation or report arrival."}
          </p>
          <div className="journey-side-quest-actions">
            <button
              type="button"
              data-testid="member-place-invitation-pin-confirm"
              onClick={() => openPlacePin(pinConsentId)}
            >
              {zh ? "同意并打开 OpenStreetMap" : "Agree and open OpenStreetMap"}
            </button>
            <button type="button" onClick={() => setPinConsentId(null)}>
              {zh ? "取消" : "Cancel"}
            </button>
          </div>
        </section>
      )}
      {progressConsent && (
        <section
          role="dialog"
          aria-modal="true"
          aria-labelledby="member-place-progress-consent-title"
          data-testid="member-place-progress-consent"
        >
          <strong id="member-place-progress-consent-title">
            {progressConsent.action === "SHARE_ARRIVAL"
              ? (zh ? "要向已加入的同行者分享这条到达自述吗？" : "Share this arrival self-report with joined companions?")
              : (zh ? "要向已加入的同行者分享这条返程自述吗？" : "Share this return self-report with joined companions?")}
          </strong>
          <p>
            {zh
              ? "你将为“" + progressConsent.placeName + "”分享这一粗粒度状态。内容是你自己的报告，不是 GPS 到场验证，也不含坐标或路线。你可以停止后续展示；别人已经看见的信息无法撤回。"
              : "You are sharing a coarse status for “" + progressConsent.placeName + "”. It is your report, not GPS-verified presence, and contains no coordinates or route. You can stop future projections; information already seen cannot be recalled."}
          </p>
          <p data-testid="member-place-progress-consent-audience">
            {zh
              ? "当前进度板成员（包括你）：" + (progressConsent.audienceNames.join("、") || "暂无") + "。若确认前名单变化，本次分享会取消并要求你重新确认。"
              : "Current progress-board members, including you: " + (progressConsent.audienceNames.join(", ") || "none") + ". If the audience changes before confirmation, this share will be cancelled and require a fresh review."}
          </p>
          <div className="journey-side-quest-actions">
            <button
              type="button"
              data-testid="member-place-progress-consent-confirm"
              disabled={busyId !== null}
              onClick={() => {
                const consent = progressConsent;
                void updateProgress(consent.invitationId, consent.action, consent.shareAudienceToken)
                  .then(() => setProgressConsent(null));
              }}
            >
              {zh ? "同意并分享这条自述" : "Agree and share this self-report"}
            </button>
            <button type="button" onClick={() => setProgressConsent(null)}>
              {zh ? "取消" : "Cancel"}
            </button>
          </div>
        </section>
      )}
      {message && <small role="status" data-testid="member-place-invitation-message">{message}</small>}
    </div>
  );
}
