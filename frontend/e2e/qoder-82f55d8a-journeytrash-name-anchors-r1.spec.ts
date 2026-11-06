/* Name-anchor guard for the recycle-bin screen (lane 82f55d8a).
 *
 * Why this exists instead of a re-pinned number: `journeyTrash.ts` shipped line-number citations twice and
 * rotted twice -- ORD-0535's r8 re-measured all of them at one tip, and r9 had to move two of them again
 * after other lanes landed above `app.main`. A drifted line anchor does not fail; it quietly starts
 * describing whatever line landed there, which is worse than a red test. The ruling on this family is that
 * a shipped citation names its subject, so this file converts the last ten numbers to names and then keeps
 * the conversion honest:
 *
 *  A1  no `file.ext:<digits>` citation survives in the shipped file -- re-introducing one reddens here;
 *  A2  every name the comment quotes resolves in the file it points at, saying what the comment claims;
 *  A3  the shipped file still carries each of those names -- deleting a citation is not converting it.
 *
 * All three arms read source text: no browser, no backend, no store, no seed. A green here means the
 * screen's comments describe real code at *this* checkout; it says nothing about rendering.
 *
 * Not the acceptance gate: `playwright.config.ts` stays the only thing that can sign one.
 */
import { expect, test } from "@playwright/test";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SHIPPED = join(ROOT, "frontend", "src", "features", "trash", "journeyTrash.ts");
const FILE = "e2e/qoder-82f55d8a-journeytrash-name-anchors-r1.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const EVIDENCE = process.env.CITYNEXUS_NAMEANCHOR_EVIDENCE ?? "";
const RUN = process.env.CITYNEXUS_NAMEANCHOR_RUN ?? "nameanchor-unset";

/** Each entry: where the comment points, the string the comment quotes, and what that string proves. */
const ANCHORS: { file: string; needle: string; claim: string }[] = [
  {
    file: "backend/app/main.py",
    needle: '@app.post("/api/v1/journeys/{journey_id}/undelete',
    claim: "the write door exists as a POST on that path",
  },
  {
    file: "backend/app/main.py",
    needle: 'refused.reason_code = "JOURNEY_NOT_DELETED"',
    claim: "the refusal answers instead of a fake success",
  },
  {
    file: "backend/app/main.py",
    needle: '@app.get("/api/v1/recycle-bin/journeys", response_model=list[RecycleBinJourney])',
    claim: "the read door exists and answers the binned-row schema",
  },
  {
    file: "backend/app/main.py",
    needle: ".order_by(Journey.deleted_at.desc(), Journey.id)",
    claim: "the server, not the client, orders the list newest-deletion first",
  },
  {
    file: "backend/app/main.py",
    needle: "live = select(Journey).where(Journey.deleted_at.is_(None))",
    claim: "the live list keeps its deleted_at IS NULL promise",
  },
  {
    file: "contracts/openapi.yaml",
    needle: "/recycle-bin/journeys:",
    claim: "the read door is declared in the contract",
  },
  {
    file: "frontend/src/generated/api.ts",
    needle: '"/recycle-bin/journeys": {',
    claim: "the door is generated into the client this screen calls",
  },
  {
    file: "frontend/src/generated/api.ts",
    needle: 'RecycleBinJourney: components["schemas"]["Journey"] & {',
    claim: "the row type is typed from the contract, not re-declared",
  },
  {
    file: "backend/app/schemas.py",
    needle: "deleted_at: datetime",
    claim: "each row carries the stored deletion moment",
  },
  {
    file: "backend/app/schemas.py",
    needle: 'bin_state: Literal["IN_RECYCLE_BIN"]',
    claim: "and the bin state the screen must not infer",
  },
];

const read = (rel: string) => readFileSync(join(ROOT, rel), "utf8");
/**
 * A4 -- the door the comment *names* must be the door that *holds* the statement.
 *
 * A2 only proves a string exists somewhere in a file, and two of these strings do not live in one place:
 * `.order_by(Journey.deleted_at.desc(), Journey.id)` appears in the whole-site bin door and again in the
 * per-traveller door ORD-0531 shipped, and `/recycle-bin/journeys:` is a substring of the traveller path
 * key as well as a key of its own. Measured at tip 6246876a3b9171f5c2d6ec948f43b35605262bee: two of the ten
 * names hit more than one line. So a comment may quote a true string and still attribute it to the wrong
 * door -- which is the same class of lie as a drifted number, just quieter.
 */
const DOOR_CLAIMS: { file: string; statement: string; door: string; why: string }[] = [
  {
    file: "backend/app/main.py",
    statement: ".order_by(Journey.deleted_at.desc(), Journey.id)",
    door: '@app.get("/api/v1/recycle-bin/journeys"',
    why: "the ordering the screen inherits is the whole-site bin door's, not the per-traveller door's",
  },
  {
    file: "backend/app/main.py",
    statement: 'refused.reason_code = "JOURNEY_NOT_DELETED"',
    door: '@app.post("/api/v1/journeys/{journey_id}/undelete',
    why: "the refusal that answers instead of a fake success belongs to the write door",
  },
];

/** Regions in a Python source file, delimited by FastAPI decorator lines. */
function doorRegion(src: string, doorNeedle: string): string | null {
  const lines = src.split(/\r?\n/);
  const start = lines.findIndex((l) => l.includes(doorNeedle));
  if (start < 0) return null;
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i += 1) {
    if (/^@app\.(get|post|put|patch|delete)\(/.test(lines[i].trim())) {
      end = i;
      break;
    }
  }
  return lines.slice(start, end).join("\n");
}

test("A4 a named door actually contains the statement the comment attributes to it", () => {
  const text = shipped();
  const wrong: string[] = [];
  for (const claim of DOOR_CLAIMS) {
    if (!text.includes(claim.door)) {
      wrong.push(`the shipped comment no longer quotes the door ${JSON.stringify(claim.door)} it uses for ${JSON.stringify(claim.statement)}`);
      continue;
    }
    const region = doorRegion(read(claim.file), claim.door);
    if (region === null) {
      wrong.push(`${claim.file}: the door ${JSON.stringify(claim.door)} is not a decorator this file carries`);
      continue;
    }
    if (!region.includes(claim.statement)) {
      wrong.push(`${claim.file}: ${JSON.stringify(claim.statement)} is not inside ${JSON.stringify(claim.door)} -- ${claim.why}`);
    }
  }
  // A path key must be a key of its own, not a substring of a longer path.
  const openapi = read("contracts/openapi.yaml").split(/\r?\n/);
  const exact = openapi.filter((l) => l.trim() === "/recycle-bin/journeys:");
  if (exact.length !== 1) {
    wrong.push(`contracts/openapi.yaml: the bin door should own exactly one path key line, found ${exact.length}`);
  }
  // The two stored fields must live in the schema the screen types from.
  const schemas = read("backend/app/schemas.py");
  const from = schemas.indexOf("class RecycleBinJourney");
  if (from < 0) {
    wrong.push("backend/app/schemas.py: class RecycleBinJourney is gone -- the screen types its rows from it");
  } else {
    const rest = schemas.slice(from);
    const stop = rest.indexOf(String.fromCharCode(10, 10) + "class ");
    const body = stop > 0 ? rest.slice(0, stop) : rest;
    for (const field of ["deleted_at: datetime", 'bin_state: Literal["IN_RECYCLE_BIN"]']) {
      if (!body.includes(field)) {
        wrong.push(`backend/app/schemas.py: ${JSON.stringify(field)} is not inside class RecycleBinJourney -- a row would stop carrying it`);
      }
    }
  }
  expect(wrong, node("A4", "a claim names a door or schema that does not hold the fact it is quoted for")).toEqual([]);
});

const shipped = () => readFileSync(SHIPPED, "utf8");

test("A1 no line-number citation survives in the shipped file", () => {
  const digits = [...shipped().matchAll(/[\w./\\-]+\.(?:py|ts|tsx|js|json|yaml|yml|md):\d+/g)].map((m) => m[0]);
  expect(
    digits,
    node("A1", "a `file.ext:<digits>` citation is back; numbers rot silently, which is what this guard exists to stop"),
  ).toEqual([]);
});

test("A2 every quoted name resolves in the file it points at", () => {
  const missing = ANCHORS.filter((a) => !read(a.file).includes(a.needle));
  expect(
    missing.map((a) => `${a.file} no longer holds ${JSON.stringify(a.needle)} (${a.claim})`),
    node("A2", "a shipped comment names something the server-side code does not contain"),
  ).toEqual([]);
});

test("A3 the shipped file still names each of those anchors", () => {
  const text = shipped();
  const lost = ANCHORS.filter((a) => !text.includes(a.needle));
  expect(
    lost.map((a) => `journeyTrash.ts no longer quotes ${JSON.stringify(a.needle)}`),
    node("A3", "a citation was deleted rather than converted; the screen's claims are now unanchored"),
  ).toEqual([]);
  expect(ANCHORS.length, node("A3", "the anchor ledger itself shrank")).toBe(10);
  if (EVIDENCE) {
    mkdirSync(dirname(EVIDENCE), { recursive: true });
    writeFileSync(
      EVIDENCE,
      JSON.stringify(
        {
          generatedBy: "82f55d8a",
          guard: "journeytrash-name-anchors-r1",
          run: RUN,
          anchors: ANCHORS.map((a) => ({ ...a, resolves: read(a.file).includes(a.needle), quoted: text.includes(a.needle) })),
          digit_citations_left: [...text.matchAll(/[\w./\\-]+\.(?:py|ts|tsx|js|json|yaml|yml|md):\d+/g)].map((m) => m[0]),
          shipped_bytes: Buffer.byteLength(text),
        },
        null,
        1,
      ),
      "utf8",
    );
  }
});
