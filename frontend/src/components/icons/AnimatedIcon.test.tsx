import type { CSSProperties, ReactNode } from 'react';
import { createRef } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { Download } from 'lucide-react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AnimatedIcon } from './AnimatedIcon';

const motionState = vi.hoisted(() => ({
  reduced: false,
  set: vi.fn(),
  start: vi.fn(() => Promise.resolve()),
  stop: vi.fn(),
}));

vi.mock('motion/react', () => ({
  motion: {
    span: ({
      animate: _animate,
      children,
      initial: _initial,
      style,
    }: {
      animate?: unknown;
      children?: ReactNode;
      initial?: unknown;
      style?: CSSProperties;
    }) => <span style={style}>{children}</span>,
  },
  useAnimationControls: () => motionState,
  useReducedMotion: () => motionState.reduced,
}));

beforeEach(() => {
  motionState.reduced = false;
  motionState.set.mockClear();
  motionState.start.mockClear();
  motionState.stop.mockClear();
});
describe('AnimatedIcon', () => {
  it('is decorative by default and forwards Lucide SVG props and refs', () => {
    const ref = createRef<SVGSVGElement>();
    render(
      <AnimatedIcon
        ref={ref}
        icon={Download}
        className="signal-icon"
        data-testid="download-icon"
        size={19}
        strokeWidth={1.75}
      />,
    );

    const icon = screen.getByTestId('download-icon');
    expect(icon).toHaveAttribute('aria-hidden', 'true');
    expect(icon).toHaveAttribute('focusable', 'false');
    expect(icon).toHaveAttribute('height', '19');
    expect(icon).toHaveAttribute('width', '19');
    expect(icon).toHaveClass('signal-icon');
    expect(ref.current).toBe(icon);
  });

  it('supports a labelled non-decorative icon without adding a tab stop', () => {
    render(
      <AnimatedIcon
        decorative={false}
        icon={Download}
        label="下载影像"
      />,
    );

    const icon = screen.getByRole('img', { name: '下载影像' });
    expect(icon).not.toHaveAttribute('aria-hidden');
    expect(icon).toHaveAttribute('focusable', 'false');
    expect(icon).not.toHaveAttribute('tabindex');
  });

  it('plays one short response from the nearest interactive parent', () => {
    render(
      <button type="button">
        <AnimatedIcon animation="download" icon={Download} />
        开始下载
      </button>,
    );

    const button = screen.getByRole('button', { name: '开始下载' });
    fireEvent.focus(button);
    expect(motionState.start).toHaveBeenCalledTimes(1);

    fireEvent.pointerDown(button);
    expect(motionState.start).toHaveBeenCalledTimes(2);

    button.dispatchEvent(new Event('pointerenter'));
    expect(motionState.start).toHaveBeenCalledTimes(3);
  });

  it('plays only when statusKey changes, using the status animation', () => {
    const { rerender } = render(
      <AnimatedIcon
        icon={Download}
        statusAnimation="draw"
        statusKey="pending"
      />,
    );
    expect(motionState.start).not.toHaveBeenCalled();

    rerender(
      <AnimatedIcon
        icon={Download}
        statusAnimation="draw"
        statusKey="ok"
      />,
    );
    expect(motionState.start).toHaveBeenCalledTimes(1);
    expect(motionState.start).toHaveBeenCalledWith(
      expect.objectContaining({ opacity: [0.35, 1, 1] }),
    );

    rerender(
      <AnimatedIcon
        icon={Download}
        statusAnimation="draw"
        statusKey="ok"
      />,
    );
    expect(motionState.start).toHaveBeenCalledTimes(1);
  });

  it('stays static when reduced motion is requested', () => {
    motionState.reduced = true;
    const { rerender } = render(
      <button type="button">
        <AnimatedIcon icon={Download} statusKey="pending" />
        下载
      </button>,
    );

    fireEvent.focus(screen.getByRole('button', { name: '下载' }));
    rerender(
      <button type="button">
        <AnimatedIcon icon={Download} statusKey="ok" />
        下载
      </button>,
    );

    expect(motionState.start).not.toHaveBeenCalled();
    expect(motionState.set).toHaveBeenCalled();
  });
});
