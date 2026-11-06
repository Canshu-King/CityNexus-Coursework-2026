/**
 * ORD-0472 — draws the trip poster and hands back one PNG.
 *
 * Nothing here goes to the network, adds a dependency, or borrows a renderer: the picture is painted
 * with the product's own `canvas` context, and every size and colour on it is read out of
 * `design-tokens.css` from the live document, so the poster takes the traveller's light/dark tokens
 * instead of carrying a second palette. The one number this file owns is `DEVICE_SCALE`, which is a
 * sharpness factor and not a layout decision.
 *
 * A drawing is only reported as a file after the raster has been read back pixel by pixel: a canvas
 * that encoded a valid PNG of nothing returns `BlankAfterDraw`, because an empty picture under the
 * words "export done" is the failure this order names.
 */

/** What the sheet shows and what the picture must show -- the same strings, so they cannot diverge. */
export type PosterLines = {
  kicker: string;
  title: string;
  subtitle: string;
  progress: string;
  attribution: string;
  days: Array<{
    label: string;
    count: string;
    stops: Array<{ time: string; title: string }>;
  }>;
};

export const posterFailureSteps = [
  "NoCanvasContext",
  "NoMeasuredWidth",
  "MissingTokens",
  "BlankAfterDraw",
  "NoPngBytes",
  "NoDownloadRoute",
] as const;

export type PosterFailureStep = (typeof posterFailureSteps)[number];

export type PosterDraw = {
  ok: true;
  blob: Blob;
  /** Raster pixels, not CSS pixels. */
  width: number;
  height: number;
  /** Raster pixels that differ from the picture's own background colour. */
  paintedPixels: number;
  /** Text runs actually drawn. */
  runsDrawn: number;
  /** Day blocks, and the drawn breaks between them: `blocks - 1` unless there is one block. */
  dayBlocks: number;
  dayDividers: number;
  /** The zero-based page actually drawn, how many pages this trip takes, and the bound. */
  page_index: number;
  page_count: number;
  limit_css: number;
};

export type PosterDrawFailure = { ok: false; step: PosterFailureStep; detail: string };

export type PosterDrawResult = PosterDraw | PosterDrawFailure;

/** Two times the sheet's own CSS width: a share image that stays legible when zoomed. */
const DEVICE_SCALE = 2;

/**
 * The one height bound this feature owns, named because a bound nobody can point at is not a bound.
 *
 * It is a technical ceiling, not a taste judgement: a canvas sits on an engine-side surface, and
 * past a few thousand device pixels an export can come back short or empty without the page saying
 * so. Splitting is therefore done by whole day blocks -- "one block per day" stays true -- and a day
 * that alone exceeds the bound is never cut: it gets a page to itself.
 */
export const POSTER_MAX_RASTER_HEIGHT_PX = 4096;

/** The same bound expressed in the CSS pixels the layout works in. */
export const POSTER_MAX_HEIGHT_CSS = POSTER_MAX_RASTER_HEIGHT_PX / DEVICE_SCALE;

const SIZE_TOKENS = [
  "--cn-s-4",
  "--cn-s-6",
  "--cn-s-8",
  "--cn-s-10",
  "--cn-s-12",
  "--cn-s-16",
  "--cn-s-24",
  "--cn-s-32",
  "--cn-r-card",
  "--cn-fs-caption",
  "--cn-fs-meta",
  "--cn-fs-secondary",
  "--cn-fs-card",
  "--cn-fs-title",
  /* Unitless, unlike the rest of this list: it is the line-height multiplier the shell already uses,
     and the poster wraps lines with it rather than inventing its own leading. */
  "--cn-lh-body",
] as const;

const COLOR_TOKENS = [
  "--cn-canvas",
  "--cn-surface",
  "--cn-ink",
  "--cn-ink-secondary",
  "--cn-ink-tertiary",
  "--cn-hairline",
] as const;

type Scale = Record<(typeof SIZE_TOKENS)[number], number>;
/* Painted with the token's own text, never with a re-built `rgb(...)`: `--cn-hairline` is
   `rgba(0, 0, 0, 0.08)`, and dropping that alpha is what made every card outline come out solid
   black in the first drawn picture. Only the background is parsed into numbers, and only because
   the blank-canvas check compares pixel values against it. */
type Palette = Record<(typeof COLOR_TOKENS)[number], string>;

function cssPalette():
  | { scale: Scale; palette: Palette; family: string; background: [number, number, number] }
  | { missing: number } {
  const css = getComputedStyle(document.documentElement);
  const scale = {} as Scale;
  const palette = {} as Palette;
  let missing = 0;
  for (const token of SIZE_TOKENS) {
    const value = Number.parseFloat(css.getPropertyValue(token).trim());
    if (Number.isFinite(value)) scale[token] = value;
    else missing += 1;
  }
  for (const token of COLOR_TOKENS) {
    const value = css.getPropertyValue(token).trim();
    if (value) palette[token] = value;
    else missing += 1;
  }
  const family = css.getPropertyValue("--cn-font").trim();
  if (!family) missing += 1;
  const background = toRgb(palette["--cn-canvas"] ?? "");
  if (!background) missing += 1;
  return missing > 0 ? { missing } : { scale, palette, family, background };
}

function toRgb(color: string): [number, number, number] | null {
  const value = color.trim();
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(value);
  if (hex) {
    const digits =
      hex[1].length === 3 ? hex[1].split("").map((digit) => digit + digit).join("") : hex[1];
    const packed = Number.parseInt(digits, 16);
    return [(packed >> 16) & 255, (packed >> 8) & 255, packed & 255];
  }
  const functional = /^rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)/i.exec(value);
  if (functional) return [Number(functional[1]), Number(functional[2]), Number(functional[3])];
  return null;
}

/**
 * Greedy wrap that works for both scripts: Latin copy breaks at the spaces it has, Chinese copy has
 * none so it breaks at the character. No run is ever cut -- a stop whose name does not fit is a stop
 * the picture would claim has no name.
 */
function wrap(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string[] {
  const source = String(text ?? "").trim();
  if (!source) return [""];
  if (maxWidth <= 0) return [source];
  const hasSpaces = source.includes(" ");
  const units = hasSpaces ? source.split(/\s+/) : Array.from(source);
  const joiner = hasSpaces ? " " : "";
  const out: string[] = [];
  let line = "";
  for (const unit of units) {
    const candidate = line ? `${line}${joiner}${unit}` : unit;
    if (!line || ctx.measureText(candidate).width <= maxWidth) {
      line = candidate;
      continue;
    }
    out.push(line);
    line = unit;
  }
  if (line) out.push(line);
  return out.length ? out : [""];
}

function roundedRectPath(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  const radius = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.lineTo(x + w - radius, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
  ctx.lineTo(x + w, y + h - radius);
  ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
  ctx.lineTo(x + radius, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
  ctx.lineTo(x, y + radius);
  ctx.quadraticCurveTo(x, y, x + radius, y);
  ctx.closePath();
}

type Run = {
  text: string;
  x: number;
  /** Baseline. */
  y: number;
  size: number;
  weight: number;
  color: string;
};

type Card = { top: number; bottom: number };

/**
 * One pass in CSS pixels that produces the box height, the text runs and the day cards.
 *
 * It runs twice -- once to size the canvas, once after the resize, because resizing a canvas clears
 * its context -- which is why layout owns no colour state beyond the palette it is handed.
 */
function layoutPoster(
  ctx: CanvasRenderingContext2D,
  lines: PosterLines,
  width: number,
  scale: Scale,
  palette: Palette,
  family: string,
): { height: number; runs: Run[]; cards: Card[]; dayDividers: number[] } {
  const runs: Run[] = [];
  const cards: Card[] = [];
  const dayDividers: number[] = [];
  const padX = scale["--cn-s-24"];
  const inner = width - padX * 2;
  const lh = scale["--cn-lh-body"];
  let y = scale["--cn-s-32"];

  const setFont = (size: number, weight: number) => {
    ctx.font = `${weight} ${size}px ${family}`;
  };

  /** Writes one wrapped run and moves the cursor past it. Returns where the run started. */
  const write = (
    text: string,
    size: number,
    weight: number,
    color: string,
    x: number,
    maxWidth: number,
  ): number => {
    setFont(size, weight);
    const pieces = wrap(ctx, text, maxWidth);
    const step = size * lh;
    const top = y;
    for (const piece of pieces) {
      runs.push({ text: piece, x, y: top + size, size, weight, color });
      y += step;
    }
    return top;
  };

  write(lines.kicker, scale["--cn-fs-caption"], 600, palette["--cn-ink-tertiary"], padX, inner);
  y += scale["--cn-s-4"];
  write(lines.title, scale["--cn-fs-title"], 700, palette["--cn-ink"], padX, inner);
  y += scale["--cn-s-8"];
  write(lines.subtitle, scale["--cn-fs-secondary"], 400, palette["--cn-ink-secondary"], padX, inner);
  write(lines.progress, scale["--cn-fs-meta"], 400, palette["--cn-ink-secondary"], padX, inner);
  y += scale["--cn-s-24"];

  /* One time column for the whole picture, sized off the widest time it has to hold: per-row widths
     would stagger every name in the poster by whatever the trip's own clocks looked like. */
  setFont(scale["--cn-fs-meta"], 400);
  let timeWidth = 0;
  for (const day of lines.days) {
    for (const stop of day.stops) {
      timeWidth = Math.max(timeWidth, ctx.measureText(stop.time).width);
    }
  }
  const timeColumn = Math.min(timeWidth + scale["--cn-s-8"], inner * 0.38);

  for (let dayIndex = 0; dayIndex < lines.days.length; dayIndex += 1) {
    const day = lines.days[dayIndex];
    const cardTop = y;
    y += scale["--cn-s-16"];
    const headX = padX + scale["--cn-s-16"];
    const headWidth = inner - scale["--cn-s-16"] * 2;

    setFont(scale["--cn-fs-card"], 700);
    const labelWidth = ctx.measureText(day.label).width;
    setFont(scale["--cn-fs-caption"], 400);
    const countWidth = ctx.measureText(day.count).width;
    if (labelWidth + countWidth + scale["--cn-s-12"] <= headWidth) {
      const top = y;
      runs.push({
        text: day.label,
        x: headX,
        y: top + scale["--cn-fs-card"],
        size: scale["--cn-fs-card"],
        weight: 700,
        color: palette["--cn-ink"],
      });
      runs.push({
        text: day.count,
        x: headX + headWidth - countWidth,
        y: top + scale["--cn-fs-card"],
        size: scale["--cn-fs-caption"],
        weight: 400,
        color: palette["--cn-ink-tertiary"],
      });
      y += scale["--cn-fs-card"] * lh;
    } else {
      write(day.label, scale["--cn-fs-card"], 700, palette["--cn-ink"], headX, headWidth);
      write(day.count, scale["--cn-fs-caption"], 400, palette["--cn-ink-tertiary"], headX, headWidth);
    }
    y += scale["--cn-s-10"];

    for (const stop of day.stops) {
      const rowTop = y;
      setFont(scale["--cn-fs-meta"], 400);
      runs.push({
        text: stop.time,
        x: headX,
        y: rowTop + scale["--cn-fs-meta"],
        size: scale["--cn-fs-meta"],
        weight: 400,
        color: palette["--cn-ink-secondary"],
      });
      setFont(scale["--cn-fs-secondary"], 500);
      const pieces = wrap(ctx, stop.title, headX + headWidth - (headX + timeColumn));
      const step = scale["--cn-fs-secondary"] * lh;
      for (const piece of pieces) {
        runs.push({
          text: piece,
          x: headX + timeColumn,
          y: rowTop + scale["--cn-fs-secondary"],
          size: scale["--cn-fs-secondary"],
          weight: 500,
          color: palette["--cn-ink"],
        });
      }
      y = rowTop + step * pieces.length + scale["--cn-s-6"];
    }

    y += scale["--cn-s-16"];
    const cardBottom = y - scale["--cn-s-12"];
    cards.push({ top: cardTop, bottom: cardBottom });
    /* One block per day, and the break between two days is drawn, not hoped for. Inside a block the
       rhythm is `--cn-s-6`; between two days the gap is `--cn-s-32` with a hairline across it, so a
       reader -- and the pixel audit -- can tell "this is the next day" from "this is the next stop".
       Two days used to differ from one day only by extra whitespace, which photographed as one tall
       card and graded the layout by luck. */
    if (dayIndex + 1 < lines.days.length) {
      dayDividers.push(cardBottom + scale["--cn-s-16"]);
      y = cardBottom + scale["--cn-s-32"];
    } else {
      y = cardBottom + scale["--cn-s-16"];
    }
  }

  write(
    `${lines.attribution}`,
    scale["--cn-fs-caption"],
    400,
    palette["--cn-ink-secondary"],
    padX,
    inner,
  );
  y += scale["--cn-s-32"];

  return { height: Math.ceil(y), runs, cards, dayDividers };
}

export type PosterPagePlan = {
  ok: true;
  pages: Array<{ first: number; count: number }>;
  total_height_css: number;
  limit_css: number;
};
export type PosterPlanFailure = { ok: false; step: PosterFailureStep; detail: string };

/**
 * Groups whole day blocks into pages that each fit under `POSTER_MAX_HEIGHT_CSS`.
 *
 * The heights come from the same layout pass the picture is drawn from -- not from an estimate like
 * "roughly 40px a stop" -- so the page count a traveller reads is the page count the encoder makes.
 */
export function planPosterPages(
  lines: PosterLines,
  measuredWidthCss: number,
): PosterPagePlan | PosterPlanFailure {
  if (typeof document === "undefined") return { ok: false, step: "NoCanvasContext", detail: "" };
  const width = Math.floor(measuredWidthCss);
  if (!Number.isFinite(width) || width <= 0) {
    return { ok: false, step: "NoMeasuredWidth", detail: `${Math.round(measuredWidthCss || 0)}px` };
  }
  const probe = document.createElement("canvas").getContext("2d");
  if (!probe) return { ok: false, step: "NoCanvasContext", detail: "" };
  const read = cssPalette();
  if ("missing" in read) return { ok: false, step: "MissingTokens", detail: `${read.missing}` };
  const { scale, palette, family } = read;
  const whole = layoutPoster(probe, lines, width, scale, palette, family);
  if (whole.cards.length === 0) return { ok: false, step: "BlankAfterDraw", detail: "0" };

  /* Everything that is not a day block -- kicker, title, subtitle, progress, the source line and the
     padding -- is charged to every page, because every page draws it again. */
  const chrome = whole.cards[0].top + (whole.height - whole.cards[whole.cards.length - 1].bottom);
  const blockHeights = whole.cards.map((card) => card.bottom - card.top);
  const gap = scale["--cn-s-32"];

  const pages: Array<{ first: number; count: number }> = [];
  let start = 0;
  let used = chrome;
  for (let index = 0; index < blockHeights.length; index += 1) {
    const need = blockHeights[index] + (index > start ? gap : 0);
    if (index > start && used + need > POSTER_MAX_HEIGHT_CSS) {
      pages.push({ first: start, count: index - start });
      start = index;
      used = chrome + blockHeights[index];
    } else {
      used += need;
    }
  }
  pages.push({ first: start, count: blockHeights.length - start });
  return { ok: true, pages, total_height_css: whole.height, limit_css: POSTER_MAX_HEIGHT_CSS };
}

export async function drawTripPoster(
  lines: PosterLines,
  measuredWidthCss: number,
  pageIndex = 0,
): Promise<PosterDrawResult> {
  if (typeof document === "undefined") {
    return { ok: false, step: "NoCanvasContext", detail: "" };
  }
  const width = Math.floor(measuredWidthCss);
  if (!Number.isFinite(width) || width <= 0) {
    return { ok: false, step: "NoMeasuredWidth", detail: `${Math.round(measuredWidthCss || 0)}px` };
  }
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) return { ok: false, step: "NoCanvasContext", detail: "" };

  const read = cssPalette();
  if ("missing" in read) {
    return { ok: false, step: "MissingTokens", detail: `${read.missing}` };
  }
  const { scale, palette, family, background } = read;

  const planned = planPosterPages(lines, measuredWidthCss);
  if (!("pages" in planned)) return planned;
  const resolvedPage = Math.max(0, Math.min(pageIndex, planned.pages.length - 1));
  const page = planned.pages[resolvedPage];
  const pageLines: PosterLines = {
    ...lines,
    days: lines.days.slice(page.first, page.first + page.count),
  };

  canvas.width = width * DEVICE_SCALE;
  canvas.height = 1;
  const plan = layoutPoster(ctx, pageLines, width, scale, palette, family);
  if (
    !Number.isFinite(plan.height) ||
    plan.height <= 0 ||
    plan.runs.length === 0 ||
    plan.cards.length === 0
  ) {
    return { ok: false, step: "BlankAfterDraw", detail: "0" };
  }

  canvas.height = plan.height * DEVICE_SCALE;
  /* The resize clears the context, so every style below is set again after it. */
  ctx.setTransform(DEVICE_SCALE, 0, 0, DEVICE_SCALE, 0, 0);
  ctx.fillStyle = palette["--cn-canvas"];
  ctx.fillRect(0, 0, width, plan.height);

  for (const card of plan.cards) {
    roundedRectPath(
      ctx,
      scale["--cn-s-24"],
      card.top,
      width - scale["--cn-s-24"] * 2,
      card.bottom - card.top,
      scale["--cn-r-card"],
    );
    ctx.fillStyle = palette["--cn-surface"];
    ctx.fill();
    ctx.strokeStyle = palette["--cn-hairline"];
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  for (const dividerY of plan.dayDividers) {
    ctx.strokeStyle = palette["--cn-hairline"];
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(scale["--cn-s-24"], dividerY);
    ctx.lineTo(width - scale["--cn-s-24"], dividerY);
    ctx.stroke();
  }

  ctx.textBaseline = "alphabetic";
  for (const run of plan.runs) {
    ctx.font = `${run.weight} ${run.size}px ${family}`;
    ctx.fillStyle = run.color;
    ctx.fillText(run.text, run.x, run.y);
  }

  const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
  let painted = 0;
  for (let i = 0; i < pixels.length; i += 4) {
    if (
      Math.abs(pixels[i] - background[0]) > 6 ||
      Math.abs(pixels[i + 1] - background[1]) > 6 ||
      Math.abs(pixels[i + 2] - background[2]) > 6
    ) {
      painted += 1;
    }
  }
  if (painted === 0) return { ok: false, step: "BlankAfterDraw", detail: "0" };

  const blob = await toPngBlob(canvas);
  if (!blob) return { ok: false, step: "NoPngBytes", detail: "null" };
  if (blob.size === 0) return { ok: false, step: "NoPngBytes", detail: "0B" };

  return {
    ok: true,
    blob,
    width: canvas.width,
    height: canvas.height,
    paintedPixels: painted,
    runsDrawn: plan.runs.length,
    dayBlocks: plan.cards.length,
    dayDividers: plan.dayDividers.length,
    page_index: resolvedPage,
    page_count: planned.pages.length,
    limit_css: planned.limit_css,
  };
}

function toPngBlob(canvas: HTMLCanvasElement): Promise<Blob | null> {
  return new Promise((resolve) => {
    canvas.toBlob((blob) => resolve(blob), "image/png");
  });
}

/**
 * Hands the PNG to the device.
 *
 * False means this browser has no object-URL or no anchor-download path at all; the sheet names that
 * as its own step rather than reporting a file the traveller never received.
 */
export function savePosterBlob(blob: Blob, fileName: string): boolean {
  if (typeof document === "undefined" || typeof URL === "undefined" || !URL.createObjectURL) {
    return false;
  }
  const anchor = document.createElement("a");
  if (typeof anchor.download !== "string") return false;
  const url = URL.createObjectURL(blob);
  anchor.href = url;
  anchor.download = fileName;
  anchor.rel = "noopener";
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  // Revoked on a later task so the download has taken the bytes first.
  setTimeout(() => URL.revokeObjectURL(url), 2000);
  return true;
}
