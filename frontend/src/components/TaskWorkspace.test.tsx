import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { LogPanel } from './LogPanel';
import { SpeedSparkline } from './SpeedSparkline';
import { TaskWorkspace } from './TaskWorkspace';

const base = {
  available: true,
  accessionText: '',
  parsed: { values: [], blank: 0, duplicate: 0, invalid: 0 },
  destination: 'D:\\dicom',
  pdiEnabled: false,
  pdiFolder: '',
  preflight: null,
  preflightSignatureMatches: false,
  preflightBusy: false,
  actionBusy: false,
  onAccessionTextChange: vi.fn(), onDestinationChange: vi.fn(), onPdiEnabledChange: vi.fn(),
  onPdiFolderChange: vi.fn(), onImport: vi.fn(), onBrowse: vi.fn(), onPreflight: vi.fn(),
  onStart: vi.fn(), onTaskAction: vi.fn(), onPdiAction: vi.fn(), onNewTask: vi.fn(), onOpenDestination: vi.fn(),
};

describe('task workspace', () => {
  it('exposes file import as a named keyboard-focusable control', () => {
    render(<TaskWorkspace {...base} task={null} />);
    const importButton = screen.getByRole('button', { name: '导入 TXT / CSV / XLSX' });
    importButton.focus();
    expect(importButton).toHaveFocus();
    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]');
    expect(fileInput).toHaveAttribute('tabindex', '-1');
    expect(fileInput).toHaveAttribute('aria-hidden', 'true');
  });

  it('moves focus to the new task view when the composer is replaced', async () => {
    const { rerender } = render(<TaskWorkspace {...base} task={null} />);
    screen.getByLabelText('检查号').focus();
    rerender(<TaskWorkspace {...base} task={{ id: 'active', status: 'running', total: 1, actions: {} }} />);
    await waitFor(() => expect(screen.getByRole('heading', { name: '下载中' })).toHaveFocus());
  });

  it('does not render item rows for tasks over 200 accessions', () => {
    const accessions = Array.from({ length: 201 }, (_, index) => `A${index}`);
    const map = vi.spyOn(accessions, 'map');
    render(<TaskWorkspace {...base} task={{ id: 'large', status: 'running', total: 201, processed: 40, accessions, actions: {} }} />);
    expect(screen.getByText('大批量任务仅显示聚合进度')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(map).not.toHaveBeenCalled();
  });

  it('shows visible text for critical transfer controls', () => {
    render(<TaskWorkspace {...base} task={{ id: 'one', status: 'running', total: 1, actions: { can_pause: true, can_cancel: true, can_end: true } }} />);
    expect(screen.getByRole('button', { name: '暂停' })).toHaveTextContent('暂停');
    expect(screen.getByRole('button', { name: '取消' })).toHaveTextContent('取消');
    expect(screen.getByRole('button', { name: '结束任务' })).toHaveTextContent('结束任务');
  });

  it('only offers a new task when the backend action allows it', () => {
    const { rerender } = render(<TaskWorkspace {...base} task={{ id: 'recoverable', status: 'cancelled', actions: { can_resume: true, can_start: false } }} />);
    expect(screen.queryByRole('button', { name: '新建任务' })).not.toBeInTheDocument();
    rerender(<TaskWorkspace {...base} task={{ id: 'ended', status: 'ended', actions: { can_start: true } }} />);
    expect(screen.getByRole('button', { name: '新建任务' })).toBeInTheDocument();
  });

  it('uses backend PDI actions instead of guessing from status', () => {
    render(<TaskWorkspace {...base} task={{
      id: 'pdi', status: 'pdi_retryable', actions: { can_retry_pdi: true, can_verify_pdi: false },
      pdi: { status: 'partial', output_directory: 'D:\\pdi' },
    }} />);
    expect(screen.getByRole('button', { name: '打开 PDI' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重试导出' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '校验' })).not.toBeInTheDocument();
  });

  it('keeps focused runtime controls stable during high-frequency refreshes', () => {
    const task = { id: 'steady', status: 'running', total: 10, processed: 1, elapsed_seconds: 1, speed_bps: 1024, actions: { can_pause: true } };
    const { rerender } = render(<TaskWorkspace {...base} task={task} />);
    const pause = screen.getByRole('button', { name: '暂停' });
    pause.focus();
    rerender(<TaskWorkspace {...base} task={{ ...task, processed: 2, elapsed_seconds: 2, speed_bps: 2048 }} />);
    expect(screen.getByRole('button', { name: '暂停' })).toHaveFocus();
  });

  it('exposes runtime and preflight states for visual treatment without changing actions', async () => {
    const { rerender } = render(<TaskWorkspace {...base} task={null} preflight={{ ok: true, checks: { receiver: { name: '接收器', ok: true } } }} preflightSignatureMatches />);
    expect(screen.getByLabelText('启动预检')).toHaveAttribute('data-state', 'ready');
    expect(screen.getByText('接收器').closest('li')).toHaveAttribute('data-state', 'ok');
    rerender(<TaskWorkspace {...base} task={{ id: 'active-state', status: 'running', total: 2, processed: 1, actions: {} }} />);
    await waitFor(() => expect(document.querySelector('.task-runtime')).toHaveAttribute('data-state', 'running'));
    expect(document.querySelector('.task-progress-block')).toHaveAttribute('data-state', 'active');
  });
});

describe('speed sparkline', () => {
  it('samples updates, keeps the configured window, and resets when the task changes', async () => {
    const { rerender } = render(<SpeedSparkline taskId="task-a" value={100} sampleKey={1} maxSamples={3} />);
    const sparkline = () => document.querySelector('svg.speed-sparkline');
    expect(sparkline()).toHaveAttribute('data-sample-count', '1');

    rerender(<SpeedSparkline taskId="task-a" value={200} sampleKey={2} maxSamples={3} />);
    await waitFor(() => expect(sparkline()).toHaveAttribute('data-sample-count', '2'));
    rerender(<SpeedSparkline taskId="task-a" value={300} sampleKey={3} maxSamples={3} />);
    rerender(<SpeedSparkline taskId="task-a" value={400} sampleKey={4} maxSamples={3} />);
    await waitFor(() => expect(sparkline()).toHaveAttribute('data-sample-count', '3'));

    rerender(<SpeedSparkline taskId="task-b" value={500} sampleKey={1} maxSamples={3} />);
    await waitFor(() => expect(sparkline()).toHaveAttribute('data-sample-count', '1'));
  });

  it('renders only the current static sample when reduced motion is requested', () => {
    render(<SpeedSparkline taskId="reduced" value={1024} sampleKey={1} reducedMotion />);
    const sparkline = document.querySelector('svg.speed-sparkline');
    expect(sparkline).toHaveAttribute('data-reduced-motion', 'true');
    expect(sparkline).toHaveAttribute('data-sample-count', '1');
  });
});

describe('log panel', () => {
  const logs = [
    { timestamp: '2026-07-24T01:00:00Z', level: 'INFO', source: '应用', message: '普通信息' },
    { timestamp: '2026-07-24T01:00:01Z', level: 'ERROR', source: 'movescu', message: '连接失败' },
  ];
  it('shows only ERROR and CRITICAL by default', () => {
    render(<LogPanel logs={logs} detailed={false} onDetailedChange={vi.fn()} onClear={vi.fn()} onOpenDirectory={vi.fn()} />);
    expect(screen.getByText('连接失败')).toBeInTheDocument();
    expect(screen.queryByText('普通信息')).not.toBeInTheDocument();
  });

});
