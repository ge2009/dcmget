import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { MotionConfig } from 'motion/react';
import App from './App';
import { AppErrorBoundary } from './components/AppErrorBoundary';
import './styles.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppErrorBoundary>
      <MotionConfig reducedMotion="user">
        <App />
      </MotionConfig>
    </AppErrorBoundary>
  </StrictMode>,
);
