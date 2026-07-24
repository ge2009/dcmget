import { Save } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { UnknownRecord } from '../schemas';
import { Button, SelectField, Sheet, SwitchRow, TextField } from './Primitives';

const STRING_FIELDS = [
  'pacs_server_ip', 'pacs_server_port', 'pacs_ae_title', 'calling_ae_title', 'storage_ae_title',
  'storage_port', 'dicom_destination_folder', 'dcmtk_bin_dir', 'directory_template',
  'minimum_free_space_gb', 'auto_retry_attempts', 'auto_retry_backoff_seconds', 'circuit_breaker_failures',
  'max_log_file_size_mb', 'anonymization_profile', 'pdi_institution_name', 'pdi_output_folder',
  'pdi_volume_size_gb',
] as const;

export function SettingsSheet({
  open,
  onOpenChange,
  config,
  busy,
  status,
  topologyLocked = false,
  onSave,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  config: UnknownRecord;
  busy: boolean;
  status: string;
  topologyLocked?: boolean;
  onSave: (config: UnknownRecord) => void;
}) {
  const [draft, setDraft] = useState<UnknownRecord>({});
  useEffect(() => {
    const derived = {
      ...config,
      minimum_free_space_gb: config.minimum_free_space_gb ?? Number(config.minimum_free_space_bytes || 0) / 1024 ** 3,
      max_log_file_size_mb: config.max_log_file_size_mb ?? Number(config.max_log_file_size_bytes || 0) / 1024 ** 2,
      pdi_volume_size_gb: config.pdi_volume_size_gb ?? Number(config.pdi_volume_size_bytes || 0) / 1024 ** 3,
    };
    setDraft(derived);
  }, [config, open]);
  const text = (key: typeof STRING_FIELDS[number]) => ({
    value: String(draft[key] ?? ''),
    onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setDraft((current) => ({ ...current, [key]: event.target.value })),
  });
  const toggle = (key: string) => (value: boolean) => setDraft((current) => ({ ...current, [key]: value }));
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const payload = { ...draft };
    for (const key of ['pacs_server_port', 'storage_port', 'auto_retry_attempts', 'auto_retry_backoff_seconds', 'circuit_breaker_failures']) {
      if (payload[key] !== '') payload[key] = Number.parseInt(String(payload[key]), 10);
    }
    payload.minimum_free_space_bytes = Math.round(Number(payload.minimum_free_space_gb || 0) * 1024 ** 3);
    payload.max_log_file_size_bytes = Math.round(Number(payload.max_log_file_size_mb || 0) * 1024 ** 2);
    payload.pdi_volume_size_bytes = Math.round(Number(payload.pdi_volume_size_gb || 0) * 1024 ** 3);
    payload.pdi_export_enabled = false;
    delete payload.minimum_free_space_gb;
    delete payload.max_log_file_size_mb;
    delete payload.pdi_volume_size_gb;
    delete payload.web_port;
    delete payload.web_bind_address;
    delete payload.web_open_browser;
    delete payload.web_session_timeout_minutes;
    onSave(payload);
  };
  return <Sheet open={open} onOpenChange={onOpenChange} title="当前 Profile 设置" description="技术参数按用途分组；保存后新任务使用新配置。" footer={<><span className="sheet-status">{status}</span><Button form="settings-form" type="submit" variant="primary" disabled={busy}><Save size={17} />{busy ? '保存中…' : '保存设置'}</Button></>}>
    <form id="settings-form" className="settings-form" onSubmit={submit}>
      <fieldset><legend><span>01</span><div><strong>PACS 服务端</strong><small>查询与 C-MOVE 来源</small></div></legend><div className="form-grid">
        <TextField label="PACS 地址" required {...text('pacs_server_ip')} />
        <TextField label="PACS 端口" inputMode="numeric" required {...text('pacs_server_port')} />
        <TextField label="PACS AE" maxLength={16} required wide disabled={topologyLocked} {...text('pacs_ae_title')} />
      </div></fieldset>
      <fieldset><legend><span>02</span><div><strong>本地接收端</strong><small>storescp 身份与监听端口</small></div></legend><div className="form-grid">
        {topologyLocked && <div className="inline-warning field--wide">当前 Profile 正在运行。AE 与接收端口请从工作台的“启动参数”入口停止后修改。</div>}
        <TextField label="本机调用 AE" maxLength={16} required wide disabled={topologyLocked} {...text('calling_ae_title')} />
        <TextField label="接收 AE" maxLength={16} required disabled={topologyLocked} {...text('storage_ae_title')} />
        <TextField label="监听端口" inputMode="numeric" required disabled={topologyLocked} {...text('storage_port')} />
      </div></fieldset>
      <fieldset><legend><span>03</span><div><strong>保存与可靠性</strong><small>归档目录和断线恢复</small></div></legend><div className="form-grid">
        <TextField label="默认 DICOM 目录" wide {...text('dicom_destination_folder')} />
        <TextField label="DCMTK bin 目录" wide {...text('dcmtk_bin_dir')} />
        <TextField label="目录模板" hint="{PatientID} / {AccessionNumber} / {StudyInstanceUID}" wide {...text('directory_template')} />
        <TextField label="磁盘保留（GB）" inputMode="decimal" {...text('minimum_free_space_gb')} />
        <TextField label="自动重试次数" inputMode="numeric" {...text('auto_retry_attempts')} />
        <TextField label="重试间隔（秒）" inputMode="numeric" {...text('auto_retry_backoff_seconds')} />
        <TextField label="连续失败暂停阈值" inputMode="numeric" {...text('circuit_breaker_failures')} />
        <TextField label="单日志上限（MB）" inputMode="numeric" {...text('max_log_file_size_mb')} />
      </div></fieldset>
      <fieldset><legend><span>04</span><div><strong>隐私与 PDI</strong><small>下载后的处理策略</small></div></legend>
        <SwitchRow checked={Boolean(draft.anonymization_enabled)} onCheckedChange={toggle('anonymization_enabled')} label="启用匿名化" description="不能清除烧录在像素中的文字或面部特征。" />
        <SelectField label="匿名方案" {...text('anonymization_profile')}><option value="basic">基础脱敏（院内）</option><option value="research">研究匿名（推荐）</option><option value="strict">严格元数据匿名</option></SelectField>
        <div className="inline-warning">新任务默认不生成 PDI；需要时请在任务页按需开启。未匿名数据可能包含患者隐私。</div>
        <div className="form-grid">
          <TextField label="机构名称" {...text('pdi_institution_name')} /><TextField label="PDI 输出目录" {...text('pdi_output_folder')} />
          <TextField label="单卷容量（GB）" inputMode="decimal" {...text('pdi_volume_size_gb')} />
          <SwitchRow checked={Boolean(draft.pdi_include_ohif_viewer)} onCheckedChange={toggle('pdi_include_ohif_viewer')} label="包含离线中文 OHIF" />
        </div>
      </fieldset>
    </form>
  </Sheet>;
}
