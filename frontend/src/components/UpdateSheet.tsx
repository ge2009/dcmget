import { Download, Globe2, RefreshCw, ShieldCheck, Sparkles } from 'lucide-react';
import { formatBytes, normalizeStatus } from '../domain';
import type { UpdateState } from '../schemas';
import { Button, Sheet, StatusBadge, SwitchRow } from './Primitives';

export function UpdateSheet({
  open,
  onOpenChange,
  update,
  busy,
  localSession,
  onAction,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  update: UpdateState;
  busy: string;
  localSession: boolean;
  onAction: (action: 'check' | 'download' | 'apply' | 'policy', body?: unknown) => void;
}) {
  const state = normalizeStatus(update.state);
  const downloaded = Boolean(update.downloaded) || ['ready', 'downloaded'].includes(state);
  const tone = state === 'error' ? 'error' : update.available ? 'warning' : downloaded ? 'success' : state === 'checking' || state === 'downloading' ? 'working' : 'neutral';
  return <Sheet open={open} onOpenChange={onOpenChange} title="软件更新" description="仅管理中心提供更新；专网环境可关闭自动联网。" size="normal">
    <div className="update-hero">
      <span><Sparkles size={22} /></span>
      <div><StatusBadge tone={tone}>{update.message || (update.supported === false ? '当前安装未配置更新服务' : update.available ? '发现可用更新' : '当前已是最新版本')}</StatusBadge><p>签名清单和更新包会在安装前再次校验。</p></div>
    </div>
    <div className="update-facts">
      <div><span>当前版本</span><strong>{update.current_version || '—'}</strong></div>
      <div><span>最新版本</span><strong>{update.latest_version || '—'}</strong></div>
      <div><span>更新方式</span><strong>{update.package_kind === 'patch' ? '增量补丁' : update.package_kind || '—'}</strong></div>
      <div><span>下载大小</span><strong>{formatBytes(update.download_size)}</strong></div>
    </div>
    {update.supported !== false && <>
      <SwitchRow checked={update.policy === 'automatic'} onCheckedChange={(checked) => onAction('policy', { policy: checked ? 'automatic' : 'disabled' })} disabled={!localSession || Boolean(busy)} label="自动检查稳定通道" description="外网不可达时静默跳过；关闭后不访问更新服务器。" />
      {!localSession && <div className="inline-warning"><Globe2 size={16} />下载和安装只允许在运行 DcmGet 的本机操作。</div>}
      <div className="update-actions">
        <Button onClick={() => onAction('check')} disabled={Boolean(busy) || !localSession}><RefreshCw size={17} className={busy === 'check' ? 'spin' : ''} />立即检查</Button>
        {update.available && !downloaded && <Button variant="primary" onClick={() => onAction('download')} disabled={Boolean(busy) || !localSession}><Download size={17} />下载更新</Button>}
        {downloaded && <Button variant="primary" onClick={() => onAction('apply')} disabled={Boolean(busy) || !localSession}><ShieldCheck size={17} />安装更新</Button>}
      </div>
    </>}
  </Sheet>;
}
