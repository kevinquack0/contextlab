import { Select, SelectItem } from '@carbon/react';
import { useMemo, useState, type ChangeEvent } from 'react';

import type { ContextLabViewerExport } from '../data/contract';
import { ArtifactLink, MetricLink } from '../components/ProvenanceLink';
import { EmptyState } from '../components/RuntimeStates';
import { ViewHeader } from '../components/ViewPrimitives';

function StrategyMatrixContent({ data }: { data: ContextLabViewerExport }) {
  const taskFamilies = useMemo(
    () => [...new Set(data.strategyMatrix.cells.map((cell) => cell.taskFamily))].toSorted(),
    [data.strategyMatrix.cells],
  );
  const [taskFamily, setTaskFamily] = useState(taskFamilies[0]);
  const reasoningEfforts = useMemo(
    () => [...new Set(data.strategyMatrix.cells.map((cell) => cell.reasoningEffort))].toSorted(),
    [data.strategyMatrix.cells],
  );
  const [reasoningEffort, setReasoningEffort] = useState<'high' | 'low'>(
    reasoningEfforts[0] ?? 'low',
  );
  const strategyById = useMemo(
    () => new Map(data.strategies.map((strategy) => [strategy.id, strategy])),
    [data.strategies],
  );
  const cells = data.strategyMatrix.cells.filter(
    (cell) => cell.taskFamily === taskFamily && cell.reasoningEffort === reasoningEffort,
  );

  return (
    <section aria-labelledby="matrix-heading" className="view-stack">
      <ViewHeader
        actions={
          <div className="matrix-controls">
            <Select
              id="matrix-family"
              labelText="Task family"
              onChange={(event: ChangeEvent<HTMLSelectElement>) => setTaskFamily(event.target.value)}
              size="sm"
              value={taskFamily}
            >
              {taskFamilies.map((family) => (
                <SelectItem key={family} text={family} value={family} />
              ))}
            </Select>
            <Select
              id="matrix-effort"
              labelText="Reasoning effort"
              onChange={(event: ChangeEvent<HTMLSelectElement>) =>
                setReasoningEffort(event.target.value as 'high' | 'low')
              }
              size="sm"
              value={reasoningEffort}
            >
              {reasoningEfforts.map((effort) => (
                <SelectItem key={effort} text={effort} value={effort} />
              ))}
            </Select>
          </div>
        }
        description="Compare public execution status, evidence-row counts, context use, latency, cost, and trial coverage."
        title="Strategy matrix"
      />
      <div className="matrix-provenance">
        <h2 id="matrix-heading">{taskFamily} · {reasoningEffort} effort</h2>
        <ArtifactLink artifact={data.strategyMatrix.artifact} />
      </div>
      <div className="table-scroll" role="region" aria-label={`${taskFamily} strategy matrix`} tabIndex={0}>
        <table className="cds--data-table cds--data-table--compact matrix-table">
          <thead>
            <tr>
              <th scope="col">Strategy</th>
              <th scope="col">Completion</th>
              <th scope="col">Candidate evidence</th>
              <th scope="col">Selected evidence</th>
              <th scope="col">Context</th>
              <th scope="col">Latency</th>
              <th scope="col">Execution cost</th>
              <th scope="col">Trials</th>
              <th scope="col">Aggregate artifact</th>
            </tr>
          </thead>
          <tbody>
            {cells.map((cell) => {
              const strategy = strategyById.get(cell.strategyId);
              return (
                <tr key={`${cell.taskFamily}-${cell.strategyId}`}>
                  <th scope="row">
                    <strong>{strategy?.label ?? cell.strategyId}</strong>
                    {strategy ? <ArtifactLink artifact={strategy.artifact} compact /> : null}
                  </th>
                  <td>
                    <MetricLink compact label="Completion" metric={cell.completionRatio} />
                  </td>
                  <td>
                    <MetricLink compact label="Candidates" metric={cell.meanCandidateEvidence} />
                  </td>
                  <td>
                    <MetricLink compact label="Selected" metric={cell.meanSelectedEvidence} />
                  </td>
                  <td>
                    <MetricLink compact label="Used" metric={cell.meanContextTokens} />
                    <MetricLink compact label="Budget" metric={cell.contextBudget} />
                  </td>
                  <td>
                    <MetricLink compact label="Latency" metric={cell.meanLatency} />
                  </td>
                  <td>
                    <MetricLink compact label="Cost" metric={cell.meanExecutionCost} />
                  </td>
                  <td>
                    <MetricLink compact label="Trial count" metric={cell.trialCount} />
                  </td>
                  <td>
                    <ArtifactLink artifact={cell.artifact} compact />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <section className="relationship-grid" aria-label="Measured relationships">
        <article>
          <h2>Candidate evidence against selected evidence</h2>
          <div className="relationship-list">
            {cells.map((cell) => (
              <div className="relationship-row" key={cell.strategyId}>
                <strong>{strategyById.get(cell.strategyId)?.label ?? cell.strategyId}</strong>
                <MetricLink compact label="Candidates" metric={cell.meanCandidateEvidence} />
                <MetricLink compact label="Selected" metric={cell.meanSelectedEvidence} />
              </div>
            ))}
          </div>
        </article>
        <article>
          <h2>Context use against configured budget</h2>
          <div className="relationship-list">
            {cells.map((cell) => (
              <div className="relationship-row" key={cell.strategyId}>
                <strong>{strategyById.get(cell.strategyId)?.label ?? cell.strategyId}</strong>
                <MetricLink compact label="Used" metric={cell.meanContextTokens} />
                <MetricLink compact label="Budget" metric={cell.contextBudget} />
              </div>
            ))}
          </div>
        </article>
      </section>
    </section>
  );
}

export default function StrategyMatrix({ data }: { data: ContextLabViewerExport }) {
  if (data.strategyMatrix.cells.length === 0) {
    return (
      <EmptyState
        detail="Add aggregate matrix cells with metric provenance and source run IDs."
        title="No strategy matrix cells are present"
      />
    );
  }
  return <StrategyMatrixContent data={data} />;
}
