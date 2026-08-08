import { describe, expect, it } from 'vitest';

import {
  buildReplaySnapshot,
  createReplayState,
  replayReducer,
  selectedTraceSpan,
} from '../src/data/replay';
import { validViewerExport } from './fixture';

describe('saved run replay state', () => {
  it('restores the exact saved input and output references', () => {
    const snapshot = buildReplaySnapshot(validViewerExport, 'run-2');

    expect(snapshot.displayedInputs).toEqual({
      corpusPath: snapshot.run.corpusSnapshot.path,
      corpusHash: snapshot.run.corpusSnapshot.sha256,
      memoryPath: snapshot.run.memorySnapshot.path,
      memoryHash: snapshot.run.memorySnapshot.sha256,
      promptPath: snapshot.run.prompt.path,
      promptHash: snapshot.run.prompt.sha256,
      configurationId: snapshot.run.configuration.id,
      configurationPath: snapshot.run.configuration.artifact.path,
      configurationHash: snapshot.run.configuration.artifact.sha256,
    });
    expect(snapshot.displayedOutput.answer).toBe(snapshot.run.answer.text);
    expect(snapshot.displayedOutput.rawOutputHash).toBe(snapshot.run.rawOutput.sha256);
  });

  it('clears transient inspection state when reset', () => {
    const snapshot = buildReplaySnapshot(validViewerExport, 'run-3');
    const selected = replayReducer(createReplayState(snapshot), {
      type: 'select-span',
      spanId: 'run-3-span',
    });
    expect(selectedTraceSpan(selected)?.id).toBe('run-3-span');

    const reset = replayReducer(selected, { type: 'reset' });
    expect(reset.current).toEqual(snapshot);
    expect(reset.selectedSpanId).toBeNull();
  });

  it('rejects an unknown saved run', () => {
    expect(() => buildReplaySnapshot(validViewerExport, 'missing-run')).toThrow(
      'Unknown replay run: missing-run',
    );
  });
});
