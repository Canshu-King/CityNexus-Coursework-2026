import { type APIRequestContext } from "@playwright/test";

/**
 * Per-spec journey isolation (pass-9 TEST_INFRA_ISOLATION_BLOCKER).
 *
 * The suite shares one backend and one SQLite file. A global seed can therefore
 * only ever be a *default*: any spec that resets the demo, selects a different
 * option, or accepts a proposal changes what the next spec sees, which is why
 * three v10 specs pass in nine seconds alone and fail inside the serial run.
 * Reordering or retrying would just be Playwright feng shui.
 *
 * The fix is ownership: a spec that depends on journey state creates its own and
 * carries its own id, exactly as `v13-5-fully-grounded` and
 * `real-world-trip-operations-v9` already do. This helper is that pattern in one
 * place, built only from public product endpoints — no SQL, no mocked routes.
 */
export type FreshJourney = { journeyId: string; optionCount: number; optionId: string | null };

const API =
  process.env.CITYNEXUS_API_BASE_URL ??
  `http://127.0.0.1:${Number(process.env.CITYNEXUS_BACKEND_PORT ?? 8000)}`;

async function post<T = any>(
  request: APIRequestContext,
  url: string,
  data?: unknown,
  headers?: Record<string, string>,
): Promise<T> {
  const res = await request.post(`${API}${url}`, { data, headers });
  if (!res.ok()) {
    throw new Error(`${url} -> HTTP ${res.status()}: ${(await res.text()).slice(0, 180)}`);
  }
  return res.json();
}

async function get<T = any>(request: APIRequestContext, url: string): Promise<T> {
  const res = await request.get(`${API}${url}`);
  if (!res.ok()) throw new Error(`${url} -> HTTP ${res.status()}`);
  return res.json();
}

/**
 * Two nights in Tianjin, because a one-day trip makes the stay-aware optimiser
 * return zero options and silently turns every downstream assertion into a test
 * of an empty screen.
 */
export async function createFreshJourney(
  request: APIRequestContext,
  opts: { select?: boolean } = {},
): Promise<FreshJourney> {
  const traveler = await post(request, "/api/v1/travelers", {
    display_name: "Qoder per-spec traveller",
    locale: "en-GB",
    timezone: "Asia/Shanghai",
  });
  const journey = await post(request, "/api/v1/journeys", {
    traveler_id: traveler.id,
    title: "Tianjin · per-spec weekend",
    city_code: "CN-TIANJIN",
    timezone: "Asia/Shanghai",
    start_at: "2026-10-09T18:00:00+08:00",
    end_at: "2026-10-11T15:00:00+08:00",
    budget: { amount_minor: 120000, currency: "CNY" },
  });

  let optionCount = 0;
  let optionId: string | null = null;
  if (opts.select !== false) {
    await post(request, `/api/v1/journeys/${journey.id}/stay-options/generate`);
    const listed = await get(request, `/api/v1/journeys/${journey.id}/stay-options`);
    const options = listed.options ?? [];
    optionCount = options.length;
    if (optionCount) {
      const chosen = options.find((o: any) => o.status === "RECOMMENDED") ?? options[0];
      optionId = chosen.id;
      await post(request, `/api/v1/journeys/${journey.id}/stay-options/${chosen.id}/select`);
    }
  }

  return { journeyId: journey.id, optionCount, optionId };
}

/** Carry a spec's own journey into the browser, overriding any shared seed. */
export async function useJourney(page: import("@playwright/test").Page, journeyId: string) {
  await page.addInitScript((id) => {
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.demo.journey_id", id);
  }, journeyId);
}

export type ProtectedCommitment = {
  activityId: string;
  placeId: string;
  bookingId: string;
  commitmentId: string | null;
  amount: unknown;
};

/**
 * Create a real protected commitment through the path a traveller would use.
 *
 * Measured, not assumed: on the demo plan the cruise activity is `locked: true`
 * but `protected: false` until a booking exists and its payment is captured —
 * `protected` is derived from ACTIVE Commitment rows.  So a spec that asserts
 * "the protected commitment survived the recovery" without creating one is
 * asserting on a fixture that has nothing to protect, and the flag it reads is
 * true for the wrong reason.
 *
 * Must run before `/start`: booking an activity on a journey that is already
 * travelling is refused with 422.
 */
export async function protectCommitment(
  request: APIRequestContext,
  journeyId: string,
  placeId = "place-river-cruise",
): Promise<ProtectedCommitment> {
  const experience = await get<{ active_plan?: { activities?: any[] } }>(
    request,
    `/api/v1/journeys/${journeyId}/experience`,
  );
  const activity = (experience.active_plan?.activities ?? []).find(
    (item) => item.place_id === placeId,
  );
  if (!activity) {
    throw new Error(`no activity for ${placeId} on journey ${journeyId} to protect`);
  }

  const booking = await post<{
    id: string;
    commitment_id?: string | null;
    amount?: unknown;
  }>(request, `/api/v1/journeys/${journeyId}/bookings`, { activity_id: activity.id });
  await post(
    request,
    `/api/v1/journeys/${journeyId}/transactions`,
    { booking_id: booking.id, transaction_type: "CAPTURE", amount: booking.amount },
    { "Idempotency-Key": `protect-${booking.id}` },
  );

  /* Prove it landed before anything else leans on it. */
  const after = await get<{ active_plan?: { activities?: any[] } }>(
    request,
    `/api/v1/journeys/${journeyId}/experience`,
  );
  const projected = (after.active_plan?.activities ?? []).find(
    (item) => item.place_id === placeId,
  );
  if (!projected?.protected) {
    throw new Error(
      `booking ${booking.id} + CAPTURE did not make ${placeId} a protected commitment ` +
        `(locked=${projected?.locked}, protected=${projected?.protected})`,
    );
  }

  return {
    activityId: activity.id,
    placeId,
    bookingId: booking.id,
    commitmentId: booking.commitment_id ?? null,
    amount: booking.amount,
  };
}
