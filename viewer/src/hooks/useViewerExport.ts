import { useCallback, useEffect, useState } from 'react';

import type { ContextLabViewerExport } from '../data/contract';
import { formatValidationIssues, validateViewerExport } from '../data/validation';

export type ExportLoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: ContextLabViewerExport };

function defaultExportUrl(): string {
  return `${import.meta.env.BASE_URL}contextlab-viewer.v1.json`;
}

export function useViewerExport(): {
  exportUrl: string;
  state: ExportLoadState;
  retry: () => void;
} {
  const [attempt, setAttempt] = useState(0);
  const [exportUrl] = useState(defaultExportUrl);
  const [state, setState] = useState<ExportLoadState>({ status: 'loading' });

  useEffect(() => {
    const controller = new AbortController();

    async function load(): Promise<void> {
      try {
        const response = await fetch(exportUrl, {
          headers: { Accept: 'application/json' },
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(`Export request failed with HTTP ${response.status}`);
        }
        const payload: unknown = await response.json();
        const result = validateViewerExport(payload);
        if (!result.ok) {
          throw new Error(`Export contract rejected the file:\n${formatValidationIssues(result.issues)}`);
        }
        setState({ status: 'ready', data: result.data });
      } catch (error) {
        if (controller.signal.aborted) return;
        const message = error instanceof Error ? error.message : 'The export could not be loaded.';
        setState({ status: 'error', message });
      }
    }

    void load();
    return () => controller.abort();
  }, [attempt, exportUrl]);

  const retry = useCallback(() => {
    setState({ status: 'loading' });
    setAttempt((value) => value + 1);
  }, []);
  return { exportUrl, state, retry };
}
