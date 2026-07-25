import { Dialog } from '@base-ui/react/dialog';
import { Copy } from 'lucide-react';
import { useEffect, useId, useState } from 'react';
import { Button } from './Primitives';

export function CloneProfileDialog({
  open,
  sourceName,
  busy,
  error,
  onOpenChange,
  onSubmit,
}: {
  open: boolean;
  sourceName: string;
  busy: boolean;
  error: string;
  onOpenChange: (open: boolean) => void;
  onSubmit: (displayName: string) => void | Promise<void>;
}) {
  const [displayName, setDisplayName] = useState('');
  const validationId = useId();
  const errorId = useId();

  useEffect(() => {
    if (open) setDisplayName(`${sourceName} 副本`);
  }, [open, sourceName]);

  const trimmedName = displayName.trim();
  const empty = trimmedName.length === 0;
  const describedBy = [empty ? validationId : '', error ? errorId : ''].filter(Boolean).join(' ') || undefined;

  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (empty || busy) return;
    void onSubmit(trimmedName);
  };

  return <Dialog.Root open={open} onOpenChange={(nextOpen) => !busy && onOpenChange(nextOpen)}>
    <Dialog.Portal>
      <Dialog.Backdrop className="dialog-backdrop" />
      <Dialog.Viewport className="dialog-viewport dialog-viewport--center">
        <Dialog.Popup className="confirm-dialog">
          <Dialog.Title className="sheet__title">复制 Profile</Dialog.Title>
          <Dialog.Description className="confirm-dialog__description">
            将复制“{sourceName}”的 PACS、接收端和保存目录配置。任务与影像文件不会复制。
          </Dialog.Description>
          <form className="profile-editor" aria-busy={busy} onSubmit={submit}>
            <label className="field">
              <span>新 Profile 名称</span>
              <input
                autoFocus
                autoComplete="off"
                disabled={busy}
                maxLength={80}
                value={displayName}
                aria-invalid={empty || undefined}
                aria-describedby={describedBy}
                onChange={(event) => setDisplayName(event.target.value)}
              />
            </label>
            {empty && <p id={validationId} className="field-error">请输入 Profile 名称。</p>}
            {error && <p id={errorId} className="field-error" role="alert">{error}</p>}
            <div className="confirm-dialog__actions">
              <Dialog.Close type="button" className="button button--secondary button--normal" disabled={busy}>取消</Dialog.Close>
              <Button type="submit" variant="primary" disabled={empty || busy}>
                <Copy size={16} aria-hidden="true" />{busy ? '正在复制…' : '复制 Profile'}
              </Button>
            </div>
          </form>
        </Dialog.Popup>
      </Dialog.Viewport>
    </Dialog.Portal>
  </Dialog.Root>;
}
