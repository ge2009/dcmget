import {
  Activity,
  AlertCircle,
  Check,
  CheckCircle2,
  CircleStop,
  Download,
  FolderOpen,
  Pause,
  Play,
  RefreshCw,
  Settings,
  Upload,
  Wifi,
  WifiOff,
  X,
  type LucideIcon,
} from 'lucide-react';
import type { AnimatedIconMotion } from './AnimatedIcon';

export { AnimatedIcon } from './AnimatedIcon';
export type {
  AnimatedIconMotion,
  AnimatedIconProps,
  AnimatedIconStatusKey,
} from './AnimatedIcon';

export interface SemanticIconDefinition {
  animation: AnimatedIconMotion;
  icon: LucideIcon;
}

export const semanticIconMap = {
  brand: { icon: Activity, animation: 'pulse' },
  connected: { icon: Wifi, animation: 'wifi' },
  disconnected: { icon: WifiOff, animation: 'attention' },
  globalError: { icon: AlertCircle, animation: 'attention' },
  importFile: { icon: Upload, animation: 'upload' },
  openDirectory: { icon: FolderOpen, animation: 'folder' },
  pauseTask: { icon: Pause, animation: 'pause' },
  preflightFailed: { icon: X, animation: 'attention' },
  preflightPassed: { icon: Check, animation: 'draw' },
  refresh: { icon: RefreshCw, animation: 'refresh' },
  resumeTask: { icon: Play, animation: 'play' },
  settings: { icon: Settings, animation: 'rotate' },
  startDownload: { icon: Download, animation: 'download' },
  stopTask: { icon: CircleStop, animation: 'stop' },
  toastSuccess: { icon: CheckCircle2, animation: 'draw' },
} as const satisfies Record<string, SemanticIconDefinition>;

export type SemanticIconName = keyof typeof semanticIconMap;
