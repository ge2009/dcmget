import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ProfileRail } from './ProfileRail';

const actions = {
  loading: false,
  onRefresh: vi.fn(),
  onCreate: vi.fn(),
  onSelect: vi.fn(),
  onStart: vi.fn(),
  onStop: vi.fn(),
  onEdit: vi.fn(),
  onClone: vi.fn(),
  onDelete: vi.fn(),
};

describe('profile rail status semantics', () => {
  it('exposes running, starting, error and idle states with visible text', () => {
    render(<ProfileRail
      {...actions}
      selectedNumber={1}
      profiles={[
        { number: 1, display_name: '运行实例', is_running: true },
        { number: 2, display_name: '启动实例', desired_running: true },
        { number: 3, display_name: '异常实例', issues: ['端口冲突'] },
        { number: 4, display_name: '空闲实例' },
      ]}
    />);

    const running = screen.getByText('运行实例', { selector: '.profile-row__identity > strong' }).closest('button');
    expect(running).toHaveAttribute('data-state', 'running');
    expect(screen.getByText('启动实例').closest('button')).toHaveAttribute('data-state', 'starting');
    expect(screen.getByText('异常实例').closest('button')).toHaveAttribute('data-state', 'error');
    expect(screen.getByText('空闲实例').closest('button')).toHaveAttribute('data-state', 'idle');
    expect(running).toHaveAttribute('aria-current', 'page');
    expect(screen.getByText('启动实例').closest('button')).not.toHaveAttribute('aria-current');
    expect(screen.getByLabelText('状态：运行中')).toHaveTextContent('运行中');
    expect(screen.getByLabelText('状态：启动中')).toHaveTextContent('启动中');
    expect(screen.getByLabelText('状态：配置异常')).toHaveTextContent('配置异常');
    expect(screen.getByLabelText('状态：未启动')).toHaveTextContent('未启动');
  });
});
