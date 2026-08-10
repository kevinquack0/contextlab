import { Select, SelectItem } from '@carbon/react';
import { useMemo, useState, type ChangeEvent } from 'react';

import type { ContextLabViewerExport, RunRecord } from '../data/contract';
import TemporalStrata from '../components/TemporalStrata';
import { CitationLink, MetricLink } from '../components/ProvenanceLink';
import { EmptyState } from '../components/RuntimeStates';
import { EvidenceCallout, RunIdentity, ViewHeader } from '../components/ViewPrimitives';

function ExecutionComparison({
  label,
  run,
  emphasis = false,
}: {
  label: string;
  run: RunRecord;
  emphasis?: boolean;
}) {
  return (
    <article className={emphasis ? 'execution-comparison execution-comparison--emphasis' : 'execution-comparison'}>
      <header>
        <p className="instrument-kicker">{label}</p>
        <RunIdentity run={run} />
      </header>
      <p className="execution-comparison__answer">{run.answer.text}</p>
      <MetricLink compact label="Saved context" metric={run.metrics.contextTokens} />
      <div className="citation-stack">
        {run.answer.citations.map((citation) => (
          <CitationLink citation={citation} key={citation.id} />
        ))}
      </div>
    </article>
  );
}

function TimeMachineContent({ data }: { data: ContextLabViewerExport }) {
  const [caseId, setCaseId] = useState(data.temporalEvidenceCases[0].id);
  const [eventIndex, setEventIndex] = useState(0);
  const caseById = useMemo(
    () => new Map(data.temporalEvidenceCases.map((item) => [item.id, item])),
    [data.temporalEvidenceCases],
  );
  const runById = useMemo(() => new Map(data.runs.map((run) => [run.id, run])), [data.runs]);
  const item = caseById.get(caseId) ?? data.temporalEvidenceCases[0];
  const baselineRun = runById.get(item.baselineRunId);
  const memoryRun = runById.get(item.memoryEvidenceRunId);

  function selectCase(nextId: string): void {
    setCaseId(nextId);
    setEventIndex(0);
  }

  return (
    <section aria-labelledby="time-heading" className="view-stack time-view">
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
        description="Scrub through saved claims, authority changes, and supersession without erasing the earlier record."
        title="Time machine"
      />

      <EvidenceCallout insight={data.showcase.temporalEvidence} />

      <section className="time-case-intro">
        <p className="instrument-kicker">Saved event sequence</p>
        <h2 id="time-heading">{item.title}</h2>
      </section>

      <TemporalStrata item={item} onSelect={setEventIndex} selectedIndex={eventIndex} />

      {baselineRun || memoryRun ? (
        <section className="execution-comparisons" aria-labelledby="comparison-heading">
          <header className="instrument-heading">
            <div>
              <p className="instrument-kicker">Effect on the answer</p>
              <h2 id="comparison-heading">The same question, two evidence states</h2>
              <p>Compare the saved corpus-only execution with the memory-enabled execution that can carry the active event.</p>
            </div>
          </header>
          <div className="execution-comparisons__grid">
            {baselineRun ? <ExecutionComparison label="Corpus only" run={baselineRun} /> : null}
            {memoryRun ? <ExecutionComparison emphasis label="Memory evidence visible" run={memoryRun} /> : null}
          </div>
        </section>
      ) : null}
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
