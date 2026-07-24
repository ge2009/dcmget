import { Dialog } from '@base-ui/react/dialog';
import { Menu } from '@base-ui/react/menu';
import {
  Moon, Sun, MonitorCog, Wrench, MoreHorizontal, Plus,
} from 'lucide-react';
import { AnimatePresence, motion } from 'motion/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiClient, ApiError, managedApiPath } from './api';
import { BrandMark } from './components/BrandMark';
import { Button, ConfirmDialog, StatusBadge } from './components/Primitives';
import { AnimatedIcon, semanticIconMap } from './components/icons';
import { LogPanel, normalizeLog, type NormalizedLog } from './components/LogPanel';
import { OperationsSheet } from './components/OperationsSheet';
import { ProfileEditor, type ProfileDraft } from './components/ProfileEditor';
import { ProfileRail } from './components/ProfileRail';
import { SettingsSheet } from './components/SettingsSheet';
import { TaskWorkspace } from './components/TaskWorkspace';
import { UpdateSheet } from './components/UpdateSheet';
import { normalizeStatus, parseAccessions, TERMINAL_STATUSES } from './domain';
import {
  BootstrapSchema, EventPageSchema, LogSchema, PreflightSchema, ProfileSchema,
  ProfilesResponseSchema, TaskSchema, UnknownRecordSchema, UpdateSchema,
  profileIssues, profileName, profileNumber,
  type Bootstrap, type Preflight, type Profile, type Task, type UnknownRecord, type UpdateState,
} from './schemas';

type Connection = 'connecting' | 'connected' | 'disconnected';
type ConfirmState = { title: string; description: string; label?: string; danger?: boolean; action: () => void | Promise<void> } | null;

const EMPTY_UPDATE: UpdateState = { supported: false, state: 'unavailable', message: '当前安装未配置更新服务' };

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === 'object' ? value as UnknownRecord : {};
}

function profileConfig(profile: Profile): UnknownRecord {
  return {
    pacs_server_ip: profile.pacs_server_ip || '', pacs_server_port: profile.pacs_server_port || 104,
    calling_ae_title: profile.calling_ae_title || 'DCMGET', pacs_ae_title: profile.pacs_ae_title || 'PACS',
    storage_ae_title: profile.storage_ae_title || 'DCMGET', storage_port: profile.storage_port || 6666,
    web_port: profile.web_port || 8787,
    dicom_destination_folder: profile.destination_directory || profile.dicom_destination_folder || '',
  };
}

function extractTask(payload: UnknownRecord): Task | null {
  const candidate = payload.task || payload.active_task || (payload.id ? payload : null);
  if (!candidate) return null;
  const parsed = TaskSchema.safeParse(candidate);
  return parsed.success ? parsed.data : null;
}

export default function App() {
  const [loading, setLoading] = useState(true);
  const [bootstrap, setBootstrap] = useState<Bootstrap>({});
  const [managerMode, setManagerMode] = useState(false);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profilesBusy, setProfilesBusy] = useState(false);
  const [activeProfile, setActiveProfile] = useState<Profile | null>(null);
  const [profileBootstrap, setProfileBootstrap] = useState<Bootstrap>({});
  const [config, setConfig] = useState<UnknownRecord>({});
  const [task, setTask] = useState<Task | null>(null);
  const [newTaskDraftOpen, setNewTaskDraftOpen] = useState(false);
  const [connection, setConnection] = useState<Connection>('connecting');
  const [connectionLabel, setConnectionLabel] = useState('正在连接');
  const [globalError, setGlobalError] = useState('');
  const [toast, setToast] = useState('');
  const [logs, setLogs] = useState<NormalizedLog[]>([]);
  const [detailedLogs, setDetailedLogs] = useState(false);
  const [accessionText, setAccessionText] = useState('');
  const parsed = useMemo(() => parseAccessions(accessionText), [accessionText]);
  const [destination, setDestination] = useState('');
  const [pdiEnabled, setPdiEnabled] = useState(false);
  const [pdiFolder, setPdiFolder] = useState('');
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [preflightSignature, setPreflightSignature] = useState('');
  const [preflightBusy, setPreflightBusy] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [settingsStatus, setSettingsStatus] = useState('');
  const [updateOpen, setUpdateOpen] = useState(false);
  const [update, setUpdate] = useState<UpdateState>(EMPTY_UPDATE);
  const [updateBusy, setUpdateBusy] = useState('');
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorProfile, setEditorProfile] = useState<Profile | null>(null);
  const [editorLaunch, setEditorLaunch] = useState(false);
  const [editorBusy, setEditorBusy] = useState(false);
  const [editorError, setEditorError] = useState('');
  const [operationsOpen, setOperationsOpen] = useState(false);
  const [operationsBusy, setOperationsBusy] = useState(false);
  const [health, setHealth] = useState<UnknownRecord>({});
  const [license, setLicense] = useState<UnknownRecord>({});
  const [releases, setReleases] = useState<UnknownRecord[]>([]);
  const [confirm, setConfirm] = useState<ConfirmState>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [directoryOpen, setDirectoryOpen] = useState(false);
  const [directoryPath, setDirectoryPath] = useState('');
  const [directories, setDirectories] = useState<Array<{ name: string; path: string }>>([]);
  const [directoryBusy, setDirectoryBusy] = useState(false);
  const [columnFile, setColumnFile] = useState<File | null>(null);
  const [columnOptions, setColumnOptions] = useState<UnknownRecord[]>([]);
  const [columnOpen, setColumnOpen] = useState(false);
  const [dark, setDark] = useState(document.documentElement.dataset.theme === 'dark');
  const generation = useRef(0);
  const profileAbort = useRef<AbortController | null>(null);
  const eventCursor = useRef('');
  const preflightRequest = useRef(0);
  const draftSignatureRef = useRef('');
  const newTaskBaseId = useRef('');
  const mounted = useRef(true);

  const activeNumber = profileNumber(activeProfile);
  const activeRunning = !managerMode || Boolean(activeProfile?.is_running);
  const localSession = (profileBootstrap.web as UnknownRecord | undefined)?.local_session !== false
    && (bootstrap.web as UnknownRecord | undefined)?.local_session !== false;
  const version = profileBootstrap.version || profileBootstrap.app_version || bootstrap.version || bootstrap.app_version || '—';

  const notify = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast((current) => current === message ? '' : current), 3200);
  }, []);

  const addLog = useCallback((entry: UnknownRecord) => {
    const parsedLog = LogSchema.safeParse(entry);
    if (!parsedLog.success) return;
    setLogs((current) => [...current, normalizeLog(parsedLog.data)].slice(-600));
  }, []);

  const applyTaskSnapshot = useCallback((next: Task | null) => {
    setTask(next);
    const nextId = next?.id == null ? '' : String(next.id);
    const nextStatus = normalizeStatus(next?.status);
    const draftCompatibleStatus = nextStatus === 'idle' || TERMINAL_STATUSES.has(nextStatus);
    if (next && (
      !draftCompatibleStatus
      || (newTaskBaseId.current && nextId !== newTaskBaseId.current)
      || (!newTaskBaseId.current && Boolean(nextId))
    )) {
      newTaskBaseId.current = '';
      setNewTaskDraftOpen(false);
    }
  }, []);

  const draft = useMemo(() => ({
    accessions: parsed.values,
    destination,
    pdi: { enabled: pdiEnabled, output_folder: pdiFolder.trim() },
  }), [parsed.values, destination, pdiEnabled, pdiFolder]);
  const draftSignature = useMemo(() => JSON.stringify({ profile: managerMode ? activeNumber : null, draft }), [activeNumber, draft, managerMode]);
  draftSignatureRef.current = draftSignature;

  const profileRequest = useCallback(async <T,>(path: string, schema: import('zod').ZodType<T>, options: { method?: string; body?: unknown; headers?: HeadersInit } = {}) => {
    if (!managerMode) return apiClient.request(path, schema, options);
    if (activeNumber == null) throw new ApiError('请先选择一个 Profile。', 409);
    const currentGeneration = generation.current;
    const result = await apiClient.request(managedApiPath(activeNumber, path), schema, {
      ...options,
      signal: profileAbort.current?.signal,
    });
    if (currentGeneration !== generation.current) throw new DOMException('Profile 已切换', 'AbortError');
    return result;
  }, [activeNumber, managerMode]);

  const applyProfilePayload = useCallback((payload: Bootstrap, profile?: Profile) => {
    apiClient.setCsrfToken(payload.csrf_token || payload.csrfToken);
    const mergedProfile = { ...(profile || {}), ...(payload.profile || {}) } as Profile;
    setProfileBootstrap({ ...payload, profile: mergedProfile });
    setActiveProfile(mergedProfile);
    const nextConfig = payload.config || profileConfig(mergedProfile);
    setConfig(nextConfig);
    setDestination(String(nextConfig.dicom_destination_folder || mergedProfile.destination_directory || mergedProfile.dicom_destination_folder || ''));
    setPdiEnabled(Boolean(nextConfig.pdi_export_enabled));
    setPdiFolder(String(nextConfig.pdi_output_folder || ''));
    const nextTask = payload.task || payload.active_task || null;
    newTaskBaseId.current = '';
    setNewTaskDraftOpen(false);
    applyTaskSnapshot(nextTask);
    const historicalErrors = Array.isArray(nextTask?.error_logs) ? nextTask.error_logs : [];
    setLogs(historicalErrors.flatMap((entry) => {
      const parsedLog = LogSchema.safeParse(entry);
      return parsedLog.success ? [normalizeLog(parsedLog.data)] : [];
    }).slice(-600));
    setLicense(asRecord(payload.license));
    if (payload.update) setUpdate(payload.update);
    setConnection('connected');
    setConnectionLabel('后台已连接');
  }, [applyTaskSnapshot]);

  const selectProfile = useCallback(async (profile: Profile) => {
    const number = profileNumber(profile);
    if (number == null) return;
    profileAbort.current?.abort();
    profileAbort.current = new AbortController();
    const currentGeneration = ++generation.current;
    eventCursor.current = '';
    setActiveProfile(profile);
    setTask(null);
    newTaskBaseId.current = '';
    setNewTaskDraftOpen(false);
    setLogs([]);
    setHealth({});
    setLicense({});
    const summaryConfig = profileConfig(profile);
    setConfig(summaryConfig);
    setDestination(String(summaryConfig.dicom_destination_folder || ''));
    setPdiEnabled(false);
    setPdiFolder('');
    setPreflight(null);
    setPreflightSignature('');
    setGlobalError('');
    if (!profile.is_running) {
      setProfileBootstrap({ profile });
      setConnection('connected');
      setConnectionLabel(profile.desired_running ? 'Profile 正在启动' : 'Profile 未启动');
      return;
    }
    setConnection('connecting');
    setConnectionLabel('正在载入 Profile');
    try {
      const payload = await apiClient.request(managedApiPath(number, '/api/bootstrap'), BootstrapSchema, { signal: profileAbort.current.signal });
      if (currentGeneration !== generation.current) return;
      applyProfilePayload(payload, profile);
    } catch (error) {
      if ((error as Error).name === 'AbortError') return;
      setConnection('disconnected');
      setConnectionLabel('Profile 载入失败');
      setGlobalError((error as Error).message);
    }
  }, [applyProfilePayload]);

  const refreshProfiles = useCallback(async (selectIfNeeded = true) => {
    if (!managerMode && !selectIfNeeded) return;
    setProfilesBusy(true);
    try {
      const result = await apiClient.request('/api/management/profiles', ProfilesResponseSchema);
      const list = Array.isArray(result) ? result : result.profiles || result.items || [];
      setProfiles(list);
      const latestActive = list.find((profile) => profileNumber(profile) === activeNumber);
      if (latestActive) {
        const changed = Boolean(latestActive.is_running) !== Boolean(activeProfile?.is_running);
        if (changed) await selectProfile(latestActive);
        else setActiveProfile(latestActive);
      } else if (selectIfNeeded && list.length) {
        await selectProfile(list.find((profile) => profile.is_running) || list.find((profile) => profile.desired_running) || list[0]);
      } else if (!list.length) {
        setActiveProfile(null); setTask(null); newTaskBaseId.current = ''; setNewTaskDraftOpen(false); setConfig({});
      }
    } catch (error) {
      setGlobalError(`Profile 列表读取失败：${(error as Error).message}`);
    } finally {
      setProfilesBusy(false);
    }
  }, [activeNumber, activeProfile?.is_running, managerMode, selectProfile]);

  useEffect(() => {
    mounted.current = true;
    (async () => {
      try {
        const payload = await apiClient.request('/api/bootstrap', BootstrapSchema);
        if (!mounted.current) return;
        setBootstrap(payload);
        apiClient.setCsrfToken(payload.csrf_token || payload.csrfToken);
        const isManager = payload.mode === 'manager' || payload.profile?.mode === 'manager';
        setManagerMode(isManager);
        setUpdate(payload.update || EMPTY_UPDATE);
        if (isManager) {
          setConnection('connected'); setConnectionLabel('管理中心已连接');
          const result = await apiClient.request('/api/management/profiles', ProfilesResponseSchema);
          const list = Array.isArray(result) ? result : result.profiles || result.items || [];
          if (!mounted.current) return;
          setProfiles(list);
          const first = list.find((profile) => profile.is_running) || list.find((profile) => profile.desired_running) || list[0];
          if (first) await selectProfile(first);
        } else {
          applyProfilePayload(payload, payload.profile);
        }
      } catch (error) {
        setConnection('disconnected'); setConnectionLabel('后台连接失败');
        setGlobalError(`无法连接 DcmGet 后台：${(error as Error).message}`);
      } finally {
        if (mounted.current) setLoading(false);
      }
    })();
    return () => { mounted.current = false; profileAbort.current?.abort(); };
  }, []); // bootstrap exactly once

  const consumeEvent = useCallback((type: string, data: unknown) => {
    const record = asRecord(data);
    if (['task', 'task_started', 'state', 'progress', 'task_state', 'task_progress', 'pdi_progress', 'pdi_finished', 'verification_progress'].includes(type)) {
      // Manager events are deltas; replacing the authoritative task with one of
      // them would temporarily remove actions and result rows. Direct SSE wraps
      // its live snapshot in `task`, while the manager poll refreshes `/api/task`.
      const snapshot = record.task || (type === 'task' ? record : null);
      if (snapshot && typeof snapshot === 'object') {
        const next = extractTask({ task: snapshot });
        if (next) applyTaskSnapshot(next);
      }
    } else if (type === 'log' || type === 'receiver') addLog(record);
    else if (type === 'config') setConfig(asRecord(record.config || record));
    else if (type === 'update') {
      const parsedUpdate = UpdateSchema.safeParse(record);
      if (parsedUpdate.success) setUpdate(parsedUpdate.data);
    }
  }, [addLog, applyTaskSnapshot]);

  useEffect(() => {
    if (loading || managerMode || !activeRunning) return;
    const source = new EventSource('/api/events/stream', { withCredentials: true });
    source.onopen = () => { setConnection('connected'); setConnectionLabel('后台已连接'); };
    source.onerror = () => { setConnection('disconnected'); setConnectionLabel('连接中断，自动重连'); };
    const handler = (event: MessageEvent) => {
      try {
        const payload = JSON.parse(event.data) as UnknownRecord;
        consumeEvent(String(payload.type || event.type), payload.payload ?? payload.data ?? payload);
      } catch { if (event.data) addLog({ level: 'INFO', source: '后台', message: event.data }); }
    };
    source.onmessage = handler;
    ['task', 'task_started', 'state', 'progress', 'pdi_progress', 'pdi_finished', 'verification_progress', 'verification_finished', 'log', 'health', 'config', 'license', 'receiver']
      .forEach((type) => source.addEventListener(type, handler as EventListener));
    return () => source.close();
  }, [activeRunning, addLog, consumeEvent, loading, managerMode]);

  useEffect(() => {
    if (!managerMode || activeNumber == null || !activeRunning) return;
    let cancelled = false;
    let timer = 0;
    const poll = async () => {
      try {
        const query = eventCursor.current ? `?after_id=${encodeURIComponent(eventCursor.current)}` : '';
        const result = await profileRequest(`/api/events${query}`, EventPageSchema);
        if (cancelled) return;
        const events = result.events || result.items || result.records || [];
        for (const entry of events) {
          const type = String(entry.type || entry.event_type || entry.name || 'message');
          if (entry.id != null) eventCursor.current = String(entry.id);
          consumeEvent(type, entry.payload ?? entry.data ?? entry);
        }
        if (events.some((entry) => [
          'task', 'task_started', 'state', 'progress', 'pdi_progress', 'pdi_finished',
          'verification_progress', 'verification_finished', 'task_ended',
        ].includes(String(entry.type || entry.event_type || entry.name || '')))) {
          const current = await profileRequest('/api/task', UnknownRecordSchema);
          if (cancelled) return;
          const next = extractTask(current);
          if (next) applyTaskSnapshot(next);
        }
        if (result.last_id != null) eventCursor.current = String(result.last_id);
        setConnection('connected'); setConnectionLabel('当前 Profile 已同步');
        timer = window.setTimeout(poll, events.length ? 350 : 1500);
      } catch (error) {
        if (cancelled || (error as Error).name === 'AbortError') return;
        setConnection('disconnected'); setConnectionLabel('同步中断，自动重试');
        timer = window.setTimeout(poll, 2500);
      }
    };
    poll();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [activeNumber, activeRunning, applyTaskSnapshot, consumeEvent, managerMode, profileRequest]);

  const refreshSnapshot = useCallback(async () => {
    if (!activeRunning || (managerMode && activeNumber == null)) return;
    try {
      const result = managerMode
        ? await profileRequest('/api/task', UnknownRecordSchema)
        : await apiClient.request('/api/task', UnknownRecordSchema);
      const next = extractTask(result);
      if (next) applyTaskSnapshot(next);
    } catch (error) { if ((error as ApiError).status !== 404 && (error as Error).name !== 'AbortError') console.warn(error); }
  }, [activeNumber, activeRunning, applyTaskSnapshot, managerMode, profileRequest]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.hidden) return;
      if (managerMode) refreshProfiles(false);
      if (activeRunning) refreshSnapshot();
    }, 15000);
    return () => window.clearInterval(timer);
  }, [activeRunning, managerMode, refreshProfiles, refreshSnapshot]);

  useEffect(() => {
    setPreflight(null); setPreflightSignature('');
    if (!activeRunning || !destination.trim()) return;
    const timer = window.setTimeout(() => runPreflight(true), 500);
    return () => window.clearTimeout(timer);
  }, [draftSignature, activeRunning]);

  async function runPreflight(silent = false) {
    if (!destination.trim()) return false;
    const signature = draftSignature;
    const requestId = ++preflightRequest.current;
    setPreflightBusy(true);
    try {
      const result = await profileRequest('/api/preflight', PreflightSchema, { method: 'POST', body: draft });
      if (requestId !== preflightRequest.current || signature !== draftSignatureRef.current) return false;
      setPreflight(result); setPreflightSignature(result.ok ? signature : '');
      if (!result.ok && !silent) setGlobalError(result.message || '部分启动检查未通过。');
      return result.ok === true;
    } catch (error) {
      if ((error as Error).name === 'AbortError') return false;
      if (requestId !== preflightRequest.current || signature !== draftSignatureRef.current) return false;
      setPreflight({ ok: false, message: (error as Error).message });
      if (!silent) setGlobalError((error as Error).message);
      return false;
    } finally { if (requestId === preflightRequest.current) setPreflightBusy(false); }
  }

  async function startTask() {
    if (preflightSignature !== draftSignature || !preflight?.ok) {
      if (!await runPreflight()) return;
    }
    setConfirm({
      title: '确认开始下载', danger: false, label: '确认并开始',
      description: `将向 ${String(config.pacs_server_ip || 'PACS')}:${String(config.pacs_server_port || '—')} 提交 ${parsed.values.length.toLocaleString()} 个检查号。\n保存目录：${destination}`,
      action: async () => {
        setActionBusy(true);
        try {
          const result = await profileRequest('/api/task/start', UnknownRecordSchema, { method: 'POST', body: draft });
          newTaskBaseId.current = ''; setNewTaskDraftOpen(false); applyTaskSnapshot(extractTask(result)); notify('任务已交给后台执行，关闭页面不会停止下载。'); setConfirm(null);
        } catch (error) { setGlobalError((error as Error).message); }
        finally { setActionBusy(false); }
      },
    });
  }

  async function executeTaskAction(action: string) {
    const irreversible = ['cancel', 'end', 'accept-partial'].includes(action);
    if (irreversible) {
      const copy = action === 'end'
        ? ['结束当前任务', '将永久结束当前恢复点；已下载文件保留，但任务不能再继续。', '确认结束']
        : action === 'cancel'
          ? ['停止当前执行', '将停止当前 movescu；已收到文件和恢复点会保留。', '确认停止']
          : ['接受已有文件', '将把当前任务作为部分结果结束，未完成检查号不会继续下载。', '确认接受'];
      setConfirm({ title: copy[0], description: copy[1], label: copy[2], action: () => runTaskAction(action) });
      return;
    }
    await runTaskAction(action);
  }

  async function runTaskAction(action: string) {
    setActionBusy(true);
    try {
      const result = await profileRequest(`/api/task/${action}`, UnknownRecordSchema, { method: 'POST', body: {} });
      applyTaskSnapshot(extractTask(result)); notify('任务状态已更新'); setConfirm(null);
    } catch (error) { setGlobalError((error as Error).message); }
    finally { setActionBusy(false); }
  }

  async function importFile(file: File) {
    try {
      await uploadAccessionFile(file);
    } catch (error) { setGlobalError(`导入失败：${(error as Error).message}`); }
  }

  async function uploadAccessionFile(file: File, column?: number) {
    try {
      const query = column == null ? '' : `?column=${column}`;
      const result = await profileRequest(`/api/files/accessions${query}`, UnknownRecordSchema, {
        method: 'POST', headers: { 'Content-Type': file.type || 'application/octet-stream', 'X-File-Name': encodeURIComponent(file.name) },
        body: file,
      });
      const values = Array.isArray(result.accessions) ? result.accessions : Array.isArray(result.values) ? result.values : [];
      setAccessionText(values.map(String).join('\n')); notify(`已导入 ${values.length.toLocaleString()} 个检查号`);
      setColumnOpen(false); setColumnFile(null); setColumnOptions([]);
    } catch (error) {
      const apiError = error as ApiError;
      const payload = asRecord(apiError.details);
      const detail = asRecord(payload.detail);
      const columns = Array.isArray(detail.columns) ? detail.columns : Array.isArray(payload.columns) ? payload.columns : [];
      if (apiError.status === 422 && columns.length) {
        setColumnFile(file); setColumnOptions(columns.map((item) => asRecord(item))); setColumnOpen(true); return;
      }
      throw error;
    }
  }

  async function profileStart(profile: Profile) {
    if (profileIssues(profile).length) { openProfileEditor(profile, true); return; }
    const number = profileNumber(profile); if (number == null) return;
    try {
      await apiClient.request(`/api/management/profiles/${number}/start`, UnknownRecordSchema, { method: 'POST', body: {} });
      notify(`${profileName(profile)} 启动命令已提交`); window.setTimeout(() => refreshProfiles(false), 700);
    } catch (error) { setGlobalError((error as Error).message); }
  }

  function profileStop(profile: Profile, after?: () => void) {
    const number = profileNumber(profile); if (number == null) return;
    setConfirm({
      title: '停止当前 Profile', label: '确认停止',
      description: `将停止 ${profileName(profile)} 的接收端、下载进程与 PDI 子任务；已下载文件和恢复点会保留。`,
      action: async () => {
        setConfirmBusy(true);
        try {
          await apiClient.request(`/api/management/profiles/${number}/stop`, UnknownRecordSchema, { method: 'POST', body: {} });
          setConfirm(null); notify(`${profileName(profile)} 已停止`); await refreshProfiles(false); after?.();
        } catch (error) { setGlobalError((error as Error).message); }
        finally { setConfirmBusy(false); }
      },
    });
  }

  function openProfileEditor(profile: Profile, launch = false) {
    if (profile.is_running || profile.desired_running) {
      profileStop(profile, () => { setEditorProfile({ ...profile, is_running: false, desired_running: false }); setEditorLaunch(launch); setEditorOpen(true); });
      return;
    }
    setEditorProfile(profile); setEditorLaunch(launch); setEditorError(''); setEditorOpen(true);
  }

  async function createProfile() {
    try {
      const result = await apiClient.request('/api/management/profiles', UnknownRecordSchema, { method: 'POST', body: {} });
      await refreshProfiles(false);
      const parsedProfile = ProfileSchema.safeParse(result.profile);
      if (parsedProfile.success) openProfileEditor(parsedProfile.data, false);
      notify('新 Profile 已创建，请补充配置。');
    } catch (error) { setGlobalError((error as Error).message); }
  }

  async function saveProfile(draftProfile: ProfileDraft, launch: boolean) {
    setEditorBusy(true); setEditorError('');
    try {
      await apiClient.request('/api/operations/profile-update', UnknownRecordSchema, { method: 'POST', body: draftProfile });
      if (launch) await apiClient.request('/api/operations/profile-launch', UnknownRecordSchema, { method: 'POST', body: { profile_number: draftProfile.profile_number } });
      setEditorOpen(false); notify(launch ? 'Profile 已保存并启动' : 'Profile 配置已保存'); await refreshProfiles(false);
    } catch (error) { setEditorError((error as Error).message); }
    finally { setEditorBusy(false); }
  }

  async function cloneProfile(profile: Profile) {
    const name = window.prompt('新 Profile 显示名称', `${profileName(profile)} 副本`); if (name == null) return;
    try {
      await apiClient.request('/api/operations/profile-clone', UnknownRecordSchema, { method: 'POST', body: { source_profile_number: profileNumber(profile), display_name: name } });
      notify('Profile 已复制'); await refreshProfiles(false);
    } catch (error) { setGlobalError((error as Error).message); }
  }

  function deleteProfile(profile: Profile) {
    setConfirm({ title: '删除 Profile', description: `将删除“${profileName(profile)}”的配置，但不会删除影像、PDI 或日志。`, label: '确认删除', action: async () => {
      try {
        await apiClient.request('/api/operations/profile-delete', UnknownRecordSchema, { method: 'POST', body: { profile_number: profileNumber(profile) } });
        setConfirm(null); notify('Profile 配置已删除'); await refreshProfiles(false);
      } catch (error) { setGlobalError((error as Error).message); }
    } });
  }

  function openSettings() {
    if (managerMode) {
      if (activeProfile && activeRunning) { setSettingsStatus(''); setSettingsOpen(true); }
      else if (activeProfile) openProfileEditor(activeProfile, false);
    } else { setSettingsStatus(''); setSettingsOpen(true); }
  }

  async function saveSettings(payload: UnknownRecord) {
    setSettingsBusy(true); setSettingsStatus('正在保存…');
    try {
      const result = await profileRequest('/api/config', UnknownRecordSchema, { method: 'PUT', body: payload });
      const next = asRecord(result.config || result); setConfig(next); setSettingsStatus(String(result.message || '设置已保存'));
      notify('设置已保存');
    } catch (error) { setSettingsStatus((error as Error).message); }
    finally { setSettingsBusy(false); }
  }

  async function updateAction(action: 'check' | 'download' | 'apply' | 'policy', body: unknown = {}) {
    if (action === 'apply') {
      setConfirm({
        title: '安装软件更新',
        description: '安装期间将停止当前管理中心和正在运行的 Profile，Web 页面会暂时断开；下载任务的恢复点和已有文件会保留。',
        label: '确认安装并重启',
        action: async () => { setConfirm(null); await performUpdateAction(action, body); },
      });
      return;
    }
    await performUpdateAction(action, body);
  }

  async function performUpdateAction(action: 'check' | 'download' | 'apply' | 'policy', body: unknown = {}) {
    setUpdateBusy(action);
    try {
      const method = action === 'policy' ? 'PUT' : 'POST';
      const result = await apiClient.request(`/api/update/${action}`, UpdateSchema, { method, body });
      setUpdate(result); notify(action === 'apply' ? '更新安装已提交，管理中心将短暂断开。' : '更新状态已刷新');
    } catch (error) { setGlobalError((error as Error).message); }
    finally { setUpdateBusy(''); }
  }

  async function pdiAction(action: 'open' | 'verify' | 'retry') {
    try {
      const result = await profileRequest(`/api/pdi/${action}`, UnknownRecordSchema, { method: 'POST', body: { task_id: task?.id } });
      const next = extractTask(result); if (next) applyTaskSnapshot(next);
      notify(String(result.message || (action === 'open' ? '已在 DcmGet 主机打开 PDI' : action === 'verify' ? 'PDI 校验已启动' : '已重新加入 PDI 导出队列')));
    } catch (error) { setGlobalError((error as Error).message); }
  }

  async function loadOperations() {
    setOperationsBusy(true);
    try {
      const jobs: Promise<void>[] = [];
      if (!activeRunning) setHealth({});
      if (activeRunning) {
        jobs.push(profileRequest('/api/operations/health', UnknownRecordSchema, { method: 'POST', body: {} }).then(setHealth));
        jobs.push(profileRequest('/api/license', UnknownRecordSchema).then((result) => setLicense({ ...asRecord(result.license), machine_code: result.machine_code })));
      }
      jobs.push(apiClient.request('/api/operations/release-notes', UnknownRecordSchema).then((result) => setReleases(Array.isArray(result.releases) ? result.releases.map((item) => asRecord(item)) : [])));
      await Promise.allSettled(jobs);
    } finally { setOperationsBusy(false); }
  }

  async function activateLicense(token: string) {
    setOperationsBusy(true);
    try {
      const result = await profileRequest('/api/license/activate', UnknownRecordSchema, { method: 'POST', body: { token } });
      setLicense({ ...asRecord(result.license), machine_code: result.machine_code }); notify('软件授权已激活');
    } catch (error) { setGlobalError((error as Error).message); }
    finally { setOperationsBusy(false); }
  }

  async function openOperations() {
    setOperationsOpen(true);
    await loadOperations();
  }

  async function runOperation(name: string) {
    try {
      const path = managerMode && activeNumber != null ? managedApiPath(activeNumber, `/api/operations/${name}`) : `/api/operations/${name}`;
      const result = await apiClient.request(path, UnknownRecordSchema, { method: 'POST', body: {} });
      notify(String(result.message || '操作已完成'));
      if (typeof result.download_url === 'string') window.location.assign(result.download_url);
    } catch (error) { setGlobalError((error as Error).message); }
  }

  async function browseDirectory(path = destination) {
    setDirectoryOpen(true); setDirectoryBusy(true);
    const query = new URLSearchParams({ purpose: 'destination' }); if (path) query.set('path', path);
    try {
      const result = await profileRequest(`/api/files/directories?${query}`, UnknownRecordSchema);
      setDirectoryPath(String(result.path || result.current || path || ''));
      const list = Array.isArray(result.directories) ? result.directories : Array.isArray(result.items) ? result.items : Array.isArray(result.children) ? result.children : [];
      setDirectories(list.map((item) => typeof item === 'string' ? { path: item, name: item.split(/[\\/]/).filter(Boolean).at(-1) || item } : { path: String(item.path || ''), name: String(item.name || item.path || '') }));
    } catch (error) { setGlobalError((error as Error).message); }
    finally { setDirectoryBusy(false); }
  }

  function resetTask() {
    newTaskBaseId.current = task?.id == null ? '' : String(task.id);
    setNewTaskDraftOpen(true); setAccessionText('');
    setPdiEnabled(Boolean(config.pdi_export_enabled));
    setPdiFolder(String(config.pdi_output_folder || ''));
    setPreflight(null); setPreflightSignature('');
  }

  function toggleTheme() {
    const next = !dark; setDark(next); document.documentElement.dataset.theme = next ? 'dark' : 'light';
    try { localStorage.setItem('dcmget-theme', next ? 'dark' : 'light'); } catch { /* session-only */ }
  }

  if (loading) return <div className="boot-screen"><BrandMark className="boot-mark" /><strong>正在启动 DcmGet</strong><span>正在读取实例与任务状态…</span></div>;

  const issues = profileIssues(activeProfile);
  const configuredPacs = `${String(config.pacs_server_ip || activeProfile?.pacs_server_ip || '—')}:${String(config.pacs_server_port || activeProfile?.pacs_server_port || '—')}`;
  const receiver = `${String(config.storage_ae_title || activeProfile?.storage_ae_title || '—')}:${String(config.storage_port || activeProfile?.storage_port || '—')}`;
  const visibleProfile = activeProfile || profileBootstrap.profile || null;
  const visibleName = visibleProfile ? profileName(visibleProfile) : managerMode ? '请选择 Profile' : '影像下载';
  const canOpenNewTask = activeRunning && task?.actions?.can_start === true && normalizeStatus(task.status) !== 'idle';
  const runtimeTone = issues.length ? 'error' : activeRunning ? 'success' : activeProfile?.desired_running ? 'working' : 'neutral';
  const runtimeLabel = issues.length ? '配置异常' : activeRunning ? '实例运行中' : activeProfile?.desired_running ? '正在启动' : '实例未启动';

  return <div className={`app-shell ${managerMode ? 'is-manager' : 'is-profile'}`}>
    {managerMode && <ProfileRail profiles={profiles} selectedNumber={activeNumber} loading={profilesBusy} onRefresh={() => refreshProfiles(false)} onCreate={createProfile} onSelect={selectProfile} onStart={profileStart} onStop={profileStop} onEdit={(profile) => openProfileEditor(profile)} onClone={cloneProfile} onDelete={deleteProfile} />}
    <main className="workspace">
      <header className="app-header">
        <div className="app-header__identity">
          {!managerMode && <div className="compact-brand"><BrandMark /><strong>DcmGet</strong></div>}
          <div className="profile-context">
            <h1>{visibleName}</h1>
            <div><StatusBadge tone={runtimeTone}>{runtimeLabel}</StatusBadge><span>Profile {activeNumber == null ? '—' : String(activeNumber).padStart(2, '0')}</span></div>
          </div>
        </div>
        <div className="app-header__actions">
          <span className="connection-state" data-state={connection} title={connectionLabel} role="status" aria-live="polite">
            {connection === 'connected'
              ? <AnimatedIcon {...semanticIconMap.connected} statusKey={connection} size={15} />
              : connection === 'disconnected'
                ? <AnimatedIcon {...semanticIconMap.disconnected} statusKey={connection} size={15} />
                : <AnimatedIcon {...semanticIconMap.refresh} statusKey={connection} size={15} className="spin" />}
            <span>{connectionLabel}</span>
          </span>
          {canOpenNewTask && <Button variant="primary" size="small" onClick={resetTask}><Plus size={16} />创建任务</Button>}
          {managerMode && activeProfile && (!activeRunning
            ? <Button variant="primary" size="small" onClick={() => profileStart(activeProfile)}><AnimatedIcon {...semanticIconMap.resumeTask} size={16} />启动实例</Button>
            : <Button variant="secondary" size="small" onClick={() => profileStop(activeProfile)}><AnimatedIcon {...semanticIconMap.stopTask} size={16} />停止实例</Button>)}
          <Menu.Root>
            <Menu.Trigger className="icon-button" aria-label="打开工作台菜单"><MoreHorizontal size={19} /></Menu.Trigger>
            <Menu.Portal>
              <Menu.Positioner className="menu-positioner" sideOffset={8} align="end">
                <Menu.Popup className="app-menu">
                  <Menu.Item className="app-menu__item" onClick={openSettings} disabled={!activeProfile}><AnimatedIcon {...semanticIconMap.settings} size={16} /><span>当前实例设置</span></Menu.Item>
                  {managerMode && <Menu.Item className="app-menu__item" onClick={() => setUpdateOpen(true)}><MonitorCog size={16} /><span>软件更新</span></Menu.Item>}
                  <Menu.Item className="app-menu__item" onClick={openOperations}><Wrench size={16} /><span>维护与支持</span></Menu.Item>
                  <Menu.Separator className="app-menu__separator" />
                  <Menu.Item className="app-menu__item" onClick={toggleTheme}>{dark ? <Sun size={16} /> : <Moon size={16} />}<span>{dark ? '切换浅色主题' : '切换深色主题'}</span></Menu.Item>
                </Menu.Popup>
              </Menu.Positioner>
            </Menu.Portal>
          </Menu.Root>
        </div>
      </header>

      <div className="workspace-scroll">
        {visibleProfile && <section className="context-strip" aria-label="当前连接信息">
          <div><span>PACS</span><strong>{configuredPacs}</strong></div>
          <div><span>接收端</span><strong>{receiver}</strong></div>
          <div><span>版本</span><strong>{version}</strong></div>
          {issues.length > 0 && <button type="button" onClick={() => activeProfile && openProfileEditor(activeProfile)}><AnimatedIcon {...semanticIconMap.globalError} size={15} />{issues[0]}</button>}
        </section>}

        <AnimatePresence>{globalError && <motion.div className="global-alert" role="alert" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}><AnimatedIcon {...semanticIconMap.globalError} size={18} /><div><strong>需要处理</strong><p>{globalError}</p></div><button onClick={() => setGlobalError('')} aria-label="关闭">×</button></motion.div>}</AnimatePresence>

        {!activeProfile && managerMode
          ? <section className="workspace-empty"><BrandMark className="workspace-empty__brand" /><h2>创建第一个接收实例</h2><p>每个实例拥有独立的 PACS、AE、接收端口和保存目录。</p><Button variant="primary" onClick={createProfile}><Plus size={17} />新建 Profile</Button></section>
          : !activeRunning
            ? <section className="workspace-empty"><span><AnimatedIcon {...semanticIconMap.stopTask} size={26} /></span><h2>{activeProfile?.desired_running ? '实例正在启动' : '当前实例未启动'}</h2><p>启动实例后，DICOM 接收端和任务控制将在这里就绪。</p>{activeProfile && <div><Button variant="primary" onClick={() => profileStart(activeProfile)}><AnimatedIcon {...semanticIconMap.resumeTask} size={17} />启动当前实例</Button><Button onClick={() => openProfileEditor(activeProfile)}><AnimatedIcon {...semanticIconMap.settings} size={17} />启动参数</Button></div>}</section>
            : <>
                <TaskWorkspace available={activeRunning} task={newTaskDraftOpen ? null : task} accessionText={accessionText} parsed={parsed} destination={destination} pdiEnabled={pdiEnabled} pdiFolder={pdiFolder} preflight={preflight} preflightSignatureMatches={preflightSignature === draftSignature} preflightBusy={preflightBusy} actionBusy={actionBusy} onAccessionTextChange={setAccessionText} onDestinationChange={setDestination} onPdiEnabledChange={setPdiEnabled} onPdiFolderChange={setPdiFolder} onImport={importFile} onBrowse={() => browseDirectory()} onPreflight={() => runPreflight()} onStart={startTask} onTaskAction={executeTaskAction} onPdiAction={pdiAction} onNewTask={resetTask} onOpenDestination={() => runOperation('open-destination')} />
                <LogPanel logs={logs} detailed={detailedLogs} onDetailedChange={setDetailedLogs} onClear={() => setLogs([])} onOpenDirectory={() => runOperation('open-log-directory')} />
              </>}
      </div>
    </main>

    <SettingsSheet open={settingsOpen} onOpenChange={setSettingsOpen} config={config} busy={settingsBusy} status={settingsStatus} topologyLocked={managerMode && activeRunning} onSave={saveSettings} />
    <UpdateSheet open={updateOpen} onOpenChange={setUpdateOpen} update={update} busy={updateBusy} localSession={localSession} onAction={updateAction} />
    <OperationsSheet open={operationsOpen} onOpenChange={setOperationsOpen} health={health} license={license} releases={releases} busy={operationsBusy} onRefresh={loadOperations} onActivate={activateLicense} onOperation={runOperation} />
    <ProfileEditor open={editorOpen} onOpenChange={setEditorOpen} profile={editorProfile} launchAfterSave={editorLaunch} busy={editorBusy} error={editorError} onSave={saveProfile} />
    <ConfirmDialog open={Boolean(confirm)} onOpenChange={(open) => !open && setConfirm(null)} title={confirm?.title || ''} description={confirm?.description || ''} confirmLabel={confirm?.label} danger={confirm?.danger !== false} busy={confirmBusy || actionBusy} onConfirm={async () => { if (!confirm) return; setConfirmBusy(true); try { await confirm.action(); } finally { setConfirmBusy(false); } }} />

    <Dialog.Root open={directoryOpen} onOpenChange={setDirectoryOpen}>
      <Dialog.Portal>
        <Dialog.Backdrop className="dialog-backdrop" />
        <Dialog.Viewport className="dialog-viewport dialog-viewport--center">
          <Dialog.Popup className="directory-dialog">
            <header>
              <div><p className="eyebrow">HOST FILESYSTEM</p><Dialog.Title>选择 DICOM 保存目录</Dialog.Title></div>
              <Dialog.Close className="icon-button" aria-label="关闭目录选择">×</Dialog.Close>
            </header>
            <div className="directory-current">
              <input aria-label="当前目录路径" value={directoryPath} onChange={(event) => setDirectoryPath(event.target.value)} />
              <Button onClick={() => browseDirectory(directoryPath)} disabled={directoryBusy}>{directoryBusy ? '读取中…' : '转到'}</Button>
            </div>
            <div className="directory-list">
              {directories.length
                ? directories.map((item) => <button key={item.path} type="button" onDoubleClick={() => browseDirectory(item.path)} onClick={() => setDirectoryPath(item.path)} aria-pressed={item.path === directoryPath}><AnimatedIcon {...semanticIconMap.openDirectory} size={17} /><span>{item.name}</span></button>)
                : <p>此目录没有可显示的子目录</p>}
            </div>
            <footer>
              <Dialog.Close className="button button--secondary button--normal">取消</Dialog.Close>
              <Button variant="primary" onClick={() => { setDestination(directoryPath); setDirectoryOpen(false); }}>选择当前目录</Button>
            </footer>
          </Dialog.Popup>
        </Dialog.Viewport>
      </Dialog.Portal>
    </Dialog.Root>
    <Dialog.Root open={columnOpen} onOpenChange={setColumnOpen}><Dialog.Portal><Dialog.Backdrop className="dialog-backdrop" /><Dialog.Viewport className="dialog-viewport dialog-viewport--center"><Dialog.Popup className="confirm-dialog"><Dialog.Title className="sheet__title">选择检查号所在列</Dialog.Title><Dialog.Description className="confirm-dialog__description">文件包含多个可用列，请明确选择后继续导入。</Dialog.Description><div className="column-options">{columnOptions.map((column, index) => <button key={String(column.index ?? index)} onClick={() => columnFile && uploadAccessionFile(columnFile, Number(column.index ?? index))}><strong>{String(column.name || column.label || `第 ${index + 1} 列`)}</strong><small>列序号 {String(column.index ?? index)}</small></button>)}</div><div className="confirm-dialog__actions"><Dialog.Close className="button button--secondary button--normal">取消</Dialog.Close></div></Dialog.Popup></Dialog.Viewport></Dialog.Portal></Dialog.Root>
    <AnimatePresence>{toast && <motion.div className="toast" role="status" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}><AnimatedIcon {...semanticIconMap.toastSuccess} size={18} />{toast}</motion.div>}</AnimatePresence>
  </div>;
}
