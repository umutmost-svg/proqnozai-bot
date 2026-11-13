import { BrowserRouter, Link, Route, Routes } from 'react-router-dom';
import { LoginFlow } from './pages/Auth';
import { Forecasts } from './pages/Forecasts';
import { Analytics } from './pages/Analytics';
import { BroadcastEditor, Broadcasts, Credits, Dialogs, Segments } from './pages/Crm';
import { SettingsAdmins, SettingsFeatures, SettingsJournal } from './pages/Settings';
import { AiCosts, SportsApi, System } from './pages/Telemetry';
import { UserCardRoute } from './pages/UserCard';
import { Users } from './pages/Users';
import { Referrals } from './pages/Referrals';
import { AuthProvider, PrefsProvider, useAuth } from './state';
import { Empty, Layout, Loading, PageHead } from './ui';

function Gate() {
  const { state } = useAuth();
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
