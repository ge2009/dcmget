import { Save } from 'lucide-react';
import { useEffect, useState } from 'react';
import { profileNumber, type Profile } from '../schemas';
import { Button, Sheet, TextField } from './Primitives';

export type ProfileDraft = {
  profile_number: number;
  display_name: string;
  dicom_destination_folder: string;
  pacs_server_ip: string;
  pacs_server_port: number;
  calling_ae_title: string;
  pacs_ae_title: string;
  storage_ae_title: string;
  storage_port: number;
  web_port: number;
};

export function ProfileEditor({
  open,
  onOpenChange,
  profile,
  launchAfterSave,
  busy,
  error,
  onSave,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  profile: Profile | null;
  launchAfterSave: boolean;
  busy: boolean;
  error: string;
  onSave: (draft: ProfileDraft, launch: boolean) => void;
}) {
  const [draft, setDraft] = useState<ProfileDraft | null>(null);
  useEffect(() => {
    if (!profile) return;
    setDraft({
      profile_number: profileNumber(profile) || 0,
      display_name: profile.display_name || `Profile ${profileNumber(profile) || ''}`,
      dicom_destination_folder: profile.destination_directory || profile.dicom_destination_folder || '',
      pacs_server_ip: profile.pacs_server_ip || '',
      pacs_server_port: Number(profile.pacs_server_port || 104),
      calling_ae_title: profile.calling_ae_title || 'DCMGET',
      pacs_ae_title: profile.pacs_ae_title || 'PACS',
      storage_ae_title: profile.storage_ae_title || 'DCMGET',
      storage_port: Number(profile.storage_port || 6666),
      web_port: Number(profile.web_port || 8787),
    });
  }, [profile, open]);
  if (!draft) return null;
  const value = (key: keyof ProfileDraft) => ({
    value: String(draft[key]),
    onChange: (event: React.ChangeEvent<HTMLInputElement>) => setDraft((current) => current && ({
      ...current,
      [key]: ['profile_number', 'pacs_server_port', 'storage_port', 'web_port'].includes(key)
        ? Number.parseInt(event.target.value, 10) || 0
        : event.target.value,
    })),
  });
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (draft.storage_port === draft.web_port) return;
    onSave(draft, launchAfterSave);
  };
  return <Sheet open={open} onOpenChange={onOpenChange} title={launchAfterSave ? '配置并启动 Profile' : '配置 Profile'} description="每个 Profile 必须使用独立的 DICOM 接收端口和 Web 端口。" footer={<Button type="submit" form="profile-editor" variant="primary" disabled={busy}><Save size={17} />{busy ? '保存中…' : launchAfterSave ? '保存并启动' : '保存配置'}</Button>} size="normal">
    <form id="profile-editor" className="profile-editor" onSubmit={submit}>
      <fieldset className="profile-editor__section">
        <legend>基本信息</legend>
        <p className="profile-editor__section-description">用于区分工作台中的接收实例，并指定该实例的影像保存位置。</p>
        <TextField label="Profile 名称" required maxLength={80} hint="显示在左侧实例列表中，不会发送给 PACS。" {...value('display_name')} />
        <TextField label="影像目标目录" required hint="接收到的 DICOM 文件和任务日志将保存在此目录下。" {...value('dicom_destination_folder')} />
      </fieldset>

      <fieldset className="profile-editor__section">
        <legend>PACS 连接</legend>
        <p className="profile-editor__section-description">配置远端 PACS 地址，以及发起 C-MOVE 请求时使用的 AE 标识。</p>
        <div className="form-grid"><TextField label="PACS 地址" required hint="PACS 服务器的 IP 地址或主机名。" {...value('pacs_server_ip')} /><TextField label="PACS 端口" required inputMode="numeric" hint="PACS 的 DICOM 服务端口。" {...value('pacs_server_port')} /></div>
        <div className="form-grid"><TextField label="本机调用 AE" required maxLength={16} hint="DcmGet 向 PACS 发起查询时使用的 AE。" {...value('calling_ae_title')} /><TextField label="PACS AE" required maxLength={16} hint="PACS 服务端配置的 Called AE。" {...value('pacs_ae_title')} /></div>
      </fieldset>

      <fieldset className="profile-editor__section">
        <legend>本地接收端</legend>
        <p className="profile-editor__section-description">配置 storescp 接收服务。接收 AE 与端口需要和 PACS 中的 C-MOVE 目标映射一致。</p>
        <div className="form-grid"><TextField label="接收 AE" required maxLength={16} hint="PACS 回传影像时使用的目标 AE。" {...value('storage_ae_title')} /><TextField label="SCP 接收端口" required inputMode="numeric" hint="storescp 监听的 DICOM 入站端口。" {...value('storage_port')} /></div>
        <TextField label="Web 端口" required inputMode="numeric" hint="工作台访问端口，不能与接收端口或其他 Profile 端口重复。" {...value('web_port')} />
      </fieldset>
      {draft.storage_port === draft.web_port && <p className="field-error">SCP 接收端口不能与 Web 端口相同。</p>}
      {error && <p className="field-error" role="alert">{error}</p>}
    </form>
  </Sheet>;
}
