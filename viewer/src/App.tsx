import { Suspense, lazy, useEffect, useState } from 'react';
import { SkeletonText, Theme } from '@carbon/react';
import ChartRelationship from '@carbon/icons-react/es/ChartRelationship';
import Compare from '@carbon/icons-react/es/Compare';
import DataVis_4 from '@carbon/icons-react/es/DataVis_4';
import FlowStream from '@carbon/icons-react/es/FlowStream';
import QBlochSphere from '@carbon/icons-react/es/Q/BlochSphere';
import RecentlyViewed from '@carbon/icons-react/es/RecentlyViewed';

import type { ContextLabViewerExport } from './data/contract';
import { ArtifactLink } from './components/ProvenanceLink';
import { ErrorState, LoadingState } from './components/RuntimeStates';
import { useViewerExport } from './hooks/useViewerExport';
import Story, { type StoryLabView } from './story/Story';

const QuestionComparison = lazy(() => import('./views/QuestionComparison'));
const EvidencePipeline = lazy(() => import('./views/EvidencePipeline'));
const TimeMachine = lazy(() => import('./views/TimeMachine'));
const StrategyMatrix = lazy(() => import('./views/StrategyMatrix'));
const RunReplay = lazy(() => import('./views/RunReplay'));
const MethodsSources = lazy(() => import('./views/MethodsSources'));

type ViewId = 'comparison' | 'pipeline' | 'time' | 'matrix' | 'replay' | 'methods';
type AppRoute = { kind: 'story' } | { kind: 'lab'; view: ViewId };
const VIEW_IDS = new Set<ViewId>(['comparison', 'pipeline', 'time', 'matrix', 'replay', 'methods']);

const VIEW_ITEMS = [
  { id: 'comparison', label: 'Question comparison', icon: Compare },
  { id: 'pipeline', label: 'Evidence pipeline', icon: FlowStream },
  { id: 'time', label: 'Time machine', icon: RecentlyViewed },
  { id: 'matrix', label: 'Strategy matrix', icon: DataVis_4 },
  { id: 'replay', label: 'Run replay', icon: ChartRelationship },
  { id: 'methods', label: 'Methods and sources', icon: QBlochSphere },
] as const;

function routeFromHash(): AppRoute {
  const hash = window.location.hash.slice(1) as ViewId;
  return VIEW_IDS.has(hash) ? { kind: 'lab', view: hash } : { kind: 'story' };
}

function ViewLoading() {
  return (
    <div aria-label="Loading analysis view" className="view-loading" role="status">
      <SkeletonText heading width="35%" />
      <SkeletonText lineCount={3} paragraph width="70%" />
    </div>
  );
}

function renderView(activeView: ViewId, data: ContextLabViewerExport) {
  switch (activeView) {
    case 'comparison':
      return <QuestionComparison data={data} />;
    case 'pipeline':
      return <EvidencePipeline data={data} />;
    case 'time':
      return <TimeMachine data={data} />;
    case 'matrix':
      return <StrategyMatrix data={data} />;
    case 'replay':
      return <RunReplay data={data} />;
    case 'methods':
      return <MethodsSources data={data} />;
  }
}

function AppShell({
  activeView,
  data,
  onNavigate,
  onStory,
}: {
  activeView: ViewId;
  data: ContextLabViewerExport;
  onNavigate: (view: ViewId) => void;
  onStory: () => void;
}) {
  return (
    <Theme className="app-theme" theme="g100">
      <a className="skip-link" href="#main-content">
        Skip to analysis
      </a>
      <header className="app-header">
        <div className="app-header__brand">
          <a className="app-header__product" href="#story" onClick={onStory}>
            <span className="app-header__mark">CL</span>
            <span className="app-header__name">ContextLab</span>
          </a>
          <span className="app-header__title">{data.title}</span>
        </div>
        <div className="app-header__export">
          <span className="app-header__export-id">{data.exportId}</span>
          <ArtifactLink artifact={data.exportManifest} compact />
        </div>
      </header>
      <nav aria-label="Viewer sections" className="view-nav">
        <a className="view-nav__item view-nav__item--story" href="#story" onClick={onStory}>
          Story
        </a>
        {VIEW_ITEMS.map((item) => {
          const Icon = item.icon;
          return (
            <a
              aria-current={activeView === item.id ? 'page' : undefined}
              className="view-nav__item"
              href={`#${item.id}`}
              key={item.id}
              onClick={() => onNavigate(item.id)}
            >
              <Icon aria-hidden size={18} />
              <span>{item.label}</span>
            </a>
          );
        })}
      </nav>
      <main className="app-main" id="main-content" tabIndex={-1}>
        <Suspense fallback={<ViewLoading />}>
          {renderView(activeView, data)}
        </Suspense>
      </main>
    </Theme>
  );
}

function LabApp({
  activeView,
  onNavigate,
  onStory,
}: {
  activeView: ViewId;
  onNavigate: (view: ViewId) => void;
  onStory: () => void;
}) {
  const { exportUrl, retry, state } = useViewerExport();

  if (state.status === 'loading') {
    return (
      <Theme className="app-theme" theme="g100">
        <LoadingState />
      </Theme>
    );
  }
  if (state.status === 'error') {
    return (
      <Theme className="app-theme" theme="g100">
        <ErrorState exportUrl={exportUrl} message={state.message} onRetry={retry} />
      </Theme>
    );
  }
  return <AppShell activeView={activeView} data={state.data} onNavigate={onNavigate} onStory={onStory} />;
}

export default function App() {
  const [route, setRoute] = useState<AppRoute>(routeFromHash);

  useEffect(() => {
    function syncHash(): void {
      setRoute(routeFromHash());
    }
    window.addEventListener('hashchange', syncHash);
    return () => window.removeEventListener('hashchange', syncHash);
  }, []);

  function openLab(view: StoryLabView | ViewId): void {
    setRoute({ kind: 'lab', view });
  }

  function openStory(): void {
    setRoute({ kind: 'story' });
  }

  if (route.kind === 'story') return <Story onOpenLab={openLab} />;
  return <LabApp activeView={route.view} onNavigate={openLab} onStory={openStory} />;
}
