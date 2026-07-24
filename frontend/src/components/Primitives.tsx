import { Dialog } from '@base-ui/react/dialog';
import { Switch } from '@base-ui/react/switch';
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from 'react';
import { X } from 'lucide-react';
import type { Tone } from '../domain';

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'quiet' | 'danger';
  size?: 'small' | 'normal' | 'large';
};

export function Button({ variant = 'secondary', size = 'normal', className = '', ...props }: ButtonProps) {
  return <button className={`button button--${variant} button--${size} ${className}`} {...props} />;
}

export function StatusBadge({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`status-badge status-badge--${tone}`}><i aria-hidden="true" />{children}</span>;
}

export function Field({
  label,
  hint,
  wide = false,
  children,
}: { label: string; hint?: string; wide?: boolean; children: ReactNode }) {
  return <label className={`field ${wide ? 'field--wide' : ''}`}>
    <span>{label}</span>
    {children}
    {hint && <small>{hint}</small>}
  </label>;
}

export function TextField(props: InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: string; wide?: boolean }) {
  const { label, hint, wide, ...input } = props;
  return <Field label={label} hint={hint} wide={wide}><input {...input} /></Field>;
}

export function SelectField(props: SelectHTMLAttributes<HTMLSelectElement> & { label: string; hint?: string; wide?: boolean; children: ReactNode }) {
  const { label, hint, wide, children, ...select } = props;
  return <Field label={label} hint={hint} wide={wide}><select {...select}>{children}</select></Field>;
}

export function SwitchRow({
  checked,
  onCheckedChange,
  label,
  description,
  disabled = false,
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  label: string;
  description?: string;
  disabled?: boolean;
}) {
  return <div className="switch-row" aria-disabled={disabled || undefined}>
    <span><strong>{label}</strong>{description && <small>{description}</small>}</span>
    <Switch.Root className="switch-root" checked={checked} onCheckedChange={onCheckedChange} disabled={disabled} aria-label={label}>
      <Switch.Thumb className="switch-thumb" />
    </Switch.Root>
  </div>;
}

export function Sheet({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  size = 'wide',
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
  size?: 'normal' | 'wide';
}) {
  return <Dialog.Root open={open} onOpenChange={onOpenChange}>
    <Dialog.Portal>
      <Dialog.Backdrop className="dialog-backdrop" />
      <Dialog.Viewport className="dialog-viewport">
        <Dialog.Popup className={`sheet sheet--${size}`}>
          <header className="sheet__header">
            <div>
              <Dialog.Title className="sheet__title">{title}</Dialog.Title>
              {description && <Dialog.Description className="sheet__description">{description}</Dialog.Description>}
            </div>
            <Dialog.Close className="icon-button" aria-label="关闭"><X size={20} /></Dialog.Close>
          </header>
          <div className="sheet__body">{children}</div>
          {footer && <footer className="sheet__footer">{footer}</footer>}
        </Dialog.Popup>
      </Dialog.Viewport>
    </Dialog.Portal>
  </Dialog.Root>;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = '确认',
  danger = true,
  busy = false,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  confirmLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void | Promise<void>;
}) {
  return <Dialog.Root open={open} onOpenChange={onOpenChange}>
    <Dialog.Portal>
      <Dialog.Backdrop className="dialog-backdrop" />
      <Dialog.Viewport className="dialog-viewport dialog-viewport--center">
        <Dialog.Popup className="confirm-dialog">
          <Dialog.Title className="sheet__title">{title}</Dialog.Title>
          <Dialog.Description className="confirm-dialog__description">{description}</Dialog.Description>
          <div className="confirm-dialog__actions">
            <Dialog.Close className="button button--secondary button--normal">取消</Dialog.Close>
            <Button variant={danger ? 'danger' : 'primary'} disabled={busy} onClick={onConfirm}>
              {busy ? '处理中…' : confirmLabel}
            </Button>
          </div>
        </Dialog.Popup>
      </Dialog.Viewport>
    </Dialog.Portal>
  </Dialog.Root>;
}
