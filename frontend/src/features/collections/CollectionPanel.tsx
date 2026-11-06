/* ORD-0444 -- the smallest face that proves the collection container can be read.

What this round is, said plainly: ① the container's tables and doors on the server, ② this one face
reading them.  The capture entry, the multiselect exit and the day-drop wiring belong to the next
round, so nothing here pretends to move a place onto a shelf -- there is no add control, and the one
empty sentence points at the block that is actually above it.

What the face is allowed to show, and why:
  - only this order's own copy (`collectionCopy.ts`), chosen by a state name this component picks;
  - shelf names, place names and notes.  The place fields are resolved from the `places` row the
    server holds (`backend/app/services.py` -- `collection_item_schema`), so they are the product's
    own data read back, not an upstream provider's prose;
  - no status code, no reason token, no server message.  An earlier cut of this screen carried the
    refusal's code in a `data-*` slot and as a child of a details face, and the Q64 exposure ruler
    counted both; the ruled route through the shared display table grew cells in files this order
    never opens.  Removing the channel is what makes this order's growth zero rather than moved.
  - no key is read out of the app dictionary either: `t()` answers an absent key with the key
    itself (`i18n/LocaleContext.tsx` -- `dictionaries.en[key] ?? key`), which would put
    `agent.readResultAnnounce` on a Chinese screen.
*/

import { useEffect, useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import { COLLECTION_NO_TRAVELER } from "./collectionCopy";
import {
  copyFor,
  readCollections,
  refusalCopy,
  type CollectionShelf,
  type ReadOutcome,
} from "./collections";
import "./collections.css";

type Props = {
  /** The traveller the shelves are scoped to.  The door requires it; this face does not invent one. */
  travelerId: string | null;
};

export function CollectionPanel({ travelerId }: Props) {
  const { locale } = useLocale();
  const zh = locale !== "en";
  const [outcome, setOutcome] = useState<ReadOutcome | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!travelerId) {
      setOutcome(null);
      return;
    }
    let live = true;
    setOutcome(null);
    void readCollections(travelerId).then((answer) => {
      if (live) setOutcome(answer);
    });
    return () => {
      live = false;
    };
  }, [travelerId, attempt]);

  const shelves: CollectionShelf[] = outcome && outcome.kind === "loaded" ? outcome.shelves : [];
  const rows = shelves.reduce((total, shelf) => total + shelf.item_count, 0);

  return (
    <section className="cn-collections" data-testid="collection-panel">
      <header className="cn-collections__head">
        <div>
          <p className="cnx-eyebrow">{copyFor("title", zh)}</p>
          <h2>{copyFor("scope", zh)}</h2>
        </div>
        <button
          type="button"
          className="cn-collections__retry"
          onClick={() => setAttempt((n) => n + 1)}
          disabled={!travelerId || outcome === null}
          data-testid="collection-retry"
        >
          {copyFor("retry", zh)}
        </button>
      </header>

      {!travelerId ? (
        <p className="cn-collections__state" data-testid="collection-no-traveler">
          {zh ? COLLECTION_NO_TRAVELER.zh : COLLECTION_NO_TRAVELER.en}
        </p>
      ) : !outcome ? (
        <p className="cn-collections__state">{copyFor("loading", zh)}</p>
      ) : outcome.kind === "refused" || outcome.kind === "unreachable" ? (
        <p className="cn-collections__state" data-testid="collection-failure">
          {refusalCopy(zh)}
        </p>
      ) : shelves.length === 0 ? (
        <p className="cn-collections__state" data-testid="collection-empty">
          {copyFor("empty", zh)}
        </p>
      ) : (
        <ul className="cn-collections__list">
          {shelves.map((shelf) => (
            <li key={shelf.id} className="cn-collections__shelf" data-testid="collection-shelf">
              <p className="cn-collections__shelf-name">{shelf.name}</p>
              <p className="cn-collections__shelf-count">
                {shelf.item_count} {copyFor("places", zh)}
              </p>
              {shelf.items.length === 0 ? (
                <p className="cn-collections__noshelf">{copyFor("noShelf", zh)}</p>
              ) : (
                <ul className="cn-collections__items">
                  {shelf.items.map((item) => (
                    <li key={item.id} data-testid="collection-item">
                      <span className="cn-collections__item-name">{item.name}</span>
                      <span className="cn-collections__item-note">
                        {item.note === null ? copyFor("noteAbsent", zh) : item.note}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
      {/* Both halves of this line come from the same answer the rows above are drawn from, so the
          number cannot describe a different read than the list under it. */}
      <p className="cn-collections__tally" data-testid="collection-tally">
        {`${shelves.length} ${copyFor("shelf", zh)} · ${rows} ${copyFor("places", zh)}`}
      </p>
    </section>
  );
}
