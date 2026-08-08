import { Button, ProgressBar, Tag } from '@carbon/react';
import { useMemo, useState } from 'react';

import type { ContextLabViewerExport } from '../data/contract';
import { ArtifactLink, CitationLink, MetricLink } from '../components/ProvenanceLink';
import { EmptyState } from '../components/RuntimeStates';
import { EvidenceCallout, RunIdentity, RunPicker, ViewHeader } from '../components/ViewPrimitives';

function EvidencePipelineContent({ data }: { data: ContextLabViewerExport }) {
  const [runId, setRunId] = useState(data.runs[0].id);
  const [stageIndex, setStageIndex] = useState(0);
  const runById = useMemo(() => new Map(data.runs.map((run) => [run.id, run])), [data.runs]);
  const run = runById.get(runId) ?? data.runs[0];
  const stage = run.pipeline.stages[stageIndex] ?? run.pipeline.stages[0];
  const budgetPercent = Math.min(
    100,
    (run.pipeline.contextUsed.value / Math.max(run.pipeline.contextBudget.value, 1)) * 100,
  );

  function selectRun(nextRunId: string): void {
    setRunId(nextRunId);
    setStageIndex(0);
  }

  return (
    <section aria-labelledby="pipeline-heading" className="view-stack">
      <ViewHeader
        actions={
          <RunPicker data={data} id="pipeline-run" onChange={selectRun} runId={run.id} />
        }
        description="Follow saved candidates through retrieval, fusion, reranking, removal, and final context selection."
        title="Evidence pipeline"
      />
      <EvidenceCallout insight={data.showcase.retrievalWin} />
      <RunIdentity run={run} />
      <section className="budget-panel" aria-label="Saved context budget">
        <div className="budget-panel__metrics">
          <MetricLink label="Budget" metric={run.pipeline.contextBudget} />
          <MetricLink label="Used" metric={run.pipeline.contextUsed} />
        </div>
        <ProgressBar
          label="Context budget consumption"
          max={100}
          size="small"
          status="active"
          value={budgetPercent}
        />
      </section>
      <nav aria-label="Evidence pipeline stages" className="pipeline-stage-nav">
        {run.pipeline.stages.map((item, index) => (
          <Button
            aria-pressed={stageIndex === index}
            key={item.id}
            kind={stageIndex === index ? 'primary' : 'ghost'}
            onClick={() => setStageIndex(index)}
            size="sm"
          >
            {item.label}
          </Button>
        ))}
      </nav>
      <div aria-live="polite" className="pipeline-stage" key={`${run.id}-${stage.id}`}>
        <header className="pipeline-stage__header">
          <div>
            <p>{stage.kind}</p>
            <h2 id="pipeline-heading">{stage.label}</h2>
          </div>
          <ArtifactLink artifact={stage.artifact} />
        </header>
        <div className="table-scroll" role="region" aria-label={`${stage.label} candidates`} tabIndex={0}>
          <table className="cds--data-table cds--data-table--compact pipeline-table">
            <thead>
              <tr>
                <th scope="col">Evidence</th>
                <th scope="col">Origin</th>
                <th scope="col">Rank</th>
                <th scope="col">Stage score</th>
                <th scope="col">Tokens</th>
                <th scope="col">Decision</th>
                <th scope="col">Context order</th>
              </tr>
            </thead>
            <tbody>
              {stage.candidates.map((candidate) => (
                <tr key={candidate.id}>
                  <td>
                    <CitationLink citation={candidate.citation} />
                  </td>
                  <td>
                    <Tag size="sm" type="cool-gray">{candidate.origin}</Tag>
                  </td>
                  <td>
                    <MetricLink compact label="Rank" metric={candidate.rank} />
                  </td>
                  <td>
                    {candidate.score ? (
                      <MetricLink compact label="Score" metric={candidate.score} />
                    ) : (
                      <span className="muted-copy">Not recorded</span>
                    )}
                  </td>
                  <td>
                    <MetricLink compact label="Tokens" metric={candidate.tokenCount} />
                  </td>
                  <td>
                    <Tag size="sm" type={candidate.decision === 'kept' ? 'green' : 'red'}>
                      {candidate.decision}
                    </Tag>
                    {candidate.reason ? <span className="decision-reason">{candidate.reason}</span> : null}
                  </td>
                  <td>
                    {candidate.contextOrder ? (
                      <MetricLink compact label="Order" metric={candidate.contextOrder} />
                    ) : (
                      <span className="muted-copy">Not selected</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

export default function EvidencePipeline({ data }: { data: ContextLabViewerExport }) {
  if (data.runs.length === 0) {
    return (
      <EmptyState
        detail="The export must include a saved evidence pipeline for each replayable run."
        title="No pipeline runs are present"
      />
    );
  }
  return <EvidencePipelineContent data={data} />;
}
