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
import { DemoDataProvider, type DemoOperation } from './data/DemoDataProvider';
import { createTelegramAdapter } from './telegram/telegram';
import { systemClock } from './time/clock';

document.title = brand.name;

// Demo-only switch to show error + retry states: ?demoFail=events,search,ai (each fails once).
const failOnce = (new URLSearchParams(window.location.search).get('demoFail') ?? '')
  .split(',')
  .filter((op): op is DemoOperation => op === 'events' || op === 'search' || op === 'ai');

const telegram = createTelegramAdapter(window.Telegram?.WebApp);
const services = {
  clock: systemClock,
  // Sports data stays demo in M2 regardless of the sign-in state.
  data: new DemoDataProvider(systemClock, { failOnce }),
  telegram,
  // Created once per page, outside React: StrictMode cannot double-bootstrap it.
  auth: new AuthController(createHttpAuthApi(), () => telegram.initData(), createSessionStorageMarker()),
  scrollMemory: new Map<string, number>(),
};

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App services={services} />
  </StrictMode>,
);
