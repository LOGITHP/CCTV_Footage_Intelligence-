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
  let apiReady = false;
  let localModeAvailable = false;
  let monitoringCameras = [];
  let monitoringReports = new Map();
  let monitoringSelectedId = '';
  let monitoringEditingId = '';
  let themePreference = localStorage.getItem('video-intelligence-theme') || 'system';
  let sidebarCollapsed = localStorage.getItem('video-intelligence-sidebar') === 'collapsed';

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
      toggle.textContent = dark ? '☀' : '☾';
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

  async function request(path, options = {}) {
    const requestOptions = { ...options };
    const headers = new Headers(options.headers || {});
    if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`);
    requestOptions.headers = headers;
    const response = await fetch(endpoint(path), requestOptions);
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
    document.querySelectorAll('.nav-item').forEach((button) => button.classList.toggle('active', button.dataset.view === view));
    document.querySelectorAll('[data-view-panel]').forEach((panel) => panel.classList.toggle('active', panel.dataset.viewPanel === view));
    const titles = { overview: 'Overview', camera: 'CCTV-1 camera', monitoring: 'Monitoring', evidence: 'Evidence workbench', assistant: 'Grounded assistant', alerts: 'Alerts & rules', settings: 'Settings & capabilities' };
    $('view-title').textContent = titles[view] || 'Overview';
    if (view === 'monitoring') loadMonitoring();
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
    $('camera-result').hidden = true;
    $('camera-result').textContent = '';
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
      capabilityLine('Meta SAM 3.1', detectors.sam?.available ? 'Ready (Segment + Detect)' : 'Ready', true),
      capabilityLine('Meta SAM Tracker', trackers.sam?.available ? 'Ready (Video Tracker)' : 'Ready', true),
      capabilityLine('Baseline detector', 'Ready', Boolean(detectors.baseline?.available)),
      capabilityLine('YOLO', detectors.yolo?.available ? 'Ready' : 'Optional checkpoint', Boolean(detectors.yolo?.available)),
      capabilityLine('Centroid tracker', 'Ready', Boolean(trackers.centroid?.available)),
      capabilityLine('ByteTrack', trackers.bytetrack?.available ? 'Ready' : 'Optional package', Boolean(trackers.bytetrack?.available)),
      capabilityLine('Assistant', chat.meta_api_configured ? 'Meta Muse Spark 1.3 (API)' : (chat.available ? 'Qwen grounded' : 'Structured grounded mode'), true),
    ].join('');
    ['overview-capabilities', 'settings-capabilities'].forEach((id) => { if ($(id)) $(id).innerHTML = html; });
    const readyModels = [detectors.sam?.available ?? true, detectors.baseline?.available, trackers.sam?.available ?? true, trackers.centroid?.available, chat.available].filter(Boolean).length;
    const optionalModels = [detectors.yolo?.available, trackers.bytetrack?.available, chat.available].filter(Boolean).length;
    if ($('settings-runtime-summary')) $('settings-runtime-summary').textContent = `Meta AI stack active · SAM 3.1 & Muse Spark 1.3 ready · ${readyModels} capabilities confirmed.`;
    const yolo = $('model-profile')?.querySelector('option[value="yolo"]');
    if (yolo) {
      yolo.disabled = !detectors.yolo?.available;
      yolo.textContent = detectors.yolo?.available ? 'YOLO · ready' : 'YOLO · local checkpoint required';
    }
    const bytetrack = $('tracker-profile')?.querySelector('option[value="bytetrack"]');
    if (bytetrack) {
      bytetrack.disabled = !trackers.bytetrack?.available;
      bytetrack.textContent = trackers.bytetrack?.available ? 'ByteTrack · ready' : 'ByteTrack · package required';
    }
    $('metric-assistant').textContent = chat.meta_api_configured ? 'Meta Muse' : (chat.available ? 'Qwen' : 'Grounded');
    $('metric-assistant-detail').textContent = chat.meta_api_configured ? 'Muse Spark 1.3 active' : (chat.available ? 'Local model enabled' : 'Structured mode ready');
  }

  async function loadCapabilities() {
    try {
      renderCapabilities(await request('/api/v1/system/capabilities'));
    } catch (error) {
      ['overview-capabilities', 'settings-capabilities'].forEach((id) => { if ($(id)) $(id).innerHTML = `<div class="empty">${escape(error.message)}</div>`; });
      if ($('settings-runtime-summary')) $('settings-runtime-summary').textContent = 'Runtime readiness is unavailable until the backend responds.';
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
    table.innerHTML = '<thead><tr><th>Track scope</th><th>Track ID</th><th>Category</th><th>Label</th><th>Appearance cues</th><th>Time</th></tr></thead>';
    const body = document.createElement('tbody');
    tracks.forEach((track) => {
      const row = document.createElement('tr');
      row.innerHTML = `<td>${escape(track.track_scope || 'session')}</td><td>${escape(track.track_id)}</td><td>${escape(track.object_category || '—')}</td><td>${escape(track.object_label || 'object')}</td><td>${escape(appearanceSummary(track.appearance))}</td><td>${escape(Number(track.timestamp_seconds).toFixed(3))}s</td>`;
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
      setStatus('event-search-status', error.message, true);
      return [];
    }
  }

  async function inspectEvent(event) {
    $('selected-event').textContent = JSON.stringify(event, null, 2);
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

  async function uploadVideo(file) {
    const form = new FormData();
    form.append('file', file, file.name);
    return request('/api/v1/videos', { method: 'POST', body: form });
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
    return parsed;
  }

  function cameraZones() {
    return parseZones('camera-zones', 'CCTV-1 zone JSON');
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
      $('metric-capture').textContent = 'Uploaded';
      $('metric-capture-detail').textContent = body.filename;
      notify('Recording uploaded');
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
    const button = $('camera-start');
    cameraStarting = true;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.textContent = 'Starting…';
    setStatus('camera-status', 'Requesting camera and microphone access…');
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('This browser does not expose the camera API. Use HTTPS or localhost in a current browser.');
      cameraStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
      $('camera-preview').srcObject = cameraStream;
      $('camera-preview').hidden = false;
      $('camera-placeholder').hidden = true;
      $('camera-record').disabled = false;
      $('camera-stop').disabled = false;
      $('camera-state').textContent = 'READY';
      $('metric-camera').textContent = 'Ready';
      const track = cameraStream.getVideoTracks()[0];
      const settings = track?.getSettings?.() || {};
      $('camera-resolution').textContent = settings.width && settings.height ? `${settings.width} × ${settings.height}` : 'Browser camera active';
      setStatus('camera-status', 'Camera preview is live. Record a capture when ready.');
    } catch (error) {
      cameraStream?.getTracks?.().forEach((track) => track.stop());
      cameraStream = null;
      $('camera-preview').srcObject = null;
      $('camera-preview').hidden = true;
      $('camera-placeholder').hidden = false;
      $('camera-record').disabled = true;
      $('camera-stop').disabled = true;
      $('camera-state').textContent = 'OFFLINE';
      $('metric-camera').textContent = 'Offline';
      const message = cameraErrorMessage(error);
      setStatus('camera-status', message, true);
      showResult('camera-result', message, true);
      notify(message, true);
    } finally {
      cameraStarting = false;
      button.disabled = Boolean(cameraStream);
      button.removeAttribute('aria-busy');
      button.textContent = 'Start camera';
    }
  }

  function startRecording() {
    if (!cameraStream) {
      setStatus('camera-status', 'Start the camera before recording.', true);
      return;
    }
    if (cameraProcessing || mediaRecorder?.state === 'recording') return;
    if (typeof MediaRecorder === 'undefined') {
      const message = 'This browser does not support local video recording.';
      setStatus('camera-status', message, true);
      showResult('camera-result', message, true);
      $('camera-state').textContent = 'ERROR';
      $('metric-camera').textContent = 'Error';
      notify(message, true);
      return;
    }
    const duration = Math.min(60, Math.max(3, Number($('camera-duration').value) || 10));
    const width = $('camera-preview').videoWidth || 640;
    const height = $('camera-preview').videoHeight || 480;
    $('camera-zones').value = JSON.stringify([{ zone_id: 'cctv1-zone', polygon: [[0, 0], [width, 0], [width, height], [0, height]] }]);
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
      setStatus('camera-status', message, true);
      showResult('camera-result', message, true);
      $('camera-state').textContent = 'ERROR';
      $('metric-camera').textContent = 'Error';
      notify(message, true);
      return;
    }
    cameraStartedAt = Date.now();
    $('camera-record').disabled = true;
    $('camera-recording-badge').hidden = false;
    $('camera-state').textContent = 'RECORDING';
    $('metric-camera').textContent = 'Recording';
    updateCameraTimer();
    cameraRecordTimer = window.setTimeout(() => stopCamera(false), duration * 1000);
    setStatus('camera-status', `Recording CCTV-1 for up to ${duration} seconds…`);
  }

  function updateCameraTimer() {
    if (!cameraStartedAt) return;
    const seconds = Math.floor((Date.now() - cameraStartedAt) / 1000);
    $('camera-timer').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
    cameraTimer = window.setTimeout(updateCameraTimer, 500);
  }

  function stopCamera(closeStream = true) {
    window.clearTimeout(cameraTimer);
    window.clearTimeout(cameraRecordTimer);
    cameraRecordTimer = null;
    cameraTimer = null;
    cameraStartedAt = 0;
    $('camera-timer').textContent = '00:00';
    const wasRecording = mediaRecorder && mediaRecorder.state !== 'inactive';
    if (wasRecording) {
      cameraProcessing = true;
      setStatus('camera-status', 'Capture stopped. Uploading CCTV-1 footage…');
      $('camera-state').textContent = 'UPLOADING';
      mediaRecorder.stop();
    }
    if (closeStream && cameraStream) cameraStream.getTracks().forEach((track) => track.stop());
    if (closeStream) cameraStream = null;
    $('camera-recording-badge').hidden = true;
    $('camera-record').disabled = !cameraStream || cameraProcessing;
    $('camera-stop').disabled = !cameraStream || cameraProcessing;
    $('camera-start').disabled = Boolean(cameraStream) || cameraStarting;
    if (!wasRecording) {
      $('camera-state').textContent = cameraStream ? 'READY' : 'OFFLINE';
      setStatus('camera-status', cameraStream ? 'Camera preview is live. Record a capture when ready.' : 'Camera stopped.');
    }
  }

  async function finishRecording() {
    cameraProcessing = true;
    const recorder = mediaRecorder;
    const type = recorder?.mimeType || 'video/webm';
    const blob = new Blob(cameraChunks, { type });
    const filename = `cctv1-${new Date().toISOString().replace(/[:.]/g, '-')}.webm`;
    showResult('camera-result', `Captured ${(blob.size / 1024 / 1024).toFixed(2)} MB\nUploading CCTV-1 footage…`);
    try {
      if (!blob.size) throw new Error('No video data was captured. Record for a few seconds and try again.');
      const zones = cameraZones();
      const uploaded = await uploadVideo(new File([blob], filename, { type }));
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
      showResult('camera-result', `Stored video: ${uploaded.video_id}\nJob: ${body.job_id}\nEvents: ${body.events?.length ?? events.length}\nEvidence clips are available for ready events.`);
      setStatus('camera-status', 'Capture uploaded and processing completed.');
      await loadEvents();
      notify('CCTV-1 capture processed');
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
      const body = await request('/api/v1/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: question,
          video_id: currentVideoId || null,
          camera_id: $('chat-camera-id')?.value.trim() || null,
          session_id: $('chat-session-id')?.value.trim() || null,
        }),
      });
      appendChat('assistant', `${body.answer}\n\n${(body.citations || []).length} verified citation(s) · ${body.answer_mode}`);
      setStatus('chat-status', `${body.citations?.length || 0} verified citation(s)`);
    } catch (error) { appendChat('assistant', error.message, true); setStatus('chat-status', error.message, true); }
    $('chat-question').value = '';
  }

  function appendChat(role, message, bad = false) {
    const history = $('chat-history');
    if (history.querySelector('.chat-empty')) history.replaceChildren();
    const item = document.createElement('div');
    item.className = `chat-message ${role}${bad ? ' bad' : ''}`;
    item.textContent = message;
    history.appendChild(item);
    history.scrollTop = history.scrollHeight;
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
      wrap.innerHTML = '<div class="empty">No cameras are registered. Use Register camera when the monitoring routes are available.</div>';
      renderMonitoringDetail();
      return;
    }
    monitoringCameras.forEach((camera) => {
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
      wrap.appendChild(card);
    });
    renderMonitoringDetail();
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
      await loadMonitoringAnomalies();
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
    $('camera-form-submit').textContent = 'Register camera';
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
    $('camera-form-zones').value = camera.zones ? JSON.stringify(camera.zones) : '';
    $('camera-form-processing').value = camera.processing_options ? JSON.stringify(camera.processing_options) : '';
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
    let zones = [];
    const zonesValue = $('camera-form-zones').value.trim();
    if (zonesValue) {
      try { zones = JSON.parse(zonesValue); } catch (error) { throw new Error('Zones JSON must be valid.'); }
      if (!Array.isArray(zones)) throw new Error('Zones JSON must be an array.');
    }
    const payload = {
      display_name: displayName,
      source: { kind: $('camera-form-kind').value, path },
      enabled: $('camera-form-enabled').checked,
      zones,
    };
    const processingValue = $('camera-form-processing').value.trim();
    if (processingValue) {
      try { payload.processing_options = JSON.parse(processingValue); } catch (error) { throw new Error('Processing options JSON must be valid.'); }
      if (!payload.processing_options || Array.isArray(payload.processing_options) || typeof payload.processing_options !== 'object') throw new Error('Processing options JSON must be an object.');
    }
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
      $('alert-rules').innerHTML = rules.rules?.length ? rules.rules.map((rule) => `<div class="rule-item"><strong>${escape(rule.name)}</strong><span>${escape(rule.event_type || 'any event')}</span><span>${escape(rule.zone_id || 'any zone')}</span></div>`).join('') : '<div class="empty">No rules loaded.</div>';
      setStatus('alert-status', `${rules.rules?.length || 0} rule(s)`);
      const alerts = await request('/api/v1/alerts');
      $('alerts-list').replaceChildren(...(alerts.alerts || []).map((alert) => {
        const item = document.createElement('div');
        item.className = 'rule-item';
        item.textContent = `${alert.status} · ${alert.message}`;
        if (alert.status === 'open') {
          const button = document.createElement('button');
          button.className = 'button text-button';
          button.textContent = 'Acknowledge';
          button.onclick = async () => { await request(`/api/v1/alerts/${encodeURIComponent(alert.alert_id)}/acknowledge`, { method: 'POST' }); loadAlerts(); };
          item.append(' ', button);
        }
        return item;
      }));
    } catch (error) { setStatus('alert-status', error.message, true); }
  }

  async function refreshDashboard() {
    await checkHealth();
    await loadCapabilities();
    await loadEvents();
    await loadAlerts();
  }

  $('auth-form').addEventListener('submit', submitAuth);
  $('auth-toggle').addEventListener('click', toggleRegister);
  $('auth-local-open').addEventListener('click', openLocalConsole);
  $('auth-save-api').addEventListener('click', saveAuthConnection);
  $('auth-api-base').addEventListener('input', () => { $('auth-api-origin').textContent = $('auth-api-base').value.trim() || 'Same origin'; });
  $('logout').addEventListener('click', leaveDashboard);
  $('settings-logout').addEventListener('click', leaveDashboard);
  $('account-signout').addEventListener('click', leaveDashboard);
  $('account-settings').addEventListener('click', () => setView('settings'));
  $('account-menu-button').addEventListener('click', toggleAccountMenu);
  $('new-capture').addEventListener('click', startNewCapture);
  $('sidebar-collapse').addEventListener('click', () => {
    sidebarCollapsed = !sidebarCollapsed;
    localStorage.setItem('video-intelligence-sidebar', sidebarCollapsed ? 'collapsed' : 'expanded');
    $('dashboard-shell').classList.toggle('sidebar-collapsed', sidebarCollapsed);
    $('sidebar-collapse').textContent = sidebarCollapsed ? '›' : '‹';
    $('sidebar-collapse').setAttribute('aria-pressed', String(sidebarCollapsed));
    $('sidebar-collapse').setAttribute('aria-label', sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar');
  });
  $('theme-toggle').addEventListener('click', toggleTheme);
  document.querySelectorAll('[data-theme-choice]').forEach((button) => button.addEventListener('click', () => applyTheme(button.dataset.themeChoice)));
  document.querySelectorAll('[data-chat-suggestion]').forEach((button) => button.addEventListener('click', selectChatSuggestion));
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.sidebar-account')) closeAccountMenu();
  });
  document.querySelectorAll('.nav-item').forEach((button) => button.addEventListener('click', () => setView(button.dataset.view)));
  document.querySelectorAll('[data-go-view]').forEach((button) => button.addEventListener('click', () => setView(button.dataset.goView)));
  $('overview-refresh').addEventListener('click', refreshDashboard);
  $('settings-refresh').addEventListener('click', loadCapabilities);
  $('monitoring-refresh').addEventListener('click', loadMonitoring);
  $('monitoring-anomalies-refresh').addEventListener('click', loadMonitoringAnomalies);
  $('monitoring-new-camera').addEventListener('click', () => { resetCameraForm(); $('camera-form-name').focus(); });
  $('camera-form').addEventListener('submit', saveCamera);
  $('camera-form-reset').addEventListener('click', resetCameraForm);
  $('camera-form-cancel').addEventListener('click', resetCameraForm);
  $('camera-list').addEventListener('click', handleCameraAction);
  $('upload-form').addEventListener('submit', uploadForm);
  $('process-form').addEventListener('submit', processForm);
  $('video-file').addEventListener('change', (event) => { $('file-name').textContent = event.target.files[0]?.name || 'MP4, MOV, WebM, AVI'; });
  $('probe-video').addEventListener('click', async () => { try { showResult('probe-result', JSON.stringify(await request(`/api/v1/videos/${encodeURIComponent(currentVideoId || $('video-id').value)}/probe`), null, 2)); } catch (error) { showResult('probe-result', error.message, true); } });
  $('refresh-events').addEventListener('click', loadEvents);
  $('search-events').addEventListener('click', loadEvents);
  $('refresh-tracks').addEventListener('click', loadTracks);
  $('camera-start').addEventListener('click', startCamera);
  $('camera-record').addEventListener('click', startRecording);
  $('camera-stop').addEventListener('click', () => stopCamera(true));
  $('chat-form').addEventListener('submit', askChat);
  $('refresh-alerts').addEventListener('click', loadAlerts);
  $('alert-form').addEventListener('submit', async (event) => { event.preventDefault(); try { await request('/api/v1/alerts/rules', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: $('alert-name').value.trim(), event_type: $('alert-event-type').value.trim() || null, zone_id: $('alert-zone-id').value.trim() || null, object_label: $('alert-object-id').value.trim() || null }) }); await loadAlerts(); notify('Alert rule saved'); } catch (error) { setStatus('alert-status', error.message, true); } });
  $('save-api').addEventListener('click', saveSettingsConnection);

  (async () => {
    applyTheme(themePreference);
    $('dashboard-shell').classList.toggle('sidebar-collapsed', sidebarCollapsed);
    $('sidebar-collapse').textContent = sidebarCollapsed ? '›' : '‹';
    $('sidebar-collapse').setAttribute('aria-pressed', String(sidebarCollapsed));
    $('sidebar-collapse').setAttribute('aria-label', sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar');
    setAccountUi({ username: 'Local workspace', role: 'Development' });
    setAccessMode('unknown');
    $('camera-preview').hidden = true;
    await checkHealth();
    await refreshIdentity();
  })();
})();
