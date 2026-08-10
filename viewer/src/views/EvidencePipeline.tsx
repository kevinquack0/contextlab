import { useMemo, useState } from 'react';

import type { ContextLabViewerExport } from '../data/contract';
import { ArtifactLink, CitationLink, MetricLink } from '../components/ProvenanceLink';
import { EmptyState } from '../components/RuntimeStates';
import { EvidenceCallout, RunIdentity, RunPicker, ViewHeader } from '../components/ViewPrimitives';

/**
 * Stage labels arrive from the export as e.g. "Fusion (uninstrumented; bound to
 * trace artifact)". The qualifier matters but should not set the width of a
 * stepper, so it is split onto a second line. The full recorded label stays in
 * the accessible name.
 */
function splitStageLabel(label: string): { name: string; qualifier: string | null } {
  const open = label.indexOf(' (');
  if (open === -1 || !label.endsWith(')')) return { name: label, qualifier: null };
  return { name: label.slice(0, open), qualifier: label.slice(open + 2, -1) };
}

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
        <div className="budget-gauge">
          <div className="budget-gauge__head">
            <span>Context budget consumption</span>
            <strong>{budgetPercent.toFixed(1)}%</strong>
          </div>
          <div
            aria-valuemax={100}
            aria-valuemin={0}
            aria-valuenow={Number(budgetPercent.toFixed(1))}
            aria-valuetext={`${budgetPercent.toFixed(1)} percent of the saved context budget`}
            className="budget-gauge__track"
            role="meter"
            aria-label="Context budget consumption"
          >
            <span className="budget-gauge__fill" style={{ inlineSize: `${budgetPercent}%` }} />
          </div>
        </div>
      </section>

      {/* The stages are a sequence, so they are drawn as one: numbered, connected,
          and carrying the candidate count each stage actually recorded. */}
      <nav aria-label="Evidence pipeline stages" className="pipeline-steps">
        {run.pipeline.stages.map((item, index) => {
          const { name, qualifier } = splitStageLabel(item.label);
          const active = stageIndex === index;
          return (
            <button
              aria-current={active ? 'step' : undefined}
              aria-label={`${item.label}. ${item.candidates.length} candidates.`}
              className="pipeline-step"
              key={item.id}
              onClick={() => setStageIndex(index)}
              type="button"
            >
              <span aria-hidden className="pipeline-step__index">
                {String(index + 1).padStart(2, '0')}
              </span>
              <span aria-hidden className="pipeline-step__body">
                <span className="pipeline-step__name">{name}</span>
                {qualifier ? <span className="pipeline-step__qualifier">{qualifier}</span> : null}
              </span>
              <span aria-hidden className="pipeline-step__count">
                {item.candidates.length}
              </span>
            </button>
          );
        })}
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
          <table className="pipeline-table">
            <thead>
              <tr>
                <th scope="col">Evidence</th>
                <th scope="col">Origin</th>
                <th className="is-num" scope="col">Rank</th>
                <th className="is-num" scope="col">Stage score</th>
                <th className="is-num" scope="col">Tokens</th>
                <th scope="col">Decision</th>
                <th className="is-num" scope="col">Context order</th>
              </tr>
            </thead>
            <tbody>
              {stage.candidates.map((candidate) => (
                <tr key={candidate.id} data-decision={candidate.decision}>
                  <td>
                    <CitationLink citation={candidate.citation} />
                  </td>
                  <td>
                    <span className="chip chip--origin">{candidate.origin}</span>
                  </td>
                  <td className="is-num">
                    <MetricLink label="Rank" metric={candidate.rank} variant="cell" />
                  </td>
                  <td className="is-num">
                    {candidate.score ? (
                      <MetricLink label="Stage score" metric={candidate.score} variant="cell" />
                    ) : (
                      <span className="muted-copy">Not recorded</span>
                    )}
                  </td>
                  <td className="is-num">
                    <MetricLink label="Tokens" metric={candidate.tokenCount} variant="cell" />
                  </td>
                  <td>
                    <span className="chip" data-decision={candidate.decision}>
                      {candidate.decision}
                    </span>
                    {candidate.reason ? <span className="decision-reason">{candidate.reason}</span> : null}
                  </td>
                  <td className="is-num">
                    {candidate.contextOrder ? (
                      <MetricLink label="Context order" metric={candidate.contextOrder} variant="cell" />
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
