import { useEffect, useState } from "react";
import {
  cityNexusApi,
  type GroupRendezvousRouteResult,
  type JourneyGroupPresenceProjection,
  type MemberSideQuestFitPreview,
  type MemberSideQuestNavigationResult,
  type MemberSideQuestNavigationTarget,
  type MemberPlaceInvitationPreview,
  type MemberPlaceInvitationProjection,
  type MemberSideQuestProjection,
  type PlaceRecord,
} from "../../api/citynexus";
import { useLocale } from "../../i18n/LocaleContext";
import {
  buildGoogleWalkingDirectionsUrl,
  buildOpenStreetMapPinUrl,
} from "./memberSideQuestNavigationLinks";
import { MemberMeetupHandshakePanel } from "./MemberMeetupHandshakePanel";

type Rendezvous = {
  title: string;
  place_name: string;
  scheduled_at?: string | null;
};

type NavigationConsent = {
  key: string;
  target: MemberSideQuestNavigationTarget;
};

function routeSketch(geometry: number[][]): string | null {
  const points = geometry.filter((point) =>
    Array.isArray(point) && point.length >= 2
      && Number.isFinite(point[0]) && Number.isFinite(point[1]),
  );
  if (points.length < 2) return null;
  const xs = points.map((point) => point[0]);
  const ys = points.map((point) => point[1]);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const width = Math.max(maxX - minX, Number.EPSILON);
  const height = Math.max(maxY - minY, Number.EPSILON);
  return points.map((point, index) => {
    const x = 8 + ((point[0] - minX) / width) * 304;
    const y = 8 + ((maxY - point[1]) / height) * 64;
    return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
}

export function MemberSideQuestCard({
  journeyId,
  sideQuest,
  rendezvous,
  meetupHandshake,
  placeInvitations,
  currentSplitActive = true,
  onRefreshLocation,
  onHeadBack,
  onUpdated,
}: {
  journeyId: string;
  sideQuest: MemberSideQuestProjection | null;
  rendezvous: Rendezvous | null;
  meetupHandshake?: JourneyGroupPresenceProjection["meetup_handshake"];
  placeInvitations?: MemberPlaceInvitationProjection | null;
  currentSplitActive?: boolean;
  onRefreshLocation?: () => Promise<string | null>;
  onHeadBack?: () => Promise<GroupRendezvousRouteResult | null>;
  onUpdated: () => Promise<void>;
}) {
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const [places, setPlaces] = useState<PlaceRecord[]>([]);
  const [placesReady, setPlacesReady] = useState(false);
  const [query, setQuery] = useState("");
  const [selectedPlaceId, setSelectedPlaceId] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [dwellMinutes, setDwellMinutes] = useState<10 | 20 | 30>(20);
  // Keep one-time navigation advice bound to the exact private quest state.
  // A changed destination, dwell time, shared meetup, or quest transition
  // must never resurrect a route/leave-by captured for an earlier reality.
  const [fitPreviewReceipt, setFitPreviewReceipt] = useState<{ key: string; value: MemberSideQuestFitPreview } | null>(null);
  const [privateRouteReceipt, setPrivateRouteReceipt] = useState<{ key: string; value: MemberSideQuestNavigationResult } | null>(null);
  const [navigationConsent, setNavigationConsent] = useState<NavigationConsent | null>(null);
  const [navigationBusy, setNavigationBusy] = useState(false);
  const [invitationPreview, setInvitationPreview] = useState<MemberPlaceInvitationPreview | null>(null);
  const [invitationBusy, setInvitationBusy] = useState(false);
  const [invitationMessage, setInvitationMessage] = useState<string | null>(null);

  // One-time live routing guidance must not stay on screen indefinitely.
  // These timers only expire a displayed snapshot; they do not query the
  // device's location, poll a provider, or change Journey execution state.
  useEffect(() => {
    if (!fitPreviewReceipt) return;
    const timeout = window.setTimeout(() => setFitPreviewReceipt(null), 30_000);
    return () => window.clearTimeout(timeout);
  }, [fitPreviewReceipt]);
  useEffect(() => {
    if (!privateRouteReceipt) return;
    const timeout = window.setTimeout(() => setPrivateRouteReceipt(null), 30_000);
    return () => window.clearTimeout(timeout);
  }, [privateRouteReceipt]);

  useEffect(() => {
    if (!currentSplitActive) {
      setPlacesReady(true);
      return;
    }
    let cancelled = false;
    void cityNexusApi.getJourney(journeyId)
      .then((journey) => cityNexusApi.listPlaces(journey.city_code))
      .then((rows) => {
        if (!cancelled) {
          setPlaces(rows);
          setPlacesReady(true);
        }
      })
      .catch(() => {
        if (!cancelled) setPlacesReady(true);
      });
    return () => { cancelled = true; };
  }, [currentSplitActive, journeyId]);

  const visiblePlaces = places
    .filter((place) => !query.trim() || place.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
    .slice(0, 100);
  const state = sideQuest?.state ?? null;
  const selectedPlace = places.find((place) => place.id === selectedPlaceId) ?? null;
  const displayedRendezvous = sideQuest?.rendezvous ?? rendezvous;
  const scheduledAt = sideQuest?.rendezvous.scheduled_at ?? rendezvous?.scheduled_at ?? null;
  const scheduledLabel = scheduledAt && Number.isFinite(Date.parse(scheduledAt))
    ? new Date(scheduledAt).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })
    : null;
  const canChooseDestination = currentSplitActive && (!sideQuest
    || state === "DESTINATION_UNRESOLVED"
    || ((state === "COMPLETED" || state === "CANCELLED" || state === "READY") && editing));
  const canNavigate = currentSplitActive
    && (state === "READY" || state === "EN_ROUTE")
    && sideQuest?.destination.authority === "EXACT_KNOWN_PLACE";
  const routeContextKey = JSON.stringify([
    journeyId,
    sideQuest?.id ?? null,
    state,
    sideQuest?.destination.place_id ?? null,
    sideQuest?.destination.title ?? null,
    sideQuest?.rendezvous.activity_id ?? null,
    sideQuest?.rendezvous.scheduled_at ?? null,
    currentSplitActive,
  ]);
  const previewContextKey = JSON.stringify([routeContextKey, dwellMinutes]);
  const fitPreview = fitPreviewReceipt?.key === previewContextKey ? fitPreviewReceipt.value : null;
  const privateRoute = privateRouteReceipt?.key === routeContextKey ? privateRouteReceipt.value : null;
  const navigationKind = state === "READY" || state === "EN_ROUTE"
    ? "PRIVATE_STOP"
    : state === "COMPLETED" || state === "CANCELLED"
      ? "SHARED_RENDEZVOUS"
      : null;
  const canSharePlace = Boolean(
    currentSplitActive
    && sideQuest
    && ["READY", "EN_ROUTE", "ARRIVED"].includes(sideQuest.state)
    && sideQuest.destination.authority === "EXACT_KNOWN_PLACE"
    && sideQuest.destination.place_id,
  );
  const validInvitationPreview = invitationPreview?.side_quest_id === sideQuest?.id
    && invitationPreview.place.place_id === sideQuest?.destination.place_id
    ? invitationPreview
    : null;
  const currentSentInvitations = (placeInvitations?.sent ?? [])
    .filter((invitation) => invitation.side_quest_id === sideQuest?.id);

  useEffect(() => {
    setNavigationConsent((current) => current?.key === routeContextKey ? current : null);
  }, [routeContextKey]);

  const prepareExternalNavigation = async () => {
    if (!sideQuest || !navigationKind || !currentSplitActive || navigationBusy || busy) return;
    setNavigationBusy(true);
    setMessage(null);
    setNavigationConsent(null);
    try {
      const { target } = await cityNexusApi.getMemberSideQuestNavigationTarget(journeyId, sideQuest.id);
      if (target.kind !== navigationKind) {
        throw new Error("The current navigation target changed.");
      }
      if (!target.navigation_available) {
        setMessage(zh
          ? "这个地点没有可用的 WGS84 精确坐标，无法安全打开地图；你仍可按支线手动前往。"
          : "Exact WGS84 coordinates are unavailable, so maps cannot open safely. You can still follow the stop manually.");
        return;
      }
      setNavigationConsent({ key: routeContextKey, target });
    } catch {
      setMessage(zh
        ? "导航目标已变化或暂时不可用；我已刷新当前支线状态。"
        : "The navigation target changed or is unavailable. Current side-quest state was refreshed.");
      await onUpdated();
    } finally {
      setNavigationBusy(false);
    }
  };

  const launchExternalNavigation = async (provider: "GOOGLE" | "OPENSTREETMAP") => {
    const consent = navigationConsent;
    if (
      !consent || consent.key !== routeContextKey || !sideQuest
      || !navigationKind || !currentSplitActive || navigationBusy
    ) return;
    const popup = window.open("about:blank", "_blank");
    if (!popup) {
      setMessage(zh
        ? "浏览器阻止了新窗口；允许此站点打开新窗口后再试。"
        : "The browser blocked the new tab. Allow popups for this site and try again.");
      return;
    }
    popup.opener = null;
    setNavigationBusy(true);
    setMessage(null);
    try {
      // Revalidate at the user's final consent click. The external URL is only
      // assigned if the signed owner still has the same exact current target.
      const { target } = await cityNexusApi.getMemberSideQuestNavigationTarget(journeyId, sideQuest.id);
      const stillSameTarget = target.navigation_available
        && target.kind === consent.target.kind
        && target.place_id === consent.target.place_id
        && target.name === consent.target.name
        && target.latitude === consent.target.latitude
        && target.longitude === consent.target.longitude
        && target.coordinate_system === consent.target.coordinate_system;
      if (!stillSameTarget || target.kind !== navigationKind) {
        throw new Error("The navigation target changed after consent was shown.");
      }
      popup.location.href = provider === "GOOGLE"
        ? buildGoogleWalkingDirectionsUrl(target)
        : buildOpenStreetMapPinUrl(target);
      setNavigationConsent(null);
      setMessage(zh
        ? "已打开外部地图。路线与路况由地图服务提供；这不会报告到达、返程或已与大家会合。"
        : "External maps opened. Its route and traffic are separate; this does not report arrival, return, or reunion.");
    } catch {
      popup.close();
      setNavigationConsent(null);
      setMessage(zh
        ? "共同计划或导航目标刚刚变化；没有打开旧地点，请刷新后重新确认。"
        : "The plan or target just changed. No stale destination was opened; refresh and review it again.");
      await onUpdated();
    } finally {
      setNavigationBusy(false);
    }
  };

  const previewPlaceSharing = async () => {
    if (!sideQuest || !canSharePlace || invitationBusy) return;
    setInvitationBusy(true);
    setInvitationMessage(null);
    try {
      const preview = await cityNexusApi.getMemberPlaceInvitationPreview(journeyId, sideQuest.id);
      setInvitationPreview(preview);
    } catch {
      setInvitationMessage(zh
        ? "地点或共同会合安排刚刚变化；刷新后再检查可分享内容。"
        : "The Place or shared rendezvous changed. Refresh and review what can be shared.");
      await onUpdated();
    } finally {
      setInvitationBusy(false);
    }
  };

  const confirmPlaceSharing = async () => {
    if (!sideQuest || !validInvitationPreview || invitationBusy) return;
    setInvitationBusy(true);
    setInvitationMessage(null);
    try {
      await cityNexusApi.createMemberPlaceInvitation(
        journeyId,
        sideQuest.id,
        validInvitationPreview.audience_snapshot,
      );
      setInvitationPreview(null);
      setInvitationMessage(zh
        ? `已向当前 ${validInvitationPreview.recipient_count} 位同行者分享公开地点；邀请 15 分钟后失效。`
        : `The public Place was shared with ${validInvitationPreview.recipient_count} current companions. The invitation expires in 15 minutes.`);
      await onUpdated();
    } catch {
      setInvitationMessage(zh
        ? "无法发送邀请；来源支线或同行者名单可能已变化。刷新后可重新确认。"
        : "The invitation could not be sent; the source stop or companion list may have changed. Refresh and review again.");
      await onUpdated();
    } finally {
      setInvitationBusy(false);
    }
  };

  const withdrawPlaceInvitation = async (invitationId: string) => {
    if (invitationBusy) return;
    setInvitationBusy(true);
    setInvitationMessage(null);
    try {
      await cityNexusApi.withdrawMemberPlaceInvitation(journeyId, invitationId);
      await onUpdated();
    } catch {
      setInvitationMessage(zh ? "邀请状态刚刚变化；刷新后查看最新状态。" : "The invitation changed state. Refresh to see its current status.");
      await onUpdated();
    } finally {
      setInvitationBusy(false);
    }
  };

  if (!displayedRendezvous) return null;

  const saveDestination = async () => {
    if (busy || (!selectedPlaceId && !query.trim())) return;
    setBusy(true);
    setMessage(null);
    try {
      const updated = await cityNexusApi.createMemberSideQuest(
        journeyId,
        selectedPlaceId ? { placeId: selectedPlaceId } : { destinationText: query.trim() },
      );
      setEditing(false);
      setQuery("");
      setSelectedPlaceId("");
      setMessage(updated.member_side_quest?.state === "DESTINATION_UNRESOLVED"
        ? (zh
          ? "已私下保存这个地点描述；请选择一个精确的已知地点后才能开始导航。"
          : "That description is saved privately. Choose an exact known place before starting the stop.")
        : (zh ? "私人支线已准备好；共享行程没有改变。" : "Your private stop is ready. The shared Journey is unchanged."));
      await onUpdated();
    } catch {
      setMessage(zh
        ? "无法保存这条私人支线；请刷新后重试。共享行程没有改变。"
        : "The private stop could not be saved. Refresh and try again; the shared Journey is unchanged.");
    } finally {
      setBusy(false);
    }
  };

  const advance = async (action: "start" | "arrive" | "complete" | "cancel") => {
    if (!sideQuest || busy) return;
    setBusy(true);
    setMessage(null);
    try {
      await cityNexusApi.advanceMemberSideQuest(journeyId, sideQuest.id, action);
      await onUpdated();
      setMessage(action === "complete" || action === "cancel"
        ? (zh ? "私人停留已结束；请返回共同会合点。共享行程没有改变。" : "The personal stop has ended. Return to the shared rendezvous; the shared Journey is unchanged.")
        : null);
    } catch {
      setMessage(zh
        ? "这项支线操作已失效或暂时无法保存；我刷新了当前状态。"
        : "That side-quest action is stale or could not be saved. Current state was refreshed.");
      await onUpdated();
    } finally {
      setBusy(false);
    }
  };

  const requestFitPreview = async () => {
    if (!sideQuest || busy || !currentSplitActive) return;
    setBusy(true);
    setMessage(null);
    setFitPreviewReceipt(null);
    setPrivateRouteReceipt(null);
    try {
      const observationId = await onRefreshLocation?.() ?? null;
      const result = await cityNexusApi.previewMemberSideQuestFit(
        journeyId,
        sideQuest.id,
        observationId,
        dwellMinutes,
      );
      setFitPreviewReceipt({ key: previewContextKey, value: result });
    } catch {
      setMessage(zh
        ? "暂时无法确认这条支线是否赶得上；共享行程没有改变。你仍可按原计划手动执行，或直接返回共同会合点。"
        : "We can't confirm whether this stop fits right now. The shared Journey is unchanged; you can continue manually or head back to the rendezvous.");
    } finally {
      setBusy(false);
    }
  };

  const navigateToPrivateStop = async () => {
    if (!sideQuest || busy || !onRefreshLocation) {
      setMessage(zh ? "请在你允许后获取这一次的当前位置。" : "Allow one fresh location sample to route to your private stop.");
      return;
    }
    setBusy(true);
    setMessage(null);
    setPrivateRouteReceipt(null);
    try {
      const observationId = await onRefreshLocation();
      if (!observationId) {
        setMessage(zh
          ? "没有取得这一次的位置；私人导航不可用，支线仍可手动执行。"
          : "No one-time location was available. Private navigation is unavailable; you can still follow the stop manually.");
        return;
      }
      const result = await cityNexusApi.navigateMemberSideQuest(
        journeyId,
        sideQuest.id,
        observationId,
      );
      setPrivateRouteReceipt({ key: routeContextKey, value: result });
      setMessage(result.status === "COMPLETED"
        ? (zh ? "已取得只对你显示的私人路线；共享行程没有改变。" : "A private route is ready for you; the shared Journey is unchanged.")
        : (zh ? "路线服务没有可靠结果；你仍可手动前往，计划没有改变。" : "No reliable route is available. You can still go manually; the plan is unchanged."));
    } catch {
      setMessage(zh
        ? "私人路线暂时不可用；请刷新状态或按原计划手动前往。"
        : "The private route is unavailable right now. Refresh or continue manually.");
    } finally {
      setBusy(false);
    }
  };

  const startReturnToGroup = async () => {
    if (!sideQuest || busy) return;
    setBusy(true);
    setMessage(null);
    try {
      const updated = await cityNexusApi.startMemberSideQuestReturn(journeyId, sideQuest.id);
      await onUpdated();
      const reportSaved = updated.member_side_quest?.return_report?.status === "RETURN_STARTED_REPORTED";
      if (!reportSaved) {
        setMessage(zh ? "返程报告已提交；正在刷新当前状态。" : "Your return report was submitted. Refreshing the current state.");
      }
      let route: GroupRendezvousRouteResult | null = null;
      try {
        route = await onHeadBack?.() ?? null;
      } catch {
        route = null;
      }
      setMessage(route?.status === "COMPLETED" && route.route.duration_min != null
        ? (zh
          ? `已记录你本人开始返程的报告；到「${route.target.place_name}」约 ${route.route.duration_min} 分钟（${route.route.provenance ?? route.route.provider ?? "来源已记录"}）。这不是定位证明。`
          : `Your return report is saved; about ${route.route.duration_min} min to ${route.target.place_name} (${route.route.provenance ?? route.route.provider ?? "source recorded"}). This does not verify your location.`)
        : (zh
          ? `已记录你本人开始返程的报告；目前没有可靠路线或定位。共同会合点仍是「${displayedRendezvous?.place_name ?? "当前会合点"}」，这不是定位证明。`
          : `Your return report is saved, but no reliable location or route is available. The shared rendezvous is ${displayedRendezvous?.place_name ?? "unchanged"}; this does not verify your location.`));
      await onUpdated();
    } catch {
      setMessage(zh
        ? "无法保存返程报告；未请求位置或路线。请刷新后重试。"
        : "The return report could not be saved. No location or route was requested; refresh and retry.");
      await onUpdated();
    } finally {
      setBusy(false);
    }
  };

  const returnCopy = (returnState: string) => {
    if (returnState === "ON_TRACK") return zh ? "返程状态：当前路线可按时到达会合点。" : "Return status: the current route reaches the rendezvous in time.";
    if (returnState === "LOW_BUFFER") return zh ? "返程缓冲较少，建议尽快返回。" : "Return status: the safety buffer is low; head back soon.";
    if (returnState === "RETURN_RISK") return zh ? "可信实时路线显示共同会合可能受影响。" : "Trusted live route evidence shows the shared rendezvous may be at risk.";
    if (returnState.startsWith("ADVISORY")) return zh ? "返程判断仅供参考，不会据此修改共享行程。" : "The return estimate is advisory and cannot change the shared Journey.";
    return zh ? "返程状态未知：目前没有可靠的返程路线证据。" : "Return status unknown: no reliable route evidence is available yet.";
  };

  return (
    <div className="journey-side-quest" data-testid="member-side-quest-card" data-state={state ?? "NOT_CREATED"}>
      <span className="v8-kicker">{zh ? "你的私人支线" : "Your private side quest"}</span>
      <small data-testid="member-side-quest-rendezvous">
        {zh ? `共同会合点：${displayedRendezvous.place_name}${scheduledLabel ? ` · ${scheduledLabel}` : ""}` : `Shared rendezvous: ${displayedRendezvous.place_name}${scheduledLabel ? ` · ${scheduledLabel}` : ""}`}
      </small>
      <small>{zh ? "私人停留不会改变共享行程计划。" : "This private stop does not change the shared Journey."}</small>

      {sideQuest && (
        <div data-testid="member-side-quest-owner-state">
          <strong>{sideQuest.destination.title}</strong>
          <small>
            {zh ? `状态：${state}` : `State: ${state}`}
            {sideQuest.destination.authority === "DESCRIPTIVE_ONLY"
              ? (zh ? " · 地点尚未精确匹配" : " · destination is not an exact known Place yet")
              : ""}
          </small>
          {(state === "COMPLETED" || state === "CANCELLED") && (
            <small data-testid="member-side-quest-return-emphasis">
              {zh ? `私人停留已${state === "COMPLETED" ? "完成" : "取消"}；现在返回「${sideQuest.rendezvous.place_name}」。` : `Your personal stop is ${state.toLowerCase()}; return to ${sideQuest.rendezvous.place_name}.`}
            </small>
          )}
          {state === "ARRIVED" && (
            <small data-testid="member-side-quest-arrival-boundary">
              {zh ? "这是你本人报告已到达，不是设备定位证明。" : "This is your report of arrival, not device-verified physical presence."}
            </small>
          )}
          <small data-testid="member-side-quest-return-state">
            {sideQuest.return_report?.status === "RETURN_STARTED_REPORTED"
              ? (zh ? "你已报告开始返程；这不是定位或实际会合证明。" : "You reported heading back; this does not verify your location or reunion.")
              : returnCopy(sideQuest.return_state)}
          </small>
          {state === "INVALIDATED" && (
            <small>{zh ? "共同会合安排已变化；这条支线保留为历史，不能继续执行。" : "The shared rendezvous changed. This side quest remains as history and can no longer be executed."}</small>
          )}
        </div>
      )}

      {canChooseDestination && (
        <div className="journey-side-quest-form">
          <label htmlFor="member-side-quest-search">
            {state === "READY"
              ? (zh ? "更换尚未开始的私人地点" : "Change the private stop before starting")
              : (zh ? "选择私人停留地点" : "Choose a private stop")}
          </label>
          {selectedPlace && (
            <small data-testid="member-side-quest-confirmation">
              {zh
                ? `确认：先去「${selectedPlace.name}」，再于「${displayedRendezvous.place_name}」和大家会合。`
                : `Go to ${selectedPlace.name} before rejoining everyone at ${displayedRendezvous.place_name}.`}
            </small>
          )}
          {!selectedPlaceId && query.trim() && (
            <small data-testid="member-side-quest-descriptive-boundary">
              {zh ? "这是地点描述，尚未匹配到精确地点；保存后仍需选中已知地点才能开始。" : "This is only a place description; select an exact known Place before the stop can start."}
            </small>
          )}
          <input
            id="member-side-quest-search"
            data-testid="member-side-quest-search"
            type="search"
            value={query}
            onChange={(event) => { setQuery(event.target.value); setSelectedPlaceId(""); }}
            placeholder={zh ? "搜索地点名称" : "Search place names"}
          />
          <select
            aria-label={zh ? "已知地点" : "Known place"}
            data-testid="member-side-quest-place"
            value={selectedPlaceId}
            onChange={(event) => setSelectedPlaceId(event.target.value)}
          >
            <option value="">{placesReady ? (zh ? "选择一个已知地点，或仅保存描述" : "Choose a known place, or save a description") : (zh ? "正在加载地点…" : "Loading places…")}</option>
            {visiblePlaces.map((place) => <option key={place.id} value={place.id}>{place.name}</option>)}
          </select>
          <button
            type="button"
            data-testid="member-side-quest-save"
            disabled={busy || (!selectedPlaceId && !query.trim())}
            onClick={() => void saveDestination()}
          >
            {busy
              ? (zh ? "正在保存…" : "Saving…")
              : selectedPlaceId
                ? (zh ? "确认私人支线" : "Confirm private stop")
                : sideQuest
                  ? (zh ? "保存地点描述" : "Save place description")
                  : (zh ? "保存地点描述" : "Save place description")}
          </button>
          {state === "READY" && (
            <button type="button" disabled={busy} onClick={() => setEditing(false)}>
              {zh ? "返回支线" : "Back to side quest"}
            </button>
          )}
        </div>
      )}

      {currentSplitActive && state === "READY" && !editing && (
        <div className="journey-side-quest-actions">
          <button type="button" data-testid="member-side-quest-start" disabled={busy} onClick={() => void advance("start")}>
            {zh ? "开始前往" : "Start stop"}
          </button>
          <button type="button" disabled={busy} onClick={() => setEditing(true)}>
            {zh ? "更换地点" : "Change destination"}
          </button>
          <button type="button" data-testid="member-side-quest-cancel" disabled={busy} onClick={() => void advance("cancel")}>
            {zh ? "取消支线" : "Cancel stop"}
          </button>
        </div>
      )}
      {canNavigate && !editing && (
        <div className="journey-side-quest-actions">
          <label>
            {zh ? "预计私人停留" : "Planned time at your stop"}
            <select
              data-testid="member-side-quest-dwell"
              value={dwellMinutes}
              disabled={busy}
              onChange={(event) => setDwellMinutes(Number(event.target.value) as 10 | 20 | 30)}
            >
              <option value={10}>{zh ? "10 分钟" : "10 minutes"}</option>
              <option value={20}>{zh ? "20 分钟" : "20 minutes"}</option>
              <option value={30}>{zh ? "30 分钟" : "30 minutes"}</option>
            </select>
          </label>
          <button type="button" data-testid="member-side-quest-fit-preview" disabled={busy} onClick={() => void requestFitPreview()}>
            {busy ? (zh ? "正在检查…" : "Checking…") : (zh ? "这条支线赶得上吗？" : "Will this detour fit?")}
          </button>
          <button type="button" data-testid="member-side-quest-navigate" disabled={busy} onClick={() => void navigateToPrivateStop()}>
            {busy ? (zh ? "正在获取路线…" : "Getting route…") : (zh ? "带我去私人地点" : "Take me to my private stop")}
          </button>
          <button
            type="button"
            data-testid="member-side-quest-open-walking-maps"
            disabled={busy || navigationBusy}
            onClick={() => void prepareExternalNavigation()}
          >
            {navigationBusy
              ? (zh ? "正在核对地点…" : "Checking destination…")
              : (zh ? "打开步行导航" : "Open walking directions")}
          </button>
          <small>{zh ? "路线与最晚离开时间是单次快照，会很快过期；路况变化后请重新检查。" : "Routes and leave-by times are one-time snapshots that expire shortly. Check again when conditions change."}</small>
          <small>{zh ? "外部地图使用自己的路线和定位权限；CityNexus 不会为此发送你的起点。" : "External maps use their own routing and location permissions. CityNexus does not send your origin for this handoff."}</small>
        </div>
      )}
      {fitPreview && (
        <div data-testid="member-side-quest-fit-result" data-state={fitPreview.state} aria-live="polite">
          <strong>
            {fitPreview.state === "FITS_WITH_BUFFER"
              ? (zh ? "按当前路线和约定缓冲，预计能赶上。" : "The current routes fit with the protected buffer.")
              : fitPreview.state === "TIGHT_RETURN"
                ? fitPreview.authority === "ADVISORY_PROTECTED_COMMITMENT"
                  ? (fitPreview.safety_buffer_minutes
                    ? (zh ? "路线估算仍赶得上受保护会合点，但没有达到规定的安全缓冲。" : "The route estimate reaches the protected rendezvous but leaves less than its required safety buffer.")
                    : (zh ? "路线估算赶得上受保护会合点；该承诺没有额外安全缓冲，因此仍需谨慎。" : "The route estimate reaches the protected rendezvous, but no additional safety buffer is configured."))
                  : (zh ? "按当前路线估算能赶上计划会合时间，但没有受保护的安全缓冲；这只是参考。" : "Current route estimates fit before the planned rendezvous, but there is no protected safety buffer; this is advisory.")
                : fitPreview.state === "DOES_NOT_FIT"
                  ? (zh ? "按当前路线估算，这段私人停留赶不上会合时间。" : "The current route estimate does not fit before the rendezvous.")
                  : (zh ? "无法确认这条私人支线是否赶得上；共享行程保持不变。" : "We can't confirm whether this private stop fits. The shared Journey is unchanged.")}
          </strong>
          {fitPreview.recommended_leave_by && (
            <small data-testid="member-side-quest-leave-by">
              {zh ? "建议最晚离开私人地点：" : "Recommended leave-by from the private stop: "}
              {new Date(fitPreview.recommended_leave_by).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })}
              {fitPreview.safety_buffer_minutes == null
                ? (zh ? "（参考估算，无受保护缓冲）" : " (advisory; no protected buffer)")
                : (zh ? `（含 ${fitPreview.safety_buffer_minutes} 分钟缓冲）` : ` (${fitPreview.safety_buffer_minutes} min buffer)`)}
            </small>
          )}
          <small>
            {zh ? "去程" : "Outbound"}: {fitPreview.outbound_eta_minutes == null ? (zh ? "未知" : "unknown") : `${fitPreview.outbound_eta_minutes} min`}
            {fitPreview.provenance.outbound ? ` · ${fitPreview.provenance.outbound.provider} / ${fitPreview.provenance.outbound.provenance}` : ""}
            {" · "}{zh ? "返程" : "Return"}: {fitPreview.return_eta_minutes == null ? (zh ? "未知" : "unknown") : `${fitPreview.return_eta_minutes} min`}
            {fitPreview.provenance.return ? ` · ${fitPreview.provenance.return.provider} / ${fitPreview.provenance.return.provenance}` : ""}
          </small>
        </div>
      )}
      {privateRoute && (
        <div data-testid="member-side-quest-private-route" data-state={privateRoute.status}>
          {privateRoute.route && (
            <>
              <small>
                {zh ? `到私人地点约 ${privateRoute.route.duration_min} 分钟` : `About ${privateRoute.route.duration_min} min to your private stop`}
                {privateRoute.route.distance_m != null ? ` · ${Math.round(privateRoute.route.distance_m)} m` : ""}
                {` · ${privateRoute.route.provider} / ${privateRoute.route.provenance}`}
              </small>
              {routeSketch(privateRoute.route.geometry) && (
                <svg viewBox="0 0 320 80" role="img" aria-label={zh ? "本人可见的私人路线示意" : "Private route sketch visible only to you"}>
                  <path d={routeSketch(privateRoute.route.geometry) ?? undefined} fill="none" stroke="currentColor" strokeWidth="3" />
                </svg>
              )}
            </>
          )}
          {!privateRoute.route && (
            <small>{zh ? "没有可靠路线；私人支线仍可手动执行。" : "No reliable route is available; you can still follow the stop manually."}</small>
          )}
        </div>
      )}
      {currentSplitActive && state === "EN_ROUTE" && (
        <div className="journey-side-quest-actions">
          <button type="button" data-testid="member-side-quest-arrive" disabled={busy} onClick={() => void advance("arrive")}>
            {zh ? "我已到达" : "I arrived"}
          </button>
          <button type="button" data-testid="member-side-quest-cancel" disabled={busy} onClick={() => void advance("cancel")}>
            {zh ? "取消支线" : "Cancel stop"}
          </button>
        </div>
      )}
      {currentSplitActive && state === "ARRIVED" && (
        <div className="journey-side-quest-actions">
          <button type="button" data-testid="member-side-quest-complete" disabled={busy} onClick={() => void advance("complete")}>
            {zh ? "完成私人停留" : "Done with stop"}
          </button>
          <button type="button" data-testid="member-side-quest-cancel" disabled={busy} onClick={() => void advance("cancel")}>
            {zh ? "取消支线" : "Cancel stop"}
          </button>
        </div>
      )}
      {currentSplitActive && (state === "COMPLETED" || state === "CANCELLED") && !editing && (
        <div className="journey-side-quest-actions">
          <button
            type="button"
            data-testid="member-side-quest-map-back"
            disabled={busy || navigationBusy}
            onClick={() => void prepareExternalNavigation()}
          >
            {navigationBusy
              ? (zh ? "正在核对会合点…" : "Checking rendezvous…")
              : (zh ? "导航回共同会合点" : "Navigate to everyone")}
          </button>
          <button type="button" data-testid="member-side-quest-head-back" disabled={busy} onClick={() => void startReturnToGroup()}>
            {busy
              ? (zh ? "正在记录返程…" : "Saving your return…")
              : sideQuest.return_report?.status === "RETURN_STARTED_REPORTED"
                ? (zh ? "重新获取会合路线" : "Get an updated route to the group")
                : (zh ? "开始返程，带我去共同会合点" : "Head back to the shared rendezvous")}
          </button>
          <button type="button" data-testid="member-side-quest-another" disabled={busy} onClick={() => setEditing(true)}>
            {zh ? "再安排一个私人停留" : "Plan another private stop"}
          </button>
        </div>
      )}
      {(canSharePlace || currentSentInvitations.length > 0) && (
        <section className="journey-member-place-sharing" data-testid="member-place-sharing">
          <span className="v8-kicker">{zh ? "与同行者分享公开地点" : "Share a public Place with companions"}</span>
          {canSharePlace && !currentSentInvitations.some((invitation) => invitation.state === "OPEN") && (
            <button
              type="button"
              data-testid="member-place-invitation-review"
              disabled={invitationBusy}
              onClick={() => void previewPlaceSharing()}
            >
              {invitationBusy
                ? (zh ? "正在核对地点…" : "Checking the Place…")
                : (zh ? "查看分享内容…" : "Review what will be shared…")}
            </button>
          )}
          {validInvitationPreview && (
            <section
              role="dialog"
              aria-modal="true"
              aria-labelledby="member-place-invitation-preview-title"
              data-testid="member-place-invitation-preview"
            >
              <strong id="member-place-invitation-preview-title">
                {zh ? `向 ${validInvitationPreview.recipient_count} 位当前同行者分享「${validInvitationPreview.place.name}」？` : `Share “${validInvitationPreview.place.name}” with ${validInvitationPreview.recipient_count} current companions?`}
              </strong>
              <small>
                {zh
                  ? `将分享地点名称和公开精确地图点（${validInvitationPreview.place.latitude.toFixed(5)}, ${validInvitationPreview.place.longitude.toFixed(5)}）。邀请有效 ${validInvitationPreview.expires_in_minutes} 分钟，只发给当前有效成员。`
                  : `This shares the Place name and its exact public pin (${validInvitationPreview.place.latitude.toFixed(5)}, ${validInvitationPreview.place.longitude.toFixed(5)}). The invitation lasts ${validInvitationPreview.expires_in_minutes} minutes and goes only to current in-force members.`}
              </small>
              <small>
                {zh
                  ? `地点来源：${validInvitationPreview.place.source}。共同会合点：「${validInvitationPreview.rendezvous.title}」· ${validInvitationPreview.rendezvous.place_name}${validInvitationPreview.rendezvous.scheduled_at ? ` · ${new Date(validInvitationPreview.rendezvous.scheduled_at).toLocaleString(locale)}` : " · 时间未知"}。`
                  : `Place source: ${validInvitationPreview.place.source}. Shared rendezvous: ${validInvitationPreview.rendezvous.title} at ${validInvitationPreview.rendezvous.place_name}${validInvitationPreview.rendezvous.scheduled_at ? ` · ${new Date(validInvitationPreview.rendezvous.scheduled_at).toLocaleString(locale)}` : " · time unknown"}.`}
              </small>
              <small>
                {zh
                  ? "不会分享你的设备位置、原始搜索坐标、私人备注、路线或浏览历史。接受邀请不会更改共享行程，也不会自动打开地图。"
                  : "Your device location, original search coordinates, private note, route, and browsing history are not shared. Accepting will not change the shared Journey or open a map automatically."}
              </small>
              <div className="journey-side-quest-actions">
                {validInvitationPreview.recipient_count > 0 ? (
                  <button
                    type="button"
                    data-testid="member-place-invitation-confirm"
                    disabled={invitationBusy}
                    onClick={() => void confirmPlaceSharing()}
                  >
                    {invitationBusy
                      ? (zh ? "正在发送…" : "Sharing…")
                      : (zh ? "确认分享这个公开地点" : "Confirm sharing this public Place")}
                  </button>
                ) : (
                  <small data-testid="member-place-invitation-no-companions">
                    {zh ? "当前没有可邀请的同行者。行程只有你一位有效成员。" : "No companions are available to invite. You are the only current in-force member."}
                  </small>
                )}
                <button type="button" disabled={invitationBusy} onClick={() => setInvitationPreview(null)}>
                  {zh ? "取消" : "Cancel"}
                </button>
              </div>
            </section>
          )}
          {currentSentInvitations.map((invitation) => (
            <article key={invitation.invitation_id} data-testid="member-place-invitation-sent" data-state={invitation.state}>
              <strong>
                {zh ? `「${invitation.place.name}」邀请 · ${invitation.state === "OPEN" ? "有效" : invitation.state === "WITHDRAWN" ? "已撤回" : invitation.state === "EXPIRED" ? "已过期" : "已失效"}` : `Invitation for ${invitation.place.name} · ${invitation.state.toLowerCase()}`}
              </strong>
              {invitation.recipients.map((recipient) => (
                <small key={recipient.member_id}>
                  {recipient.display_name}: {recipient.state}
                </small>
              ))}
              {invitation.state === "OPEN" && (
                <button
                  type="button"
                  data-testid="member-place-invitation-withdraw"
                  disabled={invitationBusy}
                  onClick={() => void withdrawPlaceInvitation(invitation.invitation_id)}
                >
                  {zh ? "撤回尚未接受的邀请" : "Withdraw invitations not yet accepted"}
                </button>
              )}
            </article>
          ))}
          {invitationMessage && <small role="status" data-testid="member-place-sharing-message">{invitationMessage}</small>}
        </section>
      )}
      <MemberMeetupHandshakePanel
        journeyId={journeyId}
        projection={meetupHandshake ?? null}
        sideQuest={sideQuest}
        onUpdated={onUpdated}
        showGroup={false}
      />
      {navigationConsent?.key === routeContextKey && (
        <section
          role="dialog"
          aria-modal="true"
          aria-labelledby="member-side-quest-navigation-title"
          data-testid="member-side-quest-navigation-consent"
        >
          <strong id="member-side-quest-navigation-title">
            {navigationConsent.target.kind === "PRIVATE_STOP"
              ? (zh ? `打开到「${navigationConsent.target.name}」的步行导航？` : `Open walking directions to ${navigationConsent.target.name}?`)
              : (zh ? `打开到「${navigationConsent.target.name}」的地图？` : `Open a map to ${navigationConsent.target.name}?`)}
          </strong>
          <p>
            {zh
              ? "所选地点的精确坐标会发送给地图服务；CityNexus 不会在导航链接中发送地点名称或你的起点。地图服务可能按自己的权限使用设备位置。打开地图不代表到达、已返程或已与大家会合。"
              : "The selected destination's exact coordinates will be sent to your chosen maps provider. CityNexus does not include the place name or your origin in the link; that provider may use device location under its own permissions. Opening maps does not report arrival, return, or reunion."}
          </p>
          <div className="journey-side-quest-actions">
            <button
              type="button"
              data-testid="member-side-quest-consent-google"
              disabled={navigationBusy}
              onClick={() => void launchExternalNavigation("GOOGLE")}
            >
              {zh ? "同意并打开 Google Maps 步行导航" : "Agree and open Google Maps walking directions"}
            </button>
            <button
              type="button"
              data-testid="member-side-quest-consent-osm"
              disabled={navigationBusy}
              onClick={() => void launchExternalNavigation("OPENSTREETMAP")}
            >
              {zh ? "同意并打开 OpenStreetMap 地图标记" : "Agree and open OpenStreetMap pin"}
            </button>
            <button
              type="button"
              data-testid="member-side-quest-consent-cancel"
              disabled={navigationBusy}
              onClick={() => setNavigationConsent(null)}
            >
              {zh ? "取消" : "Cancel"}
            </button>
          </div>
        </section>
      )}
      {message && <small data-testid="member-side-quest-message">{message}</small>}
    </div>
  );
}
