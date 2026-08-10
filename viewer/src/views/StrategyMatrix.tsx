import { Select, SelectItem } from '@carbon/react';
import { useMemo, useState, type ChangeEvent, type CSSProperties } from 'react';

import type { ContextLabViewerExport, MetricValue } from '../data/contract';
import { ArtifactLink, MetricLink } from '../components/ProvenanceLink';
import { EmptyState } from '../components/RuntimeStates';
import { ViewHeader } from '../components/ViewPrimitives';

/**
 * A pair of bound metrics drawn to the same scale, so the comparison the row is
 * making is visible before any number is read. The bar is never the only
 * carrier: the exact figures sit beside it and both stay linked to provenance.
 */
function RatioRow({
  label,
  primary,
  primaryLabel,
  reference,
  referenceLabel,
  scale,
  seriesIndex,
}: {
  label: string;
  primary: MetricValue;
  primaryLabel: string;
  reference: MetricValue;
  referenceLabel: string;
  scale: number;
  seriesIndex: number;
}) {
  const primaryWidth = scale > 0 ? Math.min(100, (primary.value / scale) * 100) : 0;
  const referenceWidth = scale > 0 ? Math.min(100, (reference.value / scale) * 100) : 0;

  return (
    <div className="ratio-row" style={{ '--series': `var(--viewer-series-${seriesIndex})` } as CSSProperties}>
      <span className="ratio-row__label">{label}</span>
      <span aria-hidden className="ratio-row__track">
        <span className="ratio-row__reference" style={{ inlineSize: `${referenceWidth}%` }} />
        <span className="ratio-row__primary" style={{ inlineSize: `${primaryWidth}%` }} />
      </span>
      <span className="ratio-row__values">
        <MetricLink label={primaryLabel} metric={primary} variant="cell" />
        <span aria-hidden className="ratio-row__of">/</span>
        <MetricLink label={referenceLabel} metric={reference} variant="cell" />
      </span>
    </div>
  );
}

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
  const strategyIndex = useMemo(
    () => new Map(data.strategies.map((strategy, index) => [strategy.id, index])),
    [data.strategies],
  );
  const cells = data.strategyMatrix.cells.filter(
    (cell) => cell.taskFamily === taskFamily && cell.reasoningEffort === reasoningEffort,
  );

  const evidenceScale = Math.max(
    ...cells.map((cell) => Math.max(cell.meanCandidateEvidence.value, cell.meanSelectedEvidence.value)),
    1,
  );
  const contextScale = Math.max(
    ...cells.map((cell) => Math.max(cell.meanContextTokens.value, cell.contextBudget.value)),
    1,
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
        <h2 id="matrix-heading">
          {taskFamily} <span aria-hidden>·</span> {reasoningEffort} effort
        </h2>
        <ArtifactLink artifact={data.strategyMatrix.artifact} />
      </div>

      <p className="matrix-hint">
        Every figure below opens the aggregate artifact it was read from.
      </p>

      <div className="table-scroll" role="region" aria-label={`${taskFamily} strategy matrix`} tabIndex={0}>
        <table className="matrix-table">
          <thead>
            <tr>
              <th scope="col">Strategy</th>
              <th className="is-num" scope="col">Completion</th>
              <th className="is-num" scope="col">Candidate evidence</th>
              <th className="is-num" scope="col">Selected evidence</th>
              <th className="is-num" scope="col">Context</th>
              <th className="is-num" scope="col">Budget</th>
              <th className="is-num" scope="col">Latency</th>
              <th className="is-num" scope="col">Execution cost</th>
              <th className="is-num" scope="col">Trials</th>
              <th scope="col">Aggregate artifact</th>
            </tr>
          </thead>
          <tbody>
            {cells.map((cell) => {
              const strategy = strategyById.get(cell.strategyId);
              const index = strategyIndex.get(cell.strategyId) ?? 0;
              return (
                <tr key={`${cell.taskFamily}-${cell.strategyId}`}>
                  <th scope="row">
                    <span
                      aria-hidden
                      className="matrix-table__swatch"
                      style={{ background: `var(--viewer-series-${index})` }}
                    />
                    <span className="matrix-table__strategy">
                      <strong>{strategy?.label ?? cell.strategyId}</strong>
                      {strategy ? <ArtifactLink artifact={strategy.artifact} compact /> : null}
                    </span>
                  </th>
                  <td className="is-num">
                    <MetricLink label="Completion" metric={cell.completionRatio} variant="cell" />
                  </td>
                  <td className="is-num">
                    <MetricLink label="Candidate evidence" metric={cell.meanCandidateEvidence} variant="cell" />
                  </td>
                  <td className="is-num">
                    <MetricLink label="Selected evidence" metric={cell.meanSelectedEvidence} variant="cell" />
                  </td>
                  <td className="is-num">
                    <MetricLink label="Context used" metric={cell.meanContextTokens} variant="cell" />
                  </td>
                  <td className="is-num">
                    <MetricLink label="Context budget" metric={cell.contextBudget} variant="cell" />
                  </td>
                  <td className="is-num">
                    <MetricLink label="Latency" metric={cell.meanLatency} variant="cell" />
                  </td>
                  <td className="is-num">
                    <MetricLink label="Execution cost" metric={cell.meanExecutionCost} variant="cell" />
                  </td>
                  <td className="is-num">
                    <MetricLink label="Trial count" metric={cell.trialCount} variant="cell" />
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
          <h2>Selected evidence against candidates retrieved</h2>
          <p className="relationship-note">
            The filled bar is the evidence that reached the context pack; the outline is everything
            retrieval offered it.
          </p>
          <div className="relationship-list">
            {cells.map((cell) => (
              <RatioRow
                key={cell.strategyId}
                label={strategyById.get(cell.strategyId)?.label ?? cell.strategyId}
                primary={cell.meanSelectedEvidence}
                primaryLabel="Selected evidence"
                reference={cell.meanCandidateEvidence}
                referenceLabel="Candidate evidence"
                scale={evidenceScale}
                seriesIndex={strategyIndex.get(cell.strategyId) ?? 0}
              />
            ))}
          </div>
        </article>
        <article>
          <h2>Context used against configured budget</h2>
          <p className="relationship-note">
            The filled bar is mean context spent; the outline is the budget the run was allowed.
          </p>
          <div className="relationship-list">
            {cells.map((cell) => (
              <RatioRow
                key={cell.strategyId}
                label={strategyById.get(cell.strategyId)?.label ?? cell.strategyId}
                primary={cell.meanContextTokens}
                primaryLabel="Context used"
                reference={cell.contextBudget}
                referenceLabel="Context budget"
                scale={contextScale}
                seriesIndex={strategyIndex.get(cell.strategyId) ?? 0}
              />
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
