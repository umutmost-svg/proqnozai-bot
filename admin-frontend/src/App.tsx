import { BrowserRouter, Link, Route, Routes } from 'react-router-dom';
import { Activate, LoginFlow } from './pages/Auth';
import { Forecasts } from './pages/Forecasts';
import { Analytics } from './pages/Analytics';
import { BroadcastEditor, Broadcasts, Credits, Dialogs, Segments } from './pages/Crm';
import { SettingsAdmins, SettingsFeatures, SettingsJournal } from './pages/Settings';
import { AiCosts, SportsApi, System } from './pages/Telemetry';
import { UserCardRoute } from './pages/UserCard';
import { Users } from './pages/Users';
import { Referrals } from './pages/Referrals';
import { Account } from './pages/Account';
import { MbQueue } from './pages/MbQueue';
import { Inquiries } from './pages/Inquiries';
import { AdsPlaceholder, Outbox, Product, SettingsRoles } from './pages/Ops';
import { AuthProvider, PrefsProvider, useAuth } from './state';
import { Empty, Layout, Loading, PageHead } from './ui';

function Gate() {
  const { state } = useAuth();
  // The invitation page works signed in or not: it never uses the session.
  if (window.location.pathname === '/activate') return <Activate />;
  if (state.status === 'loading') {
    return <div style={{ padding: 60, maxWidth: 600 }}><Loading rows={3} /></div>;
  }
  if (state.status === 'anonymous') return <LoginFlow />;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Analytics />} />
        <Route path="dialogs" element={<Dialogs />} />
        <Route path="broadcasts" element={<Broadcasts />} />
        <Route path="broadcasts/:id" element={<BroadcastEditor />} />
        <Route path="segments" element={<Segments />} />
        <Route path="users" element={<Users />} />
        <Route path="users/:id" element={<UserCardRoute />} />
        <Route path="forecasts" element={<Forecasts />} />
        <Route path="ai" element={<AiCosts />} />
        <Route path="sports-api" element={<SportsApi />} />
        <Route path="system" element={<System />} />
        <Route path="settings" element={<SettingsFeatures />} />
        <Route path="settings/admins" element={<SettingsAdmins />} />
        <Route path="settings/journal" element={<SettingsJournal />} />
        <Route path="credits" element={<Credits />} />
        <Route path="referrals" element={<Referrals />} />
        <Route path="mb" element={<MbQueue />} />
        <Route path="inquiries" element={<Inquiries />} />
        <Route path="outbox" element={<Outbox />} />
        <Route path="product" element={<Product />} />
        <Route path="settings/roles" element={<SettingsRoles />} />
        <Route path="account" element={<Account />} />
        {/* Ads hookup point. After merging feat/ads-module replace this one line with:
            import { AdsRoutes } from './ads';  <Route path="ads/*" element={<AdsRoutes />} />
            (and use ADS_NAV_LINK from './ads' for the ['/ads', 'Реклама'] entry in ui.tsx NAV). */}
        <Route path="ads/*" element={<AdsPlaceholder />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}

function NotFound() {
  return (
    <>
      <PageHead crumb="Не найдено" title="Страница не найдена" subtitle="Такого раздела нет" />
      <Empty title="Проверьте адрес" action={<Link to="/">Перейти к обзору →</Link>} />
    </>
  );
}

export function App() {
  return (
    <AuthProvider>
      <PrefsProvider>
        <BrowserRouter>
          <Gate />
        </BrowserRouter>
      </PrefsProvider>
    </AuthProvider>
  );
}
