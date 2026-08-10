import Graph from 'graphology';
import Sigma from 'sigma';
import { useEffect, useMemo, useRef, useState } from 'react';

import type { ArtifactRef, RunRecord } from '../data/contract';
import { ArtifactLink } from './ProvenanceLink';

type ConstellationKind = 'question' | 'input' | 'source' | 'process' | 'answer' | 'citation' | 'trace';

interface ConstellationNode {
  id: string;
  label: string;
  kind: ConstellationKind;
  description: string;
  x: number;
  y: number;
  size: number;
  color: string;
  artifact?: ArtifactRef;
}

interface ConstellationEdge {
  id: string;
  source: string;
  target: string;
}

const COLORS: Record<ConstellationKind, string> = {
  question: '#f6b78c',
  input: '#837f74',
  source: '#8a8580',
  process: '#c9713d',
  answer: '#f0854a',
  citation: '#9cc4d8',
  trace: '#4a4740',
};

function uniqueSources(run: RunRecord): Array<{ id: string; label: string; artifact: ArtifactRef }> {
  const retrieval = run.pipeline.stages.find((stage) => stage.kind === 'retrieval');
  if (!retrieval) return [];
  const sources = new Map<string, { id: string; label: string; artifact: ArtifactRef }>();
  retrieval.candidates.forEach((candidate) => {
    const key = candidate.citation.target.sha256;
    if (!sources.has(key)) {
      sources.set(key, {
        id: `source-${key.slice(0, 12)}`,
        label: candidate.citation.label,
        artifact: candidate.citation.target,
      });
    }
  });
  return [...sources.values()].slice(0, 18);
}

function buildConstellation(
  run: RunRecord,
  questionText: string,
  strategyLabel: string,
): { nodes: ConstellationNode[]; edges: ConstellationEdge[] } {
  const sources = uniqueSources(run);
  const retrieval = run.pipeline.stages.find((stage) => stage.kind === 'retrieval');
  const context = run.pipeline.stages.find((stage) => stage.kind === 'context');
  const nodes: ConstellationNode[] = [
    {
      id: 'question',
      label: 'Saved question',
      kind: 'question',
      description: questionText,
      x: 0,
      y: 0,
      size: 18,
      color: COLORS.question,
    },
    {
      id: 'corpus',
      label: 'Corpus snapshot',
      kind: 'input',
      description: 'The frozen corpus input bound to this run.',
      x: -3.6,
      y: -2.2,
      size: 10,
      color: COLORS.input,
      artifact: run.corpusSnapshot,
    },
    {
      id: 'memory',
      label: 'Memory snapshot',
      kind: 'input',
      description: 'The frozen memory input, including an empty snapshot when memory is disabled.',
      x: -3.6,
      y: 2.2,
      size: 10,
      color: COLORS.input,
      artifact: run.memorySnapshot,
    },
    {
      id: 'prompt',
      label: 'Prompt',
      kind: 'input',
      description: 'The exact prompt artifact used for the saved execution.',
      x: -1.2,
      y: -2.8,
      size: 9,
      color: COLORS.input,
      artifact: run.prompt,
    },
    {
      id: 'configuration',
      label: run.configuration.id,
      kind: 'input',
      description: `The frozen ${strategyLabel} configuration for this execution.`,
      x: -1.2,
      y: 2.8,
      size: 9,
      color: COLORS.input,
      artifact: run.configuration.artifact,
    },
    {
      id: 'retrieval',
      label: 'Candidate evidence',
      kind: 'process',
      description: `${retrieval?.candidates.length ?? 0} saved candidates are visible at the retrieval boundary.`,
      x: -1.05,
      y: 0,
      size: 14,
      color: COLORS.process,
      artifact: retrieval?.artifact,
    },
    {
      id: 'context',
      label: 'Context pack',
      kind: 'process',
      description: `${run.pipeline.contextUsed.display} of saved evidence entered generation.`,
      x: 1.25,
      y: 0,
      size: 16,
      color: COLORS.process,
      artifact: context?.artifact,
    },
    {
      id: 'answer',
      label: run.executionStatus === 'failed' ? 'Failed execution' : 'Saved answer',
      kind: 'answer',
      description:
        run.executionStatus === 'failed'
          ? 'The run failed and the viewer preserves that result without substitution.'
          : run.answer.text,
      x: 3.15,
      y: 0,
      size: 17,
      color: COLORS.answer,
      artifact: run.rawOutput,
    },
  ];

  const edges: ConstellationEdge[] = [
    { id: 'question-prompt', source: 'question', target: 'prompt' },
    { id: 'question-configuration', source: 'question', target: 'configuration' },
    { id: 'corpus-retrieval', source: 'corpus', target: 'retrieval' },
    { id: 'memory-retrieval', source: 'memory', target: 'retrieval' },
    { id: 'question-retrieval', source: 'question', target: 'retrieval' },
    { id: 'retrieval-context', source: 'retrieval', target: 'context' },
    { id: 'prompt-context', source: 'prompt', target: 'context' },
    { id: 'configuration-context', source: 'configuration', target: 'context' },
    { id: 'context-answer', source: 'context', target: 'answer' },
  ];

  sources.forEach((source, index) => {
    const spread = sources.length <= 1 ? 0 : (index / (sources.length - 1)) * 5.4 - 2.7;
    nodes.push({
      id: source.id,
      label: source.label,
      kind: 'source',
      description: 'An exact source section present in the retrieved candidate set.',
      x: -5.5 + Math.abs(spread) * 0.12,
      y: spread,
      size: 6.5,
      color: COLORS.source,
      artifact: source.artifact,
    });
    edges.push({ id: `${source.id}-retrieval`, source: source.id, target: 'retrieval' });
  });

  run.answer.citations.forEach((citation, index) => {
    const y = (index - (run.answer.citations.length - 1) / 2) * 1.2;
    const id = `citation-${citation.id}`;
    nodes.push({
      id,
      label: citation.label,
      kind: 'citation',
      description: citation.excerpt,
      x: 5.15,
      y,
      size: 8,
      color: COLORS.citation,
      artifact: citation.target,
    });
    edges.push({ id: `answer-${id}`, source: 'answer', target: id });
  });

  run.traceSpans.forEach((span, index) => {
    const id = `trace-${span.id}`;
    nodes.push({
      id,
      label: span.name,
      kind: 'trace',
      description: `${span.status} at ${span.startedAt}, ${span.duration.display}.`,
      x: 2.2 + index * 0.55,
      y: 2.6 + index * 0.35,
      size: 7,
      color: COLORS.trace,
      artifact: span.artifact,
    });
    edges.push({ id: `context-${id}`, source: 'context', target: id });
    edges.push({ id: `${id}-answer`, source: id, target: 'answer' });
  });

  return { nodes, edges };
}

export default function EvidenceConstellation({
  run,
  questionText,
  strategyLabel,
}: {
  run: RunRecord;
  questionText: string;
  strategyLabel: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const rendererRef = useRef<Sigma | null>(null);
  const selectedRef = useRef('question');
  const hoveredRef = useRef<string | null>(null);
  const [selectedId, setSelectedId] = useState('question');
  const [renderStatus, setRenderStatus] = useState<'ready' | 'error'>('ready');
  const model = useMemo(
    () => buildConstellation(run, questionText, strategyLabel),
    [questionText, run, strategyLabel],
  );
  const selected = model.nodes.find((node) => node.id === selectedId) ?? model.nodes[0];

  useEffect(() => {
    selectedRef.current = selectedId;
    rendererRef.current?.refresh();
  }, [selectedId]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;
    let renderer: Sigma | undefined;
    let active = true;

    try {
      const graph = new Graph({ multi: true, type: 'directed' });
      model.nodes.forEach((node) => graph.addNode(node.id, node));
      model.edges.forEach((edge) => {
        graph.addDirectedEdgeWithKey(edge.id, edge.source, edge.target, {
          color: '#3a3a31',
          size: 1.4,
        });
      });

      renderer = new Sigma(graph, container, {
        allowInvalidContainer: true,
        defaultEdgeColor: '#3a3a31',
        defaultNodeColor: COLORS.source,
        enableEdgeEvents: false,
        labelColor: { color: '#b6b2a7' },
        labelDensity: 0.9,
        labelFont: 'IBM Plex Sans',
        labelRenderedSizeThreshold: 6,
        labelSize: 12,
        maxCameraRatio: 3.2,
        minCameraRatio: 0.6,
        renderEdgeLabels: false,
        stagePadding: 34,
        zIndex: true,
        nodeReducer: (node, data) => {
          const focus = hoveredRef.current ?? selectedRef.current;
          const connected = node === focus || graph.areNeighbors(node, focus);
          if (!connected) return { ...data, color: '#26261f', label: '', zIndex: 0 };
          if (node === focus) return { ...data, highlighted: true, size: data.size * 1.22, zIndex: 2 };
          return { ...data, zIndex: 1 };
        },
        edgeReducer: (edge, data) => {
          const focus = hoveredRef.current ?? selectedRef.current;
          const [source, target] = graph.extremities(edge);
          const connected = source === focus || target === focus;
          return connected
            ? { ...data, color: '#f0854a', size: 2.4, zIndex: 1 }
            : { ...data, color: '#26261f', size: 0.8, zIndex: 0 };
        },
      });
      rendererRef.current = renderer;

      renderer.on('enterNode', ({ node }) => {
        hoveredRef.current = node;
        renderer?.refresh();
      });
      renderer.on('leaveNode', () => {
        hoveredRef.current = null;
        renderer?.refresh();
      });
      renderer.on('clickNode', ({ node }) => {
        selectedRef.current = node;
        setSelectedId(node);
        renderer?.refresh();
      });
      renderer.on('clickStage', () => {
        hoveredRef.current = null;
        renderer?.refresh();
      });
    } catch {
      queueMicrotask(() => {
        if (active) setRenderStatus('error');
      });
    }

    return () => {
      active = false;
      renderer?.kill();
      if (rendererRef.current === renderer) rendererRef.current = null;
    };
  }, [model]);

  function selectNode(id: string): void {
    selectedRef.current = id;
    setSelectedId(id);
    rendererRef.current?.refresh();
  }

  return (
    <section className="constellation" aria-labelledby="constellation-heading">
      <header className="instrument-heading">
        <div>
          <p className="instrument-kicker">Optional provenance explorer</p>
          <h2 id="constellation-heading">Evidence constellation</h2>
          <p>Explore how the saved question, inputs, sources, context, trace, answer, and citations connect.</p>
        </div>
        <span className="constellation__count">{model.nodes.length} evidence nodes</span>
      </header>

      <div className="constellation-stage">
        <div className="constellation-canvas-wrap">
          {renderStatus === 'error' ? (
            <p className="constellation-error" role="alert">
              WebGL is unavailable. Use the complete evidence index below.
            </p>
          ) : null}
          <div
            aria-hidden={renderStatus !== 'ready'}
            className="constellation-canvas"
            ref={containerRef}
          />
          <div className="constellation-key" aria-hidden>
            <span data-kind="source">Sources</span>
            <span data-kind="process">Process</span>
            <span data-kind="answer">Result</span>
          </div>
        </div>

        <aside aria-live="polite" className="constellation-inspector">
          <p className="instrument-kicker">{selected.kind}</p>
          <h3>{selected.label}</h3>
          <p>{selected.description}</p>
          {selected.artifact ? <ArtifactLink artifact={selected.artifact} /> : (
            <p className="muted-copy">This conceptual anchor links the evidence graph and has no separate file.</p>
          )}
        </aside>
      </div>

      <div className="constellation-index" aria-label="Evidence constellation index">
        {model.nodes.map((node) => (
          <button
            aria-pressed={selected.id === node.id}
            key={node.id}
            onClick={() => selectNode(node.id)}
            type="button"
          >
            <i data-kind={node.kind} />
            <span>{node.label}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
