import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import * as sass from 'sass';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App';
import storySource from '../src/story/Story.tsx?raw';
import { getEvidence, storyEvidence } from '../src/story/evidence';
import { storyLinks } from '../src/story/links';
import { validViewerExport } from './fixture';

function viewerResponse(): Response {
  return new Response(JSON.stringify(validViewerExport), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState({}, '', '/');
});

describe('guided ContextLab Story', () => {
  it('binds canonical social metadata to the exact public poster', () => {
    const html = readFileSync(resolve('index.html'), 'utf-8');
    const posterPath = resolve('..', 'docs', 'portfolio', 'media', 'contextlab-poster-1200x630.jpg');
    const poster = readFileSync(posterPath);

    expect(html).toContain(
      '<link rel="canonical" href="https://contextlab-research.vercel.app/" />',
    );
    expect(html).toContain(
      '<meta property="og:url" content="https://contextlab-research.vercel.app/" />',
    );
    expect(html).toContain('name="author" content="Kevin Araujo"');
    expect(html).toContain('property="og:image"');
    expect(html).toContain('name="twitter:card" content="summary_large_image"');
    expect(html).toContain(
      'https://raw.githubusercontent.com/kevinquack0/contextlab/main/docs/portfolio/media/contextlab-poster-1200x630.jpg',
    );
    expect(createHash('sha256').update(poster).digest('hex')).toBe(
      '10e56c1b3d2a9e5edb7edc5f7d006a7b8368fa57c968fe695db4f81ad631ea83',
    );
  });

  it('opens before the lab and does not fetch the large viewer export', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);

    expect(screen.getByRole('heading', { name: 'Complexity has to earn its place.' })).toBeVisible();
    expect(screen.getByText('Postgraduate research by Kevin Araujo')).toBeVisible();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('enters the existing lab on demand and can return to Story', async () => {
    const fetchMock = vi.fn().mockResolvedValue(viewerResponse());
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);

    fireEvent.click(screen.getAllByRole('link', { name: 'Explore the lab' })[0]);
    expect(await screen.findByRole('heading', { name: 'Question comparison' })).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('link', { name: 'Story' }));
    expect(screen.getByRole('heading', { name: 'Complexity has to earn its place.' })).toBeVisible();
  });

  it.each([
    ['comparison', 'Question comparison'],
    ['pipeline', 'Evidence pipeline'],
    ['time', 'Time machine'],
    ['matrix', 'Strategy matrix'],
    ['replay', 'Run replay'],
    ['methods', 'Methods and sources'],
  ])('keeps the #%s deep link', async (hash, heading) => {
    window.history.replaceState({}, '', `/#${hash}`);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(viewerResponse()));
    render(<App />);

    expect(await screen.findByRole('heading', { name: heading })).toBeVisible();
  });

  it('presents the architecture in execution order with the evaluator outside', () => {
    vi.stubGlobal('fetch', vi.fn());
    render(<App />);

    const architecture = screen.getByRole('list', { name: 'ContextLab system architecture' });
    const nodes = within(architecture).getAllByRole('listitem');
    expect(nodes.map((node) => within(node).getByRole('strong').textContent)).toEqual([
      'Corpus and events',
      'Strategy adapters',
      'Context packs',
      'Provider gateway',
      'Grading and gates',
      'Evidence viewer',
    ]);
    expect(screen.getByRole('complementary', { name: 'Sealed evaluator boundary' })).toHaveTextContent(
      'Outside the public boundary',
    );
  });

  it('shows every required stage in the public trace', () => {
    vi.stubGlobal('fetch', vi.fn());
    render(<App />);

    expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual([
      'Candidate retrieval',
      'Context construction',
      'Generation',
      'Citations',
      'Review',
      'Gate decision',
    ]);
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Both conflicting events survived retrieval.');
  });

  it('supports arrow-key navigation through trace stages', () => {
    vi.stubGlobal('fetch', vi.fn());
    render(<App />);

    const candidateTab = screen.getByRole('tab', { name: 'Candidate retrieval' });
    candidateTab.focus();
    fireEvent.keyDown(candidateTab, { key: 'ArrowRight' });

    const contextTab = screen.getByRole('tab', { name: 'Context construction' });
    expect(contextTab).toHaveFocus();
    expect(contextTab).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tabpanel')).toHaveTextContent('The context pack kept the disagreement visible.');
  });

  it('makes temporal supersession visible through an accessible time control', () => {
    vi.stubGlobal('fetch', vi.fn());
    render(<App />);

    const control = screen.getByRole('slider', { name: 'Knowledge state' });
    expect(control).toHaveAttribute('aria-valuetext', 'Before supersession');
    expect(screen.getByText('Later evidence has not entered the snapshot.')).toBeVisible();

    fireEvent.input(control, { target: { value: '1' } });
    expect(control).toHaveAttribute('aria-valuetext', 'After supersession');
    expect(screen.getAllByText('superseded')[0]).toBeVisible();
    expect(screen.getAllByText('regulated enterprises')[0]).toBeVisible();
  });

  it('binds current frontier status and human approval to exact source hashes', () => {
    expect(getEvidence('f3.final_status')).toMatchObject({
      value: 'accepted-negative',
      source_file_sha256: '81bc0ad40a828888df5430d8bc56fe56359fc17bab6fdac4c2c1ec12e2646595',
      source_artifact_sha256: 'd82232f114b83718eada843b8b18fe270cc1d611c11b87f21c3fcd0efdd202a5',
      json_pointer: '/final_status',
    });
    expect(getEvidence('f3.human_status').value).toBe('approved');
    expect(getEvidence('f5.final_status')).toMatchObject({
      value: 'accepted-negative',
      source_file_sha256: 'f1850c8438cda8606ec9eb601bcd500663b292ec11316d072afc5910825736ec',
      source_artifact_sha256: '9b8b2c4f6430017c94230e956b92270143c4f106ee9e87a84e4ca427dcb3a81f',
      json_pointer: '/final_status',
    });
    expect(getEvidence('f5.human_status').value).toBe('approved');
  });

  it('keeps Story evidence local, scalar, and free of private or defective paths', () => {
    for (const entry of storyEvidence) {
      expect(entry.source_path).not.toMatch(/^\/Users\/|^\/Volumes\/|^[A-Za-z]:\\/);
      expect(entry.source_path).not.toContain('e84bbeac05191d5b38ce796580a5bc45d3d79109e99e77415c054615f20288bd');
      if (entry.public_url !== null) expect(entry.public_url).not.toMatch(/^https?:/);
      expect(['string', 'number', 'boolean', 'object']).toContain(typeof entry.value);
    }
    expect(storySource).not.toContain('e84bbeac05191d5b38ce796580a5bc45d3d79109e99e77415c054615f20288bd');
    expect(storySource).not.toContain('/Users/');
    expect(storySource).not.toContain('/Volumes/');
  });

  it('ships every linked Story artifact at its declared hash', () => {
    for (const entry of storyEvidence) {
      if (entry.public_url === null) continue;
      const artifact = readFileSync(resolve('public', entry.public_url.slice(2)));
      expect(createHash('sha256').update(artifact).digest('hex')).toBe(entry.source_file_sha256);
    }
  });

  it('centralizes proposed public source links without a private path', () => {
    expect(storyLinks.map((link) => link.id)).toEqual(['source', 'code', 'methodology', 'tcc']);
    for (const link of storyLinks) {
      expect(link.href).toMatch(/^https:\/\/github\.com\/kevinquack0\/contextlab/);
      if (link.target_path !== null) expect(link.target_path).not.toMatch(/^\//);
    }
    expect(storyLinks.find((link) => link.id === 'tcc')?.sha256).toBe(
      'bf3efd964c0370fda2c7b37e2208f1775b8c5ac16b09b72af9f28d8eb2369864',
    );
  });

  it('defines reduced-motion behavior and avoids scroll listeners', () => {
    const appStyles = sass.compile('src/styles/app.scss', {
      loadPaths: ['node_modules'],
      quietDeps: true,
    }).css;
    expect(appStyles).toContain('@media (prefers-reduced-motion: reduce)');
    expect(appStyles).toContain('transition-duration: 0.01ms !important');
    expect(storySource).not.toContain("addEventListener('scroll'");
    expect(storySource).not.toContain('window.scrollY');
  });
});
