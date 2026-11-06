import { useEffect, useMemo, useRef, useState } from "react";
import "maplibre-gl/dist/maplibre-gl.css";
import type { FeatureCollection } from "geojson";
import type { ActivityCoordinate, V8Data } from "../../app/v8Types";
import { useT, useLocale } from "../../i18n/LocaleContext";
import { mapCopy } from "../../i18n/mapPanelCopy";
import { MapViewportControls, type ViewportState } from "./MapViewportControls";
import "../../map-attribution.css";
import "../../map-selection.css";

/**
 * ORD-0394: the one rule for "does this activity have a place on the map".
 *
 * It reads `location`, which the server joins off the place row the activity names, and it returns
 * the system the number is written in alongside it.  Six call sites used to test
 * `metadata.longitude/latitude` -- a pair the backend has never once sent (0 of 140 activity rows),
 * so every one of those branches had only ever taken the "no coordinate" path on real data.  One
 * exported predicate instead, because a rule with six copies is six chances to drift.
 */
export function activityCoordinate(item: {
  location?: ActivityCoordinate | null;
}): { latitude: number; longitude: number; coordinate_system: string | null } | null {
  const location = item.location;
  if (!location || location.latitude == null || location.longitude == null) return null;
  return {
    latitude: location.latitude,
    longitude: location.longitude,
    coordinate_system: location.coordinate_system,
  };
}

/**
 * ORD-0082 ④: the map's own "there is nothing to centre this on" condition, as a pure function of
 * the same fields the point list reads.  Exported because one viewport used to hold two
 * contradictory answers -- the map panel saying this trip has no trustworthy coordinates while the
 * attention band beside it said "一切正常".  One screen, one rule.
 */
export function tripHasNoCoordinates(data: V8Data | null): boolean {
  if (!data) return true;
  const locatedActivity = (data.active_plan?.activities ?? []).some(
    (item) => activityCoordinate(item) != null,
  );
  if (locatedActivity) return false;
  return (data.offers ?? []).some(
    (offer) => offer.location.longitude != null && offer.location.latitude != null,
  ) === false;
}

export function MapPanel({
  data,
  option,
  onNoLocationChange = null,
  selectedActivityId = null,
  onSelectActivity = null,
}: {
  data: V8Data | null;
  option: V8Data["options"][number] | null;
  /** ORD-0082 ④: only the map knows whether it found *something to centre on* -- route geometry
      counts, and a trip with no located activity can still have a route.  Attention needs that
      same answer or one screen can say "没有可信的坐标" and "一切正常" at once. */
  onNoLocationChange?: ((value: boolean) => void) | null;
  /** ORD-0390: the map does not own a selection.  This is the same `detailActivityId` the trips
      list and `ActivityDetail` already read, so one stop can be selected in exactly one place. */
  selectedActivityId?: string | null;
  /** ORD-0390: called with the activity id a marker stands for, or null to clear.  A stay offer
      that has no plan activity reports null rather than inventing an id. */
  onSelectActivity?: ((id: string | null) => void) | null;
}) {
  const host = useRef<HTMLDivElement | null>(null);
  /* ORD-0390: the marker elements and the map instance are kept in refs so a selection change can
     restyle a marker and move the camera without tearing the map down and rebuilding it — the
     rebuild path is `useEffect([data, points, route])` below, and panning through it would reset
     the zoom the traveller just chose. */
  const mapRef = useRef<import("maplibre-gl").Map | null>(null);
  const markerEls = useRef<{ el: HTMLElement; id: string | null }[]>([]);
  /* The click handlers are attached inside the map-building effect, which deliberately does not
     depend on `onSelectActivity` (rebuilding the map to rebind a callback would reset the camera).
     A ref keeps the handler current without making the camera its hostage. */
  const selectRef = useRef(onSelectActivity);
  selectRef.current = onSelectActivity;
  // Map truth: never centre on a hardcoded city.  If the Journey has no
  // trustworthy coordinates we say so explicitly instead of silently showing
  // an unrelated city (design corrections §10).
  const [noLocation, setNoLocation] = useState(false);
  /* A blocked or offline raster tile server used to degrade silently to the flat
     canvas underneath, which is exactly what reads as "the map is just a picture".
     The fallback stays, but it is now named. */
  const [tilesUnavailable, setTilesUnavailable] = useState(false);
  /* ORD-0403: one control, and the control carries its own state. `targetRef` is the point the
     camera was last asked to frame — the whole journey, or a single selected stop — and
     `viewportState` says whether the camera is still on it. It is recomputed from the camera on
     every move/zoom end, so what changes the label is the map actually moving, not a flag set at
     click time that could drift away from what is on screen. */
  const [viewportState, setViewportState] = useState<ViewportState>("on-target");
  const targetRef = useRef<{ lng: number; lat: number } | null>(null);
  const refitRef = useRef<(() => void) | null>(null);
  const t = useT();
  const { locale } = useLocale();
  const route = useMemo(() => {
    /* Stamp the travel mode and the leg order onto the feature so the line layer has
       something to group by.  Measured: the provider's geometry already carries
       `properties.mode` ({"crs":"WGS84","mode":"AUTO",...}), so for today's data this
       re-states what is already there.  It stays because the grouping is what stops
       "一根大线": a provider that omits the key would otherwise collapse every leg onto
       the fallback colour and nothing on screen would say the style had degraded. */
    const tag = (
      feature: { type: "Feature"; properties?: Record<string, unknown>; geometry: { type: "LineString"; coordinates: number[][] } },
      mode: string | null | undefined,
      index: number,
    ) => ({
      ...feature,
      properties: { ...(feature.properties ?? {}), mode: mode ?? "", leg_index: index },
    });
    if (data?.active_plan?.legs.length)
      return data.active_plan.legs.map((leg, index) => tag(leg.geometry, leg.mode, index));
    if (!data || !option) return [];
    return option.leg_mode_map.flatMap((leg, index) => {
      const fact = data.mobility_facts.find(
        (item) =>
          item.from_key === leg.from_key &&
          item.to_key === leg.to_key &&
          item.mode === leg.mode,
      );
      return fact?.geometry ? [tag(fact.geometry, leg.mode, index)] : [];
    });
  }, [data, option]);
  /* ORD-0418: which sentence the caption is allowed to speak. `route.length` is the same predicate the
     line layer above already uses, so the words and the drawn geometry cannot disagree -- a second
     condition would be a second truth. The provider status is read from the payload's own
     `provider_mesh.mobility`, and only from the two entries that can actually produce a leg geometry
     (valhalla and amap): the mesh also carries transit_realtime and ride_hail, whose statuses are
     UNKNOWN_REALTIME / UNKNOWN_COST on this deployment and say nothing about routing. Treating every
     mobility entry as a routing provider would have kept the sharper sentence permanently unreachable
     -- measured, not assumed. */
  const noRoutingProvider = (() => {
    const mobility = data?.provider_mesh?.mobility;
    if (!mobility || typeof mobility !== "object") return false;
    const routers = ["valhalla", "amap"]
      .map((key) => (mobility as Record<string, { status?: unknown }>)[key])
      .filter((entry) => entry);
    return routers.length > 0 && routers.every((entry) => entry.status === "NOT_CONFIGURED");
  })();
  const routeModes = useMemo(
    () =>
      Array.from(
        new Set(route.map((feature) => String(feature.properties?.mode ?? "")).filter(Boolean)),
      ),
    [route],
  );
  const points = useMemo(() => {
    const active =
      data?.active_plan?.activities.flatMap((item) => {
        const at = activityCoordinate(item);
        return at
          ? [
              {
                id: item.id as string | null,
                title: item.title,
                longitude: at.longitude,
                latitude: at.latitude,
                role: item.metadata.role,
                protected: item.protected,
                current: item.id === data?.active_plan?.current_activity_id,
                disrupted:
                  data?.active_plan?.disruptions.some(
                    (event) => event.affected_activity_id === item.id,
                  ) ?? false,
              },
            ]
          : [];
      }) ?? [];
    if (active.length) return active;
    if (!data) return [];
    return data.offers.slice(0, 1).map((offer) => ({
      // A stay offer is not a plan activity, so it has no id to select.  Inventing one would let a
      // marker claim a selection the trips list cannot show.
      id: null as string | null,
      title: offer.property_name,
      longitude: offer.location.longitude,
      latitude: offer.location.latitude,
      role: "STAY",
      protected: false,
      current: false,
      disrupted: false,
    }));
  }, [data]);

  /* ORD-0417: the stop the shared selection currently names, so the glass can say which one a finger
     just landed on.  Read off the same two values the selection effect below reads (`points` and
     `selectedActivityId`), because the map does not own a selection -- ORD-0390.  A stay offer has no
     activity id, so it selects nothing and this stays null rather than inventing a name to paint. */
  const selectedPoint = useMemo(
    () => points.find((point) => point.id && point.id === selectedActivityId) ?? null,
    [points, selectedActivityId],
  );

  useEffect(() => {
    let disposed = false;
    let map: import("maplibre-gl").Map | null = null;
    let resizeObserver: ResizeObserver | null = null;
    void import("maplibre-gl").then(({ default: maplibregl }) => {
      if (disposed || !host.current) return;
      const coordinates = route
        .flatMap((feature) => feature.geometry.coordinates)
        .filter((value) => value.length >= 2);
      const center: [number, number] | null =
        (coordinates[Math.floor(coordinates.length / 2)] as [number, number] | undefined) ??
        (points[0] ? [points[0].longitude, points[0].latitude] : null);
      if (!center) {
        // No route geometry and no located activity: an honest empty state is
        // the correct answer, not another city's map.
        setNoLocation(true);
        onNoLocationChange?.(true);
        return;
      }
      setNoLocation(false);
      onNoLocationChange?.(false);
      /* Read the app's own palette out of the document rather than inventing a
         second set of literals inside the map style: the route lines then follow
         the light/dark binding the rest of the product already uses. */
      const css = getComputedStyle(document.documentElement);
      const token = (name: string, fallback: string) =>
        css.getPropertyValue(name).trim() || fallback;
      const routePalette: Record<string, string> = {
        WALK: token("--cn-accent", "#0a66ff"),
        TRANSIT: token("--cn-success", "#248a3d"),
        AUTO: token("--cn-warning", "#b06000"),
        TRAIN: token("--v8-violet", "#8063d7"),
      };
      const routeFallbackColour = token("--cn-accent-text", "#0a5cd6");
      const casingColour = token("--v8-panel", "#ffffff");
      const modesPresent = Array.from(new Set(route.map((feature) => String(feature.properties?.mode ?? ""))));
      const lineCases: unknown[] = ["case"];
      for (const mode of modesPresent) {
        lineCases.push(["==", ["get", "mode"], mode], routePalette[mode] ?? routeFallbackColour);
      }
      lineCases.push(routeFallbackColour);
      const lineColour = lineCases.length > 2 ? lineCases : routeFallbackColour;
      const mapInstance = new maplibregl.Map({
        /* Never leave the traveller staring at an empty box when the tile
           server is unreachable (common on restricted networks). The Apple
           branch's GeoJSON typing fix dropped this line along with the comment
           that explained it; maplibre's own default happens to be false, but an
           accessibility-relevant intent belongs in the source, not in an
           implicit library default. maplibre's shipped MapOptions type omits
           this documented constructor flag, hence the narrow cast at the end of
           the options object. */
        failIfMajorPerformanceCaveat: false,
        container: host.current,
      style: {
        version: 8,
        sources: {
          "open-street-map": {
            type: "raster",
            tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            tileSize: 256,
            attribution: t("mapComp.attribution"),
          },
          // A flat canvas behind the raster tiles means a blocked or offline
          // tile server degrades to a readable route view instead of a blank
          // white rectangle.
          "base-canvas": {
            type: "geojson",
            data: { type: "FeatureCollection", features: [] },
          },
          "journey-provider-route": {
            type: "geojson",
            data: {
              type: "FeatureCollection",
              features: route,
            } as FeatureCollection,
          },
        },
        layers: [
          {
            id: "canvas",
            type: "background",
            paint: { "background-color": "#dfeae8" },
          },
          {
            id: "open-street-map",
            type: "raster",
            source: "open-street-map",
            paint: { "raster-opacity": 0.82 },
          },
          {
            /* A casing underneath so the line stays readable on a pale raster the
               same way a road label keeps its outline. */
            id: "journey-provider-route-casing",
            type: "line",
            source: "journey-provider-route",
            paint: {
              "line-color": casingColour,
              "line-width": [
                "interpolate", ["exponential", 1.3], ["zoom"],
                8, 3,
                12, 6,
                16, 11,
              ],
              "line-opacity": 0.95,
            },
          },
          {
            /* Was a single #4658d9 stroke at a fixed 4px for every leg, which is
               what read as "one big line". Colour now follows the travel mode and
               width follows the zoom level. */
            id: "journey-provider-route",
            type: "line",
            source: "journey-provider-route",
            paint: {
              "line-color": lineColour as unknown as string,
              "line-width": [
                "interpolate", ["exponential", 1.3], ["zoom"],
                8, 1.5,
                12, 3.5,
                16, 7,
              ],
              "line-opacity": 0.9,
            },
          },
        ],
      },
      center: center as [number, number],
      zoom: 11,
      attributionControl: false,
      /* `interactive: false` here switched off drag, zoom, rotate and pitch at once,
         which is why the panel behaved like a static image.  Rotation and pitch stay off
         because a tilted, spun city view is not readable on a phone and nothing in this
         product depends on it.

         ORD-0134 ②: `cooperativeGestures: true` is what made the panel dead under a finger.
         It asks for two fingers to pan, and on the phones we tested one finger then does
         neither thing -- the map refuses the drag and the page above it does not take it
         either (measured: twelve of twelve one-finger swipes starting on the canvas moved
         the page 0px, and the camera with it).  A traveller's words were "滑不动，用力滑就滑到
         旁边的面板".  Single-finger drag therefore belongs to the map again, and the page is
         scrolled from the page -- which is the ownership rule the new gate measures both ways. */
      interactive: true,
      dragRotate: false,
      pitchWithRotate: false,
      touchPitch: false,
      maxPitch: 0,
      } as maplibregl.MapOptions & { failIfMajorPerformanceCaveat?: boolean });
      /* The flat canvas behind the raster tiles is the right degradation, but it
         must not be a *silent* one: with every tile blocked the panel looked like a
         picture of a map rather than a map that failed to load its basemap. */
      mapInstance.on("error", (event) => {
        const source = (event as { sourceId?: string }).sourceId;
        if (source === "open-street-map") setTilesUnavailable(true);
      });
      mapInstance.on("load", () => {
        const raster = mapInstance.getSource("open-street-map") as
          | { tiles?: { has: (v: unknown) => boolean; size?: number }[]; cache?: { has: (v: unknown) => boolean } }
          | undefined;
        const loaded = raster?.tiles?.some((bucket) => typeof bucket?.has === "function" && bucket.size !== 0);
        if (loaded === false) setTilesUnavailable(true);
      });
      map = mapInstance;
      mapRef.current = mapInstance;
      /* Test seam.  maplibre keeps its instance off the DOM, but the acceptance
         gate has to read the camera to prove a gesture actually moved it — a
         rendered-frame diff alone cannot say *why* nothing changed.  Exposing the
         instance on the container is the smallest surface that does it, and nothing
         in the app reads it back. */
      (host.current as (HTMLElement & { citynexusMap?: unknown })).citynexusMap = mapInstance;
      resizeObserver = new ResizeObserver(() => map?.resize());
      resizeObserver.observe(host.current);
    markerEls.current = [];
    points.forEach((point, index) => {
      const marker = document.createElement("div");
      marker.className = `v9-map-marker ${point.role === "STAY" ? "stay" : ""} ${point.protected ? "protected" : ""} ${point.current ? "current" : ""} ${point.disrupted ? "disrupted" : ""}`;
      marker.textContent = String(index + 1);
      marker.setAttribute("aria-label", point.title);
      /* ORD-0390: a marker used to be a painted dot — `aria-label` for a screen reader and nothing
         else.  It is now a real button that selects the stop it stands for, and `aria-pressed`
         carries the shared selection so the state is readable without a class-name convention.
         Keyboard is wired alongside pointer because a map-only affordance is unreachable on a
         keyboard; the hit area is the marker box itself, which the narrow-width gate measures. */
      marker.setAttribute("role", "button");
      marker.tabIndex = 0;
      marker.setAttribute("aria-pressed", String(!!point.id && point.id === selectedActivityId));
      const choose = () => selectRef.current?.(point.id ?? null);
      marker.addEventListener("click", choose);
      marker.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          choose();
        }
      });
      markerEls.current.push({ el: marker, id: point.id });
        new maplibregl.Marker({ element: marker })
        .setLngLat([point.longitude, point.latitude])
        .addTo(mapInstance);
    });
    if (data?.active_plan?.user_location) {
      const user = document.createElement("div");
      user.className = "v9-map-marker user";
      user.textContent = "●";
      user.setAttribute(
        "aria-label",
        t("mapComp.yourLocation", { n: Math.round(data.active_plan.user_location.accuracy_m) }),
      );
      new maplibregl.Marker({ element: user })
        .setLngLat([
          data.active_plan.user_location.longitude,
          data.active_plan.user_location.latitude,
        ])
        .addTo(mapInstance);
    }
    /* ORD-0410: what the journey's view is framed on — the route geometry when there is any, and
       otherwise the stops this panel just painted markers for.  Gating this on route coordinates
       alone made the whole block conditional on a journey having leg geometry, so a journey with
       located stops and no legs never got a `refitRef.current` at all: the named control rendered,
       took the press, called `refitRef.current?.()` into a null, and did nothing without an
       exception.  The fallback is the same set of positions the traveller can see, which keeps the
       one-definition rule below intact instead of inventing a second "journey's view" for the
       button. */
    const framePositions: number[][] =
      coordinates.length > 1
        ? coordinates
        : points.map((point) => [point.longitude, point.latitude]);
    if (framePositions.length > 0) {
      const bounds = framePositions
        .slice(1)
        .reduce(
          (value, coordinate) => value.extend(coordinate as [number, number]),
          new maplibregl.LngLatBounds(
            framePositions[0] as [number, number],
            framePositions[0] as [number, number],
          ),
        );
      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      /* The route-context caption floats over the bottom of the canvas, so a
         uniform 42px padding let the last marker sit underneath it (pass-9
         blocker #1). The fix tells the camera how much of its own viewport the
         overlay occupies, rather than raising the marker's z-index so the two
         elements take turns bullying each other.
         ORD-A: that overlay is now the whole stack (bubble + caption + notice), so the camera is
         told about all of it — measuring only the caption would re-open the hole the comment above
         describes, one element at a time. */
      const overlay = host.current?.parentElement?.querySelector(
        ".v8-map-overlay-stack, .v8-map-caption",
      ) as HTMLElement | null;
      const overlayHeight = overlay?.getBoundingClientRect().height ?? 0;
      const side = 42;
      const safetyGap = 16;
      /* The same call the reset button re-runs, kept as a closure so "back to the journey's view"
         reframes exactly what the first fit framed — same padding, same maxZoom — instead of
         inventing a second definition of the journey's view somewhere else. */
      const fitToJourney = () =>
        mapInstance.fitBounds(bounds, {
          padding: {
            top: side,
            right: side,
            bottom: side + overlayHeight + safetyGap,
            left: side,
          },
          maxZoom: 13,
          duration: reducedMotion ? 0 : 650,
        });
      refitRef.current = () => {
        refitPending = true;
        fitToJourney();
      };
      /* The initial framing is this same call — it was lost once when the direct `fitBounds` was
         turned into a closure, which is how a map can come up centred on a midpoint nobody asked
         for. Calling it here also seeds the target below on its first moveend. */
      fitToJourney();
    }
    /* Distance is measured in projected pixels, not degrees, so the tolerance means the same
       physical thing at every zoom level and every latitude. */
    /* Declared out here because both the framing block above and this listener need it: a fit we
       asked for is what redefines the target, and the flag has to survive the block that ran it. */
    let refitPending = true;
    const TOLERANCE_PX = 24;
    const syncViewportState = () => {
      /* A fit we asked for defines what "on target" means, so the target is recorded from the
         camera once that fit settles. `bounds.getCenter()` would be wrong here: the fit is given
         extra bottom padding for the overlay stack, so the camera deliberately settles north of
         the geometric centre, and a correctly framed journey would have been reported as lost. */
      if (refitPending) {
        refitPending = false;
        const settled = mapInstance.getCenter();
        targetRef.current = { lng: settled.lng, lat: settled.lat };
        setViewportState("on-target");
        return;
      }
      const target = targetRef.current;
      if (!target) return;
      const want = mapInstance.project([target.lng, target.lat]);
      const have = mapInstance.project([mapInstance.getCenter().lng, mapInstance.getCenter().lat]);
      const next: ViewportState =
        Math.hypot(want.x - have.x, want.y - have.y) > TOLERANCE_PX ? "off-target" : "on-target";
      setViewportState((prev) => (prev === next ? prev : next));
    };
    mapInstance.on("moveend", syncViewportState);
    mapInstance.on("zoomend", syncViewportState);
    });
    return () => {
      disposed = true;
      resizeObserver?.disconnect();
      map?.remove();
      mapRef.current = null;
      markerEls.current = [];
    };
  }, [data, points, route]);

  /* ORD-0390, the reverse half of the link: a selection made anywhere else (the trips card's
     「在地图上显示」, or a sibling stop in the detail sheet) has to move this camera.  It is a
     separate effect on purpose — the map-building effect above keys on data/points/route, and
     rebuilding to pan would throw away the zoom the traveller just set.  `easeTo` rather than
     `fitBounds` because the question is "where is this stop", not "reframe the whole plan". */
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const chosen = points.find((point) => point.id && point.id === selectedActivityId) ?? null;
    for (const marker of markerEls.current) {
      const on = !!chosen && marker.id === chosen.id;
      marker.el.classList.toggle("selected", on);
      marker.el.setAttribute("aria-pressed", String(on));
    }
    if (!chosen) return;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    targetRef.current = { lng: chosen.longitude, lat: chosen.latitude };
    map.easeTo({
      center: [chosen.longitude, chosen.latitude],
      duration: reducedMotion ? 0 : 450,
    });
  }, [selectedActivityId, points]);
  if (noLocation) {
    return (
      <div
        className="v8-map-panel v8-map-empty"
        data-testid="v8-map-empty"
        /* ORD-0407 ①: the tier this cell is in, named the way ORD-0359 names a tier -- the sentence
           carries the words, the attribute carries the code. NOT_AVAILABLE is measured, not asserted:
           see the note on `noLocationBody` in i18n/mapPanelCopy.ts. */
        data-cnx-activity-entry="NOT_AVAILABLE"
        role="status"
        aria-live="polite"
      >
        <div className="v8-map-empty-body">
          <b>{mapCopy(locale, "noLocationTitle")}</b>
          <p>{mapCopy(locale, "noLocationBody")}</p>
        </div>
      </div>
    );
  }

  return (
    <div
      className="v8-map-panel"
      data-testid="v8-maplibre-panel"
      data-route-legs={route.length}
      data-route-modes={routeModes.join(" ")}
      aria-label={t("mapComp.journeyMap")}
    >
      <div ref={host} className="v8-maplibre-canvas" data-testid="v8-maplibre-canvas" />
      <MapViewportControls
        state={viewportState}
        onResetView={() => refitRef.current?.()}
        labels={{
          resetToJourney: mapCopy(locale, "viewportResetToJourney"),
          onTarget: mapCopy(locale, "viewportOnTarget"),
          offTarget: mapCopy(locale, "viewportOffTarget"),
        }}
      />
      {tilesUnavailable && (
        /* Requirement 3 of ORD-0037: the flat canvas behind the raster tiles is the
           right degradation, but saying nothing is what made the panel read as a
           picture someone had drawn rather than a map that failed to fetch one. */
          <p className="v8-map-tile-notice" data-testid="v8-map-tile-notice" role="status">
            {mapCopy(locale, "tileNotice")}
          </p>
        )}
      {/* ORD-A: the caption and the gesture bubble used to be positioned independently
          (`.v8-map-caption` bottom:12px, `.v8-map-gesture-hint` bottom:calc(40px + safe-area)), which
          was fine while the caption was one line. Adding the basemap notice made it two, and the
          bubble then painted over the notice at every width from 320 to 414 — measured, not guessed
          (2,513–3,316 px² of overlap). One bottom-anchored stack removes the whole class of bug
          instead of nudging one offset. */}
      <div className="v8-map-overlay-stack">
        <p className="v8-map-gesture-hint" data-testid="v8-map-gesture-hint">
          {mapCopy(locale, "gestureHint")}
        </p>
        <div className="v8-map-caption">
          {/* ORD-0417: the answer to a tap on a pin, painted on the glass the tap happened on.  The
              name used to live only in the pin's `aria-label`, so a traveller whose eyes work got a
              number and no name; the card that does name the stop is below the panel, and reaching it
              means the page scrolling away from the surface the finger is on.
              It goes inside the caption rather than as another block in the stack, and that placement
              is the load-bearing part of this change.  The stack is the box `fitBounds` measures its
              bottom padding against -- once, with nothing selected, paying `side + overlayHeight +
              safetyGap`.  A line added to the stack, or permanently reserved there, raises the band's
              top edge into ground the framing was promised: this lane's gate measured both, a
              conditional stack line covering three pins at 139->184 px and a reserved one still
              covering a pin at 390x844 that the untouched tip leaves clear.  Text laid out in the line
              box the caption already owns is the form that costs no band height; the `geometry-*` arms
              of `e2e/qoder-ord0417-tap-names-the-stop.spec.ts` measure it rather than assume it. */}
          {selectedPoint && (
            <span className="v8-map-selected-stop" data-testid="v8-map-selected-stop">
              {t("mapComp.selectedStopOnMap", { name: selectedPoint.title })}
            </span>
          )}
          <span className="status-dot" />
          {t("mapComp.routeContext")} <b>{t("mapComp.legs", { n: route.length })}</b>
          {/* ORD-0418: "供应商支持的路线" is a claim about where the geometry came from, so it may
              only be spoken -- and read aloud, this span is `v8-sr-only` -- when there is geometry
              at all. With zero legs the server holds no route, and saying "provider-backed" anyway
              handed the traveller a promise no request had earned. */}
          {route.length > 0 ? (
            <span className="v8-sr-only">{t("mapComp.providerRoute")}</span>
          ) : noRoutingProvider ? (
            <span className="v8-sr-only">{t("mapComp.noRouteNoProvider")}</span>
          ) : (
            <span className="v8-sr-only">{t("mapComp.noRouteNotRouted")}</span>
          )}
          {/* ORD-A: the basemap is fetched from tile.openstreetmap.org, so the notice belongs on the
              glass. `attributionControl:false` above means maplibre will not draw it, and the
              source's own `attribution` field only reaches the style document — invisible to a
              traveller. It inherits the caption's colour, so light/dark needs no rule here. */}
          <span className="v8-map-attribution" data-testid="v8-map-attribution">
            {mapCopy(locale, "attributionRaster")}
          </span>
        </div>
      </div>
    </div>
  );
}
