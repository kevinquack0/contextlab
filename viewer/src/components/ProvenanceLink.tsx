import Launch from '@carbon/icons-react/es/Launch';

import type { ArtifactRef, CitationRecord, MetricValue } from '../data/contract';

function basename(path: string): string {
  return path.split('/').at(-1) ?? path;
}

interface ArtifactLinkProps {
  artifact: ArtifactRef;
  compact?: boolean;
  download?: boolean;
}

export function ArtifactLink({ artifact, compact = false, download = false }: ArtifactLinkProps) {
  const hash = compact ? artifact.sha256.slice(0, 12) : artifact.sha256;
  const path = compact ? basename(artifact.path) : artifact.path;
  const accessibleLabel = `${artifact.label}. ${artifact.path}. SHA-256 ${artifact.sha256}`;

  return (
    <a
      aria-label={accessibleLabel}
      className={`artifact-link${compact ? ' artifact-link--compact' : ''}`}
      download={download || undefined}
      href={artifact.staticUrl}
      rel="noreferrer"
      target="_blank"
      title={`${artifact.path}\nSHA-256 ${artifact.sha256}`}
    >
      <Launch aria-hidden size={14} />
      <span className="artifact-link__path">{path}</span>
      <code className="artifact-link__hash">{hash}</code>
    </a>
  );
}

interface MetricLinkProps {
  label: string;
  metric: MetricValue;
  compact?: boolean;
}

export function MetricLink({ label, metric, compact = false }: MetricLinkProps) {
  const { artifact, runIds } = metric.provenance;
  const detail = `${artifact.path}, SHA-256 ${artifact.sha256}, runs ${runIds.join(', ')}`;

  return (
    <a
      aria-label={`${label}: ${metric.display}. Provenance: ${detail}`}
      className={`metric-link${compact ? ' metric-link--compact' : ''}`}
      href={artifact.staticUrl}
      rel="noreferrer"
      target="_blank"
      title={detail}
    >
      <span className="metric-link__label">{label}</span>
      <data className="metric-link__value" value={metric.value}>
        {metric.display}
      </data>
      <span className="metric-link__source">
        {basename(artifact.path)} #{artifact.sha256.slice(0, 10)}
      </span>
    </a>
  );
}

export function CitationLink({ citation }: { citation: CitationRecord }) {
  const { jsonPointer, runIds } = citation.provenance;
  const detail = `${citation.source.path}, source SHA-256 ${citation.source.sha256}, exact section ${citation.target.path}, section SHA-256 ${citation.target.sha256}, runs ${runIds.join(', ')}, pointer ${jsonPointer}`;

  return (
    <a
      aria-label={`${citation.label}. Provenance: ${detail}`}
      className="citation-link"
      href={citation.target.staticUrl}
      rel="noreferrer"
      target="_blank"
      title={detail}
    >
      <span>{citation.label}</span>
      <span className="citation-link__source">
        {citation.source.path} #{citation.source.sha256.slice(0, 10)}
      </span>
    </a>
  );
}
