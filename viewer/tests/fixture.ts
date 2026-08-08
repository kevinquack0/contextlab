/* TEST-ONLY data. This module is not imported by src or copied into the build. */
import type {
  ArtifactKind,
  ArtifactRef,
  ContextLabViewerExport,
  MetricUnit,
  MetricValue,
  PipelineStageKind,
  RunRecord,
} from '../src/data/contract';

const HEX = ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'a', 'b', 'c', 'd', 'e', 'f'];

function hash(seed: number): string {
  return HEX[seed % HEX.length].repeat(64);
}

function artifact(
  label: string,
  path: string,
  seed: number,
  kind: ArtifactKind = 'report',
  mediaType = 'application/json',
): ArtifactRef {
  const sha256 = hash(seed);
  const filename = path.split('/').at(-1) ?? `artifact-${seed}.json`;
  return {
    kind,
    label,
    path,
    sha256,
    staticUrl: `./artifacts/${sha256}/${filename}`,
    mediaType,
  };
}

function metric(
  value: number,
  display: string,
  unit: MetricUnit,
  runId: string,
  seed: number,
): MetricValue {
  return {
    value,
    display,
    unit,
    provenance: {
      artifact: artifact(`${runId} metric`, `results/v2/runs/${runId}.json`, seed, 'run'),
      runIds: [runId],
      jsonPointer: '/metrics',
    },
  };
}

const strategyIds = [
  'full_context',
  'v1_dense_rag',
  'compiled_wiki',
  'text_to_sql',
  'promoted_v2',
] as const;

const strategies: ContextLabViewerExport['strategies'] = strategyIds.map((id, index) => ({
  id,
  label: ['Full context', 'v1 dense RAG', 'Compiled wiki', 'Text to SQL', 'Promoted v2'][index],
  summary: 'Frozen test strategy.',
  artifact: artifact(`${id} definition`, `evaluation/v2/strategies/${id}.json`, index + 1, 'configuration'),
})) as ContextLabViewerExport['strategies'];

const stageKinds: PipelineStageKind[] = [
  'retrieval',
  'fusion',
  'reranking',
  'deduplication',
  'diversity',
  'budget',
  'context',
];

function makeRun(strategyId: string, index: number): RunRecord {
  const runId = `run-${index + 1}`;
  const source = artifact('Test source', 'novalearn_synthetic_corpus/NL-001.md', 10, 'source', 'text/markdown');
  const runArtifact = artifact(`${runId} record`, `results/v2/runs/${runId}.json`, index + 20, 'run');
  const citation = {
    id: `${runId}-citation`,
    sourceId: 'NL-001',
    sectionId: 'NL-001-S01',
    label: 'NL-001#NL-001-S01',
    excerpt: 'Saved test evidence.',
    source,
    target: artifact(
      'Exact test section',
      'viewer/public/artifacts/section/NL-001-S01.md',
      11,
      'source',
      'text/markdown',
    ),
    provenance: {
      artifact: runArtifact,
      runIds: [runId],
      jsonPointer: '/answer/citations/0',
    },
  };

  return {
    id: runId,
    questionId: 'Q001',
    strategyId,
    reasoningEffort: index % 2 === 0 ? 'low' : 'high',
    runArtifact,
    rawOutput: artifact(`${runId} raw output`, `results/v2/raw/${runId}.json`, index + 30, 'run'),
    configuration: {
      id: `config-${index + 1}`,
      artifact: artifact(`${runId} config`, `results/v2/configs/config-${index + 1}.json`, index + 40, 'configuration'),
      values: { topK: index + 4, reasoningEffort: index % 2 === 0 ? 'low' : 'high' },
    },
    corpusSnapshot: artifact('Corpus snapshot', 'results/v2/snapshots/corpus.json', 50, 'corpus'),
    memorySnapshot: artifact(`${runId} memory`, `results/v2/snapshots/${runId}-memory.json`, index + 51, 'memory'),
    prompt: artifact(`${runId} prompt`, `evaluation/v2/prompts/${runId}.md`, index + 61, 'prompt', 'text/markdown'),
    executionFacts: artifact(`${runId} execution facts`, `results/v2/runs/${runId}-facts.json`, index + 71, 'run-facts'),
    answer: {
      text: index === 0 ? 'The earlier event is cited.' : 'The later event is cited.',
      citations: [citation],
    },
    metrics: {
      contextTokens: metric(800 + index, `${800 + index} tokens`, 'tokens', runId, index + 80),
      latency: metric(1200 + index, `${1200 + index} ms`, 'milliseconds', runId, index + 85),
      estimatedCost: metric(0.002 + index * 0.001, `$${(0.002 + index * 0.001).toFixed(3)}`, 'usd', runId, index + 90),
    },
    executionStatus: 'completed',
    pipeline: {
      contextBudget: metric(2048, '2,048 tokens', 'tokens', runId, index + 100),
      contextUsed: metric(800 + index, `${800 + index} tokens`, 'tokens', runId, index + 105),
      stages: stageKinds.map((kind, stageIndex) => ({
        id: `${runId}-${kind}`,
        kind,
        label: kind[0].toUpperCase() + kind.slice(1),
        artifact: artifact(`${kind} output`, `results/v2/traces/${runId}/${kind}.json`, stageIndex + 110, 'trace'),
        candidates: [
          {
            id: `${runId}-${kind}-candidate-1`,
            citation: { ...citation, id: `${citation.id}-${kind}` },
            origin: 'corpus',
            rank: metric(1, '1', 'count', runId, stageIndex + 120),
            score: metric(0.91 - stageIndex * 0.01, (0.91 - stageIndex * 0.01).toFixed(2), 'score', runId, stageIndex + 130),
            decision: 'kept',
            reason: null,
            tokenCount: metric(210, '210', 'tokens', runId, stageIndex + 140),
            contextOrder: kind === 'context' ? metric(1, '1', 'count', runId, stageIndex + 150) : null,
          },
        ],
      })),
    },
    traceSpans: [
      {
        id: `${runId}-span`,
        parentId: null,
        name: 'saved retrieval',
        startedAt: '2026-08-05T18:00:00Z',
        status: 'ok',
        duration: metric(42 + index, `${42 + index} ms`, 'milliseconds', runId, index + 160),
        artifact: artifact(`${runId} span`, `results/v2/traces/${runId}/span.json`, index + 170, 'trace'),
        toolResult: artifact(`${runId} tool result`, `results/v2/traces/${runId}/tool.json`, index + 180, 'tool-result'),
      },
    ],
  };
}

const runs = strategyIds.map(makeRun);

export const validViewerExport: ContextLabViewerExport = {
  schemaVersion: 'contextlab.viewer.v1',
  exportId: 'fixture-export',
  generatedAt: '2026-08-05T20:00:00Z',
  title: 'Test evidence export',
  interfaceLanguage: 'en',
  tccLanguage: 'pt-BR',
  exportManifest: artifact('Export manifest', 'results/v2/viewer/manifest.json', 190, 'export-manifest'),
  strategies,
  questions: [
    {
      id: 'Q001',
      text: 'Which saved claim is current?',
      taskFamily: 'temporal',
      artifact: artifact('Question record', 'evaluation/v2/questions/Q001.json', 191, 'method'),
      comparisonRunIds: runs.map((run) => run.id),
    },
  ],
  runs,
  sourceRuns: runs.map((run) => ({
    id: run.id,
    suite: 'temporal',
    taskId: 'Q001',
    taskFamily: 'temporal',
    strategyId: run.strategyId,
    reasoningEffort: run.reasoningEffort,
    artifact: run.runArtifact,
    jsonPointer: '/',
  })),
  showcase: {
    retrievalWin: {
      title: 'Saved retrieval difference',
      explanation: 'The saved public component runs record different retrieval values.',
      runIds: ['run-5'],
      artifact: artifact('Retrieval win report', 'results/v2/reports/retrieval-win.json', 250, 'report'),
    },
    temporalEvidence: {
      title: 'Saved temporal evidence comparison',
      explanation: 'Two public runs reference the same linked event sequence.',
      runIds: ['run-1', 'run-5'],
      artifact: artifact('Temporal event stream', 'results/v2/reports/temporal-events.json', 251, 'report'),
    },
  },
  temporalEvidenceCases: [
    {
      id: 'temporal-case',
      title: 'Public event transition',
      questionId: 'Q001',
      baselineRunId: 'run-1',
      memoryEvidenceRunId: 'run-5',
      artifact: artifact('Temporal case', 'results/v2/memory/temporal-case.json', 192, 'report'),
      events: [
        {
          id: 'claim-old',
          label: 'Earlier claim',
          claim: 'The earlier claim was active.',
          state: 'superseded',
          effectiveAt: '2026-01-01T00:00:00Z',
          authority: metric(2, '2 of 5', 'score', 'run-1', 193),
          source: artifact('Earlier source', 'novalearn_synthetic_corpus/NL-001.md', 194, 'source', 'text/markdown'),
          supersedesEventId: null,
        },
        {
          id: 'claim-new',
          label: 'Later event',
          claim: 'The later event is active.',
          state: 'active',
          effectiveAt: '2026-06-01T00:00:00Z',
          authority: metric(5, '5 of 5', 'score', 'run-5', 195),
          source: artifact('Later source', 'novalearn_synthetic_corpus/NL-002.md', 196, 'source', 'text/markdown'),
          supersedesEventId: 'claim-old',
        },
      ],
    },
  ],
  strategyMatrix: {
    artifact: artifact('Strategy matrix', 'results/v2/reports/strategy-matrix.json', 197, 'report'),
    cells: strategyIds.map((strategyId, index) => {
      const runId = `run-${index + 1}`;
      return {
        taskFamily: 'temporal',
        reasoningEffort: 'low',
        strategyId,
        artifact: artifact(`${strategyId} aggregate`, `results/v2/reports/${strategyId}.json`, index + 198, 'report'),
        completionRatio: metric(1, '100% completed', 'ratio', runId, index + 203),
        meanCandidateEvidence: metric(12 + index, `${12 + index} candidates`, 'count', runId, index + 208),
        meanSelectedEvidence: metric(4 + index, `${4 + index} selected rows`, 'count', runId, index + 209),
        contextBudget: metric(2048, '2,048 tokens', 'tokens', runId, index + 213),
        meanContextTokens: metric(800 + index, `${800 + index} tokens`, 'tokens', runId, index + 218),
        meanLatency: metric(1200 + index, `${1200 + index} ms`, 'milliseconds', runId, index + 223),
        meanExecutionCost: metric(0.01 + index * 0.002, `$${(0.01 + index * 0.002).toFixed(3)}`, 'usd', runId, index + 228),
        trialCount: metric(32, '32', 'count', runId, index + 233),
      };
    }),
  },
  methods: {
    experimentalContract: artifact('Experiment charter', 'docs/CONTEXTLAB_V2_EXPERIMENT_CHARTER.md', 240, 'method', 'text/markdown'),
    limitations: ['One human reviewer participated.', 'This fixture contains test-only synthetic evidence.'],
    v1V2Boundary: 'v1 is preserved as a baseline. v2 is evaluated under the frozen charter.',
    reviewBoundary: 'Review packets hide strategy and reasoning identities.',
    sealedDataBoundary: 'Sealed expected answers and gold labels remain outside the repository.',
    novaLearnSyntheticStatement: 'NovaLearn is a synthetic company and corpus used only for this study.',
    portugueseSummary: 'Este visualizador apresenta somente execuções salvas e mantém a proveniência de cada resultado.',
    reviewers: {
      aiJudges: [
        {
          id: 'gpt-5.6-sol-high',
          name: 'GPT-5.6 Sol high',
          modelId: 'gpt-5.6-sol',
          reasoningEffort: 'high',
          invocation: 'Codex subagent',
          artifact: artifact('Review protocol', 'evaluation/v2/review_protocol.json', 241, 'method'),
        },
        {
          id: 'claude-opus-5-medium',
          name: 'Claude Opus 5 medium',
          modelId: 'claude-opus-5',
          reasoningEffort: 'medium',
          invocation: 'Local Claude CLI',
          artifact: artifact('Review protocol', 'evaluation/v2/review_protocol.json', 241, 'method'),
        },
      ],
      human: {
        id: 'kevin',
        name: 'Kevin Araujo',
        modelId: null,
        reasoningEffort: null,
        invocation: 'Local resumable review interface',
        artifact: artifact('Review protocol', 'evaluation/v2/review_protocol.json', 241, 'method'),
        soleHumanReviewer: true,
      },
    },
    sourceMap: [
      {
        label: 'Primary sources',
        description: 'Saved corpus sources used by the test run.',
        artifacts: [artifact('Test source', 'novalearn_synthetic_corpus/NL-001.md', 10, 'source', 'text/markdown')],
      },
      {
        label: 'AI-Brain synthesis',
        description: 'Local synthesis is separated from primary experiment evidence.',
        artifacts: [artifact('Source contract', 'docs/CONTEXTLAB_V2_EXPERIMENT_CHARTER.md', 240, 'method', 'text/markdown')],
      },
    ],
  },
};
