import { afterEach, describe, expect, it, vi } from 'vitest';
import { z } from 'zod';
import { apiClient, managedApiPath } from './api';

afterEach(() => vi.restoreAllMocks());

describe('same-origin API client', () => {
  it('rotates the in-memory CSRF token and sends it on mutations', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, csrf_token: 'rotated' }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    const schema = z.looseObject({ ok: z.boolean(), csrf_token: z.string().optional() });
    await apiClient.request('/api/bootstrap', schema);
    await apiClient.request('/api/task/pause', schema, { method: 'POST', body: {} });
    const options = fetchMock.mock.calls[1][1] as RequestInit;
    expect(new Headers(options.headers).get('X-CSRF-Token')).toBe('rotated');
    expect(options.credentials).toBe('same-origin');
  });

  it('constructs managed profile proxy paths without a duplicate api segment', () => {
    expect(managedApiPath(3, '/api/task/start')).toBe('/api/management/profiles/3/task/start');
  });
});
