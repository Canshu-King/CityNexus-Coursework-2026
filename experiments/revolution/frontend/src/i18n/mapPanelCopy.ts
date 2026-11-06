/**
 * ORD-A — the map surface's own copy table, kept in its own file on purpose:
 * `LocaleContext.tsx` is being edited by two other lanes at the same time, so this lane's keys land
 * here and the commander lifts them by file. `mapComp.attribution` (already in LocaleContext) holds
 * the bare OSM phrase; the lines below are the sentences that must be *readable on screen*, which
 * additionally name the tile host the component actually calls.
 *
 * `noLocationTitle` / `noLocationBody` / `tileNotice` / `gestureHint` were lifted VERBATIM out of
 * `components/v8/MapPanel.tsx` at 090c048f (the four `locale.startsWith("zh") ? … : …` ternaries at
 * :358, :360-362, :383-385, :394-396). They must stay byte-identical — moving them here is a
 * refactor, not a rewrite. `e2e/qoder-map-gesture-ownership.spec.ts` asserts the gesture hint by
 * substring, and `e2e/qoder-map-interaction.spec.ts` asserts it is visible.
 *
 * One of those four has since been rewritten *on purpose*, by the order that asked for it: ORD-0407 ①
 * changed `noLocationBody`'s second sentence because it promised a door this build does not have (the
 * measurement is on the key below). The lift stays byte-identical for `noLocationTitle`, `tileNotice`
 * and `gestureHint`; the rewrite is scoped to the one cell the order named, and
 * `e2e/qoder-ord0407-empty-state-actions.spec.ts` pins both arms of the new sentence.
 *
 * `attributionRaster` / `attributionVector` are new: the basemap obligation for
 * tile.openstreetmap.org (MapPanel) and tiles.openfreemap.org (JourneyMap), neither of which was
 * readable on screen before — both set `attributionControl:false`.
 */

export type MapCopyKey =
  | "noLocationTitle"
  | "noLocationBody"
  | "tileNotice"
  | "gestureHint"
  | "attributionRaster"
  | "attributionVector"
  | "stopsKicker"
  | "stopLocateAria"
  | "stopSelectedLead"
  | "viewportResetToJourney"
  | "viewportOnTarget"
  | "viewportOffTarget";

const COPY: Record<MapCopyKey, { zh: string; en: string }> = {
  noLocationTitle: { zh: "尚未获取地图位置",
            en: "No map location yet" },
  /* ORD-0407 ①. The second sentence used to name two remedies -- "添加活动地点" and "开启定位" -- and
     neither has a door in this build, so the cell was a promise with no handle on it. Measured, not
     assumed (review/n3_0407_write_door_inventory_r1.py, against this file's own tip):
       - the contract declares 5 write paths whose own path contains `/activities`
         (arrive/begin/complete/depart/skip); all five take an `{activity_id}` that must already exist,
         and there is no collection-level POST/PUT to `/…/activities`;
       - the backend inserts an Activity row at 6 points, none of which takes a traveller-named place:
         plan composition (`services.py:1212` in `generate_plans`/`_generic_compose_plans`,
         `services.py:1770`, `planning/authoritative_v12.py:1347` in `compose_authoritative_multiday`),
         stay-option materialisation (`operations/v9.py:631` in `materialize_stay_option`), trip-studio
         accept of ids that already exist (`main.py:5970` in `_trip_studio_accept_impl`) and replan
         accept (`main.py:9978`) -- re-measure with
         `review/n3_0407_write_door_inventory_r1.py`, which names the enclosing function with each line
         so a moved line number is catchable rather than silently stale;
       - `POST /journeys/{id}/location` does exist (`main.py:4534`, `post_foreground_location`) and the
         shell does call it (`V8Experience.tsx`, the function named `shareForegroundLocation`), but
         `tripHasNoCoordinates` (this component, :38) and `points` (:134) decide on the plan's located
         activities and the stay offers' coordinates -- by ORD-0394 that is `activityCoordinate` at :20
         reading `location`, joined off the place row -- so sharing a position cannot clear this state.
         Line numbers here are re-measurable with `review/n3_0407_receipt_anchors_r1.py`; the names are
         the anchor, because every landing shifts them (this one moved +55 within a day).
     So the cell now says which tier it is in, in the ORD-0359 word (`NOT_AVAILABLE`), and says nothing
     about a remedy the screen cannot offer. The opening clause is verbatim as before, because
     `e2e/qoder-today-follows-the-stage.spec.ts` reads it to keep the attention band and the map from
     contradicting each other; what was dropped is the promise, which is the part with no door. */
  noLocationBody: { zh: "这个行程还没有可信的坐标。「添加活动地点」这扇门，这一版没有（NOT_AVAILABLE）。",
            en: "This trip has no trustworthy coordinates yet. This build has no door to add an activity location (NOT_AVAILABLE)." },
  tileNotice: { zh: "底图未加载，显示的是路线示意。",
            en: "Basemap did not load — showing the route diagram only." },
  gestureHint: { zh: "单指拖动地图 · 双指缩放 · 想翻这一页就拖地图以外的地方",
            en: "One finger drags the map · two fingers zoom · to move the page, drag outside the map" },
  attributionRaster: { zh: "底图数据 © OpenStreetMap 贡献者 · 瓦片服务 tile.openstreetmap.org",
            en: "Basemap data © OpenStreetMap contributors · tiles: tile.openstreetmap.org" },
  attributionVector: { zh: "底图数据 © OpenStreetMap 贡献者 · 瓦片服务 tiles.openfreemap.org",
            en: "Basemap data © OpenStreetMap contributors · tiles: tiles.openfreemap.org" },
  /* ORD-0390. `stopsKicker` names the list, `stopLocateAria` says what pressing a row does (a
     screen reader gets "定位" not just the stop name), and `stopSelectedLead` marks the card that
     reflects the shared selection so it reads as a state, not as a second heading. */
  stopsKicker: { zh: "这一天去的地点", en: "Stops on this day" },
  stopLocateAria: { zh: "在地图上定位 %s", en: "Locate %s on the map" },
  /* ORD-0403. The state arms name what the traveller would lose by pressing, so the label is a
     different sentence in each state rather than the same word with a different colour. */
  viewportResetToJourney: { zh: "回到行程视野", en: "Fit to my journey" },
  viewportOnTarget: { zh: "视野正落在这段行程上", en: "The view is on your journey" },
  viewportOffTarget: { zh: "已移出行程视野，点按回到全程", en: "Moved off the journey — tap to fit it all" },
  stopSelectedLead: { zh: "已选中", en: "Selected" },
};

/** Same locale rule the component used inline: anything starting with zh takes the Chinese arm. */
export function mapCopy(locale: string, key: MapCopyKey): string {
  const arms = COPY[key];
  return locale.startsWith("zh") ? arms.zh : arms.en;
}
