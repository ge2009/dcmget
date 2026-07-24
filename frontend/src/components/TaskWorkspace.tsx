import { Collapsible } from '@base-ui/react/collapsible';
import { Progress } from '@base-ui/react/progress';
import {
  ChevronDown, FileText, RotateCcw, ShieldCheck,
} from 'lucide-react';
import { AnimatePresence, motion, useReducedMotion } from 'motion/react';
import { useCallback, useRef } from 'react';
import {
  DETAIL_LIMIT, formatDuration, formatRate, normalizeStatus,
  statusView, taskCount, taskItems,
} from '../domain';
import type { Preflight, Task, UnknownRecord } from '../schemas';
import { Button, StatusBadge, SwitchRow } from './Primitives';
import { SpeedSparkline } from './SpeedSparkline';
import { AnimatedIcon, semanticIconMap } from './icons';

type ParseResult = { values: string[]; blank: number; duplicate: number; invalid: number };

type Props = {
  available: boolean;
  task: Task | null;
  accessionText: string;
  parsed: ParseResult;
  destination: string;
  pdiEnabled: boolean;
  pdiFolder: string;
  preflight: Preflight | null;
  preflightSignatureMatches: boolean;
  preflightBusy: boolean;
  actionBusy: boolean;
  onAccessionTextChange: (value: string) => void;
  onDestinationChange: (value: string) => void;
  onPdiEnabledChange: (value: boolean) => void;
  onPdiFolderChange: (value: string) => void;
  onOpenPdiDirectory: () => void;
  onImport: (file: File) => void;
  onBrowse: () => void;
  onPreflight: () => void;
  onStart: () => void;
  onTaskAction: (action: string) => void;
  onPdiAction: (action: 'open' | 'verify' | 'retry') => void;
  onNewTask: () => void;
  onOpenDestination: () => void;
};

function normalizeChecks(preflight: Preflight | null) {
  const raw = preflight?.checks || preflight?.items || {};
  if (Array.isArray(raw)) return raw.map((item) => item as UnknownRecord);
  return Object.entries(raw).map(([key, value]): UnknownRecord => ({ key, ...(typeof value === 'object' && value ? value : { ok: Boolean(value) }) }));
}

function actionEnabled(task: Task, action: string): boolean {
  return task.actions?.[action] === true;
}

function checkState(check: UnknownRecord): 'pending' | 'ok' | 'error' {
  const status = normalizeStatus(check.status);
  if (['pending', 'checking', 'running', 'starting'].includes(status)) return 'pending';
  return check.ok === true || check.success === true || check.ready === true ? 'ok' : 'error';
}

function isActiveRuntime(status: string): boolean {
  return [
    'preflight', 'starting', 'starting_receiver', 'running', 'downloading',
    'pausing', 'pause_pending', 'stopping', 'ending', 'pdi_pending',
    'pdi_running', 'verifying',
  ].includes(status);
}

export function TaskWorkspace(props: Props) {
  const reduceMotion = useReducedMotion();
  const status = normalizeStatus(props.task?.status);
  const hasTask = Boolean(props.task?.id) || status !== 'idle';
  const previousHasTask = useRef(hasTask);
  const pendingFocus = useRef<'composer' | 'runtime' | null>(null);
  if (previousHasTask.current !== hasTask) {
    pendingFocus.current = hasTask ? 'runtime' : 'composer';
    previousHasTask.current = hasTask;
  }
  const focusComposerHeading = useCallback((node: HTMLHeadingElement | null) => {
    if (node && pendingFocus.current === 'composer') {
      node.focus();
      pendingFocus.current = null;
    }
  }, []);
  const focusRuntimeHeading = useCallback((node: HTMLHeadingElement | null) => {
    if (node && pendingFocus.current === 'runtime') {
      node.focus();
      pendingFocus.current = null;
    }
  }, []);
  return <div className="task-stack" data-state={hasTask ? 'runtime' : 'composer'}>
    <AnimatePresence mode="wait">
      {!hasTask
        ? <motion.section key="editor" className="task-card task-composer" data-state="editing" initial={reduceMotion ? { opacity: 0 } : { opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
            <TaskComposer {...props} headingRef={focusComposerHeading} />
          </motion.section>
        : <motion.section key="runtime" className="task-card task-runtime" data-state={status} initial={reduceMotion ? { opacity: 0 } : { opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
            <TaskRuntime {...props} task={props.task!} headingRef={focusRuntimeHeading} />
          </motion.section>}
    </AnimatePresence>
  </div>;
}

function TaskComposer(props: Props & { headingRef: (node: HTMLHeadingElement | null) => void }) {
  const ready = props.available && props.parsed.values.length > 0 && Boolean(props.destination.trim())
    && props.preflight?.ok === true && props.preflightSignatureMatches;
  const checks = normalizeChecks(props.preflight);
  const preflightState = !props.available
    ? 'unavailable'
    : props.preflightBusy
      ? 'checking'
      : props.preflight && !props.preflightSignatureMatches
        ? 'stale'
        : props.preflight?.ok === true
          ? 'ready'
          : props.preflight
            ? 'blocked'
            : 'idle';
  const importInput = useRef<HTMLInputElement>(null);
  const guidance = !props.available
    ? '请先启动当前 Profile'
    : !props.parsed.values.length
      ? '输入检查号后将自动执行启动预检'
      : !props.destination.trim()
        ? '请选择影像保存目录'
        : props.preflightBusy
          ? '正在检查 DCMTK、目录和接收端口'
          : props.preflight?.ok !== true
            ? '预检尚未通过，请处理右侧检查项'
            : `已准备好下载 ${props.parsed.values.length.toLocaleString()} 个检查号`;

  return <>
    <header className="task-card__header">
      <div>
        <h2 ref={props.headingRef} tabIndex={-1}>新建影像下载</h2>
        <p>粘贴检查号、选择目录，然后开始后台下载。</p>
      </div>
      <StatusBadge tone={!props.available ? 'warning' : ready ? 'success' : 'neutral'}>
        {!props.available ? '实例未启动' : ready ? '可以开始' : '准备任务'}
      </StatusBadge>
    </header>

    <div className="composer-layout">
      <div className="composer-form">
        <section className="form-section">
          <div className="form-section__heading">
            <span>1</span><div><h3>检查号</h3><p>每行一个，也可以导入 TXT、CSV 或 XLSX。</p></div>
          </div>
          <div className="accession-editor">
            <textarea
              id="accessions"
              aria-label="检查号"
              rows={9}
              spellCheck={false}
              value={props.accessionText}
              onChange={(event) => props.onAccessionTextChange(event.target.value)}
              placeholder={'例如：\n202601261643\nCT202108130339'}
            />
          </div>
          <div className="accession-toolbar" data-state={props.parsed.invalid > 0 ? 'invalid' : props.parsed.values.length > 0 ? 'valid' : 'empty'}>
            <Button type="button" variant="secondary" size="small" onClick={() => importInput.current?.click()}>
              <AnimatedIcon {...semanticIconMap.importFile} size={15} />导入 TXT / CSV / XLSX
            </Button>
            <input ref={importInput} className="file-import-input" type="file" tabIndex={-1} aria-hidden="true" accept=".txt,.csv,.xlsx,text/plain,text/csv" onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) props.onImport(file);
              event.target.value = '';
            }} />
            <div className="import-facts" aria-live="polite" data-state={props.parsed.invalid > 0 ? 'warning' : 'valid'}>
              <span><strong>{props.parsed.values.length.toLocaleString()}</strong> 有效</span>
              <span>{props.parsed.duplicate} 重复</span>
              <span>{props.parsed.blank} 空行</span>
              {props.parsed.invalid > 0 && <span className="text-danger">{props.parsed.invalid} 无效</span>}
            </div>
          </div>
        </section>

        <section className="form-section form-section--compact">
          <div className="form-section__heading">
            <span>2</span><div><h3>保存位置</h3><p>文件将由运行 DcmGet 的主机写入此目录。</p></div>
          </div>
          <label className="field-label" htmlFor="destination">保存到 DcmGet 主机</label>
          <div className="input-action">
            <input id="destination" value={props.destination} onChange={(event) => props.onDestinationChange(event.target.value)} placeholder="D:\\DICOM 或 /data/dicom" />
            <Button onClick={props.onBrowse}><AnimatedIcon {...semanticIconMap.openDirectory} size={16} />浏览</Button>
          </div>
          <div className="pdi-option">
            <SwitchRow checked={props.pdiEnabled} onCheckedChange={props.onPdiEnabledChange} label="任务完成后导出 PDI" description="可选；生成可复制到 U 盘的便携目录。" />
            {props.pdiEnabled && <div className="input-action">
              <input aria-label="PDI 输出目录" value={props.pdiFolder} onChange={(event) => props.onPdiFolderChange(event.target.value)} placeholder="PDI 输出目录（留空使用默认值）" />
              <Button type="button" onClick={props.onOpenPdiDirectory}><AnimatedIcon {...semanticIconMap.openDirectory} size={16} />打开目录</Button>
            </div>}
          </div>
        </section>
      </div>

      <aside className="launch-panel" aria-label="启动预检" data-state={preflightState} aria-busy={props.preflightBusy}>
        <div className="launch-panel__header">
          <span className="launch-panel__icon"><ShieldCheck size={18} /></span>
          <div><h3>启动预检</h3><p>自动核对下载所需环境</p></div>
        </div>
        {!checks.length
          ? <div className="preflight-empty"><AnimatedIcon {...semanticIconMap.refresh} size={17} className={props.preflightBusy ? 'spin' : ''} /><p>{props.preflightBusy ? '正在检查…' : '填写任务信息后自动检查'}</p></div>
          : <ul className="check-list">{checks.map((check, index) => {
              const state = checkState(check);
              return <li key={String(check.key || check.name || index)} data-state={state} data-ok={state === 'ok'}>
                <span aria-hidden="true">{state === 'ok'
                  ? <AnimatedIcon {...semanticIconMap.preflightPassed} statusKey={state} size={14} />
                  : state === 'pending'
                    ? <AnimatedIcon {...semanticIconMap.refresh} statusKey={state} size={14} className="spin" />
                    : <AnimatedIcon {...semanticIconMap.preflightFailed} statusKey={state} size={14} />}</span>
                <div><strong>{String(check.name || check.label || check.key || '检查项')}</strong><small>{String(check.message || check.detail || (state === 'ok' ? '已就绪' : state === 'pending' ? '检查中' : '未通过'))}</small></div>
              </li>;
            })}</ul>}
        <Button className="preflight-refresh" variant="quiet" size="small" disabled={props.preflightBusy || !props.destination.trim()} onClick={props.onPreflight}>
          <AnimatedIcon {...semanticIconMap.refresh} size={14} className={props.preflightBusy ? 'spin' : ''} />{props.preflightBusy ? '检查中' : '重新检查'}
        </Button>
        <div className="launch-panel__footer">
          <p>{guidance}</p>
          <Button className="launch-cta" data-state={props.actionBusy ? 'busy' : ready ? 'ready' : 'blocked'} variant="primary" size="large" disabled={!ready || props.actionBusy} onClick={props.onStart}>
            <AnimatedIcon {...semanticIconMap.startDownload} size={18} />{props.actionBusy ? '正在创建…' : '开始下载'}
          </Button>
          <small>关闭页面不会停止后台任务</small>
        </div>
      </aside>
    </div>
  </>;
}

function TaskRuntime(props: Props & { task: Task; headingRef: (node: HTMLHeadingElement | null) => void }) {
  const task = props.task;
  const status = normalizeStatus(task.status);
  const view = statusView(status);
  const reportedTotal = taskCount(task, 'total', 'total_count', 'accession_count');
  const inlineItems = task.items?.length ? task.items : task.results;
  const total = reportedTotal || inlineItems?.length || task.accessions?.length || 0;
  const large = total > DETAIL_LIMIT;
  const items = large ? [] : taskItems(task);
  const processed = taskCount(task, 'processed', 'processed_count', 'finished_count');
  const percent = total ? Math.min(100, Math.round(processed / total * 100)) : 0;
  const files = taskCount(task, 'file_count', 'received_files', 'files');
  const currentRecord = task.current && typeof task.current === 'object' ? task.current as UnknownRecord : {};
  const current = task.current_accession || String(currentRecord.accession || task.accession || '—');
  const active = isActiveRuntime(status);
  const speed = task.speed_bytes_per_second ?? task.speed_bps ?? task.current_speed;
  const speedTaskId = String(task.id ?? task.task_id ?? 'active');
  const speedSampleKey = `${task.elapsed_seconds ?? ''}:${files}:${processed}:${speed ?? ''}`;

  return <>
    <header className="task-card__header task-card__header--runtime" data-state={status}>
      <div className="runtime-title">
        <StatusBadge tone={view.tone}>{view.label}</StatusBadge>
        <h2 ref={props.headingRef} tabIndex={-1}>{view.label}</h2>
        <p>{task.message || task.detail || '后台任务状态持续同步中'}</p>
      </div>
      <div className="runtime-primary-actions">
        {actionEnabled(task, 'can_pause') && <Button variant="secondary" size="small" onClick={() => props.onTaskAction('pause')}><AnimatedIcon {...semanticIconMap.pauseTask} size={16} />暂停</Button>}
        {actionEnabled(task, 'can_resume') && <Button variant="primary" size="small" onClick={() => props.onTaskAction('resume')}><AnimatedIcon {...semanticIconMap.resumeTask} size={16} />继续</Button>}
        {actionEnabled(task, 'can_retry_failed') && <Button variant="secondary" size="small" onClick={() => props.onTaskAction('retry')}><RotateCcw size={16} />重试</Button>}
        {actionEnabled(task, 'can_cancel') && <Button variant="quiet" size="small" onClick={() => props.onTaskAction('cancel')}><AnimatedIcon {...semanticIconMap.stopTask} size={16} />取消</Button>}
      </div>
    </header>

    <div className="task-progress-block" data-state={active ? 'active' : status}>
      <div className="task-progress-block__summary">
        <div><span>总体进度</span><strong>{processed.toLocaleString()} / {total.toLocaleString()}</strong></div>
        <strong>{percent}%</strong>
      </div>
      <Progress.Root className="transfer-progress" value={percent} aria-label="下载总进度" aria-valuetext={`${processed} / ${total}，${percent}%`} data-state={active ? 'active' : status}>
        <Progress.Track className="transfer-progress__track"><Progress.Indicator className="transfer-progress__indicator" data-state={active ? 'active' : status} /></Progress.Track>
      </Progress.Root>
    </div>

    <div className="runtime-metrics" data-state={active ? 'active' : 'static'}>
      <article className="metric-tile metric-tile--accession"><span>当前检查号</span><strong>{current}</strong></article>
      <article className="metric-tile metric-tile--files"><span>接收文件</span><strong>{files.toLocaleString()}</strong></article>
      <article className="metric-tile metric-tile--speed" data-state={active ? 'live' : 'static'}>
        <span>实时速度</span><strong>{formatRate(speed)}</strong>
        <SpeedSparkline key={speedTaskId} taskId={speedTaskId} value={speed} sampleKey={speedSampleKey} />
      </article>
      <article className="metric-tile metric-tile--elapsed"><span>已用时间</span><strong>{formatDuration(task.elapsed_seconds)}</strong></article>
    </div>

    <div className="runtime-actions">
      <Button variant="quiet" size="small" onClick={props.onOpenDestination}><AnimatedIcon {...semanticIconMap.openDirectory} size={16} />打开结果目录</Button>
      <span className="runtime-actions__spacer" />
      {actionEnabled(task, 'can_accept_partial') && <Button size="small" onClick={() => props.onTaskAction('accept-partial')}>接受已有文件</Button>}
      {actionEnabled(task, 'can_end') && <Button variant="danger" size="small" onClick={() => props.onTaskAction('end')}><AnimatedIcon {...semanticIconMap.stopTask} size={16} />结束任务</Button>}
      {actionEnabled(task, 'can_start') && <Button variant="primary" size="small" onClick={props.onNewTask}>新建任务</Button>}
    </div>

    <div className="task-sections">
      <Collapsible.Root className="result-details" defaultOpen>
        <Collapsible.Trigger className="result-details__trigger">
          <span><FileText size={17} />任务结果</span>
          <span className="result-details__meta">{large ? `${total.toLocaleString()} 条汇总` : `${items.length} 条明细`}<ChevronDown size={16} /></span>
        </Collapsible.Trigger>
        <Collapsible.Panel className="result-details__panel">
          {large
            ? <div className="aggregate-panel">
                <strong className="aggregate-title">大批量任务仅显示聚合进度</strong>
                <p>任务超过 {DETAIL_LIMIT} 条，仅显示聚合数据以保持界面流畅。</p>
                <div className="aggregate-grid">
                  {[
                    ['完成', taskCount(task, 'completed', 'completed_count', '完成')],
                    ['无数据', taskCount(task, 'no_data', 'no_data_count', '无数据')],
                    ['部分成功', taskCount(task, 'partial', 'partial_count', '部分成功')],
                    ['失败', taskCount(task, 'failed', 'failed_count', '失败')],
                    ['已取消', taskCount(task, 'cancelled', 'cancelled_count', '已取消')],
                  ].map(([label, value]) => <span key={label}><strong>{Number(value).toLocaleString()}</strong>{label}</span>)}
                </div>
              </div>
            : items.length > 0
              ? <div className="table-scroll"><table><thead><tr><th>检查号</th><th>状态</th><th>文件</th><th>耗时</th><th>说明</th></tr></thead>
                  <tbody>{items.slice(0, DETAIL_LIMIT).map((item, index) => {
                    const itemStatus = statusView(item.status);
                    return <tr key={`${item.accession || item.accession_number || index}`} data-state={normalizeStatus(item.status)}>
                      <td><strong>{item.accession || item.accession_number || item.value || '—'}</strong></td>
                      <td><StatusBadge tone={itemStatus.tone}>{itemStatus.label}</StatusBadge></td>
                      <td>{Number(item.file_count || item.files || 0).toLocaleString()}</td>
                      <td>{formatDuration(item.elapsed_seconds || item.duration_seconds)}</td>
                      <td>{item.error_summary || item.message || item.detail || '—'}</td>
                    </tr>;
                  })}</tbody>
                </table></div>
              : <p className="section-empty">任务结果将在这里显示。</p>}
        </Collapsible.Panel>
      </Collapsible.Root>
      <PdiRuntime task={task} onAction={props.onPdiAction} />
    </div>
  </>;
}

function PdiRuntime({ task, onAction }: { task: Task; onAction: Props['onPdiAction'] }) {
  const raw = task.pdi || task.pdi_result;
  if (!raw) return null;
  const status = normalizeStatus(raw.status);
  const view = statusView(status);
  const finished = ['completed', 'partial', 'partial_success', 'verification_completed'].includes(status);
  const canVerify = task.actions?.can_verify_pdi === true;
  const canRetry = task.actions?.can_retry_pdi === true;
  return <Collapsible.Root className="result-details pdi-section" data-state={status} defaultOpen={canRetry}>
    <Collapsible.Trigger className="result-details__trigger">
      <span><ShieldCheck size={17} />PDI 便携目录</span>
      <span className="result-details__meta"><StatusBadge tone={view.tone}>{view.label}</StatusBadge><ChevronDown size={16} /></span>
    </Collapsible.Trigger>
    <Collapsible.Panel className="result-details__panel">
      <div className="pdi-section__content">
        <p>{String(raw.message || raw.detail || raw.output_folder || raw.output_directory || '导出状态已更新')}</p>
        <div>
          {finished && <Button size="small" onClick={() => onAction('open')}><AnimatedIcon {...semanticIconMap.openDirectory} size={15} />打开 PDI 目录</Button>}
          {finished && canVerify && <Button size="small" onClick={() => onAction('verify')}><ShieldCheck size={15} />校验</Button>}
          {canRetry && <Button size="small" onClick={() => onAction('retry')}><RotateCcw size={15} />重试导出</Button>}
        </div>
      </div>
    </Collapsible.Panel>
  </Collapsible.Root>;
}
