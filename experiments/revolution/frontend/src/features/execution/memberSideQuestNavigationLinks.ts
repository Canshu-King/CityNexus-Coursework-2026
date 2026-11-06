import type { MemberSideQuestNavigationTarget } from "../../api/citynexus";

type ExactMapPin = Pick<MemberSideQuestNavigationTarget, "latitude" | "longitude" | "coordinate_system">;

function exactWgs84Coordinates(target: ExactMapPin): [number, number] {
  const { latitude, longitude } = target;
  if (
    target.coordinate_system.trim().toUpperCase() !== "WGS84"
    || typeof latitude !== "number"
    || typeof longitude !== "number"
    || !Number.isFinite(latitude)
    || !Number.isFinite(longitude)
    || latitude < -90
    || latitude > 90
    || longitude < -180
    || longitude > 180
  ) {
    throw new Error("An exact WGS84 map pin is unavailable.");
  }
  return [latitude, longitude];
}

export function buildGoogleWalkingDirectionsUrl(target: MemberSideQuestNavigationTarget): string {
  const [latitude, longitude] = exactWgs84Coordinates(target);
  const url = new URL("https://www.google.com/maps/dir/");
  url.searchParams.set("api", "1");
  // The documented Maps URL contract supports exact destination coordinates,
  // walking mode and dir_action=navigate. The previously used
  // "destination_label" is not a documented directions parameter; do not
  // rely on it to identify an OSM-backed Place in Google Maps.
  // Origin remains omitted: Maps asks for device location under its own consent.
  url.searchParams.set("destination", `${latitude},${longitude}`);
  url.searchParams.set("travelmode", "walking");
  url.searchParams.set("dir_action", "navigate");
  return url.toString();
}

export function buildOpenStreetMapPinUrl(target: ExactMapPin): string {
  const [latitude, longitude] = exactWgs84Coordinates(target);
  const zoom = 18;
  const url = new URL("https://www.openstreetmap.org/");
  url.searchParams.set("mlat", String(latitude));
  url.searchParams.set("mlon", String(longitude));
  url.searchParams.set("zoom", String(zoom));
  url.hash = `map=${zoom}/${latitude}/${longitude}`;
  return url.toString();
}
