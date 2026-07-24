import { Activity, Archive, ClipboardCheck, FileArchive, KeyRound, ScrollText } from 'lucide-react';
import { useState } from 'react';
import type { UnknownRecord } from '../schemas';
import { Button, Sheet, StatusBadge } from './Primitives';
import { AnimatedIcon, semanticIconMap } from './icons';

export function OperationsSheet({
  open,
  onOpenChange,
  health,
  license,
  releases,
  busy,
  onRefresh,
  onActivate,
  onOperation,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  health: UnknownRecord;
  license: UnknownRecord;
  releases: UnknownRecord[];
  busy: boolean;
  onRefresh: () => void;
  onActivate: (token: string) => void;
  onOperation: (name: string) => void;
}) {
  const [token, setToken] = useState('');
  const checks = Array.isArray(health.checks) ? health.checks.map((item) => item as UnknownRecord) : [];
  const licensed = Boolean(license.licensed ?? license.registered);
  const licenseText = licensed ? `已授权${license.customer ? ` · ${license.customer}` : ''}` : license.trial_remaining != null ? `试用剩余 ${license.trial_remaining} 次` : String(license.message || '未授权');
  return <Sheet open={open} onOpenChange={onOpenChange} title="运维与产品信息" description="低频诊断、授权、支持材料和版本说明集中在这里。">
    <div className="operations-sections">
      <section className="operation-section">
        <header><div><p className="eyebrow">HEALTH</p><h3>运行健康</h3></div><Button size="small" onClick={onRefresh} disabled={busy}><AnimatedIcon {...semanticIconMap.refresh} size={15} className={busy ? 'spin' : ''} />重新检查</Button></header>
        <ul className="health-list">{checks.length ? checks.map((check, index) => {
          const ok = Boolean(check.ok ?? check.success ?? check.ready);
          const warning = check.severity === 'warning' || check.warning === true;
          return <li key={String(check.name || index)}><StatusBadge tone={warning ? 'warning' : ok ? 'success' : 'error'}>{warning ? '提醒' : ok ? '正常' : '异常'}</StatusBadge><div><strong>{String(check.name || check.label || '检查项')}</strong><small>{String(check.message || check.detail || '')}</small></div></li>;
        }) : <li className="muted">尚未执行健康检查。</li>}</ul>
      </section>
      <section className="operation-section">
        <header><div><p className="eyebrow">LICENSE</p><h3>软件授权</h3></div><StatusBadge tone={licensed ? 'success' : 'warning'}>{licenseText}</StatusBadge></header>
        <dl className="license-facts"><div><dt>机器码</dt><dd>{String(license.machine_code || '—')}</dd></div><div><dt>状态</dt><dd>{licenseText}</dd></div></dl>
        <label className="field"><span>注册码</span><textarea rows={3} value={token} onChange={(event) => setToken(event.target.value)} placeholder="粘贴 DGM1 开头的注册码" /></label>
        <Button variant="primary" disabled={!token.trim() || busy} onClick={() => { onActivate(token.trim()); setToken(''); }}><KeyRound size={16} />激活授权</Button>
      </section>
      <section className="operation-section operation-section--wide">
        <header><div><p className="eyebrow">SUPPORT</p><h3>诊断与支持</h3></div></header>
        <div className="operation-grid">
          <button onClick={() => onOperation('open-log-directory')}><AnimatedIcon {...semanticIconMap.openDirectory} /><strong>日志目录</strong><small>查看完整运行日志</small></button>
          <button onClick={() => onOperation('profile-backup')}><Archive /><strong>备份 Profile</strong><small>保存当前实例配置</small></button>
          <button onClick={() => onOperation('support-bundle')}><FileArchive /><strong>脱敏支持包</strong><small>不包含 DICOM 文件</small></button>
          <button onClick={() => onOperation('acceptance-report')}><ClipboardCheck /><strong>验收报告</strong><small>导出环境与任务摘要</small></button>
        </div>
      </section>
      <section className="operation-section operation-section--wide">
        <header><div><p className="eyebrow">RELEASE NOTES</p><h3>版本说明</h3></div></header>
        <div className="release-list">{releases.length ? releases.map((release, index) => <details key={String(release.version || index)} open={index === 0}><summary><ScrollText size={16} />{[release.version, release.date].filter(Boolean).join(' · ')}</summary><ul>{(Array.isArray(release.items) ? release.items : Array.isArray(release.changes) ? release.changes : []).map((item, itemIndex) => <li key={itemIndex}>{String(item)}</li>)}</ul></details>) : <p className="muted">暂无版本说明。</p>}</div>
      </section>
    </div>
  </Sheet>;
}
