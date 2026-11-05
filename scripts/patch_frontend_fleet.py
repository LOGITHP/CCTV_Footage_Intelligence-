"""Patch frontend/index.html and frontend/app.js for dynamic fleet synchronization."""

# 1. Patch frontend/index.html
html_path = "frontend/index.html"
with open(html_path, "r", encoding="utf-8") as f:
    html = f.read()

old_badge = '<span id="live-fleet-badge" class="vms-agent-pill offline" style="color: #fca5a5; background: rgba(239, 68, 68, 0.12); border: 1px solid rgba(239, 68, 68, 0.35);"><span class="blink-dot" style="background: #ef4444; box-shadow: 0 0 8px #ef4444;"></span>ALL CAMERAS OFFLINE (0 ACTIVE)</span>'
new_badge = '<span id="live-fleet-badge" class="vms-agent-pill offline" style="color: #94a3b8; background: rgba(148, 163, 184, 0.12); border: 1px solid rgba(148, 163, 184, 0.25);"><span class="blink-dot" style="background: #94a3b8;"></span>SURVEILLANCE FLEET STANDBY (0 ACTIVE)</span>'
if old_badge in html:
    html = html.replace(old_badge, new_badge)

old_empty = '''                <div class="chat-empty">
                  <span class="chat-empty-mark" style="color: #ef4444; background: rgba(239, 68, 68, 0.15);">⊘</span>
                  <strong>All Camera Streams Currently Offline</strong>
                  <small>No live RTSP camera streams are currently streaming frames. The Live Copilot reports verified offline fleet telemetry. Connect a camera in Camera Streams or start CAM-01 to begin real-time surveillance.</small>
                </div>'''

new_empty = '''                <div class="chat-empty">
                  <span class="chat-empty-mark" style="color: #38bdf8; background: rgba(56, 189, 248, 0.15);">📡</span>
                  <strong>Surveillance Fleet Standby</strong>
                  <small>Camera streams are armed in standby mode. Start CAM-01 on the Live Video Matrix or connect an RTSP stream to begin real-time surveillance. You can still query armed rules and system health below.</small>
                </div>'''

if old_empty in html:
    html = html.replace(old_empty, new_empty)

with open(html_path, "w", encoding="utf-8") as f:
    f.write(html)
print("index.html patched!")

# 2. Patch frontend/app.js
js_path = "frontend/app.js"
with open(js_path, "r", encoding="utf-8") as f:
    js = f.read()

# Define updateGlobalFleetStatus function
global_fleet_func = '''
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
'''

if 'function updateGlobalFleetStatus()' not in js:
    # Insert right before renderActiveCameraFeeds or near startCamera
    target = '  function renderActiveCameraFeeds() {'
    js = js.replace(target, global_fleet_func + '\n' + target)

# Call updateGlobalFleetStatus in startCamera
old_start_cam_state = '''      if ($('camera-state')) {
        $('camera-state').textContent = '1 ACTIVE CAMERA ONLINE';
        $('camera-state').className = 'state-pill live';
        $('camera-state').style.cssText = 'color: #6ee7b7; background: rgba(16, 185, 129, 0.15); border-color: rgba(16, 185, 129, 0.35);';
      }'''

new_start_cam_state = '''      if ($('camera-state')) {
        $('camera-state').textContent = '1 ACTIVE CAMERA ONLINE';
        $('camera-state').className = 'state-pill live';
        $('camera-state').style.cssText = 'color: #6ee7b7; background: rgba(16, 185, 129, 0.15); border-color: rgba(16, 185, 129, 0.35);';
      }
      updateGlobalFleetStatus();'''

if old_start_cam_state in js:
    js = js.replace(old_start_cam_state, new_start_cam_state)

# Call updateGlobalFleetStatus in stopCamera
old_stop_cam_end = "      notify('CAM-01 primary sentry stopped');\n    }"
new_stop_cam_end = "      notify('CAM-01 primary sentry stopped');\n      updateGlobalFleetStatus();\n    }"
if old_stop_cam_end in js:
    js = js.replace(old_stop_cam_end, new_stop_cam_end)

# Also call updateGlobalFleetStatus in renderActiveCameraFeeds
old_feed_render_end = "    updateMatrixFleetStatus();"
new_feed_render_end = "    updateMatrixFleetStatus();\n    updateGlobalFleetStatus();"
if old_feed_render_end in js:
    js = js.replace(old_feed_render_end, new_feed_render_end)

# In loadCameras, protect loadMonitoringAnomalies
old_anom_call = "      await loadMonitoringAnomalies();\n    } catch (error) {"
new_anom_call = "      try { await loadMonitoringAnomalies(); } catch (err) { console.warn('Monitoring anomalies note:', err); }\n      updateGlobalFleetStatus();\n    } catch (error) {"
if old_anom_call in js:
    js = js.replace(old_anom_call, new_anom_call)

# In askLiveChat, ensure active camera default
old_live_chat_cam = "      const activeCamera = monitoringCameras.find((c) => c.enabled)?.camera_id || null;\n      const rawSession = $('live-chat-session-id')?.value.trim() || null;\n      const rawCamera = $('live-chat-camera-id')?.value.trim() || activeCamera;"
new_live_chat_cam = "      const isLocalLive = Boolean(cameraStream && cameraStream.active);\n      const activeCamera = isLocalLive ? 'cam-01' : (monitoringCameras.find((c) => c.enabled)?.camera_id || 'cam-01');\n      const rawSession = $('live-chat-session-id')?.value.trim() || null;\n      const rawCamera = $('live-chat-camera-id')?.value.trim() || activeCamera;"

if old_live_chat_cam in js:
    js = js.replace(old_live_chat_cam, new_live_chat_cam)

with open(js_path, "w", encoding="utf-8") as f:
    f.write(js)
print("app.js patched!")

