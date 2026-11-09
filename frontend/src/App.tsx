import { useEffect } from 'react';
import { HashRouter, Navigate, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom';
import { useAuthBootstrap } from './auth/useAuth';
import { useGoBack } from './hooks/useGoBack';
import { AppLoadingGate } from './loading/AppLoadingGate';
import { AiScreen } from './screens/AiScreen';
import { TournamentScreen } from './screens/TournamentScreen';
import { TeamScreen } from './screens/TeamScreen';
import { SupportScreen } from './screens/SupportScreen';
import { CatalogScreen } from './screens/CatalogScreen';
import { EventScreen } from './screens/EventScreen';
import { HomeScreen } from './screens/HomeScreen';
import { ProfileScreen } from './screens/ProfileScreen';
import { QuickScreen } from './screens/QuickScreen';
import { AppearanceScreen, LanguageScreen, TimezoneScreen } from './screens/SettingsScreens';
import { AppProviders } from './state/AppProviders';
import { useServices, type Services } from './state/contexts';
import { bindViewport } from './telegram/telegram';

const TAB_ROOTS = new Set(['/', '/matches', '/quick', '/profile']);

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

/** One AI screen per match: a "create" tapped for one match never survives into another's screen. */
function AiForEvent() {
  const { id = '' } = useParams();
  return <AiScreen key={id} />;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<HomeScreen />} />
      <Route path="/matches" element={<CatalogScreen />} />
      <Route path="/event/:id" element={<EventScreen />} />
      <Route path="/event/:id/ai" element={<AiForEvent />} />
      <Route path="/tournament/:id" element={<TournamentScreen />} />
      <Route path="/team/:id" element={<TeamScreen />} />
      <Route path="/quick" element={<QuickScreen />} />
      <Route path="/profile/support" element={<SupportScreen />} />
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
