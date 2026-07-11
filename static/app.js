// ── WebSocket接続 ──────────────────────────────────────────
const wsProtocol = location.protocol === "https:" ? "wss:" : "ws:";
const ws = new WebSocket(`${wsProtocol}//${location.host}/ws`);

ws.onopen = () => {
  console.log("[WS] 接続完了");
  btnStart.disabled = false;
  btnStart.textContent = "● 録音開始";
};

ws.onerror = (e) => {
  console.error("[WS] エラー", e);
  btnStart.textContent = "接続エラー";
};

ws.onclose = () => {
  console.log("[WS] 切断 - 3秒後に再接続");
  btnStart.disabled = true;
  btnStart.textContent = "再接続中...";
  setTimeout(() => location.reload(), 3000);
};

ws.onmessage = (e) => handleEvent(JSON.parse(e.data));

function send(payload) {
  if (ws.readyState !== WebSocket.OPEN) {
    console.warn("[WS] 接続が確立されていません");
    return;
  }
  ws.send(JSON.stringify(payload));
}

// ── 状態管理 ───────────────────────────────────────────────
const rows = {};   // speech_id → DOM要素への参照

// ── UIコントロール ─────────────────────────────────────────
const btnStart    = document.getElementById("btn-start");
const btnStop     = document.getElementById("btn-stop");
const recDot      = document.getElementById("rec-dot");
const goalText    = document.getElementById("goal-text");
const goalInput   = document.getElementById("goal-input");
const btnEditGoal = document.getElementById("btn-edit-goal");

// ── MediaRecorder 録音管理 ─────────────────────────────────
let mediaRecorder = null;

async function startRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

    // ブラウザが対応しているMIMEタイプを選択
    const mimeType = [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/ogg;codecs=opus",
      "audio/ogg",
    ].find((t) => MediaRecorder.isTypeSupported(t)) || "";

    const options = mimeType ? { mimeType } : {};
    mediaRecorder = new MediaRecorder(stream, options);

    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0 && ws.readyState === WebSocket.OPEN) {
        const reader = new FileReader();
        reader.onloadend = () => {
          // "data:audio/webm;base64,XXXX..." → "XXXX..."
          const base64 = reader.result.split(",")[1];
          send({ cmd: "audio_chunk", data: base64 });
        };
        reader.readAsDataURL(e.data);
      }
    };

    // 5秒ごとにチャンクをサーバーへ送信
    mediaRecorder.start(5000);
    send({ cmd: "start" });
  } catch (err) {
    alert("マイクへのアクセスが拒否されました: " + err.message);
  }
}

function stopRecording() {
  if (mediaRecorder) {
    mediaRecorder.stop();
    mediaRecorder.stream.getTracks().forEach((t) => t.stop());
    mediaRecorder = null;
  }
  send({ cmd: "stop" });
}

btnStart.addEventListener("click", () => startRecording());
btnStop.addEventListener("click",  () => stopRecording());

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
  }
}

function handleStatus({ recording }) {
  recDot.classList.toggle("active", recording);
  btnStart.style.display = recording ? "none"   : "inline-block";
  btnStop.style.display  = recording ? "inline-block" : "none";
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
