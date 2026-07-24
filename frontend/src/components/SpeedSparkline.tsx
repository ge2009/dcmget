import { useEffect, useMemo, useRef, useState } from 'react';

type Props = {
  value: unknown;
  taskId: string | number;
  sampleKey?: unknown;
  maxSamples?: number;
  className?: string;
  reducedMotion?: boolean;
};

const WIDTH = 120;
const HEIGHT = 32;
const PADDING = 2;

function numericSpeed(value: unknown): number {
  const speed = Number(value ?? 0);
  return Number.isFinite(speed) && speed > 0 ? speed : 0;
}

function usePrefersReducedMotion(override: boolean | undefined): boolean {
  const [preferred, setPreferred] = useState(() => {
    if (override != null) return override;
    return typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  });

  useEffect(() => {
    if (override != null) {
      setPreferred(override);
      return undefined;
    }
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return undefined;
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    const update = (event: MediaQueryListEvent | MediaQueryList) => setPreferred(event.matches);
    update(query);
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, [override]);

  return override ?? preferred;
}

function chartPoints(samples: number[]): Array<[number, number]> {
  const values = samples.length > 1 ? samples : [samples[0] ?? 0, samples[0] ?? 0];
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const span = maximum - minimum;
  return values.map((value, index) => {
    const x = PADDING + (index / Math.max(1, values.length - 1)) * (WIDTH - PADDING * 2);
    const ratio = span > 0 ? (value - minimum) / span : 0.5;
    const y = HEIGHT - PADDING - ratio * (HEIGHT - PADDING * 2);
    return [x, y];
  });
}

/**
 * A dependency-free, non-interactive trend view for the high-frequency runtime path.
 * Sampling is keyed explicitly so internal state updates never create a sampling loop.
 */
export function SpeedSparkline({
  value,
  taskId,
  sampleKey,
  maxSamples = 24,
  className = '',
  reducedMotion,
}: Props) {
  const speed = numericSpeed(value);
  const limit = Math.max(2, Math.floor(maxSamples));
  const [history, setHistory] = useState<number[]>(() => [speed]);
  const previousSample = useRef({ taskId, sampleKey, speed });
  const prefersReducedMotion = usePrefersReducedMotion(reducedMotion);

  useEffect(() => {
    const previous = previousSample.current;
    previousSample.current = { taskId, sampleKey, speed };
    if (previous.taskId !== taskId) {
      setHistory([speed]);
      return;
    }
    if (Object.is(previous.sampleKey, sampleKey) && Object.is(previous.speed, speed)) return;
    setHistory((samples) => [...samples, speed].slice(-limit));
  }, [limit, sampleKey, speed, taskId]);

  const visibleSamples = prefersReducedMotion ? [speed] : history;
  const points = useMemo(() => chartPoints(visibleSamples), [visibleSamples]);
  const polyline = points.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(' ');
  const area = `M ${points[0][0].toFixed(2)} ${HEIGHT - PADDING} L ${polyline.replaceAll(',', ' ')} L ${points[points.length - 1][0].toFixed(2)} ${HEIGHT - PADDING} Z`;
  const last = points[points.length - 1];

  return <svg
    className={`speed-sparkline ${className}`.trim()}
    viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
    preserveAspectRatio="none"
    focusable="false"
    aria-hidden="true"
    data-reduced-motion={prefersReducedMotion ? 'true' : 'false'}
    data-sample-count={visibleSamples.length}
  >
    <path className="speed-sparkline__area" d={area} />
    <polyline className="speed-sparkline__line" points={polyline} />
    <circle className="speed-sparkline__point" cx={last[0]} cy={last[1]} r="1.75" />
  </svg>;
}
