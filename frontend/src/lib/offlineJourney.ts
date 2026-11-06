export type OfflineJourney = {
  journey_id: string;
  sequence: number;
  valid_until: string;
  snapshot: {
    active_plan_version_id: string | null;
    current_activity_id: string | null;
    execution?: {
      now: { id: string; title: string; state: string; start_at: string; end_at: string; locked: boolean } | null;
      next: { id: string; title: string; state: string; start_at: string; end_at: string; locked: boolean } | null;
      now_activity_id: string | null;
      next_activity_id: string | null;
      now_state: string;
      next_state: string | null;
      next_mode: string;
      next_eta_min: number;
    };
    now?: { id: string; title: string; state: string } | null;
    next?: { id: string; title: string; state: string } | null;
    next_24h: Array<{ id: string; title: string; type: string; start_at: string; end_at: string; locked: boolean }>;
    legs: Array<{ id: string; mode: string; duration_min: number; from_key: string; to_key: string }>;
    hotel: { name: string; latitude: number; longitude: number; crs: string } | null;
    reservations: Array<{ id: string; type: string; confirmation_ref: string; scheduled_arrival_at: string; realtime_status: string }>;
    read_only: true;
    financial_actions_allowed: false;
  };
  provider_freshness: { latest_provider_capture_at: string | null; transit_realtime: string };
  updated_at: string;
};

class OfflineDatabase extends Dexie {
  journeys!: Table<OfflineJourney, string>;

  constructor() {
    super("citynexus-offline");
    this.version(1).stores({ journeys: "journey_id" });
  }
}

const database = new OfflineDatabase();

export async function saveOfflineJourney(snapshot: OfflineJourney): Promise<void> {
  await database.journeys.put(snapshot);
}

/**
 * ORD-0178 ③: "已保存" may only be claimed when the row was written **and read back**.  A `put()`
 * that resolves is not evidence the copy is on the device -- a blocked or evicted store resolves
 * differently, and the sentence was previously printed from in-memory state alone.  This returns
 * whether the copy is genuinely there, so the caller can gate the sentence and the read-only action
 * on a measurement rather than on a hope.
 */
export async function saveOfflineJourneyVerified(snapshot: OfflineJourney): Promise<boolean> {
  try {
    await database.journeys.put(snapshot);
    const readBack = await database.journeys.get(snapshot.journey_id);
    return Boolean(readBack && readBack.journey_id === snapshot.journey_id);
  } catch {
    return false;
  }
}

export async function readOfflineJourney(journeyId: string): Promise<OfflineJourney | null> {
  return (await database.journeys.get(journeyId)) ?? null;
}
import Dexie, { type Table } from "dexie";
