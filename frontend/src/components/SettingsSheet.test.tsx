import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SettingsSheet } from './SettingsSheet';

describe('settings PDI defaults', () => {
  it('keeps PDI parameters editable without offering a default-enable switch', () => {
    const onSave = vi.fn();
    render(<SettingsSheet
      open
      onOpenChange={vi.fn()}
      config={{
        pacs_server_ip: '127.0.0.1',
        pacs_server_port: 104,
        pacs_ae_title: 'PACS',
        calling_ae_title: 'DCMGET',
        storage_ae_title: 'DCMGET',
        storage_port: 6666,
        pdi_export_enabled: true,
        pdi_institution_name: '测试医院',
        pdi_output_folder: 'D:\\PDI',
        pdi_include_ohif_viewer: true,
        web_port: 8787,
        web_bind_address: '0.0.0.0',
        web_open_browser: true,
        web_session_timeout_minutes: 480,
      }}
      busy={false}
      status=""
      onSave={onSave}
    />);

    expect(screen.queryByRole('switch', { name: '新任务默认生成 PDI' })).not.toBeInTheDocument();
    expect(screen.getByText('新任务默认不生成 PDI；需要时请在任务页按需开启。未匿名数据可能包含患者隐私。')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '机构名称' })).toHaveValue('测试医院');
    expect(screen.getByRole('textbox', { name: 'PDI 输出目录' })).toHaveValue('D:\\PDI');
    expect(screen.getByRole('switch', { name: '包含离线中文 OHIF' })).toBeChecked();
    expect(screen.queryByRole('textbox', { name: 'Web 端口' })).not.toBeInTheDocument();
    expect(screen.queryByRole('switch', { name: '允许局域网访问' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '保存设置' }));
    expect(onSave).toHaveBeenCalledOnce();
    const payload = onSave.mock.calls[0][0];
    expect(payload).not.toHaveProperty('web_port');
    expect(payload).not.toHaveProperty('web_bind_address');
    expect(payload).not.toHaveProperty('web_open_browser');
    expect(payload).not.toHaveProperty('web_session_timeout_minutes');
    expect(payload.pdi_export_enabled).toBe(false);
  });
});
