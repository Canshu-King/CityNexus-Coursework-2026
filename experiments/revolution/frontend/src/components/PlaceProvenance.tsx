/**
 * The place-provenance pair (ORD-B): whose fact this is, and in which coordinate
 * system the numbers on this card mean anything.
 *
 * Three rules hold this component together:
 *  - A row is never dropped. A missing value shows a placeholder that names *which*
 *    fact is absent, because a field that disappears teaches the traveller nothing.
 *  - "The server answered: none recorded" and "this payload never carried the cell"
 *    are different facts and get different sentences, so a traveller never reads a
 *    silence as a record.
 *  - A row is only anchored when it carries a value the server actually sent (Q70:
 *    the anchor is the contract field name, same convention as `CnxMeasuredFacts`).
 *    A placeholder is our own word, not a reading, so it gets no anchor.
 */

import { useLocale } from "../i18n/LocaleContext";
import { provenanceCopy } from "../i18n/placeProvenanceCopy";

type Props = {
  /** The place's own name, as the server sent it. */
  name: string;
  category?: string | null;
  providerKey?: string | null;
  coordinateSystem?: string | null;
  sourceCoordinateSystem?: string | null;
  host?: string | null;
};

/**
 * A string is a fact.  null is the server answering that it recorded none.
 * undefined is the cell being absent from this payload -- and an empty string is
 * too, because it carries nothing either.  null and undefined must never collapse.
 */
function resolve(value?: string | null): string | null | undefined {
  if (value === undefined || value === null) return value;
  const trimmed = value.trim();
  return trimmed ? trimmed : undefined;
}

function shown(value: string | null | undefined, recordedNone: string, notSent: string): string {
  if (typeof value === "string") return value;
  return value === null ? recordedNone : notSent;
}

function Field({
  label,
  field,
  value,
  recordedNone,
  notSent,
}: {
  label: string;
  field: string;
  value: string | null | undefined;
  recordedNone: string;
  notSent: string;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd
        data-testid={`place-provenance-${field}`}
        {...(typeof value === "string"
          ? { "data-cnx-field": field, "data-cnx-value": value }
          : {})}
      >
        {shown(value, recordedNone, notSent)}
      </dd>
    </div>
  );
}

export function PlaceProvenance({
  name,
  category,
  providerKey,
  coordinateSystem,
  sourceCoordinateSystem,
  host,
}: Props) {
  const { locale } = useLocale();
  const c = provenanceCopy(locale);
  const source = resolve(providerKey);
  const summary = [category, shown(source, c.noSource, c.notSent)].filter(Boolean).join(" · ");

  return (
    <details data-testid="place-provenance">
      <summary data-testid="place-provenance-summary">{`${c.detailsLabel} · ${summary}`}</summary>
      <dl className="trial-review activity-facts">
        <Field
          label={c.name}
          field="name"
          value={resolve(name)}
          recordedNone={c.noName}
          notSent={c.notSent}
        />
        <Field
          label={c.source}
          field="provider_key"
          value={source}
          recordedNone={c.noSource}
          notSent={c.notSent}
        />
        <Field
          label={c.coordinateSystem}
          field="coordinate_system"
          value={resolve(coordinateSystem)}
          recordedNone={c.noCoordinateSystem}
          notSent={c.notSent}
        />
        <Field
          label={c.sourceCoordinateSystem}
          field="source_coordinate_system"
          value={resolve(sourceCoordinateSystem)}
          recordedNone={c.noSourceCoordinateSystem}
          notSent={c.notSent}
        />
        <Field
          label={c.host}
          field="opening_hours.source_urls"
          value={resolve(host)}
          recordedNone={c.noHost}
          notSent={c.notSent}
        />
      </dl>
    </details>
  );
}
