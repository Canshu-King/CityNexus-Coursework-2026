import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale } from "../../i18n/LocaleContext";
import { cityNexusApi } from "../../api/citynexus";
import { formatMinorMoney } from "../../lib/money";
import { useAuthoritativeTwin } from "../../api/v12Authoritative";
import {
  basisWord,
  evidenceWord,
  invalidateScene,
  sceneInstant,
  useDemoScene,
} from "../../lib/sceneClock";
import {
  describeBookingRefusal,
  describeFailure,
  listCityServices,
  NEXT_STEP,
  readBookableTargets,
  runStageAction,
  postSimulatedEvent,
  resetStage,
  transitionStage,
  type CityServiceRow,
} from "./simulationAdapter";
import "./SimulationPanel.css";

/**
 * ORD-0059 (b) -- the visible Simulation entry.
 *
 * What this screen is allowed to know comes from two server reads and nothing else: the stage
 * (`GET /demo/scene`) and the journey's twin.  It owns no scenario state, no clock, and no
 * lifecycle of its own -- every row below is one existing product endpoint, and the line the
 * traveller gets afterwards is what the server answered, not a promise this screen made.
 *
 * So the disabled states are the design, not decoration.  A stage where "推进一个活动" is greyed
 * out because the seeded journey has no cursor yet must say why in the traveller's language and
 * stay reachable (same 甲 discipline as the wizard's blocked Next: aria-disabled, focusable, the
 * reason attached); a grey box that explains nothing is how a person ends up clicking around for
 * ten minutes against a refusal they were never told about.
 */

type ActionId =
  | "reset"
  | "prepare"
  | "start"
  | "advance"
  | "event"
  | "replan"
  | "accept"
  | "reject"
  | "book"
  | "degrade"
  | "restore";

type Blocker = { zh: string; en: string; /** ORD-0086: true when the sentence is only about a reading
    that has not landed yet.  Those may not stick once a newer reading arrives. */ transient?: boolean };

type Row = {
  id: ActionId;
  zh: string;
  en: string;
  /** Why this row cannot run right now, or null when it can. */
  blocked: Blocker | null;
  /** `refused` marks a server answer that declined the step while still answering 200. */
  run: () => Promise<{ zh: string; en: string; refused?: boolean }>;
};

/* ORD-0078 (a) / ORD-0081 ①: the shell picks its journey once, at boot, from this key, and the
   stage routes create journeys the key has never heard of.  Handing over the id the server
   returned -- and only that -- is what lets a teacher get from the wizard to Today by pressing a
   button instead of someone editing storage. */
const JOURNEY_ID_KEY = "REDACTED_FOR_PUBLIC_RELEASE";
function handOverStage(journeyId: string | null | undefined) {
  if (!journeyId) return false;
  localStorage.setItem(JOURNEY_ID_KEY, journeyId);
  return true;
}

const newKey = () =>
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `sim-${Math.random().toString(36).slice(2, 12)}`;

const shortId = (value: string | null) => (value ? value.slice(0, 8) : "—");

export function SimulationPanel({
  viewingJourneyId = null,
  onStageChanged = null,
  onClose = null,
}: {
  /** The journey the shell is showing, so the panel can say when the stage is a different one. */
  viewingJourneyId?: string | null;
  /** ORD-0082: Today renders from the shell's own journey read, so any action that moves the
      stage has to tell that read to refresh.  Without it the panel advances while the hero keeps
      showing the cursor it loaded with -- a new way to show two answers on one screen. */
  onStageChanged?: ((action: ActionId) => void) | null;
  /** ORD-0085: on a phone the open panel is laid out above the fixed nav, where its own close is
      the way back -- a sheet the traveller can only leave by finding a summary off-screen is its
      own trap. */
  onClose?: (() => void) | null;
}) {
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const client = useQueryClient();
  const scene = useDemoScene();
  const journeyId = scene.scene?.journeyId ?? null;
  const twin = useAuthoritativeTwin(journeyId);
  /* The transition graph lives on the server, so the row has to ask which state the current
     activity is in before it can name a step.  `plan_version_id` comes from the scene read, and
     the plan itself from the ordinary product endpoint -- no second source of truth. */
  const planVersionId = scene.scene?.planVersionId ?? null;
  const plan = useQuery({
    queryKey: ["sim-stage-plan", planVersionId],
    enabled: Boolean(planVersionId),
    queryFn: () => cityNexusApi.getPlan(planVersionId as string),
  });
  const [running, setRunning] = useState<ActionId | null>(null);
  const [lastEventId, setLastEventId] = useState<string | null>(null);
  /* ORD-0082 (b): whether a proposal exists is the server's fact, not this screen's memory.  A
     remembered id left 接受/拒绝 clickable on a journey whose `GET /replans` answers [] -- the tap
     then did nothing at all, which is the same silence ORD-0066 was opened against. */
  const replans = useQuery({
    queryKey: ["sim-replans", journeyId],
    enabled: Boolean(journeyId),
    queryFn: () => cityNexusApi.listReplans(journeyId as string),
  });
  /* TEMPORARY-CLIENT-FILTER-ORD-0180 -- transitional bridge, not a contract.
     `GET /journeys/{id}/replans` answers the whole Journey-scoped list; the server has no status
     filter yet (ORD-0180 ②, filed with C as ORD-0182, same gap on /plans).  Until that lands, the
     panel narrows the list here.  When it does land, the request becomes `?status=PRESENTED` and this
     filter AND this marker are deleted together -- a marker left behind is a gate failure, because an
     unmarked shim becomes the next person's contract.
     What this filter is NOT: an authorization token.  A row appearing here means "the list contained
     it", nothing more -- `accept`/`reject` still re-read the current state server-side, and this
     screen does not treat presence as permission (ORD-0183 ②). */
  const openProposals = (replans.data ?? []).filter(
    (item) =>
      !["ACCEPTED", "REJECTED", "DECLINED", "SUPERSEDED"].includes(String(item.status ?? "").toUpperCase()),
  );
  const proposalId = openProposals.length ? String(openProposals[0].id) : null;
  const [outcome, setOutcome] = useState<{
    ok: boolean;
    zh: string;
    en: string;
    /** The scene reading this line was written against, and whether it was only about a read. */
    transient: boolean;
    sceneStamp: number | null;
  } | null>(null);

  /* ORD-0086: a refusal that said "still reading" must not survive the reading.  E measured it
     stuck -- throttle the round trip, press, lift the throttle, `GET /demo/scene` answers 200, and
     the line stayed on screen until a reload, so the only way back was to leave the screen. */
  useEffect(() => {
    setOutcome((current) =>
      current?.transient && current.sceneStamp !== scene.updatedAt ? null : current,
    );
    // The stamp is deliberately the only dependency: this retires an old line, it never writes one.
  }, [scene.updatedAt]);

  /* ORD-0082 (wiring): `POST /replans/{id}/accept` requires an `Idempotency-Key` (measured: without
     it the server answers 422) and a *different* key is a *different* request.  So one key per
     proposal, kept across retries -- a second tap replays, it does not open a second acceptance. */
  const idempotencyKeys = useRef(new Map<string, string>());
  const keyFor = (scope: string) => {
    const held = idempotencyKeys.current.get(scope);
    if (held) return held;
    const created = newKey();
    idempotencyKeys.current.set(scope, created);
    return created;
  };

  const refreshAll = (action?: ActionId) => {
    if (action) onStageChanged?.(action);
    invalidateScene(client);
    void client.invalidateQueries({
      queryKey: ["v12-authoritative-twin", journeyId],
    });
    void client.invalidateQueries({
      queryKey: ["sim-stage-plan", planVersionId],
    });
    void client.invalidateQueries({
      queryKey: ["sim-replans", journeyId],
    });
  };

  const notReady =
    scene.phase !== "ready"
      ? {
          transient: true,
          zh:
            scene.phase === "loading"
              ? "还在读服务器的场景状态。"
              : "场景状态没读到，先看下面的原因。",
          en:
            scene.phase === "loading"
              ? "The stage state is still being read from the server."
              : "The stage state could not be read; see the reason below.",
        }
      : null;
  const noJourney =
    !notReady && !journeyId
      ? {
          zh: "台上没有场景：服务器回的是 NO_SCENARIO。先在产品里建一趟行程，或按下面的重置播一个演示场景。",
          en: "There is no scenario on stage (the server answered NO_SCENARIO). Create a trip in the product, or re-seed one below.",
        }
      : null;
  const cursorless = twin.isPending
    ? {
        transient: true,
        zh: "服务器游标还在读，读完才知道有没有可推进的活动。",
        en: "The server cursor is still being read.",
      }
    : !twin.data?.current_activity_id
      ? (scene.scene?.journeyStatus ?? null) !== "ACTIVE"
        ? /* ORD-0078/0088 shape: `65e184d` stopped reset from starting the journey, so "no cursor"
             is now the normal state after 重置/准备舞台.  Saying "没有可推进的一步" alone sends the
             teacher looking for a broken button; the step exists and has a name. */
          {
            zh: `这趟还没有开始执行（服务器说行程状态是 ${scene.scene?.journeyStatus ?? "未知"}）。先按上面「开始执行」，才会有可推进的游标。`,
            en: `This journey is not executing yet (the server says ${scene.scene?.journeyStatus ?? "unknown"}). Press "Put the journey into execution" above; that is what gives a step to advance.`,
          }
        : {
            zh: "服务器说这趟在执行中，但游标是空的（没有任何活动在进行），所以这一步没有可推进的位置。",
            en: "The server says the journey is executing but the cursor is empty (no activity is in progress), so there is no position to advance from.",
          }
      : null;
  const noPlan =
    !notReady && !journeyId
      ? noJourney
      : !twin.data?.active_plan_version_id
        ? {
            zh: "这趟还没有 ACTIVE 计划（服务器没返回计划版本），重规划需要它。",
            en: "No ACTIVE plan on this journey (the server returned no plan version), and a replan needs one.",
          }
        : null;

  /* The step this row may offer is whichever one the server's state graph allows from the
     current activity's status -- not the one this screen would like.  A panel that hard-codes
     "complete" gets `INVALID_ACTIVITY_TRANSITION:NOT_STARTED->COMPLETED` on every frozen plan and
     then has to explain a refusal it invented itself. */
  const currentActivity =
    plan.data?.activities.find(
      (item) => item.id === twin.data?.current_activity_id,
    ) ?? null;
  const nextStep = currentActivity
    ? (NEXT_STEP[currentActivity.status] ?? null)
    : null;
  const stepBlocked =
    plan.isPending && Boolean(twin.data?.current_activity_id)
      ? {
          transient: true,
          zh: "还在读这份计划里当前活动的状态。",
          en: "The current activity's status is still being read.",
        }
      : !currentActivity
        ? {
            zh: "服务器给的当前活动不在这份计划的清单里，所以没有可执行的下一步。",
            en: "The cursor points at an activity that is not on this plan, so there is no next step to offer.",
          }
        : !nextStep
          ? {
              zh: `这项活动在服务器已经是 ${currentActivity.status}，它的状态图里没有下一步。`,
              en: `The server holds this activity as ${currentActivity.status}; its state graph has no next step.`,
            }
          : null;

  /* ORD-0100 ①: what activity 「建一个硬承诺」 aims at is now the server's answer, not the plan's
     array order.  What used to sit here sorted by "the server gave this activity a service identity"
     -- and `city_service_id` is null on **all six** activities of the demo plan
     (`review/city-services-shape.json`), so that sort was vacuous, the default fell back to
     `Old Town Gate`, and the 422 that followed was the server being right about a target this screen
     had chosen by position.  Two reads replace it: the city's catalogue, then the slot window of each
     activity whose place carries an orderable service type. */
  const [bookIndex, setBookIndex] = useState(0);
  const openActivities = (plan.data?.activities ?? []).filter(
    (item) =>
      item &&
      !["COMPLETED", "SKIPPED", "CANCELLED"].includes(String(item.status)),
  );
  const journeyRead = useQuery({
    queryKey: ["sim-stage-journey", journeyId],
    enabled: Boolean(journeyId),
    queryFn: () => cityNexusApi.getJourney(journeyId as string),
  });
  const cityCode = journeyRead.data?.city_code ?? null;
  const servicesRead = useQuery({
    queryKey: ["sim-stage-city-services", cityCode],
    enabled: Boolean(cityCode),
    queryFn: () => listCityServices(cityCode as string),
  });
  const bookableRead = useQuery({
    queryKey: ["sim-stage-bookable", planVersionId, cityCode],
    enabled: Boolean(
      planVersionId && cityCode && servicesRead.data && openActivities.length,
    ),
    queryFn: () =>
      readBookableTargets(
        openActivities.map((item) => ({
          id: item.id,
          place_id: item.place_id ?? null,
          start_at: item.start_at,
          end_at: item.end_at,
        })),
        servicesRead.data as CityServiceRow[],
      ),
  });
  /* Only the activities the server gave a slot for, in the plan's own order.  The row's label and its
     换一个活动 control both read from this list, so what the row names is what a press will send. */
  const bookCandidates = bookableRead.data
    ? bookableRead.data.targets
        .map((target) =>
          openActivities.find((item) => item.id === target.activityId),
        )
        .filter(
          (item): item is (typeof openActivities)[number] => Boolean(item),
        )
    : [];
  const bookIndexClamped = bookCandidates.length
    ? Math.min(bookIndex, bookCandidates.length - 1)
    : 0;
  const bookTarget = bookCandidates[bookIndexClamped] ?? null;
  const bookNext = bookCandidates.length
    ? bookCandidates[(bookIndexClamped + 1) % bookCandidates.length]
    : null;

  /* ORD-0090 residual 3, measured before it was written (`review/booking-when-active.json`, own port
     and own DB): the same six activities asked twice -- while the journey is PLANNED the cruise books
     (201) and the other five answer their own named refusal; after `start-scenario` puts the journey in
     execution **all six** answer `422 / booking requires a PLANNED Journey`.  So the row is not
     "occasionally refused" at that point, it is refused by a rule about the journey, and the traveller
     learns it from the row instead of from six taps that all do the same thing. */
  const journeyStatus = scene.scene?.journeyStatus ?? null;
  const notPlanned =
    !notReady && journeyStatus !== null && journeyStatus !== "PLANNED"
      ? {
          zh: `服务器说这趟已经不在计划阶段（行程状态 ${journeyStatus}），而硬承诺只能建在还没开始的行程上：这时按下去，服务器对清单上每个活动都回同一句“booking requires a PLANNED Journey”。要改已经开始的这一趟，请用下面「请求重规划」。`,
          en: `The server says this journey is past the planning stage (status ${journeyStatus}), and a hard commitment is only taken for one that has not started: pressed now, the server answers every activity on the list with "booking requires a PLANNED Journey". To change a journey that has begun, use "Request a replan" below.`,
        }
      : null;

  /* The row's availability now rests on three reads, so "现在建不了" has three honest shapes and must
     not collapse into the wrong one: still reading, the read failed, or the server answered and said
     no.  A refusal about a read is marked `transient` so it retires with the next reading (ORD-0086). */
  const bookReadWanted = Boolean(
    planVersionId && cityCode && servicesRead.data && openActivities.length,
  );
  const bookReadBlocked =
    journeyRead.isPending && Boolean(journeyId)
      ? {
          transient: true,
          zh: "还在读这趟行程属于哪座城。",
          en: "The journey's city is still being read.",
        }
      : journeyRead.isError
        ? { transient: true, ...describeFailure(journeyRead.error) }
        : servicesRead.isPending && Boolean(cityCode)
          ? {
              transient: true,
              zh: "还在读这座城的服务目录（哪些地点挂着可下单的服务）。",
              en: "The city's service catalogue is still being read.",
            }
          : servicesRead.isError
            ? { transient: true, ...describeFailure(servicesRead.error) }
            : bookableRead.isPending && bookReadWanted
              ? {
                  transient: true,
                  zh: "目录里有带着可下单服务的活动，正在逐个按它自己的时间问服务器那一段有没有时段。",
                  en: "Some activities carry an orderable service; their windows are being asked of the server.",
                }
              : bookableRead.isError
                ? { transient: true, ...describeFailure(bookableRead.error) }
                : null;
  const nothingBookable =
    bookableRead.data &&
    bookableRead.data.targets.length === 0 &&
    openActivities.length > 0
      ? bookableRead.data.withOrderableService === 0
        ? {
            zh: `服务器说这座城的目录里，这趟 ${openActivities.length} 个还没结束的活动没有一个挂着可下单的服务类型，所以现在建不了硬承诺。要改这一趟，请用下面「请求重规划」。`,
            en: `The server says none of this journey's ${openActivities.length} open activities carries an orderable service type in this city's catalogue, so there is nothing to build a hard commitment on. To change the journey, use "Request a replan" below.`,
          }
        : {
            zh: `服务器说这趟有 ${bookableRead.data.withOrderableService} 个活动带着可下单的服务类型，但逐个按它自己的时间问时段时，一个都没给${bookableRead.data.unreadable ? `（另有 ${bookableRead.data.unreadable} 次没读到，那不是"没有时段"）` : ""}，所以现在建不了硬承诺。`,
            en: `The server says ${bookableRead.data.withOrderableService} of this journey's activities carry an orderable service type, but asked for each one's own window it returned no slot at all${bookableRead.data.unreadable ? ` (${bookableRead.data.unreadable} reads did not answer, which is not the same as "no slot")` : ""}, so there is nothing to book right now.`,
          }
      : null;

  const rows: Row[] = [
    {
      id: "reset",
      zh: "重置到场景起点",
      en: "Re-seed the scenario",
      /* ORD-0389: this action deletes the whole library (`services.reset_all`, 52 tables), and it
         now also requires a token the server reads from its own environment.  A browser cannot
         hold that value without shipping it to every visitor, which is the same leak the flag was,
         so the row is refused here on every deployment rather than only when demo_mode is off.
         Kept visible on purpose: a silently missing row would hide *why* the stage cannot be
         re-seeded from a screen, and the reason is the part a teacher needs. */
      blocked: {
        zh: "这一步会删掉整库（52 张表），现在还要服务器自己的环境变量里的令牌；浏览器拿不到它，也不该拿到。所以这里不给按——要重播种行程，请由运行服务器的人来做。",
        en: "This step deletes the whole library (52 tables) and now needs a token that lives in the server's own environment. A browser cannot hold that value and should not, so this is refused on every deployment -- re-seeding is done by whoever runs the server.",
      },
      run: async () => {
        const result = await resetStage();
        setLastEventId(null);
        const advance = result.advance;
        const refused =
          advance?.steps.find((step) => step.refused !== undefined) ?? null;
        /* ORD-0078 (a): the shell decides which journey to show once, at boot, from
           `localStorage["REDACTED_FOR_PUBLIC_RELEASE"]` (V8Experience.tsx:493), and `demo/reset`
           deletes the rows the old id pointed at without writing the new one.  So a teacher who
           presses 重置 is dropped back on the three-step wizard holding a journey that does exist
           on the server.  Handing over the id the server just returned is not an invented trip --
           it is the trip `demo/reset` answered with. */
        const reachedActive = advance?.journeyStatus === "ACTIVE";
        /* Written whenever the server really created a journey.  Requiring ACTIVE here looked
           safer and was a false gate: with a seeded ACTIVE journey already in the database the
           single-ACTIVE-plan rule refuses the advance, so the id was never handed over and the
           teacher stayed in the wizard -- the exact defect ORD-0078 (a) names.  My own node
           `sim-reset-hands-the-stage-to-today` caught this by going red, not by being relaxed.
           Whether the stage is executable is said in the outcome line, not used to hide the id. */
        if (result.status === "reset" && handOverStage(result.journey_id)) {
          /* A moment to read the line, then the shell re-boots onto the journey it now names; the
             longer pause on a refused advance is so the refusal itself is readable first. */
          window.setTimeout(
            () => window.location.reload(),
            reachedActive ? 1_200 : 2_400,
          );
        }
        if (!advance) {
          return {
            zh: `服务器回的是 ${result.status}，但没有报上推进结果，所以这一步到底到没到可执行状态，我们不说。`,
            en: `The server answered ${result.status} with no advance report, so whether the stage is executable is not claimed here.`,
          };
        }
        if (refused) {
          return {
            zh: `服务器重新播了场景（行程 ${shortId(result.journey_id)}），但推进到可执行状态被挡在「${refused.step}」（${refused.refused}）：这趟现在的状态是 ${advance.journeyStatus ?? "未知"}。`,
            en: `The server re-seeded (journey ${shortId(result.journey_id)}) but the advance was refused at "${refused.step}" (${refused.refused}): the trip is now ${advance.journeyStatus ?? "unknown"}.`,
          };
        }
        return {
          zh: `服务器重新播了场景并走完真实流水线：行程 ${shortId(result.journey_id)} · 状态 ${advance.journeyStatus ?? "未知"} · 计划 ${shortId(advance.selectedPlanVersionId)} · 用时 ${advance.totalMs ?? 0}ms。`,
          en: `The server re-seeded and ran the real pipeline: journey ${shortId(result.journey_id)} · status ${advance.journeyStatus ?? "unknown"} · plan ${shortId(advance.selectedPlanVersionId)} · ${advance.totalMs ?? 0}ms.`,
        };
      },
    },
    {
      id: "prepare",
      zh: "准备舞台（生成并选定计划）",
      en: "Put a plan on the stage",
      blocked:
        notReady ??
        noJourney ??
        (scene.scene?.journeyStatus === "ACTIVE"
          ? {
              zh: "服务器说这趟已经在执行中，不需要再准备一次。",
              en: "The server says this journey is already executing; preparing again is not the step.",
            }
          : null),
      run: async () => {
        const report = await runStageAction("prepare-scenario");
        handOverStage(report.journeyId);
        const refused = Boolean(report.code);
        return {
          refused,
          zh: refused
            ? `服务器拒绝了这一步（具名 ${report.code}），行程状态仍是 ${report.journeyStatus ?? "未知"}。`
            : `服务器已把计划放上舞台：状态 ${report.journeyStatus ?? "未知"} · 计划 ${shortId(report.planVersionId)}${report.advance?.totalMs != null ? ` · ${report.advance.totalMs}ms` : ""}。`,
          en: refused
            ? `The server refused this step (code ${report.code}); the journey is still ${report.journeyStatus ?? "unknown"}.`
            : `The server put a plan on the stage: status ${report.journeyStatus ?? "unknown"} · plan ${shortId(report.planVersionId)}${report.advance?.totalMs != null ? ` · ${report.advance.totalMs}ms` : ""}.`,
        };
      },
    },
    {
      id: "book",
      zh: bookTarget
        ? `建一个硬承诺：${bookTarget.title}`
        : "建一个硬承诺",
      en: bookTarget ? `Book a commitment: ${bookTarget.title}` : "Book a commitment",
      /* ORD-0100 ①: the target is an activity the server gave a slot for, named on the row before the
         tap; 换一个活动 moves between those, not between everything.  When the server refuses anyway,
         the traveller reads its own sentence, not this screen's guess. */
      blocked:
        notReady ??
        noJourney ??
        notPlanned ??
        bookReadBlocked ??
        nothingBookable ??
        (!bookTarget
          ? {
              zh: "服务器给的这份计划里没有可下单的活动，所以这里没有可建承诺的东西。",
              en: "The plan the server gave carries no activity that can be ordered, so there is nothing here to book.",
            }
          : null),
      run: async () => {
        const target = bookTarget as { id: string; title: string };
        try {
          const booking = await cityNexusApi.createBooking(
            journeyId as string,
            target.id,
          );
          return {
            zh: `服务器建了承诺 ${shortId(booking.id)}（${target.title} · ${booking.status} · ${formatMinorMoney(booking.amount.amount_minor, booking.amount.currency, locale)}）。`,
            en: `The server created commitment ${shortId(booking.id)} (${target.title} · ${booking.status} · ${formatMinorMoney(booking.amount.amount_minor, booking.amount.currency, locale)}).`,
          };
        } catch (error) {
          const named = describeBookingRefusal(error, target.title);
          return { refused: true, zh: named.zh, en: named.en };
        }
      },
    },
    {
      id: "start",
      zh: "开始执行（真实 /start）",
      en: "Put the journey into execution",
      blocked:
        notReady ??
        noJourney ??
        (scene.scene?.journeyStatus === "ACTIVE"
          ? {
              zh: "服务器说这趟已经在执行中，不需要再开始一次。",
              en: "The server says this journey is already executing.",
            }
          : !planVersionId
            ? {
                zh: "舞台上还没有计划（服务器回的 plan_version_id 是空的）。先按上面「准备舞台」，才有可开始的东西。",
                en: "No plan on the stage yet (the server's plan_version_id is empty). Put a plan on it first; that is what there is to start.",
              }
            : null),
      run: async () => {
        const report = await runStageAction("start-scenario");
        handOverStage(report.journeyId);
        const refused = Boolean(report.code);
        return {
          refused,
          zh: refused
            ? `服务器拒绝了这一步（具名 ${report.code}），行程状态仍是 ${report.journeyStatus ?? "未知"}。`
            : `服务器说这趟开始执行了：状态 ${report.journeyStatus ?? "未知"} · 用时 ${report.advance?.totalMs ?? 0}ms。游标在哪一站，由它下面那行说。`,
          en: refused
            ? `The server refused this step (code ${report.code}); the journey is still ${report.journeyStatus ?? "unknown"}.`
            : `The server put the journey into execution: status ${report.journeyStatus ?? "unknown"} · ${report.advance?.totalMs ?? 0}ms. Where the cursor sits is its line to say, not mine.`,
        };
      },
    },
    {
      id: "advance",
      zh: nextStep ? `推进当前活动：${nextStep.zh}` : "推进当前活动",
      en: nextStep
        ? `Advance the current activity: ${nextStep.en}`
        : "Advance the current activity",
      blocked: notReady ?? noJourney ?? cursorless ?? stepBlocked,
      run: async () => {
        if (!nextStep || !currentActivity)
          throw new Error("NO_NEXT_STEP_OFFERED");
        const after = await transitionStage(
          journeyId as string,
          currentActivity.id,
          nextStep.step,
        );
        setLastEventId(null);
        return {
          zh: `服务器接了「${nextStep.zh}」这一转移（原来这项活动是 ${currentActivity.status}）：游标 ${shortId(after.current_activity_id)}，投影 ${after.projection_status}。新状态由它说，不由我说。`,
          en: `The server took the "${nextStep.en}" transition (the activity was ${currentActivity.status}): cursor ${shortId(after.current_activity_id)}, projection ${after.projection_status}. Its state, not my guess.`,
        };
      },
    },
    {
      id: "event",
      zh: "触发一次中断",
      en: "Raise a disruption",
      blocked: notReady ?? noJourney,
      run: async () => {
        const created = await postSimulatedEvent(journeyId as string, {
          eventType: "TRAFFIC_DELAY",
          severity: "MAJOR",
          activityId: twin.data?.current_activity_id ?? null,
        });
        setLastEventId(created.id);
        return {
          zh: `服务器记下一次中断 ${shortId(created.id)}，发生时间按它自己的 ${sceneInstant(created.occurred_at)}；来源由服务器判定为模拟。`,
          en: `The server recorded disruption ${shortId(created.id)} at its own ${sceneInstant(created.occurred_at)}; provenance is assigned server-side as simulated.`,
        };
      },
    },
    {
      id: "replan",
      zh: "请求重规划",
      en: "Ask for a replan",
      blocked:
        notReady ??
        noJourney ??
        noPlan ??
        (lastEventId
          ? null
          : {
              zh: "重规划要由一个中断触发（服务器要求 trigger_event_id）。先按上面「触发一次中断」。",
              en: "A replan needs a triggering event (the server requires trigger_event_id). Raise a disruption above first.",
            }),
      run: async () => {
        await cityNexusApi.generateReplans(
          journeyId as string,
          lastEventId as string,
          3,
          newKey(),
        );
        /* ORD-0100 ②: this sentence used to print the id the POST answered with, while 接受方案 took
           its id from `GET /replans` -- and one POST leaves more than one proposal on the stage
           (measured: printed `350b9e3c`, accepted `f6691a42`, and the printed one later read
           INVALIDATED).  So the receipt no longer quotes the POST's id at all: it asks the same
           question the accept row asks, and writes the answer into the very cache that row reads, so
           the number on screen and the number in the request are one read apart from each other, not
           two reads apart from the truth. */
        const listed = await cityNexusApi.listReplans(journeyId as string);
        client.setQueryData(["sim-replans", journeyId], listed);
        const open = listed.filter(
          (item) =>
            !["ACCEPTED", "REJECTED", "DECLINED", "SUPERSEDED"].includes(
              String(item.status ?? "").toUpperCase(),
            ),
        );
        return {
          zh: open.length
            ? `服务器回了 ${listed.length} 份方案，其中 ${open.length} 份还开着；下面「接受方案」那一行现在拿的是 ${shortId(open[0].id)}。接受才会换计划。`
            : `服务器回了方案，但它现在列出的 ${listed.length} 份里没有还开着的那一份，所以「接受方案」这一行会说不给按——理由在它自己那行。`,
          en: open.length
            ? `The server answered with ${listed.length} proposals, ${open.length} still open; "Accept the proposal" below now points at ${shortId(open[0].id)}. Accepting is what changes the plan.`
            : `The server produced proposals, but none of the ${listed.length} it now lists is open, so "Accept the proposal" will refuse and say why on its own line.`,
        };
      },
    },
    {
      id: "accept",
      /* ORD-0100 ②: the row says which proposal it will accept.  The receipt used to name one id and
         this row send another, because the id came from the list while the sentence came from the POST
         -- one read, one name, printed where the press goes. */
      zh: proposalId ? `接受方案：${shortId(proposalId)}` : "接受方案",
      en: proposalId
        ? `Accept the proposal: ${shortId(proposalId)}`
        : "Accept the proposal",
      blocked:
        notReady ??
        noJourney ??
        (proposalId
          ? null
          : {
              zh: "服务器还没有给出方案（它回的列表是空的），所以这里没有可接受的东西。",
              en: "The server has not offered a proposal (its list is empty), so there is nothing to accept.",
            }),
      run: async () => {
        const accepted = await cityNexusApi.acceptReplan(
          proposalId as string,
          keyFor(`accept:${proposalId}`),
        );
        return {
          zh: `服务器已接受 ${shortId(accepted.proposal.id)}，新 ACTIVE 计划 ${shortId(accepted.twin.active_plan_version_id ?? null)}。`,
          en: `The server accepted ${shortId(accepted.proposal.id)}; new ACTIVE plan ${shortId(accepted.twin.active_plan_version_id ?? null)}.`,
        };
      },
    },
    {
      id: "reject",
      zh: proposalId ? `拒绝方案：${shortId(proposalId)}` : "拒绝方案",
      en: proposalId
        ? `Reject the proposal: ${shortId(proposalId)}`
        : "Reject the proposal",
      blocked:
        notReady ??
        noJourney ??
        (proposalId
          ? null
          : {
              zh: "服务器还没有给出方案（它回的列表是空的），所以这里没有可拒绝的东西。",
              en: "The server has not offered a proposal (its list is empty), so there is nothing to reject.",
            }),
      run: async () => {
        const rejected = await cityNexusApi.rejectReplan(
          proposalId as string,
          keyFor(`reject:${proposalId}`),
        );
        return {
          zh: `服务器记下了拒绝 ${shortId(rejected.id)}，原计划不变。`,
          en: `The server recorded the rejection of ${shortId(rejected.id)}; the plan stands.`,
        };
      },
    },
    {
      id: "degrade",
      zh: "提供方降级",
      en: "Degrade the provider",
      blocked: notReady ?? noJourney,
      run: async () => {
        const fixture = await cityNexusApi.setGuardianProviderFixture(
          journeyId as string,
          "OUTAGE",
        );
        return {
          zh: `服务器把信号夹具设为 OUTAGE${fixture?.state ? `（现值 ${fixture.state}）` : ""}。这只影响这台演示舞台，不是真实世界变了。`,
          en: `The server set the signal fixture to OUTAGE${fixture?.state ? ` (now ${fixture.state})` : ""}. This moves the stage only, not the real world.`,
        };
      },
    },
    {
      id: "restore",
      zh: "提供方恢复",
      en: "Restore the provider",
      blocked: notReady ?? noJourney,
      run: async () => {
        const fixture = await cityNexusApi.setGuardianProviderFixture(
          journeyId as string,
          "NORMAL",
        );
        return {
          zh: `服务器把信号夹具设回 NORMAL${fixture?.state ? `（现值 ${fixture.state}）` : ""}。`,
          en: `The server returned the fixture to NORMAL${fixture?.state ? ` (now ${fixture.state})` : ""}.`,
        };
      },
    },
  ];

  const act = async (row: Row) => {
    if (row.blocked || running) {
      setOutcome({
        ok: false,
        /* ORD-0086: a refusal about a read that has not landed is only true of *that* read.  Without
           the stamp there was no way to tell it apart from a refusal about the world, and the
           "还在读" sentence stayed up after the scene answered 200. */
        transient: Boolean(row.blocked?.transient) || Boolean(running),
        sceneStamp: scene.updatedAt,
        zh: row.blocked
          ? `这个动作现在做不了：${row.blocked.zh}`
          : "上一个动作还没回来，先等它。",
        en: row.blocked
          ? `This action is unavailable right now: ${row.blocked.en}`
          : "The previous action has not returned yet.",
      });
      return;
    }
    setRunning(row.id);
    setOutcome(null);
    try {
      const result = await row.run();
      setOutcome({
        ok: !result.refused,
        transient: false,
        sceneStamp: scene.updatedAt,
        zh: result.zh,
        en: result.en,
      });
    } catch (error) {
      const named = describeFailure(error);
      setOutcome({
        ok: false,
        transient: false,
        sceneStamp: scene.updatedAt,
        zh: named.zh,
        en: named.en,
      });
    } finally {
      setRunning(null);
      refreshAll(row.id);
    }
  };

  return (
    <section
      className="cn-sim"
      data-testid="sim-panel"
      aria-labelledby="sim-banner"
      aria-busy={scene.phase === "loading" ? "true" : undefined}
    >
      {onClose && (
        <button
          type="button"
          className="v8-quiet-button cn-sim-close"
          data-testid="sim-close"
          onClick={onClose}
        >
          {zh ? "收起舞台" : "Close the stage"}
        </button>
      )}
      <p
        className="cn-sim-banner"
        id="sim-banner"
        data-testid="sim-banner"
        data-scene-phase={scene.phase}
        role="status"
      >
        {scene.phase === "loading" &&
          (zh
            ? "正在读服务器上的场景状态…"
            : "Reading the stage state from the server…")}
        {scene.phase === "unavailable" &&
          (zh
            ? `场景状态没读到：${scene.reason ?? "未知"}。`
            : `The stage state could not be read: ${scene.reason ?? "unknown"}.`)}
        {scene.phase === "ready" &&
          scene.scene?.demoMode &&
          (zh
            ? `这是教学模拟场景，不是真实出行提醒 · 屏幕上的“现在”由服务器给定：${sceneInstant(scene.scene.effectiveNow)}`
            : `This is a teaching simulation, not live travel advice · the server supplies "now": ${sceneInstant(scene.scene.effectiveNow)}`)}
        {scene.phase === "ready" &&
          !scene.scene?.demoMode &&
          (zh
            ? "这台部署没有开模拟舞台，下面的动作不会被执行"
            : "This deployment has no simulation stage switched on, so the actions below will not run")}
      </p>
      {/* The flag itself stays available -- the declaration may be in human words, but a lane that
          needs to know which switch the server answered must not have to guess. */}
      <p className="cn-sim-diag" data-testid="sim-diag">
        {`demo_mode=${scene.scene ? String(scene.scene.demoMode) : "—"} · basis=${scene.scene?.basis ?? "—"} · evidence=${scene.scene?.evidenceState ?? "—"}`}
      </p>

      {scene.phase === "unavailable" && (
        <button
          type="button"
          className="v8-outline-button cn-sim-retry"
          data-testid="sim-retry"
          onClick={scene.refresh}
        >
          {zh ? "再读一次" : "Read it again"}
        </button>
      )}

      <dl className="cn-sim-facts" data-testid="sim-scene-facts">
        <div>
          <dt>{zh ? "台上行程" : "On stage"}</dt>
          <dd data-testid="sim-journey">{`${shortId(scene.scene?.journeyId ?? null)} · ${scene.scene?.journeyStatus ?? "—"}`}</dd>
        </div>
        <div>
          <dt>{zh ? "场景窗口" : "Scene window"}</dt>
          <dd data-testid="sim-window">{`${sceneInstant(scene.scene?.sceneStartAt ?? null)} → ${sceneInstant(scene.scene?.sceneEndAt ?? null)}`}</dd>
        </div>
        <div>
          <dt>{zh ? "依据" : "Basis"}</dt>
          <dd data-testid="sim-basis">
            {basisWord(scene.scene?.basis ?? null, zh)}
          </dd>
        </div>
        <div>
          <dt>{zh ? "证据" : "Evidence"}</dt>
          <dd data-testid="sim-evidence">{`${evidenceWord(scene.scene?.evidenceState ?? null, zh)} · ${
            scene.scene?.evidenceExpiresAt
              ? sceneInstant(scene.scene.evidenceExpiresAt)
              : zh
                ? "没有到期时间"
                : "no expiry"
          }`}</dd>
        </div>
      </dl>

      {viewingJourneyId && journeyId && viewingJourneyId !== journeyId && (
        <p
          className="cn-sim-mismatch"
          data-testid="sim-journey-mismatch"
          role="status"
        >
          {zh
            ? `注意：台上是行程 ${shortId(journeyId)}，而你现在这屏看的是 ${shortId(viewingJourneyId)}。下面的动作打在台上那一趟。`
            : `Note: the stage holds journey ${shortId(journeyId)} while this screen is showing ${shortId(viewingJourneyId)}. The actions below hit the one on stage.`}
        </p>
      )}

      <ul className="cn-sim-actions" data-testid="sim-actions">
        {rows.map((row) => {
          const reasonId = `sim-reason-${row.id}`;
          const busy = running !== null && running !== row.id;
          const blocked =
            row.blocked ??
            (busy
              ? {
                  zh: "上一个动作还没回来。",
                  en: "The previous action is still running.",
                }
              : null);
          return (
            <li key={row.id} className="cn-sim-row">
              <button
                type="button"
                className="v8-outline-button cn-sim-button"
                data-testid={`sim-action-${row.id}`}
                aria-busy={running === row.id ? "true" : undefined}
                aria-disabled={blocked ? "true" : undefined}
                aria-describedby={blocked ? reasonId : undefined}
                onClick={() => void act(row)}
              >
                {running === row.id
                  ? zh
                    ? `正在执行：${row.zh}`
                    : `Running: ${row.en}`
                  : zh
                    ? row.zh
                    : row.en}
              </button>
              {/* The way out of a refusal that names the activity: move the booking row to the next
                  candidate the server's plan lists, instead of pressing the same 422 again. */}
              {row.id === "book" && bookCandidates.length > 1 && !notPlanned && (
                <button
                  type="button"
                  className="v8-quiet-button cn-sim-book-next"
                  data-testid="sim-book-next"
                  onClick={() =>
                    setBookIndex((bookIndexClamped + 1) % bookCandidates.length)
                  }
                >
                  {zh
                    ? `换一个活动（下一个：${bookNext?.title ?? "—"}）`
                    : `Try another activity (next: ${bookNext?.title ?? "—"})`}
                </button>
              )}
              {blocked && (
                <p
                  className="cn-sim-reason"
                  id={reasonId}
                  data-testid={`sim-blocked-${row.id}`}
                  role="status"
                >
                  {zh ? blocked.zh : blocked.en}
                </p>
              )}
            </li>
          );
        })}
      </ul>

      {outcome && (
        <p
          className={outcome.ok ? "cn-sim-outcome" : "cn-sim-refusal"}
          data-testid="sim-outcome"
          data-outcome={outcome.ok ? "ok" : "refused"}
          data-transient={outcome.transient ? "true" : "false"}
          role={outcome.ok ? "status" : "alert"}
        >
          {zh ? outcome.zh : outcome.en}
        </p>
      )}
      {outcome && !outcome.ok && (
        <p className="cn-sim-outcome-note" data-testid="sim-outcome-note">
          {zh
            ? "界面没有替服务器圆场：这一步没有发生，行程保持原样。"
            : "Nothing is smoothed over: this step did not happen and the trip is unchanged."}
        </p>
      )}
    </section>
  );
}

/**
 * The one surface the shell mounts: a ball, and nothing else until it is pressed.
 *
 * It replaced an inline `<details>` block that was either a wall of eleven rows over the page or
 * invisible, which is what the traveller meant by "别把屏幕挡住".  Closed at rest is therefore the
 * default at every width, and the declaration the stage owes the reader lives in the ball's
 * accessible name and in the window's first line rather than on a collapsed summary.
 */
/**
 * Where the bottom of the screen actually is.
 *
 * The first version of this widget parked itself at a number copied from the app's reserved band
 * (`124px + safe-area`), and the full suite answered with two reds: on the wizard the ball covered the
 * sticky primary CTA by 12-16px, and it sat inside the Copilot pill's resting band, so the pill yielded
 * to it and -- being a permanent fixture rather than content passing by -- never came back.
 * So the clearance is measured off the fixed chrome itself: the widget rests above the highest top edge
 * it can find, and re-reads it when the layout changes.
 */
function useBottomChromeClearance(active: boolean) {
  const [bottom, setBottom] = useState<number | null>(null);
  useEffect(() => {
    if (!active || typeof window === "undefined") return;
    const measure = () => {
      const selectors = [".v8-mobile-nav", ".cn-copilot-dock", ".trial-sticky-cta"];
      let top = Number.POSITIVE_INFINITY;
      for (const selector of selectors) {
        for (const element of document.querySelectorAll(selector)) {
          const box = element.getBoundingClientRect();
          if (box.width < 1 || box.height < 1) continue;
          top = Math.min(top, box.top);
        }
      }
      if (!Number.isFinite(top)) return setBottom(null);
      setBottom(Math.max(12, window.innerHeight - top + 12));
    };
    let timer = 0;
    const schedule = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(measure, 220);
    };
    measure();
    /* The CTA on the journey-creation screen mounts *after* this widget, and no scroll or resize
       follows, so listening to the window alone left the ball 8px inside it. A mutation observer is
       what notices the chrome arriving. */
    const observed =
      typeof MutationObserver === "function"
        ? new MutationObserver(schedule)
        : null;
    observed?.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["class", "style", "hidden"],
    });
    window.addEventListener("resize", schedule);
    window.addEventListener("scroll", schedule, { passive: true });
    return () => {
      window.clearTimeout(timer);
      observed?.disconnect();
      window.removeEventListener("resize", schedule);
      window.removeEventListener("scroll", schedule);
    };
  }, [active]);
  return bottom;
}

export function SimulationEntry({
  viewingJourneyId = null,
  onStageChanged = null,
  defaultOpen = null,
}: {
  viewingJourneyId?: string | null;
  onStageChanged?: ((action: ActionId) => void) | null;
  /** `true` opens the window on first paint; `null`/`false` leave the ball closed, which is the
      pose on a phone where a wall of rows over the content was the thing being complained about. */
  defaultOpen?: boolean | null;
}) {
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const scene = useDemoScene();
  /* Unlike the old <details>, this disclosure has no default action fighting React: the click that
     opens it is the only thing that writes the flag. */
  const [open, setOpen] = useState<boolean>(() => defaultOpen ?? false);
  /* Where the fixed bottom chrome ends, so the ball never rests on the nav, the Copilot pill or the
     wizard's sticky primary CTA. */
  const floatBottom = useBottomChromeClearance(true);
  const rootRef = useRef<HTMLDivElement | null>(null);

  /* Tap outside closes. Listening only while open keeps the ball's own click from being eaten by
     the same gesture that opened it. */
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const root = rootRef.current;
      if (root && !root.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  /* Escape closes from wherever the focus happens to be.  It used to be a React `onKeyDown` on this
     wrapper, which only sees the key when the event's target is inside the widget -- and a traveller
     who taps into the window's own text loses focus from the ball to `body`, so the key did nothing.
     Measured on the desktop as `closedByEsc=false`.  Registering on the document while open is the
     one place that claim stops being true; on a phone nothing depends on it, since the ball, the row
     inside the window and a tap outside all close it without a keyboard. */
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open]);

  /* The line the stage owes the traveller: that this is a teaching simulation, and whose clock
     "now" comes from. It used to sit on the collapsed line; a 44px ball cannot carry a sentence, so
     it now lives in the window and in the ball's accessible name, and the dot only says
     "there is a stage on stage" or "there isn't". */
  const declared =
    scene.phase === "loading"
      ? zh
        ? "正在读服务器上的场景状态…"
        : "Reading the stage state from the server…"
      : scene.phase === "unavailable"
        ? zh
          ? `场景状态没读到：${scene.reason ?? "未知"}`
          : `The stage state could not be read: ${scene.reason ?? "unknown"}`
        : scene.scene?.demoMode
          ? zh
            ? `教学模拟舞台 · 屏幕上的“现在”由服务器给定：${sceneInstant(scene.scene.effectiveNow)}`
            : `teaching simulation stage · the server supplies "now": ${sceneInstant(scene.scene.effectiveNow)}`
          : zh
            ? "这台部署没有开模拟舞台"
            : "this deployment has no simulation stage switched on";
  const dotState =
    scene.phase === "loading"
      ? "CHECKING"
      : scene.phase === "unavailable"
        ? "UNREADABLE"
        : scene.scene?.demoMode
          ? "STAGE_LIVE"
          : "NO_STAGE";
  const ballName = zh
    ? `模拟舞台（${dotState === "STAGE_LIVE" ? "舞台上有场景" : "暂无场景"}）：${declared}`
    : `Simulation stage (${dotState === "STAGE_LIVE" ? "a scenario is on stage" : "no scenario yet"}): ${declared}`;

  /* The portal is not a style preference. `design-tokens.css` gives `.v8-page` and every child of
     it `animation: cn-rise … both`, which leaves an identity transform in place forever, and any
     transformed ancestor becomes the containing block for `position: fixed` -- measured as the
     last attempt at this widget parking itself at top -374 and riding the page off screen. On
     document.body the ball belongs to the viewport, which is what "悬浮" means. */
  return createPortal(
    <div
      ref={rootRef}
      className="cn-sim-float"
      data-testid="sim-float"
      data-open={open ? "true" : "false"}
      data-scene-phase={scene.phase}
      style={floatBottom === null ? undefined : { bottom: `${floatBottom}px` }}
    >
      {open ? (
        <div
          id="cn-sim-pop"
          className="v8-page cn-sim-pop"
          data-testid="sim-pop"
          role="group"
          aria-label={ballName}
        >
          <p className="cn-sim-pop-decl" data-testid="sim-entry-decl">
            {declared}
          </p>
          <div className="cn-sim-pop-body" data-testid="sim-pop-body">
            <SimulationPanel
              viewingJourneyId={viewingJourneyId}
              onStageChanged={onStageChanged}
              onClose={() => setOpen(false)}
            />
          </div>
        </div>
      ) : null}
      <button
        type="button"
        className="cn-sim-ball"
        data-testid="sim-ball"
        data-scene-phase={scene.phase}
        aria-expanded={open}
        aria-controls="cn-sim-pop"
        aria-label={ballName}
        onClick={() => setOpen((value) => !value)}
      >
        <span
          className="cn-sim-ball-dot"
          data-testid="sim-ball-dot"
          data-state={dotState}
          aria-hidden="true"
        />
      </button>
    </div>,
    document.body,
  );
}
