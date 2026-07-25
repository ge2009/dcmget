import { Component, type ErrorInfo, type ReactNode } from 'react';

type CopyState = 'idle' | 'copying' | 'copied' | 'failed';

type AppErrorBoundaryProps = {
  children: ReactNode;
  onReload?: () => void;
  copyErrorDetails?: (details: string) => Promise<void> | void;
};

type AppErrorBoundaryState = {
  error: Error | null;
  componentStack: string;
  diagnosticId: string;
  occurredAt: string;
  copyState: CopyState;
};

function createDiagnosticId() {
  const timestamp = new Date().toISOString().replace(/\D/g, '').slice(0, 14);
  const nonce = Math.floor(Math.random() * 0x1000000).toString(16).padStart(6, '0').toUpperCase();
  return `DCM-${timestamp}-${nonce}`;
}

function normalizeError(error: unknown) {
  return error instanceof Error ? error : new Error(String(error));
}

async function copyToClipboard(details: string) {
  if (!navigator.clipboard?.writeText) throw new Error('当前环境不支持剪贴板写入');
  await navigator.clipboard.writeText(details);
}

export class AppErrorBoundary extends Component<AppErrorBoundaryProps, AppErrorBoundaryState> {
  private headingRef = { current: null as HTMLHeadingElement | null };

  state: AppErrorBoundaryState = {
    error: null,
    componentStack: '',
    diagnosticId: '',
    occurredAt: '',
    copyState: 'idle',
  };

  static getDerivedStateFromError(error: unknown): Partial<AppErrorBoundaryState> {
    return {
      error: normalizeError(error),
      diagnosticId: createDiagnosticId(),
      occurredAt: new Date().toISOString(),
      copyState: 'idle',
    };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[DcmGet] 未捕获的前端渲染错误（${this.state.diagnosticId}）`, error, info);
    this.setState({ componentStack: info.componentStack ?? '' });
    this.headingRef.current?.focus();
  }

  private errorDetails() {
    const { componentStack, diagnosticId, error, occurredAt } = this.state;
    if (!error) return '';

    return [
      'DcmGet 前端运行时错误',
      `诊断编号：${diagnosticId}`,
      `发生时间：${occurredAt}`,
      `页面地址：${window.location.href}`,
      `错误类型：${error.name}`,
      `错误信息：${error.message}`,
      error.stack ? `错误堆栈：\n${error.stack}` : '',
      componentStack ? `组件堆栈：${componentStack}` : '',
      `运行环境：${navigator.userAgent}`,
    ].filter(Boolean).join('\n');
  }

  private handleReload = () => {
    if (this.props.onReload) {
      this.props.onReload();
      return;
    }
    window.location.reload();
  };

  private handleCopy = async () => {
    this.setState({ copyState: 'copying' });
    try {
      await (this.props.copyErrorDetails ?? copyToClipboard)(this.errorDetails());
      this.setState({ copyState: 'copied' });
    } catch (error) {
      console.error('[DcmGet] 复制前端错误详情失败', error);
      this.setState({ copyState: 'failed' });
    }
  };

  render() {
    const { copyState, diagnosticId, error } = this.state;
    if (!error) return this.props.children;

    const copyLabel = copyState === 'copying'
      ? '正在复制…'
      : copyState === 'copied'
        ? '错误详情已复制'
        : '复制错误详情';
    const copyStatus = copyState === 'copied'
      ? '错误详情已复制到剪贴板。'
      : copyState === 'failed'
        ? '复制失败，请手动记录诊断编号。'
        : '';

    return (
      <main className="app-error-screen">
        <section className="app-error-card" role="alert" aria-labelledby="app-error-title" aria-describedby="app-error-description">
          <div className="app-error-card__rule" aria-hidden="true" />
          <header className="app-error-card__header">
            <span className="app-error-card__mark" aria-hidden="true">!</span>
            <div>
              <p className="eyebrow">DcmGet · Application recovery</p>
              <h1 id="app-error-title" ref={(node) => { this.headingRef.current = node; }} tabIndex={-1}>应用界面暂时无法显示</h1>
              <p id="app-error-description">界面遇到了无法自动恢复的错误。您可以重新加载应用，或复制错误详情交给维护人员排查。</p>
            </div>
          </header>

          <dl className="app-error-card__diagnostic">
            <dt>诊断编号</dt>
            <dd><code>{diagnosticId}</code></dd>
          </dl>

          <div className="app-error-card__actions">
            <button type="button" className="button button--primary" onClick={this.handleReload}>重新加载应用</button>
            <button type="button" className="button button--secondary" onClick={this.handleCopy} disabled={copyState === 'copying'}>{copyLabel}</button>
          </div>

          {copyStatus && <p className="app-error-card__copy-status" data-state={copyState} role="status" aria-live="polite">{copyStatus}</p>}
          <p className="app-error-card__hint">若重新加载后问题仍然出现，请保留上方诊断编号。</p>
        </section>
      </main>
    );
  }
}
