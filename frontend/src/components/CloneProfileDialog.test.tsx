import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { CloneProfileDialog } from './CloneProfileDialog';

function renderDialog(overrides: Partial<React.ComponentProps<typeof CloneProfileDialog>> = {}) {
  const props: React.ComponentProps<typeof CloneProfileDialog> = {
    open: true,
    sourceName: 'CT 接收',
    busy: false,
    error: '',
    onOpenChange: vi.fn(),
    onSubmit: vi.fn(),
    ...overrides,
  };
  return { ...render(<CloneProfileDialog {...props} />), props };
}

describe('CloneProfileDialog', () => {
  it('prefills the copy name and submits its trimmed value', () => {
    const { props } = renderDialog();
    const input = screen.getByRole('textbox', { name: '新 Profile 名称' });

    expect(screen.getByRole('dialog', { name: '复制 Profile' })).toBeInTheDocument();
    expect(input).toHaveValue('CT 接收 副本');
    fireEvent.change(input, { target: { value: '  CT 夜班  ' } });
    fireEvent.click(screen.getByRole('button', { name: '复制 Profile' }));

    expect(props.onSubmit).toHaveBeenCalledWith('CT 夜班');
  });

  it('shows inline validation and disables submission for a blank name', () => {
    const { props } = renderDialog();
    const input = screen.getByRole('textbox', { name: '新 Profile 名称' });

    fireEvent.change(input, { target: { value: '   ' } });

    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByText('请输入 Profile 名称。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '复制 Profile' })).toBeDisabled();
    fireEvent.submit(input.closest('form')!);
    expect(props.onSubmit).not.toHaveBeenCalled();
  });

  it('keeps errors in the dialog and locks controls while submitting', () => {
    renderDialog({ busy: true, error: '名称已经存在' });

    expect(screen.getByRole('alert')).toHaveTextContent('名称已经存在');
    expect(screen.getByRole('textbox', { name: '新 Profile 名称' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '正在复制…' })).toBeDisabled();
  });

  it('can be cancelled when idle', () => {
    const { props } = renderDialog();

    fireEvent.click(screen.getByRole('button', { name: '取消' }));

    expect(props.onOpenChange).toHaveBeenCalledWith(false);
  });
});
