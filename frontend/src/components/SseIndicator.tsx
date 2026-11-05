import type { StreamStatus } from '../lib/useEventStream';

const LABEL: Record<StreamStatus, string> = {
  connecting: 'Connecting…',
  connected: 'Connected',
  reconnecting: 'Reconnecting…',
};

const DOT: Record<StreamStatus, string> = {
  connecting: 'bg-slate-400',
  connected: 'bg-emerald-600',
  reconnecting: 'bg-amber-500',
};

/** The live-updates state in the header (docs/18 §4). The text carries it, not the colour. */
export function SseIndicator({ status }: { status: StreamStatus }) {
  return (
    <span role="status" aria-live="polite" className="flex items-center gap-2 text-sm">
      <span aria-hidden="true" className={`size-2.5 rounded-full ${DOT[status]}`} />
      <span>
        <span className="sr-only">Live updates: </span>
        {LABEL[status]}
      </span>
    </span>
  );
}
