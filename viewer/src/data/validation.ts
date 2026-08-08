import {
  VIEWER_SCHEMA_VERSION,
  type ArtifactKind,
  type ArtifactRef,
  type ContextLabViewerExport,
  type MetricUnit,
} from './contract';

export interface ValidationIssue {
  path: string;
  message: string;
}

export type ValidationResult =
  | { ok: true; data: ContextLabViewerExport }
  | { ok: false; issues: ValidationIssue[] };

const SHA256 = /^[a-f0-9]{64}$/i;
const CONTENT_ADDRESSED_URL = /^\.\/artifacts\/([a-f0-9]{64})\/[^/?#]+$/;
const INVALID_JSON_POINTER_ESCAPE = /~(?![01])/;
const FORBIDDEN_PUBLIC_PATH_TOKENS = [
  'evaluation_only_do_not_index',
  'protected',
  'sealed',
  'gold',
  '/grades/',
  'scoring',
] as const;
const ARTIFACT_KINDS = new Set<ArtifactKind>([
  'configuration',
  'corpus',
  'export-manifest',
  'memory',
  'method',
  'prompt',
  'report',
  'run',
  'run-facts',
  'source',
  'tool-result',
  'trace',
]);
const METRIC_UNITS = new Set<MetricUnit>([
  'count',
  'milliseconds',
  'percent',
  'ratio',
  'score',
  'tokens',
  'usd',
]);
const PIPELINE_STAGE_KINDS = new Set([
  'retrieval',
  'fusion',
  'reranking',
  'deduplication',
  'diversity',
  'budget',
  'context',
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function addIssue(issues: ValidationIssue[], path: string, message: string): void {
  issues.push({ path, message });
}

function requireRecord(
  value: unknown,
  path: string,
  issues: ValidationIssue[],
): Record<string, unknown> | null {
  if (!isRecord(value)) {
    addIssue(issues, path, 'must be an object');
    return null;
  }
  return value;
}

function requireArray(
  value: unknown,
  path: string,
  issues: ValidationIssue[],
): unknown[] | null {
  if (!Array.isArray(value)) {
    addIssue(issues, path, 'must be an array');
    return null;
  }
  return value;
}

function requireString(
  value: unknown,
  path: string,
  issues: ValidationIssue[],
  allowEmpty = false,
): value is string {
  if (typeof value !== 'string' || (!allowEmpty && value.trim().length === 0)) {
    addIssue(issues, path, allowEmpty ? 'must be a string' : 'must be a non-empty string');
    return false;
  }
  return true;
}

function requireIsoDate(value: unknown, path: string, issues: ValidationIssue[]): void {
  if (!requireString(value, path, issues)) return;
  if (Number.isNaN(Date.parse(value))) addIssue(issues, path, 'must be an ISO date-time string');
}

function validateStaticUrl(
  value: unknown,
  path: string,
  issues: ValidationIssue[],
  digest: unknown,
): void {
  if (!requireString(value, path, issues)) return;
  const match = CONTENT_ADDRESSED_URL.exec(value);
  if (!match) {
    addIssue(issues, path, 'must be a local content-addressed ./artifacts/<sha256>/<file> URL');
    return;
  }
  if (typeof digest !== 'string' || match[1].toLowerCase() !== digest.toLowerCase()) {
    addIssue(issues, path, 'must contain the artifact SHA-256');
  }
}

function validateArtifact(value: unknown, path: string, issues: ValidationIssue[]): void {
  const artifact = requireRecord(value, path, issues);
  if (!artifact) return;

  if (!requireString(artifact.kind, `${path}.kind`, issues) || !ARTIFACT_KINDS.has(artifact.kind as ArtifactKind)) {
    addIssue(issues, `${path}.kind`, 'must be a supported artifact kind');
  }
  requireString(artifact.label, `${path}.label`, issues);
  if (requireString(artifact.path, `${path}.path`, issues)) {
    const segments = artifact.path.split('/');
    if (artifact.path.startsWith('/') || segments.includes('..')) {
      addIssue(issues, `${path}.path`, 'must be a repository-relative path without parent traversal');
    }
    const lowered = `/${artifact.path.toLowerCase()}`;
    if (FORBIDDEN_PUBLIC_PATH_TOKENS.some((token) => lowered.includes(token))) {
      addIssue(issues, `${path}.path`, 'must not expose sealed, protected, gold, grade, or scoring paths');
    }
  }
  if (!requireString(artifact.sha256, `${path}.sha256`, issues) || !SHA256.test(artifact.sha256)) {
    addIssue(issues, `${path}.sha256`, 'must be a 64-character SHA-256 hex digest');
  }
  validateStaticUrl(artifact.staticUrl, `${path}.staticUrl`, issues, artifact.sha256);
  requireString(artifact.mediaType, `${path}.mediaType`, issues);
}

function validateJsonPointer(value: unknown, path: string, issues: ValidationIssue[]): void {
  if (!requireString(value, path, issues)) return;
  if (
    !value.startsWith('/') ||
    INVALID_JSON_POINTER_ESCAPE.test(value) ||
    [...value].some((character) => character.charCodeAt(0) < 0x20)
  ) {
    addIssue(issues, path, 'must be a well-formed absolute JSON pointer');
  }
}

function validateMetric(
  value: unknown,
  path: string,
  issues: ValidationIssue[],
  runIds: ReadonlySet<string>,
): void {
  const metric = requireRecord(value, path, issues);
  if (!metric) return;
  if (typeof metric.value !== 'number' || !Number.isFinite(metric.value)) {
    addIssue(issues, `${path}.value`, 'must be a finite number');
  }
  if (!requireString(metric.unit, `${path}.unit`, issues) || !METRIC_UNITS.has(metric.unit as MetricUnit)) {
    addIssue(issues, `${path}.unit`, 'must be a supported metric unit');
  }
  requireString(metric.display, `${path}.display`, issues);
  const provenance = requireRecord(metric.provenance, `${path}.provenance`, issues);
  if (!provenance) return;
  validateArtifact(provenance.artifact, `${path}.provenance.artifact`, issues);
  const metricRunIds = requireArray(provenance.runIds, `${path}.provenance.runIds`, issues);
  if (!metricRunIds) return;
  if (metricRunIds.length === 0) {
    addIssue(issues, `${path}.provenance.runIds`, 'must name at least one source run');
  }
  metricRunIds.forEach((runId, index) => {
    const runPath = `${path}.provenance.runIds[${index}]`;
    if (requireString(runId, runPath, issues) && !runIds.has(runId)) {
      addIssue(issues, runPath, `references unknown run ${runId}`);
    }
  });
  validateJsonPointer(provenance.jsonPointer, `${path}.provenance.jsonPointer`, issues);
}

function validateCitation(
  value: unknown,
  path: string,
  issues: ValidationIssue[],
  runIds: ReadonlySet<string>,
): void {
  const citation = requireRecord(value, path, issues);
  if (!citation) return;
  requireString(citation.id, `${path}.id`, issues);
  requireString(citation.sourceId, `${path}.sourceId`, issues);
  requireString(citation.sectionId, `${path}.sectionId`, issues);
  requireString(citation.label, `${path}.label`, issues);
  requireString(citation.excerpt, `${path}.excerpt`, issues, true);
  validateArtifact(citation.source, `${path}.source`, issues);
  validateArtifact(citation.target, `${path}.target`, issues);
  const provenance = requireRecord(citation.provenance, `${path}.provenance`, issues);
  if (!provenance) return;
  validateArtifact(provenance.artifact, `${path}.provenance.artifact`, issues);
  const citationRunIds = requireArray(provenance.runIds, `${path}.provenance.runIds`, issues);
  if (citationRunIds?.length === 0) {
    addIssue(issues, `${path}.provenance.runIds`, 'must name at least one source run');
  }
  citationRunIds?.forEach((runId, index) => {
    const runPath = `${path}.provenance.runIds[${index}]`;
    if (requireString(runId, runPath, issues) && !runIds.has(runId)) {
      addIssue(issues, runPath, `references unknown run ${runId}`);
    }
  });
  validateJsonPointer(provenance.jsonPointer, `${path}.provenance.jsonPointer`, issues);
}

function validateStringArray(value: unknown, path: string, issues: ValidationIssue[]): void {
  const values = requireArray(value, path, issues);
  values?.forEach((entry, index) => requireString(entry, `${path}[${index}]`, issues));
}

function validateStrategies(root: Record<string, unknown>, issues: ValidationIssue[]): Set<string> {
  const strategies = requireArray(root.strategies, '$.strategies', issues);
  const ids = new Set<string>();
  if (!strategies) return ids;
  if (strategies.length !== 5) addIssue(issues, '$.strategies', 'must contain exactly five strategy lanes');
  strategies.forEach((value, index) => {
    const path = `$.strategies[${index}]`;
    const strategy = requireRecord(value, path, issues);
    if (!strategy) return;
    if (requireString(strategy.id, `${path}.id`, issues)) {
      if (ids.has(strategy.id)) addIssue(issues, `${path}.id`, 'must be unique');
      ids.add(strategy.id);
    }
    requireString(strategy.label, `${path}.label`, issues);
    requireString(strategy.summary, `${path}.summary`, issues);
    validateArtifact(strategy.artifact, `${path}.artifact`, issues);
  });
  return ids;
}

function collectIds(values: unknown, path: string, issues: ValidationIssue[]): Set<string> {
  const ids = new Set<string>();
  const array = requireArray(values, path, issues);
  array?.forEach((value, index) => {
    const record = requireRecord(value, `${path}[${index}]`, issues);
    if (!record || !requireString(record.id, `${path}[${index}].id`, issues)) return;
    if (ids.has(record.id)) addIssue(issues, `${path}[${index}].id`, 'must be unique');
    ids.add(record.id);
  });
  return ids;
}

function validatePipeline(
  value: unknown,
  path: string,
  issues: ValidationIssue[],
  runIds: ReadonlySet<string>,
): void {
  const pipeline = requireRecord(value, path, issues);
  if (!pipeline) return;
  validateMetric(pipeline.contextBudget, `${path}.contextBudget`, issues, runIds);
  validateMetric(pipeline.contextUsed, `${path}.contextUsed`, issues, runIds);
  const stages = requireArray(pipeline.stages, `${path}.stages`, issues);
  const presentKinds = new Set<string>();
  stages?.forEach((value, stageIndex) => {
    const stagePath = `${path}.stages[${stageIndex}]`;
    const stage = requireRecord(value, stagePath, issues);
    if (!stage) return;
    requireString(stage.id, `${stagePath}.id`, issues);
    if (
      requireString(stage.kind, `${stagePath}.kind`, issues) &&
      PIPELINE_STAGE_KINDS.has(stage.kind)
    ) {
      presentKinds.add(stage.kind);
    } else {
      addIssue(issues, `${stagePath}.kind`, 'must be a supported pipeline stage kind');
    }
    requireString(stage.label, `${stagePath}.label`, issues);
    validateArtifact(stage.artifact, `${stagePath}.artifact`, issues);
    const candidates = requireArray(stage.candidates, `${stagePath}.candidates`, issues);
    const candidateIds = new Set<string>();
    candidates?.forEach((candidateValue, candidateIndex) => {
      const candidatePath = `${stagePath}.candidates[${candidateIndex}]`;
      const candidate = requireRecord(candidateValue, candidatePath, issues);
      if (!candidate) return;
      if (requireString(candidate.id, `${candidatePath}.id`, issues)) {
        if (candidateIds.has(candidate.id)) addIssue(issues, `${candidatePath}.id`, 'must be unique within the stage');
        candidateIds.add(candidate.id);
      }
      validateCitation(candidate.citation, `${candidatePath}.citation`, issues, runIds);
      if (!['corpus', 'memory'].includes(String(candidate.origin))) {
        addIssue(issues, `${candidatePath}.origin`, 'must be corpus or memory');
      }
      validateMetric(candidate.rank, `${candidatePath}.rank`, issues, runIds);
      if (candidate.score !== null) {
        validateMetric(candidate.score, `${candidatePath}.score`, issues, runIds);
      }
      if (candidate.decision !== 'kept' && candidate.decision !== 'removed') {
        addIssue(issues, `${candidatePath}.decision`, 'must be kept or removed');
      }
      if (candidate.reason !== null) requireString(candidate.reason, `${candidatePath}.reason`, issues);
      validateMetric(candidate.tokenCount, `${candidatePath}.tokenCount`, issues, runIds);
      if (candidate.contextOrder !== null) {
        validateMetric(candidate.contextOrder, `${candidatePath}.contextOrder`, issues, runIds);
      }
    });
  });
  for (const requiredKind of PIPELINE_STAGE_KINDS) {
    if (!presentKinds.has(requiredKind)) {
      addIssue(issues, `${path}.stages`, `must include a ${requiredKind} stage`);
    }
  }
}

function validateRuns(
  root: Record<string, unknown>,
  issues: ValidationIssue[],
  runIds: ReadonlySet<string>,
  questionIds: ReadonlySet<string>,
  strategyIds: ReadonlySet<string>,
): void {
  const runs = requireArray(root.runs, '$.runs', issues);
  runs?.forEach((value, index) => {
    const path = `$.runs[${index}]`;
    const run = requireRecord(value, path, issues);
    if (!run) return;
    requireString(run.id, `${path}.id`, issues);
    if (requireString(run.questionId, `${path}.questionId`, issues) && !questionIds.has(run.questionId)) {
      addIssue(issues, `${path}.questionId`, `references unknown question ${run.questionId}`);
    }
    if (requireString(run.strategyId, `${path}.strategyId`, issues) && !strategyIds.has(run.strategyId)) {
      addIssue(issues, `${path}.strategyId`, `references unknown strategy ${run.strategyId}`);
    }
    requireString(run.reasoningEffort, `${path}.reasoningEffort`, issues);
    validateArtifact(run.runArtifact, `${path}.runArtifact`, issues);
    validateArtifact(run.rawOutput, `${path}.rawOutput`, issues);
    validateArtifact(run.corpusSnapshot, `${path}.corpusSnapshot`, issues);
    validateArtifact(run.memorySnapshot, `${path}.memorySnapshot`, issues);
    validateArtifact(run.prompt, `${path}.prompt`, issues);
    validateArtifact(run.executionFacts, `${path}.executionFacts`, issues);

    const configuration = requireRecord(run.configuration, `${path}.configuration`, issues);
    if (configuration) {
      requireString(configuration.id, `${path}.configuration.id`, issues);
      validateArtifact(configuration.artifact, `${path}.configuration.artifact`, issues);
      requireRecord(configuration.values, `${path}.configuration.values`, issues);
    }

    const answer = requireRecord(run.answer, `${path}.answer`, issues);
    if (answer) {
      requireString(answer.text, `${path}.answer.text`, issues, true);
      const citations = requireArray(answer.citations, `${path}.answer.citations`, issues);
      citations?.forEach((citation, citationIndex) =>
        validateCitation(citation, `${path}.answer.citations[${citationIndex}]`, issues, runIds),
      );
    }

    const metrics = requireRecord(run.metrics, `${path}.metrics`, issues);
    if (metrics) {
      validateMetric(metrics.contextTokens, `${path}.metrics.contextTokens`, issues, runIds);
      validateMetric(metrics.latency, `${path}.metrics.latency`, issues, runIds);
      validateMetric(metrics.estimatedCost, `${path}.metrics.estimatedCost`, issues, runIds);
    }
    requireString(run.executionStatus, `${path}.executionStatus`, issues);
    validatePipeline(run.pipeline, `${path}.pipeline`, issues, runIds);

    const spans = requireArray(run.traceSpans, `${path}.traceSpans`, issues);
    if (spans?.length === 0) {
      addIssue(issues, `${path}.traceSpans`, 'must include at least one observable span');
    }
    let hasToolResult = false;
    spans?.forEach((spanValue, spanIndex) => {
      const spanPath = `${path}.traceSpans[${spanIndex}]`;
      const span = requireRecord(spanValue, spanPath, issues);
      if (!span) return;
      requireString(span.id, `${spanPath}.id`, issues);
      if (span.parentId !== null) requireString(span.parentId, `${spanPath}.parentId`, issues);
      requireString(span.name, `${spanPath}.name`, issues);
      requireIsoDate(span.startedAt, `${spanPath}.startedAt`, issues);
      if (span.status !== 'ok' && span.status !== 'error') {
        addIssue(issues, `${spanPath}.status`, 'must be ok or error');
      }
      validateMetric(span.duration, `${spanPath}.duration`, issues, runIds);
      validateArtifact(span.artifact, `${spanPath}.artifact`, issues);
      if (span.toolResult !== null) {
        hasToolResult = true;
        validateArtifact(span.toolResult, `${spanPath}.toolResult`, issues);
      }
    });
    if (spans && spans.length > 0 && !hasToolResult) {
      addIssue(issues, `${path}.traceSpans`, 'must include at least one saved tool result');
    }
  });
}

function validateSourceRuns(
  root: Record<string, unknown>,
  issues: ValidationIssue[],
): Set<string> {
  const values = requireArray(root.sourceRuns, '$.sourceRuns', issues);
  const ids = new Set<string>();
  values?.forEach((value, index) => {
    const path = `$.sourceRuns[${index}]`;
    const run = requireRecord(value, path, issues);
    if (!run) return;
    if (requireString(run.id, `${path}.id`, issues)) {
      if (ids.has(run.id)) addIssue(issues, `${path}.id`, 'must be unique');
      ids.add(run.id);
    }
    if (run.suite !== 'static' && run.suite !== 'temporal') {
      addIssue(issues, `${path}.suite`, 'must be static or temporal');
    }
    requireString(run.taskId, `${path}.taskId`, issues);
    requireString(run.taskFamily, `${path}.taskFamily`, issues);
    requireString(run.strategyId, `${path}.strategyId`, issues);
    if (run.reasoningEffort !== null) {
      requireString(run.reasoningEffort, `${path}.reasoningEffort`, issues);
    }
    validateArtifact(run.artifact, `${path}.artifact`, issues);
    validateJsonPointer(run.jsonPointer, `${path}.jsonPointer`, issues);
  });
  if (values?.length === 0) addIssue(issues, '$.sourceRuns', 'must contain saved source runs');
  return ids;
}

function validateQuestions(
  root: Record<string, unknown>,
  issues: ValidationIssue[],
  runIds: ReadonlySet<string>,
  strategyIds: ReadonlySet<string>,
): Set<string> {
  const questions = requireArray(root.questions, '$.questions', issues);
  const ids = new Set<string>();
  const runs = Array.isArray(root.runs) ? root.runs : [];
  const runById = new Map<string, Record<string, unknown>>();
  for (const value of runs) {
    if (isRecord(value) && typeof value.id === 'string') runById.set(value.id, value);
  }

  questions?.forEach((value, index) => {
    const path = `$.questions[${index}]`;
    const question = requireRecord(value, path, issues);
    if (!question) return;
    if (requireString(question.id, `${path}.id`, issues)) {
      if (ids.has(question.id)) addIssue(issues, `${path}.id`, 'must be unique');
      ids.add(question.id);
    }
    requireString(question.text, `${path}.text`, issues);
    requireString(question.taskFamily, `${path}.taskFamily`, issues);
    validateArtifact(question.artifact, `${path}.artifact`, issues);
    const comparison = requireArray(question.comparisonRunIds, `${path}.comparisonRunIds`, issues);
    if (!comparison) return;
    if (comparison.length !== 5) {
      addIssue(issues, `${path}.comparisonRunIds`, 'must contain exactly five saved runs');
    }
    const comparedStrategies = new Set<string>();
    comparison.forEach((runId, runIndex) => {
      const runPath = `${path}.comparisonRunIds[${runIndex}]`;
      if (!requireString(runId, runPath, issues)) return;
      if (!runIds.has(runId)) {
        addIssue(issues, runPath, `references unknown run ${runId}`);
        return;
      }
      const run = runById.get(runId);
      if (!run) return;
      if (run.questionId !== question.id) addIssue(issues, runPath, 'must reference the same question');
      if (typeof run.strategyId === 'string') comparedStrategies.add(run.strategyId);
    });
    if (comparedStrategies.size !== strategyIds.size) {
      addIssue(issues, `${path}.comparisonRunIds`, 'must cover each strategy exactly once');
    }
  });
  return ids;
}

function validateTemporalEvidence(
  root: Record<string, unknown>,
  issues: ValidationIssue[],
  runIds: ReadonlySet<string>,
  questionIds: ReadonlySet<string>,
): void {
  const cases = requireArray(root.temporalEvidenceCases, '$.temporalEvidenceCases', issues);
  cases?.forEach((value, index) => {
    const path = `$.temporalEvidenceCases[${index}]`;
    const item = requireRecord(value, path, issues);
    if (!item) return;
    requireString(item.id, `${path}.id`, issues);
    requireString(item.title, `${path}.title`, issues);
    if (requireString(item.questionId, `${path}.questionId`, issues) && !questionIds.has(item.questionId)) {
      addIssue(issues, `${path}.questionId`, 'references an unknown question');
    }
    for (const key of ['baselineRunId', 'memoryEvidenceRunId'] as const) {
      if (requireString(item[key], `${path}.${key}`, issues) && !runIds.has(item[key] as string)) {
        addIssue(issues, `${path}.${key}`, 'references an unknown run');
      }
    }
    validateArtifact(item.artifact, `${path}.artifact`, issues);
    const events = requireArray(item.events, `${path}.events`, issues);
    if (events && events.length < 2) {
      addIssue(issues, `${path}.events`, 'must contain at least two linked public events');
    }
    const eventIds = new Set<string>();
    events?.forEach((eventValue, eventIndex) => {
      const eventPath = `${path}.events[${eventIndex}]`;
      const event = requireRecord(eventValue, eventPath, issues);
      if (!event) return;
      if (requireString(event.id, `${eventPath}.id`, issues)) eventIds.add(event.id);
      requireString(event.label, `${eventPath}.label`, issues);
      requireString(event.claim, `${eventPath}.claim`, issues);
      if (!['active', 'superseded'].includes(String(event.state))) {
        addIssue(issues, `${eventPath}.state`, 'must be active or superseded');
      }
      requireIsoDate(event.effectiveAt, `${eventPath}.effectiveAt`, issues);
      validateMetric(event.authority, `${eventPath}.authority`, issues, runIds);
      validateArtifact(event.source, `${eventPath}.source`, issues);
      if (event.supersedesEventId !== null) {
        requireString(event.supersedesEventId, `${eventPath}.supersedesEventId`, issues);
      }
    });
    events?.forEach((eventValue, eventIndex) => {
      if (
        isRecord(eventValue) &&
        typeof eventValue.supersedesEventId === 'string' &&
        !eventIds.has(eventValue.supersedesEventId)
      ) {
        addIssue(issues, `${path}.events[${eventIndex}].supersedesEventId`, 'references an unknown event');
      }
    });
  });
}

function validateMatrix(
  root: Record<string, unknown>,
  issues: ValidationIssue[],
  runIds: ReadonlySet<string>,
  strategyIds: ReadonlySet<string>,
): void {
  const matrix = requireRecord(root.strategyMatrix, '$.strategyMatrix', issues);
  if (!matrix) return;
  validateArtifact(matrix.artifact, '$.strategyMatrix.artifact', issues);
  const cells = requireArray(matrix.cells, '$.strategyMatrix.cells', issues);
  cells?.forEach((value, index) => {
    const path = `$.strategyMatrix.cells[${index}]`;
    const cell = requireRecord(value, path, issues);
    if (!cell) return;
    requireString(cell.taskFamily, `${path}.taskFamily`, issues);
    if (cell.reasoningEffort !== 'low' && cell.reasoningEffort !== 'high') {
      addIssue(issues, `${path}.reasoningEffort`, 'must be low or high');
    }
    if (requireString(cell.strategyId, `${path}.strategyId`, issues) && !strategyIds.has(cell.strategyId)) {
      addIssue(issues, `${path}.strategyId`, 'references an unknown strategy');
    }
    validateArtifact(cell.artifact, `${path}.artifact`, issues);
    validateMetric(cell.completionRatio, `${path}.completionRatio`, issues, runIds);
    validateMetric(cell.meanCandidateEvidence, `${path}.meanCandidateEvidence`, issues, runIds);
    validateMetric(cell.meanSelectedEvidence, `${path}.meanSelectedEvidence`, issues, runIds);
    validateMetric(cell.contextBudget, `${path}.contextBudget`, issues, runIds);
    validateMetric(cell.meanContextTokens, `${path}.meanContextTokens`, issues, runIds);
    validateMetric(cell.meanLatency, `${path}.meanLatency`, issues, runIds);
    validateMetric(cell.meanExecutionCost, `${path}.meanExecutionCost`, issues, runIds);
    validateMetric(cell.trialCount, `${path}.trialCount`, issues, runIds);
  });
}

function validateShowcase(
  root: Record<string, unknown>,
  issues: ValidationIssue[],
  runIds: ReadonlySet<string>,
): void {
  const showcase = requireRecord(root.showcase, '$.showcase', issues);
  if (!showcase) return;
  const runStatus = new Map<string, unknown>();
  if (Array.isArray(root.runs)) {
    for (const run of root.runs) {
      if (isRecord(run) && typeof run.id === 'string') runStatus.set(run.id, run.executionStatus);
    }
  }
  const allowed = new Set(['retrievalWin', 'temporalEvidence', 'executionFailure']);
  const keys = Object.keys(showcase);
  if (
    !keys.includes('retrievalWin') ||
    !keys.includes('temporalEvidence') ||
    keys.some((key) => !allowed.has(key))
  ) {
    addIssue(issues, '$.showcase', 'must contain retrievalWin, temporalEvidence, and only an optional executionFailure');
  }
  for (const key of keys) {
    const path = `$.showcase.${key}`;
    const insight = requireRecord(showcase[key], path, issues);
    if (!insight) continue;
    requireString(insight.title, `${path}.title`, issues);
    requireString(insight.explanation, `${path}.explanation`, issues);
    validateArtifact(insight.artifact, `${path}.artifact`, issues);
    const insightRunIds = requireArray(insight.runIds, `${path}.runIds`, issues);
    if (insightRunIds?.length === 0 && runIds.size > 0) {
      addIssue(issues, `${path}.runIds`, 'must name at least one source run');
    }
    insightRunIds?.forEach((runId, index) => {
      const runPath = `${path}.runIds[${index}]`;
      if (requireString(runId, runPath, issues) && !runIds.has(runId)) {
        addIssue(issues, runPath, `references unknown run ${runId}`);
      } else if (key === 'executionFailure' && runStatus.get(String(runId)) !== 'failed') {
        addIssue(issues, runPath, 'must reference a run with executionStatus=failed');
      }
    });
  }
}

function validateReviewer(
  value: unknown,
  path: string,
  issues: ValidationIssue[],
  allowNullModel: boolean,
): void {
  const reviewer = requireRecord(value, path, issues);
  if (!reviewer) return;
  requireString(reviewer.id, `${path}.id`, issues);
  requireString(reviewer.name, `${path}.name`, issues);
  if (reviewer.modelId === null && !allowNullModel) {
    addIssue(issues, `${path}.modelId`, 'must name the AI model');
  } else if (reviewer.modelId !== null) {
    requireString(reviewer.modelId, `${path}.modelId`, issues);
  }
  if (reviewer.reasoningEffort === null && !allowNullModel) {
    addIssue(issues, `${path}.reasoningEffort`, 'must name the reasoning effort');
  } else if (reviewer.reasoningEffort !== null) {
    requireString(reviewer.reasoningEffort, `${path}.reasoningEffort`, issues);
  }
  requireString(reviewer.invocation, `${path}.invocation`, issues);
  validateArtifact(reviewer.artifact, `${path}.artifact`, issues);
}

function validateMethods(root: Record<string, unknown>, issues: ValidationIssue[]): void {
  const methods = requireRecord(root.methods, '$.methods', issues);
  if (!methods) return;
  validateArtifact(methods.experimentalContract, '$.methods.experimentalContract', issues);
  validateStringArray(methods.limitations, '$.methods.limitations', issues);
  requireString(methods.v1V2Boundary, '$.methods.v1V2Boundary', issues);
  requireString(methods.reviewBoundary, '$.methods.reviewBoundary', issues);
  requireString(methods.sealedDataBoundary, '$.methods.sealedDataBoundary', issues);
  if (
    requireString(methods.novaLearnSyntheticStatement, '$.methods.novaLearnSyntheticStatement', issues) &&
    !methods.novaLearnSyntheticStatement.toLowerCase().includes('synthetic')
  ) {
    addIssue(issues, '$.methods.novaLearnSyntheticStatement', 'must clearly state that NovaLearn is synthetic');
  }
  requireString(methods.portugueseSummary, '$.methods.portugueseSummary', issues);

  const reviewers = requireRecord(methods.reviewers, '$.methods.reviewers', issues);
  if (reviewers) {
    const aiJudges = requireArray(reviewers.aiJudges, '$.methods.reviewers.aiJudges', issues);
    if (aiJudges && aiJudges.length !== 2) {
      addIssue(issues, '$.methods.reviewers.aiJudges', 'must contain exactly two AI judges');
    }
    aiJudges?.forEach((reviewer, index) =>
      validateReviewer(reviewer, `$.methods.reviewers.aiJudges[${index}]`, issues, false),
    );
    const modelIds = new Set(
      aiJudges?.flatMap((reviewer) =>
        isRecord(reviewer) && typeof reviewer.modelId === 'string' ? [reviewer.modelId] : [],
      ),
    );
    if (!modelIds.has('gpt-5.6-sol') || !modelIds.has('claude-opus-5')) {
      addIssue(issues, '$.methods.reviewers.aiJudges', 'must name GPT-5.6 Sol and Claude Opus 5');
    }
    const gptJudge = aiJudges?.find(
      (reviewer) => isRecord(reviewer) && reviewer.modelId === 'gpt-5.6-sol',
    );
    const claudeJudge = aiJudges?.find(
      (reviewer) => isRecord(reviewer) && reviewer.modelId === 'claude-opus-5',
    );
    if (!isRecord(gptJudge) || gptJudge.reasoningEffort !== 'high') {
      addIssue(issues, '$.methods.reviewers.aiJudges', 'GPT-5.6 Sol must use high reasoning');
    }
    if (!isRecord(claudeJudge) || claudeJudge.reasoningEffort !== 'medium') {
      addIssue(issues, '$.methods.reviewers.aiJudges', 'Claude Opus 5 must use medium reasoning');
    }
    validateReviewer(reviewers.human, '$.methods.reviewers.human', issues, true);
    if (isRecord(reviewers.human)) {
      if (reviewers.human.name !== 'Kevin Araujo') {
        addIssue(issues, '$.methods.reviewers.human.name', 'must identify Kevin Araujo');
      }
      if (reviewers.human.soleHumanReviewer !== true) {
        addIssue(issues, '$.methods.reviewers.human.soleHumanReviewer', 'must be true');
      }
    }
  }

  const sourceMap = requireArray(methods.sourceMap, '$.methods.sourceMap', issues);
  if (sourceMap && sourceMap.length < 2) {
    addIssue(issues, '$.methods.sourceMap', 'must include primary-source and AI-Brain groups');
  }
  const sourceLabels = new Set<string>();
  sourceMap?.forEach((value, index) => {
    const path = `$.methods.sourceMap[${index}]`;
    const group = requireRecord(value, path, issues);
    if (!group) return;
    if (requireString(group.label, `${path}.label`, issues)) {
      sourceLabels.add(group.label.toLowerCase());
    }
    requireString(group.description, `${path}.description`, issues);
    const artifacts = requireArray(group.artifacts, `${path}.artifacts`, issues);
    if (artifacts?.length === 0) {
      addIssue(issues, `${path}.artifacts`, 'must contain at least one artifact');
    }
    artifacts?.forEach((artifact, artifactIndex) =>
      validateArtifact(artifact, `${path}.artifacts[${artifactIndex}]`, issues),
    );
  });
  if (![...sourceLabels].some((label) => label.includes('primary'))) {
    addIssue(issues, '$.methods.sourceMap', 'must identify the primary-source group');
  }
  if (![...sourceLabels].some((label) => label.includes('ai-brain') || label.includes('ai brain'))) {
    addIssue(issues, '$.methods.sourceMap', 'must identify the AI-Brain group');
  }
}

export function validateViewerExport(value: unknown): ValidationResult {
  const issues: ValidationIssue[] = [];
  const root = requireRecord(value, '$', issues);
  if (!root) return { ok: false, issues };

  const expectedRootFields = new Set([
    'schemaVersion',
    'exportId',
    'generatedAt',
    'title',
    'interfaceLanguage',
    'tccLanguage',
    'exportManifest',
    'strategies',
    'questions',
    'runs',
    'sourceRuns',
    'temporalEvidenceCases',
    'showcase',
    'strategyMatrix',
    'methods',
  ]);
  const actualRootFields = Object.keys(root);
  if (
    actualRootFields.length !== expectedRootFields.size ||
    actualRootFields.some((field) => !expectedRootFields.has(field))
  ) {
    addIssue(issues, '$', 'must contain exactly the contextlab.viewer.v1 root fields');
  }

  if (root.schemaVersion !== VIEWER_SCHEMA_VERSION) {
    addIssue(issues, '$.schemaVersion', `must equal ${VIEWER_SCHEMA_VERSION}`);
  }
  requireString(root.exportId, '$.exportId', issues);
  requireIsoDate(root.generatedAt, '$.generatedAt', issues);
  requireString(root.title, '$.title', issues);
  if (root.interfaceLanguage !== 'en') addIssue(issues, '$.interfaceLanguage', 'must equal en');
  if (root.tccLanguage !== 'pt-BR') addIssue(issues, '$.tccLanguage', 'must equal pt-BR');
  validateArtifact(root.exportManifest, '$.exportManifest', issues);

  const strategyIds = validateStrategies(root, issues);
  const detailedRunIds = collectIds(root.runs, '$.runs', issues);
  const sourceRunIds = validateSourceRuns(root, issues);
  const knownRunIds = new Set([...detailedRunIds, ...sourceRunIds]);
  for (const runId of detailedRunIds) {
    if (!sourceRunIds.has(runId)) {
      addIssue(issues, '$.sourceRuns', `must index detailed run ${runId}`);
    }
  }
  const questionIds = validateQuestions(root, issues, detailedRunIds, strategyIds);
  validateRuns(root, issues, knownRunIds, questionIds, strategyIds);
  validateTemporalEvidence(root, issues, detailedRunIds, questionIds);
  validateShowcase(root, issues, knownRunIds);
  validateMatrix(root, issues, knownRunIds, strategyIds);
  validateMethods(root, issues);

  return issues.length === 0
    ? { ok: true, data: value as ContextLabViewerExport }
    : { ok: false, issues };
}

export function formatValidationIssues(issues: readonly ValidationIssue[]): string {
  const visible = issues.slice(0, 6).map((issue) => `${issue.path}: ${issue.message}`);
  if (issues.length > visible.length) visible.push(`${issues.length - visible.length} more validation issues`);
  return visible.join('\n');
}

export function isArtifactRef(value: unknown): value is ArtifactRef {
  const issues: ValidationIssue[] = [];
  validateArtifact(value, '$', issues);
  return issues.length === 0;
}
