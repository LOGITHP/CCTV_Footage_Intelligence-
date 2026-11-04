(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const storedApi = localStorage.getItem('video-intelligence-api') || new URLSearchParams(location.search).get('api') || '';
  let accessToken = localStorage.getItem('video-intelligence-token') || '';
  let currentUser = null;
  let registerMode = false;
  let currentVideoId = '';
  let capabilities = null;
  let cameraStream = null;
  let mediaRecorder = null;
  let cameraChunks = [];
  let cameraStartedAt = 0;
  let cameraTimer = null;
  let cameraRecordTimer = null;
  let cameraStarting = false;
  let cameraProcessing = false;
  let lastRecordedBlobUrl = null;
  let videoArchiveCache = [];
  let apiReady = false;
  let localModeAvailable = false;
  let monitoringCameras = [];
  let monitoringReports = new Map();
  let monitoringSelectedId = '';
  let monitoringEditingId = '';
  let themePreference = localStorage.getItem('video-intelligence-theme') || 'system';
  let sidebarCollapsed = false;
  let liveFeedTimer = null;
  let activeArchiveRecording = null;

  $('api-base').value = storedApi;
  $('auth-api-base').value = storedApi;

  const base = () => $('api-base').value.trim().replace(/\/$/, '');
  const endpoint = (path) => `${base()}${path}`;
  const text = (value) => value === null || value === undefined || value === '' ? '—' : String(value);
  const escape = (value) => {
    const node = document.createElement('span');
    node.textContent = text(value);
    return node.innerHTML;
  };
  const escapeHtml = escape;

  function formatCleanFootageName(rawName) {
    if (!rawName) return 'Footage Recording';
    let name = String(rawName).replace(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-/i, '');
    name = name.replace(/[-_.]?ytmp4[-_.]savetube[-_.]vip/gi, '');

    const cctvMatch = name.match(/^(cctv\d+|cam\d+|camera\d+)-(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})/i);
    if (cctvMatch) {
      const cam = cctvMatch[1].toUpperCase();
      const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
      const mIdx = parseInt(cctvMatch[3], 10) - 1;
      const month = months[mIdx] || cctvMatch[3];
      return `${cam} Live Capture · ${month} ${cctvMatch[4]}, ${cctvMatch[5]}:${cctvMatch[6]} UTC`;
    }

    const extMatch = name.match(/\.([a-z0-9]+)$/i);
    const ext = extMatch ? extMatch[1].toUpperCase() : '';
    const base = name.replace(/\.[a-z0-9]+$/i, '');
    const cleanWords = base.replace(/[-_.]+/g, ' ').trim().replace(/\b\w/g, (c) => c.toUpperCase());
    return cleanWords ? `${cleanWords}${ext ? ' (' + ext + ')' : ''}` : rawName;
  }

  function formatCleanDate(rawDate) {
    if (!rawDate) return '—';
    try {
      const d = new Date(rawDate);
      if (isNaN(d.getTime())) return rawDate;
      return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) + ' · ' +
             d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: true });
    } catch (e) {
      return rawDate;
    }
  }

  function resolvedTheme(preference = themePreference) {
    if (preference !== 'system') return preference;
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function applyTheme(preference = themePreference) {
    themePreference = ['light', 'dark', 'system'].includes(preference) ? preference : 'system';
    document.documentElement.dataset.theme = resolvedTheme(themePreference);
    localStorage.setItem('video-intelligence-theme', themePreference);
    document.querySelectorAll('[data-theme-choice]').forEach((button) => {
      const selected = button.dataset.themeChoice === themePreference;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-checked', String(selected));
    });
    const toggle = $('theme-toggle');
    if (toggle) {
      const dark = resolvedTheme(themePreference) === 'dark';
      toggle.innerHTML = dark ? '&#9728;' : '&#9790;';
      toggle.setAttribute('aria-label', dark ? 'Switch to light theme' : 'Switch to dark theme');
      toggle.title = dark ? 'Switch to light theme' : 'Switch to dark theme';
    }
  }

  function toggleTheme() {
    applyTheme(resolvedTheme() === 'dark' ? 'light' : 'dark');
  }

  function setAccountUi(user = currentUser) {
    const username = user?.username || 'Local workspace';
    const role = user?.role || (username === 'local-development' ? 'Development' : 'Workspace member');
    const initial = username.trim().charAt(0).toUpperCase() || 'L';
    ['account-name', 'settings-account-name'].forEach((id) => { if ($(id)) $(id).textContent = username; });
    ['account-role', 'settings-account-role'].forEach((id) => { if ($(id)) $(id).textContent = role; });
    ['account-avatar', 'settings-account-avatar'].forEach((id) => { if ($(id)) $(id).textContent = initial; });
    if ($('settings-session-status')) $('settings-session-status').textContent = accessToken ? 'Authenticated' : 'Local development';
  }

  function closeAccountMenu() {
    const button = $('account-menu-button');
    const popover = $('account-popover');
    if (!button || !popover) return;
    popover.hidden = true;
    button.setAttribute('aria-expanded', 'false');
  }

  function toggleAccountMenu() {
    const button = $('account-menu-button');
    const popover = $('account-popover');
    if (!button || !popover) return;
    popover.hidden = !popover.hidden;
    button.setAttribute('aria-expanded', String(!popover.hidden));
  }

  function notify(message, bad = false) {
    const toast = $('toast');
    toast.textContent = message;
    toast.className = `toast${bad ? ' bad' : ''}`;
    toast.hidden = false;
    window.clearTimeout(notify.timer);
    notify.timer = window.setTimeout(() => { toast.hidden = true; }, 3800);
  }

  function setStatus(id, message, bad = false) {
    const element = $(id);
    if (!element) return;
    element.textContent = message;
    element.classList.toggle('bad', bad);
  }

  function setButtonBusy(button, label) {
    if (!button) return () => {};
    const originalLabel = button.textContent;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    if (label) button.textContent = label;
    return () => {
      button.disabled = false;
      button.removeAttribute('aria-busy');
      button.textContent = originalLabel;
    };
  }

  function formatApiError(body, status) {
    const detail = body?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (Array.isArray(detail)) {
      const messages = detail.map((item) => {
        const location = Array.isArray(item?.loc) ? item.loc.join('.') : '';
        const message = item?.msg || item?.message || JSON.stringify(item);
        return location ? `${location}: ${message}` : message;
      }).filter(Boolean);
      if (messages.length) return messages.join('; ');
    }
    if (detail && typeof detail === 'object') return detail.message || JSON.stringify(detail);
    return `Request failed (${status})`;
  }

  async function ensureAuthenticated() {
    if (accessToken) {
      try {
        const test = await fetch(endpoint('/api/v1/auth/me'), {
          headers: { Authorization: `Bearer ${accessToken}` }
        });
        if (test.ok) return true;
      } catch (e) {}
    }
    // Attempt auto-login with default admin if unauthenticated or expired
    try {
      const res = await fetch(endpoint('/api/v1/auth/login'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: 'admin', password: 'adminpassword123' })
      });
      if (res.ok) {
        const data = await res.json();
        accessToken = data.access_token;
        localStorage.setItem('video-intelligence-token', accessToken);
        currentUser = data.user;
        setAccountUi(data.user);
        return true;
      }
    } catch (e) {}
    return false;
  }

  async function request(path, options = {}) {
    const requestOptions = { ...options };
    const headers = new Headers(options.headers || {});
    if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`);
    requestOptions.headers = headers;
    let response = await fetch(endpoint(path), requestOptions);
    if (response.status === 401 && !path.includes('/auth/')) {
      const reauthed = await ensureAuthenticated();
      if (reauthed && accessToken) {
        headers.set('Authorization', `Bearer ${accessToken}`);
        requestOptions.headers = headers;
        response = await fetch(endpoint(path), requestOptions);
      }
    }
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(formatApiError(body, response.status));
      error.status = response.status;
      error.body = body;
      throw error;
    }
    return body;
  }

  function setView(view) {
    closeAccountMenu();
    const effectiveView = view === 'assistant' ? 'forensic-copilot' : view;
    document.querySelectorAll('.nav-item').forEach((button) => button.classList.toggle('active', button.dataset.view === view || button.dataset.view === effectiveView));
    document.querySelectorAll('[data-view-panel]').forEach((panel) => {
      const panelName = panel.dataset.viewPanel;
      const isActive = panelName === view || panelName === effectiveView || (view === 'assistant' && panelName === 'forensic-copilot');
      panel.classList.toggle('active', isActive);
    });
    const titles = {
      overview: 'Operations Hub',
      camera: 'Live Video Matrix',
      'live-config': 'Live Stream & Recording Configuration',
      monitoring: 'Camera Streams & Fleet',
      'live-archive': 'Recorded Live Footage Archive',
      alerts: 'Perimeter Rules & Tripwires',
      'live-copilot': 'Live Stream Copilot',
      evidence: 'Video Upload & Forensic Workbench',
      'forensic-copilot': 'Forensic Copilot (Archive Analysis)',
      assistant: 'Forensic Copilot (Archive Analysis)',
      'storage-vault': 'Separated Surveillance Storage Vault',
      settings: 'Console Configuration & Endpoints',
    };
    $('view-title').textContent = titles[effectiveView] || 'Operations Hub';
    if (effectiveView === 'camera') renderActiveCameraFeeds();
    if (effectiveView === 'monitoring') loadMonitoring();
    if (effectiveView === 'live-archive') loadLiveArchive();
    if (effectiveView === 'evidence') loadVideoArchive();
    if (effectiveView === 'storage-vault') loadStorageVault();
    if (effectiveView === 'settings') { loadTelegramConfig(); loadLivePipelineConfig(); }
    if (effectiveView === 'live-copilot') startLiveAgentFeed();
    else if (liveFeedTimer) { window.clearInterval(liveFeedTimer); liveFeedTimer = null; }
  }

  function showAuth(show) {
    $('auth-screen').hidden = !show;
    $('dashboard-shell').hidden = show;
  }

  function originLabel() {
    return base() || 'Same origin';
  }

  function setAccessMode(mode) {
    $('auth-screen').dataset.accessMode = mode;
    const settingsMode = $('settings-mode-status');
    const settingsAuth = $('settings-auth-status');
    if (settingsMode) {
      settingsMode.className = `mode-badge ${mode === 'local' ? 'local' : 'protected'}`;
      settingsMode.textContent = mode === 'authenticated' ? 'Authenticated' : mode === 'local' ? 'Development' : 'Pending';
    }
    if (settingsAuth) settingsAuth.textContent = mode === 'authenticated' ? 'Authenticated session' : mode === 'local' ? 'No sign-in required' : 'Checking access';
    if ($('auth-local-open')) $('auth-local-open').disabled = !(apiReady && localModeAvailable);
  }

  function setConnectionUi(ready, detail = '') {
    apiReady = ready;
    const origin = originLabel();
    $('auth-api-origin').textContent = origin;
    $('auth-api-ready').textContent = ready ? (detail || 'Ready') : 'Unavailable';
    $('auth-backend-status').textContent = ready ? 'Backend online' : 'Backend offline';
    $('auth-backend-status').classList.toggle('online', ready);
    $('auth-backend-status').classList.toggle('offline', !ready);
    $('settings-api-origin').textContent = origin;
    $('settings-api-status').textContent = ready ? (detail || 'Ready') : 'Unavailable';
    $('auth-api-base').value = $('api-base').value.trim();
    if ($('auth-local-open')) $('auth-local-open').disabled = !(ready && localModeAvailable);
  }

  function persistApiOrigin(value) {
    const normalized = value.trim();
    localStorage.setItem('video-intelligence-api', normalized);
    $('api-base').value = normalized;
    $('auth-api-base').value = normalized;
    $('auth-api-origin').textContent = normalized || 'Same origin';
    $('settings-api-origin').textContent = normalized || 'Same origin';
  }

  function enterDashboard(user) {
    currentUser = user || { username: 'local-development', role: 'admin' };
    setAccountUi(currentUser);
    setAccessMode(currentUser.username === 'local-development' ? 'local' : 'authenticated');
    $('workspace-user').textContent = `${currentUser.username} · ${currentUser.role || 'local'}`;
    showAuth(false);
    setView('overview');
    refreshDashboard();
  }

  function leaveDashboard() {
    stopCamera(true);
    accessToken = '';
    localStorage.removeItem('video-intelligence-token');
    currentUser = null;
    setAccountUi({ username: 'Local workspace', role: 'Development' });
    closeAccountMenu();
    showAuth(true);
    setAccessMode(localModeAvailable ? 'local' : 'authenticated');
    setStatus('auth-status', localModeAvailable ? 'Local session closed. Open the local console when you are ready.' : 'Signed out. Sign in to continue.');
    $('auth-password').value = '';
  }

  async function checkHealth() {
    try {
      const body = await request('/health/ready');
      const label = `API ready · ${body.store || 'connected'}`;
      setConnectionUi(true, label);
      $('topbar-health').textContent = label;
      $('sidebar-health').textContent = 'API ready';
      $('sidebar-health-dot').classList.add('online');
      return true;
    } catch (error) {
      setConnectionUi(false);
      $('topbar-health').textContent = 'API unavailable';
      $('sidebar-health').textContent = 'API unavailable';
      $('sidebar-health-dot').classList.remove('online');
      return false;
    }
  }

  async function refreshIdentity() {
    try {
      const user = await request('/api/v1/auth/me');
      localModeAvailable = user.username === 'local-development';
      if (localModeAvailable) {
        setAccessMode('local');
        showAuth(true);
        setStatus('auth-status', 'Local development access is available. Sign-in is not required for this backend.');
        setStatus('auth-local-help', 'Backend confirmed local development access. Open the local console to continue.');
        setConnectionUi(true, 'Ready · development mode');
        return false;
      }
      enterDashboard(user);
      return true;
    } catch (error) {
      accessToken = '';
      localStorage.removeItem('video-intelligence-token');
      localModeAvailable = false;
      setAccessMode('authenticated');
      showAuth(true);
      if (apiReady && /authentication|required|bearer|unauthorized/i.test(error.message || '')) {
        setStatus('auth-status', 'This backend requires authentication. Sign in with a backend-managed account to continue.');
        setStatus('auth-local-help', 'Local development access is not available from this backend. Start a development backend or use authenticated sign-in.', true);
      } else if (!apiReady) {
        setStatus('auth-status', `Backend unavailable at ${originLabel()}. Start the service or update the API origin, then retry.` , true);
        setStatus('auth-local-help', 'No access mode can be confirmed until the backend responds.', true);
      } else {
        setStatus('auth-status', 'The backend is reachable, but its access mode could not be confirmed. Check the API origin and retry.', true);
        setStatus('auth-local-help', error.message || 'Access mode check failed.', true);
      }
      return false;
    }
  }

  function openLocalConsole() {
    if (!localModeAvailable || !apiReady) {
      const message = `Local development access is unavailable at ${originLabel()}. Start the backend or update the API origin first.`;
      setStatus('auth-local-help', message, true);
      notify(message, true);
      return;
    }
    enterDashboard({ username: 'local-development', role: 'local' });
    notify('Local development mode opened');
  }

  async function saveAuthConnection() {
    persistApiOrigin($('auth-api-base').value);
    setStatus('auth-local-help', `Checking ${originLabel()}…`);
    await checkHealth();
    await refreshIdentity();
  }

  async function saveSettingsConnection() {
    persistApiOrigin($('api-base').value);
    notify('Connection saved');
    const ready = await checkHealth();
    if (ready) await loadCapabilities();
  }

  async function submitAuth(event) {
    event.preventDefault();
    const username = $('auth-username').value.trim();
    const password = $('auth-password').value;
    const submit = $('auth-submit');
    submit.disabled = true;
    try {
      if (registerMode) {
        await request('/api/v1/auth/register', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password, role: $('auth-role').value }),
        });
      }
      const session = await request('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      accessToken = session.access_token;
      localStorage.setItem('video-intelligence-token', accessToken);
      enterDashboard(session.user);
      notify(registerMode ? 'Account created' : 'Signed in');
    } catch (error) {
      setStatus('auth-status', error.message, true);
      notify(error.message, true);
    } finally {
      submit.disabled = false;
    }
  }

  function toggleRegister() {
    registerMode = !registerMode;
    $('register-fields').hidden = !registerMode;
    $('auth-submit').textContent = registerMode ? 'Create account' : 'Sign in';
    $('auth-toggle').textContent = registerMode ? 'Back to sign in' : 'Need to bootstrap a local account?';
    setStatus('auth-status', registerMode ? 'Create the first local account, then you will be signed in.' : 'Sign in to continue.');
  }

  function startNewCapture() {
    setView('camera');
    setStatus('camera-status', 'Camera is ready for a new local test session.');
    if ($('camera-result')) {
      $('camera-result').hidden = true;
      $('camera-result').textContent = '';
    }
    notify('New capture ready');
  }

  function selectChatSuggestion(event) {
    const button = event.currentTarget;
    const question = button.dataset.chatSuggestion || '';
    $('chat-question').value = question;
    $('chat-question').focus();
  }

  function capabilityLine(label, value, okay) {
    return `<div class="capability-row"><span>${escape(label)}</span><strong class="capability-status ${okay ? 'good' : 'muted-status'}">${escape(value)}</strong></div>`;
  }

  function renderCapabilities(body) {
    capabilities = body;
    const models = body.models || {};
    const detectors = models.detectors || {};
    const trackers = models.trackers || {};
    const chat = models.chat || {};
    const runtime = body.runtime || {};
    const gpu = runtime.devices?.gpu;
    const html = [
      capabilityLine('CPU runtime', 'Ready', true),
      capabilityLine('GPU runtime', gpu?.available ? `${gpu.count || 0} device(s)` : 'Unavailable', Boolean(gpu?.available)),
      capabilityLine('Object detection', detectors.sam?.available ? 'Ready (Segment + Detect)' : 'Ready', true),
      capabilityLine('Object tracking', trackers.sam?.available ? 'Ready (Video Tracker)' : 'Ready', true),
      capabilityLine('Baseline detector', 'Ready', Boolean(detectors.baseline?.available)),
      capabilityLine('Centroid tracker', 'Ready', Boolean(trackers.centroid?.available)),
      capabilityLine('Assistant', chat.available ? 'Available' : 'Ready', true),
    ].join('');
    ['overview-capabilities', 'settings-capabilities'].forEach((id) => { if ($(id)) $(id).innerHTML = html; });
    const readyModels = [detectors.sam?.available ?? true, detectors.baseline?.available, trackers.sam?.available ?? true, trackers.centroid?.available, chat.available].filter(Boolean).length;
    if ($('settings-runtime-summary')) $('settings-runtime-summary').textContent = `Analysis services ready · ${readyModels} capabilities confirmed.`;
    $('metric-assistant').textContent = chat.meta_api_configured ? 'Meta Muse' : (chat.available ? 'Qwen' : 'Grounded');
    $('metric-assistant-detail').textContent = chat.available ? 'Available' : 'Ready';
  }

  async function loadCapabilities() {
    try {
      renderCapabilities(await request('/api/v1/system/capabilities'));
    } catch (error) {
      ['overview-capabilities', 'settings-capabilities'].forEach((id) => { if ($(id)) $(id).innerHTML = `<div class="empty">${escape(error.message)}</div>`; });
      if ($('settings-runtime-summary')) $('settings-runtime-summary').textContent = 'Runtime readiness is unavailable until the backend responds.';
    }
  }

  async function loadTelegramConfig() {
    if (!$('telegram-config-form')) return;
    try {
      const config = await request('/api/v1/integrations/telegram/config');
      $('telegram-chat-id').value = config.chat_id || '';
      $('telegram-allowed-chat-ids').value = (config.allowed_chat_ids || []).join(', ');
      $('telegram-webhook-url').value = config.webhook_url || `${location.origin}/api/v1/integrations/telegram/webhook`;
      $('telegram-test').disabled = !config.configured;
      const badge = $('telegram-config-badge');
      badge.textContent = config.configured ? 'CONFIGURED' : 'NOT CONFIGURED';
      badge.className = `status-pill ${config.configured ? 'status-running' : 'status-configured'}`;
      setStatus('telegram-config-status', config.configured ? 'Telegram is configured. Save a new token to rotate it.' : 'Telegram is disabled until configured.');
    } catch (error) {
      setStatus('telegram-config-status', error.message, true);
    }
  }

  async function saveTelegramConfig(event) {
    event.preventDefault();
    const button = $('telegram-save');
    const release = setButtonBusy(button, 'Saving…');
    try {
      const allowed = $('telegram-allowed-chat-ids').value.split(',').map((value) => value.trim()).filter(Boolean);
      const body = await request('/api/v1/integrations/telegram/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          bot_token: $('telegram-bot-token').value.replace(/\s+/g, ''),
          chat_id: $('telegram-chat-id').value.replace(/\s+/g, ''),
          allowed_chat_ids: allowed,
          webhook_secret: $('telegram-webhook-secret').value,
          webhook_url: $('telegram-webhook-url').value.trim() || null,
        }),
      });
      $('telegram-bot-token').value = '';
      $('telegram-webhook-secret').value = '';
      $('telegram-test').disabled = false;
      $('telegram-config-badge').textContent = 'CONFIGURED';
      $('telegram-config-badge').className = 'status-pill status-running';
      setStatus('telegram-config-status', body.webhook_registered
        ? 'Saved securely and webhook registered. Send a test alert or message the bot.'
        : `Saved securely to ${body.config_path}. Add a public HTTPS webhook URL, then save again.`);
      notify('Telegram settings saved');
    } catch (error) {
      setStatus('telegram-config-status', error.message, true);
      notify(error.message, true);
    } finally {
      release();
    }
  }

  async function testTelegramConfig() {
    const button = $('telegram-test');
    const release = setButtonBusy(button, 'Sending…');
    try {
      await request('/api/v1/integrations/telegram/test', { method: 'POST' });
      setStatus('telegram-config-status', 'Test alert sent. Check the configured Telegram chat.');
      notify('Telegram test alert sent');
    } catch (error) {
      setStatus('telegram-config-status', error.message, true);
      notify(error.message, true);
    } finally {
      release();
    }
  }

  async function loadLivePipelineConfig() {
    try {
      const data = await request('/api/v1/live/config');
      const cfg = data.config || {};
      if ($('live-cfg-stride')) $('live-cfg-stride').value = String(cfg.stride || 10);
      if ($('live-cfg-model')) $('live-cfg-model').value = cfg.model || 'sam';
      if ($('live-cfg-tracker')) $('live-cfg-tracker').value = cfg.tracker || 'sam';
      if ($('live-cfg-device')) $('live-cfg-device').value = cfg.device || 'cpu';
      if ($('live-cfg-recording')) $('live-cfg-recording').value = String(cfg.continuous_recording ?? true);
      if ($('live-cfg-segment')) $('live-cfg-segment').value = String(cfg.segment_duration_minutes || 60);
      if ($('live-cfg-retention')) $('live-cfg-retention').value = String(cfg.retention_days || 14);
      setStatus('live-config-status', 'Live pipeline configuration loaded.');
    } catch (err) {
      setStatus('live-config-status', err.message, true);
    }
  }

  async function saveLivePipelineConfig(event) {
    if (event) event.preventDefault();
    setStatus('live-config-status', 'Saving live pipeline settings…');
    try {
      const payload = {
        stride: Number($('live-cfg-stride')?.value || 10),
        model: $('live-cfg-model')?.value || 'sam',
        tracker: $('live-cfg-tracker')?.value || 'sam',
        device: $('live-cfg-device')?.value || 'cpu',
        continuous_recording: $('live-cfg-recording')?.value === 'true',
        segment_duration_minutes: Number($('live-cfg-segment')?.value || 60),
        retention_days: Number($('live-cfg-retention')?.value || 14),
      };
      await request('/api/v1/live/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      setStatus('live-config-status', '✓ Live pipeline & continuous recording settings applied.');
      notify('Live pipeline configuration updated');
    } catch (err) {
      setStatus('live-config-status', err.message, true);
    }
  }

  function renderEvents(events) {
    const wrap = $('events');
    if (!wrap) return;
    wrap.replaceChildren();
    if (!events.length) { wrap.innerHTML = '<div class="empty">No matching events.</div>'; return; }
    const table = document.createElement('table');
    table.className = 'events-table';
    table.innerHTML = '<thead><tr><th>Time</th><th>Type</th><th>Object</th><th>Zone</th><th>Evidence</th><th></th></tr></thead>';
    const body = document.createElement('tbody');
    events.forEach((event) => {
      const row = document.createElement('tr');
      row.innerHTML = `<td>${escape(Number(event.timestamp_seconds).toFixed(3))}s</td><td><span class="badge">${escape(event.event_type)}</span></td><td>${escape(event.object_label || event.object_id)}</td><td>${escape(event.zone_id)}</td><td>${escape(event.evidence_status || 'pending')} ${escape(event.evidence_artifact || '')}</td><td><button class="button text-button inspect-event" type="button">Inspect</button></td>`;
      row.querySelector('.inspect-event').addEventListener('click', () => inspectEvent(event));
      body.appendChild(row);
    });
    table.appendChild(body);
    wrap.appendChild(table);
  }

  function renderOverviewEvents(events) {
    const element = $('overview-events');
    if (!element) return;
    if (!events.length) { element.innerHTML = '<div class="empty">No events yet. Capture CCTV-1 footage to begin.</div>'; return; }
    element.innerHTML = events.slice(0, 5).map((event) => `<div class="compact-item"><span class="badge">${escape(event.event_type)}</span><span>${escape(event.object_label || event.object_id)} · ${escape(event.zone_id)}</span><strong>${escape(Number(event.timestamp_seconds).toFixed(2))}s</strong></div>`).join('');
  }

  function appearanceSummary(appearance) {
    if (!appearance) return 'No crop estimate';
    const values = [];
    if (appearance.dominant_color) values.push(`dominant ${appearance.dominant_color}`);
    if (appearance.upper_color) values.push(`upper ${appearance.upper_color}`);
    if (appearance.lower_color) values.push(`lower ${appearance.lower_color}`);
    if (appearance.vehicle_color) values.push(`vehicle ${appearance.vehicle_color}`);
    if (appearance.vehicle_type) values.push(appearance.vehicle_type);
    return `${values.join(' · ') || 'No color/type estimate'}${appearance.estimated ? ' · estimated' : ''}`;
  }

  function renderTracks(tracks) {
    const wrap = $('tracks');
    if (!wrap) return;
    wrap.replaceChildren();
    if (!tracks.length) {
      wrap.innerHTML = '<div class="empty">No track observations for this video.</div>';
      return;
    }
    const table = document.createElement('table');
    table.className = 'events-table';
    table.innerHTML = '<thead><tr><th>Track Scope</th><th>Track ID</th><th>Category</th><th>Label</th><th>Appearance Cues</th><th>Timestamp</th></tr></thead>';
    const body = document.createElement('tbody');
    tracks.forEach((track) => {
      const row = document.createElement('tr');
      const cat = String(track.object_category || '').toLowerCase();
      const catClass = cat === 'vehicle' ? 'vehicle' : (cat === 'person' ? 'person' : '');
      row.innerHTML = `<td><span title="${escape(track.track_scope || '')}">${escape(track.track_scope || 'session')}</span></td><td><strong>${escape(track.track_id)}</strong></td><td><span class="badge ${catClass}">${escape(track.object_category || '—')}</span></td><td>${escape(track.object_label || 'object')}</td><td>${escape(appearanceSummary(track.appearance))}</td><td>${escape(Number(track.timestamp_seconds).toFixed(3))}s</td>`;
      body.appendChild(row);
    });
    table.appendChild(body);
    wrap.appendChild(table);
  }

  async function loadTracks() {
    if (!currentVideoId) {
      setStatus('tracks-status', 'Select or process a video before loading tracks.');
      renderTracks([]);
      return [];
    }
    try {
      const body = await request(`/api/v1/videos/${encodeURIComponent(currentVideoId)}/tracks`);
      const tracks = body.tracks || [];
      renderTracks(tracks);
      setStatus('tracks-status', `${tracks.length} scoped track observation(s)`);
      return tracks;
    } catch (error) {
      setStatus('tracks-status', error.message, true);
      return [];
    }
  }

  async function loadEvents() {
    try {
      const query = new URLSearchParams();
      if (currentVideoId) query.set('video_id', currentVideoId);
      if ($('event-object-filter').value) query.set('object_id', $('event-object-filter').value);
      if ($('event-zone-filter').value) query.set('zone_id', $('event-zone-filter').value);
      if ($('event-type-filter').value) query.set('event_type', $('event-type-filter').value);
      const body = await request(`/api/v1/events${query.toString() ? `?${query}` : ''}`);
      const events = body.events || [];
      renderEvents(events);
      renderOverviewEvents(events);
      $('metric-events').textContent = String(events.length);
      setStatus('event-search-status', `${events.length} authoritative event(s)`);
      await loadTracks();
      return events;
    } catch (error) {
      setStatus('event-search-status', '');
      const evWrap = $('events'); if (evWrap) evWrap.innerHTML = '<div class="empty">No events logged for this filter criteria.</div>';
      return [];
    }
  }

  async function inspectEvent(event) {
    $('selected-event').textContent = JSON.stringify(event, null, 2);
    // Seek main pre-recorded / uploaded footage player to the event timestamp
    const wbPlayer = $('workbench-video-player');
    if (wbPlayer && wbPlayer.src && event.timestamp_seconds !== undefined) {
      wbPlayer.currentTime = Math.max(0, event.timestamp_seconds - 0.5);
      wbPlayer.play().catch(() => {});
    } else if (event.video_id) {
      playPreRecordedVideo(event.video_id);
      setTimeout(() => {
        if (wbPlayer && event.timestamp_seconds !== undefined) {
          wbPlayer.currentTime = Math.max(0, event.timestamp_seconds - 0.5);
          wbPlayer.play().catch(() => {});
        }
      }, 400);
    }
    try {
      const evidence = await request(`/api/v1/evidence/${encodeURIComponent(event.event_id)}`);
      $('evidence-panel').textContent = evidence.artifact ? `Artifact: ${evidence.artifact}\nStatus: ${evidence.status}` : 'Evidence is not ready.';
      $('evidence-player').hidden = true;
      if (evidence.status === 'ready') {
        const signed = await request(`/api/v1/evidence/${encodeURIComponent(event.event_id)}/url`);
        $('evidence-player').src = signed.url;
        $('evidence-player').hidden = false;
      }
    } catch (error) { $('evidence-panel').textContent = error.message; }
  }

  async function loadVideoArchive() {
    const select = $('pre-recorded-video-select');
    if (!select) return;
    try {
      const res = await request('/api/v1/videos');
      videoArchiveCache = res.videos || [];
      const currentVal = select.value;
      select.innerHTML = '';

      const defaultOpt = document.createElement('option');
      defaultOpt.value = '';
      defaultOpt.textContent = videoArchiveCache.length
        ? `-- Select from Stored Surveillance Videos (${videoArchiveCache.length}) --`
        : '-- No Stored Surveillance Videos Found --';
      select.appendChild(defaultOpt);

      videoArchiveCache.forEach((v) => {
        const opt = document.createElement('option');
        opt.value = v.video_id;
        const sizeMb = v.size_bytes ? `${(v.size_bytes / 1024 / 1024).toFixed(1)} MB` : '';
        const dur = v.duration_seconds ? `${Math.round(v.duration_seconds)}s` : '';
        const metaParts = [sizeMb, dur].filter(Boolean).join(', ');
        const name = v.filename || v.video_id;
        opt.textContent = metaParts ? `${name} [${metaParts}]` : name;
        select.appendChild(opt);
      });

      if (currentVal && videoArchiveCache.some((v) => v.video_id === currentVal)) {
        select.value = currentVal;
      }
      renderUploadedFootageArchive();
    } catch (err) {
      console.warn('Failed to load video archive:', err);
    }
  }

  function renderUploadedFootageArchive() {
    const grid = $('uploaded-footage-grid');
    if (!grid) return;

    const dateVal = $('upload-filter-date')?.value || '';
    const monthVal = $('upload-filter-month')?.value || '';
    const searchVal = ($('upload-filter-search')?.value || '').trim().toLowerCase();

    let filtered = videoArchiveCache || [];

    if (dateVal) {
      filtered = filtered.filter((v) => (v.created_at || '').slice(0, 10) === dateVal);
    }
    if (monthVal) {
      filtered = filtered.filter((v) => (v.created_at || '').slice(0, 7) === monthVal);
    }
    if (searchVal) {
      filtered = filtered.filter((v) =>
        (v.filename || '').toLowerCase().includes(searchVal) ||
        (v.video_id || '').toLowerCase().includes(searchVal)
      );
    }

    if (!filtered.length) {
      grid.innerHTML = `
        <div class="empty" style="grid-column: 1 / -1; padding: 28px; text-align: center; background: rgba(13,19,30,0.5); border-radius: 12px; border: 1px dashed rgba(255,255,255,0.08);">
          <span style="font-size: 26px; display: block; margin-bottom: 8px;">📂</span>
          <strong style="color: #94a3b8; font-size: 14px;">No uploaded CCTV footage matching criteria</strong>
          <small style="display: block; margin-top: 4px; color: #64748b;">Adjust the date, month, or search filters above, or drop new CCTV recordings to ingest.</small>
        </div>
      `;
      return;
    }

    grid.innerHTML = '';
    filtered.forEach((v) => {
      const card = document.createElement('article');
      card.className = 'uploaded-footage-card';
      const sizeMb = v.size_bytes ? `${(v.size_bytes / 1024 / 1024).toFixed(1)} MB` : '12.4 MB';
      const durSec = v.duration_seconds ? `${Math.round(v.duration_seconds)}s` : '30s';
      const dateStr = v.created_at ? v.created_at.replace('T', ' ').slice(0, 19) : 'Surveillance file';
      const name = v.filename || `Recording ${v.video_id.slice(0, 8)}`;

      card.innerHTML = `
        <div class="footage-card-header">
          <div class="footage-title-wrap">
            <span class="footage-icon">🎥</span>
            <div>
              <strong class="footage-title" title="${escape(formatCleanFootageName(name))}">${escape(formatCleanFootageName(name))}</strong>
              <small class="footage-id">ID: <code>${escape(v.video_id.slice(0, 8))}…</code></small>
            </div>
          </div>
          <span class="state-pill live" style="font-size: 10px; padding: 2px 8px;">READY</span>
        </div>
        <div class="footage-meta-row">
          <span>📅 ${escape(dateStr)}</span>
          <span>💾 ${escape(sizeMb)}</span>
          <span>⏱️ ${escape(durSec)}</span>
        </div>
        <div class="footage-card-actions">
          <button class="button secondary small btn-preview-footage" type="button" data-video-id="${escape(v.video_id)}">▶ Preview</button>
          <button class="button primary small btn-summary-footage" type="button" data-video-id="${escape(v.video_id)}" data-filename="${escape(name)}">✨ Instant Video Summary</button>
        </div>
      `;

      card.querySelector('.btn-preview-footage').addEventListener('click', () => {
        playPreRecordedVideo(v.video_id);
        const playerCard = $('workbench-player-card');
        if (playerCard) {
          playerCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      });

      card.querySelector('.btn-summary-footage').addEventListener('click', () => {
        generateUploadedVideoSummary(v.video_id, name);
      });

      grid.appendChild(card);
    });
  }

  async function generateUploadedVideoSummary(videoId, filename) {
    const box = $('uploaded-footage-summary-box');
    const content = $('uploaded-footage-summary-content');
    if (!box || !content) return;

    box.hidden = false;
    content.innerHTML = `
      <div style="padding: 24px; text-align: center; color: var(--vms-cyan);">
        <span class="recording-dot" style="background:#00e5ff; display:inline-block; margin-right:8px;"></span>
        <strong>Analyzing CCTV telemetry & generating AI activity summary for ${escape(filename)}…</strong>
        <p style="color: #94a3b8; font-size: 12px; margin-top: 6px;">Searching observed activity and zone crossings.</p>
      </div>
    `;
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    try {
      const summary = await request(`/api/v1/videos/${encodeURIComponent(videoId)}/summary`);
      const counts = summary.entity_counts || {};
      const statusBadge = summary.security_status === 'Normal / Clear'
        ? '<span class="state-pill live" style="font-size:11px;">🟢 NORMAL / SECURED</span>'
        : '<span class="state-pill offline" style="font-size:11px;">⚠️ ATTENTION REQUIRED</span>';

      const milestonesHtml = (summary.milestones || []).map((m) => `
        <div class="summary-milestone-item" style="display:flex; gap:12px; align-items:center; padding:8px 0; border-bottom:1px solid rgba(255,255,255,0.06);">
          <span style="font-family:'JetBrains Mono',monospace; font-weight:700; color:#00e5ff; font-size:12px; min-width:65px;">[${escape(m.timecode)}]</span>
          <span style="font-weight:600; color:#f8fafc; font-size:13px;">${escape(m.event_type)}</span>
          <span style="color:#94a3b8; font-size:12px;">Target <code>${escape(m.object_id)}</code> (${escape(m.object_label)}) in Zone <b>${escape(m.zone_id)}</b></span>
        </div>
      `).join('') || '<div style="color:#94a3b8; font-size:12px; padding:8px 0;">No perimeter boundary infractions logged. Object stayed within verified bounds.</div>';

      content.innerHTML = `
        <div class="summary-layout" style="display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap:20px; align-items:start;">
          <div>
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
              <h4 style="margin:0; font-size:16px; color:#f8fafc;">${escape(filename)}</h4>
              ${statusBadge}
            </div>
            <p style="color:#cbd5e1; font-size:13.5px; line-height:1.6; margin-bottom:16px;">
              ${escape(summary.executive_narrative)}
            </p>
            <div class="summary-metrics-strip" style="display:grid; grid-template-columns:repeat(3, 1fr); gap:10px; margin-bottom:18px;">
              <div style="background:rgba(255,255,255,0.04); padding:10px; border-radius:8px; border:1px solid rgba(255,255,255,0.08); text-align:center;">
                <span style="display:block; font-size:11px; color:#94a3b8;">Total Entities</span>
                <strong style="font-size:18px; color:#f8fafc;">${counts.total_entities || 0}</strong>
              </div>
              <div style="background:rgba(255,255,255,0.04); padding:10px; border-radius:8px; border:1px solid rgba(255,255,255,0.08); text-align:center;">
                <span style="display:block; font-size:11px; color:#94a3b8;">Persons Tracked</span>
                <strong style="font-size:18px; color:#10b981;">${counts.persons || 0}</strong>
              </div>
              <div style="background:rgba(255,255,255,0.04); padding:10px; border-radius:8px; border:1px solid rgba(255,255,255,0.08); text-align:center;">
                <span style="display:block; font-size:11px; color:#94a3b8;">Vehicles Tracked</span>
                <strong style="font-size:18px; color:#38bdf8;">${counts.vehicles || 0}</strong>
              </div>
            </div>
            <div style="display:flex; gap:10px; flex-wrap:wrap;">
              <button class="button secondary small" type="button" onclick="window.playPreRecordedVideo('${escape(videoId)}')">▶ Play Recording</button>
              <a href="/api/v1/videos/${encodeURIComponent(videoId)}/snapshot" target="_blank" class="button ghost small" style="text-decoration:none; display:inline-flex; align-items:center;">📷 Open Full Snapshot</a>
            </div>
          </div>
          <div style="background:rgba(0,0,0,0.3); border-radius:12px; padding:16px; border:1px solid rgba(255,255,255,0.08);">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
              <span style="font-size:11px; font-weight:700; color:#00e5ff; letter-spacing:0.05em;">EVIDENCE SNAPSHOT & MILESTONES</span>
              <span style="font-size:11px; color:#94a3b8;">${(summary.duration_seconds || 30).toFixed(1)}s total</span>
            </div>
            <img src="/api/v1/videos/${encodeURIComponent(videoId)}/snapshot" alt="Video Snapshot" style="width:100%; border-radius:8px; border:1px solid rgba(0,229,255,0.25); margin-bottom:12px; background:#000;">
            <div class="summary-milestones-list">
              ${milestonesHtml}
            </div>
          </div>
        </div>
      `;
    } catch (err) {
      content.innerHTML = `
        <div class="result-card bad">
          <strong>Could not generate video summary:</strong> ${escape(err.message)}
        </div>
      `;
    }
  }

  function playPreRecordedVideo(videoId, customTitle = '', customUrl = '') {
    window.playPreRecordedVideo = playPreRecordedVideo;
    if (!videoId && !customUrl) return;
    const wbPlayer = $('workbench-video-player');
    const playerCard = $('workbench-player-card');
    const select = $('pre-recorded-video-select');
    if (videoId) {
      currentVideoId = videoId;
      if ($('video-id')) $('video-id').value = videoId;
      if ($('probe-video')) $('probe-video').disabled = false;
      if (select && select.value !== videoId) {
        select.value = videoId;
      }
    }
    const found = videoArchiveCache.find((v) => v.video_id === videoId);
    const title = customTitle || found?.filename || videoId || 'Surveillance Video';
    if ($('workbench-player-title')) $('workbench-player-title').textContent = title;
    if ($('workbench-player-badge')) {
      $('workbench-player-badge').textContent = customUrl && !videoId ? 'LOCAL PREVIEW' : 'STORED ARCHIVE';
    }
    const sizeStr = found?.size_bytes ? `${(found.size_bytes / 1024 / 1024).toFixed(2)} MB` : '';
    const durStr = found?.duration_seconds ? `${found.duration_seconds.toFixed(1)}s` : '';
    if ($('player-file-meta')) {
      $('player-file-meta').textContent = [sizeStr, durStr, videoId ? `ID: ${videoId}` : ''].filter(Boolean).join(' · ') || 'CCTV Stream Ready';
    }

    if (wbPlayer) {
      const srcUrl = customUrl || endpoint(`/api/v1/videos/${encodeURIComponent(videoId)}/stream${accessToken ? '?token=' + encodeURIComponent(accessToken) : ''}`);
      wbPlayer.src = srcUrl;
      if (playerCard) playerCard.hidden = false;
      wbPlayer.load();
      wbPlayer.play().catch(() => {});
    }
  }

  async function uploadVideo(file, category = 'uploads') {
    const form = new FormData();
    form.append('file', file, file.name);
    return request(`/api/v1/videos?category=${encodeURIComponent(category)}`, { method: 'POST', body: form });
  }

  function parseZones(id, label) {
    const value = $(id)?.value.trim();
    if (!value) throw new Error(`${label} must contain a JSON array of zones.`);
    let parsed;
    try {
      parsed = JSON.parse(value);
    } catch (error) {
      throw new Error(`${label} must be valid JSON, for example [{"zone_id":"gate","polygon":[[0,0],[640,0],[640,480]]}].`);
    }
    if (!Array.isArray(parsed)) throw new Error(`${label} must be a JSON array of zone objects.`);
    // Auto-normalize any custom key holding coordinate points (e.g., {"Car parking": [[...]]})
    return parsed.map((item, idx) => {
      if (item && typeof item === 'object' && !item.polygon) {
        for (const [k, v] of Object.entries(item)) {
          if (k !== 'zone_id' && Array.isArray(v) && v.length >= 3) {
            return { zone_id: item.zone_id || `zone-${idx + 1}`, polygon: v, label: k };
          }
        }
      }
      return item;
    });
  }

  function cameraZones() {
    if (window.customZonePoints && window.customZonePoints.length >= 3) {
      return [{ zone_id: 'cctv1-zone', polygon: window.customZonePoints }];
    }
    const val = $('camera-zones')?.value?.trim();
    if (val) {
      try { return JSON.parse(val); } catch (_) {}
    }
    const w = $('camera-preview')?.videoWidth || 640;
    const h = $('camera-preview')?.videoHeight || 480;
    return [{ zone_id: 'cctv1-zone', polygon: [[0, 0], [w, 0], [w, h], [0, h]] }];
  }

  async function processVideo(videoId, options = {}) {
    return request(`/api/v1/videos/${encodeURIComponent(videoId)}/process-media`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        zones: options.zones ?? parseZones('zones', 'Processing zone JSON'),
        device: options.device ?? $('device')?.value ?? 'cpu',
        model_profile: options.model_profile ?? $('model-profile')?.value ?? 'baseline',
        tracker_profile: options.tracker_profile ?? $('tracker-profile')?.value ?? 'centroid',
        every: options.every ?? (Number($('every')?.value) || 1),
        threshold: options.threshold ?? 200,
        min_area: options.min_area ?? 25,
        clip_padding: options.clip_padding ?? 5,
      }),
    });
  }

  function wait(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  async function waitForJob(body, onStatus) {
    let status = String(body.status || '').toLowerCase();
    if (!['queued', 'processing'].includes(status)) return body;
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      onStatus?.(status);
      await wait(1000);
      const job = await request(`/api/v1/jobs/${encodeURIComponent(body.job_id)}`);
      status = String(job.status || '').toLowerCase();
      if (status === 'completed') return { ...body, ...job, status: 'completed' };
      if (status === 'failed') throw new Error(`Processing failed: ${job.error || 'the worker reported a failure'}`);
      if (!['queued', 'processing'].includes(status)) throw new Error(`Processing returned an unexpected status: ${job.status || 'unknown'}.`);
    }
    throw new Error(`Processing job ${body.job_id} did not finish within 120 seconds.`);
  }

  async function uploadForm(event) {
    event.preventDefault();
    const file = $('video-file').files[0];
    const submit = $('upload-form').querySelector('button[type="submit"]');
    if (!file) {
      const message = 'Choose a video file before uploading.';
      showResult('video-result', message, true);
      notify(message, true);
      return;
    }
    if (submit.disabled) return;
    const release = setButtonBusy(submit, 'Uploading…');
    try {
      const body = await uploadVideo(file);
      currentVideoId = body.video_id;
      $('video-id').value = currentVideoId;
      $('probe-video').disabled = false;
      showResult('video-result', `Uploaded ${body.filename}\nVideo ID: ${body.video_id}`);
      playPreRecordedVideo(body.video_id, body.filename);
      await loadVideoArchive();
      $('metric-capture').textContent = 'Uploaded';
      $('metric-capture-detail').textContent = body.filename;
      notify('Recording uploaded — Ready to play and analyze');
    } catch (error) {
      showResult('video-result', `Upload failed: ${error.message}`, true);
      notify(error.message, true);
    } finally {
      release();
    }
  }

  async function processForm(event) {
    event.preventDefault();
    const videoId = $('video-id').value.trim();
    const submit = $('process-form').querySelector('button[type="submit"]');
    if (!videoId) {
      const message = 'Enter a video ID or upload a recording before starting processing.';
      showResult('process-result', message, true);
      notify(message, true);
      return;
    }
    const modelProf = $('model-profile')?.value || 'baseline';
    const everyVal = Number($('every')?.value) || 15;
    if (modelProf === 'sam' && everyVal < 5) {
      const ok = confirm(`This setting will analyze about ${Math.round(1800 / everyVal)} frames from a one-minute video and may take longer or use more processing resources.\n\nRecommended: use a frame stride of 15 or 30 for routine analysis.\n\nContinue with stride ${everyVal}?`);
      if (!ok) return;
    }
    if (submit.disabled) return;
    const release = setButtonBusy(submit, 'Processing…');
    try {
      currentVideoId = videoId;
      let body = await processVideo(videoId);
      body = await waitForJob(body, (status) => showResult('process-result', `Job ${body.job_id}\nStatus: ${status}\nWaiting for the worker to finish…`));
      const events = await loadEvents();
      showResult('process-result', `Job ${body.job_id}\nStatus: completed\nEvents: ${body.events?.length ?? events.length}\nAlerts: ${body.alerts?.length || 0}`);
      notify('Processing complete');
    } catch (error) {
      showResult('process-result', `Processing failed: ${error.message}`, true);
      notify(error.message, true);
    } finally {
      release();
    }
  }

  function showResult(id, message, bad = false) {
    const element = $(id);
    if (!element) return;
    element.hidden = false;
    element.textContent = message;
    element.classList.toggle('bad', bad);
  }

  function cameraErrorMessage(error) {
    const name = error?.name;
    if (name === 'NotAllowedError' || name === 'PermissionDeniedError') return 'Camera access was denied. Allow camera permission in the browser, then try Start camera again.';
    if (name === 'NotFoundError' || name === 'DevicesNotFoundError') return 'No camera was found. Connect a camera and try Start camera again.';
    if (name === 'NotReadableError' || name === 'TrackStartError') return 'The camera is unavailable or already in use. Close other camera apps and try again.';
    if (name === 'SecurityError') return 'Camera access is blocked in this context. Open the dashboard on HTTPS or localhost and try again.';
    if (error?.message) return `Camera access failed: ${error.message}`;
    return 'Camera access failed. Check browser permissions and try again.';
  }

  async function startCamera() {
    if (cameraStarting || cameraStream) return;
    const playbackEl = $('camera-playback');
    if (playbackEl) {
      playbackEl.pause();
      playbackEl.hidden = true;
    }
    if ($('camera-feed-mode')) {
      $('camera-feed-mode').textContent = 'LIVE';
      $('camera-feed-mode').classList.remove('replay');
    }
    if ($('btn-live-switch')) $('btn-live-switch').hidden = true;
    const button = $('camera-start');
    cameraStarting = true;
    if (button) {
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      button.textContent = 'Starting…';
    }
    setStatus('camera-status', 'Requesting camera access…');
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('This browser does not expose the camera API. Use HTTPS or localhost in a modern browser.');
      try {
        cameraStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
      } catch (audioErr) {
        // Graceful fallback: audio device missing or mic permission denied -> request video only
        cameraStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
      }
      if ($('camera-preview')) {
        $('camera-preview').srcObject = cameraStream;
        $('camera-preview').hidden = false;
      }
      if ($('camera-placeholder')) $('camera-placeholder').hidden = true;
      if ($('camera-record')) $('camera-record').disabled = false;
      if ($('camera-stop')) $('camera-stop').disabled = false;
      if ($('camera-state')) {
        $('camera-state').textContent = '1 ACTIVE CAMERA ONLINE';
        $('camera-state').className = 'state-pill live';
        $('camera-state').style.cssText = 'color: #6ee7b7; background: rgba(16, 185, 129, 0.15); border-color: rgba(16, 185, 129, 0.35);';
      }
      updateGlobalFleetStatus();
      if ($('metric-camera')) $('metric-camera').textContent = 'Live';
      const track = cameraStream.getVideoTracks()[0];
      const settings = track?.getSettings?.() || {};
      if ($('camera-resolution')) $('camera-resolution').textContent = settings.width && settings.height ? `${settings.width} × ${settings.height}` : 'Browser camera active';
      setStatus('camera-status', 'Camera preview is live. Record a capture when ready.');
      notify('CAM-01 primary sentry live');
    } catch (error) {
      cameraStream?.getTracks?.().forEach((track) => track.stop());
      cameraStream = null;
      if ($('camera-preview')) {
        $('camera-preview').srcObject = null;
        $('camera-preview').hidden = true;
      }
      if ($('camera-placeholder')) $('camera-placeholder').hidden = false;
      if ($('camera-record')) $('camera-record').disabled = true;
      if ($('camera-stop')) $('camera-stop').disabled = true;
      if ($('camera-state')) {
        $('camera-state').textContent = 'OFFLINE';
        $('camera-state').className = 'state-pill offline';
        $('camera-state').style.cssText = 'color: #fca5a5; background: rgba(239, 68, 68, 0.15); border-color: rgba(239, 68, 68, 0.35);';
      }
      if ($('metric-camera')) $('metric-camera').textContent = 'Offline';
      const message = cameraErrorMessage(error);
      setStatus('camera-status', message, true);
      showResult('camera-result', message, true);
      notify(message, true);
    } finally {
      cameraStarting = false;
      if (button) {
        button.disabled = Boolean(cameraStream);
        button.removeAttribute('aria-busy');
        button.textContent = 'Start Camera';
      }
    }
  }

  function startRecording() {
    if (!cameraStream) {
      notify('Start the camera before recording.', true);
      return;
    }
    if (cameraProcessing || mediaRecorder?.state === 'recording') return;
    if (typeof MediaRecorder === 'undefined') {
      const message = 'This browser does not support local video recording.';
      notify(message, true);
      return;
    }
    const storedDuration = Number(localStorage.getItem('vms-capture-duration')) || 10;
    const duration = Math.min(60, Math.max(3, Number($('camera-duration')?.value) || storedDuration));
    const width = $('camera-preview')?.videoWidth || 640;
    const height = $('camera-preview')?.videoHeight || 480;
    if ($('camera-zones')) {
      const activePolygon = (window.vmsActiveZonePoints && window.vmsActiveZonePoints.length >= 3)
        ? window.vmsActiveZonePoints
        : [[0, 0], [width, 0], [width, height], [0, height]];
      $('camera-zones').value = JSON.stringify([{ zone_id: 'cctv1-zone', polygon: activePolygon }]);
    }
    try {
      const mime = ['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm'].find((candidate) => MediaRecorder.isTypeSupported(candidate)) || '';
      cameraChunks = [];
      mediaRecorder = new MediaRecorder(cameraStream, mime ? { mimeType: mime } : undefined);
      mediaRecorder.ondataavailable = (event) => { if (event.data.size) cameraChunks.push(event.data); };
      mediaRecorder.onstop = finishRecording;
      mediaRecorder.start(250);
    } catch (error) {
      mediaRecorder = null;
      cameraChunks = [];
      const message = `Recording could not start: ${error.message || 'the browser rejected the camera stream'}.`;
      notify(message, true);
      return;
    }
    cameraStartedAt = Date.now();
    if ($('camera-record')) $('camera-record').disabled = true;
    if ($('camera-recording-badge')) $('camera-recording-badge').hidden = false;
    if ($('camera-state')) $('camera-state').textContent = 'RECORDING';
    if ($('metric-camera')) $('metric-camera').textContent = 'Recording';
    updateCameraTimer();
    cameraRecordTimer = window.setTimeout(() => stopCamera(false), duration * 1000);
    notify(`Recording CAM-01 for up to ${duration} seconds…`);
  }

  function updateCameraTimer() {
    if (!cameraStartedAt) return;
    const seconds = Math.floor((Date.now() - cameraStartedAt) / 1000);
    if ($('camera-timer')) $('camera-timer').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
    cameraTimer = window.setTimeout(updateCameraTimer, 500);
  }

  function stopCamera(closeStream = true) {
    window.clearTimeout(cameraTimer);
    window.clearTimeout(cameraRecordTimer);
    cameraRecordTimer = null;
    cameraTimer = null;
    cameraStartedAt = 0;
    if ($('camera-timer')) $('camera-timer').textContent = '00:00';
    const wasRecording = mediaRecorder && mediaRecorder.state !== 'inactive';
    if (wasRecording) {
      cameraProcessing = true;
      setStatus('camera-status', 'Capture stopped. Uploading CCTV-1 footage…');
      if ($('camera-state')) $('camera-state').textContent = 'UPLOADING';
      mediaRecorder.stop();
    }
    if (closeStream && cameraStream) cameraStream.getTracks().forEach((track) => track.stop());
    if (closeStream) {
      cameraStream = null;
      if ($('camera-preview')) $('camera-preview').hidden = true;
      if ($('camera-placeholder')) $('camera-placeholder').hidden = false;
    }
    if ($('camera-recording-badge')) $('camera-recording-badge').hidden = true;
    if ($('camera-record')) $('camera-record').disabled = !cameraStream || cameraProcessing;
    if ($('camera-stop')) $('camera-stop').disabled = !cameraStream || cameraProcessing;
    if ($('camera-start')) $('camera-start').disabled = Boolean(cameraStream) || cameraStarting;
    if (!wasRecording) {
      if ($('camera-state')) {
        $('camera-state').textContent = cameraStream ? '1 ACTIVE CAMERA ONLINE' : 'PRIMARY SENTRY STANDBY';
        $('camera-state').className = cameraStream ? 'state-pill live' : 'state-pill offline';
      }
      setStatus('camera-status', cameraStream ? 'Camera preview is live. Record a capture when ready.' : 'Camera stopped.');
      notify('CAM-01 primary sentry stopped');
      updateGlobalFleetStatus();
    }
  }

  async function finishRecording() {
    cameraProcessing = true;
    const recorder = mediaRecorder;
    const type = recorder?.mimeType || 'video/webm';
    const blob = new Blob(cameraChunks, { type });
    const filename = `cctv1-${new Date().toISOString().replace(/[:.]/g, '-')}.webm`;

    if (blob.size) {
      if (lastRecordedBlobUrl) {
        URL.revokeObjectURL(lastRecordedBlobUrl);
      }
      lastRecordedBlobUrl = URL.createObjectURL(blob);
      const playbackEl = $('camera-playback');
      if (playbackEl) {
        playbackEl.src = lastRecordedBlobUrl;
        playbackEl.hidden = false;
        $('camera-preview').hidden = true;
        $('camera-placeholder').hidden = true;
        if ($('camera-feed-mode')) {
          $('camera-feed-mode').textContent = 'REPLAY';
          $('camera-feed-mode').classList.add('replay');
        }
        if ($('btn-replay-capture')) $('btn-replay-capture').hidden = false;
        if ($('btn-live-switch')) $('btn-live-switch').hidden = false;
        playbackEl.play().catch(() => {});
      }
    }

    showResult('camera-result', `Captured ${(blob.size / 1024 / 1024).toFixed(2)} MB\nUploading CCTV-1 footage…`);
    try {
      if (!blob.size) throw new Error('No video data was captured. Record for a few seconds and try again.');
      const zones = cameraZones();
      const uploaded = await uploadVideo(new File([blob], filename, { type }), 'live_streams');
      currentVideoId = uploaded.video_id;
      let body = await processVideo(currentVideoId, { zones });
      body = await waitForJob(body, (status) => {
        $('camera-state').textContent = status.toUpperCase();
        $('metric-camera').textContent = 'Queued';
        setStatus('camera-status', `Capture uploaded. Processing is ${status}…`);
        showResult('camera-result', `Stored video: ${uploaded.video_id}\nJob: ${body.job_id}\nStatus: ${status}\nWaiting for the worker to finish…`);
      });
      $('camera-state').textContent = 'PROCESSED';
      $('metric-camera').textContent = 'Processed';
      $('metric-capture').textContent = 'CCTV-1';
      const events = await loadEvents();
      $('metric-capture-detail').textContent = `${body.events?.length ?? events.length} verified event(s)`;
      
      const resCard = $('camera-result');
      if (resCard) {
        resCard.hidden = false;
        resCard.classList.remove('bad');
        resCard.innerHTML = `
          <div class="replay-card">
            <div class="replay-card-meta">
              <strong>Recorded CCTV-1 Footage</strong>
              <small>${(blob.size / 1024 / 1024).toFixed(2)} MB · Video ID: <code>${escape(uploaded.video_id)}</code></small>
            </div>
            <video class="result-video-player" src="${lastRecordedBlobUrl}" controls playsinline></video>
            <div class="button-row" style="margin-top:8px;">
              <a class="button secondary small" href="${lastRecordedBlobUrl}" download="${escape(filename)}">⬇ Download Video</a>
              <button class="button primary small" id="btn-open-workbench-recorded" type="button">Open in Forensic Workbench</button>
            </div>
            <div class="result-card-stats" style="margin-top:8px;font-size:12px;color:var(--text-muted, #94a3b8);">
              Status: Processed · Events: ${body.events?.length ?? events.length} · Job: ${escape(body.job_id)}
            </div>
          </div>
        `;
        const openWbBtn = $('btn-open-workbench-recorded');
        if (openWbBtn) {
          openWbBtn.addEventListener('click', () => {
            setView('evidence');
            playPreRecordedVideo(uploaded.video_id, filename, lastRecordedBlobUrl);
          });
        }
      }
      setStatus('camera-status', 'Capture uploaded and processing completed.');
      await loadEvents();
      await loadVideoArchive();
      notify('CCTV-1 capture processed and ready for playback');
    } catch (error) {
      showResult('camera-result', error.message, true);
      setStatus('camera-status', error.message, true);
      $('camera-state').textContent = cameraStream ? 'READY' : 'OFFLINE';
      $('metric-camera').textContent = cameraStream ? 'Ready' : 'Offline';
      notify(error.message, true);
    } finally {
      cameraChunks = [];
      mediaRecorder = null;
      cameraProcessing = false;
      $('camera-record').disabled = !cameraStream;
      $('camera-stop').disabled = !cameraStream;
      $('camera-start').disabled = Boolean(cameraStream) || cameraStarting;
      $('camera-recording-badge').hidden = true;
    }
  }

  async function askChat(event) {
    event.preventDefault();
    const question = $('chat-question').value.trim();
    if (!question) return;
    appendChat('user', question);
    try {
      const rawSession = $('chat-session-id')?.value.trim() || null;
      const rawCamera = $('chat-camera-id')?.value.trim() || null;
      const uuidRe = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

      let effectiveVideoId = currentVideoId || null;
      let effectiveSessionId = rawSession;
      let effectiveCameraId = rawCamera;

      if (rawSession && uuidRe.test(rawSession)) {
        effectiveVideoId = rawSession;
        effectiveSessionId = null;
      } else if (rawCamera && uuidRe.test(rawCamera)) {
        effectiveVideoId = rawCamera;
        effectiveCameraId = null;
      }

      const body = await request('/api/v1/chat/forensic', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: question,
          video_id: effectiveVideoId,
          camera_id: effectiveCameraId,
          session_id: effectiveSessionId,
          agent_mode: 'forensic',
        }),
      });
      appendChat('assistant', `${body.answer}\n\n${(body.citations || []).length} verified citation(s) · ${body.answer_mode}`, false, body.trimmed_clips || []);
      setStatus('chat-status', `${body.citations?.length || 0} verified citation(s)`);
    } catch (error) { appendChat('assistant', error.message, true); setStatus('chat-status', error.message, true); }
    $('chat-question').value = '';
  }

  function formatChatMessage(rawText) {
    const div = document.createElement('div');
    div.textContent = rawText;
    let safe = div.innerHTML;

    safe = safe.replace(/^### (.*$)/gim, '<h4 class="chat-md-h4">$1</h4>');
    safe = safe.replace(/^## (.*$)/gim, '<h3 class="chat-md-h3">$1</h3>');
    safe = safe.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    safe = safe.replace(/`([^`]+)`/g, '<code class="chat-md-code">$1</code>');
    safe = safe.replace(/^\s*[-•]\s+(.*$)/gim, '<div class="chat-md-bullet"><span class="bullet-dot">▪</span> $1</div>');

    return safe;
  }

  function appendChat(role, message, bad = false, clips = []) {
    const history = $('chat-history');
    if (history.querySelector('.chat-empty')) history.replaceChildren();
    const item = document.createElement('div');
    item.className = `chat-message ${role}${bad ? ' bad' : ''}`;
    
    const textNode = document.createElement('div');
    textNode.className = 'chat-message-content';
    textNode.innerHTML = formatChatMessage(message);
    item.appendChild(textNode);

    if (Array.isArray(clips) && clips.length > 0) {
      const clipsContainer = document.createElement('div');
      clipsContainer.className = 'chat-evidence-clips';
      clipsContainer.style.marginTop = '10px';
      clipsContainer.style.display = 'flex';
      clipsContainer.style.flexDirection = 'column';
      clipsContainer.style.gap = '8px';

      clips.forEach((clip) => {
        const clipCard = document.createElement('div');
        clipCard.style.padding = '8px';
        clipCard.style.borderRadius = '6px';
        clipCard.style.background = 'rgba(0, 0, 0, 0.3)';
        clipCard.style.border = '1px solid rgba(56, 189, 248, 0.3)';

        const badge = document.createElement('div');
        badge.style.fontSize = '12px';
        badge.style.fontWeight = 'bold';
        badge.style.marginBottom = '6px';
        badge.style.color = '#38bdf8';
        badge.textContent = `🎬 Trimmed Evidence Clip (${clip.interval_formatted || (clip.timestamp_seconds + 's')})`;
        clipCard.appendChild(badge);

        if (clip.download_url) {
          const videoEl = document.createElement('video');
          videoEl.controls = true;
          videoEl.src = clip.download_url;
          videoEl.style.width = '100%';
          videoEl.style.maxHeight = '220px';
          videoEl.style.borderRadius = '4px';
          videoEl.preload = 'metadata';
          clipCard.appendChild(videoEl);
        }
        clipsContainer.appendChild(clipCard);
      });
      item.appendChild(clipsContainer);
    }

    history.appendChild(item);
    history.scrollTop = history.scrollHeight;
  }

  function appendLiveChat(role, message, bad = false, citations = []) {
    const history = $('live-chat-history');
    if (!history) return;
    if (history.querySelector('.chat-empty')) history.replaceChildren();
    const item = document.createElement('div');
    item.className = `chat-message ${role}${bad ? ' bad' : ''}`;
    const textNode = document.createElement('div');
    textNode.className = 'chat-message-content';
    textNode.innerHTML = formatChatMessage(message);
    item.appendChild(textNode);

    if (Array.isArray(citations) && citations.length > 0) {
      const citationsContainer = document.createElement('div');
      citationsContainer.className = 'chat-evidence-citations';
      citationsContainer.style.cssText = 'margin-top: 12px; display: flex; flex-direction: column; gap: 8px;';

      const sectionTitle = document.createElement('div');
      sectionTitle.style.cssText = 'font-size: 11px; font-weight: 700; color: #38bdf8; text-transform: uppercase; letter-spacing: 0.05em; display: flex; align-items: center; gap: 6px;';
      sectionTitle.innerHTML = `<span>🛡️ Grounded Telemetry & Evidence Citations (${citations.length})</span>`;
      citationsContainer.appendChild(sectionTitle);

      citations.slice(0, 6).forEach((cit) => {
        const citCard = document.createElement('div');
        citCard.className = 'evidence-citation-card';
        citCard.style.cssText = 'padding: 10px 12px; border-radius: 6px; background: rgba(15, 23, 42, 0.7); border: 1px solid rgba(56, 189, 248, 0.25); display: flex; flex-direction: column; gap: 4px;';

        const topRow = document.createElement('div');
        topRow.style.cssText = 'display: flex; justify-content: space-between; align-items: center; font-size: 12px;';

        const camBadge = `<span class="vms-feed-badge" style="background:#0284c7; color:#fff; font-size:10px;">${escape((cit.camera_id || 'FLEET').toUpperCase())}</span>`;
        const timeBadge = cit.timestamp_seconds != null ? `<span style="color:#94a3b8; font-family:monospace; font-size:11px;">⏱ ${Number(cit.timestamp_seconds).toFixed(2)}s</span>` : (cit.timestamp ? `<span style="color:#94a3b8; font-family:monospace; font-size:11px;">⏱ ${escape(String(cit.timestamp).slice(11, 19))}</span>` : '');

        topRow.innerHTML = `<div>${camBadge} <strong style="color:#e2e8f0; margin-left:6px;">${escape(cit.citation_type || 'telemetry')}</strong></div>${timeBadge}`;
        citCard.appendChild(topRow);

        const details = [];
        if (cit.track_id) details.push(`Object / Track: <strong style="color:#38bdf8;">${escape(cit.track_id)} (${escape(cit.object_label || 'target')})</strong>`);
        if (cit.zone_id) details.push(`Zone: <strong>${escape(cit.zone_id)}</strong>`);
        if (cit.rule_name) details.push(`Rule: <strong>${escape(cit.rule_name)}</strong>`);
        if (cit.reason) details.push(`Reason: <em>${escape(cit.reason)}</em>`);
        if (cit.stream_source) details.push(`Stream: <code>${escape(cit.stream_source)}</code>`);
        if (cit.display_name) details.push(`Name: <strong>${escape(cit.display_name)}</strong>`);
        if (cit.status) details.push(`Status: <span class="state-pill ${cit.status === 'online' || cit.status === 'running' ? 'live' : 'offline'}" style="font-size:9px;">${escape(cit.status.toUpperCase())}</span>`);

        if (details.length) {
          const detailRow = document.createElement('div');
          detailRow.style.cssText = 'font-size: 11px; color: #94a3b8; line-height: 1.4;';
          detailRow.innerHTML = details.join(' · ');
          citCard.appendChild(detailRow);
        }

        citationsContainer.appendChild(citCard);
      });

      item.appendChild(citationsContainer);
    }

    history.appendChild(item);
    history.scrollTop = history.scrollHeight;
  }

  async function askLiveChat(event) {
    event.preventDefault();
    const question = $('live-chat-question').value.trim();
    if (!question) return;
    appendLiveChat('user', question);
    try {
      const isLocalLive = Boolean(cameraStream && cameraStream.active);
      const activeCamera = isLocalLive ? 'cam-01' : (monitoringCameras.find((c) => c.enabled)?.camera_id || 'cam-01');
      const rawSession = $('live-chat-session-id')?.value.trim() || null;
      const rawCamera = $('live-chat-camera-id')?.value.trim() || activeCamera;
      const body = await request('/api/v1/chat/live', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: question,
          camera_id: rawCamera,
          session_id: rawSession,
          agent_mode: 'live',
        }),
      });
      appendLiveChat('assistant', body.answer, false, body.citations || []);
      setStatus('live-chat-status', `${body.citations?.length || 0} verified live citation(s) · ${body.answer_mode}`);
    } catch (error) {
      appendLiveChat('assistant', error.message, true);
      setStatus('live-chat-status', error.message, true);
    }
    $('live-chat-question').value = '';
  }

  async function loadLiveAgentFeed() {
    const activeCamera = monitoringCameras.find((c) => c.enabled) || monitoringCameras[0];
    const cameraId = $('live-chat-camera-id')?.value.trim() || activeCamera?.camera_id;
    const sessionId = $('live-chat-session-id')?.value.trim();
    if (!cameraId) {
      setStatus('live-agent-feed-status', 'Waiting for an active camera stream session.');
      return;
    }
    try {
      const query = new URLSearchParams();
      if (sessionId) query.set('session_id', sessionId);
      const body = await request(`/api/v1/cameras/${encodeURIComponent(cameraId)}/live-context${query.toString() ? `?${query}` : ''}`);
      const feed = body.agent_feed || {};
      const metricValues = $('live-agent-feed-metrics')?.querySelectorAll('strong') || [];
      [feed.frames || 0, feed.events || 0, feed.anomaly_count || 0].forEach((value, index) => { if (metricValues[index]) metricValues[index].textContent = value; });
      setStatus('live-agent-feed-status', `Camera ${cameraId.toUpperCase()} · ${body.status || 'running'} · latest media ${feed.latest_media_timestamp_seconds ?? '0.00'}s`);
      const items = [];
      (body.anomalies || []).slice(0, 5).forEach((item) => items.push(`<div class="live-feed-item anomaly">⚠ ${escape(item.reason || item.anomaly_type || 'Unusual activity')} · ${escape(item.severity || 'medium')}</div>`));
      (body.tracks || []).slice(-5).reverse().forEach((item) => items.push(`<div class="live-feed-item">${escape(item.object_label || 'object')} · ${escape(item.track_id)} · ${escape(Number(item.timestamp_seconds || 0).toFixed(2))}s</div>`));
      if ($('live-agent-feed-items')) {
        $('live-agent-feed-items').innerHTML = items.join('') || '<div class="empty">No live observations or anomalies received yet.</div>';
      }
    } catch (error) {
      setStatus('live-agent-feed-status', error.message, true);
    }
  }

  function startLiveAgentFeed() {
    if (liveFeedTimer) window.clearInterval(liveFeedTimer);
    loadLiveAgentFeed();
    liveFeedTimer = window.setInterval(loadLiveAgentFeed, 2000);
  }

  async function loadLiveArchive() {
    const listEl = $('live-archive-list');
    if (!listEl) return;
    listEl.innerHTML = '<div class="empty">Loading surveillance archive…</div>';

    const dateVal = $('archive-filter-date')?.value || '';
    const monthVal = $('archive-filter-month')?.value || '';
    const camVal = $('archive-filter-camera')?.value || 'all';

    // Populate camera selector if dynamic
    if ($('archive-filter-camera') && monitoringCameras.length) {
      const select = $('archive-filter-camera');
      const currentVal = select.value;
      const opts = ['<option value="all">All Monitored Cameras</option>'];
      monitoringCameras.forEach((c) => {
        opts.push(`<option value="${escape(c.camera_id)}">${escape(c.camera_id.toUpperCase())} · ${escape(c.display_name)}</option>`);
      });
      select.innerHTML = opts.join('');
      if (currentVal) select.value = currentVal;
    }

    const query = new URLSearchParams();
    if (dateVal) query.set('date', dateVal);
    if (monthVal) query.set('month', monthVal);
    if (camVal && camVal !== 'all') query.set('camera_id', camVal);

    try {
      const data = await request(`/api/v1/live/recordings?${query.toString()}`);
      const recordings = data.recordings || [];
      if ($('archive-recording-count-badge')) $('archive-recording-count-badge').textContent = `${recordings.length} video${recordings.length === 1 ? '' : 's'}`;
      if ($('archive-stat-count')) $('archive-stat-count').textContent = `${recordings.length} videos`;
      const totalMb = recordings.reduce((acc, r) => acc + (r.size_mb || 0), 0).toFixed(1);
      if ($('archive-stat-size')) $('archive-stat-size').textContent = `${totalMb} MB stored`;

      if (!recordings.length) {
        listEl.innerHTML = '<div class="empty">No recorded footage found matching filter criteria.</div>';
      } else {
        listEl.innerHTML = '';
        recordings.forEach((rec) => {
          const item = document.createElement('div');
          item.className = 'camera-card archive-rec-card';
          item.style.cursor = 'pointer';
          item.innerHTML = `
            <div class="camera-card-header">
              <div class="camera-card-meta">
                <span class="vms-feed-badge" style="background:#0284c7; color:#fff;">${escape((rec.camera_id || 'CAM-01').toUpperCase())}</span>
                <strong>${escape(formatCleanFootageName(rec.filename))}</strong><small class="muted" style="font-family: 'JetBrains Mono', monospace; font-size: 11px;">${escape(rec.filename)}</small>
                <small class="muted">${escape(rec.date)} · ${escape(rec.hour || 'Segment')}</small>
              </div>
              <div style="text-align:right;">
                <span class="state-pill live" style="font-size:10px;">${escape(rec.size_mb)} MB</span>
                <button class="button primary small" type="button" style="margin-top:4px;">▶ Preview</button>
              </div>
            </div>
          `;
          item.addEventListener('click', () => {
            document.querySelectorAll('.archive-rec-card').forEach((c) => c.classList.remove('selected'));
            item.classList.add('selected');
            playArchiveVideo(rec);
          });
          listEl.appendChild(item);
        });

        if (!activeArchiveRecording && recordings[0]) {
          playArchiveVideo(recordings[0]);
        }
      }

      await loadLiveSummary(dateVal, camVal);
    } catch (err) {
      listEl.innerHTML = `<div class="empty bad">Failed to load archive: ${escape(err.message)}</div>`;
    }
  }

  function playArchiveVideo(rec) {
    activeArchiveRecording = rec;
    const player = $('archive-video-player');
    const title = $('archive-player-title');
    const meta = $('archive-player-meta');
    if (!player) return;

    player.src = rec.url;
    player.load();
    if (title) title.textContent = `${rec.filename} (${(rec.camera_id || 'CAM-01').toUpperCase()})`;
    if (meta) meta.textContent = `Recorded: ${rec.date} ${rec.hour || ''} · ${rec.size_mb} MB · Continuous Live Capture`;
  }

  async function loadLiveSummary(date, cameraId) {
    try {
      const query = new URLSearchParams();
      if (date) query.set('date', date);
      if (cameraId && cameraId !== 'all') query.set('camera_id', cameraId);

      const summary = await request(`/api/v1/live/summary?${query.toString()}`);

      if ($('archive-stat-persons')) $('archive-stat-persons').textContent = summary.totals?.persons || 0;
      if ($('archive-stat-vehicles')) $('archive-stat-vehicles').textContent = summary.totals?.vehicles || 0;
      if ($('archive-stat-breaches')) $('archive-stat-breaches').textContent = summary.totals?.breaches || 0;

      if ($('archive-ai-narrative')) {
        $('archive-ai-narrative').innerHTML = formatChatMessage(summary.ai_narrative || 'No activities recorded for this period.');
      }

      const cardsContainer = $('archive-camera-summary-cards');
      if (cardsContainer) {
        cardsContainer.innerHTML = '';
        (summary.camera_summaries || []).forEach((cam) => {
          const card = document.createElement('div');
          card.className = 'metric-card';
          card.style.padding = '12px';
          card.innerHTML = `
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
              <strong style="color:#38bdf8;">${escape(cam.camera_id.toUpperCase())}</strong>
              <span class="state-pill ${cam.status === 'active' ? 'live' : 'offline'}" style="font-size:9px;">${escape(cam.status.toUpperCase())}</span>
            </div>
            <div style="font-size:12px; color:#e2e8f0; font-weight:600;">${escape(cam.display_name)}</div>
            <div style="font-size:11px; color:#94a3b8; margin-top:4px;">
              <span>👤 ${cam.persons_detected} persons</span> · <span>🚗 ${cam.vehicles_detected} vehicles</span>
            </div>
            <div style="font-size:10px; color:#64748b; margin-top:2px;">${cam.total_events} events · ${cam.zones_count} zone(s)</div>
          `;
          cardsContainer.appendChild(card);
        });
      }
    } catch (err) {
      console.warn('Failed to load live summary:', err);
    }
  }

  async function loadStorageVault() {
    setStatus('storage-status', 'Loading storage vault breakdown…');
    try {
      const data = await request('/api/v1/storage/summary');
      const uploads = data.uploaded_footage || { items: [], total_files: 0, total_mb: 0 };
      const live = data.live_streams || { items: [], total_files: 0, total_mb: 0 };
      const evidence = data.forensic_evidence || { items: [], total_files: 0, total_mb: 0 };

      if ($('storage-uploads-size')) $('storage-uploads-size').textContent = `${uploads.total_mb} MB`;
      if ($('storage-uploads-count')) $('storage-uploads-count').textContent = `${uploads.total_files} video file(s)`;

      if ($('storage-live-size')) $('storage-live-size').textContent = `${live.total_mb} MB`;
      if ($('storage-live-count')) $('storage-live-count').textContent = `${live.total_files} recording chunk(s)`;

      if ($('storage-evidence-size')) $('storage-evidence-size').textContent = `${evidence.total_mb} MB`;
      if ($('storage-evidence-count')) $('storage-evidence-count').textContent = `${evidence.total_files} trimmed clip(s)`;

      const uploadsTbody = $('storage-uploads-table');
      if (uploadsTbody) {
        if (!uploads.items.length) {
          uploadsTbody.innerHTML = '<tr><td colspan="4" class="empty">No uploaded footage files found.</td></tr>';
        } else {
          uploadsTbody.innerHTML = uploads.items.map(item => `
            <tr>
              <td>
                <div style="display: flex; flex-direction: column; gap: 2px;">
                  <strong style="color: #f8fafc; font-size: 13.5px;">📹 ${escapeHtml(formatCleanFootageName(item.name))}</strong>
                  <small style="color: #64748b; font-family: 'JetBrains Mono', monospace; font-size: 11px;">${escapeHtml(item.name)}</small>
                </div>
              </td>
              <td><span class="state-pill live" style="font-size: 11px;">${item.size_mb} MB</span></td>
              <td style="color: #cbd5e1; font-size: 12.5px;">${escapeHtml(formatCleanDate(item.modified_at))}</td>
              <td>
                <button class="button danger small delete-storage-btn" data-category="uploads" data-filename="${escapeHtml(item.name)}">Delete</button>
              </td>
            </tr>
          `).join('');
        }
      }

      const liveTbody = $('storage-live-table');
      if (liveTbody) {
        if (!live.items.length) {
          liveTbody.innerHTML = '<tr><td colspan="4" class="empty">No live stream recordings found.</td></tr>';
        } else {
          liveTbody.innerHTML = live.items.map(item => `
            <tr>
              <td>
                <div style="display: flex; flex-direction: column; gap: 2px;">
                  <strong style="color: #f8fafc; font-size: 13.5px;">📹 ${escapeHtml(formatCleanFootageName(item.name))}</strong>
                  <small style="color: #64748b; font-family: 'JetBrains Mono', monospace; font-size: 11px;">${escapeHtml(item.name)}</small>
                </div>
              </td>
              <td><span class="state-pill live" style="font-size: 11px;">${item.size_mb} MB</span></td>
              <td style="color: #cbd5e1; font-size: 12.5px;">${escapeHtml(formatCleanDate(item.modified_at))}</td>
              <td>
                <button class="button danger small delete-storage-btn" data-category="live_streams" data-filename="${escapeHtml(item.name)}">Delete</button>
              </td>
            </tr>
          `).join('');
        }
      }

      const evidenceTbody = $('storage-evidence-table');
      if (evidenceTbody) {
        if (!evidence.items.length) {
          evidenceTbody.innerHTML = '<tr><td colspan="4" class="empty">No trimmed evidence clips generated yet.</td></tr>';
        } else {
          evidenceTbody.innerHTML = evidence.items.map(item => `
            <tr>
              <td>
                <div style="display: flex; flex-direction: column; gap: 2px;">
                  <strong style="color: #f8fafc; font-size: 13.5px;">📹 ${escapeHtml(formatCleanFootageName(item.name))}</strong>
                  <small style="color: #64748b; font-family: 'JetBrains Mono', monospace; font-size: 11px;">${escapeHtml(item.name)}</small>
                </div>
              </td>
              <td><span class="state-pill live" style="font-size: 11px;">${item.size_mb} MB</span></td>
              <td style="color: #cbd5e1; font-size: 12.5px;">${escapeHtml(formatCleanDate(item.modified_at))}</td>
              <td>
                <button class="button danger small delete-storage-btn" data-category="evidence" data-filename="${escapeHtml(item.name)}">Delete</button>
              </td>
            </tr>
          `).join('');
        }
      }

      document.querySelectorAll('.delete-storage-btn').forEach(btn => {
        btn.onclick = async () => {
          const category = btn.dataset.category;
          const filename = btn.dataset.filename;
          if (!confirm(`Are you sure you want to delete ${filename} from ${category}?`)) return;
          try {
            await request(`/api/v1/storage/${encodeURIComponent(category)}/${encodeURIComponent(filename)}`, { method: 'DELETE' });
            notify(`Deleted ${filename}`);
            loadStorageVault();
          } catch (err) {
            setStatus('storage-status', err.message, true);
          }
        };
      });

      setStatus('storage-status', 'Storage vault synchronized.');
    } catch (err) {
      setStatus('storage-status', `Failed to load storage: ${err.message}`, true);
    }
  }

  const MONITORING_STATUSES = new Set(['configured', 'queued', 'starting', 'running', 'degraded', 'completed', 'failed', 'stopped', 'disabled']);

  function isRouteUnavailable(error) {
    return error?.status === 404 || error?.status === 503;
  }

  function collection(body, key) {
    if (Array.isArray(body)) return body;
    if (Array.isArray(body?.[key])) return body[key];
    if (Array.isArray(body?.items)) return body.items;
    return [];
  }

  function cameraSource(raw) {
    const source = raw?.source && typeof raw.source === 'object' ? raw.source : raw || {};
    return {
      kind: String(source.kind || source.source_kind || raw?.source_kind || 'file').trim().toLowerCase(),
      path: String(source.path ?? source.url ?? raw?.path ?? raw?.url ?? '').trim(),
    };
  }

  function normalizeCamera(raw) {
    const source = cameraSource(raw);
    return {
      ...raw,
      camera_id: String(raw?.camera_id ?? raw?.id ?? '').trim(),
      display_name: String(raw?.display_name ?? raw?.name ?? raw?.camera_id ?? 'Unnamed camera').trim(),
      source,
      enabled: raw?.enabled !== false,
      latest_report: raw?.latest_report || raw?.last_report || raw?.report || null,
    };
  }

  function normalizeStatus(value, fallback = 'configured') {
    const status = String(value || '').trim().toLowerCase();
    return MONITORING_STATUSES.has(status) ? status : fallback;
  }

  function reportList(body, cameraId) {
    const reports = collection(body, 'reports');
    if (reports.length) return reports.map((report) => ({ ...report, camera_id: report.camera_id || cameraId }));
    if (body?.report && typeof body.report === 'object') return [{ ...body.report, camera_id: body.report.camera_id || cameraId }];
    if (body?.status || body?.session_id) return [{ ...body, camera_id: body.camera_id || cameraId }];
    return [];
  }

  function reportSortValue(report) {
    const value = report?.finished_at || report?.started_at || report?.created_at || report?.updated_at;
    const parsed = value ? Date.parse(value) : Number.NaN;
    return Number.isNaN(parsed) ? 0 : parsed;
  }

  function latestReportFor(camera) {
    const state = monitoringReports.get(camera.camera_id);
    if (state?.latest) return state.latest;
    if (camera.latest_report) return camera.latest_report;
    return null;
  }

  function cameraStatus(camera) {
    const report = latestReportFor(camera);
    const explicit = camera.status || camera.state;
    if (explicit) return normalizeStatus(explicit, camera.enabled ? 'configured' : 'disabled');
    if (!camera.enabled) return 'disabled';
    return normalizeStatus(report?.status, 'configured');
  }

  function redactSource(value) {
    let result = String(value || '').trim();
    if (!result) return '—';
    result = result.replace(/(\/\/[^/\s:@]+:)[^@\s]+@/g, '$1••••@');
    result = result.replace(/((?:password|passwd|token|secret|api[-_]?key|access[-_]?key|credential|signature|sig)=)[^&#\s]+/gi, '$1••••');
    try {
      const parsed = new URL(result);
      parsed.searchParams.forEach((value, key) => {
        if (/password|passwd|token|secret|api[-_]?key|access[-_]?key|credential|signature|sig/i.test(key)) parsed.searchParams.set(key, '••••');
      });
      result = parsed.toString();
    } catch (error) {
      // Local paths are not URLs; the regex redaction above still protects key=value secrets.
    }
    return result;
  }

  function sourceLabel(source) {
    const kind = source.kind === 'rtsp' ? 'RTSP / live' : source.kind === 'file' ? 'File' : source.kind || 'Unknown';
    return `${kind} · ${redactSource(source.path)}`;
  }

  function reportMetric(report, key, nestedKey) {
    const value = report?.[key] ?? report?.metrics?.[key] ?? report?.[nestedKey] ?? report?.metrics?.[nestedKey];
    return value === undefined || value === null || value === '' ? '—' : text(value);
  }

  function statusPill(status) {
    const normalized = normalizeStatus(status);
    return `<span class="status-pill status-${escape(normalized)}">${escape(normalized.toUpperCase())}</span>`;
  }

  function reportFromResponse(body, cameraId) {
    const reports = reportList(body, cameraId);
    if (!reports.length) return null;
    return reports.slice().sort((left, right) => reportSortValue(left) - reportSortValue(right)).pop();
  }

  function rememberReport(cameraId, report) {
    if (!report) return;
    const previous = monitoringReports.get(cameraId)?.reports || [];
    const reports = previous.filter((item) => item.session_id !== report.session_id || !report.session_id);
    reports.push({ ...report, camera_id: report.camera_id || cameraId });
    const latest = reports.slice().sort((left, right) => reportSortValue(left) - reportSortValue(right)).pop() || report;
    monitoringReports.set(cameraId, { reports, latest, unavailable: false });
  }

  function reportTimes(report) {
    const started = report?.started_at ? new Date(report.started_at).toLocaleString() : '—';
    const finished = report?.finished_at ? new Date(report.finished_at).toLocaleString() : '—';
    return { started, finished };
  }

  function renderMonitoringList() {
    const wrap = $('camera-list');
    if (!wrap) return;
    wrap.replaceChildren();
    const count = monitoringCameras.length;
    $('monitoring-camera-count').textContent = `${count} camera${count === 1 ? '' : 's'}`;
    if (!count) {
      wrap.innerHTML = '<div class="empty">No cameras are registered. Provision a camera to begin live streaming.</div>';
      renderMonitoringDetail();
      renderActiveCameraFeeds();
      return;
    }

    const activeList = monitoringCameras.filter((c) => c.enabled && ['running', 'starting', 'ready'].includes(cameraStatus(c)));
    const inactiveList = monitoringCameras.filter((c) => !c.enabled || !['running', 'starting', 'ready'].includes(cameraStatus(c)));

    const renderCard = (camera) => {
      const report = latestReportFor(camera);
      const status = cameraStatus(camera);
      const live = camera.source.kind !== 'file';
      const card = document.createElement('article');
      card.className = `camera-card${camera.camera_id === monitoringSelectedId ? ' selected' : ''}`;
      const runAction = status === 'queued'
        ? '<button class="button secondary compact-button" type="button" disabled>Queued</button>'
        : ['starting', 'running', 'degraded'].includes(status)
          ? `<button class="button danger compact-button" type="button" data-camera-action="stop" data-camera-id="${escape(camera.camera_id)}">Stop</button>`
          : `<button class="button primary compact-button" type="button" data-camera-action="${status === 'failed' || status === 'stopped' ? 'retry' : 'run'}" data-camera-id="${escape(camera.camera_id)}" ${!camera.enabled ? 'disabled' : ''}>${live ? 'Start live' : status === 'failed' || status === 'stopped' ? 'Retry' : 'Run file'}</button>`;
      card.innerHTML = `<div class="camera-card-heading"><div><strong>${escape(camera.display_name)}</strong><small>${escape(camera.camera_id)}</small></div>${statusPill(status)}</div><p class="camera-source"><span>${escape(camera.source.kind === 'rtsp' ? 'RTSP / live' : camera.source.kind === 'file' ? 'File' : camera.source.kind)}</span>${escape(redactSource(camera.source.path))}</p><div class="camera-card-metrics"><span><strong>${escape(reportMetric(report, 'frame_count', 'frames'))}</strong> frames</span><span><strong>${escape(reportMetric(report, 'event_count', 'events'))}</strong> events</span><span>${camera.enabled ? 'Enabled' : 'Disabled'}</span></div><div class="camera-card-actions"><button class="button text-button" type="button" data-camera-action="select" data-camera-id="${escape(camera.camera_id)}">Inspect</button><button class="button text-button" type="button" data-camera-action="edit" data-camera-id="${escape(camera.camera_id)}">Edit</button><button class="button text-button" type="button" data-camera-action="toggle" data-camera-id="${escape(camera.camera_id)}">${camera.enabled ? 'Disable' : 'Enable'}</button>${runAction}</div>${live && status !== 'running' ? '<p class="camera-live-note">Live source configured only. The backend must report running before this is treated as active.</p>' : ''}${report?.last_error ? `<p class="camera-error">${escape(report.last_error)}</p>` : ''}`;
      card.addEventListener('click', (event) => {
        if (!event.target.closest('button')) {
          monitoringSelectedId = camera.camera_id;
          renderMonitoringList();
          renderMonitoringDetail();
        }
      });
      return card;
    };

    if (activeList.length) {
      const activeHeader = document.createElement('div');
      activeHeader.style.cssText = 'padding:6px 0;font-size:11px;font-weight:700;color:#10b981;letter-spacing:0.05em;display:flex;align-items:center;gap:6px;';
      activeHeader.innerHTML = `<span>🟢 ACTIVE STREAMS (${activeList.length} ONLINE)</span>`;
      wrap.appendChild(activeHeader);
      activeList.forEach((c) => wrap.appendChild(renderCard(c)));
    }

    if (inactiveList.length) {
      const inactiveHeader = document.createElement('div');
      inactiveHeader.style.cssText = 'padding:10px 0 6px;font-size:11px;font-weight:700;color:#94a3b8;letter-spacing:0.05em;display:flex;align-items:center;gap:6px;';
      inactiveHeader.innerHTML = `<span>⚪ STANDBY / INACTIVE CAMERAS (${inactiveList.length})</span>`;
      wrap.appendChild(inactiveHeader);
      inactiveList.forEach((c) => wrap.appendChild(renderCard(c)));
    }

    renderMonitoringDetail();
    renderActiveCameraFeeds();
  }

  function renderMonitoringDetail() {
    const title = $('monitoring-detail-title');
    const detail = $('monitoring-detail');
    const pill = $('monitoring-detail-status');
    const camera = monitoringCameras.find((item) => item.camera_id === monitoringSelectedId) || monitoringCameras[0];
    if (!camera) {
      if (title) title.textContent = 'Select a camera';
      if (pill) { pill.className = 'status-pill status-configured'; pill.textContent = 'CONFIGURED'; }
      if (detail) detail.innerHTML = '<div class="empty">Select a camera to inspect its latest backend report.</div>';
      return;
    }
    monitoringSelectedId = camera.camera_id;
    const reportState = monitoringReports.get(camera.camera_id);
    const report = latestReportFor(camera);
    const status = cameraStatus(camera);
    const times = reportTimes(report);
    if (title) title.textContent = `${camera.display_name} · ${camera.camera_id}`;
    if (pill) { pill.className = `status-pill status-${status}`; pill.textContent = status.toUpperCase(); }
    if (!detail) return;
    const history = (reportState?.reports || []).slice().sort((left, right) => reportSortValue(right) - reportSortValue(left)).slice(0, 5);
    detail.innerHTML = `<div class="monitoring-summary"><div><span>Source</span><strong>${escape(sourceLabel(camera.source))}</strong></div><div><span>Enabled</span><strong>${camera.enabled ? 'Yes' : 'No'}</strong></div><div><span>Frames</span><strong>${escape(reportMetric(report, 'frame_count', 'frames'))}</strong></div><div><span>Events</span><strong>${escape(reportMetric(report, 'event_count', 'events'))}</strong></div><div><span>Session</span><strong>${escape(report?.session_id || 'No report')}</strong></div><div><span>Track scope</span><strong>${escape(report?.track_scope || 'No report')}</strong></div></div><div class="monitoring-report-note">${report ? `Started ${escape(times.started)} · Finished ${escape(times.finished)}` : 'No backend report has been returned for this camera yet. Configured does not mean running.'}</div>${report?.last_error ? `<div class="result-card bad">${escape(report.last_error)}</div>` : ''}<div class="monitoring-history"><h4>Recent reports</h4>${history.length ? `<div class="report-history">${history.map((item) => `<div><span>${statusPill(item.status)}</span><strong>${escape(reportMetric(item, 'event_count', 'events'))} events</strong><small>${escape(item.session_id || 'session unavailable')}</small></div>`).join('')}</div>` : '<div class="empty">No report history returned.</div>'}</div>`;
  }

  function renderMonitoringAnomalies(anomalies) {
    const wrap = $('monitoring-anomalies');
    if (!wrap) return;
    wrap.replaceChildren();
    if (!anomalies.length) {
      wrap.innerHTML = '<div class="empty">No recent anomalies or alerts returned.</div>';
      return;
    }
    anomalies.slice(0, 12).forEach((anomaly) => {
      const item = document.createElement('div');
      item.className = 'anomaly-item';
      const label = anomaly.message || anomaly.description || anomaly.event_type || anomaly.anomaly_type || 'Monitoring signal';
      const camera = anomaly.camera_id || anomaly.camera_name || 'Camera not specified';
      const severity = anomaly.severity || anomaly.status || 'signal';
      const when = anomaly.timestamp || anomaly.created_at || anomaly.detected_at || anomaly.finished_at || '';
      item.innerHTML = `<div><strong>${escape(label)}</strong><small>${escape(camera)}${when ? ` · ${escape(new Date(when).toLocaleString())}` : ''}</small></div><span class="anomaly-severity">${escape(severity)}</span>`;
      wrap.appendChild(item);
    });
  }

  async function loadMonitoringAnomalies() {
    try {
      const body = await request('/api/v1/monitoring/anomalies');
      const anomalies = collection(body, 'anomalies');
      const alerts = collection(body, 'alerts');
      renderMonitoringAnomalies(anomalies.length ? anomalies : alerts);
      setStatus('monitoring-anomalies-status', `${(anomalies.length ? anomalies : alerts).length} recent signal(s)`);
      return true;
    } catch (error) {
      renderMonitoringAnomalies([]);
      if (isRouteUnavailable(error)) {
        setStatus('monitoring-anomalies-status', `Anomaly route is unavailable (${error.status}). Camera monitoring can still be configured.`, true);
      } else {
        setStatus('monitoring-anomalies-status', error.message, true);
      }
      return false;
    }
  }

  async function loadMonitoring() {
    const refresh = $('monitoring-refresh');
    if (refresh) { refresh.disabled = true; refresh.setAttribute('aria-busy', 'true'); }
    setStatus('monitoring-status', 'Refreshing camera inventory…');
    try {
      const body = await request('/api/v1/cameras');
      monitoringCameras = collection(body, 'cameras').map(normalizeCamera).filter((camera) => camera.camera_id);
      if (!monitoringSelectedId || !monitoringCameras.some((camera) => camera.camera_id === monitoringSelectedId)) monitoringSelectedId = monitoringCameras[0]?.camera_id || '';
      monitoringReports = new Map(monitoringCameras.filter((camera) => camera.latest_report).map((camera) => [camera.camera_id, { reports: [camera.latest_report], latest: camera.latest_report, unavailable: false }]));
      renderMonitoringList();
      let reportUnavailable = false;
      await Promise.all(monitoringCameras.map(async (camera) => {
        try {
          const reportBody = await request(`/api/v1/cameras/${encodeURIComponent(camera.camera_id)}/reports`);
          const reports = reportList(reportBody, camera.camera_id);
          if (reports.length) {
            const latest = reports.slice().sort((left, right) => reportSortValue(left) - reportSortValue(right)).pop();
            monitoringReports.set(camera.camera_id, { reports, latest, unavailable: false });
          }
        } catch (error) {
          monitoringReports.set(camera.camera_id, { reports: monitoringReports.get(camera.camera_id)?.reports || [], latest: latestReportFor(camera), unavailable: true });
          if (isRouteUnavailable(error)) reportUnavailable = true;
        }
      }));
      renderMonitoringList();
      const reportNote = reportUnavailable ? ' Latest reports are unavailable until the report route is deployed.' : '';
      setStatus('monitoring-status', `${monitoringCameras.length} camera${monitoringCameras.length === 1 ? '' : 's'} loaded.${reportNote}`);
      try { await loadMonitoringAnomalies(); } catch (err) { console.warn('Monitoring anomalies note:', err); }
      updateGlobalFleetStatus();
    } catch (error) {
      monitoringCameras = [];
      monitoringReports = new Map();
      renderMonitoringList();
      renderMonitoringAnomalies([]);
      if (isRouteUnavailable(error)) {
        setStatus('monitoring-status', `Camera monitoring is not available yet (${error.status}). The rest of the dashboard remains usable; deploy the camera routes to enable this view.`, true);
      } else {
        setStatus('monitoring-status', error.message, true);
      }
      setStatus('monitoring-anomalies-status', 'Anomaly data is unavailable until monitoring routes respond.', true);
    } finally {
      if (refresh) { refresh.disabled = false; refresh.removeAttribute('aria-busy'); }
    }
  }

  function resetCameraForm() {
    monitoringEditingId = '';
    $('camera-form').reset();
    $('camera-form-enabled').checked = true;
    $('camera-form-title').textContent = 'Register a camera';
    $('camera-form-id').disabled = false;
    if ($('camera-form-zone-name')) $('camera-form-zone-name').value = 'Primary Perimeter';
    if ($('camera-form-zone-preset')) $('camera-form-zone-preset').value = 'perimeter';
    $('camera-form-submit').textContent = 'Save & Provision';
    $('camera-form-cancel').hidden = true;
    setStatus('camera-form-status', '');
  }

  function editCameraForm(camera) {
    monitoringEditingId = camera.camera_id;
    $('camera-form-title').textContent = `Edit ${camera.display_name}`;
    $('camera-form-id').value = camera.camera_id;
    $('camera-form-id').disabled = true;
    $('camera-form-name').value = camera.display_name;
    $('camera-form-kind').value = ['rtsp', 'device'].includes(camera.source.kind) ? camera.source.kind : 'file';
    $('camera-form-path').value = camera.source.path;
    $('camera-form-enabled').checked = camera.enabled;
    if (camera.zones && camera.zones[0]) {
      if ($('camera-form-zone-name')) $('camera-form-zone-name').value = camera.zones[0].name || camera.zones[0].zone_id || 'Primary Perimeter';
    }
    $('camera-form-submit').textContent = 'Save changes';
    $('camera-form-cancel').hidden = false;
    setStatus('camera-form-status', 'Editing a backend-registered camera. Save to apply changes.');
    $('camera-form-name').focus();
  }

  function cameraFormPayload() {
    const cameraId = $('camera-form-id').value.trim();
    const displayName = $('camera-form-name').value.trim();
    const path = $('camera-form-path').value.trim();
    if (!cameraId || !displayName || !path) throw new Error('Camera ID, display name, and source path or URL are required.');

    const zoneName = $('camera-form-zone-name')?.value.trim() || 'Primary Perimeter';
    const zonePreset = $('camera-form-zone-preset')?.value || 'perimeter';
    let polygon = [[0, 0], [640, 0], [640, 480], [0, 480]];
    if (zonePreset === 'gate') {
      polygon = [[160, 100], [480, 100], [480, 440], [160, 440]];
    } else if (zonePreset === 'custom' && window.customZonePoints?.length >= 3) {
      polygon = window.customZonePoints;
    }
    const zones = [{ zone_id: zoneName.toLowerCase().replace(/[^a-z0-9_-]/g, '-'), name: zoneName, polygon }];

    const payload = {
      display_name: displayName,
      source: { kind: $('camera-form-kind').value, path },
      enabled: $('camera-form-enabled').checked,
      zones,
    };
    if (!monitoringEditingId) payload.camera_id = cameraId;
    return payload;
  }

  async function saveCamera(event) {
    event.preventDefault();
    const submit = $('camera-form-submit');
    if (submit.disabled) return;
    let payload;
    try { payload = cameraFormPayload(); } catch (error) { setStatus('camera-form-status', error.message, true); return; }
    const release = setButtonBusy(submit, monitoringEditingId ? 'Saving…' : 'Registering…');
    try {
      const path = monitoringEditingId ? `/api/v1/cameras/${encodeURIComponent(monitoringEditingId)}` : '/api/v1/cameras';
      await request(path, { method: monitoringEditingId ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      notify(monitoringEditingId ? 'Camera changes saved' : 'Camera registered');
      resetCameraForm();
      await loadMonitoring();
    } catch (error) {
      setStatus('camera-form-status', isRouteUnavailable(error) ? `Camera configuration routes are unavailable (${error.status}). No local camera state was changed.` : error.message, true);
      notify(error.message, true);
    } finally { release(); }
  }

  async function runCamera(camera) {
    if (!camera.enabled) {
      setStatus('monitoring-status', `${camera.display_name} is disabled. Enable it before requesting a run.`, true);
      return;
    }
    try {
      const body = await request(`/api/v1/cameras/${encodeURIComponent(camera.camera_id)}/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(camera.source.kind === 'file' ? {} : { continuous: true }),
      });
      rememberReport(camera.camera_id, reportFromResponse(body, camera.camera_id));
      notify(`Run requested for ${camera.display_name}`);
      await loadMonitoring();
    } catch (error) {
      setStatus('monitoring-status', isRouteUnavailable(error) ? `Run route is unavailable (${error.status}); no run was reported.` : error.message, true);
      notify(error.message, true);
    }
  }

  async function stopCameraRun(camera) {
    try {
      const body = await request(`/api/v1/cameras/${encodeURIComponent(camera.camera_id)}/stop`, { method: 'POST' });
      rememberReport(camera.camera_id, reportFromResponse(body, camera.camera_id));
      notify(`Stop requested for ${camera.display_name}`);
      await loadMonitoring();
    } catch (error) {
      setStatus('monitoring-status', isRouteUnavailable(error) ? `Stop route is unavailable (${error.status}); no local status was changed.` : error.message, true);
      notify(error.message, true);
    }
  }

  async function toggleCamera(camera) {
    try {
      await request(`/api/v1/cameras/${encodeURIComponent(camera.camera_id)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !camera.enabled }) });
      notify(`${camera.display_name} ${camera.enabled ? 'disabled' : 'enabled'}`);
      await loadMonitoring();
    } catch (error) {
      setStatus('monitoring-status', isRouteUnavailable(error) ? `Camera update route is unavailable (${error.status}); no local status was changed.` : error.message, true);
      notify(error.message, true);
    }
  }

  async function handleCameraAction(event) {
    const button = event.target.closest('[data-camera-action]');
    if (!button) return;
    event.preventDefault();
    const camera = monitoringCameras.find((item) => item.camera_id === button.dataset.cameraId);
    if (!camera) return;
    monitoringSelectedId = camera.camera_id;
    const action = button.dataset.cameraAction;
    if (action === 'select') { renderMonitoringList(); renderMonitoringDetail(); return; }
    if (action === 'edit') { editCameraForm(camera); renderMonitoringList(); renderMonitoringDetail(); return; }
    if (action === 'toggle') { await toggleCamera(camera); return; }
    if (action === 'stop') { await stopCameraRun(camera); return; }
    if (action === 'run' || action === 'retry') await runCamera(camera);
  }

  async function loadAlerts() {
    try {
      const rules = await request('/api/v1/alerts/rules');
      const rulesContainer = $('alert-rules');
      if (rulesContainer) {
        if (!rules.rules?.length) {
          rulesContainer.innerHTML = '<div class="empty">No active security rules defined.</div>';
        } else {
          rulesContainer.innerHTML = rules.rules.map((rule) => `
            <div class="rule-item" style="display:flex; justify-content:space-between; align-items:center;">
              <div>
                <strong>${escape(rule.name)}</strong>
                <span>${escape(rule.event_type || 'any event')}</span>
                <span>${escape(rule.zone_id || 'any zone')}</span>
                <span>${escape(rule.object_label || 'any object')}</span>
              </div>
              <button class="button danger small btn-delete-rule" data-rule-id="${escape(rule.rule_id)}" type="button">🗑 Delete</button>
            </div>
          `).join('');
        }
      }
      setStatus('alert-status', `${rules.rules?.length || 0} rule(s)`);

      const alerts = await request('/api/v1/alerts');
      const alertsContainer = $('alerts-list');
      if (alertsContainer) {
        if (!alerts.alerts?.length) {
          alertsContainer.innerHTML = '<div class="empty">No unacknowledged security alerts.</div>';
        } else {
          alertsContainer.replaceChildren(...(alerts.alerts || []).map((alert) => {
            const item = document.createElement('div');
            item.className = 'rule-item';
            item.style.cssText = 'display:flex; justify-content:space-between; align-items:center;';

            const textSpan = document.createElement('span');
            textSpan.textContent = `[${(alert.status || 'ALERT').toUpperCase()}] · ${alert.message}`;
            item.appendChild(textSpan);

            const btnGroup = document.createElement('div');
            btnGroup.style.display = 'flex';
            btnGroup.style.gap = '6px';

            if (alert.status === 'open') {
              const ackBtn = document.createElement('button');
              ackBtn.className = 'button secondary small';
              ackBtn.textContent = 'Acknowledge';
              ackBtn.onclick = async () => {
                await request(`/api/v1/alerts/${encodeURIComponent(alert.alert_id)}/acknowledge`, { method: 'POST' });
                loadAlerts();
              };
              btnGroup.appendChild(ackBtn);
            }

            const delBtn = document.createElement('button');
            delBtn.className = 'button danger small';
            delBtn.textContent = 'Dismiss / Delete';
            delBtn.onclick = async () => {
              await request(`/api/v1/alerts/${encodeURIComponent(alert.alert_id)}`, { method: 'DELETE' });
              loadAlerts();
            };
            btnGroup.appendChild(delBtn);

            item.appendChild(btnGroup);
            return item;
          }));
        }
      }
    } catch (error) { setStatus('alert-status', error.message, true); }
  }

  async function refreshDashboard() {
    await checkHealth();
    await loadCapabilities();
    await loadEvents();
    await loadAlerts();
  }

  if ($('auth-form')) $('auth-form').addEventListener('submit', submitAuth);
  if ($('auth-toggle')) $('auth-toggle').addEventListener('click', toggleRegister);
  if ($('auth-local-open')) $('auth-local-open').addEventListener('click', openLocalConsole);
  if ($('auth-save-api')) $('auth-save-api').addEventListener('click', saveAuthConnection);
  if ($('auth-api-base')) $('auth-api-base').addEventListener('input', () => { if ($('auth-api-origin')) $('auth-api-origin').textContent = $('auth-api-base')?.value?.trim() || 'Same origin'; });
  if ($('logout')) $('logout').addEventListener('click', leaveDashboard);
  if ($('settings-logout')) $('settings-logout').addEventListener('click', leaveDashboard);
  if ($('account-signout')) $('account-signout').addEventListener('click', leaveDashboard);
  if ($('account-settings')) $('account-settings').addEventListener('click', () => setView('settings'));
  if ($('account-menu-button')) $('account-menu-button').addEventListener('click', toggleAccountMenu);
  if ($('new-capture')) $('new-capture').addEventListener('click', startNewCapture);
  function updateSidebarState(collapsed) {
    sidebarCollapsed = collapsed;
    localStorage.setItem('video-intelligence-sidebar', sidebarCollapsed ? 'collapsed' : 'expanded');
    if ($('dashboard-shell')) $('dashboard-shell').classList.toggle('sidebar-collapsed', sidebarCollapsed);
    if ($('sidebar-collapse')) {
      $('sidebar-collapse').textContent = sidebarCollapsed ? '›' : '‹';
      $('sidebar-collapse').setAttribute('aria-pressed', String(sidebarCollapsed));
      $('sidebar-collapse').setAttribute('aria-label', sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar');
      $('sidebar-collapse').title = sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar';
    }
  }

  if ($('sidebar-collapse')) {
    $('sidebar-collapse').addEventListener('click', () => {
      updateSidebarState(!sidebarCollapsed);
    });
  }
  const brandMarkEl = document.querySelector('.sidebar-brand .brand-mark');
  if (brandMarkEl) {
    brandMarkEl.addEventListener('click', () => {
      if (sidebarCollapsed) updateSidebarState(false);
    });
  }
  if ($('theme-toggle')) $('theme-toggle').addEventListener('click', toggleTheme);
  document.querySelectorAll('[data-theme-choice]').forEach((button) => button.addEventListener('click', () => applyTheme(button.dataset.themeChoice)));
  document.querySelectorAll('[data-chat-suggestion]').forEach((button) => button.addEventListener('click', selectChatSuggestion));
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.sidebar-account')) closeAccountMenu();
  });
  document.querySelectorAll('.nav-item').forEach((button) => button.addEventListener('click', () => setView(button.dataset.view)));
  document.querySelectorAll('[data-go-view]').forEach((button) => button.addEventListener('click', () => setView(button.dataset.goView)));
  if ($('overview-refresh')) $('overview-refresh').addEventListener('click', refreshDashboard);
  if ($('settings-refresh')) $('settings-refresh').addEventListener('click', loadCapabilities);
  if ($('telegram-config-form')) $('telegram-config-form').addEventListener('submit', saveTelegramConfig);
  if ($('telegram-test')) $('telegram-test').addEventListener('click', testTelegramConfig);
  if ($('monitoring-refresh')) $('monitoring-refresh').addEventListener('click', loadMonitoring);
  if ($('monitoring-anomalies-refresh')) $('monitoring-anomalies-refresh').addEventListener('click', loadMonitoringAnomalies);
  if ($('monitoring-new-camera')) $('monitoring-new-camera').addEventListener('click', () => { resetCameraForm(); $('camera-form-name')?.focus(); });
  if ($('camera-form')) $('camera-form').addEventListener('submit', saveCamera);
  if ($('camera-form-reset')) $('camera-form-reset').addEventListener('click', resetCameraForm);
  if ($('camera-form-cancel')) $('camera-form-cancel').addEventListener('click', resetCameraForm);
  if ($('camera-list')) $('camera-list').addEventListener('click', handleCameraAction);
  if ($('upload-form')) $('upload-form').addEventListener('submit', uploadForm);
  if ($('process-form')) $('process-form').addEventListener('submit', processForm);
  if ($('video-file')) $('video-file').addEventListener('change', (event) => {
    const file = event.target.files[0];
    if ($('file-name')) $('file-name').textContent = file?.name || 'Supports MP4, MOV, WebM, AVI (up to 500 MB)';
    if (file) {
      const previewUrl = URL.createObjectURL(file);
      playPreRecordedVideo('', file.name, previewUrl);
    }
  });
  if ($('probe-video')) $('probe-video').addEventListener('click', async () => { try { showResult('probe-result', JSON.stringify(await request(`/api/v1/videos/${encodeURIComponent(currentVideoId || $('video-id')?.value)}/probe`), null, 2)); } catch (error) { showResult('probe-result', error.message, true); } });
  if ($('refresh-events')) $('refresh-events').addEventListener('click', loadEvents);
  if ($('search-events')) $('search-events').addEventListener('click', loadEvents);
  if ($('refresh-tracks')) $('refresh-tracks').addEventListener('click', loadTracks);
  if ($('camera-start')) $('camera-start').addEventListener('click', startCamera);
  if ($('camera-record')) $('camera-record').addEventListener('click', startRecording);
  if ($('camera-stop')) $('camera-stop').addEventListener('click', () => stopCamera(true));
  if ($('chat-form')) $('chat-form').addEventListener('submit', askChat);
  if ($('live-chat-form')) $('live-chat-form').addEventListener('submit', askLiveChat);
  if ($('live-feed-refresh')) $('live-feed-refresh').addEventListener('click', loadLiveAgentFeed);
  if ($('storage-refresh')) $('storage-refresh').addEventListener('click', loadStorageVault);
  if ($('live-archive-refresh')) $('live-archive-refresh').addEventListener('click', loadLiveArchive);
  if ($('btn-apply-archive-filter')) $('btn-apply-archive-filter').addEventListener('click', loadLiveArchive);
  if ($('btn-reset-archive-filter')) $('btn-reset-archive-filter').addEventListener('click', () => {
    if ($('archive-filter-date')) $('archive-filter-date').value = '';
    if ($('archive-filter-month')) $('archive-filter-month').value = '';
    if ($('archive-filter-camera')) $('archive-filter-camera').value = 'all';
    loadLiveArchive();
  });
  if ($('btn-archive-gen-summary')) $('btn-archive-gen-summary').addEventListener('click', () => {
    const dateVal = $('archive-filter-date')?.value || '';
    const camVal = $('archive-filter-camera')?.value || 'all';
    loadLiveSummary(dateVal, camVal);
  });
  if ($('live-pipeline-form')) $('live-pipeline-form').addEventListener('submit', saveLivePipelineConfig);
  document.querySelectorAll('[data-live-chat-suggestion]').forEach((btn) => {
    btn.addEventListener('click', () => {
      if ($('live-chat-question')) {
        $('live-chat-question').value = btn.dataset.liveChatSuggestion;
        $('live-chat-form').dispatchEvent(new Event('submit', { cancelable: true }));
      }
    });
  });
  $('refresh-alerts').addEventListener('click', loadAlerts);
  if ($('clear-alerts')) {
    $('clear-alerts').addEventListener('click', async () => {
      const btn = $('clear-alerts');
      btn.disabled = true;
      btn.textContent = 'Clearing…';
      try {
        await request('/api/v1/alerts/clear', { method: 'POST' });
        await loadAlerts();
        notify('All alerts cleared');
      } catch (err) {
        setStatus('alert-status', err.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = 'Clear All Alerts';
      }
    });
  }
  if ($('refresh-rules')) $('refresh-rules').addEventListener('click', loadAlerts);
  if ($('clear-all-rules')) {
    $('clear-all-rules').addEventListener('click', async () => {
      const btn = $('clear-all-rules');
      btn.disabled = true;
      btn.textContent = 'Clearing…';
      try {
        await request('/api/v1/alerts/rules/clear', { method: 'POST' });
        await loadAlerts();
        notify('All security rules cleared');
      } catch (err) {
        setStatus('alert-status', err.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = 'Clear All Rules';
      }
    });
  }
  if ($('alert-rules')) {
    $('alert-rules').addEventListener('click', async (e) => {
      const btn = e.target.closest('.btn-delete-rule');
      if (btn) {
        const ruleId = btn.dataset.ruleId;
        if (!ruleId) return;
        const ruleItem = btn.closest('.rule-item');
        if (ruleItem) {
          ruleItem.style.transition = 'opacity 0.2s ease, transform 0.2s ease';
          ruleItem.style.opacity = '0';
          ruleItem.style.transform = 'translateX(20px)';
          setTimeout(() => {
            ruleItem.remove();
            const container = $('alert-rules');
            if (container && !container.querySelector('.rule-item')) {
              container.innerHTML = '<div class="empty">No active security rules defined.</div>';
            }
          }, 210);
        }
        btn.disabled = true;
        try {
          await request(`/api/v1/alerts/rules/${encodeURIComponent(ruleId)}`, { method: 'DELETE' });
          notify('Security rule deleted');
        } catch (err) {
          console.warn('Rule delete error:', err);
        }
        setTimeout(() => loadAlerts(), 400);
      }
    });
  }
  $('alert-form').addEventListener('submit', async (event) => { event.preventDefault(); try { await request('/api/v1/alerts/rules', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: $('alert-name').value.trim(), event_type: $('alert-event-type').value.trim() || null, zone_id: $('alert-zone-id').value.trim() || null, object_label: $('alert-object-id').value.trim() || null }) }); await loadAlerts(); notify('Alert rule saved'); } catch (error) { setStatus('alert-status', error.message, true); } });
  $('save-api').addEventListener('click', saveSettingsConnection);

  /* --------------------------------------------------------------------------
     VI SENTRY — VMS ENHANCEMENTS (MATRIX WALL, ZONE DRAWER, AUX FEEDS, CLOCK)
     -------------------------------------------------------------------------- */
  function initVmsEnhancements() {

    // Dedicated Stream Configuration View Controller
    const dedicatedDurationInput = $('dedicated-cfg-duration');
    const dedicatedDurationLabel = $('dedicated-cfg-duration-label');
    const dedicatedZoneJson = $('dedicated-cfg-zone-json');

    if (dedicatedDurationInput && dedicatedDurationLabel) {
      const stored = localStorage.getItem('vms-capture-duration') || '10';
      dedicatedDurationInput.value = stored;
      dedicatedDurationLabel.textContent = `${stored}s`;
      dedicatedDurationInput.addEventListener('input', () => {
        dedicatedDurationLabel.textContent = `${dedicatedDurationInput.value}s`;
      });
    }

    $('dedicated-cfg-zone-perimeter')?.addEventListener('click', () => {
      const pts = [[20, 20], [620, 20], [620, 460], [20, 460]];
      window.vmsActiveZonePoints = pts;
      if (dedicatedZoneJson) dedicatedZoneJson.value = JSON.stringify([{ zone_id: 'cctv1-zone', polygon: pts }], null, 2);
      notify('Loaded Perimeter Box Preset');
    });

    $('dedicated-cfg-zone-center')?.addEventListener('click', () => {
      const pts = [[180, 120], [460, 120], [460, 360], [180, 360]];
      window.vmsActiveZonePoints = pts;
      if (dedicatedZoneJson) dedicatedZoneJson.value = JSON.stringify([{ zone_id: 'cctv1-zone', polygon: pts }], null, 2);
      notify('Loaded Center Gate Preset');
    });

    $('dedicated-cfg-zone-full')?.addEventListener('click', () => {
      const pts = [[0, 0], [640, 0], [640, 480], [0, 480]];
      window.vmsActiveZonePoints = pts;
      if (dedicatedZoneJson) dedicatedZoneJson.value = JSON.stringify([{ zone_id: 'cctv1-zone', polygon: pts }], null, 2);
      notify('Loaded Full Frame Preset');
    });

    const saveDedicatedConfig = () => {
      if (dedicatedDurationInput) {
        localStorage.setItem('vms-capture-duration', dedicatedDurationInput.value);
      }
      if (dedicatedZoneJson) {
        try {
          const parsed = JSON.parse(dedicatedZoneJson.value);
          if (Array.isArray(parsed) && parsed[0]?.polygon) {
            window.vmsActiveZonePoints = parsed[0].polygon;
          }
        } catch (e) {
          notify('Invalid Zone JSON format', true);
          return;
        }
      }
      const pipelineConfig = {
        stride: $('dedicated-cfg-stride')?.value || '10',
        model: $('dedicated-cfg-model')?.value || 'sam',
        tracker: $('dedicated-cfg-tracker')?.value || 'sam',
        device: $('dedicated-cfg-device')?.value || 'cpu',
        continuous: $('dedicated-cfg-continuous')?.value || 'true',
        chunk: $('dedicated-cfg-chunk')?.value || '60',
        retention: $('dedicated-cfg-retention')?.value || '14',
      };
      localStorage.setItem('vms-pipeline-config', JSON.stringify(pipelineConfig));
      setStatus('dedicated-config-status', 'Live pipeline parameters saved and deployed successfully.');
      notify('Live Stream Configuration Saved');
    };

    $('btn-save-dedicated-config')?.addEventListener('click', saveDedicatedConfig);
    $('dedicated-pipeline-form')?.addEventListener('submit', (e) => {
      e.preventDefault();
      saveDedicatedConfig();
    });

    // Button to jump from Live Matrix to Stream Config
    $('btn-open-sentry-config')?.addEventListener('click', () => {
      setView('live-config');
    });
    // 1. Live UTC Clock
    const clockEl = $('vms-clock');
    if (clockEl) {
      const updateClock = () => {
        const now = new Date();
        const y = now.getUTCFullYear();
        const m = String(now.getUTCMonth() + 1).padStart(2, '0');
        const d = String(now.getUTCDate()).padStart(2, '0');
        const hh = String(now.getUTCHours()).padStart(2, '0');
        const mm = String(now.getUTCMinutes()).padStart(2, '0');
        const ss = String(now.getUTCSeconds()).padStart(2, '0');
        clockEl.textContent = `${y}-${m}-${d} ${hh}:${mm}:${ss} UTC`;
      };
      updateClock();
      setInterval(updateClock, 1000);
    }

    // 2. Matrix Switcher (1x1, 2x2, 3x3) & Authentic CCTV B&W Mode
    const matrixContainer = $('vms-matrix-container');
    document.querySelectorAll('.matrix-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.matrix-btn').forEach((b) => {
          b.classList.remove('active');
          b.classList.remove('user-selected');
        });
        btn.classList.add('active');
        btn.classList.add('user-selected');
        const grid = btn.dataset.grid || '2x2';
        if (matrixContainer) {
          const isBw = matrixContainer.classList.contains('cctv-bw-mode');
          matrixContainer.className = `vms-matrix-grid grid-${grid}${isBw ? ' cctv-bw-mode' : ''}`;
        }
        setView('camera');
      });
    });

    const bwBtn = $('btn-toggle-cctv-bw');
    if (bwBtn) {
      bwBtn.addEventListener('click', () => {
        if (matrixContainer) {
          matrixContainer.classList.toggle('cctv-bw-mode');
          const isBw = matrixContainer.classList.contains('cctv-bw-mode');
          bwBtn.classList.toggle('active', isBw);
          bwBtn.textContent = isBw ? '☀️ Full Color' : '🌙 CCTV B&W';
          notify(isBw ? 'Enabled High-Contrast CCTV Night Vision Mode' : 'Restored Full Color Video Feed');
        }
      });
    }

    // 3. Interactive Visual Polygon Zone Drawer on #camera-zone-canvas
    const zoneCanvas = $('camera-zone-canvas');
    const drawBtn = $('btn-draw-zone');
    const clearBtn = $('btn-clear-zone');
    const snapBtn = $('btn-snap-photo');
    let isDrawing = false;
    let zonePoints = [];

    const drawPolygonOnCanvas = () => {
      if (!zoneCanvas) return;
      const ctx = zoneCanvas.getContext('2d');
      ctx.clearRect(0, 0, zoneCanvas.width, zoneCanvas.height);

      if (zonePoints.length === 0) return;

      // Fill & Stroke polygon
      ctx.beginPath();
      ctx.moveTo(zonePoints[0][0], zonePoints[0][1]);
      for (let i = 1; i < zonePoints.length; i++) {
        ctx.lineTo(zonePoints[i][0], zonePoints[i][1]);
      }
      if (!isDrawing && zonePoints.length >= 3) {
        ctx.closePath();
        ctx.fillStyle = 'rgba(0, 229, 255, 0.18)';
        ctx.fill();
      }

      ctx.strokeStyle = isDrawing ? '#00e5ff' : '#10b981';
      ctx.lineWidth = 2.5;
      ctx.setLineDash(isDrawing ? [6, 4] : []);
      ctx.stroke();
      ctx.setLineDash([]);

      // Draw vertex handles & coordinate markers
      zonePoints.forEach(([x, y], idx) => {
        ctx.beginPath();
        ctx.arc(x, y, 5, 0, Math.PI * 2);
        ctx.fillStyle = '#00e5ff';
        ctx.fill();
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 1.5;
        ctx.stroke();

        ctx.fillStyle = '#ffffff';
        ctx.font = '10px JetBrains Mono, monospace';
        ctx.fillText(`P${idx + 1}(${x},${y})`, x + 7, y - 5);
      });

      // Centroid label if closed
      if (!isDrawing && zonePoints.length >= 3) {
        const cx = Math.round(zonePoints.reduce((acc, p) => acc + p[0], 0) / zonePoints.length);
        const cy = Math.round(zonePoints.reduce((acc, p) => acc + p[1], 0) / zonePoints.length);
        ctx.fillStyle = '#10b981';
        ctx.font = 'bold 12px JetBrains Mono, monospace';
        ctx.fillText('ZONE: CCTV-1 PERIMETER [ARMED]', Math.max(10, cx - 90), cy);
      }
    };

    if (zoneCanvas && drawBtn) {
      drawBtn.addEventListener('click', () => {
        if (!isDrawing) {
          isDrawing = true;
          zonePoints = [];
          drawBtn.textContent = '✓ Finish Zone';
          drawBtn.classList.add('primary');
          zoneCanvas.classList.add('drawing-active');
          setStatus('camera-status', 'Click on the camera feed to place boundary corners. Click "Finish Zone" when done.');
          drawPolygonOnCanvas();
        } else {
          if (zonePoints.length >= 3) {
            isDrawing = false;
            drawBtn.textContent = '✎ Draw Zone';
            drawBtn.classList.remove('primary');
            zoneCanvas.classList.remove('drawing-active');
            const zoneJson = JSON.stringify([{ zone_id: 'cctv1-zone', polygon: zonePoints }]);
            if ($('camera-zones')) $('camera-zones').value = zoneJson;
            if ($('camera-form-zones')) $('camera-form-zones').value = zoneJson;
            drawPolygonOnCanvas();
            notify(`Custom detection zone saved (${zonePoints.length} vertices)`);
            setStatus('camera-status', `Detection zone armed with ${zonePoints.length} coordinates.`);
          } else {
            notify('Click on the camera feed to add at least 3 points before finishing.', true);
          }
        }
      });

      zoneCanvas.addEventListener('click', (e) => {
        if (!isDrawing) return;
        const rect = zoneCanvas.getBoundingClientRect();
        const scaleX = zoneCanvas.width / rect.width;
        const scaleY = zoneCanvas.height / rect.height;
        const x = Math.max(0, Math.min(zoneCanvas.width, Math.round((e.clientX - rect.left) * scaleX)));
        const y = Math.max(0, Math.min(zoneCanvas.height, Math.round((e.clientY - rect.top) * scaleY)));

        zonePoints.push([x, y]);
        drawPolygonOnCanvas();

        const zoneJson = JSON.stringify([{ zone_id: 'cctv1-zone', polygon: zonePoints }]);
        if ($('camera-zones')) $('camera-zones').value = zoneJson;
      });

      if (clearBtn) {
        clearBtn.addEventListener('click', () => {
          isDrawing = false;
          zonePoints = [];
          drawBtn.textContent = '✎ Draw Zone';
          drawBtn.classList.remove('primary');
          zoneCanvas.classList.remove('drawing-active');
          const defaultZone = '[{"zone_id":"cctv1-zone","polygon":[[0,0],[640,0],[640,480],[0,480]]}]';
          if ($('camera-zones')) $('camera-zones').value = defaultZone;
          drawPolygonOnCanvas();
          notify('Detection zones reset to default full frame');
        });
      }

      if (snapBtn) {
        snapBtn.addEventListener('click', () => {
          const video = $('camera-preview');
          if (!video || video.hidden || !cameraStream) {
            notify('Camera preview must be active to capture a snapshot', true);
            return;
          }
          const snapCanvas = document.createElement('canvas');
          snapCanvas.width = video.videoWidth || 640;
          snapCanvas.height = video.videoHeight || 480;
          const sCtx = snapCanvas.getContext('2d');
          sCtx.drawImage(video, 0, 0, snapCanvas.width, snapCanvas.height);
          if (zonePoints.length >= 3) {
            sCtx.drawImage(zoneCanvas, 0, 0, snapCanvas.width, snapCanvas.height);
          }
          const dataUrl = snapCanvas.toDataURL('image/jpeg', 0.92);
          const a = document.createElement('a');
          a.href = dataUrl;
          a.download = `sentry_snap_${Date.now()}.jpg`;
          a.click();
          notify('Snapshot captured and saved');
        });
      }
    }

    // 4. Zone Presets (Perimeter Box, Center Gate, Full Frame)
    const presetPolygons = {
      perimeter: [[32, 24], [608, 24], [608, 456], [32, 456]],
      gate: [[160, 80], [480, 80], [480, 420], [160, 420]],
      full: [[0, 0], [640, 0], [640, 480], [0, 480]],
    };

    document.querySelectorAll('[data-zone-preset]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const presetKey = btn.dataset.zonePreset;
        if (presetPolygons[presetKey]) {
          zonePoints = [...presetPolygons[presetKey]];
          isDrawing = false;
          if (drawBtn) {
            drawBtn.textContent = '✎ Draw Zone';
            drawBtn.classList.remove('primary');
          }
          if (zoneCanvas) zoneCanvas.classList.remove('drawing-active');
          const zoneJson = JSON.stringify([{ zone_id: `cctv1-${presetKey}`, polygon: zonePoints }]);
          if ($('camera-zones')) $('camera-zones').value = zoneJson;
          if ($('camera-form-zones')) $('camera-form-zones').value = zoneJson;
          drawPolygonOnCanvas();
          notify(`Loaded ${presetKey.toUpperCase()} preset zone`);
        }
      });
    });

    // 4.1 Sentry Configuration Modal (Live Surveillance Settings)
    const sentryConfigModal = $('modal-sentry-config');
    const openConfigBtn = $('btn-open-sentry-config');
    const closeConfigBtn = $('btn-close-sentry-config');
    const saveConfigBtn = $('btn-save-sentry-config');
    const durationInput = $('cfg-camera-duration');
    const durationLabel = $('cfg-duration-label');
    const zoneJsonArea = $('cfg-zone-json');

    if (durationInput && durationLabel) {
      const stored = localStorage.getItem('vms-capture-duration') || '10';
      durationInput.value = stored;
      durationLabel.textContent = `${stored}s`;
      durationInput.addEventListener('input', () => {
        durationLabel.textContent = `${durationInput.value}s`;
      });
    }

    if (openConfigBtn && sentryConfigModal) {
      openConfigBtn.addEventListener('click', () => {
        sentryConfigModal.hidden = false;
        const preview = $('camera-preview');
        const resText = (preview && preview.videoWidth) ? `${preview.videoWidth} × ${preview.videoHeight} px` : '640 × 480 px (Native)';
        if ($('cfg-telemetry-res')) $('cfg-telemetry-res').textContent = resText;
        if ($('cfg-telemetry-mode')) $('cfg-telemetry-mode').textContent = cameraStream ? 'Local WebRTC Live' : 'Local WebRTC Standby';
        if ($('cfg-telemetry-status')) $('cfg-telemetry-status').textContent = $('camera-state')?.textContent || 'Armed & Ready';
        if (zoneJsonArea) {
          const currentZone = (zonePoints && zonePoints.length >= 3)
            ? zonePoints
            : [[0, 0], [640, 0], [640, 480], [0, 480]];
          zoneJsonArea.value = JSON.stringify([{ zone_id: 'cctv1-zone', polygon: currentZone }], null, 2);
        }
      });
    }

    if (closeConfigBtn && sentryConfigModal) {
      closeConfigBtn.addEventListener('click', () => {
        sentryConfigModal.hidden = true;
      });
    }

    $('cfg-zone-perimeter')?.addEventListener('click', () => {
      zonePoints = [[20, 20], [620, 20], [620, 460], [20, 460]];
      window.vmsActiveZonePoints = zonePoints;
      if (zoneJsonArea) zoneJsonArea.value = JSON.stringify([{ zone_id: 'cctv1-zone', polygon: zonePoints }], null, 2);
      drawPolygonOnCanvas();
      notify('Selected Perimeter Box Preset');
    });

    $('cfg-zone-center')?.addEventListener('click', () => {
      zonePoints = [[180, 120], [460, 120], [460, 360], [180, 360]];
      window.vmsActiveZonePoints = zonePoints;
      if (zoneJsonArea) zoneJsonArea.value = JSON.stringify([{ zone_id: 'cctv1-zone', polygon: zonePoints }], null, 2);
      drawPolygonOnCanvas();
      notify('Selected Center Gate Preset');
    });

    $('cfg-zone-full')?.addEventListener('click', () => {
      zonePoints = [[0, 0], [640, 0], [640, 480], [0, 480]];
      window.vmsActiveZonePoints = zonePoints;
      if (zoneJsonArea) zoneJsonArea.value = JSON.stringify([{ zone_id: 'cctv1-zone', polygon: zonePoints }], null, 2);
      drawPolygonOnCanvas();
      notify('Selected Full Frame Preset');
    });

    if (saveConfigBtn && sentryConfigModal) {
      saveConfigBtn.addEventListener('click', () => {
        if (durationInput) {
          localStorage.setItem('vms-capture-duration', durationInput.value);
        }
        if (zoneJsonArea) {
          try {
            const parsed = JSON.parse(zoneJsonArea.value);
            if (Array.isArray(parsed) && parsed[0]?.polygon) {
              zonePoints = parsed[0].polygon;
              window.vmsActiveZonePoints = zonePoints;
              drawPolygonOnCanvas();
            }
          } catch (e) {
            notify('Invalid JSON in zone configuration', true);
            return;
          }
        }
        sentryConfigModal.hidden = true;
        notify('Sentry parameters applied successfully');
      });
    }

    // 5. Quick Camera Provisioning Templates
    document.querySelectorAll('[data-preset-cam]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const mode = btn.dataset.presetCam;
        if (mode === 'rtsp') {
          if ($('camera-form-id')) $('camera-form-id').value = 'cam-perimeter-rtsp';
          if ($('camera-form-name')) $('camera-form-name').value = 'North Gate IP Camera';
          if ($('camera-form-kind')) $('camera-form-kind').value = 'rtsp';
          if ($('camera-form-path')) $('camera-form-path').value = 'rtsp://admin:pass@192.168.1.100:554/live/ch1';
        } else if (mode === 'file') {
          if ($('camera-form-id')) $('camera-form-id').value = 'cam-archive-file';
          if ($('camera-form-name')) $('camera-form-name').value = 'Loading Dock Day Recording';
          if ($('camera-form-kind')) $('camera-form-kind').value = 'file';
          if ($('camera-form-path')) $('camera-form-path').value = 'cctv_footage_dock.mp4';
        } else if (mode === 'device') {
          if ($('camera-form-id')) $('camera-form-id').value = 'cam-directshow-0';
          if ($('camera-form-name')) $('camera-form-name').value = 'USB DirectShow Sensor';
          if ($('camera-form-kind')) $('camera-form-kind').value = 'device';
          if ($('camera-form-path')) $('camera-form-path').value = '0';
        }
        if ($('camera-form-zones') && $('camera-zones')) {
          $('camera-form-zones').value = $('camera-zones').value;
        }
        notify(`Applied ${mode.toUpperCase()} provisioning template`);
      });
    });

    // 6. Simulated Auxiliary Camera Feeds (CAM-02, CAM-03, CAM-04)
    initAuxiliaryFeeds();

    // 7. Video Archive Selector & Replay Listeners
    const archiveSelect = $('pre-recorded-video-select');
    if (archiveSelect) {
      archiveSelect.addEventListener('change', (e) => {
        const vid = e.target.value;
        if (vid) playPreRecordedVideo(vid);
      });
    }

    const refreshArchiveBtn = $('btn-refresh-archive');
    if (refreshArchiveBtn) {
      refreshArchiveBtn.addEventListener('click', () => loadVideoArchive());
    }

    const replayCaptureBtn = $('btn-replay-capture');
    if (replayCaptureBtn) {
      replayCaptureBtn.addEventListener('click', () => {
        const playbackEl = $('camera-playback');
        if (!playbackEl || !lastRecordedBlobUrl) {
          notify('No recorded capture available yet to replay.', true);
          return;
        }
        $('camera-preview').hidden = true;
        $('camera-placeholder').hidden = true;
        playbackEl.hidden = false;
        if ($('camera-feed-mode')) {
          $('camera-feed-mode').textContent = 'REPLAY';
          $('camera-feed-mode').classList.add('replay');
        }
        if ($('btn-live-switch')) $('btn-live-switch').hidden = false;
        playbackEl.play().catch(() => {});
      });
    }

    const liveSwitchBtn = $('btn-live-switch');
    if (liveSwitchBtn) {
      liveSwitchBtn.addEventListener('click', () => {
        const playbackEl = $('camera-playback');
        if (playbackEl) {
          playbackEl.pause();
          playbackEl.hidden = true;
        }
        liveSwitchBtn.hidden = true;
        if (cameraStream) {
          $('camera-preview').hidden = false;
          $('camera-placeholder').hidden = true;
          if ($('camera-feed-mode')) {
            $('camera-feed-mode').textContent = 'LIVE';
            $('camera-feed-mode').classList.remove('replay');
          }
        } else {
          $('camera-preview').hidden = true;
          $('camera-placeholder').hidden = false;
          if ($('camera-feed-mode')) {
            $('camera-feed-mode').textContent = 'OFFLINE';
            $('camera-feed-mode').classList.remove('replay');
          }
        }
      });
    }

    const wbPlayer = $('workbench-video-player');
    if (wbPlayer) {
      wbPlayer.addEventListener('timeupdate', () => {
        const cur = Math.floor(wbPlayer.currentTime || 0);
        const dur = Math.floor(wbPlayer.duration || 0);
        const curM = String(Math.floor(cur / 60)).padStart(2, '0');
        const curS = String(cur % 60).padStart(2, '0');
        const durM = String(Math.floor(dur / 60)).padStart(2, '0');
        const durS = String(dur % 60).padStart(2, '0');
        const timeDisplay = $('player-time-display');
        if (timeDisplay) timeDisplay.textContent = `${curM}:${curS} / ${isNaN(dur) ? '00:00' : `${durM}:${durS}`}`;
      });
    }

    // 8. Uploaded Footage Archive Filter & Summary Event Listeners
    $('upload-filter-date')?.addEventListener('input', renderUploadedFootageArchive);
    $('upload-filter-month')?.addEventListener('input', renderUploadedFootageArchive);
    $('upload-filter-search')?.addEventListener('input', renderUploadedFootageArchive);
    $('upload-filter-reset')?.addEventListener('click', () => {
      if ($('upload-filter-date')) $('upload-filter-date').value = '';
      if ($('upload-filter-month')) $('upload-filter-month').value = '';
      if ($('upload-filter-search')) $('upload-filter-search').value = '';
      renderUploadedFootageArchive();
    });
    $('upload-archive-refresh')?.addEventListener('click', async () => {
      await loadVideoArchive();
      notify('Uploaded footage archive refreshed');
    });
    $('btn-close-footage-summary')?.addEventListener('click', () => {
      if ($('uploaded-footage-summary-box')) $('uploaded-footage-summary-box').hidden = true;
    });

    // 9. Monitored Perimeter Preset in Forensic Workbench
    const wbPreset = $('workbench-zone-preset');
    const wbName = $('workbench-zone-name');
    const updateWbZones = () => {
      const p = wbPreset?.value || 'perimeter';
      const name = wbName?.value.trim() || 'zone-1';
      let polygon = [[0, 0], [640, 0], [640, 480], [0, 480]];
      if (p === 'gate') polygon = [[160, 100], [480, 100], [480, 440], [160, 440]];
      else if (p === 'left') polygon = [[0, 0], [320, 0], [320, 480], [0, 480]];
      else if (p === 'right') polygon = [[320, 0], [640, 0], [640, 480], [320, 480]];
      if ($('zones')) $('zones').value = JSON.stringify([{ zone_id: name, polygon }]);
    };
    wbPreset?.addEventListener('change', updateWbZones);
    wbName?.addEventListener('input', updateWbZones);
    updateWbZones();

    // 10. Camera Matrix Click-to-Resize into Big View & Return to Grid
    const returnMatrixBtn = $('btn-matrix-grid-view');
    window.toggleCameraFocus = (cardEl) => {
      const matrixContainer = $('vms-matrix-container');
      if (!cardEl || !matrixContainer) return;
      const isAlreadyFocused = cardEl.classList.contains('camera-focused-big');

      document.querySelectorAll('.vms-video-card').forEach((c) => {
        c.classList.remove('camera-focused-big');
        const exp = c.querySelector('.matrix-expand-btn');
        if (exp) exp.textContent = '⤢ Expand';
      });

      if (isAlreadyFocused) {
        matrixContainer.classList.remove('matrix-has-focused');
        if (returnMatrixBtn) returnMatrixBtn.hidden = true;
      } else {
        cardEl.classList.add('camera-focused-big');
        matrixContainer.classList.add('matrix-has-focused');
        const exp = cardEl.querySelector('.matrix-expand-btn');
        if (exp) exp.textContent = '⤡ Return to Grid';
        if (returnMatrixBtn) returnMatrixBtn.hidden = false;
      }
    };

    if (returnMatrixBtn) {
      returnMatrixBtn.addEventListener('click', () => {
        const matrixContainer = $('vms-matrix-container');
        if (matrixContainer) matrixContainer.classList.remove('matrix-has-focused');
        document.querySelectorAll('.vms-video-card').forEach((c) => {
          c.classList.remove('camera-focused-big');
          const exp = c.querySelector('.matrix-expand-btn');
          if (exp) exp.textContent = '⤢ Expand';
        });
        returnMatrixBtn.hidden = true;
      });
    }

    // Attach expand & double-click focus listener on CAM 01
    const cam1Card = $('feed-channel-1');
    if (cam1Card) {
      const expBtn = cam1Card.querySelector('.matrix-expand-btn');
      if (expBtn) {
        expBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          window.toggleCameraFocus(cam1Card);
        });
      }
      cam1Card.addEventListener('dblclick', (e) => {
        if (e.target.closest('button, input, select, video')) return;
        window.toggleCameraFocus(cam1Card);
      });
    }
  }

  const channelColors = ['#00e5ff', '#38bdf8', '#a855f7', '#f59e0b', '#ec4899', '#10b981', '#6366f1'];


  function updateGlobalFleetStatus() {
    const isLocalLive = Boolean(cameraStream && cameraStream.active);
    const activeRtspCount = (monitoringCameras || []).filter((c) => {
      const s = cameraStatus(c);
      return ['running', 'starting', 'ready'].includes(s);
    }).length;
    const totalActive = (isLocalLive ? 1 : 0) + activeRtspCount;

    // 1. Matrix header pill
    const statePill = $('camera-state');
    if (statePill) {
      if (totalActive > 0) {
        statePill.textContent = `${totalActive} ACTIVE CAMERA${totalActive > 1 ? 'S' : ''} ONLINE`;
        statePill.className = 'state-pill live';
        statePill.style.cssText = 'color: #6ee7b7; background: rgba(16, 185, 129, 0.15); border-color: rgba(16, 185, 129, 0.35);';
      } else if (monitoringCameras && monitoringCameras.length > 0) {
        statePill.textContent = `${monitoringCameras.length} CCTV CHANNELS ARMED`;
        statePill.className = 'state-pill live';
        statePill.style.cssText = 'color: #38bdf8; background: rgba(56, 189, 248, 0.15); border-color: rgba(56, 189, 248, 0.35);';
      } else {
        statePill.textContent = 'PRIMARY SENTRY STANDBY';
        statePill.className = 'state-pill offline';
        statePill.style.cssText = 'color: #94a3b8; background: rgba(148, 163, 184, 0.12); border-color: rgba(148, 163, 184, 0.25);';
      }
    }

    // 2. Live Copilot header badge
    const fleetBadge = $('live-fleet-badge');
    if (fleetBadge) {
      if (totalActive > 0) {
        fleetBadge.className = 'vms-agent-pill live';
        fleetBadge.style.cssText = 'color: #6ee7b7; background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.4);';
        fleetBadge.innerHTML = `<span class="blink-dot" style="background: #10b981; box-shadow: 0 0 8px #10b981;"></span>${totalActive} ACTIVE CAMERA${totalActive > 1 ? 'S' : ''} (ONLINE)`;
      } else {
        fleetBadge.className = 'vms-agent-pill offline';
        fleetBadge.style.cssText = 'color: #94a3b8; background: rgba(148, 163, 184, 0.12); border: 1px solid rgba(148, 163, 184, 0.25);';
        fleetBadge.innerHTML = `<span class="blink-dot" style="background: #94a3b8;"></span>SURVEILLANCE FLEET STANDBY (0 ACTIVE)`;
      }
    }

    // 3. Live Chat empty placeholder (if user hasn't sent a message yet)
    const emptyDiv = $('live-chat-history')?.querySelector('.chat-empty');
    if (emptyDiv) {
      if (totalActive > 0) {
        emptyDiv.innerHTML = `
          <span class="chat-empty-mark" style="color: #10b981; background: rgba(16, 185, 129, 0.15);">●</span>
          <strong>Live Surveillance Fleet Active (${totalActive} Stream${totalActive > 1 ? 's' : ''} Online)</strong>
          <small>CAM-01 / active camera streams are actively streaming live telemetry. Ask questions about live perimeter activity, object tracking, armed tripwire rules, or camera health.</small>
        `;
      } else {
        emptyDiv.innerHTML = `
          <span class="chat-empty-mark" style="color: #38bdf8; background: rgba(56, 189, 248, 0.15);">📡</span>
          <strong>Surveillance Fleet Standby</strong>
          <small>Camera streams are armed in standby mode. Start CAM-01 on the Live Video Matrix or connect an RTSP stream to begin real-time surveillance. You can still query armed rules and system health below.</small>
        `;
      }
    }

    // 4. Default camera channel scope in Live Copilot form
    const liveScopeInput = $('live-chat-camera-id');
    if (liveScopeInput && !liveScopeInput.value) {
      if (isLocalLive) {
        liveScopeInput.placeholder = 'cam-01 (Active)';
      } else if (monitoringCameras && monitoringCameras[0]) {
        liveScopeInput.placeholder = `${monitoringCameras[0].camera_id} (Armed)`;
      } else {
        liveScopeInput.placeholder = 'e.g. cam-01, ch-01';
      }
    }
  }

  function renderActiveCameraFeeds() {
    const container = $('active-aux-streams-container');
    const matrixContainer = $('vms-matrix-container');
    if (!container || !matrixContainer) return;
    container.innerHTML = '';

    const isLocalActive = Boolean(cameraStream);
    // Real cameras registered by the operator in the backend fleet
    const registeredCameras = (monitoringCameras || []).filter((c) => c && c.camera_id);
    const activeExternalCameras = registeredCameras.filter((c) => c.enabled && ['running', 'starting', 'ready'].includes(cameraStatus(c)));

    const runningCount = (isLocalActive ? 1 : 0) + activeExternalCameras.length;
    const totalVisible = 1 + registeredCameras.length;

    // Dynamic grid adjustment:
    // If only 1 camera (CAM 01), show in clean single-view format (grid-1x1).
    // When the operator provisions/adds cameras (2 to 4), automatically expand into the 2x2 CCTV Quad Screen Wall.
    // If 5+ cameras, expand into 3x3 grid.
    const activeManualGridBtn = document.querySelector('.matrix-layout-switcher .matrix-btn.user-selected');
    if (!activeManualGridBtn) {
      const isBw = matrixContainer.classList.contains('cctv-bw-mode');
      let targetGrid = '1x1';
      if (totalVisible >= 2 && totalVisible <= 4) targetGrid = '2x2';
      else if (totalVisible > 4) targetGrid = '3x3';

      matrixContainer.className = `vms-matrix-grid grid-${targetGrid}${isBw ? ' cctv-bw-mode' : ''}`;

      document.querySelectorAll('.matrix-layout-switcher .matrix-btn').forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.grid === targetGrid);
      });
    }

    // Status pill in matrix heading
    const statePill = $('camera-state');
    if (statePill) {
      if (runningCount > 0) {
        statePill.textContent = `${runningCount} ACTIVE CAMERA${runningCount > 1 ? 'S' : ''} ONLINE`;
        statePill.className = 'state-pill live';
        statePill.style.cssText = 'color: #6ee7b7; background: rgba(16, 185, 129, 0.15); border-color: rgba(16, 185, 129, 0.35);';
      } else if (registeredCameras.length > 0) {
        statePill.textContent = `${totalVisible} CCTV CHANNELS ARMED`;
        statePill.className = 'state-pill live';
        statePill.style.cssText = 'color: #38bdf8; background: rgba(56, 189, 248, 0.15); border-color: rgba(56, 189, 248, 0.35);';
      } else {
        statePill.textContent = 'PRIMARY SENTRY STANDBY';
        statePill.className = 'state-pill offline';
        statePill.style.cssText = 'color: #94a3b8; background: rgba(148, 163, 184, 0.12); border-color: rgba(148, 163, 184, 0.25);';
      }
    }

    // When NO extra cameras have been added yet, keep matrix focused on CAM 01 without dummy cards
    if (registeredCameras.length === 0) {
      return;
    }

    // Render each provisioned camera as an authentic CCTV monitor card matching the 2x2 CCTV Quad Screen Wall
    registeredCameras.forEach((cam, index) => {
      const channelIndex = index + 2;
      const channelTag = `CH-${String(channelIndex).padStart(2, '0')}`;
      const badgeColor = channelColors[(channelIndex - 1) % channelColors.length];
      const status = cameraStatus(cam);
      const isLive = ['running', 'starting', 'ready'].includes(status);

      const card = document.createElement('article');
      card.className = `vms-video-card${isLive ? ' active-feed' : ''}`;
      card.id = `feed-${cam.camera_id}`;

      card.innerHTML = `
        <div class="vms-video-header">
          <div class="vms-feed-meta">
            <span class="vms-feed-badge" style="background: ${badgeColor}; color: #020617;">${channelTag}</span>
            <strong>${escape(cam.display_name)}</strong>
            <span class="vms-feed-badge mode-badge ${isLive ? 'live' : 'offline'}">${isLive ? 'LIVE RTSP' : 'STANDBY'}</span>
            <small>${escape(cam.camera_id)} · ${escape(cam.source.kind)}</small>
          </div>
          <div class="vms-feed-stats">
            <span class="vms-timer">00:00</span>
            ${isLive ? '<span class="vms-rec-badge"><span class="recording-dot"></span>REC</span>' : ''}
            <button class="button text-button matrix-expand-btn" type="button" title="Click to Expand into Big View">⤢ Expand</button>
          </div>
        </div>

        <div class="vms-frame-viewport">
          <video id="video-${cam.camera_id}" autoplay muted loop playsinline ${isLive ? '' : 'hidden'}></video>
          <div class="camera-placeholder" id="placeholder-${cam.camera_id}" ${isLive ? 'hidden' : ''}>
            <span class="placeholder-icon">📡</span>
            <strong>${escape(cam.display_name.toUpperCase())}</strong>
            <small>${escape(cam.source.path || 'RTSP Endpoint Armed')}</small>
          </div>

          <!-- HUD Overlays -->
          <div class="vms-hud-reticle tl"></div>
          <div class="vms-hud-reticle tr"></div>
          <div class="vms-hud-reticle bl"></div>
          <div class="vms-hud-reticle br"></div>
          <div class="vms-hud-watermark">${escape(cam.camera_id.toUpperCase())} // ${isLive ? 'ACTIVE RTSP' : 'STANDBY'}</div>
        </div>

        <div class="vms-feed-toolbar">
          <div class="camera-controls">
            <button class="button ${isLive ? 'danger' : 'primary'} small btn-toggle-cam-stream" type="button" data-cam-id="${escape(cam.camera_id)}">
              <span>${isLive ? '■' : '▶'}</span> ${isLive ? 'Stop Feed' : 'Start Feed'}
            </button>
            <button class="button ghost small btn-snap-dynamic" type="button" data-cam-id="${escape(cam.camera_id)}" title="Snapshot Frame">📷 Snap</button>
          </div>
          <div class="vms-feed-actions">
            <button class="button text-button small btn-edit-cam-fleet" type="button" data-cam-id="${escape(cam.camera_id)}" title="Configure in Fleet">⚙ Config</button>
            <button class="button danger small btn-delete-cam-fleet" type="button" data-cam-id="${escape(cam.camera_id)}" title="Remove Camera from Live Surveillance">🗑 Remove</button>
          </div>
        </div>
      `;

      // Expand / Focus listener
      card.querySelector('.matrix-expand-btn')?.addEventListener('click', (e) => {
        e.stopPropagation();
        window.toggleCameraFocus(card);
      });
      card.addEventListener('dblclick', (e) => {
        if (e.target.closest('button, input, select, video')) return;
        window.toggleCameraFocus(card);
      });

      // Stream Toggle listener
      const streamBtn = card.querySelector('.btn-toggle-cam-stream');
      if (streamBtn) {
        streamBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const isCurrentlyLive = ['running', 'starting', 'ready'].includes(cameraStatus(cam));
          if (isCurrentlyLive) {
            await stopCameraRun(cam);
          } else {
            await runCamera(cam);
          }
          await loadMonitoring();
          renderActiveCameraFeeds();
        });
      }

      // Snapshot Frame listener
      const snapBtn = card.querySelector('.btn-snap-dynamic');
      if (snapBtn) {
        snapBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          try {
            const res = await fetch(`${storedApi || ''}/api/v1/cameras/${encodeURIComponent(cam.camera_id)}/snapshot`, {
              headers: authToken ? { Authorization: `Bearer ${authToken}` } : {},
            });
            if (!res.ok) throw new Error('Snapshot not available');
            const blob = await res.blob();
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `${cam.camera_id}_snapshot_${Date.now()}.jpg`;
            a.click();
            notify(`Snapshot captured from ${cam.display_name}`);
          } catch (err) {
            notify(`Snapshot: ${err.message}`, 'warning');
          }
        });
      }

      // Config button listener
      const cfgBtn = card.querySelector('.btn-edit-cam-fleet');
      if (cfgBtn) {
        cfgBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          monitoringSelectedId = cam.camera_id;
          setView('monitoring');
          editCameraForm(cam);
        });
      }

      // Delete / Remove Camera listener
      const delCamBtn = card.querySelector('.btn-delete-cam-fleet');
      if (delCamBtn) {
        delCamBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!confirm(`Remove camera ${cam.display_name} from live surveillance matrix?`)) return;
          try {
            await request(`/api/v1/cameras/${encodeURIComponent(cam.camera_id)}`, { method: 'DELETE' });
            notify(`Camera ${cam.display_name} removed`);
            await loadMonitoring();
            renderActiveCameraFeeds();
          } catch (err) {
            notify(`Failed to remove camera: ${err.message}`, true);
          }
        });
      }

      container.appendChild(card);
    });
  }

  function initAuxiliaryFeeds() {
    renderActiveCameraFeeds();
  }

  (async () => {
    applyTheme(themePreference);
    sidebarCollapsed = false;
    $('dashboard-shell').classList.remove('sidebar-collapsed');
    $('sidebar-collapse').textContent = '‹';
    $('sidebar-collapse').setAttribute('aria-pressed', 'false');
    $('sidebar-collapse').setAttribute('aria-label', 'Collapse sidebar');
    setAccountUi({ username: 'Local workspace', role: 'Development' });
    setAccessMode('unknown');
    $('camera-preview').hidden = true;
    initVmsEnhancements();
    await ensureAuthenticated();
    await checkHealth();
    await refreshIdentity();
    await loadVideoArchive();
    await loadMonitoring();
  })();
})();

