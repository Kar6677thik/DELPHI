import { lazy, Suspense, useMemo } from 'react';

import type { HypothesisStyles } from '../components/HypothesisBar';
import type { ReplayHypothesis } from './replay';

const Oracle = lazy(() => import('./Oracle').then((m) => ({ default: m.Oracle })));

const UNEXPLAINED = 'UNEXPLAINED';
const GENUINE = 'GENUINE';
const UNSTYLED = '#8A8C92';

let webglAvailable: boolean | undefined;

/** Whether this browser can draw WebGL (it can't in jsdom, or with the GPU switched off).
 * Checked once per page load. */
export function hasWebGL(): boolean {
  if (webglAvailable === undefined) {
    try {
      const canvas = document.createElement('canvas');
      webglAvailable = Boolean(canvas.getContext('webgl2') ?? canvas.getContext('webgl'));
    } catch {
      webglAvailable = false;
    }
  }
  return webglAvailable;
}

/** The KB's hypotheses as the Oracle's wells: GENUINE first (the far left, so belief swings
 * across the stage as in the Theatre), then the KB's own order, Unexplained last (the centre). */
export function wellsOf(
  posterior: Record<string, number>,
  styles: HypothesisStyles,
): ReplayHypothesis[] {
  const kbOrder = Object.keys(styles);
  const rank = (key: string) =>
    key === GENUINE ? -1 : key === UNEXPLAINED ? kbOrder.length + 1 : kbOrder.indexOf(key);
  const keys = Object.keys(posterior).sort((a, b) => rank(a) - rank(b));
  return keys.map((key) => ({
    key,
    name: styles[key]?.name ?? (key === UNEXPLAINED ? 'Unexplained' : key),
    color: styles[key]?.color ?? UNSTYLED,
  }));
}

/**
 * A live session's belief as the Oracle (the Theatre's act 1, on real data), in a card. Where
 * WebGL is missing it renders `fallback` instead, so the panel always shows the posterior.
 */
export function BeliefView({
  posterior,
  styles,
  decision,
  answers,
  fallback,
}: {
  posterior: Record<string, number>;
  styles: HypothesisStyles;
  decision: string | null;
  /** How many observations have come in: each new one sends a burst into the cloud. */
  answers: number;
  fallback: React.ReactNode;
}) {
  const webgl = hasWebGL();
  const keysKey = Object.keys(posterior).sort().join(',');
  const stylesKey = Object.keys(styles).sort().join(',');
  const hypotheses = useMemo(
    () => wellsOf(posterior, styles),
    // The wells change only when the set of hypotheses or their styles does.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [keysKey, stylesKey],
  );
  if (!webgl) return <>{fallback}</>;
  return (
    <div className="h-[360px] overflow-hidden rounded-md">
      <Suspense fallback={fallback}>
        <Oracle
          hypotheses={hypotheses}
          posterior={posterior}
          decision={decision}
          pulse={answers}
          background="#131217"
          distance={10.2}
          wide={1.75}
        />
      </Suspense>
    </div>
  );
}
