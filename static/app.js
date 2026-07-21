// ── デバッグ表示 ───────────────────────────────────────────
function dbg(msg) {
  console.log('[DEBUG]', msg);
  const bar = document.getElementById('debug-bar');
  if (!bar) return;
  bar.style.display = 'block';
  bar.textContent = '[DEBUG] ' + msg;
}

// ── 状態管理（WebSocket/UIより先に宣言） ───────────────────
const rows = {};   // speech_id → DOM要素への参照
let isRecordingActive = false;  // 連続録音中フラグ
let pendingMicRequest = false;  // マイク許可ダイアログ表示中

// ── UIコントロール ─────────────────────────────────────────
const btnStart    = document.getElementById("btn-start");
const btnStop     = document.getElementById("btn-stop");
const recDot      = document.getElementById("rec-dot");
const goalText    = document.getElementById("goal-text");
const goalInput   = document.getElementById("goal-input");
const btnEditGoal = document.getElementById("btn-edit-goal");

// ── WebSocket接続 ──────────────────────────────────────────
let ws = null;
let pingInterval = null;
let reconnectTimer = null;
let connectTimeoutTimer = null;
let wsState = "connecting"; // connecting | open | closed | failed
const CONNECT_TIMEOUT_MS = 10000;

function showStatus(msg, isError = false) {
  const bar = document.getElementById("status-bar");
  if (!bar) return;
  bar.style.display = "block";
  bar.style.color = isError ? "#fca5a5" : "#94a3b8";
  bar.textContent = msg;
}

function clearStatus() {
  const bar = document.getElementById("status-bar");
  if (bar) bar.style.display = "none";
}

function updateConnectionUI() {
  if (!btnStart || isRecordingActive) return;
  if (pendingMicRequest) {
    btnStart.disabled = true;
    btnStart.textContent = "マイク準備中...";
    return;
  }
  if (wsState === "open") {
    btnStart.disabled = false;
    btnStart.textContent = "● 録音開始";
    clearStatus();
  } else if (wsState === "failed") {
    btnStart.disabled = true;
    btnStart.textContent = "接続失敗";
  } else {
    btnStart.disabled = true;
    btnStart.textContent = wsState === "closed" ? "再接続中..." : "接続中...";
  }
}

function connectWS() {
  dbg("WebSocket接続試行中...");
  wsState = "connecting";
  updateConnectionUI();
  clearTimeout(connectTimeoutTimer);
  connectTimeoutTimer = setTimeout(() => {
    if (wsState !== "open") {
      wsState = "failed";
      showStatus("サーバーに接続できません。ページを再読み込みしてください。", true);
      updateConnectionUI();
    }
  }, CONNECT_TIMEOUT_MS);

  const wsProtocol = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${wsProtocol}//${location.host}/ws`);

  ws.onopen = () => {
    dbg("WebSocket接続完了");
    console.log("[WS] 接続完了");
    clearTimeout(connectTimeoutTimer);
    wsState = "open";
    updateConnectionUI();
    pingInterval = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ cmd: "ping" }));
      }
    }, 15000);
    if (isRecordingActive) {
      dbg("録音中に再接続 — startを再送");
      send({ cmd: "start" });
    }
  };

  ws.onerror = (e) => {
    dbg("WebSocketエラー: " + e.type);
    console.error("[WS] エラー", e);
  };

  ws.onclose = () => {
    dbg("WebSocket切断 → 再接続");
    console.log("[WS] 切断 - 3秒後に再接続");
    clearInterval(pingInterval);
    clearTimeout(connectTimeoutTimer);
    if (wsState === "failed") return;
    wsState = "closed";
    if (!pendingMicRequest && !isRecordingActive) {
      updateConnectionUI();
    } else {
      showStatus("接続が一時切断されました。自動で再接続します…");
    }
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connectWS, 3000);
  };

  ws.onmessage = (e) => {
    const parsed = JSON.parse(e.data);
    if (parsed.event !== "pong") dbg("受信: " + parsed.event);
    handleEvent(parsed);
  };
}

function send(payload) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn("[WS] 接続が確立されていません", payload);
    return false;
  }
  ws.send(JSON.stringify(payload));
  return true;
}

function ensureWSConnected(timeoutMs = 15000) {
  if (ws && ws.readyState === WebSocket.OPEN) return Promise.resolve(true);
  return new Promise((resolve) => {
    const start = Date.now();
    const tick = () => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        resolve(true);
        return;
      }
      if (Date.now() - start >= timeoutMs) {
        resolve(false);
        return;
      }
      setTimeout(tick, 200);
    };
    tick();
  });
}

// ── MediaRecorder 録音管理 ─────────────────────────────────
let mediaRecorder = null;
let audioChunks = [];
let currentStream = null;       // マイクストリーム（停止ボタンで解放）
let chunkIntervalId = null;     // チャンク切り替えタイマー
let chunkRestartTimer = null;   // 次チャンク開始のフォールバック
const CHUNK_DURATION_MS = 10000; // 10秒ごとに文字起こし送信
const MIN_CHUNK_BYTES = 2000;    // サーバーと同じ最小サイズ

function setRecordingUI(recording) {
  recDot.classList.toggle("active", recording);
  btnStart.style.display = recording ? "none" : "inline-block";
  btnStop.style.display = recording ? "inline-block" : "none";
  if (!recording) updateConnectionUI();
}

function micErrorMessage(err) {
  if (err.name === "NotAllowedError" || err.name === "PermissionDeniedError") {
    return "マイクの使用が拒否されました。ブラウザ設定でマイクを許可してください。";
  }
  if (err.name === "NotFoundError" || err.name === "DevicesNotFoundError") {
    return "マイクが見つかりません。デバイスを確認してください。";
  }
  if (err.name === "NotReadableError" || err.name === "TrackStartError") {
    return "マイクが他のアプリで使用中の可能性があります。";
  }
  return `${err.name}: ${err.message}`;
}

async function startRecording() {
  if (pendingMicRequest || isRecordingActive) return;
  dbg("録音開始試行...");
  pendingMicRequest = true;
  updateConnectionUI();

  try {
    dbg("マイク権限要求中...");
    currentStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    pendingMicRequest = false;
    dbg("マイク権限取得完了");

    const connected = await ensureWSConnected();
    if (!connected) {
      throw new Error("サーバー接続が切れています。ページを再読み込みしてください。");
    }

    isRecordingActive = true;
    setRecordingUI(true);
    clearStatus();

    if (!send({ cmd: "start" })) {
      throw new Error("録音開始コマンドの送信に失敗しました。");
    }
    startNewChunk();

    chunkIntervalId = setInterval(() => {
      if (isRecordingActive && mediaRecorder && mediaRecorder.state === "recording") {
        dbg("チャンク切り替え（10秒経過）");
        flushAndStopRecorder();
      }
    }, CHUNK_DURATION_MS);

    dbg("連続録音ループ開始");
  } catch (err) {
    pendingMicRequest = false;
    isRecordingActive = false;
    if (currentStream) {
      currentStream.getTracks().forEach((t) => t.stop());
      currentStream = null;
    }
    setRecordingUI(false);
    const msg = micErrorMessage(err);
    dbg("エラー: " + msg);
    console.error("録音エラー:", err);
    showStatus(msg, true);
  }
}

function scheduleNextChunk(delayMs = 0) {
  clearTimeout(chunkRestartTimer);
  if (!isRecordingActive) return;
  chunkRestartTimer = setTimeout(() => {
    if (isRecordingActive && (!mediaRecorder || mediaRecorder.state === 'inactive')) {
      startNewChunk();
    }
  }, delayMs);
}

function flushAndStopRecorder() {
  if (!mediaRecorder || mediaRecorder.state === 'inactive') return;
  try {
    if (typeof mediaRecorder.requestData === 'function') {
      mediaRecorder.requestData();
    }
  } catch (err) {
    console.warn('[Recorder] requestData failed:', err);
  }
  mediaRecorder.stop();
}

function startNewChunk() {
  if (!isRecordingActive || !currentStream) return;
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    dbg('前チャンクがまだ停止中 — 待機');
    scheduleNextChunk(200);
    return;
  }

  // Safari対応: サポートされているmimeTypeを自動検出
  const mimeType = getSupportedMimeType();
  const options = mimeType ? { mimeType } : {};

  audioChunks = [];
  try {
    mediaRecorder = new MediaRecorder(currentStream, options);
  } catch (err) {
    dbg('MediaRecorder作成失敗: ' + err.message);
    console.error('[Recorder] create failed:', err);
    scheduleNextChunk(500);
    return;
  }
  dbg('新チャンク開始: ' + (mediaRecorder.mimeType || mimeType || 'デフォルト'));

  mediaRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) {
      audioChunks.push(e.data);
    }
  };

  mediaRecorder.onerror = (e) => {
    const msg = e.error ? e.error.message : 'unknown';
    dbg('MediaRecorderエラー: ' + msg);
    console.error('[Recorder] error:', e.error || e);
    scheduleNextChunk(500);
  };

  // 停止時に全チャンクを結合して一括送信（Safari mp4 の moov atom 問題を回避）
  mediaRecorder.onstop = () => {
    const blobType = mediaRecorder.mimeType || mimeType || 'audio/webm';
    const blob = new Blob(audioChunks, { type: blobType });
    const ext = getExtension(blobType);
    dbg('チャンク完成: ' + blob.size + ' bytes, ext=' + ext);

    // 録音継続中は FileReader 完了を待たず次チャンクを開始（無音区間を防ぐ）
    if (isRecordingActive) {
      scheduleNextChunk(0);
    }

    if (blob.size >= MIN_CHUNK_BYTES) {
      const reader = new FileReader();
      reader.onerror = () => {
        dbg('FileReaderエラー');
        console.error('[Recorder] FileReader failed');
      };
      reader.onloadend = () => {
        if (!reader.result) {
          dbg('FileReader結果が空');
          return;
        }
        const base64 = reader.result.split(',')[1];
        if (base64) {
          send({ cmd: 'audio_chunk', data: base64, ext: ext });
        } else {
          dbg('base64変換失敗');
        }
      };
      reader.readAsDataURL(blob);
    } else {
      dbg('チャンクが小さすぎるためスキップ (' + blob.size + ' bytes)');
    }
  };

  try {
    mediaRecorder.start(1000);  // 1秒ごとにondataavailableを発火
  } catch (err) {
    dbg('MediaRecorder.start失敗: ' + err.message);
    console.error('[Recorder] start failed:', err);
    scheduleNextChunk(500);
  }
}

function getSupportedMimeType() {
  const types = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4",
    "audio/ogg;codecs=opus",
    "audio/ogg",
  ];
  for (const type of types) {
    if (MediaRecorder.isTypeSupported(type)) return type;
  }
  return "";
}

function getExtension(mimeType) {
  if (mimeType.includes("mp4")) return "mp4";
  if (mimeType.includes("ogg")) return "ogg";
  if (mimeType.includes("webm")) return "webm";
  return "webm";
}

function stopRecording() {
  dbg('録音停止要求');
  isRecordingActive = false;
  setRecordingUI(false);
  clearStatus();
  clearInterval(chunkIntervalId);
  chunkIntervalId = null;
  clearTimeout(chunkRestartTimer);
  chunkRestartTimer = null;

  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    flushAndStopRecorder();  // 最後のチャンクを送信（onstopでisRecordingActive=falseなので次は開始しない）
  }
  if (currentStream) {
    currentStream.getTracks().forEach((t) => t.stop());
    currentStream = null;
  }
  send({ cmd: "stop" });
}

btnStart.addEventListener("click", () => startRecording());
btnStop.addEventListener("click",  () => stopRecording());

connectWS();

btnEditGoal.addEventListener("click", () => {
  if (goalInput.style.display === "none" || goalInput.style.display === "") {
    goalInput.style.display = "inline-block";
    goalInput.value = goalText.dataset.raw || "";
    goalInput.focus();
  } else {
    const val = goalInput.value.trim();
    if (val) send({ cmd: "set_goal", goal: val });
    goalInput.style.display = "none";
  }
});

goalInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const val = goalInput.value.trim();
    if (val) send({ cmd: "set_goal", goal: val });
    goalInput.style.display = "none";
  }
});

// ── イベントハンドラ ───────────────────────────────────────
function handleEvent({ event, data }) {
  switch (event) {
    case "status":    handleStatus(data);   break;
    case "speech":    handleSpeech(data);   break;
    case "analysis":  handleAnalysis(data); break;
    case "goal":      handleGoal(data);     break;
    case "topics":    handleTopics(data);   break;
    case "error":     handleError(data);    break;
    case "pong":      break;
  }
}

function handleError({ message }) {
  dbg("サーバーエラー: " + message);
  console.error("[Server]", message);
  showStatus("サーバーエラー: " + message, true);
}

function handleStatus({ recording }) {
  if (isRecordingActive && !recording) {
    dbg("サーバー側が未録音 — startを再送");
    send({ cmd: "start" });
    setRecordingUI(true);
    return;
  }
  if (!isRecordingActive && !pendingMicRequest) {
    setRecordingUI(recording);
  }
}

function handleSpeech({ speech_id, speaker, text }) {
  const rowEl = document.createElement("div");
  rowEl.className = "speech-row new";
  rowEl.dataset.speechId = speech_id;
  rowEl.innerHTML = `
    <div><span class="speaker-tag">${esc(speaker)}</span></div>
    <div class="speech-text">${esc(text)}</div>
    <div><span class="analysis-cell" id="sum-${speech_id}">…</span></div>
    <div><span class="analysis-cell" id="int-${speech_id}">…</span></div>
  `;
  document.getElementById("rows-container").appendChild(rowEl);
  rows[speech_id] = rowEl;
  setTimeout(() => rowEl.classList.remove("new"), 800);

  const container = document.getElementById("table-container");
  container.scrollTop = container.scrollHeight;
}

function handleAnalysis({ speech_id, summary, intents }) {
  const sumEl = document.getElementById(`sum-${speech_id}`);
  const intEl = document.getElementById(`int-${speech_id}`);

  if (sumEl) {
    sumEl.textContent = summary;
    sumEl.classList.add("filled");
  }
  if (intEl) {
    const items = intents.map(i => `<li>${esc(i)}</li>`).join("");
    intEl.innerHTML = `<ul class="intent-list">${items}</ul>`;
    intEl.classList.add("filled");
  }
}

function handleGoal({ goal, confirmed }) {
  goalText.textContent = goal + (confirmed ? "" : "（予測）");
  goalText.dataset.raw = goal;
  goalText.classList.remove("goal-pending");
  goalText.style.color = confirmed ? "var(--green)" : "var(--yellow)";
}

function handleTopics({ nodes }) {
  renderFlow(nodes);
}

function esc(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// ── Git風フロー描画（SVG） ─────────────────────────────────
function renderFlow(nodes) {
  const svg  = document.getElementById("flow-svg");
  const W    = svg.parentElement.clientWidth || 160;
  const NODE_R   = 7;
  const NODE_GAP = 48;
  const X_MAIN   = 20;
  const X_DIG    = 70;

  svg.innerHTML = "";

  if (!nodes.length) return;

  const totalH = nodes.length * NODE_GAP + 20;
  svg.setAttribute("viewBox", `0 0 ${W} ${totalH}`);
  svg.style.height = totalH + "px";

  const pos = {};
  nodes.forEach((n, i) => {
    const x = n.digression ? X_DIG : X_MAIN;
    const y = 20 + i * NODE_GAP;
    pos[n.id] = { x, y };
  });

  nodes.forEach(n => {
    if (!n.parent || !pos[n.parent]) return;
    const from = pos[n.parent];
    const to   = pos[n.id];

    const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
    const color = n.digression ? "#f97316" : "#6366f1";

    if (n.digression) {
      line.setAttribute("d",
        `M ${from.x} ${from.y} C ${from.x} ${(from.y + to.y) / 2}, ${to.x} ${(from.y + to.y) / 2}, ${to.x} ${to.y}`
      );
    } else {
      line.setAttribute("d", `M ${from.x} ${from.y} L ${to.x} ${to.y}`);
    }

    line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", "2");
    line.setAttribute("fill", "none");
    line.setAttribute("stroke-dasharray", n.digression ? "4,3" : "none");
    svg.appendChild(line);
  });

  nodes.forEach(n => {
    const { x, y } = pos[n.id];
    const color = n.digression ? "#f97316" : "#6366f1";

    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("cx", x);
    circle.setAttribute("cy", y);
    circle.setAttribute("r",  NODE_R);
    circle.setAttribute("fill", color);
    circle.setAttribute("stroke", "#0f1117");
    circle.setAttribute("stroke-width", "2");
    svg.appendChild(circle);

    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", x + NODE_R + 5);
    text.setAttribute("y", y + 4);
    text.setAttribute("fill", n.digression ? "#f97316" : "#e2e8f0");
    text.setAttribute("font-size", "11");
    text.textContent = n.label.length > 10 ? n.label.slice(0, 9) + "…" : n.label;
    svg.appendChild(text);
  });

  svg.parentElement.scrollTop = svg.parentElement.scrollHeight;
}
