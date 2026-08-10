import { useState, type ReactNode } from 'react';

import { formatEvidenceValue, getEvidence, publicArtifactHref } from './evidence';
import { useOdometer, useScrolledIntoView } from './motion';

/**
 * Every amber mark on the Story is a bound value: a scalar read from a frozen
 * artifact at an exact JSON pointer, shipped beside the file's SHA-256. Colour
 * is reserved for evidence, so nothing decorative can be mistaken for a result.
 */
export function BoundValue({ id, suffix }: { id: string; suffix?: string }) {
  const entry = getEvidence(id);
  return (
    <span className="bound" data-evidence-id={id} title={entry.scope}>
      {formatEvidenceValue(entry)}
      {suffix}
    </span>
  );
}

/** A bound integer that counts to its recorded value the first time it is seen. */
export function BoundNumber({ id, className }: { id: string; className?: string }) {
  const entry = getEvidence(id);
  const [ref, crossed] = useScrolledIntoView<HTMLSpanElement>();
  const numeric = typeof entry.value === 'number' ? entry.value : Number.NaN;
  const shown = useOdometer(Number.isFinite(numeric) ? numeric : 0, crossed);
  const exact = formatEvidenceValue(entry);

  if (!Number.isFinite(numeric)) {
    return (
      <span className={className ? `bound ${className}` : 'bound'} data-evidence-id={id}>
        {exact}
      </span>
    );
  }

  return (
    <span
      className={className ? `bound bound--odometer ${className}` : 'bound bound--odometer'}
      data-evidence-id={id}
      ref={ref}
      title={entry.scope}
    >
      {new Intl.NumberFormat('en-US').format(shown)}
    </span>
  );
}

export function BoundGateLabel({ id }: { id: 'g2.gate' | 'g3.gate' }) {
  const entry = getEvidence(id);
  return (
    <span className="bound" data-evidence-id={id} title={entry.scope}>
      {entry.id.split('.')[0].toUpperCase()}
    </span>
  );
}

function CopyHash({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);

  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      setCopied(false);
    }
  }

  return (
    <button
      className="proof__copy"
      onClick={() => {
        void copy();
      }}
      title="Copy the full digest"
      type="button"
    >
      <code>{value}</code>
      <span aria-hidden>{copied ? 'copied' : 'copy'}</span>
      <span className="sr-only">{copied ? 'Digest copied' : 'Copy digest'}</span>
    </button>
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
    <details className="proof">
      <summary>
        <span className="proof__chevron" aria-hidden />
        <span>{label}</span>
        <span className="proof__count">
          {entries.length} {entries.length === 1 ? 'binding' : 'bindings'}
        </span>
      </summary>
      {children}
      <div className="proof__entries">
        {entries.map((entry) => {
          const href = publicArtifactHref(entry);
          return (
            <article key={entry.id}>
              <div className="proof__heading">
                <strong>{entry.scope}</strong>
                <span className="proof__status" data-status={entry.status}>
                  {entry.status}
                </span>
              </div>
              <dl>
                <div>
                  <dt>Value</dt>
                  <dd className="proof__value">{formatEvidenceValue(entry)}</dd>
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
                    <CopyHash value={entry.source_file_sha256} />
                  </dd>
                </div>
                {entry.source_artifact_sha256 ? (
                  <div>
                    <dt>Artifact SHA-256</dt>
                    <dd>
                      <CopyHash value={entry.source_artifact_sha256} />
                    </dd>
                  </div>
                ) : null}
              </dl>
              {href ? (
                <a className="proof__link" href={href}>
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
