import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { EmptyState, ErrorState } from '../src/components/RuntimeStates';

describe('accessible runtime states', () => {
  it('announces an export error and exposes a keyboard-operable retry button', () => {
    const retry = vi.fn();
    render(<ErrorState exportUrl="/contextlab-viewer.v1.json" message="Contract rejected" onRetry={retry} />);

    expect(screen.getByRole('alert')).toHaveTextContent('Contract rejected');
    const button = screen.getByRole('button', { name: 'Retry export' });
    fireEvent.click(button);
    expect(retry).toHaveBeenCalledOnce();
  });

  it('announces an empty saved-evidence state', () => {
    render(<EmptyState detail="Generate an export." title="No runs" />);
    expect(screen.getByRole('status')).toHaveTextContent('No runs');
    expect(screen.getByRole('heading', { name: 'No runs' })).toBeVisible();
  });
});
