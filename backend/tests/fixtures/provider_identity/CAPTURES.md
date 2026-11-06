# provider_identity fixtures — verbatim captures, not hand-authored

Every `*.json` here is one real HTTP exchange recorded from this machine, stored as
`{captured_at, url, request_user_agent, request_post_data, elapsed_ms, status, error,
response_headers, body_text}`.  `body_text` is the provider's own bytes decoded; nothing was
retyped, re-ordered, or "cleaned up", because the shapes are the point.

| file | captured | status | what it proves |
|---|---|---|---|
| `photon_gongshan_full_match.json` | 2026-09-22 01:00 (+0800), UA `CityNexus/7.0 (+https://citynexus.local/discovery)` | 200, 4 features | **MATCH shape**: exactly one destination-grade entity — `osm_type=R`, `osm_id=2729765`, `type=city`, name 贡山县, `extent=[98.132047, 28.3970972, 98.9182148, 27.4945677]`. The other three hits are `house`, so they are not identity candidates. |
| `photon_yunhe_county_ambiguous.json` | same minute | 200, 4 features | **AMBIGUOUS shape**: the *same* name 云和县 comes back twice as two different entities — `R:3289208` (`type=district`, with extent) **and** `N:244082768` (`type=county`, no extent). Picking one is a human decision, so the provider layer refuses to rank them. |
| `photon_yunhe_short_ambiguous.json` | same minute | 200, 8 features | Short-form ambiguity: same two 云和县 entities plus `locality` hits in 山西省/上海市 — the reason "one query, one city" cannot be assumed. |
| `photon_unknown_place_zero_features.json` | same minute | 200, `{"type":"FeatureCollection","features":[]}` | **NO_MATCH shape**: a *healthy* resolver answering "nothing like that exists". This is the only legitimate zero. |
| `photon_reverse_gongshan_200_zero_features.body.json` | 2026-09-21 20:44 (+0800), `/reverse` at the county centroid (HTTP 200 recorded in `_qoder_spike_20260921/q148_identity2.json → reverse_round_trip`, 3,109 ms) | 200, 0 features | Second real zero-answer, and a warning: a county's geometric centre can be empty mountain, so `/reverse` is not a mandatory confirmation leg. |
| `photon_refused_default_user_agent.json` | 2026-09-22 01:01 (+0800), UA `python-httpx/0.28.1` | **403**, 146-byte body | **RUNTIME_FAILURE ≠ NO_MATCH**: the same URL and query answered 200 nine seconds earlier with the project UA. A refusal that depends on our own UA must not be readable as "this place does not exist". |
| `photon_bad_request_400_runtime_failure.json` | same minute | 400 | Our own malformed request (search URL called with `lat/lon`) — classified as a runtime failure, separately from a provider outage. |
| `overpass_hotel_answer_with_elements.json` | same minute, `[timeout:25]` around the county seat | **504**, HTML body | A real Overpass gateway timeout, with its body: the shape the mirror loop must report as unreachable rather than empty. |
| `overpass_museum_answer_zero_elements.json` | same minute | 200, 0 elements | The shape the old mirror loop destroyed: an answered zero from the first endpoint, previously fallen through to slower mirrors and re-raised as `PROVIDER_TIMEOUT`. |

Filename note: the last row was first saved as `photon_reverse_zero_features_no_match.json`; it has been renamed to `photon_bad_request_400_runtime_failure.json` so that no test can mistake a 400 we caused for a provider's zero answer.  If a future capture adds a name, it must say which exchange it came from.
