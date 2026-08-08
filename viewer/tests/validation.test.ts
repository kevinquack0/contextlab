import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { validateViewerExport } from '../src/data/validation';
import { validViewerExport } from './fixture';

describe('validateViewerExport', () => {
  it('accepts the production export shipped beside the viewer', () => {
    const persisted = JSON.parse(
      readFileSync(resolve('public', 'contextlab-viewer.v1.json'), 'utf-8'),
    ) as unknown;
    const result = validateViewerExport(persisted);

    expect(result.ok ? [] : result.issues).toEqual([]);
  });

  it('accepts the complete versioned contract', () => {
    expect(validateViewerExport(validViewerExport)).toEqual({ ok: true, data: validViewerExport });
  });

  it('rejects a citation without an export-provided static URL', () => {
    const candidate: unknown = structuredClone(validViewerExport);
    if (typeof candidate !== 'object' || candidate === null) throw new Error('Fixture clone failed');
    const mutable = candidate as typeof validViewerExport;
    mutable.runs[0].answer.citations[0].source.staticUrl = '';

    const result = validateViewerExport(mutable);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.issues).toContainEqual(
        expect.objectContaining({ path: '$.runs[0].answer.citations[0].source.staticUrl' }),
      );
    }
  });

  it('rejects remote, non-content-addressed, and sealed artifact targets', () => {
    const remote = structuredClone(validViewerExport);
    remote.runs[0].answer.citations[0].target.staticUrl = 'https://example.com/fabricated.json';
    const remoteResult = validateViewerExport(remote);
    expect(remoteResult.ok).toBe(false);
    if (!remoteResult.ok) {
      expect(remoteResult.issues).toContainEqual(
        expect.objectContaining({ path: '$.runs[0].answer.citations[0].target.staticUrl' }),
      );
    }

    const sealed = structuredClone(validViewerExport);
    sealed.runs[0].answer.citations[0].target.path = 'results/v2/sealed/gold.json';
    const sealedResult = validateViewerExport(sealed);
    expect(sealedResult.ok).toBe(false);
    if (!sealedResult.ok) {
      expect(sealedResult.issues).toContainEqual(
        expect.objectContaining({ path: '$.runs[0].answer.citations[0].target.path' }),
      );
    }
  });

  it('requires artifact URLs to contain their declared hash', () => {
    const mutable = structuredClone(validViewerExport);
    mutable.exportManifest.staticUrl = `./artifacts/${'0'.repeat(64)}/manifest.json`;
    const result = validateViewerExport(mutable);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.issues).toContainEqual(
        expect.objectContaining({ path: '$.exportManifest.staticUrl' }),
      );
    }
  });

  it('rejects a citation without exact run and JSON-pointer provenance', () => {
    const mutable = structuredClone(validViewerExport);
    mutable.runs[0].answer.citations[0].provenance.runIds = [];
    mutable.runs[0].answer.citations[0].provenance.jsonPointer = 'answer/citations/0';

    const result = validateViewerExport(mutable);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.issues).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            path: '$.runs[0].answer.citations[0].provenance.runIds',
          }),
          expect.objectContaining({
            path: '$.runs[0].answer.citations[0].provenance.jsonPointer',
          }),
        ]),
      );
    }
  });

  it('rejects a number without source-run provenance', () => {
    const mutable = structuredClone(validViewerExport);
    mutable.strategyMatrix.cells[0].meanSelectedEvidence.provenance.runIds = [];
    mutable.strategyMatrix.cells[0].meanSelectedEvidence.provenance.jsonPointer = 'metrics/meanSelectedEvidence';

    const result = validateViewerExport(mutable);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.issues).toContainEqual(
        expect.objectContaining({
          path: '$.strategyMatrix.cells[0].meanSelectedEvidence.provenance.runIds',
        }),
      );
      expect(result.issues).toContainEqual(
        expect.objectContaining({
          path: '$.strategyMatrix.cells[0].meanSelectedEvidence.provenance.jsonPointer',
        }),
      );
    }
  });

  it('rejects malformed RFC 6901 pointer escapes and control characters', () => {
    for (const jsonPointer of ['/metrics/~2bad', '/metrics/\u0000bad']) {
      const mutable = structuredClone(validViewerExport);
      mutable.runs[0].metrics.contextTokens.provenance.jsonPointer = jsonPointer;

      const result = validateViewerExport(mutable);
      expect(result.ok).toBe(false);
      if (!result.ok) {
        expect(result.issues).toContainEqual(
          expect.objectContaining({
            path: '$.runs[0].metrics.contextTokens.provenance.jsonPointer',
          }),
        );
      }
    }
  });

  it('rejects a question that does not cover five strategies', () => {
    const mutable = structuredClone(validViewerExport);
    mutable.questions[0].comparisonRunIds = mutable.questions[0].comparisonRunIds.slice(0, 4);

    const result = validateViewerExport(mutable);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.issues.some((issue) => issue.path === '$.questions[0].comparisonRunIds')).toBe(true);
    }
  });

  it('accepts an explicitly unscored fallback candidate', () => {
    const mutable = structuredClone(validViewerExport);
    mutable.runs[0].pipeline.stages[0].candidates[0].score = null;
    expect(validateViewerExport(mutable)).toEqual({ ok: true, data: mutable });
  });

  it('requires an execution-failure showcase to cite an explicitly failed run', () => {
    const mutable = structuredClone(validViewerExport);
    mutable.showcase.executionFailure = {
      ...structuredClone(mutable.showcase.temporalEvidence),
      runIds: [mutable.runs[0].id],
      title: 'Saved execution failure',
    };
    const result = validateViewerExport(mutable);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.issues).toContainEqual(
        expect.objectContaining({ path: '$.showcase.executionFailure.runIds[0]' }),
      );
    }

    mutable.runs[0].executionStatus = 'failed';
    expect(validateViewerExport(mutable)).toEqual({ ok: true, data: mutable });
  });

  it('rejects duplicate candidate row identities within one stage', () => {
    const mutable = structuredClone(validViewerExport);
    const candidate = structuredClone(mutable.runs[0].pipeline.stages[0].candidates[0]);
    mutable.runs[0].pipeline.stages[0].candidates.push(candidate);
    const result = validateViewerExport(mutable);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.issues).toContainEqual(
        expect.objectContaining({
          path: '$.runs[0].pipeline.stages[0].candidates[1].id',
        }),
      );
    }
  });

  it('rejects aggregate provenance that is absent from the source-run registry', () => {
    const mutable = structuredClone(validViewerExport);
    mutable.strategyMatrix.cells[0].meanCandidateEvidence.provenance.runIds = ['unknown-run'];
    const result = validateViewerExport(mutable);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.issues).toContainEqual(
        expect.objectContaining({
          path: '$.strategyMatrix.cells[0].meanCandidateEvidence.provenance.runIds[0]',
        }),
      );
    }
  });
});
