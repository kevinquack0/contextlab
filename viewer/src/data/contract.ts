export const VIEWER_SCHEMA_VERSION = 'contextlab.viewer.v1' as const;

export type ArtifactKind =
  | 'configuration'
  | 'corpus'
  | 'export-manifest'
  | 'memory'
  | 'method'
  | 'prompt'
  | 'report'
  | 'run'
  | 'run-facts'
  | 'source'
  | 'tool-result'
  | 'trace';

/**
 * A saved file and the export-provided URL that opens that file. The viewer never
 * constructs artifact URLs from repository paths.
 */
export interface ArtifactRef {
  kind: ArtifactKind;
  label: string;
  path: string;
  sha256: string;
  staticUrl: string;
  mediaType: string;
}

export interface ProvenanceRef {
  artifact: ArtifactRef;
  runIds: string[];
  jsonPointer: string;
}

export type MetricUnit =
  | 'count'
  | 'milliseconds'
  | 'percent'
  | 'ratio'
  | 'score'
  | 'tokens'
  | 'usd';

/** Every displayed measured number uses this wrapper. */
export interface MetricValue {
  value: number;
  unit: MetricUnit;
  display: string;
  provenance: ProvenanceRef;
}

export type JsonValue =
  | boolean
  | null
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface StrategyRecord {
  id: string;
  label: string;
  summary: string;
  artifact: ArtifactRef;
}

export interface QuestionRecord {
  id: string;
  text: string;
  taskFamily: string;
  artifact: ArtifactRef;
  /** Exactly one saved run for each strategy, in strategy order. */
  comparisonRunIds: string[];
}

export interface CitationRecord {
  id: string;
  sourceId: string;
  sectionId: string;
  label: string;
  excerpt: string;
  /** Full saved source document. */
  source: ArtifactRef;
  /** Exact content-addressed section or JSONL record opened by the citation. */
  target: ArtifactRef;
  provenance: ProvenanceRef;
}

export interface ConfigurationSnapshot {
  id: string;
  artifact: ArtifactRef;
  values: Record<string, JsonValue>;
}

export type PipelineStageKind =
  | 'retrieval'
  | 'fusion'
  | 'reranking'
  | 'deduplication'
  | 'diversity'
  | 'budget'
  | 'context';

export type CandidateDecision = 'kept' | 'removed';

export interface PipelineCandidate {
  id: string;
  citation: CitationRecord;
  origin: 'corpus' | 'memory';
  rank: MetricValue;
  /** Null when the saved retriever trace did not score a fallback candidate. */
  score: MetricValue | null;
  decision: CandidateDecision;
  reason: string | null;
  tokenCount: MetricValue;
  contextOrder: MetricValue | null;
}

export interface PipelineStage {
  id: string;
  kind: PipelineStageKind;
  label: string;
  artifact: ArtifactRef;
  candidates: PipelineCandidate[];
}

export interface EvidencePipeline {
  contextBudget: MetricValue;
  contextUsed: MetricValue;
  stages: PipelineStage[];
}

export type TraceStatus = 'error' | 'ok';

export interface TraceSpan {
  id: string;
  parentId: string | null;
  name: string;
  startedAt: string;
  status: TraceStatus;
  duration: MetricValue;
  artifact: ArtifactRef;
  toolResult: ArtifactRef | null;
}

export interface RunRecord {
  id: string;
  questionId: string;
  strategyId: string;
  reasoningEffort: string;
  runArtifact: ArtifactRef;
  rawOutput: ArtifactRef;
  configuration: ConfigurationSnapshot;
  corpusSnapshot: ArtifactRef;
  memorySnapshot: ArtifactRef;
  prompt: ArtifactRef;
  executionFacts: ArtifactRef;
  answer: {
    text: string;
    citations: CitationRecord[];
  };
  metrics: {
    contextTokens: MetricValue;
    latency: MetricValue;
    estimatedCost: MetricValue;
  };
  executionStatus: string;
  pipeline: EvidencePipeline;
  traceSpans: TraceSpan[];
}

export interface SourceRunRecord {
  id: string;
  suite: 'static' | 'temporal';
  taskId: string;
  taskFamily: string;
  strategyId: string;
  reasoningEffort: string | null;
  artifact: ArtifactRef;
  jsonPointer: string;
}

export type ClaimState = 'active' | 'superseded';

export interface ClaimEvent {
  id: string;
  label: string;
  claim: string;
  state: ClaimState;
  effectiveAt: string;
  authority: MetricValue;
  source: ArtifactRef;
  supersedesEventId: string | null;
}

export interface TemporalEvidenceCase {
  id: string;
  title: string;
  questionId: string;
  baselineRunId: string;
  memoryEvidenceRunId: string;
  artifact: ArtifactRef;
  events: ClaimEvent[];
}

export interface StrategyMatrixCell {
  taskFamily: string;
  reasoningEffort: 'low' | 'high';
  strategyId: string;
  artifact: ArtifactRef;
  completionRatio: MetricValue;
  meanCandidateEvidence: MetricValue;
  meanSelectedEvidence: MetricValue;
  contextBudget: MetricValue;
  meanContextTokens: MetricValue;
  meanLatency: MetricValue;
  meanExecutionCost: MetricValue;
  trialCount: MetricValue;
}

export interface ReviewerRecord {
  id: string;
  name: string;
  modelId: string | null;
  reasoningEffort: string | null;
  invocation: string;
  artifact: ArtifactRef;
}

export interface SourceGroup {
  label: string;
  description: string;
  artifacts: ArtifactRef[];
}

export interface ShowcaseInsight {
  title: string;
  explanation: string;
  runIds: string[];
  artifact: ArtifactRef;
}

export interface MethodsRecord {
  experimentalContract: ArtifactRef;
  limitations: string[];
  v1V2Boundary: string;
  reviewBoundary: string;
  sealedDataBoundary: string;
  novaLearnSyntheticStatement: string;
  portugueseSummary: string;
  reviewers: {
    aiJudges: [ReviewerRecord, ReviewerRecord];
    human: ReviewerRecord & { soleHumanReviewer: true };
  };
  sourceMap: SourceGroup[];
}

/**
 * Exact root object the Python export step must generate. No production fixture
 * is bundled with the viewer.
 */
export interface ContextLabViewerExport {
  schemaVersion: typeof VIEWER_SCHEMA_VERSION;
  exportId: string;
  generatedAt: string;
  title: string;
  interfaceLanguage: 'en';
  tccLanguage: 'pt-BR';
  exportManifest: ArtifactRef;
  strategies: [
    StrategyRecord,
    StrategyRecord,
    StrategyRecord,
    StrategyRecord,
    StrategyRecord,
  ];
  questions: QuestionRecord[];
  runs: RunRecord[];
  sourceRuns: SourceRunRecord[];
  temporalEvidenceCases: TemporalEvidenceCase[];
  showcase: {
    retrievalWin: ShowcaseInsight;
    temporalEvidence: ShowcaseInsight;
    executionFailure?: ShowcaseInsight;
  };
  strategyMatrix: {
    artifact: ArtifactRef;
    cells: StrategyMatrixCell[];
  };
  methods: MethodsRecord;
}
