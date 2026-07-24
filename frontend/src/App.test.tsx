import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App';

function json(data: unknown) {
  return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('bootstrap modes', () => {
  it('renders the direct profile workspace from bootstrap', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const path = String(input);
      if (path === '/api/bootstrap') return json({ csrf_token: 'token', version: '3.7.0', profile: { number: 1, display_name: '院内下载', mode: 'profile', is_running: true }, config: { dicom_destination_folder: 'D:\\dicom' }, task: { status: 'idle', pdi: null, actions: { can_start: true } } });
      return json({ ok: true, checks: [] });
    });
    render(<App />);
    expect(await screen.findByRole('heading', { name: '新建影像下载' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '院内下载', level: 1 })).toBeInTheDocument();
  });

  it('renders manager mode and loads the selected running profile', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const path = String(input);
      if (path === '/api/bootstrap') return json({ csrf_token: 'token', mode: 'manager', profile: {}, update: { supported: true, state: 'idle' } });
      if (path === '/api/management/profiles') return json({ profiles: [{ number: 2, display_name: 'CT 接收', is_running: true, storage_ae_title: 'DCMGET2', storage_port: 6662 }] });
      if (path === '/api/management/profiles/2/bootstrap') return json({ csrf_token: 'token2', profile: { number: 2, display_name: 'CT 接收', is_running: true }, config: { dicom_destination_folder: 'D:\\ct' }, task: { status: 'idle', actions: { can_start: true } } });
      if (path.startsWith('/api/management/profiles/2/events')) return json({ events: [] });
      return json({});
    });
    render(<App />);
    expect(await screen.findByRole('heading', { name: '接收实例' })).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'CT 接收' })).toBeInTheDocument();
  });

  it('keeps the authoritative manager task while a progress delta is being refreshed', async () => {
    let resolveEvents: ((response: Response) => void) | undefined;
    let resolveTask: ((response: Response) => void) | undefined;
    let taskRequests = 0;
    const runningTask = {
      id: 'task-1', status: 'running', total: 10, processed: 1,
      accessions: ['A001'], actions: { can_pause: true, can_cancel: true },
    };
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const path = String(input);
      if (path === '/api/bootstrap') return json({ csrf_token: 'token', mode: 'manager', profile: {}, update: { supported: true, state: 'idle' } });
      if (path === '/api/management/profiles') return json({ profiles: [{ number: 2, display_name: 'CT 接收', is_running: true, storage_ae_title: 'DCMGET2', storage_port: 6662 }] });
      if (path === '/api/management/profiles/2/bootstrap') return json({ csrf_token: 'token2', profile: { number: 2, display_name: 'CT 接收', is_running: true }, config: { dicom_destination_folder: 'D:\\ct' }, task: runningTask });
      if (path.startsWith('/api/management/profiles/2/events')) return await new Promise<Response>((resolve) => { resolveEvents = resolve; });
      if (path === '/api/management/profiles/2/task') {
        taskRequests += 1;
        return await new Promise<Response>((resolve) => { resolveTask = resolve; });
      }
      return json({});
    });
    render(<App />);
    const pause = await screen.findByRole('button', { name: '暂停' });
    pause.focus();
    await waitFor(() => expect(resolveEvents).toBeDefined());
    await act(async () => {
      resolveEvents?.(json({
        events: [{ id: 1, type: 'progress', payload: { task_id: 'task-1', index: 2, total: 10, final: false, result: { accession: 'A002', status: 'downloading' } } }],
        last_id: 1,
      }));
    });
    await waitFor(() => expect(taskRequests).toBe(1));
    expect(screen.getByRole('button', { name: '暂停' })).toHaveFocus();

    await act(async () => { resolveTask?.(json({ task: { ...runningTask, processed: 2 } })); });
    await waitFor(() => expect(screen.getByText('2 / 10')).toBeInTheDocument());
  });

  it('discards a successful preflight response after the draft changes', async () => {
    let resolvePreflight: ((response: Response) => void) | undefined;
    let preflightCalls = 0;
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const path = String(input);
      if (path === '/api/bootstrap') return json({
        csrf_token: 'token',
        profile: { number: 1, display_name: '院内下载', mode: 'profile', is_running: true },
        config: { dicom_destination_folder: '' },
        task: { status: 'idle', actions: { can_start: true } },
      });
      if (path === '/api/preflight') {
        preflightCalls += 1;
        if (preflightCalls === 1) return await new Promise<Response>((resolve) => { resolvePreflight = resolve; });
        return json({ ok: false, message: '草稿已变化' });
      }
      return json({});
    });
    render(<App />);
    const accessions = await screen.findByLabelText('检查号');
    fireEvent.change(accessions, { target: { value: 'OLD-001' } });
    fireEvent.change(screen.getByLabelText('保存到 DcmGet 主机'), { target: { value: 'D:\\dicom' } });
    fireEvent.click(screen.getByRole('button', { name: /重新检查/ }));
    await waitFor(() => expect(preflightCalls).toBe(1));
    fireEvent.change(accessions, { target: { value: 'NEW-002' } });
    await act(async () => { resolvePreflight?.(json({ ok: true, checks: [] })); });
    expect(screen.getByRole('button', { name: /开始下载/ })).toBeDisabled();
    expect(screen.queryByText('可以开始')).not.toBeInTheDocument();
  });

  it('labels the directory picker and uses button selection semantics', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const path = String(input);
      if (path === '/api/bootstrap') return json({
        csrf_token: 'token',
        profile: { number: 1, display_name: '院内下载', mode: 'profile', is_running: true },
        config: { dicom_destination_folder: 'D:\\dicom' },
        task: { status: 'idle', actions: { can_start: true } },
      });
      if (path.startsWith('/api/files/directories?')) return json({
        path: 'D:\\dicom',
        directories: [{ name: '研究影像', path: 'D:\\dicom\\research' }],
      });
      return json({ ok: false, checks: [] });
    });
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '浏览' }));
    expect(await screen.findByLabelText('当前目录路径')).toHaveValue('D:\\dicom');
    expect(screen.getByRole('button', { name: '关闭目录选择' })).toBeInTheDocument();
    const directory = screen.getByRole('button', { name: '研究影像' });
    expect(directory).not.toHaveAttribute('aria-selected');
    expect(directory).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(directory);
    expect(directory).toHaveAttribute('aria-pressed', 'true');
  });

  it('keeps a new-task draft open when polling returns the prior terminal task', async () => {
    let poll: (() => void) | null = null;
    let taskRequests = 0;
    const finished = { id: 'finished-task', status: 'ended', total: 1, processed: 1, actions: { can_start: true } };
    let polledTask: Record<string, unknown> = finished;
    const nativeSetInterval = window.setInterval;
    vi.spyOn(window, 'setInterval').mockImplementation((handler: TimerHandler, timeout?: number) => {
      if (timeout === 15_000 && typeof handler === 'function') {
        poll = () => handler();
        return 1;
      }
      return nativeSetInterval(handler, timeout);
    });
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const path = String(input);
      if (path === '/api/bootstrap') return json({
        csrf_token: 'token',
        profile: { number: 1, display_name: '院内下载', mode: 'profile', is_running: true },
        config: { dicom_destination_folder: 'D:\\dicom' },
        task: finished,
      });
      if (path === '/api/task') {
        taskRequests += 1;
        return json({ task: polledTask });
      }
      return json({ ok: false, checks: [] });
    });
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '新建任务' }));
    expect(await screen.findByRole('heading', { name: '新建影像下载' })).toBeInTheDocument();
    polledTask = { id: 'finished-task', status: 'ended', total: 1, processed: 1 };
    await act(async () => { poll?.(); });
    await waitFor(() => expect(taskRequests).toBe(1));
    expect(screen.getByRole('heading', { name: '新建影像下载' })).toBeInTheDocument();
    polledTask = { id: 'external-task', status: 'running', total: 1, processed: 0, actions: { can_start: false } };
    await act(async () => { poll?.(); });
    await waitFor(() => expect(taskRequests).toBe(2));
    expect(await screen.findByRole('heading', { name: '下载中' })).toBeInTheDocument();
  });
});
