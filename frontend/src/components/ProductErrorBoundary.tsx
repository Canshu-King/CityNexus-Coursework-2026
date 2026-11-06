/**
 * Route/product error boundary.
 *
 * A runtime error in any panel must never trap the traveller on a white screen.
 * The boundary always explains what happened, reassures that trip data is safe,
 * and offers retry / back-to-today / reload.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = { children: ReactNode };

/* ORD-0323 cell 1 (Q64).  The technical fold used to print two things it was handed: the failure's own
   prose and React's component stack.  Either one can be somebody else's words -- a panel that renders a
   reply it could not read is exactly where a server sentence turns into an exception message -- and the
   stack names component and file paths the traveller has no use for.  What this boundary may state on its
   own is what this device knows at the moment it catches: when, on which route, how many times, and
   whether the device is online. */
type Fields = {
  caught_at: string;
  route: string;
  failures_caught_here: number;
  online_at_this_device: boolean;
};
type State = { error: Error | null; fields: Fields | null };

/* The fold is where a machine reading belongs, so the reading is written out even when a value never
   arrived -- a missing measurement shows as a named field with no value, never as a silent blank. */
function fieldsText(fields: Fields | null): string {
  return [
    `caught_at=${fields?.caught_at ?? ""}`,
    `route=${fields?.route ?? ""}`,
    `failures_caught_here=${fields ? fields.failures_caught_here : ""}`,
    `online_at_this_device=${fields ? (fields.online_at_this_device ? "yes" : "no") : ""}`,
    "failure_text_rendered=no",
  ].join("\n");
}

const COPY = {
  en: {
    title: "Something went wrong here",
    body: "Your trip data has not been lost. You can retry this screen or go back to Today.",
    retry: "Try again",
    home: "Back to Today",
    reload: "Reload the app",
    detail: "Technical detail",
  },
  zh: {
    title: "这里出了点问题",
    body: "你的行程数据没有丢失。可以重试当前页面，或回到「今天」。",
    retry: "重试",
    home: "返回今天",
    reload: "重新加载",
    detail: "技术详情",
  },
} as const;

function pick() {
  const lang = (typeof navigator !== "undefined" && navigator.language) || "en";
  return lang.toLowerCase().startsWith("zh") ? COPY.zh : COPY.en;
}

export class ProductErrorBoundary extends Component<Props, State> {
  state: State = { error: null, fields: null };
  private caught = 0;

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(_error: Error, _info: ErrorInfo) {
    /* Both arguments are deliberately not read.  React writes the failure and its frames to this
       device's console on its own; copying them into state is what put them on the screen. */
    this.caught += 1;
    this.setState({
      fields: {
        caught_at: new Date().toISOString(),
        route: window.location.pathname,
        failures_caught_here: this.caught,
        online_at_this_device: Boolean(navigator.onLine),
      },
    });
  }

  private reset = () => this.setState({ error: null, fields: null });

  private goHome = () => {
    try {
      window.history.pushState({}, "", "/v8/hub");
      window.dispatchEvent(new PopStateEvent("popstate"));
    } catch {
      window.location.assign("/v8/hub");
    }
    this.reset();
  };

  render() {
    const { error, fields } = this.state;
    if (!error) return this.props.children;
    const t = pick();
    return (
      <section
        className="v8-page"
        data-testid="product-error-boundary"
        role="alert"
        aria-live="assertive"
        style={{ padding: "24px 18px", maxWidth: 520, margin: "0 auto" }}
      >
        <h1 style={{ fontSize: 20, marginBottom: 8 }}>{t.title}</h1>
        <p style={{ opacity: 0.8, lineHeight: 1.5 }}>{t.body}</p>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 16 }}>
          <button
            type="button"
            data-testid="error-retry"
            className="v8-primary-button"
            onClick={this.reset}
          >
            {t.retry}
          </button>
          <button
            type="button"
            data-testid="error-home"
            className="v8-outline-button"
            onClick={this.goHome}
          >
            {t.home}
          </button>
          <button
            type="button"
            data-testid="error-reload"
            className="v8-outline-button"
            onClick={() => window.location.reload()}
          >
            {t.reload}
          </button>
        </div>
        <details style={{ marginTop: 18, opacity: 0.7, fontSize: 12 }}>
          <summary>{t.detail}</summary>
          <pre style={{ whiteSpace: "pre-wrap" }} data-testid="error-detail">
            {fieldsText(fields)}
          </pre>
        </details>
      </section>
    );
  }
}
