import '@testing-library/jest-dom/vitest';

class MockEventSource {
  static CLOSED = 2;
  readyState = 1;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  addEventListener() {}
  close() { this.readyState = MockEventSource.CLOSED; }
}

Object.defineProperty(window, 'EventSource', { value: MockEventSource, writable: true });
