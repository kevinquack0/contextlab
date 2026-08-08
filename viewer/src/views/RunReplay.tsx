import { Button, CodeSnippet, Tag } from '@carbon/react';
import Download from '@carbon/icons-react/es/Download';
import Reset from '@carbon/icons-react/es/Reset';
import { useMemo, useReducer } from 'react';

import type { ContextLabViewerExport, JsonValue } from '../data/contract';
import {
  buildReplaySnapshot,
  createReplayState,
  replayReducer,
  selectedTraceSpan,
} from '../data/replay';
import { ArtifactLink, CitationLink, MetricLink } from '../components/ProvenanceLink';
import { EmptyState } from '../components/RuntimeStates';
import { RunIdentity, RunPicker, ViewHeader } from '../components/ViewPrimitives';

function serializeConfig(values: Record<string, JsonValue>): string {
  return JSON.stringify(values, null, 2);
}

function RunReplayContent({ data }: { data: ContextLabViewerExport }) {
  const runById = useMemo(() => new Map(data.runs.map((run) => [run.id, run])), [data.runs]);
  const initialSnapshot = buildReplaySnapshot(data, data.runs[0].id);
  const [state, dispatch] = useReducer(replayReducer, initialSnapshot, (snapshot) =>
    createReplayState(snapshot),
  );
  const { run } = state.current;
  const selectedSpan = selectedTraceSpan(state);

  function restoreRun(runId: string): void {
    if (!runById.has(runId)) return;
    dispatch({ type: 'restore', snapshot: buildReplaySnapshot(data, runId) });
  }

  return (
    <section aria-labelledby="replay-heading" className="view-stack">
      <ViewHeader
        actions={<RunPicker data={data} id="replay-run" onChange={restoreRun} runId={run.id} />}
        description="Restore the exact saved inputs and outputs for one run. Replay does not call a model or alter an artifact."
        title="Run replay"
      />
      <div className="replay-toolbar">
        <RunIdentity run={run} />
        <Button kind="secondary" onClick={() => dispatch({ type: 'reset' })} renderIcon={Reset} size="sm">
          Restore selected run
        </Button>
        <Button
          as="a"
          download
          href={run.rawOutput.staticUrl}
          kind="primary"
          renderIcon={Download}
          size="sm"
        >
          Download raw output
        </Button>
      </div>
      <section className="replay-question">
        <p>{state.current.strategyLabel}</p>
        <h2 id="replay-heading">{state.current.questionText}</h2>
      </section>
      <div className="replay-layout">
        <section className="replay-panel">
          <h2>Restored inputs</h2>
          <dl className="artifact-definition-list">
            <div>
              <dt>Corpus snapshot</dt>
              <dd><ArtifactLink artifact={run.corpusSnapshot} /></dd>
            </div>
            <div>
              <dt>Memory snapshot</dt>
              <dd><ArtifactLink artifact={run.memorySnapshot} /></dd>
            </div>
            <div>
              <dt>Prompt</dt>
              <dd><ArtifactLink artifact={run.prompt} /></dd>
            </div>
            <div>
              <dt>Configuration</dt>
              <dd>
                <strong>{run.configuration.id}</strong>
                <ArtifactLink artifact={run.configuration.artifact} />
              </dd>
            </div>
          </dl>
          <CodeSnippet feedback="Copied configuration" type="multi" wrapText>
            {serializeConfig(run.configuration.values)}
          </CodeSnippet>
        </section>
        <section className="replay-panel">
          <h2>Restored output</h2>
          <p className="replay-answer">{run.answer.text || 'The saved output is empty.'}</p>
          <div className="strategy-lane__metrics">
            <MetricLink compact label="Context" metric={run.metrics.contextTokens} />
            <MetricLink compact label="Latency" metric={run.metrics.latency} />
            <MetricLink compact label="Cost" metric={run.metrics.estimatedCost} />
          </div>
          <div className="citation-stack">
            {run.answer.citations.map((citation) => (
              <CitationLink citation={citation} key={citation.id} />
            ))}
          </div>
          <div className="raw-output-provenance">
            <span>Raw output provenance</span>
            <ArtifactLink artifact={run.rawOutput} />
          </div>
        </section>
      </div>
      <section className="trace-section">
        <header>
          <h2>Observable spans and tool results</h2>
          <ArtifactLink artifact={run.executionFacts} />
        </header>
        <div className="trace-layout">
          <div aria-label="Saved trace spans" className="trace-list" role="list">
            {run.traceSpans.map((span) => (
              <div
                className="trace-row"
                key={span.id}
                role="listitem"
              >
                <button
                  aria-pressed={state.selectedSpanId === span.id}
                  className="trace-row__select"
                  onClick={() => dispatch({ type: 'select-span', spanId: span.id })}
                  type="button"
                >
                  <Tag size="sm" type={span.status === 'ok' ? 'green' : 'red'}>{span.status}</Tag>
                  <strong>{span.name}</strong>
                </button>
                <MetricLink compact label="Duration" metric={span.duration} />
              </div>
            ))}
          </div>
          <aside aria-live="polite" className="trace-detail">
            {selectedSpan ? (
              <>
                <h3>{selectedSpan.name}</h3>
                <p>{selectedSpan.startedAt}</p>
                <ArtifactLink artifact={selectedSpan.artifact} />
                {selectedSpan.toolResult ? (
                  <>
                    <h4>Saved tool result</h4>
                    <ArtifactLink artifact={selectedSpan.toolResult} />
                  </>
                ) : (
                  <p className="muted-copy">This span has no saved tool result.</p>
                )}
              </>
            ) : (
              <p>Select a saved span to inspect its trace artifact and tool result.</p>
            )}
          </aside>
        </div>
      </section>
    </section>
  );
}

export default function RunReplay({ data }: { data: ContextLabViewerExport }) {
  if (data.runs.length === 0) {
    return (
      <EmptyState
        detail="Add saved runs with snapshots, trace spans, execution facts, and a raw-output static URL."
        title="No runs are available to replay"
      />
    );
  }
  return <RunReplayContent data={data} />;
}
