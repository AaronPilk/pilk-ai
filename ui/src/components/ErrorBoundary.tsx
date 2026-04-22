import { Component, type ErrorInfo, type ReactNode } from "react";

/** Contain render-time exceptions so a broken subtree doesn't blank the
 * whole page. Used around BrainGraph because the force-graph canvas has
 * a history of throwing on edge cases (huge disconnected vaults,
 * library version mismatches) and we'd rather show a message than
 * unmount the entire Brain route.
 */
interface Props {
  fallback: (err: Error, reset: () => void) => ReactNode;
  children: ReactNode;
}

interface State {
  err: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { err: null };

  static getDerivedStateFromError(err: Error): State {
    return { err };
  }

  componentDidCatch(err: Error, info: ErrorInfo) {
    console.error("ErrorBoundary caught:", err, info);
  }

  reset = () => this.setState({ err: null });

  render() {
    if (this.state.err) {
      return this.props.fallback(this.state.err, this.reset);
    }
    return this.props.children;
  }
}
