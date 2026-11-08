import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource/inter/400.css';
import '@fontsource/inter/500.css';
import '@fontsource/inter/700.css';
import '@fontsource/manrope/500.css';
import '@fontsource/manrope/600.css';
import '@fontsource/manrope/800.css';
import './styles/global.css';
import { App } from './App';
import { createHttpAuthApi } from './auth/api';
import { AuthController } from './auth/controller';
import { createSessionStorageMarker } from './auth/signedOut';
import { brand } from './config/brand';
import { waitForSession } from './auth/waitForSession';
import { BackendSportsDataProvider } from './data/BackendSportsDataProvider';
import { DemoDataProvider, type DemoOperation } from './data/DemoDataProvider';
import { resolveDataMode } from './data/mode';
import { BootTracker } from './loading/boot';
import { createTelegramAdapter } from './telegram/telegram';
import { systemClock } from './time/clock';

document.title = brand.name;

// Demo-only switch to show error + retry states: ?demoFail=events,search,ai (each fails once).
const failOnce = (new URLSearchParams(window.location.search).get('demoFail') ?? '')
  .split(',')
  .filter((op): op is DemoOperation => op === 'events' || op === 'search' || op === 'ai');

const telegram = createTelegramAdapter(window.Telegram?.WebApp);
// Created once per page, outside React: StrictMode cannot double-bootstrap it.
const auth = new AuthController(createHttpAuthApi(), () => telegram.initData(), createSessionStorageMarker());
// Real data needs a session; a real build never switches to demo data on errors.
const data =
  resolveDataMode(import.meta.env.VITE_DATA_MODE) === 'real'
    ? new BackendSportsDataProvider({
        waitForSession: () => waitForSession(auth),
        getCsrfToken: () => {
          const state = auth.getState();
          return state.status === 'authenticated' ? state.session.csrfToken : null;
        },
      })
    : new DemoDataProvider(systemClock, { failOnce });
const services = {
  clock: systemClock,
  data,
  telegram,
  auth,
  scrollMemory: new Map<string, number>(),
  boot: new BootTracker(),
};

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App services={services} />
  </StrictMode>,
);
