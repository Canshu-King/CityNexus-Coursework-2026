/**
 * Copy for the place-provenance pair (ORD-B).
 *
 * Kept out of `i18n/LocaleContext.tsx` on purpose: two other lanes are editing that
 * dictionary tonight, and a placeholder word is the smallest possible unit of copy.
 *
 * The rule these strings exist to serve: a field never disappears. When the value is
 * not there, what shows instead names *which* fact is missing -- it is not a generic
 * "unavailable", not an empty string, and never a guessed provider or coordinate
 * system. And the two absences are kept apart: `no*` words are for a cell the server
 * answered with "none recorded", `notSent` is for a cell this payload never carried.
 */

export type PlaceProvenanceCopy = {
  detailsLabel: string;
  name: string;
  source: string;
  coordinateSystem: string;
  sourceCoordinateSystem: string;
  host: string;
  notSent: string;
  noName: string;
  noSource: string;
  noCoordinateSystem: string;
  noSourceCoordinateSystem: string;
  noHost: string;
};

export const placeProvenanceCopy: Record<"en" | "zhCN", PlaceProvenanceCopy> = {
  en: {
    detailsLabel: "Source and coordinate system",
    name: "Place",
    source: "Source",
    coordinateSystem: "Stored in",
    sourceCoordinateSystem: "Handed over in",
    host: "Source host",
    notSent: "the server did not send this cell this time",
    noName: "the server recorded no name",
    noSource: "source not recorded",
    noCoordinateSystem: "coordinate system not recorded",
    noSourceCoordinateSystem: "original system not recorded at capture",
    noHost: "not taken from a web address",
  },
  zhCN: {
    detailsLabel: "来源与坐标系",
    name: "地点",
    source: "来源",
    coordinateSystem: "存储坐标系",
    sourceCoordinateSystem: "原始坐标系",
    host: "来源主机",
    notSent: "这一格服务器这次没有给",
    noName: "服务器没记录名称",
    noSource: "未记录来源",
    noCoordinateSystem: "未记录坐标系",
    noSourceCoordinateSystem: "采集时未记录原始坐标系",
    noHost: "不是从网址采来的",
  },
};

export function provenanceCopy(locale: string): PlaceProvenanceCopy {
  return locale.startsWith("zh") ? placeProvenanceCopy.zhCN : placeProvenanceCopy.en;
}
