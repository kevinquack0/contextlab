import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App';
import { validViewerExport } from './fixture';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState({}, '', '/');
});

describe('viewer application shell', () => {
  it('loads validated data and keeps every section keyboard reachable', async () => {
    window.history.replaceState({}, '', '/#comparison');
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(validViewerExport), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );
    render(<App />);

    expect(await screen.findByRole('heading', { name: 'Question comparison' })).toBeVisible();
    expect(screen.getByRole('link', { name: 'Skip to analysis' })).toHaveAttribute(
      'href',
      '#main-content',
    );
    expect(screen.getAllByRole('link', { name: /NL-001#NL-001-S01/ })[0]).toHaveAttribute(
      'href',
      validViewerExport.runs[0].answer.citations[0].target.staticUrl,
    );

    for (const viewName of [
      'Evidence pipeline',
      'Time machine',
      'Strategy matrix',
      'Run replay',
    ]) {
      fireEvent.click(screen.getByRole('link', { name: viewName }));
      expect(await screen.findByRole('heading', { name: viewName })).toBeVisible();
    }

    expect(screen.getByRole('heading', { name: 'Evidence flow' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Explore constellation' })).toBeVisible();

    const methodsLink = screen.getByRole('link', { name: 'Methods and sources' });
    methodsLink.focus();
    expect(methodsLink).toHaveFocus();
    fireEvent.click(methodsLink);
    expect(await screen.findByRole('heading', { name: 'Methods and sources' })).toBeVisible();
    expect(screen.getByText('NovaLearn is a synthetic company and corpus used only for this study.')).toBeVisible();
  });

  it('exposes temporal strata and evidence flow as interactive, evidence-bound views', async () => {
    window.history.replaceState({}, '', '/#time');
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(validViewerExport), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );
    render(<App />);

    expect(await screen.findByRole('heading', { name: 'Temporal strata' })).toBeVisible();
    expect(screen.getByRole('slider', { name: 'Inspect saved event' })).toHaveAttribute('max', '1');
    const laterEvent = screen.getByRole('button', { name: /Later event\. active/i });
    fireEvent.click(laterEvent);
    expect(screen.getByRole('heading', { name: 'Later event' })).toBeVisible();
    expect(screen.getByText(/Supersedes claim-old/i)).toBeVisible();

    fireEvent.click(screen.getByRole('link', { name: 'Run replay' }));
    expect(await screen.findByRole('heading', { name: 'Evidence flow' })).toBeVisible();
    expect(screen.getByText(/Ribbon width is the saved candidate or context token volume/i)).toBeVisible();
    const contextIndex = screen.getByRole('button', { name: 'Selected evidence: Context pack' });
    fireEvent.click(contextIndex);
    expect(screen.getByText('The exact saved evidence passed to generation after the recorded budget decision.')).toBeVisible();
  });

  it('ignores query-string export overrides and loads only the fixed local export', async () => {
    window.history.replaceState({}, '', '/?export=https://example.com/fabricated.json#comparison');
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(validViewerExport), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);

    expect(await screen.findByRole('heading', { name: 'Question comparison' })).toBeVisible();
    expect(fetchMock).toHaveBeenCalledWith(
      '/contextlab-viewer.v1.json',
      expect.objectContaining({ headers: { Accept: 'application/json' } }),
    );
  });

  it('renders corpus and memory candidates that cite the same source as distinct rows', async () => {
    window.history.replaceState({}, '', '/#comparison');
    const payload = structuredClone(validViewerExport);
    const corpusCandidate = payload.runs[0].pipeline.stages[0].candidates[0];
    payload.runs[0].pipeline.stages[0].candidates.push({
      ...structuredClone(corpusCandidate),
      id: 'run-1-retrieval-memory-candidate',
      origin: 'memory',
    });
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Question comparison' })).toBeVisible();
    fireEvent.click(screen.getByRole('link', { name: 'Evidence pipeline' }));
    expect(await screen.findByRole('heading', { name: 'Evidence pipeline' })).toBeVisible();
    expect(screen.getAllByRole('link', { name: /NL-001#NL-001-S01/ })).toHaveLength(2);
    expect(screen.getByText('corpus')).toBeVisible();
    expect(screen.getByText('memory')).toBeVisible();
  });
});
