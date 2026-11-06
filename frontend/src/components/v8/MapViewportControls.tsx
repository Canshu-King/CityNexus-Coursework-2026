import { useEffect, useRef, useState } from "react";
import "../../map-controls.css";

/**
 * ORD-0403 — the one named control on the traveller's map.
 *
 * Before this the v8 map had no control at all (`MapPanel` drew markers and a caption and nothing
 * else), while the evidence map used maplibre's stock `NavigationControl`
 * (`components/JourneyMap.tsx`). Two surfaces, two unrelated answers about "how do I get back".
 * This is a single named component both can be measured against.
 *
 * One button, two states, and the state is carried by the control itself rather than by a sentence
 * next to it: `data-viewport-state` is `on-target` when the camera currently sits on what the
 * journey wants in frame, and `off-target` when the traveller has dragged or zoomed away.
 *
 * What actually changes per state: `data-viewport-state`, the glyph (a filled ring against a ring
 * crossed by a bar), and the accessible name — which is a different sentence in each state, and the
 * gate asserts that it differs rather than trusting this comment. The visible caption is the action
 * name and stays constant on purpose: it is what pressing does, and a control that rewrites its own
 * label and resizes on every pan is harder to aim at than one whose glyph changed.
 *
 * No third-party asset is used: the ring and the crosshair are drawn from our own borders and
 * pseudo-elements, sized in the project's own spacing tokens.
 */

export type ViewportState = "on-target" | "off-target";

export function MapViewportControls({
  state,
  onResetView,
  labels,
}: {
  state: ViewportState;
  onResetView: () => void;
  /** Copy comes from the caller's copy table so this component owns no strings of its own. */
  labels: { resetToJourney: string; offTarget: string; onTarget: string };
}) {
  const [pressed, setPressed] = useState(false);
  const timer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );
  const on = state === "on-target";
  return (
    <div className="v8-map-viewport-controls" data-testid="map-viewport-controls">
      <button
        type="button"
        className="v8-map-viewport-control"
        data-testid="map-viewport-reset"
        data-viewport-state={state}
        aria-label={on ? labels.onTarget : labels.offTarget}
        aria-pressed={pressed}
        onClick={() => {
          onResetView();
          setPressed(true);
          if (timer.current !== null) window.clearTimeout(timer.current);
          timer.current = window.setTimeout(() => setPressed(false), 1_200);
        }}
      >
        <span className="v8-map-viewport-control-glyph" aria-hidden="true" />
        <span className="v8-map-viewport-control-label">{labels.resetToJourney}</span>
      </button>
    </div>
  );
}
