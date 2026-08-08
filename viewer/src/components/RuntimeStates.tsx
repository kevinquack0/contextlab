import {
  Button,
  InlineNotification,
  SkeletonPlaceholder,
  SkeletonText,
} from '@carbon/react';
import Renew from '@carbon/icons-react/es/Renew';

export function LoadingState() {
  return (
    <main aria-busy="true" aria-label="Loading viewer export" className="runtime-state" id="main-content">
      <div className="runtime-state__skeleton" role="status">
        <SkeletonText heading width="38%" />
        <SkeletonText paragraph lineCount={3} width="62%" />
        <div className="runtime-state__skeleton-grid">
          <SkeletonPlaceholder />
          <SkeletonPlaceholder />
          <SkeletonPlaceholder />
        </div>
        <span className="visually-hidden">Loading saved ContextLab artifacts.</span>
      </div>
    </main>
  );
}

interface ErrorStateProps {
  exportUrl: string;
  message: string;
  onRetry: () => void;
}

export function ErrorState({ exportUrl, message, onRetry }: ErrorStateProps) {
  return (
    <main className="runtime-state" id="main-content">
      <InlineNotification
        hideCloseButton
        kind="error"
        lowContrast
        role="alert"
        subtitle={`${message} No fixture data was substituted. Requested URL: ${exportUrl}`}
        title="Saved export unavailable"
      />
      <Button kind="primary" onClick={onRetry} renderIcon={Renew} size="sm">
        Retry export
      </Button>
    </main>
  );
}

interface EmptyStateProps {
  title: string;
  detail: string;
}

export function EmptyState({ title, detail }: EmptyStateProps) {
  return (
    <section className="empty-state" role="status">
      <p className="empty-state__label">No saved evidence</p>
      <h2>{title}</h2>
      <p>{detail}</p>
    </section>
  );
}
