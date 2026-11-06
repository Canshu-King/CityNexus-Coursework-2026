import { useEffect, useState } from "react";
import {
  CityNexusApiError,
  cityNexusApi,
  type LiveMomentScoutResult,
  type MomentScoutInterest,
  type MomentScoutResult,
} from "../../api/citynexus";
import {
  buildGoogleWalkingDirectionsUrl,
  buildOpenStreetMapPinUrl,
} from "./memberSideQuestNavigationLinks";
import { useLocale } from "../../i18n/LocaleContext";

export function MomentScoutCard({
  journeyId,
  onRefreshLocation,
  onUpdated,
  sharedRendezvous,
}: {
  journeyId: string;
  onRefreshLocation?: () => Promise<string | null>;
  onUpdated: () => Promise<void>;
  sharedRendezvous?: {
    placeName: string;
    latitude: number | null;
    longitude: number | null;
    coordinateSystem: "WGS84" | null;
  } | null;
}) {
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const [budgetMinutes, setBudgetMinutes] = useState<15 | 30 | 45>(30);
  const [interest, setInterest] = useState<MomentScoutInterest>("ANYTHING");
  const [receipt, setReceipt] = useState<{
    value: MomentScoutResult;
    locationObservationId: string;
  } | null>(null);
  const [selectedPlaceId, setSelectedPlaceId] = useState("");
  const [liveReceipt, setLiveReceipt] = useState<LiveMomentScoutResult | null>(null);
  const [liveSelectedCandidateId, setLiveSelectedCandidateId] = useState("");
  const [showLiveConsent, setShowLiveConsent] = useState(false);
  const [showSharedStopFallback, setShowSharedStopFallback] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const sharedStopTarget = sharedRendezvous
    && sharedRendezvous.latitude != null
    && sharedRendezvous.longitude != null
    && sharedRendezvous.coordinateSystem === "WGS84"
    ? {
        kind: "SHARED_RENDEZVOUS" as const,
        place_id: "shared-rendezvous",
        name: sharedRendezvous.placeName,
        latitude: sharedRendezvous.latitude,
        longitude: sharedRendezvous.longitude,
        coordinate_system: "WGS84" as const,
        provenance: {
          provider_key: "journey-active-plan",
          external_id: "shared-rendezvous",
          source_coordinate_system: "WGS84",
        },
        navigation_available: true,
        unavailable_reason: null,
      }
    : null;

  const openSharedStopMap = (provider: "google" | "osm") => {
    if (!sharedStopTarget) {
      setMessage(zh
        ? "当前共同集合点没有可公开使用的 WGS84 坐标；没有打开地图。"
        : "The current shared stop has no public WGS84 coordinates, so no map was opened.");
      return;
    }
    try {
      const url = provider === "google"
        ? buildGoogleWalkingDirectionsUrl(sharedStopTarget)
        : buildOpenStreetMapPinUrl(sharedStopTarget);
      window.open(url, "_blank", "noopener,noreferrer");
      setShowSharedStopFallback(false);
    } catch {
      setMessage(zh ? "当前共同集合点暂时没有可用地图坐标。" : "The current shared stop has no usable map coordinates.");
    }
  };

  useEffect(() => {
    if (!receipt) return;
    const timeout = window.setTimeout(() => {
      setReceipt(null);
      setSelectedPlaceId("");
    }, 60_000);
    return () => window.clearTimeout(timeout);
  }, [receipt]);

  useEffect(() => {
    if (!liveReceipt?.expires_at) return;
    const delay = Math.max(0, Date.parse(liveReceipt.expires_at) - Date.now());
    const timeout = window.setTimeout(() => {
      setLiveReceipt(null);
      setLiveSelectedCandidateId("");
      setMessage(zh
        ? "这组实时地点线索已过期；如需继续，请再次明确同意并搜索。"
        : "These live place leads expired. Give consent again to run a new search.");
    }, delay);
    return () => window.clearTimeout(timeout);
  }, [liveReceipt, zh]);

  const scout = async () => {
    if (busy || !onRefreshLocation) return;
    setBusy(true);
    setMessage(null);
    setReceipt(null);
    setLiveReceipt(null);
    setLiveSelectedCandidateId("");
    setShowLiveConsent(false);
    setShowSharedStopFallback(false);
    setSelectedPlaceId("");
    try {
      const locationObservationId = await onRefreshLocation();
      if (!locationObservationId) {
        setShowSharedStopFallback(Boolean(sharedStopTarget));
        setMessage(zh
          ? "没有取得这一次的位置；没有生成附近推荐。你可以打开当前共同集合点的公共地图，或继续共同计划。"
          : "No one-time location was available, so no nearby options were generated. Open the current shared stop on a public map or continue the shared plan.");
        return;
      }
      const value = await cityNexusApi.scoutMemberMoment(
        journeyId,
        locationObservationId,
        budgetMinutes,
        interest,
      );
      setReceipt({ value, locationObservationId });
      if (value.scout_state === "NO_GROUNDED_OPTIONS") {
        setMessage(zh
          ? "附近没有可核实的已知地点；当前共同计划保持不变。"
          : "There are no grounded known places nearby. Your shared Journey stays unchanged.");
      }
    } catch {
      setMessage(zh
        ? "暂时无法查找附近体验；共享行程没有改变。请刷新位置后重试。"
        : "Nearby discovery is unavailable right now. The shared Journey is unchanged; refresh your location and try again.");
    } finally {
      setBusy(false);
    }
  };

  const requestLiveScout = async () => {
    if (busy || !showLiveConsent || !onRefreshLocation) return;
    setShowLiveConsent(false);
    setBusy(true);
    setMessage(null);
    setLiveReceipt(null);
    setLiveSelectedCandidateId("");
    setShowSharedStopFallback(false);
    try {
      const locationObservationId = await onRefreshLocation();
      if (!locationObservationId) {
        setShowSharedStopFallback(Boolean(sharedStopTarget));
        setLiveReceipt({
          scout_state: "LOCATION_UNAVAILABLE",
          budget_minutes: budgetMinutes,
          interest,
          radius_m: 1200,
          message: zh
            ? "没有取得这一次的精确位置；外部地点服务没有收到搜索请求。"
            : "A fresh location was unavailable; the external place provider was not contacted.",
          candidates: [],
        });
        return;
      }
      const value = await cityNexusApi.scoutLiveMemberMoment(
        journeyId,
        locationObservationId,
        budgetMinutes,
        interest,
      );
      setLiveReceipt(value);
      if (value.scout_state !== "LIVE_RESULTS") setMessage(value.message);
    } catch (error) {
      const detail = error instanceof CityNexusApiError
        && error.body && typeof error.body === "object" && "detail" in error.body
        ? String(error.body.detail ?? "")
        : "";
      if (detail.includes("LIVE_MOMENT_SCOUT_CONTEXT_CHANGED")
        || detail.includes("LIVE_MOMENT_SCOUT_RENDEZVOUS_TIME_PASSED")) {
        setLiveReceipt(null);
        setMessage(zh
          ? "外部查询期间共同会合时间、行程或成员状态已变化，未把这次地点线索用于私人支线。请刷新当前行程后重新选择。"
          : "The shared meetup, Journey or member status changed while searching. These live leads were not used. Refresh the Journey before choosing a stop.");
        return;
      }
      const locationUnavailable = detail.includes("LOCATION_OBSERVATION")
        || detail.includes("SIDE_QUEST_LOCATION")
        || detail.includes("MOMENT_SCOUT_LOCATION");
      setLiveReceipt({
        scout_state: locationUnavailable ? "LOCATION_UNAVAILABLE" : "PROVIDER_UNAVAILABLE",
        budget_minutes: budgetMinutes,
        interest,
        radius_m: 1200,
        message: locationUnavailable
          ? (zh
            ? "这次位置已过期或不够精确；没有向外部地点服务发送搜索。请重新读取位置。"
            : "This location is stale or imprecise; no search was sent to the external provider. Refresh your location.")
          : (zh
            ? "实时地点服务暂时不可用；已知地点和手动选择仍可继续使用。"
            : "Live place data is unavailable right now. Known places and manual choices remain available."),
        candidates: [],
      });
    } finally {
      setBusy(false);
    }
  };

  const confirmLiveSelection = async () => {
    const candidate = liveReceipt?.candidates.find(
      (item) => item.candidate_id === liveSelectedCandidateId,
    );
    if (!candidate || !liveReceipt?.receipt_id || busy) return;
    setBusy(true);
    setMessage(null);
    try {
      await cityNexusApi.selectLiveMomentScoutCandidate(
        journeyId,
        liveReceipt.receipt_id,
        candidate.candidate_id,
      );
      setLiveReceipt(null);
      setLiveSelectedCandidateId("");
      setMessage(zh
        ? "已把你确认的 OSM 地点加入公共地点目录，并准备好本人可见的私人停留；路线和返程时间仍未核实。"
        : "Your confirmed OSM place is in the public place catalog and your private stop is ready. Route and return time remain unverified.");
      try {
        await onUpdated();
      } catch {
        setMessage(zh
          ? "私人停留已保存；页面刷新失败，请手动刷新查看当前状态。"
          : "Your private stop is saved, but the screen did not refresh. Reload to see its current state.");
      }
    } catch (error) {
      const detail = error instanceof CityNexusApiError
        && error.body && typeof error.body === "object" && "detail" in error.body
        ? String(error.body.detail ?? "")
        : "";
      if (detail.includes("LIVE_MOMENT_SCOUT_RENDEZVOUS_TIME_PASSED")) {
        setLiveReceipt(null);
        setLiveSelectedCandidateId("");
        setMessage(zh
          ? "共同会合时间已过，不能继续创建私人停留；请先跟进当前共享行程。"
          : "The shared rendezvous time has passed. Don't start a new private stop; check the current shared Journey.");
      } else if (detail.includes("SCOUT_EXPIRED")) {
        setLiveReceipt(null);
        setLiveSelectedCandidateId("");
        setMessage(zh
          ? "这组实时地点线索或共同会合安排已过期；请刷新后重新查找并再次同意外部搜索。"
          : "These live leads or the shared rendezvous expired. Refresh, search again, and give consent again.");
      } else if (detail.includes("Start the Journey Operator before planning a side trip")) {
        setMessage(zh
          ? "请先点击「照看这趟行程」开启 Journey Operator，再确认这条私人支线。实时地点回执仍保留。"
          : "Start Journey Operator on this page, then confirm this private stop. The live result is still available.");
      } else if (error instanceof CityNexusApiError && [401, 403].includes(error.status)) {
        setMessage(zh
          ? "你的行程成员身份无效或已失效；没有替其他成员创建私人支线。"
          : "Your Journey membership or sign-in is no longer valid. No stop was created for another member.");
      } else {
        setMessage(zh
          ? "地点确认未完成；请刷新查看当前支线状态后再试。"
          : "The place was not confirmed. Refresh to check the current private-stop state before trying again.");
      }
      try {
        await onUpdated();
      } catch {
        // Keep the recovery guidance visible if the projection refresh fails.
      }
    } finally {
      setBusy(false);
    }
  };

  const confirmSelection = async () => {
    const candidate = receipt?.value.candidates.find((item) => item.place_id === selectedPlaceId);
    if (!candidate || !receipt || busy) return;
    setBusy(true);
    setMessage(null);
    try {
      await cityNexusApi.selectMomentScoutCandidate(
        journeyId,
        receipt.locationObservationId,
        receipt.value.budget_minutes,
        receipt.value.interest,
        receipt.value.scout_context,
        candidate.place_id,
      );
      setReceipt(null);
      setSelectedPlaceId("");
      setMessage(zh
        ? "私人停留已准备好；路线时间仍未核实。共同会合点和共享计划未改变。"
        : "Your private stop is ready. Route time remains unverified; the rendezvous and shared plan are unchanged.");
      try {
        await onUpdated();
      } catch {
        setMessage(zh
          ? "私人停留已保存；页面刷新失败，请手动刷新查看当前状态。"
          : "Your private stop is saved, but the screen did not refresh. Reload to see its current state.");
      }
    } catch (error) {
      setReceipt(null);
      setSelectedPlaceId("");
      const detail = error instanceof CityNexusApiError
        && error.body && typeof error.body === "object" && "detail" in error.body
        ? String(error.body.detail ?? "")
        : "";
      if (detail.includes("Start the Journey Operator before planning a side trip")) {
        setMessage(zh
          ? "请先在本页点击「照看这趟行程」开启 Journey Operator，再重新查找并确认私人停留。尚未创建私人支线。"
          : "First tap 'Take care of this trip' to start Journey Operator on this page, then scout and confirm again. No private stop was created.");
      } else if (error instanceof CityNexusApiError && [401, 403].includes(error.status)) {
        setMessage(zh
          ? "你的行程成员身份无效或已失效，请重新登录自己的账号。没有替其他成员创建私人支线。"
          : "Your Journey membership or sign-in is no longer valid. Sign in as yourself; no stop was created for another member.");
      } else if (error instanceof CityNexusApiError && error.status === 409) {
        setMessage(zh
          ? "推荐、共同会合安排或当前私人支线状态已经变化。请刷新再查找；如果分离意图已生效，可以继续完成私人地点选择。"
          : "The suggestion, rendezvous or private-stop state changed. Refresh and scout again; if a split was created, continue choosing your stop.");
      } else {
        setMessage(zh
          ? "暂时无法完成地点选择，请刷新确认当前状态后再试。"
          : "The destination could not be selected. Refresh to check the current state before trying again.");
      }
      try {
        await onUpdated();
      } catch {
        // Keep the recoverable selection message visible if the projection reload also fails.
      }
    } finally {
      setBusy(false);
    }
  };

  const rendezvous = receipt?.value.rendezvous;
  const rendezvousIsPast = rendezvous?.time_state === "PAST";
  const shortWindow = rendezvous?.time_state === "AVAILABLE"
    && rendezvous.minutes_until !== null
    && rendezvous.minutes_until < (receipt?.value.budget_minutes ?? 0);
  const rendezvousTime = rendezvous?.scheduled_at
    ? new Date(rendezvous.scheduled_at).toLocaleString(locale, { dateStyle: "short", timeStyle: "short" })
    : null;

  return (
    <section className="journey-moment-scout" data-testid="moment-scout" aria-live="polite">
      <strong>{zh ? "附近有什么可以顺路玩？" : "Find a quick adventure nearby"}</strong>
      <small>
        {zh
          ? "仅在你点击后读取一次当前位置；距离是直线距离，不是步行时间。推荐仅本人可见。"
          : "A single foreground location sample is requested only after you tap. Distance is straight-line, not walking time; suggestions are visible only to you."}
      </small>
      <label>
        {zh ? "个人时间偏好" : "Personal time budget"}
        <select
          data-testid="moment-scout-budget"
          value={budgetMinutes}
          disabled={busy}
          onChange={(event) => {
            setBudgetMinutes(Number(event.target.value) as 15 | 30 | 45);
            setReceipt(null);
            setSelectedPlaceId("");
            setLiveReceipt(null);
            setLiveSelectedCandidateId("");
            setShowLiveConsent(false);
          }}
        >
          <option value={15}>{zh ? "15 分钟" : "15 minutes"}</option>
          <option value={30}>{zh ? "30 分钟" : "30 minutes"}</option>
          <option value={45}>{zh ? "45 分钟" : "45 minutes"}</option>
        </select>
      </label>
      <label>
        {zh ? "兴趣（可选）" : "Interest (optional)"}
        <select
          data-testid="moment-scout-interest"
          value={interest}
          disabled={busy}
          onChange={(event) => {
            setInterest(event.target.value as MomentScoutInterest);
            setReceipt(null);
            setSelectedPlaceId("");
            setLiveReceipt(null);
            setLiveSelectedCandidateId("");
            setShowLiveConsent(false);
          }}
        >
          <option value="ANYTHING">{zh ? "随便看看" : "Anything"}</option>
          <option value="COFFEE_FOOD">{zh ? "咖啡 / 美食" : "Coffee / food"}</option>
          <option value="CULTURE">{zh ? "文化" : "Culture"}</option>
          <option value="NATURE">{zh ? "自然" : "Nature"}</option>
        </select>
      </label>
      <button
        type="button"
        data-testid="moment-scout-submit"
        disabled={busy || !onRefreshLocation}
        onClick={() => void scout()}
      >
        {busy ? (zh ? "正在查找…" : "Looking nearby…") : (zh ? "查找附近体验" : "Scout nearby")}
      </button>
      {!liveReceipt && (
        <button
          type="button"
          data-testid="moment-scout-live-opt-in"
          disabled={busy || !onRefreshLocation || rendezvousIsPast}
          onClick={() => setShowLiveConsent(true)}
        >
          {receipt?.value.scout_state === "NO_GROUNDED_OPTIONS"
            ? (zh ? "搜索真实的附近地点" : "Search live nearby places")
            : (zh ? "直接搜索真实附近地点（需同意）" : "Explore real nearby places (consent required)")}
        </button>
      )}
      {showLiveConsent && (
        <div role="group" data-testid="moment-scout-live-consent">
          <small>
            {zh
              ? "同意后会主动读取一次前台位置：位置观察保存于 Journey，可能改变同行者可见的粗粒度队伍位置状态；外部 OpenStreetMap 地点服务收到你附近的搜索中心。同行者不会自动收到你的精确起点、搜索兴趣和未选择地点。地点可能过时、关闭或不可达；取消不会发送外部查询。"
              : "With consent, one foreground observation is stored on this Journey and may change companions' coarse group-presence view. The external OpenStreetMap place provider receives the nearby search center. Companions do not automatically receive your precise origin, search interest, or unselected places. Leads may be outdated, closed or unreachable; cancelling sends no external query."}
          </small>
          <button
            type="button"
            data-testid="moment-scout-live-consent-confirm"
            disabled={busy || !onRefreshLocation}
            onClick={() => void requestLiveScout()}
          >
            {zh ? "同意并搜索一次" : "Consent and search once"}
          </button>
          <button
            type="button"
            data-testid="moment-scout-live-consent-cancel"
            disabled={busy}
            onClick={() => setShowLiveConsent(false)}
          >
            {zh ? "取消" : "Cancel"}
          </button>
        </div>
      )}
      {showSharedStopFallback && sharedStopTarget && (
        <div role="group" data-testid="moment-scout-shared-stop-fallback">
          <strong>{zh ? "先回到当前共同集合点" : "Return to the current shared stop"}</strong>
          <small>
            {zh
              ? `当前位置没有取得；你可以打开「${sharedStopTarget.name}」的公共地图。这里只发送公开集合点坐标，不发送当前位置，也不会请求附近地点服务。`
              : `Your current location was unavailable. Open the public map for “${sharedStopTarget.name}”. Only the public shared-stop coordinates are sent; your current location is not shared and no nearby provider request is made.`}
          </small>
          <button
            type="button"
            data-testid="moment-scout-shared-stop-google"
            onClick={() => openSharedStopMap("google")}
          >
            {zh ? "打开步行地图" : "Open walking map"}
          </button>
          <button
            type="button"
            data-testid="moment-scout-shared-stop-osm"
            onClick={() => openSharedStopMap("osm")}
          >
            {zh ? "打开 OpenStreetMap" : "Open OpenStreetMap"}
          </button>
        </div>
      )}
      {receipt?.value.scout_state === "OPTIONS_AVAILABLE" && (
        <div data-testid="moment-scout-results" data-state={receipt.value.scout_state}>
          <small>
            {zh
              ? `共同会合点：${receipt.value.rendezvous.place_name}${rendezvousTime ? ` · ${rendezvousTime}` : ""} · ${receipt.value.budget_minutes} 分钟仅作个人偏好。`
              : `Rejoin at ${receipt.value.rendezvous.place_name}${rendezvousTime ? ` · ${rendezvousTime}` : ""} · ${receipt.value.budget_minutes} min is a personal preference.`}
          </small>
          {rendezvous?.time_state === "UNKNOWN" && (
            <small role="status" data-testid="moment-scout-rendezvous-time-warning">
              {zh
                ? "共同会合时间未知；离开前请核对共享计划。路线时间也未核实。"
                : "The shared rendezvous time is unknown. Check the shared plan before leaving; route time is also unverified."}
            </small>
          )}
          {rendezvous?.time_state === "PAST" && (
            <small role="alert" data-testid="moment-scout-rendezvous-time-warning">
              {zh
                ? "下一共同会合时间已过，不能创建私人停留；请继续共同计划。"
                : "The next shared rendezvous time has passed, so a private stop cannot be started. Continue the shared plan."}
            </small>
          )}
          {rendezvous?.time_state === "AVAILABLE" && shortWindow && (
            <small role="status" data-testid="moment-scout-rendezvous-time-warning">
              {zh
                ? `距离会合约 ${rendezvous.minutes_until} 分钟，少于你选的 ${receipt.value.budget_minutes} 分钟偏好；路线时间未核实。`
                : `About ${rendezvous.minutes_until} minutes remain before the rendezvous, less than your ${receipt.value.budget_minutes}-minute preference. Route time is unverified.`}
            </small>
          )}
          {receipt.value.candidates.map((candidate) => (
            <article key={candidate.place_id} data-testid="moment-scout-candidate" data-place-id={candidate.place_id}>
              <strong>{candidate.name}</strong>
              <small>
                {candidate.category} · {zh ? `直线 ${candidate.straight_line_distance_m} 米` : `${candidate.straight_line_distance_m} m straight line, not a route ETA`}
              </small>
              <small>
                {candidate.place_provenance === "DEMO_FIXTURE"
                  ? (zh ? "来源：演示 / 夹具地点" : "Source: demo / fixture place")
                  : `Source: ${candidate.place_provenance.replace("CITY_PLACE_CATALOG:", "")}`}
                {zh ? " · 营业状态未知 · 会合适配未核实" : " · opening status unknown · return fit unverified"}
              </small>
              <button
                type="button"
                data-testid="moment-scout-choose"
                disabled={busy || rendezvousIsPast}
                aria-pressed={selectedPlaceId === candidate.place_id}
                onClick={() => setSelectedPlaceId(candidate.place_id)}
              >
                {selectedPlaceId === candidate.place_id
                  ? (zh ? "已选择" : "Selected")
                  : (zh ? "选择这个地点" : "Choose this place")}
              </button>
            </article>
          ))}
          {selectedPlaceId && (
            <div data-testid="moment-scout-confirmation">
              <small>
                {zh
                  ? `确认先去「${receipt.value.candidates.find((item) => item.place_id === selectedPlaceId)?.name}」，再回到「${receipt.value.rendezvous.place_name}」${rendezvousTime ? `（${rendezvousTime}）` : ""}会合。${rendezvous?.time_state === "UNKNOWN" ? "会合时间未知，请核对共享计划。" : shortWindow ? `剩余时间少于 ${receipt.value.budget_minutes} 分钟偏好。` : ""}路线时间尚未核实。`
                  : `Go to ${receipt.value.candidates.find((item) => item.place_id === selectedPlaceId)?.name}, then rejoin at ${receipt.value.rendezvous.place_name}${rendezvousTime ? ` (${rendezvousTime})` : ""}.${rendezvous?.time_state === "UNKNOWN" ? " Rendezvous time is unknown; check the shared plan." : shortWindow ? ` Less than your ${receipt.value.budget_minutes}-minute preference remains.` : ""} Route time is unverified.`}
              </small>
              <button
                type="button"
                data-testid="moment-scout-confirm"
                disabled={busy || rendezvousIsPast}
                onClick={() => void confirmSelection()}
              >
                {busy
                  ? (zh ? "正在准备私人停留…" : "Preparing private stop…")
                  : shortWindow
                    ? (zh ? "仍要继续并确认" : "Continue despite short time")
                    : (zh ? "确认私人停留" : "Confirm private stop")}
              </button>
            </div>
          )}
          <small>{zh ? "选项是附近地点线索，不代表营业、可达或能按时返回。" : "These are nearby place leads, not proof of opening, route access, or an on-time return."}</small>
        </div>
      )}
      {liveReceipt && (
        <div
          data-testid="moment-scout-live-results"
          data-state={liveReceipt.scout_state}
        >
          <strong>
            {liveReceipt.scout_state === "LIVE_RESULTS"
              ? (zh ? "OpenStreetMap 实时地点线索" : "Live OpenStreetMap place leads")
              : liveReceipt.scout_state === "LIVE_ZERO_RESULTS"
                ? (zh ? "没有可操作的命名地点" : "No actionable named places")
                : liveReceipt.scout_state === "PROVIDER_DISABLED"
                  ? (zh ? "实时地点搜索已关闭" : "Live place search is disabled")
                  : liveReceipt.scout_state === "LOCATION_UNAVAILABLE"
                    ? (zh ? "当前位置不可用" : "Location unavailable")
                    : (zh ? "实时地点服务未响应" : "Live place provider did not respond")}
          </strong>
          <small>{liveReceipt.message}</small>
          {liveReceipt.captured_at && (
            <small>
              {zh ? "采集时间：" : "Captured: "}
              {new Date(liveReceipt.captured_at).toLocaleString()}
            </small>
          )}
          {liveReceipt.scout_state === "LIVE_RESULTS" && liveReceipt.rendezvous && (
            <small>
              {zh
                ? `选定后返回当前共同会合点：${liveReceipt.rendezvous.place_name}。`
                : `If selected, rejoin at the current shared rendezvous: ${liveReceipt.rendezvous.place_name}.`}
            </small>
          )}
          {liveReceipt.scout_state === "LIVE_RESULTS" && liveReceipt.candidates.map((candidate) => (
            <article
              key={candidate.candidate_id}
              data-testid="moment-scout-live-candidate"
              data-candidate-id={candidate.candidate_id}
            >
              <strong>{candidate.name}</strong>
              <small>
                {candidate.category} · {zh
                  ? `直线 ${candidate.straight_line_distance_m} 米`
                  : `${candidate.straight_line_distance_m} m straight line, not a route ETA`}
              </small>
              <small>
                {candidate.osm_type}/{candidate.osm_id} · {zh
                  ? "营业状态未知 · 费用未知 · 路线和会合适配未核实"
                  : "opening and cost unknown · route and return fit unverified"}
              </small>
              <a href={candidate.source_url} target="_blank" rel="noreferrer">
                {zh ? "查看 OpenStreetMap 来源" : "View OpenStreetMap source"}
              </a>
              <button
                type="button"
                data-testid="moment-scout-live-choose"
                disabled={busy}
                aria-pressed={liveSelectedCandidateId === candidate.candidate_id}
                onClick={() => setLiveSelectedCandidateId(candidate.candidate_id)}
              >
                {liveSelectedCandidateId === candidate.candidate_id
                  ? (zh ? "已选择" : "Selected")
                  : (zh ? "选择这个地点" : "Choose this place")}
              </button>
            </article>
          ))}
          {liveSelectedCandidateId && liveReceipt.rendezvous && (
            <div data-testid="moment-scout-live-confirmation">
              <small>
                {zh
                  ? `确认去「${liveReceipt.candidates.find((item) => item.candidate_id === liveSelectedCandidateId)?.name}」，然后返回当前共同会合点「${liveReceipt.rendezvous.place_name}」。营业、路线和按时返回能力都尚未核实。`
                  : `Confirm the place, then return to the current shared rendezvous at ${liveReceipt.rendezvous.place_name}. Opening, route and on-time return are unverified.`}
              </small>
              <button
                type="button"
                data-testid="moment-scout-live-confirm"
                disabled={busy}
                onClick={() => void confirmLiveSelection()}
              >
                {busy
                  ? (zh ? "正在准备私人停留…" : "Preparing private stop…")
                  : (zh ? "确认地点并返回会合点" : "Confirm place and return rendezvous")}
              </button>
            </div>
          )}
          {liveReceipt.attribution && (
            <small>
              {liveReceipt.attribution} · <a href={liveReceipt.attribution_url} target="_blank" rel="noreferrer">
                {zh ? "数据许可与署名" : "Data license and attribution"}
              </a>
            </small>
          )}
          <small>
            {zh
              ? "外部地点线索仅在本人确认选择后加入公共地点目录；搜索位置、兴趣和未选择候选不会写入同行者可读记录。"
              : "Only the place you confirm is added to the public place catalog. Your search location, interest and unselected leads are not written to companion-readable Journey records."}
          </small>
        </div>
      )}
      {message && <small data-testid="moment-scout-message">{message}</small>}
    </section>
  );
}
