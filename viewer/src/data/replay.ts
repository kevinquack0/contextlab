import type { ContextLabViewerExport, RunRecord, TraceSpan } from './contract';

export interface ReplaySnapshot {
  run: RunRecord;
  questionText: string;
  strategyLabel: string;
  displayedInputs: {
    corpusPath: string;
    corpusHash: string;
    memoryPath: string;
    memoryHash: string;
    promptPath: string;
    promptHash: string;
    configurationId: string;
    configurationPath: string;
    configurationHash: string;
  };
  displayedOutput: {
    answer: string;
    citationIds: string[];
    rawOutputPath: string;
    rawOutputHash: string;
  };
}

export interface ReplayState {
  initial: ReplaySnapshot;
  current: ReplaySnapshot;
  selectedSpanId: string | null;
}

export type ReplayAction =
  | { type: 'restore'; snapshot: ReplaySnapshot }
  | { type: 'select-span'; spanId: string | null }
  | { type: 'reset' };

export function buildReplaySnapshot(
  data: ContextLabViewerExport,
  runId: string,
): ReplaySnapshot {
  const runById = new Map(data.runs.map((run) => [run.id, run]));
  const questionById = new Map(data.questions.map((question) => [question.id, question]));
  const strategyById = new Map(data.strategies.map((strategy) => [strategy.id, strategy]));
  const run = runById.get(runId);
  if (!run) throw new Error(`Unknown replay run: ${runId}`);
  const question = questionById.get(run.questionId);
  const strategy = strategyById.get(run.strategyId);
  if (!question || !strategy) throw new Error(`Replay run ${runId} has unresolved references`);

  return {
    run,
    questionText: question.text,
    strategyLabel: strategy.label,
    displayedInputs: {
      corpusPath: run.corpusSnapshot.path,
      corpusHash: run.corpusSnapshot.sha256,
      memoryPath: run.memorySnapshot.path,
      memoryHash: run.memorySnapshot.sha256,
      promptPath: run.prompt.path,
      promptHash: run.prompt.sha256,
      configurationId: run.configuration.id,
      configurationPath: run.configuration.artifact.path,
      configurationHash: run.configuration.artifact.sha256,
    },
    displayedOutput: {
      answer: run.answer.text,
      citationIds: run.answer.citations.map((citation) => citation.id),
      rawOutputPath: run.rawOutput.path,
      rawOutputHash: run.rawOutput.sha256,
    },
  };
}

export function createReplayState(snapshot: ReplaySnapshot): ReplayState {
  return { initial: snapshot, current: snapshot, selectedSpanId: null };
}

export function replayReducer(state: ReplayState, action: ReplayAction): ReplayState {
  switch (action.type) {
    case 'restore':
      return createReplayState(action.snapshot);
    case 'select-span':
      return { ...state, selectedSpanId: action.spanId };
    case 'reset':
      return createReplayState(state.initial);
  }
}

export function selectedTraceSpan(state: ReplayState): TraceSpan | null {
  if (!state.selectedSpanId) return null;
  return state.current.run.traceSpans.find((span) => span.id === state.selectedSpanId) ?? null;
}
