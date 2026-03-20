const state = {
  speakers: [],
  remote: null,
  busy: false,
};

const els = {};

function $(id) {
  return document.getElementById(id);
}

function setStatusLine(message, isError = false) {
  els.statusLine.textContent = message;
  els.statusLine.classList.toggle("error", isError);
}

function formatTimestamp(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("de-DE", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });

  let payload = {};
  try {
    payload = await response.json();
  } catch (error) {
    payload = {};
  }

  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }

  return payload;
}

function renderSpeakers() {
  els.speakerGrid.innerHTML = "";
  const template = $("speakerCardTemplate");

  state.speakers.forEach((speaker) => {
    const node = template.content.firstElementChild.cloneNode(true);
    node.dataset.speakerId = speaker.id;
    node.querySelector(".speaker-label").textContent = speaker.name;
    node.querySelector(".speaker-name").textContent = speaker.device_name;
    node.querySelector(".speaker-device").textContent = speaker.mac;

    const button = node.querySelector(".play-btn");
    button.disabled = state.busy;
    button.addEventListener("click", () => startSpeaker(speaker.id));

    if (state.remote?.running && state.remote?.speaker_id === speaker.id) {
      node.classList.add("active");
      button.textContent = "Laeuft gerade auf diesem Lautsprecher";
    }

    els.speakerGrid.appendChild(node);
  });
}

function renderRemoteStatus() {
  const remote = state.remote || {};
  const status = remote.status || "idle";

  els.statusBadge.textContent = status.toUpperCase();
  els.statusBadge.className = `badge ${status}`;
  els.statusMessage.textContent = remote.message || "Kein Status vorhanden.";
  els.statusSpeaker.textContent = remote.speaker_name || "-";
  els.statusReason.textContent = remote.reason || "-";
  els.statusUpdated.textContent = formatTimestamp(remote.updated_at);
  els.stopBtn.disabled = state.busy || !remote.running;
}

function renderFromPayload(payload) {
  const settings = payload.settings || {};
  state.speakers = settings.speakers || [];
  state.remote = payload.remote || null;

  if (document.activeElement !== els.streamUrl) {
    els.streamUrl.value = settings.stream_url || "";
  }

  renderSpeakers();
  renderRemoteStatus();
}

function setBusy(nextBusy) {
  state.busy = nextBusy;
  els.saveSettingsBtn.disabled = nextBusy;
  els.refreshBtn.disabled = nextBusy;
  renderSpeakers();
  renderRemoteStatus();
}

async function refreshState({ silent = false } = {}) {
  if (!silent) setStatusLine("Pi5-Status wird geladen.");
  const payload = await api("/api/state");
  renderFromPayload(payload);
  if (!silent) setStatusLine("Status aktualisiert.");
}

async function saveSettings() {
  setBusy(true);
  setStatusLine("Stream-URL wird gespeichert.");
  try {
    const payload = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ stream_url: els.streamUrl.value }),
    });
    renderFromPayload(payload);
    setStatusLine("Stream-URL gespeichert.");
  } catch (error) {
    setStatusLine(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function startSpeaker(speakerId) {
  setBusy(true);
  setStatusLine("Pi5 startet die Wiedergabe.");
  try {
    const payload = await api("/api/play", {
      method: "POST",
      body: JSON.stringify({
        speaker_id: speakerId,
        stream_url: els.streamUrl.value,
      }),
    });
    renderFromPayload(payload);
    const remote = payload.remote || {};
    if (remote.status === "error") {
      setStatusLine(remote.message || "Pi5 meldet einen Fehler.", true);
    } else if (remote.status === "running") {
      setStatusLine("Stream laeuft auf dem Pi5.");
    } else {
      setStatusLine("Pi5 verbindet den Lautsprecher und startet den Stream.");
    }
  } catch (error) {
    setStatusLine(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function stopPlayback() {
  setBusy(true);
  setStatusLine("Pi5 stoppt die Wiedergabe.");
  try {
    const payload = await api("/api/stop", { method: "POST", body: "{}" });
    renderFromPayload(payload);
    setStatusLine("Wiedergabe gestoppt.");
  } catch (error) {
    setStatusLine(error.message, true);
  } finally {
    setBusy(false);
  }
}

function bindEvents() {
  els.saveSettingsBtn.addEventListener("click", saveSettings);
  els.refreshBtn.addEventListener("click", () => refreshState());
  els.stopBtn.addEventListener("click", stopPlayback);
}

async function init() {
  Object.assign(els, {
    streamUrl: $("streamUrl"),
    saveSettingsBtn: $("saveSettingsBtn"),
    refreshBtn: $("refreshBtn"),
    stopBtn: $("stopBtn"),
    speakerGrid: $("speakerGrid"),
    statusBadge: $("statusBadge"),
    statusMessage: $("statusMessage"),
    statusSpeaker: $("statusSpeaker"),
    statusReason: $("statusReason"),
    statusUpdated: $("statusUpdated"),
    statusLine: $("statusLine"),
  });

  bindEvents();

  try {
    await refreshState();
  } catch (error) {
    setStatusLine(error.message, true);
  }

  window.setInterval(() => {
    refreshState({ silent: true }).catch(() => {});
  }, 5000);
}

window.addEventListener("DOMContentLoaded", init);
