import { sankey, sankeyLinkHorizontal, type SankeyGraph, type SankeyLink, type SankeyNode } from 'd3-sankey';
import { useMemo, useState, type KeyboardEvent } from 'react';

import type {
  ArtifactRef,
  CitationRecord,
  MetricValue,
  PipelineCandidate,
  PipelineStage,
  RunRecord,
} from '../data/contract';
import { ArtifactLink, CitationLink, MetricLink } from './ProvenanceLink';

type FlowKind = 'source' | 'retrieval' | 'context' | 'excluded' | 'answer' | 'evidence' | 'failure';

interface FlowNodeDatum {
  id: string;
  column: number;
  kind: FlowKind;
  title: string;
  eyebrow: string;
  description: string;
  valueLabel: string;
  artifact?: ArtifactRef;
  metric?: MetricValue;
  citations?: CitationRecord[];
}

interface FlowLinkDatum {
  id: string;
  source: string;
  target: string;
  value: number;
  label: string;
}

interface SourceGroup {
  id: string;
  title: string;
  origin: PipelineCandidate['origin'];
  candidateCount: number;
  tokens: number;
  artifact: ArtifactRef;
}

const WIDTH = 1180;
const HEIGHT = 560;
const FLOW_COLORS: Record<FlowKind, string> = {
  source: '#8a8580',
  retrieval: '#c9713d',
  context: '#f0854a',
  excluded: '#4a4740',
  answer: '#f6b78c',
  evidence: '#d97b45',
  failure: '#e8806a',
};

function formatTokens(value: number): string {
  return `${new Intl.NumberFormat('en-US').format(value)} tokens`;
}

function getStage(run: RunRecord, kind: PipelineStage['kind']): PipelineStage | undefined {
  return run.pipeline.stages.find((stage) => stage.kind === kind);
}

function buildSourceGroups(run: RunRecord): SourceGroup[] {
  const retrieval = getStage(run, 'retrieval');
  if (!retrieval) return [];

  const grouped = new Map<string, SourceGroup>();
  retrieval.candidates.forEach((candidate) => {
    const key = `${candidate.origin}:${candidate.citation.sourceId}`;
    const current = grouped.get(key);
    if (current) {
      current.candidateCount += 1;
      current.tokens += candidate.tokenCount.value;
      return;
    }
    grouped.set(key, {
      id: `source-${key}`,
      title: candidate.citation.sourceId,
      origin: candidate.origin,
      candidateCount: 1,
      tokens: candidate.tokenCount.value,
      artifact: candidate.citation.source,
    });
  });

  const sorted = [...grouped.values()].sort((a, b) => b.tokens - a.tokens);
  if (sorted.length <= 6) return sorted;

  const visible = sorted.slice(0, 5);
  const remainder = sorted.slice(5);
  visible.push({
    id: 'source-other',
    title: `${remainder.length} more sources`,
    origin: 'corpus',
    candidateCount: remainder.reduce((sum, group) => sum + group.candidateCount, 0),
    tokens: remainder.reduce((sum, group) => sum + group.tokens, 0),
    artifact: retrieval.artifact,
  });
  return visible;
}

function buildFlow(run: RunRecord): {
  graph: SankeyGraph<FlowNodeDatum, FlowLinkDatum> | null;
  nodes: FlowNodeDatum[];
  uninstrumented: PipelineStage[];
} {
  const retrieval = getStage(run, 'retrieval');
  const context = getStage(run, 'context');
  const sourceGroups = buildSourceGroups(run);
  const retrievedTokens = sourceGroups.reduce((sum, group) => sum + group.tokens, 0);
  const contextTokens = run.pipeline.contextUsed.value;
  const excludedTokens = Math.max(0, retrievedTokens - contextTokens);
  const uninstrumented = run.pipeline.stages.filter((stage) => stage.candidates.length === 0);

  if (!retrieval || !context || retrievedTokens <= 0 || contextTokens <= 0) {
    return { graph: null, nodes: [], uninstrumented };
  }

  const nodes: FlowNodeDatum[] = sourceGroups.map((group) => ({
    id: group.id,
    column: 0,
    kind: 'source',
    title: group.title,
    eyebrow: group.origin === 'memory' ? 'Memory evidence' : 'Corpus evidence',
    description: `${group.candidateCount} saved candidate${group.candidateCount === 1 ? '' : 's'} entered retrieval from this source.`,
    valueLabel: formatTokens(group.tokens),
    artifact: group.artifact,
  }));

  nodes.push(
    {
      id: 'retrieval',
      column: 1,
      kind: 'retrieval',
      title: 'Candidate evidence',
      eyebrow: 'Retrieval',
      description: `${retrieval.candidates.length} saved candidates entered the measured retrieval stage.`,
      valueLabel: formatTokens(retrievedTokens),
      artifact: retrieval.artifact,
    },
    {
      id: 'context',
      column: 2,
      kind: 'context',
      title: 'Context pack',
      eyebrow: 'Selected evidence',
      description: 'The exact saved evidence passed to generation after the recorded budget decision.',
      valueLabel: run.pipeline.contextUsed.display,
      artifact: context.artifact,
      metric: run.pipeline.contextUsed,
    },
    {
      id: 'answer',
      column: 3,
      kind: run.executionStatus === 'failed' ? 'failure' : 'answer',
      title: run.executionStatus === 'failed' ? 'Execution failed' : 'Saved answer',
      eyebrow: 'Generation',
      description:
        run.executionStatus === 'failed'
          ? 'The saved run stopped with a provider or execution failure. No replacement answer is synthesized.'
          : 'The answer is restored from the immutable public run output. Replay never calls a model.',
      valueLabel: run.executionStatus,
      artifact: run.rawOutput,
    },
    {
      id: 'evidence',
      column: 4,
      kind: run.executionStatus === 'failed' ? 'failure' : 'evidence',
      title:
        run.executionStatus === 'failed'
          ? 'Failure evidence'
          : `${run.answer.citations.length} citation${run.answer.citations.length === 1 ? '' : 's'}`,
      eyebrow: run.executionStatus === 'failed' ? 'Observable result' : 'Grounding',
      description:
        run.executionStatus === 'failed'
          ? 'The failure remains visible as the final observable state for this saved run.'
          : 'Each citation opens the exact exported section and its source provenance.',
      valueLabel:
        run.executionStatus === 'failed'
          ? 'No substituted output'
          : `${run.answer.citations.length} exact target${run.answer.citations.length === 1 ? '' : 's'}`,
      artifact: run.executionStatus === 'failed' ? run.executionFacts : run.rawOutput,
      citations: run.answer.citations,
    },
  );

  if (excludedTokens > 0) {
    nodes.push({
      id: 'excluded',
      column: 2,
      kind: 'excluded',
      title: 'Excluded evidence',
      eyebrow: 'Budget boundary',
      description: 'Candidate tokens that did not enter the saved context pack.',
      valueLabel: formatTokens(excludedTokens),
      artifact: context.artifact,
    });
  }

  const links: FlowLinkDatum[] = sourceGroups.map((group) => ({
    id: `${group.id}-retrieval`,
    source: group.id,
    target: 'retrieval',
    value: group.tokens,
    label: `${group.title} to retrieval`,
  }));
  links.push(
    {
      id: 'retrieval-context',
      source: 'retrieval',
      target: 'context',
      value: contextTokens,
      label: 'Selected evidence to context',
    },
    {
      id: 'context-answer',
      source: 'context',
      target: 'answer',
      value: contextTokens,
      label: 'Context supplied to generation',
    },
    {
      id: 'answer-evidence',
      source: 'answer',
      target: 'evidence',
      value: contextTokens,
      label: 'Saved execution to observable result',
    },
  );
  if (excludedTokens > 0) {
    links.push({
      id: 'retrieval-excluded',
      source: 'retrieval',
      target: 'excluded',
      value: excludedTokens,
      label: 'Evidence excluded by the saved context boundary',
    });
  }

  const graph = sankey<FlowNodeDatum, FlowLinkDatum>()
    .nodeId((node) => node.id)
    .nodeAlign((node) => node.column)
    .nodeWidth(18)
    .nodePadding(26)
    .iterations(48)
    .extent([
      [38, 78],
      [WIDTH - 38, HEIGHT - 54],
    ])({
      nodes: nodes.map((node) => ({ ...node })),
      links: links.map((link) => ({ ...link })),
    });

  return { graph, nodes, uninstrumented };
}

function nodeId(node: string | number | SankeyNode<FlowNodeDatum, FlowLinkDatum>): string {
  return typeof node === 'object' ? node.id : String(node);
}

function activateOnKey(event: KeyboardEvent<SVGGElement>, activate: () => void): void {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    activate();
  }
}

export default function EvidenceFlow({ run }: { run: RunRecord }) {
  const { graph, nodes, uninstrumented } = useMemo(() => buildFlow(run), [run]);
  const [selectedNodeId, setSelectedNodeId] = useState('context');
  const selected = nodes.find((node) => node.id === selectedNodeId) ?? nodes[0];
  const path = useMemo(() => sankeyLinkHorizontal<FlowNodeDatum, FlowLinkDatum>(), []);

  if (!graph || !selected) {
    return (
      <section className="flow-empty" aria-labelledby="flow-heading">
        <p className="instrument-kicker">Run anatomy</p>
        <h2 id="flow-heading">Evidence flow</h2>
        <p>This run has no positive saved token flow to plot. Its artifacts remain available below.</p>
      </section>
    );
  }

  const connectedToSelection = (link: SankeyLink<FlowNodeDatum, FlowLinkDatum>): boolean =>
    nodeId(link.source) === selected.id || nodeId(link.target) === selected.id;

  return (
    <section className="evidence-flow" aria-labelledby="flow-heading">
      <header className="instrument-heading">
        <div>
          <p className="instrument-kicker">Run anatomy</p>
          <h2 id="flow-heading">Evidence flow</h2>
          <p>Ribbon width is the saved candidate or context token volume. Select any node to inspect its exact evidence.</p>
        </div>
        <div className="flow-legend" aria-label="Evidence flow legend">
          <span><i data-kind="source" /> Source</span>
          <span><i data-kind="context" /> Selected</span>
          <span><i data-kind="excluded" /> Excluded</span>
        </div>
      </header>

      <div className="flow-stage">
        <div className="flow-canvas">
          <svg
            aria-describedby="flow-description"
            className="flow-svg"
            role="img"
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          >
            <title>Evidence flow for saved run {run.id}</title>
            <desc id="flow-description">
              Saved evidence moves from source documents through retrieval and context construction to the final observable result.
            </desc>
            <defs>
              {graph.links.map((link, index) => {
                const source = link.source as SankeyNode<FlowNodeDatum, FlowLinkDatum>;
                const target = link.target as SankeyNode<FlowNodeDatum, FlowLinkDatum>;
                return (
                  <linearGradient id={`flow-link-${index}`} key={link.id} x1="0" x2="1">
                    <stop offset="0%" stopColor={FLOW_COLORS[source.kind]} />
                    <stop offset="100%" stopColor={FLOW_COLORS[target.kind]} />
                  </linearGradient>
                );
              })}
            </defs>
            <g className="flow-column-labels" aria-hidden>
              <text x="38" y="34">SAVED SOURCES</text>
              <text x="305" y="34">RETRIEVAL</text>
              <text x="575" y="34">CONTEXT</text>
              <text x="845" y="34">GENERATION</text>
              <text textAnchor="end" x={WIDTH - 38} y="34">RESULT</text>
            </g>
            <g className="flow-links">
              {graph.links.map((link, index) => {
                const active = connectedToSelection(link);
                const target = link.target as SankeyNode<FlowNodeDatum, FlowLinkDatum>;
                return (
                  <path
                    aria-label={`${link.label}: ${formatTokens(link.value)}`}
                    className={active ? 'flow-link flow-link--active' : 'flow-link'}
                    d={path(link) ?? undefined}
                    key={link.id}
                    onClick={() => setSelectedNodeId(target.id)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        setSelectedNodeId(target.id);
                      }
                    }}
                    role="button"
                    stroke={`url(#flow-link-${index})`}
                    strokeWidth={Math.max(1, link.width ?? 1)}
                    tabIndex={0}
                  />
                );
              })}
            </g>
            <g className="flow-nodes">
              {graph.nodes.map((node) => {
                const x0 = node.x0 ?? 0;
                const x1 = node.x1 ?? 0;
                const y0 = node.y0 ?? 0;
                const y1 = node.y1 ?? 0;
                const rightSide = x0 > WIDTH * 0.72;
                const active = selected.id === node.id;
                return (
                  <g
                    aria-label={`${node.eyebrow}: ${node.title}, ${node.valueLabel}`}
                    aria-pressed={active}
                    className={active ? 'flow-node flow-node--active' : 'flow-node'}
                    key={node.id}
                    onClick={() => setSelectedNodeId(node.id)}
                    onKeyDown={(event) => activateOnKey(event, () => setSelectedNodeId(node.id))}
                    role="button"
                    tabIndex={0}
                  >
                    <rect
                      fill={FLOW_COLORS[node.kind]}
                      height={Math.max(8, y1 - y0)}
                      rx="3"
                      width={x1 - x0}
                      x={x0}
                      y={y0}
                    />
                    <text
                      className="flow-node__title"
                      textAnchor={rightSide ? 'end' : 'start'}
                      x={rightSide ? x0 - 12 : x1 + 12}
                      y={(y0 + y1) / 2 - 4}
                    >
                      {node.title}
                    </text>
                    <text
                      className="flow-node__value"
                      textAnchor={rightSide ? 'end' : 'start'}
                      x={rightSide ? x0 - 12 : x1 + 12}
                      y={(y0 + y1) / 2 + 14}
                    >
                      {node.valueLabel}
                    </text>
                  </g>
                );
              })}
            </g>
          </svg>
        </div>

        <aside aria-live="polite" className="flow-inspector">
          <p className="instrument-kicker">{selected.eyebrow}</p>
          <h3>{selected.title}</h3>
          <strong className="flow-inspector__value">{selected.valueLabel}</strong>
          <p>{selected.description}</p>
          {selected.metric ? <MetricLink label="Saved value" metric={selected.metric} /> : null}
          {selected.artifact ? <ArtifactLink artifact={selected.artifact} /> : null}
          {selected.citations?.length ? (
            <div className="flow-inspector__citations">
              {selected.citations.map((citation) => <CitationLink citation={citation} key={citation.id} />)}
            </div>
          ) : null}
        </aside>
      </div>

      <div className="flow-index" aria-label="Keyboard-accessible evidence flow index">
        {nodes.map((node) => (
          <button
            aria-label={`${node.eyebrow}: ${node.title}`}
            aria-pressed={selected.id === node.id}
            key={node.id}
            onClick={() => setSelectedNodeId(node.id)}
            type="button"
          >
            <span>{node.eyebrow}</span>
            <strong>{node.title}</strong>
          </button>
        ))}
      </div>

      {uninstrumented.length ? (
        <p className="flow-boundary">
          <strong>Instrumentation boundary:</strong>{' '}
          {uninstrumented.map((stage) => stage.kind).join(', ')} are present in the saved pipeline contract but expose no candidate-level rows for this run.
        </p>
      ) : null}
    </section>
  );
}
