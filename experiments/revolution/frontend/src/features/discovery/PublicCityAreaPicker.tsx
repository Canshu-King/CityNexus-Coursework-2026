import { useEffect, useRef, useState } from "react";
import "maplibre-gl/dist/maplibre-gl.css";
import "./public-city-area-picker.css";

export type PublicCityCenter = {
  latitude: number;
  longitude: number;
  provenance: "MAP_TAP" | "OSM_MAP_LINK";
};

export function PublicCityAreaPicker({
  selectedCenter,
  focusCenter,
  onSelect,
}: {
  selectedCenter: PublicCityCenter | null;
  focusCenter?: { latitude: number; longitude: number } | null;
  onSelect: (center: PublicCityCenter) => void;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<import("maplibre-gl").Map | null>(null);
  const markerRef = useRef<import("maplibre-gl").Marker | null>(null);
  const pendingMarkerRef = useRef<[number, number] | null>(null);
  const mapReadyRef = useRef(false);
  const selectRef = useRef(onSelect);
  const [tilesUnavailable, setTilesUnavailable] = useState(false);
  selectRef.current = onSelect;

  useEffect(() => {
    let disposed = false;
    let map: import("maplibre-gl").Map | null = null;
    let resizeObserver: ResizeObserver | null = null;
    void import("maplibre-gl").then(({ default: maplibregl }) => {
      if (disposed || !hostRef.current) return;
      const center: [number, number] = selectedCenter
        ? [selectedCenter.longitude, selectedCenter.latitude]
        : focusCenter
          ? [focusCenter.longitude, focusCenter.latitude]
          : [0, 0];
      const mapInstance = new maplibregl.Map({
        container: hostRef.current,
        style: {
          version: 8,
          sources: {
            "public-osm": {
              type: "raster",
              tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
              tileSize: 256,
              attribution: "© OpenStreetMap contributors",
            },
          },
          layers: [
            { id: "public-base", type: "background", paint: { "background-color": "#dfeae8" } },
            { id: "public-osm", type: "raster", source: "public-osm", paint: { "raster-opacity": 0.86 } },
          ],
        },
        center,
        zoom: selectedCenter || focusCenter ? 11 : 1.5,
        attributionControl: false,
        interactive: true,
        dragRotate: false,
        pitchWithRotate: false,
        touchPitch: false,
        maxPitch: 0,
      } as import("maplibre-gl").MapOptions);
      mapInstance.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      const setMarker = (latitude: number, longitude: number) => {
        if (!mapReadyRef.current) {
          pendingMarkerRef.current = [longitude, latitude];
          return;
        }
        if (!markerRef.current) {
          markerRef.current = new maplibregl.Marker({ color: "#0a66ff" }).addTo(mapInstance);
        }
        markerRef.current.setLngLat([longitude, latitude]);
      };
      if (selectedCenter) setMarker(selectedCenter.latitude, selectedCenter.longitude);
      mapInstance.on("click", (event) => {
        const latitude = Number(event.lngLat.lat.toFixed(6));
        const longitude = Number(event.lngLat.lng.toFixed(6));
        setMarker(latitude, longitude);
        selectRef.current({ latitude, longitude, provenance: "MAP_TAP" });
      });
      mapInstance.on("error", (event) => {
        if ((event as { sourceId?: string }).sourceId === "public-osm") setTilesUnavailable(true);
      });
      mapInstance.on("load", () => {
        if (pendingMarkerRef.current) {
          // The style load event can precede MapLibre's first render frame. The
          // marker is installed from the idle hook below once projection state
          // is ready; the pending coordinate remains local until then.
        }
        const source = mapInstance.getSource("public-osm") as { tiles?: unknown[] } | undefined;
        if (source && Array.isArray(source.tiles) && source.tiles.length === 0) setTilesUnavailable(true);
      });
      mapInstance.on("idle", () => {
        mapReadyRef.current = true;
        if (pendingMarkerRef.current) {
          const [longitude, latitude] = pendingMarkerRef.current;
          pendingMarkerRef.current = null;
          setMarker(latitude, longitude);
        }
      });
      map = mapInstance;
      mapRef.current = mapInstance;
      (hostRef.current as HTMLDivElement & { citynexusMap?: unknown }).citynexusMap = mapInstance;
      resizeObserver = new ResizeObserver(() => map?.resize());
      resizeObserver.observe(hostRef.current);
    });
    return () => {
      disposed = true;
      resizeObserver?.disconnect();
      markerRef.current?.remove();
      markerRef.current = null;
      pendingMarkerRef.current = null;
      mapReadyRef.current = false;
      map?.remove();
      mapRef.current = null;
    };
    // The map is deliberately mounted only after tile disclosure consent.  Its camera follows
    // the selected pin in the separate effect below, so changing the selection never reloads tiles.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const center = selectedCenter
      ? [selectedCenter.longitude, selectedCenter.latitude] as [number, number]
      : focusCenter
        ? [focusCenter.longitude, focusCenter.latitude] as [number, number]
        : null;
    if (!center) return;
    map.easeTo({ center, zoom: Math.max(map.getZoom(), selectedCenter ? 13 : 11), duration: 0 });
    if (selectedCenter && markerRef.current) markerRef.current.setLngLat(center);
  }, [selectedCenter?.latitude, selectedCenter?.longitude, focusCenter?.latitude, focusCenter?.longitude]);

  return (
    <div className="public-city-area-picker" data-testid="bootstrap-public-map" aria-label="Choose public search area">
      <div ref={hostRef} className="public-city-area-map" data-testid="bootstrap-public-map-canvas" />
      <p className="public-city-area-attribution">Map tiles: © OpenStreetMap contributors · tiles.openstreetmap.org</p>
      {tilesUnavailable ? (
        <p className="public-city-area-notice" role="status" data-testid="bootstrap-public-map-unavailable">
          Map tiles are unavailable here. You can still use the strict OpenStreetMap link or WGS84 fields below.
        </p>
      ) : null}
      <p className="public-city-area-hint" data-testid="bootstrap-public-map-hint">
        Tap or click once to place one public search pin. Pan and zoom first if needed; this pin is not your location or route origin.
      </p>
    </div>
  );
}
