import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ProfileEditor } from './ProfileEditor';

describe('ProfileEditor internal Web endpoint', () => {
  it('hides the internal Web port and does not submit it', () => {
    const onSave = vi.fn();
    render(<ProfileEditor
      open
      onOpenChange={vi.fn()}
      profile={{
        number: 3,
        display_name: 'CT 工作站',
        pacs_server_ip: '172.16.0.20',
        pacs_server_port: 104,
        calling_ae_title: 'DCMGET',
        pacs_ae_title: 'PACS',
        storage_ae_title: 'DCMGET',
        storage_port: 6663,
        web_port: 8899,
        dicom_destination_folder: 'D:/DICOM',
      }}
      launchAfterSave={false}
      busy={false}
      error=""
      onSave={onSave}
    />);

    expect(screen.queryByLabelText('Web 端口')).not.toBeInTheDocument();
    expect(screen.getByText(/工作台内部连接由系统自动管理/)).toBeInTheDocument();
    fireEvent.submit(document.querySelector('#profile-editor')!);

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0]).not.toHaveProperty('web_port');
  });
});
