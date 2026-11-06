import { useT } from "../i18n/LocaleContext";
/* ORD-0435's stylesheet is on the authoritative line now (blob 50bbbc5ae8ab643f8dcbc1a4611e78feeff36d92
   at 0fee131d02b7c7c85f59aab8307f28594bf0d65f), and it already writes both twins of every state
   -- `[data-picked="false"]` as loud as `[data-picked="true"]`, `.v8-pick-box` as loud as
   `.v8-pick-box.checked`. This card uses exactly those hooks, so it imports that sheet instead of
   restating a second set of rules for the same control. */
import "../features/discovery/discovery-batch.css";
import { PlaceProvenance } from "./PlaceProvenance";

/**
 * ORD-0437: the one card a place arrives on, whether it came from a pasted note (this order) or from
 * the batch picker (ORD-0435).
 *
 * The markup contract is lifted from the discovery panel's result card rather than written afresh,
 * because two cards that look alike are how a screen starts answering the same question twice:
 * `frontend/src/features/discovery/RealCityDiscoveryPanel.tsx` at the base of the first round
 * (8d038b21f09477b933cff3b1c7b09845c542373b), and the same file's `[data-picked]` + `.v8-pick-box`
 * control as it stands on the line now. It is extracted, not forked: nothing on the discovery side
 * was touched, so ORD-0435 can move onto this component in one edit of its own.
 *
 * Selection is a pair, never a state: the reference chain's own rule (map_findings.md) is that
 * every 选中态 needs its 未选中态, so the control is always mounted and `aria-checked` is what moves.
 */
export type PoiCardStatus = "RESOLVED" | "AMBIGUOUS" | "UNRESOLVED";

export function PoiCandidateCard({
  index,
  phrase,
  status,
  placeId,
  placeName,
  category,
  coordinateSystem,
  note,
  addable,
  selected,
  onToggle,
  ambiguousNames = [],
}: {
  index: number;
  /** The words the traveller actually wrote, kept visible so a match can be argued with. */
  phrase: string;
  status: PoiCardStatus;
  placeId?: string | null;
  placeName?: string | null;
  category?: string | null;
  coordinateSystem?: string | null;
  note?: string | null;
  addable: boolean;
  selected: boolean;
  /** null where the surface has no selection yet, so the card degrades to a read-only row. */
  onToggle: (() => void) | null;
  ambiguousNames?: string[];
}) {
  const t = useT();
  const label = placeName || phrase;
  const stateWord =
    status === "RESOLVED"
      ? t("poiImport.cardMatched")
      : status === "AMBIGUOUS"
        ? t("poiImport.cardAmbiguous")
        : t("poiImport.cardUnresolved");
  return (
    <article
      /* `.selected` used to be set here for the traveller's own pick, and on this line that class means
         the server's feasibility verdict on the discovery screen (styles.css gives it a green-ish
         ring). Measured: with it, my picked card painted #8490e9 from that shared rule instead of the
         accent line ORD-0435's sheet writes for `[data-picked="true"]`. One state, one hook -- the pick
         now rides `[data-picked]` only. */
      className="v8-stay-card"
      data-testid="poi-candidate-card"
      data-poi-status={status}
      data-poi-addable={addable ? "yes" : "no"}
      data-picked={selected ? "true" : "false"}
    >
      <div className="v8-stay-card-body">
        <div className="v8-stay-card-title">
          <div>
            <span className="v8-rank">{status === "RESOLVED" ? "✓" : "—"}</span>
            <h3>{label}</h3>
          </div>
          <span className="v8-selected-label" data-testid="poi-candidate-state">
            {stateWord}
          </span>
        </div>
        <PlaceProvenance
          name={label}
          category={category ?? "poi"}
          providerKey={null}
          coordinateSystem={coordinateSystem ?? null}
          sourceCoordinateSystem={null}
          host={null}
        />
        {note ? <p data-testid="poi-candidate-note">{note}</p> : null}
        {status !== "RESOLVED" ? (
          <p className="v8-sheet-note" data-testid="poi-candidate-reason">
            {status === "AMBIGUOUS"
              ? t("poiImport.cardAmbiguousWhy", { names: ambiguousNames.slice(0, 3).join(" / ") })
              : t("poiImport.cardUnresolvedWhy")}
          </p>
        ) : null}
        <div className="v8-stay-impact">
          <span data-testid="poi-candidate-phrase">{t("poiImport.cardPhrase", { phrase })}</span>
          <b>{t("poiImport.cardIndex", { n: index })}</b>
          {onToggle ? (
            <button
              type="button"
              role="checkbox"
              aria-checked={selected}
              className={selected ? "v8-pick-box checked" : "v8-pick-box"}
              disabled={!addable}
              data-testid="poi-card-select"
              data-picked={selected ? "true" : "false"}
              onClick={onToggle}
            >
              {/* Same control shape ORD-0435 settled on (its card is inline in the discovery panel at
                  blob 8ef92f699b5e18942a554376ac48242aa4d54a36, so nothing here is importable): the tick
                  is its own control, `aria-checked` twins the state, and `[data-picked]` is the paint
                  hook a shared sheet can address. The word stays painted -- a glyph alone would be a
                  state only a screen reader could read. */}
              <span aria-hidden="true">{selected ? "✓" : ""}</span>
              <span className="v8-pick-box-text">
                {selected ? t("poiImport.cardDeselect") : t("poiImport.cardSelect")}
              </span>
            </button>
          ) : null}
        </div>
      </div>
    </article>
  );
}
