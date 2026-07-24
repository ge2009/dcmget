import {
  AlertTriangle, Copy, Pencil, Plus, Server, Trash2,
} from 'lucide-react';
import { motion } from 'motion/react';
import { profileIssues, profileName, profileNumber, type Profile } from '../schemas';
import { BrandMark } from './BrandMark';
import { Button } from './Primitives';
import { AnimatedIcon, semanticIconMap } from './icons';

type ProfileState = 'running' | 'starting' | 'error' | 'idle';

function getProfileState(profile: Profile): { state: ProfileState; label: string } {
  const issues = profileIssues(profile);
  if (issues.length) return { state: 'error', label: '配置异常' };
  if (profile.is_running) return { state: 'running', label: '运行中' };
  if (profile.desired_running) return { state: 'starting', label: '启动中' };
  return { state: 'idle', label: '未启动' };
}

type Props = {
  profiles: Profile[];
  selectedNumber: number | null;
  loading: boolean;
  onRefresh: () => void;
  onCreate: () => void;
  onSelect: (profile: Profile) => void;
  onStart: (profile: Profile) => void;
  onStop: (profile: Profile) => void;
  onEdit: (profile: Profile) => void;
  onClone: (profile: Profile) => void;
  onDelete: (profile: Profile) => void;
};

export function ProfileRail(props: Props) {
  const selected = props.profiles.find((profile) => profileNumber(profile) === props.selectedNumber) || null;
  const selectedIssues = selected ? profileIssues(selected) : [];
  const starting = Boolean(selected && !selected.is_running && selected.desired_running);
  const selectedState = selected ? getProfileState(selected) : null;

  return <aside className="profile-rail" aria-label="Profile 列表">
    <div className="profile-rail__brand">
      <BrandMark />
      <div><strong>DcmGet</strong><small>DICOM 传输控制台</small></div>
    </div>

    <div className="profile-rail__heading">
      <div><h2>接收实例</h2><p>{props.profiles.length} 个 Profile</p></div>
      <Button size="small" variant="quiet" onClick={props.onRefresh} aria-label="刷新 Profile">
        <AnimatedIcon {...semanticIconMap.refresh} size={15} className={props.loading ? 'spin' : ''} />
      </Button>
    </div>

    <div className="profile-list">
      {!props.profiles.length && !props.loading && <div className="rail-empty">
        <Server size={22} aria-hidden="true" />
        <strong>还没有接收实例</strong>
        <p>创建后可独立配置 PACS、AE 和端口。</p>
      </div>}
      {props.profiles.map((profile, index) => {
        const number = profileNumber(profile);
        const active = number === props.selectedNumber;
        const issues = profileIssues(profile);
        const { state, label: stateLabel } = getProfileState(profile);
        return <motion.button
          type="button"
          key={number ?? String(profile.id)}
          className={`profile-row ${active ? 'is-selected' : ''}`}
          data-state={state}
          onClick={() => props.onSelect(profile)}
          aria-current={active ? 'page' : undefined}
          initial={{ opacity: 0, x: -5 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ duration: .16, delay: Math.min(index * .025, .12) }}
        >
          <span className={`profile-row__state is-${state}`} data-state={state} aria-hidden="true" />
          <span className="profile-row__identity">
            <strong>{profileName(profile)}</strong>
            <small>
              #{String(number ?? '—').padStart(2, '0')} · {profile.storage_ae_title || '未配置 AE'}:{profile.storage_port || '—'} ·{' '}
              <span className="profile-row__status-text" data-state={state} aria-label={`状态：${stateLabel}`}>{stateLabel}</span>
            </small>
          </span>
          {issues.length > 0 && <AlertTriangle className="profile-row__warning" size={15} aria-label={issues[0]} />}
        </motion.button>;
      })}
    </div>

    <div className="profile-rail__footer">
      <Button variant="primary" onClick={props.onCreate}><Plus size={16} />新建实例</Button>
      {selected && <div className="selected-profile-actions" aria-label="当前实例操作">
        <div className="selected-profile-actions__head" data-state={selectedState?.state}>
          <span>{profileName(selected)}</span>
          <small aria-live="polite">{selectedIssues.length ? '需要修复配置' : selected.is_running ? '正在运行' : starting ? '正在启动' : '当前已停止'}</small>
        </div>
        <div className="selected-profile-actions__buttons">
          {selected.is_running || starting
            ? <Button size="small" variant="secondary" onClick={() => props.onStop(selected)}><AnimatedIcon {...semanticIconMap.stopTask} size={15} />停止</Button>
            : <Button size="small" variant="secondary" onClick={() => props.onStart(selected)}><AnimatedIcon {...semanticIconMap.resumeTask} size={15} />启动</Button>}
          <Button size="small" variant="quiet" onClick={() => props.onEdit(selected)} aria-label="配置当前实例"><Pencil size={15} /></Button>
          <Button size="small" variant="quiet" onClick={() => props.onClone(selected)} aria-label="复制当前实例"><Copy size={15} /></Button>
          <Button size="small" variant="quiet" className="danger-link" disabled={Boolean(selected.is_running || selected.desired_running)} onClick={() => props.onDelete(selected)} aria-label="删除当前实例"><Trash2 size={15} /></Button>
        </div>
      </div>}
    </div>
  </aside>;
}
