import { useEffect } from 'react';
import { HashRouter, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { useAuthBootstrap } from './auth/useAuth';
import { useGoBack } from './hooks/useGoBack';
import { AppLoadingGate } from './loading/AppLoadingGate';
import { AiScreen } from './screens/AiScreen';
import { CatalogScreen } from './screens/CatalogScreen';
import { EventScreen } from './screens/EventScreen';
import { HomeScreen } from './screens/HomeScreen';
import { ProfileScreen } from './screens/ProfileScreen';
import { AppearanceScreen, LanguageScreen, TimezoneScreen } from './screens/SettingsScreens';
import { AppProviders } from './state/AppProviders';
import { useServices, type Services } from './state/contexts';
import { bindViewport } from './telegram/telegram';

const TAB_ROOTS = new Set(['/', '/matches', '/profile']);

/** Keeps Telegram's native BackButton and viewport in sync with the router. */
function TelegramBridge() {
  const { telegram, boot } = useServices();
  const { pathname } = useLocation();
  const goBack = useGoBack('/');
  const navigate = useNavigate();
  useAuthBootstrap();

  useEffect(() => {
    // ready() hides Telegram's own placeholder; our loading screen takes over.
    telegram.ready();
    telegram.expand();
    const unbind = bindViewport(telegram);
    boot.markTelegramReady();
    return unbind;
  }, [telegram, boot]);

  useEffect(() => {
    if (TAB_ROOTS.has(pathname) && pathname === '/') telegram.hideBackButton();
    else telegram.showBackButton();
  }, [pathname, telegram]);

  useEffect(
    () => telegram.onBackButton(() => (TAB_ROOTS.has(pathname) ? navigate('/') : goBack())),
    [telegram, pathname, goBack, navigate],
  );
  return null;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<HomeScreen />} />
      <Route path="/matches" element={<CatalogScreen />} />
      <Route path="/event/:id" element={<EventScreen />} />
      <Route path="/event/:id/ai" element={<AiScreen />} />
      <Route path="/profile" element={<ProfileScreen />} />
      <Route path="/profile/language" element={<LanguageScreen />} />
      <Route path="/profile/timezone" element={<TimezoneScreen />} />
      <Route path="/profile/appearance" element={<AppearanceScreen />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export function App({ services }: { services: Services }) {
  return (
    <AppProviders services={services}>
      {/* Hash routing: the Mini App is static files; no server rewrites needed. */}
      <HashRouter>
        <TelegramBridge />
        <AppLoadingGate>
          <AppRoutes />
        </AppLoadingGate>
      </HashRouter>
    </AppProviders>
  );
}
