import {
  forwardRef,
  useCallback,
  useEffect,
  useRef,
  type CSSProperties,
  type ForwardedRef,
} from 'react';
import type { LucideIcon, LucideProps } from 'lucide-react';
import {
  motion,
  useAnimationControls,
  useReducedMotion,
  type TargetAndTransition,
} from 'motion/react';

export type AnimatedIconMotion =
  | 'attention'
  | 'download'
  | 'draw'
  | 'folder'
  | 'pause'
  | 'play'
  | 'pulse'
  | 'refresh'
  | 'rotate'
  | 'stop'
  | 'upload'
  | 'wifi';

export type AnimatedIconStatusKey = string | number | boolean | null | undefined;

type AccessibleIcon = {
  decorative: false;
  label: string;
};

type DecorativeIcon = {
  decorative?: true;
  label?: never;
};

export type AnimatedIconProps = Omit<
  LucideProps,
  'aria-hidden' | 'aria-label' | 'ref' | 'role'
> &
  (AccessibleIcon | DecorativeIcon) & {
    icon: LucideIcon;
    animation?: AnimatedIconMotion;
    statusAnimation?: AnimatedIconMotion;
    statusKey?: AnimatedIconStatusKey;
  };

const INTERACTIVE_ANCESTOR = [
  'button',
  'a[href]',
  'input',
  'select',
  'textarea',
  'summary',
  '[role="button"]',
  '[role="menuitem"]',
  '[role="option"]',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

const RESTING_STATE = {
  opacity: 1,
  rotate: 0,
  scale: 1,
  x: 0,
  y: 0,
} satisfies TargetAndTransition;

const MOTION_TRANSITION = {
  duration: 0.16,
  ease: [0.16, 1, 0.3, 1],
} as const;

const MOTION_KEYFRAMES: Record<AnimatedIconMotion, TargetAndTransition> = {
  attention: {
    x: [0, -1.5, 1.5, 0],
    transition: MOTION_TRANSITION,
  },
  download: {
    y: [0, 2, -0.5, 0],
    transition: MOTION_TRANSITION,
  },
  draw: {
    opacity: [0.35, 1, 1],
    scale: [0.72, 1.04, 1],
    transition: MOTION_TRANSITION,
  },
  folder: {
    rotate: [0, -4, 3, 0],
    scale: [1, 1.04, 1],
    transition: MOTION_TRANSITION,
  },
  pause: {
    scale: [1, 0.88, 1],
    transition: MOTION_TRANSITION,
  },
  play: {
    x: [0, 2, 0],
    transition: MOTION_TRANSITION,
  },
  pulse: {
    opacity: [1, 0.72, 1],
    scale: [1, 1.08, 1],
    transition: MOTION_TRANSITION,
  },
  refresh: {
    rotate: [0, 180, 360],
    transition: MOTION_TRANSITION,
  },
  rotate: {
    rotate: [0, -22, 18, 0],
    transition: MOTION_TRANSITION,
  },
  stop: {
    scale: [1, 0.82, 1],
    transition: MOTION_TRANSITION,
  },
  upload: {
    y: [0, -2, 0.5, 0],
    transition: MOTION_TRANSITION,
  },
  wifi: {
    opacity: [0.45, 1, 1],
    scale: [0.86, 1.04, 1],
    transition: MOTION_TRANSITION,
  },
};

const WRAPPER_STYLE: CSSProperties = {
  alignItems: 'center',
  display: 'inline-flex',
  flex: '0 0 auto',
  justifyContent: 'center',
  lineHeight: 0,
  transformOrigin: '50% 50%',
  verticalAlign: 'middle',
};

function assignRef(ref: ForwardedRef<SVGSVGElement>, node: SVGSVGElement | null) {
  if (typeof ref === 'function') {
    ref(node);
    return;
  }
  if (ref) ref.current = node;
}

function isDisabled(element: Element) {
  return element.matches(':disabled, [aria-disabled="true"]');
}

/**
 * Adds short, non-looping feedback to a Lucide icon without making decorative
 * icons separate keyboard stops. Interaction events follow the nearest button
 * or other interactive ancestor, so focus and press feedback work across the
 * complete control rather than only over the SVG pixels.
 */
export const AnimatedIcon = forwardRef<SVGSVGElement, AnimatedIconProps>(
  function AnimatedIcon(
    {
      animation = 'pulse',
      decorative = true,
      icon: Icon,
      label,
      statusAnimation,
      statusKey,
      ...svgProps
    },
    forwardedRef,
  ) {
    const iconRef = useRef<SVGSVGElement | null>(null);
    const controls = useAnimationControls();
    const shouldReduceMotion = useReducedMotion();
    const previousStatusKey = useRef<AnimatedIconStatusKey>(statusKey);
    const hasMounted = useRef(false);

    const setIconRef = useCallback(
      (node: SVGSVGElement | null) => {
        iconRef.current = node;
        assignRef(forwardedRef, node);
      },
      [forwardedRef],
    );

    const playOnce = useCallback(
      (motionName: AnimatedIconMotion) => {
        if (shouldReduceMotion) return;
        controls.stop();
        controls.set(RESTING_STATE);
        void controls.start(MOTION_KEYFRAMES[motionName]);
      },
      [controls, shouldReduceMotion],
    );

    useEffect(() => {
      if (!shouldReduceMotion) return;
      controls.stop();
      controls.set(RESTING_STATE);
    }, [controls, shouldReduceMotion]);

    useEffect(() => {
      const svg = iconRef.current;
      if (!svg || shouldReduceMotion) return;

      const interactionRoot = svg.closest(INTERACTIVE_ANCESTOR) ?? svg;
      const playInteraction = () => {
        if (!isDisabled(interactionRoot)) playOnce(animation);
      };

      interactionRoot.addEventListener('focusin', playInteraction);
      interactionRoot.addEventListener('pointerdown', playInteraction);
      interactionRoot.addEventListener('pointerenter', playInteraction);

      return () => {
        interactionRoot.removeEventListener('focusin', playInteraction);
        interactionRoot.removeEventListener('pointerdown', playInteraction);
        interactionRoot.removeEventListener('pointerenter', playInteraction);
      };
    }, [animation, playOnce, shouldReduceMotion]);

    useEffect(() => {
      if (!hasMounted.current) {
        hasMounted.current = true;
        previousStatusKey.current = statusKey;
        return;
      }

      if (Object.is(previousStatusKey.current, statusKey)) return;
      previousStatusKey.current = statusKey;
      playOnce(statusAnimation ?? animation);
    }, [animation, playOnce, statusAnimation, statusKey]);

    return (
      <motion.span animate={controls} initial={false} style={WRAPPER_STYLE}>
        <Icon
          {...svgProps}
          ref={setIconRef}
          aria-hidden={decorative ? true : undefined}
          aria-label={decorative ? undefined : label}
          focusable="false"
          role={decorative ? undefined : 'img'}
        />
      </motion.span>
    );
  },
);
