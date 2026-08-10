import { Button, CodeSnippet } from '@carbon/react';
import Close from '@carbon/icons-react/es/Close';
import Download from '@carbon/icons-react/es/Download';
import Network_1 from '@carbon/icons-react/es/Network_1';
import Reset from '@carbon/icons-react/es/Reset';
import { Suspense, lazy, useMemo, useReducer, useState } from 'react';

import type { ContextLabViewerExport, JsonValue } from '../data/contract';
import {
  buildReplaySnapshot,
  createReplayState,
  replayReducer,
  selectedTraceSpan,
} from '../data/replay';
import EvidenceFlow from '../components/EvidenceFlow';
import { ArtifactLink, CitationLink, MetricLink } from '../components/ProvenanceLink';
import { EmptyState } from '../components/RuntimeStates';
import { RunIdentity, RunPicker, ViewHeader } from '../components/ViewPrimitives';

const EvidenceConstellation = lazy(() => import('../components/EvidenceConstellation'));

function serializeConfig(values: Record<string, JsonValue>): string {
  return JSON.stringify(values, null, 2);
}

function RunReplayContent({ data }: { data: ContextLabViewerExport }) {
  const runById = useMemo(() => new Map(data.runs.map((run) => [run.id, run])), [data.runs]);
  const initialSnapshot = buildReplaySnapshot(data, data.runs[0].id);
  const [state, dispatch] = useReducer(replayReducer, initialSnapshot, (snapshot) =>
    createReplayState(snapshot),
  );
  const [showConstellation, setShowConstellation] = useState(false);
  const { run } = state.current;
  const selectedSpan = selectedTraceSpan(state) ?? run.traceSpans[0] ?? null;

  function restoreRun(runId: string): void {
    if (!runById.has(runId)) return;
    dispatch({ type: 'restore', snapshot: buildReplaySnapshot(data, runId) });
  }

  return (
    <section aria-labelledby="replay-heading" className="view-stack replay-view">
      <ViewHeader
        actions={<RunPicker data={data} id="replay-run" onChange={restoreRun} runId={run.id} />}
        description="Follow one immutable run from saved evidence to its observable result. Every selection opens the exact artifact behind it."
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
        <div>
          <p className="instrument-kicker">{state.current.strategyLabel}</p>
          <h2 id="replay-heading">{state.current.questionText}</h2>
        </div>
        <div className="replay-question__metrics" aria-label="Saved run measures">
          <MetricLink compact label="Context" metric={run.metrics.contextTokens} />
          <MetricLink compact label="Latency" metric={run.metrics.latency} />
          <MetricLink compact label="Cost" metric={run.metrics.estimatedCost} />
        </div>
      </section>

      <EvidenceFlow key={run.id} run={run} />

      <section className="replay-result" aria-labelledby="result-heading">
        <article className="replay-result__answer">
          <header>
            <p className="instrument-kicker">Observable result</p>
            <h2 id="result-heading">{run.executionStatus === 'failed' ? 'Failure preserved as evidence' : 'Answer and grounding'}</h2>
          </header>
          <p className="replay-answer">
            {run.answer.text || 'The saved execution produced no answer. The viewer does not insert substitute content.'}
          </p>
          {run.answer.citations.length ? (
            <div className="citation-stack">
              {run.answer.citations.map((citation) => (
                <CitationLink citation={citation} key={citation.id} />
              ))}
            </div>
          ) : (
            <p className="replay-result__empty-citations">No citations were emitted by this saved execution.</p>
          )}
          <div className="raw-output-provenance">
            <span>Raw output provenance</span>
            <ArtifactLink artifact={run.rawOutput} />
          </div>
        </article>

        <aside className="replay-result__inputs">
          <header>
            <p className="instrument-kicker">Frozen inputs</p>
            <h2>Reproducible by construction</h2>
          </header>
          <dl className="artifact-definition-list">
            <div>
              <dt>Corpus snapshot</dt>
              <dd><ArtifactLink artifact={run.corpusSnapshot} compact /></dd>
            </div>
            <div>
              <dt>Memory snapshot</dt>
              <dd><ArtifactLink artifact={run.memorySnapshot} compact /></dd>
            </div>
            <div>
              <dt>Prompt</dt>
              <dd><ArtifactLink artifact={run.prompt} compact /></dd>
            </div>
            <div>
              <dt>Configuration</dt>
              <dd><ArtifactLink artifact={run.configuration.artifact} compact /></dd>
            </div>
          </dl>
          <details className="configuration-disclosure">
            <summary>Inspect configuration values</summary>
            <CodeSnippet feedback="Copied configuration" type="multi" wrapText>
              {serializeConfig(run.configuration.values)}
            </CodeSnippet>
          </details>
        </aside>
      </section>

      <section className="trace-section" aria-labelledby="trace-heading">
        <header className="instrument-heading">
          <div>
            <p className="instrument-kicker">Execution trace</p>
            <h2 id="trace-heading">Observable spans and tool results</h2>
            <p>The trace exposes what ran, when it ran, and which saved tool result it produced.</p>
          </div>
          <ArtifactLink artifact={run.executionFacts} compact />
        </header>
        <div className="trace-layout">
          <div aria-label="Saved trace spans" className="trace-list" role="list">
            {run.traceSpans.map((span, index) => {
              const active = selectedSpan?.id === span.id;
              return (
                <button
                  aria-pressed={active}
                  className="trace-row"
                  key={span.id}
                  onClick={() => dispatch({ type: 'select-span', spanId: span.id })}
                  role="listitem"
                  type="button"
                >
                  <span className="trace-row__index">{String(index + 1).padStart(2, '0')}</span>
                  <span>
                    <strong>{span.name}</strong>
                    <small>{span.status}</small>
                  </span>
                  <data value={span.duration.value}>{span.duration.display}</data>
                </button>
              );
            })}
          </div>
          <aside aria-live="polite" className="trace-detail">
            {selectedSpan ? (
              <>
                <p className="instrument-kicker">Selected span</p>
                <h3>{selectedSpan.name}</h3>
                <time dateTime={selectedSpan.startedAt}>{selectedSpan.startedAt}</time>
                <MetricLink label="Duration" metric={selectedSpan.duration} />
                <ArtifactLink artifact={selectedSpan.artifact} />
                {selectedSpan.toolResult ? (
                  <div className="trace-detail__tool">
                    <span>Saved tool result</span>
                    <ArtifactLink artifact={selectedSpan.toolResult} />
                  </div>
                ) : (
                  <p className="muted-copy">This span has no saved tool result.</p>
                )}
              </>
            ) : (
              <p>No trace span is available for this saved run.</p>
            )}
          </aside>
        </div>
      </section>

      <section className="constellation-launch" aria-labelledby="constellation-launch-heading">
        <div>
          <p className="instrument-kicker">Explore another way</p>
          <h2 id="constellation-launch-heading">See the provenance as a connected system.</h2>
          <p>The constellation is optional. It adds spatial exploration while the evidence flow remains the primary explanation.</p>
        </div>
        <Button
          kind={showConstellation ? 'secondary' : 'primary'}
          onClick={() => setShowConstellation((visible) => !visible)}
          renderIcon={showConstellation ? Close : Network_1}
          size="lg"
        >
          {showConstellation ? 'Close constellation' : 'Explore constellation'}
        </Button>
      </section>

      {showConstellation ? (
        <Suspense fallback={<div className="constellation-loading" aria-label="Loading evidence constellation" role="status" />}>
          <EvidenceConstellation
            key={run.id}
            questionText={state.current.questionText}
            run={run}
            strategyLabel={state.current.strategyLabel}
          />
        </Suspense>
      ) : null}
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
