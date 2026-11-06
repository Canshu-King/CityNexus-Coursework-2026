export const desktopRoutes = [
  { key: "hub", label: "Journey Hub", icon: "map", group: "TODAY" },
  { key: "live", label: "Live Journey", icon: "spark", group: "TODAY" },
  { key: "map", label: "Map", icon: "map", group: "TODAY" },
  { key: "studio", label: "Trip Studio", icon: "calendar", group: "PLAN" },
  { key: "stay", label: "Stay Explorer", icon: "bed", group: "PLAN" },
  { key: "discover", label: "City Discovery", icon: "map", group: "PLAN" },
  /* ORD-0437: the paste-a-note door. KNOWN_ROUTES is built from these two tables, so the key
     here is what makes `/v8/import` a screen instead of a URL that bounces to Today. */
  { key: "import", label: "Import places", icon: "map", group: "PLAN" },
  { key: "bookings", label: "Travel Wallet", icon: "ticket", group: "TRAVEL" },
  /* ORD-0476 slice 2: the footprint screen. A key in this table is what makes `/v8/footprints` a
     screen instead of a URL that bounces to Today (`KNOWN_ROUTES` is built from these two tables,
     app/V8Experience.tsx), and `nav.footprints` is added to both dictionary arms so the drawer
     cannot fall back to printing the key -- the fallback is `dictionaries.en[key] ?? key`.
     Deliberately not in `mobileRoutes`: that table is the five-item bottom bar, and a sixth item
     there is a crowding change this order does not own. The mobile route is reachable by URL and by
     the 打卡 control on each stop. */
  { key: "footprints", label: "Footprints", icon: "calendar", group: "TRAVEL" },
  { key: "agent", label: "Copilot", icon: "spark", group: "TRAVEL" },
  { key: "authoritative", label: "Recovery", icon: "spark", group: "SYSTEM" },
  { key: "intelligence", label: "Intelligence", icon: "spark", group: "SYSTEM" },
  { key: "twin", label: "Traveler Twin", icon: "calendar", group: "SYSTEM" },
  /* ORD-0402 slice 2: putting the key here is what makes `/v8/trash` a route instead of a
     dead URL -- KNOWN_ROUTES (app/V8Experience.tsx) is built from these two tables, and a
     key outside them sends a hard refresh back to Today. The panel is mounted from the
     registry, so the shell still holds no mount for it. */
  { key: "trash", label: "Recycle bin", icon: "calendar", group: "SYSTEM" },
  /* ORD-0436: the members-and-access screen. Same rule as the row above -- a key in this table is
     what makes `/v8/access` a route instead of a URL that bounces to Today, and the panel is
     mounted from the registry, so the shell keeps no mount for it. */
  { key: "access", label: "Access", icon: "user", group: "SYSTEM" },
] as const;

/* ORD-0465: routes addressed by a value, not just by a name. `/v8/shared/<token>` is the third thing
   a `/v8/` segment can be -- not a screen key, and not a journey id -- and it needs its own table
   rather than a row in `desktopRoutes` for two reasons. The drawer would put a "Shared trip" button in
   front of the traveller, and clicking it goes nowhere: there is nothing to share until an owner
   grants it, so the entry would advertise an action the product has not taken. And the keys in
   `desktopRoutes` / `mobileRoutes` feed `t("nav." + key)` (app/V8Experience.tsx, :1997) with no
   fallback hook, so a row here would demand a nav label for a control that must not be drawn.
   `KNOWN_ROUTES` still has to know the key, or a hard refresh on a share link lands on Today. */
export const tokenRoutes = [{ key: "shared" }] as const;

// The group code is a machine token; the heading a traveller reads comes from the dictionary.
export const navGroupKeys: Record<string, string> = {
  TODAY: "nav.groupToday",
  PLAN: "nav.groupPlan",
  TRAVEL: "nav.groupTravel",
  SYSTEM: "nav.groupSystem",
};

export function navGroupKey(group: string): string {
  return navGroupKeys[group] ?? "nav.groupOther";
}

// Official primary navigation (see docs/DESIGN_CORRECTIONS_REQUIRED.md §1).
// Wallet/bookings deliberately left the golden path; they now live under
// Trips ("预订与凭证") and Me ("钱包").
export const mobileRoutes = [
  { key: "hub", label: "Today", icon: "map" },
  { key: "trips", label: "Trips", icon: "calendar" },
  { key: "map", label: "Map", icon: "map" },
  { key: "agent", label: "Copilot", icon: "spark" },
  { key: "me", label: "Me", icon: "user" },
] as const;
