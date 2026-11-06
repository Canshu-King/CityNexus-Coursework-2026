# Live Provider Capture V10

Provenance vocabulary:

- `LIVE_CAPTURE`: obtained from a configured external provider during this run.
- `CAPTURED_REPLAY`: a stored live capture replayed through the same normalizer.
- `SYNTHETIC_FIXTURE`: deterministic demo/test data.

Synthetic formulas must never be labelled `CAPTURED_REPLAY`. A sealed capture contains provider/operation, request fingerprint, redacted raw response, normalized facts, capture/expiry, CRS, provider version, checksum, and redaction metadata; secrets are excluded.

When a provider is unavailable, preserve last-known evidence with freshness and use synthetic data only in explicit demo/test mode. `scripts/capture_trip_provider_snapshot.py` produces replayable redacted snapshots.

