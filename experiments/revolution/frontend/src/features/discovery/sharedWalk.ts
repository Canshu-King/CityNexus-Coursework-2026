export type SharedWalkObject = {
  osm_type: "node" | "way" | "relation";
  osm_id: string;
  source_url: string;
};

export type SharedWalkParseResult =
  | { ok: true; objects: SharedWalkObject[]; message: null; code: null }
  | { ok: false; objects: []; message: string; code: string };

const MAX_SHARED_WALK_TEXT = 12_000;
const URL_TOKEN = /https?:\/\/[^\s<>"']+/gi;
const OBJECT_PATH = /^\/(node|way|relation)\/([1-9][0-9]*)$/;
const SENSITIVE_MARKER =
  /(?:bearer|access[\s_-]*token|refresh[\s_-]*token|authorization|journey[\s_-]*id|member[\s_-]*id|receipt|foreground[\s_-]*gps|\bgps\b|secret|password|cookie|private[\s_-]*(?:token|key|credential))/i;
const CONTROL_OR_HTML = /[\u0000-\u0008\u000b\u000c\u000e-\u001f<>]/;

function failure(code: string, message: string): SharedWalkParseResult {
  return { ok: false, objects: [], code, message };
}

function normalizeObjectUrl(raw: string): SharedWalkObject | null {
  try {
    const url = new URL(raw.trim());
    if (
      url.protocol !== "https:" ||
      !["www.openstreetmap.org", "openstreetmap.org"].includes(url.hostname.toLowerCase()) ||
      url.port !== "" ||
      url.username !== "" ||
      url.password !== "" ||
      url.search !== "" ||
      url.hash !== ""
    ) {
      return null;
    }
    const match = OBJECT_PATH.exec(url.pathname);
    if (!match) return null;
    const [, osm_type, osm_id] = match;
    return {
      osm_type: osm_type as SharedWalkObject["osm_type"],
      osm_id,
      source_url: `https://www.openstreetmap.org/${osm_type}/${osm_id}`,
    };
  } catch {
    return null;
  }
}

export function parseSharedWalkText(value: string): SharedWalkParseResult {
  const text = String(value ?? "");
  if (!text || text.length > MAX_SHARED_WALK_TEXT) {
    return failure("SHARED_TEXT_TOO_LARGE", "Paste a public walk containing exactly two OSM object links.");
  }
  if (CONTROL_OR_HTML.test(text)) {
    return failure("UNSAFE_SHARED_TEXT", "The shared walk contains unsafe control or HTML characters.");
  }
  if (SENSITIVE_MARKER.test(text) || /\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}/.test(text)) {
    return failure("PRIVATE_DATA_IN_SHARED_TEXT", "Private credentials or member data cannot be imported.");
  }
  const tokens = text.match(URL_TOKEN) ?? [];
  if (tokens.length !== 2) {
    return failure("EXACTLY_TWO_OSM_LINKS_REQUIRED", "Paste a public walk containing exactly two OSM object links.");
  }
  const objects: SharedWalkObject[] = [];
  for (const token of tokens) {
    const candidate = token.replace(/[.,;:!?\])}]*$/, "");
    const object = normalizeObjectUrl(candidate);
    if (!object) {
      return failure("INVALID_SHARED_URL", "Shared OSM source links must be canonical HTTPS object links.");
    }
    objects.push(object);
  }
  if (objects[0].osm_type === objects[1].osm_type && objects[0].osm_id === objects[1].osm_id) {
    return failure("DUPLICATE_OSM_OBJECT", "A shared walk requires two distinct public OSM objects.");
  }
  return { ok: true, objects, message: null, code: null };
}
