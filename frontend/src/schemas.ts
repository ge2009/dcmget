import { z } from 'zod';

export const UnknownRecordSchema = z.looseObject({});
export type UnknownRecord = z.infer<typeof UnknownRecordSchema>;

export const ProfileSchema = z.looseObject({
  number: z.coerce.number().int().optional(),
  profile_number: z.coerce.number().int().optional(),
  id: z.union([z.string(), z.number()]).optional(),
  display_name: z.string().optional(),
  name: z.string().optional(),
  mode: z.string().optional(),
  is_running: z.boolean().optional(),
  desired_running: z.boolean().optional(),
  issues: z.array(z.unknown()).optional(),
  validation_errors: z.array(z.unknown()).optional(),
  last_error: z.string().optional(),
  pacs_server_ip: z.string().optional(),
  pacs_server_port: z.coerce.number().optional(),
  calling_ae_title: z.string().optional(),
  pacs_ae_title: z.string().optional(),
  storage_ae_title: z.string().optional(),
  storage_port: z.coerce.number().optional(),
  web_port: z.coerce.number().optional(),
  destination_directory: z.string().optional(),
  dicom_destination_folder: z.string().optional(),
  data_dir: z.string().optional(),
});
export type Profile = z.infer<typeof ProfileSchema>;

export const TaskItemSchema = z.looseObject({
  accession: z.string().optional(),
  accession_number: z.string().optional(),
  value: z.string().optional(),
  status: z.string().optional(),
  file_count: z.coerce.number().optional(),
  files: z.coerce.number().optional(),
  elapsed_seconds: z.coerce.number().optional(),
  duration_seconds: z.coerce.number().optional(),
  error_summary: z.string().optional(),
  message: z.string().optional(),
  detail: z.string().optional(),
});
export type TaskItem = z.infer<typeof TaskItemSchema>;

export const TaskSchema = z.looseObject({
  id: z.union([z.string(), z.number()]).optional(),
  status: z.string().default('idle'),
  message: z.string().optional(),
  detail: z.string().optional(),
  current_accession: z.string().optional(),
  total: z.coerce.number().optional(),
  total_count: z.coerce.number().optional(),
  accession_count: z.coerce.number().optional(),
  processed: z.coerce.number().optional(),
  processed_count: z.coerce.number().optional(),
  finished_count: z.coerce.number().optional(),
  file_count: z.coerce.number().optional(),
  received_files: z.coerce.number().optional(),
  files: z.coerce.number().optional(),
  speed_bytes_per_second: z.coerce.number().optional(),
  speed_bps: z.coerce.number().optional(),
  elapsed_seconds: z.coerce.number().optional(),
  accessions: z.array(z.unknown()).optional(),
  items: z.array(TaskItemSchema).optional(),
  results: z.array(TaskItemSchema).optional(),
  summary: UnknownRecordSchema.optional(),
  status_counts: UnknownRecordSchema.optional(),
  actions: UnknownRecordSchema.optional(),
  pdi: UnknownRecordSchema.nullable().optional(),
  pdi_result: UnknownRecordSchema.nullable().optional(),
});
export type Task = z.infer<typeof TaskSchema>;

export const UpdateSchema = z.looseObject({
  supported: z.boolean().optional(),
  state: z.string().optional(),
  message: z.string().optional(),
  current_version: z.string().optional(),
  latest_version: z.string().optional(),
  policy: z.enum(['disabled', 'automatic']).optional(),
  available: z.boolean().optional(),
  downloaded: z.boolean().optional(),
  package_kind: z.string().optional(),
  download_size: z.coerce.number().optional(),
  progress: z.coerce.number().optional(),
});
export type UpdateState = z.infer<typeof UpdateSchema>;

export const BootstrapSchema = z.looseObject({
  csrf_token: z.string().optional(),
  csrfToken: z.string().optional(),
  version: z.string().optional(),
  app_version: z.string().optional(),
  mode: z.string().optional(),
  profile: ProfileSchema.optional(),
  config: UnknownRecordSchema.optional(),
  receiver: UnknownRecordSchema.optional(),
  web: UnknownRecordSchema.optional(),
  license: UnknownRecordSchema.optional(),
  dcmtk: UnknownRecordSchema.optional(),
  task: TaskSchema.optional(),
  active_task: TaskSchema.optional(),
  update: UpdateSchema.optional(),
});
export type Bootstrap = z.infer<typeof BootstrapSchema>;

export const ProfilesResponseSchema = z.union([
  z.array(ProfileSchema),
  z.looseObject({ profiles: z.array(ProfileSchema).optional(), items: z.array(ProfileSchema).optional() }),
]);

export const PreflightSchema = z.looseObject({
  ok: z.boolean().optional(),
  message: z.string().optional(),
  checks: z.union([UnknownRecordSchema, z.array(UnknownRecordSchema)]).optional(),
  items: z.union([UnknownRecordSchema, z.array(UnknownRecordSchema)]).optional(),
});
export type Preflight = z.infer<typeof PreflightSchema>;

export const LogSchema = z.looseObject({
  timestamp: z.string().optional(),
  time: z.string().optional(),
  level: z.string().optional(),
  source: z.string().optional(),
  component: z.string().optional(),
  message: z.string().optional(),
  text: z.string().optional(),
});
export type LogEntry = z.infer<typeof LogSchema>;

export const SnapshotSchema = z.looseObject({
  config: UnknownRecordSchema.optional(),
  profile: ProfileSchema.optional(),
  web: UnknownRecordSchema.optional(),
  receiver: UnknownRecordSchema.optional(),
  license: UnknownRecordSchema.optional(),
  health: UnknownRecordSchema.optional(),
  version: z.string().optional(),
  task: TaskSchema.optional(),
  active_task: TaskSchema.optional(),
});
export type Snapshot = z.infer<typeof SnapshotSchema>;

export const EventPageSchema = z.looseObject({
  events: z.array(UnknownRecordSchema).optional(),
  items: z.array(UnknownRecordSchema).optional(),
  records: z.array(UnknownRecordSchema).optional(),
  last_id: z.union([z.string(), z.number()]).optional(),
});

export function profileNumber(profile?: Profile | null): number | null {
  if (!profile) return null;
  const value = profile.number ?? profile.profile_number ?? profile.id;
  const number = Number.parseInt(String(value ?? ''), 10);
  return Number.isInteger(number) ? number : null;
}

export function profileName(profile?: Profile | null): string {
  const number = profileNumber(profile);
  return profile?.display_name || profile?.name || (number == null ? '当前 Profile' : `Profile ${number}`);
}

export function profileIssues(profile?: Profile | null): string[] {
  const raw = profile?.issues || profile?.validation_errors || [];
  const values = raw.map((item) => {
    if (typeof item === 'string') return item;
    if (item && typeof item === 'object' && 'message' in item) return String(item.message);
    return String(item ?? '');
  }).filter(Boolean);
  if (profile?.last_error) values.push(profile.last_error);
  return values;
}
