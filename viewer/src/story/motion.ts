import { useEffect, useRef, useState, type RefObject } from 'react';

/**
 * Story motion primitives.
 *
 * The Story never listens to `scroll`. Entrance choreography is driven by
 * IntersectionObserver and by CSS scroll-driven animations, so the main thread
 * stays free and the page still renders completely when either is unavailable.
 */

function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/**
 * Reports whether an element crossed into the viewport from below, as opposed
 * to being on screen from the start. Only a real crossing is worth animating;
 * anything already visible must render at its final state immediately.
 */
export function useScrolledIntoView<T extends HTMLElement>(): [RefObject<T | null>, boolean] {
  const ref = useRef<T>(null);
  const [crossed, setCrossed] = useState(false);

  useEffect(() => {
    const node = ref.current;
    if (!node || typeof IntersectionObserver === 'undefined') return;

    // IntersectionObserver always delivers one callback per observed target.
    // If that first report is already intersecting, the element was on screen
    // before the reader did anything, so nothing should animate.
    let first = true;
    const observer = new IntersectionObserver(
      (entries) => {
        const intersecting = entries.some((entry) => entry.isIntersecting);
        if (first) {
          first = false;
          if (intersecting) observer.disconnect();
          return;
        }
        if (intersecting) {
          setCrossed(true);
          observer.disconnect();
        }
      },
      { rootMargin: '0px 0px -10% 0px', threshold: 0.01 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return [ref, crossed];
}

/**
 * Counts a bound integer up to its recorded value. The exact figure is what
 * renders unless an animation is genuinely running, so a bound number is never
 * shown as a value it does not have.
 */
export function useOdometer(target: number, active: boolean, durationMs = 900): number {
  const [instant] = useState(
    () => prefersReducedMotion() || typeof requestAnimationFrame === 'undefined',
  );
  const [value, setValue] = useState<number | null>(null);

  useEffect(() => {
    if (!active || instant) return;

    let frame = 0;
    let start: number | null = null;
    const step = (now: number): void => {
      if (start === null) start = now;
      const progress = Math.min(1, (now - start) / durationMs);
      const eased = 1 - Math.pow(1 - progress, 3);
      setValue(progress < 1 ? Math.round(target * eased) : target);
      if (progress < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);

    // requestAnimationFrame is suspended in background tabs and can be throttled
    // elsewhere. This guarantees the exact recorded figure lands regardless, so a
    // stalled animation can never leave a bound value showing a number it isn't.
    const settle = window.setTimeout(() => setValue(target), durationMs + 250);

    return () => {
      cancelAnimationFrame(frame);
      window.clearTimeout(settle);
    };
  }, [active, durationMs, instant, target]);

  if (instant || value === null) return target;
  return value;
}

/**
 * Tracks which section is currently under the reader, for the section rail and
 * the header navigation. Uses IntersectionObserver rather than scroll offsets.
 */
export function useActiveSection(ids: readonly string[]): string {
  const [active, setActive] = useState(ids[0] ?? '');

  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return;
    const visible = new Map<string, number>();

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          visible.set(entry.target.id, entry.isIntersecting ? entry.intersectionRatio : 0);
        }
        let best = '';
        let bestRatio = 0;
        for (const id of ids) {
          const ratio = visible.get(id) ?? 0;
          if (ratio > bestRatio) {
            best = id;
            bestRatio = ratio;
          }
        }
        if (best) setActive(best);
      },
      { rootMargin: '-20% 0px -55% 0px', threshold: [0, 0.15, 0.4, 0.75, 1] },
    );

    for (const id of ids) {
      const node = document.getElementById(id);
      if (node) observer.observe(node);
    }
    return () => observer.disconnect();
  }, [ids]);

  return active;
}
