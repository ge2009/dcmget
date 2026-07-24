import { describe, expect, it } from 'vitest';
import { DETAIL_LIMIT, formatDuration, formatRate, parseAccessions, statusView, taskCount } from './domain';

describe('DcmGet domain helpers', () => {
  it('deduplicates accessions while keeping first-seen order', () => {
    const parsed = parseAccessions(' CT001 \n\nCT002\nCT001\nBAD*VALUE');
    expect(parsed.values).toEqual(['CT001', 'CT002']);
    expect(parsed.blank).toBe(1);
    expect(parsed.duplicate).toBe(1);
    expect(parsed.invalid).toBe(1);
  });

  it('normalizes task statuses and metrics from compatible aliases', () => {
    expect(statusView('partial-success')).toEqual({ label: '部分成功', tone: 'warning' });
    expect(taskCount({ status: 'running', summary: { processed_count: 7 } }, 'processed_count')).toBe(7);
    expect(formatRate(1_572_864)).toBe('1.50 MB/s');
    expect(formatDuration(3661)).toBe('01:01:01');
  });

  it('keeps the large task threshold fixed at 200', () => {
    expect(DETAIL_LIMIT).toBe(200);
  });
});
