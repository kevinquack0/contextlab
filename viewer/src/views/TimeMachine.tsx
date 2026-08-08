import { Select, SelectItem, Tag } from '@carbon/react';
import { useMemo, useState, type ChangeEvent } from 'react';

import type { ContextLabViewerExport } from '../data/contract';
import { ArtifactLink, CitationLink, MetricLink } from '../components/ProvenanceLink';
import { EmptyState } from '../components/RuntimeStates';
import { EvidenceCallout, RunIdentity, ViewHeader } from '../components/ViewPrimitives';

function TimeMachineContent({ data }: { data: ContextLabViewerExport }) {
  const [caseId, setCaseId] = useState(data.temporalEvidenceCases[0].id);
  const [eventIndex, setEventIndex] = useState(0);
  const caseById = useMemo(
    () => new Map(data.temporalEvidenceCases.map((item) => [item.id, item])),
    [data.temporalEvidenceCases],
  );
  const runById = useMemo(() => new Map(data.runs.map((run) => [run.id, run])), [data.runs]);
  const item = caseById.get(caseId) ?? data.temporalEvidenceCases[0];
  const event = item.events[eventIndex] ?? item.events[0];
  const baselineRun = runById.get(item.baselineRunId);
  const memoryRun = runById.get(item.memoryEvidenceRunId);

  function selectCase(nextId: string): void {
    setCaseId(nextId);
    setEventIndex(0);
  }

  return (
    <section aria-labelledby="time-heading" className="view-stack">
      <ViewHeader
        actions={
          <Select
            id="time-case"
            labelText="Saved temporal case"
            onChange={(change: ChangeEvent<HTMLSelectElement>) => selectCase(change.target.value)}
            size="sm"
            value={item.id}
          >
            {data.temporalEvidenceCases.map((caseItem) => (
              <SelectItem key={caseItem.id} text={caseItem.title} value={caseItem.id} />
            ))}
          </Select>
        }
        description="Move through effective dates and authority changes in a linked public event sequence."
        title="Time machine"
      />
      <EvidenceCallout insight={data.showcase.temporalEvidence} />
      <div className="time-layout">
        <section className="timeline-panel">
          <header>
            <h2 id="time-heading">{item.title}</h2>
            <ArtifactLink artifact={item.artifact} />
          </header>
          <label htmlFor="claim-time">Claim state at saved event</label>
          <input
            aria-valuetext={`${event.label}, ${event.effectiveAt}`}
            id="claim-time"
            max={item.events.length - 1}
            min={0}
            onChange={(change) => setEventIndex(Number(change.target.value))}
            step={1}
            type="range"
            value={eventIndex}
          />
          <ol className="timeline-events">
            {item.events.map((timelineEvent, index) => (
              <li aria-current={eventIndex === index ? 'step' : undefined} key={timelineEvent.id}>
                <button onClick={() => setEventIndex(index)} type="button">
                  <span>{timelineEvent.effectiveAt}</span>
                  <strong>{timelineEvent.label}</strong>
                </button>
              </li>
            ))}
          </ol>
        </section>
        <article className="claim-card" key={event.id}>
          <div className="claim-card__meta">
            <Tag size="sm" type={event.state === 'active' ? 'green' : 'cool-gray'}>
              {event.state}
            </Tag>
            <span>{event.effectiveAt}</span>
          </div>
          <h2>{event.label}</h2>
          <p>{event.claim}</p>
          <MetricLink label="Source authority" metric={event.authority} />
          <ArtifactLink artifact={event.source} />
          <p className="claim-card__chain">
            {event.supersedesEventId
              ? `This event supersedes saved event ${event.supersedesEventId}.`
              : 'This event starts the saved claim chain.'}
          </p>
        </article>
      </div>
      <section className="memory-comparison" aria-label="Saved public execution comparison">
        {baselineRun ? (
          <article>
            <header>
              <Tag size="sm" type="cool-gray">M0 · corpus only</Tag>
              <h2>Baseline execution</h2>
            </header>
            <RunIdentity run={baselineRun} />
            <p>{baselineRun.answer.text}</p>
            <MetricLink label="Saved context" metric={baselineRun.metrics.contextTokens} />
            <div className="citation-stack">
              {baselineRun.answer.citations.map((citation) => (
                <CitationLink citation={citation} key={citation.id} />
              ))}
            </div>
          </article>
        ) : null}
        {memoryRun ? (
          <article>
            <header>
              <Tag size="sm" type="blue">Selected memory evidence</Tag>
              <h2>Memory-enabled execution</h2>
            </header>
            <RunIdentity run={memoryRun} />
            <p>{memoryRun.answer.text}</p>
            <MetricLink label="Saved context" metric={memoryRun.metrics.contextTokens} />
            <div className="citation-stack">
              {memoryRun.answer.citations.map((citation) => (
                <CitationLink citation={citation} key={citation.id} />
              ))}
            </div>
          </article>
        ) : null}
      </section>
    </section>
  );
}

export default function TimeMachine({ data }: { data: ContextLabViewerExport }) {
  if (data.temporalEvidenceCases.length === 0) {
    return (
      <EmptyState
        detail="Add a temporal case with linked public events and saved comparison run IDs."
        title="No temporal cases are present"
      />
    );
  }
  return <TimeMachineContent data={data} />;
}
