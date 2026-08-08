import type { ReactNode } from 'react';

import { formatEvidenceValue, getEvidence, publicArtifactHref } from './evidence';

export function BoundValue({ id, suffix }: { id: string; suffix?: string }) {
  const entry = getEvidence(id);
  return (
    <span className="story-bound-value" data-evidence-id={id}>
      {formatEvidenceValue(entry)}
      {suffix}
    </span>
  );
}

export function BoundGateLabel({ id }: { id: 'g2.gate' | 'g3.gate' }) {
  const entry = getEvidence(id);
  const label = entry.id.split('.')[0].toUpperCase();
  return (
    <span className="story-bound-value" data-evidence-id={id}>
      {label}
    </span>
  );
}

export function EvidenceBinding({
  children,
  ids,
  label = 'Inspect evidence binding',
}: {
  children?: ReactNode;
  ids: string[];
  label?: string;
}) {
  const entries = ids.map(getEvidence);
  return (
    <details className="story-proof">
      <summary>{label}</summary>
      {children}
      <div className="story-proof__entries">
        {entries.map((entry) => {
          const href = publicArtifactHref(entry);
          return (
            <article key={entry.id}>
              <div className="story-proof__heading">
                <strong>{entry.scope}</strong>
                <span>{entry.status}</span>
              </div>
              <dl>
                <div>
                  <dt>Value</dt>
                  <dd>{formatEvidenceValue(entry)}</dd>
                </div>
                <div>
                  <dt>Source</dt>
                  <dd>
                    <code>{entry.source_path}</code>
                  </dd>
                </div>
                <div>
                  <dt>Pointer</dt>
                  <dd>
                    <code>{entry.json_pointer}</code>
                  </dd>
                </div>
                <div>
                  <dt>File SHA-256</dt>
                  <dd>
                    <code>{entry.source_file_sha256}</code>
                  </dd>
                </div>
                {entry.source_artifact_sha256 ? (
                  <div>
                    <dt>Artifact SHA-256</dt>
                    <dd>
                      <code>{entry.source_artifact_sha256}</code>
                    </dd>
                  </div>
                ) : null}
              </dl>
              {href ? (
                <a className="story-proof__link" href={href}>
                  Open bound artifact
                </a>
              ) : null}
            </article>
          );
        })}
      </div>
    </details>
  );
}
