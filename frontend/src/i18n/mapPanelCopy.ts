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
  | "viewportOffTarget"
  | "stopsShowAllTotal"
  | "stopsCollapseCta"
  | "stopsCaptureAria"
  /* ORD-0522 -- the stops the map cannot stand on. The sentence is human words and carries no
     server token; the named reason rides in a data attribute for the gates to read. */
  | "stopsWithoutPlace"
  /* ORD-0497 -- the footprint arms. Named per read, never per traveller: the empty sentence says
     what THIS read brought back, because "还没有足迹" would be a claim about the person rather than
     about the door. */
  | "footprintStamped"
  | "footprintNone"
  | "footprintRefused"
  | "footprintUnreachable"
  | "footprintAsking"
  | "footprintNotAsked"
  | "footprintBadgeAria"
  | "footprintFilterLabel"
  | "footprintFilterClear";

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
         plan composition (`services.py` in `generate_plans`/`_generic_compose_plans`,
         `services.py`, `planning/authoritative_v12.py` in `compose_authoritative_multiday`),
         stay-option materialisation (`operations/v9.py` in `materialize_stay_option`), trip-studio
         accept of ids that already exist (`main.py` in `_trip_studio_accept_impl`) and replan
         accept (`main.py`) -- re-measure with
         `review/n3_0407_write_door_inventory_r1.py`, which names the enclosing function with each line
         so a moved line number is catchable rather than silently stale;
       - `POST /journeys/{id}/location` does exist (`main.py`, `post_foreground_location`) and the
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
  /* ORD-0453 — the third state of a long stop list on a small screen.
     Borrowed shape only (`layout/layout_ai_center_poi_recommend_v2`: a list container, a mask and a
     click-capture over it): the sentences below are ours, and no asset from that package is in this
     repository. `stopsShowAllTotal` names the WHOLE count rather than the number of rows left out,
     because pressing the list down by height keeps every row in the document and reachable — a
     sentence claiming rows are "not shown" would go false the moment the pressed box is scrolled. */
  stopsShowAllTotal: { zh: "查看全部 %s 个地点", en: "Show all %s places" },
  stopsCollapseCta: { zh: "收起清单", en: "Collapse list" },
  stopsCaptureAria: { zh: "展开或收起地点清单", en: "Expand or collapse the stop list" },
  stopsWithoutPlace: { zh: "另有 %s 个站点这次没有位置，地图就不列它们", en: "%s stops have no position, so the map leaves them out" },
  /* ORD-0497 — 足迹上地图。Three rules these sentences have to keep:
     1. the `read` arm with nothing in it names the READ ("这台服务器这次没有交回"), not the
        traveller ("你没有去过任何一站"), because an empty list from one server on one occasion is
        all this surface was ever given (`backend/app/main.py` -- absence is a list, not a 404);
     2. the two unanswered arms say the map distinguishes NOTHING, so a plain pin cannot be read as
        "checked in" by someone who never asked;
     3. `%s` carries only the server's own count. The day under a pin is that same row's
        `stamped_at`, rendered date-only -- see `formatStampDay` for the glass rule that forbids a
        clock here. */
  footprintStamped: {
    zh: "这台服务器这次交回 %s 枚足迹：实心图钉是它记下的站，钉下的日期就是那一枚的时间；空心的是这次没有印的站。",
    en: "This server handed back %s footprints on this read: solid pins are the stops it recorded, and the day under a pin is that stamp's; hollow pins are the stops it returned no stamp for.",
  },
  footprintNone: {
    zh: "这台服务器这次没有交回任何足迹，所以这一趟在图上的站全部画成空心。",
    en: "This server returned no footprint on this read, so every stop of this trip is painted hollow.",
  },
  footprintRefused: {
    zh: "服务器这次没收下这个读取请求，所以图上不区分到过的站与没到过的站。",
    en: "The server would not take this read, so the map draws no distinction between stamped and unstamped stops.",
  },
  footprintUnreachable: {
    zh: "这次没有问到服务器，所以图上不区分到过的站与没到过的站——这一片的画法不替服务器说话。",
    en: "This read never reached the server, so the map draws no distinction — nothing painted here speaks for the server.",
  },
  footprintAsking: { zh: "正在向服务器问这一趟的足迹…", en: "Asking the server for this trip's footprints…" },
  footprintNotAsked: {
    zh: "这一屏没有打开的行程，所以没向服务器问过足迹。",
    en: "No trip is open on this screen, so nothing was ever asked of the server.",
  },
  footprintBadgeAria: { zh: "到站日期 %s", en: "Day stamped %s" },
  /* The 「只看打过」 switch. Two labels, one per state, in the direction of what the press DOES --
     ORD-0403 settled that a state-bearing control on this surface says a different sentence in each
     state rather than the same word with a different colour, because "只看打过" painted while the
     filter is already on tells the traveller nothing about what the next press gives back. */
  footprintFilterLabel: { zh: "只看打过", en: "Stamped only" },
  footprintFilterClear: { zh: "看全部站", en: "Show every stop" },
};

/** Same locale rule the component used inline: anything starting with zh takes the Chinese arm. */
export function mapCopy(locale: string, key: MapCopyKey): string {
  const arms = COPY[key];
  return locale.startsWith("zh") ? arms.zh : arms.en;
}
