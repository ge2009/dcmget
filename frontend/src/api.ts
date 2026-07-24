import type { ZodType } from 'zod';

export class ApiError extends Error {
  readonly status: number;
  readonly details: unknown;

  constructor(message: string, status = 0, details: unknown = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.details = details;
  }
}

type RequestOptions = Omit<RequestInit, 'body'> & { body?: unknown };

class ApiClient {
  #csrfToken = '';

  setCsrfToken(token: unknown) {
    if (typeof token === 'string' && token) this.#csrfToken = token;
  }

  async request<T>(path: string, schema: ZodType<T>, options: RequestOptions = {}): Promise<T> {
    const method = String(options.method || 'GET').toUpperCase();
    const headers = new Headers(options.headers);
    let body = options.body as BodyInit | null | undefined;
    const raw = body instanceof FormData || body instanceof Blob || body instanceof URLSearchParams
      || body instanceof ArrayBuffer || ArrayBuffer.isView(body);
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) headers.set('X-CSRF-Token', this.#csrfToken);
    if (body != null && !raw) {
      headers.set('Content-Type', 'application/json');
      body = JSON.stringify(body);
    }
    const response = await fetch(path, {
      ...options,
      method,
      headers,
      body,
      credentials: 'same-origin',
      cache: 'no-store',
    });
    const contentType = response.headers.get('content-type') || '';
    const payload = response.status === 204
      ? {}
      : contentType.includes('application/json')
        ? await response.json().catch(() => null)
        : await response.text().catch(() => '');
    const rotated = response.headers.get('X-CSRF-Token')
      || (payload && typeof payload === 'object' && ('csrf_token' in payload || 'csrfToken' in payload)
        ? String(payload.csrf_token || payload.csrfToken || '')
        : '');
    this.setCsrfToken(rotated);
    if (!response.ok) {
      const record = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {};
      const detail = record.detail && typeof record.detail === 'object' ? record.detail as Record<string, unknown> : {};
      throw new ApiError(
        String(record.message || record.error || detail.message || (typeof record.detail === 'string' ? record.detail : '') || `请求失败（HTTP ${response.status}）`),
        response.status,
        payload,
      );
    }
    const parsed = schema.safeParse(payload ?? {});
    if (!parsed.success) {
      console.error('DcmGet API contract mismatch', path, parsed.error.issues);
      throw new ApiError('后台返回的数据格式不兼容，请更新 DcmGet 或查看诊断日志。', 502, parsed.error.issues);
    }
    return parsed.data;
  }
}

export const apiClient = new ApiClient();

export function managedApiPath(profileNumber: number, path: string): string {
  const normalized = path.replace(/^\/?api\/?/, '').replace(/^\/+/, '');
  return `/api/management/profiles/${profileNumber}/${normalized}`;
}
