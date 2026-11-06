/* ORD-0510 -- the way into the container that ORD-0444 built.

What this face is: one control on a trip card / a day row that puts *that stop's place* onto one of
the traveller's own shelves, through the doors the server already has
(`GET /api/v1/journeys/...` is not involved; the read is `/api/v1/collections?traveler_id=`, the write
is `POST /api/v1/collections/{folder_id}/items`).  No new write endpoint, no client-side shelf, and
nothing is remembered because a button was pressed -- the line that says where a place ended up is
drawn from a fresh read of the same list door the picker used (the rule ORD-0476 set for 打卡).

What it refuses to do:
  - it does not create a shelf on the traveller's behalf.  A traveller with no shelf is told there is
    nowhere to put it, which is the truth, rather than being handed one they never asked for;
  - it does not name a failure with the server's own words.  Each of the door's answers gets a
    sentence from `collectionCopy.ts`, chosen by state name; the reason code is read only to tell the
    two 404s apart and never reaches the DOM, and no uuid is interpolated into any line;
  - it does not claim a place is stored because the POST answered.  After a write the shelves are read
    again and only the names that answer carries are shown.
*/

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import {
  addPlaceToCollection,
  copyFor,
  readCollections,
  refusalCopy,
  shelvesHolding,
  type AddOutcome,
  type CollectionShelf,
  type ReadOutcome,
} from "./collections";

type Props = {
  /** Whose shelves the read door is asked about.  Absent means nothing is asked, not that all are. */
  travelerId: string | null;
  /** The place this row stands for (`places.id`).  Absent means the row cannot be collected. */
  placeId: string | null;
  /** The stop's own title, used only for the accessible name of the control. */
  placeTitle: string;
};

export function CollectInto({ travelerId, placeId, placeTitle }: Props) {
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const [open, setOpen] = useState(false);
  const [listing, setListing] = useState<ReadOutcome | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [notice, setNotice] = useState<AddOutcome | null>(null);
  const [after, setAfter] = useState<ReadOutcome | null>(null);
  const root = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    setListing(null);
    const answer = await readCollections(travelerId);
    setListing(answer);
  }, [travelerId]);

  useEffect(() => {
    if (!open) return;
    void load();
  }, [open, load]);

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  const collect = async (shelfId: string) => {
    if (!placeId) return;
    setPending(shelfId);
    setNotice(null);
    const answer = await addPlaceToCollection(shelfId, placeId);
    setPending(null);
    setNotice(answer);
    /* The write's own answer is not the evidence.  Re-read the list door and say where the place is
       according to that read; an empty read after a 201 is reported as such rather than smoothed over. */
    if (answer.kind === "added" || answer.kind === "already_there") {
      setAfter(await readCollections(travelerId));
    }
  };

  const held = after ? shelvesHolding(after, placeId ?? "") : [];
  const heldNames = held.map((shelf) => shelf.name);
  const alreadyOn =
    listing && listing.kind === "loaded"
      ? new Set(
          listing.shelves
            .filter((shelf: CollectionShelf) =>
              shelf.items.some((item) => item.place_id === placeId),
            )
            .map((shelf) => shelf.id),
        )
      : new Set<string>();

  const stateLine = () => {
    if (!notice) return null;
    if (notice.kind === "added" || notice.kind === "already_there") {
      if (after === null) return copyFor("pickerLoading", zh);
      if (after.kind !== "loaded") return copyFor("writeUnreachable", zh);
      const where = heldNames.length
        ? `${copyFor("placedInto", zh)} ${heldNames.join("、")}`
        : copyFor("placedNowhere", zh);
      /* The door answers 409 with a name, and "it is on that shelf" and "I just put it there" are two
         things the traveller asked differently.  Both end in the same read-back line, because neither
         is allowed to claim a location the read did not return. */
      return notice.kind === "already_there"
        ? `${copyFor("alreadyHere", zh)} · ${where}`
        : where;
    }
    if (notice.kind === "no_folder") return copyFor("noFolder", zh);
    if (notice.kind === "no_place") return copyFor("noPlace", zh);
    if (notice.kind === "refused") return copyFor("writeRefused", zh);
    return copyFor("writeUnreachable", zh);
  };

  return (
    <div className="cn-collect" ref={root} data-testid="collect-into">
      <button
        type="button"
        className="cn-collect__entry"
        data-testid="collect-entry"
        disabled={!placeId}
        aria-expanded={open}
        aria-label={`${copyFor("entry", zh)}: ${placeTitle}`}
        onClick={() => {
          setNotice(null);
          setAfter(null);
          setOpen((v) => !v);
        }}
      >
        {copyFor("entry", zh)}
      </button>
      {!placeId ? (
        <p className="cn-collect__state" data-testid="collect-no-place-id">
          {copyFor("noPlaceIdOnRow", zh)}
        </p>
      ) : null}
      {open ? (
        <div className="cn-collect__picker" role="group" data-testid="collect-picker">
          <p className="cn-collect__picker-title">{copyFor("pickerTitle", zh)}</p>
          {!travelerId ? (
            <p className="cn-collect__state" data-testid="collect-no-traveler">
              {copyFor("noTravelerYet", zh)}
            </p>
          ) : !listing ? (
            <p className="cn-collect__state">{copyFor("pickerLoading", zh)}</p>
          ) : listing.kind !== "loaded" ? (
            <p className="cn-collect__state" data-testid="collect-list-failure">
              {listing.kind === "refused"
                ? refusalCopy(zh)
                : copyFor("readUnreachable", zh)}
            </p>
          ) : listing.shelves.length === 0 ? (
            <p className="cn-collect__state" data-testid="collect-no-shelf">
              {copyFor("noShelfToPick", zh)}
            </p>
          ) : (
            <ul className="cn-collect__shelves">
              {listing.shelves.map((shelf) => (
                <li key={shelf.id}>
                  <button
                    type="button"
                    className="cn-collect__shelf"
                    data-testid="collect-shelf"
                    data-already-held={alreadyOn.has(shelf.id) ? "true" : "false"}
                    disabled={pending !== null}
                    onClick={() => void collect(shelf.id)}
                  >
                    <span className="cn-collect__shelf-name">{shelf.name}</span>
                    <span className="cn-collect__shelf-count">
                      {shelf.item_count} {copyFor("places", zh)}
                    </span>
                    {alreadyOn.has(shelf.id) ? (
                      <span className="cn-collect__held" data-testid="collect-held-mark">
                        {copyFor("alreadyHere", zh)}
                      </span>
                    ) : null}
                  </button>
                </li>
              ))}
            </ul>
          )}
          <button
            type="button"
            className="cn-collect__dismiss"
            data-testid="collect-dismiss"
            onClick={() => setOpen(false)}
          >
            {copyFor("dismiss", zh)}
          </button>
        </div>
      ) : null}
      {stateLine() ? (
        <p className="cn-collect__state" data-testid="collect-result" role="status">
          {stateLine()}
        </p>
      ) : null}
    </div>
  );
}
