import type { Task, TaskItem, UnknownRecord } from './schemas';

export const DETAIL_LIMIT = 200;
export const TERMINAL_STATUSES = new Set([
  'completed', 'partial', 'partial_success', 'failed', 'cancelled', 'canceled',
  'ended', 'verification_completed', 'verification_failed', 'verification_cancelled',
]);

export type Tone = 'neutral' | 'working' | 'success' | 'warning' | 'error';

const STATUS: Record<string, [string, Tone]> = {
  idle: ['等待开始', 'neutral'], queued: ['排队中', 'neutral'], preflight: ['预检中', 'working'],
  starting: ['启动接收器', 'working'], starting_receiver: ['启动接收器', 'working'],
  running: ['下载中', 'working'], downloading: ['下载中', 'working'], pausing: ['暂停中', 'warning'],
  pause_pending: ['等待暂停', 'warning'], paused: ['已暂停', 'warning'], stopping: ['停止中', 'warning'],
  ending: ['正在结束任务', 'warning'], ended: ['任务已结束', 'neutral'], end_failed: ['结束失败', 'error'],
  interrupted: ['下载已中断', 'warning'], download_retryable: ['可继续下载', 'warning'],
  pdi_pending: ['等待导出 PDI', 'working'], pdi_running: ['正在导出 PDI', 'working'],
  pdi_retryable: ['PDI 可重试', 'warning'], completed: ['已完成', 'success'],
  verifying: ['正在校验 PDI', 'working'], verification_completed: ['PDI 校验完成', 'success'],
  verification_failed: ['PDI 校验失败', 'error'], verification_cancelled: ['PDI 校验已取消', 'neutral'],
  partial: ['部分成功', 'warning'], partial_success: ['部分成功', 'warning'], failed: ['失败', 'error'],
  cancelled: ['已取消', 'neutral'], canceled: ['已取消', 'neutral'], no_data: ['无数据', 'warning'],
};

export function normalizeStatus(value: unknown): string {
  return String(value || 'idle').trim().toLowerCase().replaceAll('-', '_').replaceAll(' ', '_');
}

export function statusView(value: unknown): { label: string; tone: Tone } {
  const key = normalizeStatus(value);
  const [label, tone] = STATUS[key] || [String(value || '未知状态'), 'neutral'];
  return { label, tone };
}

export function parseAccessions(raw: string) {
  const values: string[] = [];
  const seen = new Set<string>();
  let blank = 0;
  let duplicate = 0;
  let invalid = 0;
  for (const line of raw ? raw.split(/\r?\n/) : []) {
    const value = line.trim();
    if (!value) { blank += 1; continue; }
    if (/[\\*?\u0000-\u001f\u007f]/.test(value)) { invalid += 1; continue; }
    if (seen.has(value)) { duplicate += 1; continue; }
    seen.add(value);
    values.push(value);
  }
  return { values, blank, duplicate, invalid };
}

export function taskCount(task: Task | null | undefined, ...keys: string[]): number {
  for (const key of keys) {
    const direct = task?.[key];
    const summary = task?.summary?.[key];
    const counts = task?.status_counts?.[key];
    for (const value of [direct, summary, counts]) {
      if (value != null && Number.isFinite(Number(value))) return Number(value);
    }
  }
  return 0;
}

export function taskItems(task?: Task | null): TaskItem[] {
  const items = task?.items?.length ? task.items : task?.results;
  if (items?.length) return items;
  return (task?.accessions || []).map((item) => typeof item === 'object' && item !== null
    ? item as TaskItem
    : { accession: String(item), status: 'idle' });
}

export function formatRate(value: unknown): string {
  let bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B/s';
  const units = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
  let index = 0;
  while (bytes >= 1024 && index < units.length - 1) { bytes /= 1024; index += 1; }
  const digits = bytes >= 100 || index === 0 ? 0 : bytes >= 10 ? 1 : 2;
  return `${bytes.toFixed(digits)} ${units[index]}`;
}

export function formatBytes(value: unknown): string {
  let bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB'];
  let index = 0;
  while (bytes >= 1024 && index < units.length - 1) { bytes /= 1024; index += 1; }
  return `${bytes.toFixed(index ? 1 : 0)} ${units[index]}`;
}

export function formatDuration(value: unknown): string {
  const seconds = Math.max(0, Math.floor(Number(value || 0)));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return hours
    ? `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`
    : `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
}

export function stringValue(record: UnknownRecord | undefined, key: string, fallback = ''): string {
  const value = record?.[key];
  return value == null ? fallback : String(value);
}

export function boolValue(record: UnknownRecord | undefined, key: string, fallback = false): boolean {
  const value = record?.[key];
  return value == null ? fallback : Boolean(value);
}
