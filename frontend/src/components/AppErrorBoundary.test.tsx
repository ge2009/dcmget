import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AppErrorBoundary } from './AppErrorBoundary';

function BrokenView(): ReactElement {
  throw new Error('模拟渲染故障');
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('AppErrorBoundary', () => {
  it('renders children while the application is healthy', () => {
    render(<AppErrorBoundary><p>正常工作台</p></AppErrorBoundary>);
    expect(screen.getByText('正常工作台')).toBeInTheDocument();
  });

  it('shows an accessible recovery page and logs the captured render error', async () => {
    const user = userEvent.setup();
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const onReload = vi.fn();

    render(<AppErrorBoundary onReload={onReload}><BrokenView /></AppErrorBoundary>);

    const heading = screen.getByRole('heading', { name: '应用界面暂时无法显示' });
    expect(screen.getByRole('alert')).toHaveAccessibleDescription(/无法自动恢复的错误/);
    expect(heading).toHaveFocus();
    expect(screen.getByText(/^DCM-\d{14}-[0-9A-F]{6}$/)).toBeInTheDocument();
    expect(consoleError).toHaveBeenCalledWith(
      expect.stringMatching(/^\[DcmGet] 未捕获的前端渲染错误（DCM-/),
      expect.objectContaining({ message: '模拟渲染故障' }),
      expect.objectContaining({ componentStack: expect.any(String) }),
    );

    await user.tab();
    const reload = screen.getByRole('button', { name: '重新加载应用' });
    expect(reload).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(onReload).toHaveBeenCalledOnce();
  });

  it('copies diagnostic data and announces success', async () => {
    const user = userEvent.setup();
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const copyErrorDetails = vi.fn().mockResolvedValue(undefined);

    render(<AppErrorBoundary copyErrorDetails={copyErrorDetails}><BrokenView /></AppErrorBoundary>);
    const diagnosticId = screen.getByText(/^DCM-\d{14}-[0-9A-F]{6}$/).textContent;
    await user.click(screen.getByRole('button', { name: '复制错误详情' }));

    await waitFor(() => expect(copyErrorDetails).toHaveBeenCalledOnce());
    const details = copyErrorDetails.mock.calls[0][0] as string;
    expect(details).toContain(`诊断编号：${diagnosticId}`);
    expect(details).toContain('错误信息：模拟渲染故障');
    expect(details).toContain('组件堆栈：');
    expect(screen.getByRole('status')).toHaveTextContent('错误详情已复制到剪贴板');
  });

  it('keeps the diagnostic number available when clipboard access fails', async () => {
    const user = userEvent.setup();
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const copyErrorDetails = vi.fn().mockRejectedValue(new Error('clipboard denied'));

    render(<AppErrorBoundary copyErrorDetails={copyErrorDetails}><BrokenView /></AppErrorBoundary>);
    const diagnosticId = screen.getByText(/^DCM-\d{14}-[0-9A-F]{6}$/).textContent;
    await user.click(screen.getByRole('button', { name: '复制错误详情' }));

    expect(await screen.findByRole('status')).toHaveTextContent('复制失败，请手动记录诊断编号');
    expect(screen.getByText(diagnosticId!)).toBeVisible();
  });
});
