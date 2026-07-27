import { describe, expect, it } from 'vitest';
import { BootstrapSchema } from './schemas';

describe('bootstrap schema compatibility', () => {
  it('accepts null detail collections intentionally omitted for a large task', () => {
    const result = BootstrapSchema.safeParse({
      csrf_token: 'token',
      task: {
        id: 'large-task',
        status: 'interrupted',
        total: 9338,
        processed: 28,
        accessions: null,
        items: null,
        results: null,
        actions: { can_resume: true },
      },
    });

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.task?.accessions).toBeNull();
      expect(result.data.task?.results).toBeNull();
    }
  });
});
