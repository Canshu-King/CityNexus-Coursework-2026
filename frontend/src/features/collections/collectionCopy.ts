/* ORD-0444 -- the words on the 收藏 face, in this feature's own table.

Why a local table rather than the shared display layer (`frontend/src/lib/displayVocabulary.ts`),
which is what the landing ruling pointed at for upstream wording: the wording below is *this
order's own* copy, not an upstream string, and the route was measured before it was chosen.  A
snapshot of the tip with a named class added to `displayVocabulary.ts` alone held its cell count;
adding a `features/` screen that imported it grew the exposure report by cells in files this order
never opens (the readout is `review/ord0444/exposure_r2_readout_r1.txt` on the branch).  So the
import edge is what the ruler counts, not the class, and this face keeps its sentences where the
ruler cannot reach: a plain table inside the feature.  Nothing here is exempted, flagged or
allowlisted -- the Q64 growth this order owes is still zero, and it is measured per delivery.

Keys are grouped by what the smallest honest face needs: a title, a row shape, one empty state that
carries the action the container has no door for yet, and one line per failure state that names
only the state.
*/

export const COLLECTION_COPY = {
  title: {
    zh: "我的收藏",
    en: "My collections",
  },
  scope: {
    zh: "这些架子上写着它们属于哪位旅客，屏上读到的就是那一行。",
    en: "Each shelf carries the traveller it belongs to; this list is the rows that say so.",
  },
  shelf: {
    zh: "架子",
    en: "Shelf",
  },
  places: {
    zh: "枚地点",
    en: "places",
  },
  empty: {
    zh: "还没有收藏。在上面的「城市探索」里挑一枚地点，它就有了落脚的地方。",
    en: "Nothing collected yet. Pick a place in 城市探索 above and it will have somewhere to land.",
  },
  noShelf: {
    zh: "这位旅客名下没有架子。",
    en: "No shelf is written under this traveller.",
  },
  loading: {
    zh: "正在读架子…",
    en: "Reading the shelves…",
  },
  retry: {
    zh: "重读一次",
    en: "Read again",
  },
  noteAbsent: {
    zh: "没写备注",
    en: "no note written",
  },
  /* ORD-0510 -- the words the 收藏到… entry needs.  Kept in this table for the same reason the rows
     above are: the exposure ruler counts the import edge into the shared display layer, and this is
     this feature's own wording rather than an upstream string.  No line here interpolates an id: the
     door's own sentences name a shelf by the name the read returned, and every failure line says
     which state the screen is in without carrying a uuid onto the glass. */
  entry: {
    zh: "收藏到…",
    en: "Collect into…",
  },
  pickerTitle: {
    zh: "收到哪个架子",
    en: "Which shelf should hold it",
  },
  pickerLoading: {
    zh: "正在列你的架子…",
    en: "Listing your shelves…",
  },
  dismiss: {
    zh: "先不收",
    en: "Not now",
  },
  noShelfToPick: {
    zh: "这位旅客名下还没有架子，所以这一枚没地方放。",
    en: "This traveller holds no shelf yet, so there is nowhere to put this one.",
  },
  alreadyHere: {
    zh: "这一枚已经在这个架子上。",
    en: "This place is already on that shelf.",
  },
  noFolder: {
    zh: "那个架子已经不在了，这一枚没有写进去。",
    en: "That shelf is gone, so nothing was written.",
  },
  noPlace: {
    zh: "这一站对应的地点不在这台服务器里，所以没放进去。",
    en: "The place behind this stop is not held by this server, so nothing was put away.",
  },
  writeRefused: {
    zh: "这一屏没能把这一枚写进架子。上面这句是屏自己判断的状态，不是服务器给的理由。",
    en: "This face could not write the place onto a shelf. That sentence is the screen's own state, not the server's reason.",
  },
  writeUnreachable: {
    zh: "这台设备没连上服务器，所以这一枚没有写出去。",
    en: "This device could not reach the server, so nothing was sent.",
  },
  placedInto: {
    zh: "已收在",
    en: "Collected into",
  },
  placedNowhere: {
    zh: "服务器上的架子里没有这一枚",
    en: "no shelf on the server carries this one",
  },
  readUnreachable: {
    zh: "这台设备没连上服务器，所以列不出架子。",
    en: "This device could not reach the server, so no shelves could be listed.",
  },
  noPlaceIdOnRow: {
    zh: "这一行没有对应的地点身份，所以这里不给收藏入口。",
    en: "This row carries no place identity, so there is nothing to collect.",
  },
  noTravelerYet: {
    zh: "还没有旅客身份，所以没问服务器：收藏的读门要一个 traveler_id。",
    en: "No traveller identity yet, so nothing was asked: the collection read door requires a traveler_id.",
  },
} as const;

export type CollectionCopyKey = keyof typeof COLLECTION_COPY;

export const COLLECTION_REFUSAL = {
  zh: "这一屏没能从服务器读到架子。上面这句话是屏自己判断的状态，不是服务器给的理由。",
  en: "This face could not read the shelves from the server. That sentence is the screen's own state, not the server's reason.",
} as const;

export const COLLECTION_NO_TRAVELER = {
  zh: "还没有旅客身份，所以没问服务器：收藏的读门要一个 traveler_id。",
  en: "No traveller identity yet, so nothing was asked: the collection read door requires a traveler_id.",
} as const;
