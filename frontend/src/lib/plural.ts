/**
 * ORD-0433: the one place that decides English singular vs plural for a counted noun.
 *
 * Copy in the dictionary marks the noun it counts with `{plural:stem}` and hands the count through
 * the parameter channel the call sites already use (`t(key, { n })`), so no call site has to know
 * about grammar and no sentence has to be softened into "1 or more". Chinese copy carries no marker,
 * so the Chinese arm's bytes are untouched by construction.
 *
 * `refusal(s)`-style shorthand is deliberately not a feature: a screen either says one refusal or
 * says several.
 */

const IRREGULAR: Record<string, string> = {
  /* The hub's budget line counts people, and `persons` is not what the product means by it. Added once
     here rather than spelled out in the sentence, which is the whole reason this table exists. */
  person: "people",
};

function isOne(count: unknown): boolean {
  if (typeof count === "number") return count === 1;
  const text = String(count ?? "").trim();
  return text === "1";
}

export function regularPlural(singular: string): string {
  const word = singular.trim();
  if (!word) return word;
  if (IRREGULAR[word]) return IRREGULAR[word];
  if (/[^aeiouy]y$/i.test(word)) return `${word.slice(0, -1)}ies`;
  if (/(s|x|z|ch|sh)$/i.test(word)) return `${word}es`;
  return `${word}s`;
}

/** The word a reader should see after a count: `1 traveler` / `2 travelers`. */
export function englishCountNoun(count: unknown, singular: string): string {
  return isOne(count) ? singular.trim() : regularPlural(singular);
}

/**
 * Expands every `{plural:stem}` in one dictionary line. The count may sit under a parameter name
 * other than `n` — `{plural:day:days}` reads `params.days` — because the agent copy already passes
 * `{days}` / `{activities}` / `{landed}` and the call sites are outside this file.
 *
 * A line with no marker is returned unchanged, byte for byte, which is what keeps the ~1,500
 * untouched Chinese entries out of this code path.
 */
export function expandPluralMarkers(text: string, params?: Record<string, string | number>): string {
  if (!text.includes("{plural:")) return text;
  return text.replace(/\{plural:([A-Za-z][A-Za-z_ ]*)(?::([A-Za-z_]+))?\}/g, (_m, stem: string, countKey?: string) =>
    englishCountNoun(params?.[countKey ?? "n"], stem),
  );
}
