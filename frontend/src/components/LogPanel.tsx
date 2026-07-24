import { Collapsible } from '@base-ui/react/collapsible';
import { Toggle } from '@base-ui/react/toggle';
import { ToggleGroup } from '@base-ui/react/toggle-group';
import { Check, ChevronDown, Clipboard, ListFilter, Trash2 } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { LogEntry } from '../schemas';
import { Button } from './Primitives';
import { AnimatedIcon, semanticIconMap } from './icons';

export type NormalizedLog = { timestamp: string; level: string; source: string; message: string };

export function normalizeLog(entry: LogEntry | Record<string, unknown>): NormalizedLog {
  return {
    timestamp: String(entry.timestamp || entry.time || new Date().toISOString()),
    level: String(entry.level || 'INFO').toUpperCase(),
    source: String(entry.source || entry.component || '应用'),
    message: String(entry.message || entry.text || ''),
  };
}

export function LogPanel({
  logs,
  detailed,
  onDetailedChange,
  onClear,
  onOpenDirectory,
}: {
  logs: NormalizedLog[];
  detailed: boolean;
  onDetailedChange: (value: boolean) => void;
  onClear: () => void;
  onOpenDirectory: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const visible = useMemo(() => detailed ? logs : logs.filter((entry) => ['ERROR', 'CRITICAL'].includes(entry.level)), [logs, detailed]);
  const errors = useMemo(() => logs.filter((entry) => ['ERROR', 'CRITICAL'].includes(entry.level)).length, [logs]);
  const [open, setOpen] = useState(() => errors > 0);
  const previousErrors = useRef(errors);
  const [errorAnnouncement, setErrorAnnouncement] = useState('');
  const filter = detailed ? 'all' : 'errors';
  useEffect(() => {
    const added = errors - previousErrors.current;
    if (added > 0) setErrorAnnouncement(`新增 ${added} 条错误，当前共 ${errors} 条错误需要关注`);
    previousErrors.current = errors;
  }, [errors]);
  const copy = async () => {
    if (!visible.length) return;
    await navigator.clipboard.writeText(visible.map((entry) => `${entry.timestamp} [${entry.source}] ${entry.level} ${entry.message}`).join('\n'));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };
  return <Collapsible.Root className="log-panel" open={open} onOpenChange={setOpen}>
    <span className="sr-only" role="status" aria-live="polite" aria-atomic="true">{errorAnnouncement}</span>
    <Collapsible.Trigger className="log-panel__trigger">
      <span><ListFilter size={17} /><strong>日志与错误</strong></span>
      <span className={errors ? 'log-panel__summary is-error' : 'log-panel__summary'}>{errors ? `${errors} 条错误需要关注` : '当前没有错误'}<ChevronDown size={16} /></span>
    </Collapsible.Trigger>
    <Collapsible.Panel className="log-panel__panel">
      <div className="log-panel__toolbar">
        <ToggleGroup
          className="log-filter-segmented"
          value={[filter]}
          onValueChange={(values) => {
            const next = values[0];
            if (next) onDetailedChange(next === 'all');
          }}
          aria-label="日志显示范围"
        >
          <Toggle className="log-filter-segmented__item" value="errors">
            仅错误 <span aria-hidden="true">{errors}</span>
          </Toggle>
          <Toggle className="log-filter-segmented__item" value="all">
            全部 <span aria-hidden="true">{logs.length}</span>
          </Toggle>
        </ToggleGroup>
        <div>
          <Button variant="quiet" size="small" onClick={copy} disabled={!visible.length}>{copied ? <Check size={15} /> : <Clipboard size={15} />}{copied ? '已复制' : '复制'}</Button>
          <Button variant="quiet" size="small" onClick={onClear} disabled={!logs.length}><Trash2 size={15} />清空</Button>
          <Button variant="quiet" size="small" onClick={onOpenDirectory}><AnimatedIcon {...semanticIconMap.openDirectory} size={15} />日志目录</Button>
        </div>
      </div>
      {!visible.length
        ? <div className="log-empty"><ListFilter size={19} /><div><strong>{detailed ? '暂无实时日志' : '当前没有错误'}</strong><p>{detailed ? '任务事件将在这里持续出现。' : '排查问题时可以打开详细日志。'}</p></div></div>
        : <ol className="log-list" aria-live="polite">{visible.map((entry, index) => <li key={`${entry.timestamp}-${index}`} data-level={entry.level}>
            <time>{formatTime(entry.timestamp)}</time><span>{entry.source} · {entry.level}</span><p>{entry.message}</p>
          </li>)}</ol>}
    </Collapsible.Panel>
  </Collapsible.Root>;
}

function formatTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString('zh-CN', { hour12: false });
}
