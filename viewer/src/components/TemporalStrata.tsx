import type { KeyboardEvent } from 'react';

import type { ClaimEvent, TemporalEvidenceCase } from '../data/contract';
import { ArtifactLink, MetricLink } from './ProvenanceLink';

const WIDTH = 1180;
const HEIGHT = 500;
const PLOT_TOP = 86;
const PLOT_BOTTOM = 388;

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat('en-CA', {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    timeZone: 'UTC',
  }).format(date);
}

function pointFor(event: ClaimEvent, index: number, total: number): { x: number; y: number } {
  const x = total <= 1 ? WIDTH / 2 : 170 + (index * (WIDTH - 340)) / (total - 1);
  const normalizedAuthority = Math.max(0, Math.min(5, event.authority.value)) / 5;
  const y = PLOT_BOTTOM - normalizedAuthority * (PLOT_BOTTOM - PLOT_TOP);
  return { x, y };
}

function stratumPath(
  event: ClaimEvent,
  index: number,
  events: ClaimEvent[],
): string {
  const point = pointFor(event, index, events.length);
  const next = events[index + 1] ? pointFor(events[index + 1], index + 1, events.length) : null;
  const start = Math.max(72, point.x - (index === 0 ? 96 : 28));
  const end = event.state === 'active' ? WIDTH - 72 : Math.min(WIDTH - 72, (next?.x ?? point.x + 180) + 18);
  const half = event.state === 'active' ? 33 : 24;
  const shoulder = Math.min(78, Math.max(36, (end - start) * 0.18));

  return [
    `M ${start} ${point.y - half}`,
    `C ${start + shoulder} ${point.y - half - 8}, ${end - shoulder} ${point.y - half + 8}, ${end} ${point.y - half}`,
    `L ${end} ${point.y + half}`,
    `C ${end - shoulder} ${point.y + half + 8}, ${start + shoulder} ${point.y + half - 8}, ${start} ${point.y + half}`,
    'Z',
  ].join(' ');
}

function activateOnKey(event: KeyboardEvent<SVGGElement>, activate: () => void): void {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    activate();
  }
}

export default function TemporalStrata({
  item,
  selectedIndex,
  onSelect,
  headingId = 'strata-heading',
}: {
  item: TemporalEvidenceCase;
  selectedIndex: number;
  onSelect: (index: number) => void;
  headingId?: string;
}) {
  const selected = item.events[selectedIndex] ?? item.events[0];
  const selectedPoint = pointFor(selected, selectedIndex, item.events.length);

  return (
    <section className="temporal-strata" aria-labelledby={headingId}>
      <header className="instrument-heading">
        <div>
          <p className="instrument-kicker">Authority over time · saved event sequence</p>
          <h2 id={headingId}>{item.title}</h2>
          <p>Claims remain visible as history. Higher-authority evidence becomes the active governing layer.</p>
        </div>
        <ArtifactLink artifact={item.artifact} compact />
      </header>

      <div className="strata-stage">
        <div className="strata-canvas">
          <svg
            aria-describedby="strata-description"
            className="strata-svg"
            role="img"
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          >
            <title>{item.title}</title>
            <desc id="strata-description">
              A sequence of saved claims positioned by authority. Superseded claims remain visible while the active claim continues across the current horizon.
            </desc>
            <defs>
              <linearGradient id="active-stratum" x1="0" x2="1">
                <stop offset="0%" stopColor="#f0854a" stopOpacity="0.62" />
                <stop offset="72%" stopColor="#f0854a" stopOpacity="0.28" />
                <stop offset="100%" stopColor="#f0854a" stopOpacity="0.06" />
              </linearGradient>
              <linearGradient id="past-stratum" x1="0" x2="1">
                <stop offset="0%" stopColor="#837f74" stopOpacity="0.38" />
                <stop offset="100%" stopColor="#4a4740" stopOpacity="0.1" />
              </linearGradient>
            </defs>

            <g className="strata-grid" aria-hidden>
              {[1, 2, 3, 4, 5].map((authority) => {
                const y = PLOT_BOTTOM - (authority / 5) * (PLOT_BOTTOM - PLOT_TOP);
                return (
                  <g key={authority}>
                    <line x1="64" x2={WIDTH - 64} y1={y} y2={y} />
                    <text x="68" y={y - 9}>AUTHORITY {authority}</text>
                  </g>
                );
              })}
              <line className="strata-baseline" x1="64" x2={WIDTH - 64} y1={PLOT_BOTTOM + 42} y2={PLOT_BOTTOM + 42} />
            </g>

            <g className="strata-layers">
              {item.events.map((event, index) => (
                <path
                  className={event.state === 'active' ? 'strata-layer strata-layer--active' : 'strata-layer'}
                  d={stratumPath(event, index, item.events)}
                  fill={event.state === 'active' ? 'url(#active-stratum)' : 'url(#past-stratum)'}
                  key={event.id}
                />
              ))}
            </g>

            <line
              aria-hidden
              className="strata-selection-line"
              x1={selectedPoint.x}
              x2={selectedPoint.x}
              y1="54"
              y2={PLOT_BOTTOM + 43}
            />

            <g className="strata-events">
              {item.events.map((event, index) => {
                const point = pointFor(event, index, item.events.length);
                const isSelected = index === selectedIndex;
                return (
                  <g
                    aria-label={`${event.label}. ${event.state}. Authority ${event.authority.display}. Effective ${formatDate(event.effectiveAt)}.`}
                    aria-pressed={isSelected}
                    className={isSelected ? 'strata-event strata-event--selected' : 'strata-event'}
                    key={event.id}
                    onClick={() => onSelect(index)}
                    onKeyDown={(keyboardEvent) => activateOnKey(keyboardEvent, () => onSelect(index))}
                    role="button"
                    tabIndex={0}
                  >
                    <circle cx={point.x} cy={point.y} r={isSelected ? 12 : 9} />
                    <circle className="strata-event__core" cx={point.x} cy={point.y} r="4" />
                    <text className="strata-event__label" textAnchor="middle" x={point.x} y={PLOT_BOTTOM + 78}>
                      {event.id}
                    </text>
                    <text className="strata-event__date" textAnchor="middle" x={point.x} y={PLOT_BOTTOM + 99}>
                      {formatDate(event.effectiveAt)}
                    </text>
                  </g>
                );
              })}
            </g>
          </svg>
        </div>

        <article className="strata-readout" key={selected.id}>
          <div className="strata-readout__state">
            <span data-state={selected.state}>{selected.state}</span>
            <time dateTime={selected.effectiveAt}>{formatDate(selected.effectiveAt)}</time>
          </div>
          <p className="instrument-kicker">{selected.id}</p>
          <h3>{selected.label}</h3>
          <blockquote>{selected.claim}</blockquote>
          <MetricLink label="Source authority" metric={selected.authority} />
          <ArtifactLink artifact={selected.source} />
          <p className="strata-readout__chain">
            {selected.supersedesEventId
              ? `Supersedes ${selected.supersedesEventId}. The earlier claim stays visible as history.`
              : 'Starts this saved claim chain.'}
          </p>
        </article>
      </div>

      <div className="strata-scrubber">
        <label htmlFor="claim-time">Inspect saved event</label>
        <input
          aria-valuetext={`${selected.label}, ${selected.effectiveAt}`}
          id="claim-time"
          max={item.events.length - 1}
          min={0}
          onChange={(event) => onSelect(Number(event.target.value))}
          step={1}
          type="range"
          value={selectedIndex}
        />
        <div className="strata-scrubber__events">
          {item.events.map((event, index) => (
            <button
              aria-pressed={index === selectedIndex}
              key={event.id}
              onClick={() => onSelect(index)}
              type="button"
            >
              <span>{event.state}</span>
              <strong>{event.label}</strong>
            </button>
          ))}
        </div>
      </div>
    </section>
  );
}
