import { Select, SelectItem } from '@carbon/react';
import type { ChangeEvent, ReactNode } from 'react';

import type { ContextLabViewerExport, RunRecord, ShowcaseInsight } from '../data/contract';
import { ArtifactLink } from './ProvenanceLink';

interface ViewHeaderProps {
  title: string;
  description: string;
  actions?: ReactNode;
}

export function ViewHeader({ title, description, actions }: ViewHeaderProps) {
  return (
    <header className="view-header">
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions ? <div className="view-header__actions">{actions}</div> : null}
    </header>
  );
}

interface RunPickerProps {
  data: ContextLabViewerExport;
  id: string;
  label?: string;
  runId: string;
  onChange: (runId: string) => void;
  runFilter?: (run: RunRecord) => boolean;
}

export function RunPicker({
  data,
  id,
  label = 'Saved run',
  runId,
  onChange,
  runFilter,
}: RunPickerProps) {
  const questionById = new Map(data.questions.map((question) => [question.id, question]));
  const strategyById = new Map(data.strategies.map((strategy) => [strategy.id, strategy]));
  const runs = runFilter ? data.runs.filter(runFilter) : data.runs;

  return (
    <Select
      id={id}
      labelText={label}
      onChange={(event: ChangeEvent<HTMLSelectElement>) => onChange(event.target.value)}
      size="sm"
      value={runId}
    >
      {runs.map((run) => {
        const question = questionById.get(run.questionId);
        const strategy = strategyById.get(run.strategyId);
        return (
          <SelectItem
            key={run.id}
            text={`${run.id} | ${strategy?.label ?? run.strategyId} | ${question?.id ?? run.questionId}`}
            value={run.id}
          />
        );
      })}
    </Select>
  );
}

export function RunIdentity({ run }: { run: RunRecord }) {
  return (
    <div className="run-identity">
      <div className="run-identity__status">
        <span className="chip" data-status={run.executionStatus}>
          {run.executionStatus}
        </span>
        <span>
          Run <strong>{run.id}</strong>
        </span>
        <span>
          Config <strong>{run.configuration.id}</strong>
        </span>
      </div>
      <div className="run-identity__provenance">
        <ArtifactLink artifact={run.runArtifact} compact />
        <ArtifactLink artifact={run.configuration.artifact} compact />
      </div>
    </div>
  );
}

export function EvidenceCallout({ insight }: { insight: ShowcaseInsight }) {
  return (
    <aside className="evidence-callout">
      <div>
        <h2>{insight.title}</h2>
        <p>{insight.explanation}</p>
        <p className="evidence-callout__runs">Source runs: {insight.runIds.join(', ')}</p>
      </div>
      <ArtifactLink artifact={insight.artifact} />
    </aside>
  );
}
