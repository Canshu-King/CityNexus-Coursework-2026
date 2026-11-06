# Real Compose Engine V10

Normal composition uses a traveler mission, explicit/discovered POIs, Inbox reservations, Stay candidates, Journey anchors, protected Commitments, opening hours, provider mobility, TravelerTwin soft preferences, and the autonomy envelope.

The pipeline is mission understanding → grounded POIs → availability facts → provider route matrix → bounded multi-day composition → mode-aware CP-SAT → Top-K candidates → PlanVersion materialization → deterministic gate → activation → JourneyTwin. City fixtures remain test seeds only. The generic `place_ids` path proves arbitrary selected POIs can produce a valid plan without reading a fixture Activity list.

