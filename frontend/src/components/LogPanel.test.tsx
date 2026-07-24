import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { LogPanel } from './LogPanel';

const logs = [
  { timestamp: '2026-07-24T01:00:00Z', level: 'INFO', source: '应用', message: '普通信息' },
  { timestamp: '2026-07-24T01:00:01Z', level: 'ERROR', source: 'movescu', message: '连接失败' },
];

describe('log panel controls', () => {
  it('offers a keyboard-operable segmented log filter', async () => {
    const user = userEvent.setup();
    const onDetailedChange = vi.fn();
    render(<LogPanel logs={logs} detailed={false} onDetailedChange={onDetailedChange} onClear={vi.fn()} onOpenDirectory={vi.fn()} />);

    const errorsOnly = screen.getByRole('button', { name: /仅错误/ });
    const allLogs = screen.getByRole('button', { name: /全部/ });
    expect(errorsOnly).toHaveAttribute('aria-pressed', 'true');
    errorsOnly.focus();
    await user.keyboard('{ArrowRight}{Enter}');
    expect(allLogs).toHaveFocus();
    expect(onDetailedChange).toHaveBeenCalledWith(true);
  });

  it('preserves the collapsed state while high-frequency log data refreshes', async () => {
    const user = userEvent.setup();
    const { rerender } = render(<LogPanel logs={logs} detailed={false} onDetailedChange={vi.fn()} onClear={vi.fn()} onOpenDirectory={vi.fn()} />);
    const trigger = screen.getByRole('button', { name: /日志与错误/ });
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    await user.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    rerender(<LogPanel logs={[...logs, { timestamp: '2026-07-24T01:00:02Z', level: 'ERROR', source: 'storescp', message: '新的错误' }]} detailed={false} onDetailedChange={vi.fn()} onClear={vi.fn()} onOpenDirectory={vi.fn()} />);
    expect(screen.getByRole('button', { name: /日志与错误/ })).toHaveAttribute('aria-expanded', 'false');
  });

  it('announces new errors even when the panel remains collapsed', async () => {
    const initial = logs.slice(0, 1);
    const { rerender } = render(<LogPanel logs={initial} detailed={false} onDetailedChange={vi.fn()} onClear={vi.fn()} onOpenDirectory={vi.fn()} />);
    expect(screen.getByRole('button', { name: /日志与错误/ })).toHaveAttribute('aria-expanded', 'false');

    rerender(<LogPanel logs={logs} detailed={false} onDetailedChange={vi.fn()} onClear={vi.fn()} onOpenDirectory={vi.fn()} />);

    expect(screen.getByRole('button', { name: /日志与错误/ })).toHaveAttribute('aria-expanded', 'false');
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('新增 1 条错误，当前共 1 条错误需要关注'));
  });
});
