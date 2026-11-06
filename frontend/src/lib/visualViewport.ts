/**
 * The visible rectangle of the browser, published as CSS custom properties.
 *
 * `vh` and `dvh` resolve against the **layout** viewport, and a phone's keyboard or toolbar changes
 * only the **visual** one (WebKit ignores `interactive-widget=resizes-content` outright).  That gap is
 * why a bottom-anchored sheet bounded by `92vh` can paint its own action row below what the traveller
 * can see and still be scrollable -- the scrollport is the clipped box, so scrolling cannot rescue the
 * footer (ORD-0134 ①, measured: at 360x640 the sheet's `plan-ai-submit` centre sat at y=790 with
 * `elementFromPoint` returning nothing).
 *
 * So the sheet is sized from what is actually visible:
 *   --vv-height  the visible height in CSS px
 *   --vv-offset  how far the visible rectangle starts below the layout top
 *   --vv-bottom  the layout space taken below it (toolbar / keyboard), used to lift bottom-anchored
 *                overlays back into view
 *
 * Every consumer keeps a `100dvh` fallback, because `window.visualViewport` is absent outside
 * browsers that have it, and a sheet that reads `--vv-height` with no value must not collapse to zero.
 */

const VAR_HEIGHT = "--vv-height";
const VAR_OFFSET = "--vv-offset";
const VAR_BOTTOM = "--vv-bottom";

function publish(target: HTMLElement) {
  const viewport = window.visualViewport;
  if (!viewport) {
    /* No visual viewport to read: say nothing rather than write a wrong number, and let the CSS
       fallback decide. */
    return;
  }
  const height = Math.round(viewport.height);
  const offset = Math.round(viewport.offsetTop ?? 0);
  target.style.setProperty(VAR_HEIGHT, `${height}px`);
  target.style.setProperty(VAR_OFFSET, `${offset}px`);
  target.style.setProperty(VAR_BOTTOM, `${Math.max(0, Math.round(innerHeight - height - offset))}px`);
}

/**
 * Install once, at app boot.  Returns the teardown; nothing in the product reads these values back
 * into JavaScript -- they exist so a stylesheet can bound a surface by what the traveller can see.
 */
export function installVisualViewportVars(): () => void {
  if (typeof window === "undefined") return () => undefined;
  const target = document.documentElement;
  const update = () => publish(target);
  update();
  const viewport = window.visualViewport;
  viewport?.addEventListener("resize", update);
  viewport?.addEventListener("scroll", update);
  window.addEventListener("resize", update);
  return () => {
    viewport?.removeEventListener("resize", update);
    viewport?.removeEventListener("scroll", update);
    window.removeEventListener("resize", update);
  };
}
