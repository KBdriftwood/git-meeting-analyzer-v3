import json
import asyncio
import uuid
from contextlib import asynccontextmanager
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from engine import transcribe_audio
from analyzer import Analyzer
from database import (
    init_db,
    get_or_create_speaker,
    save_speech,
    set_meeting_goal,
    get_meeting_goal,
)

# 接続中のWebSocketクライアント一覧
connected_clients: list[WebSocket] = []
analyzer: Analyzer = None
is_recording: bool = False
session_speaker_id: str = None
parent_speech_id: str = None
loop = None  # asyncioイベントループ（lifespan内でセット）


async def broadcast(event: str, data: dict):
    """全クライアントにイベントを送信する"""
    message = json.dumps({"event": event, "data": data}, ensure_ascii=False)
    dead = []
    for ws in connected_clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connected_clients.remove(ws)


def on_analysis(speech_id: str, summary: str, intents: list[str]):
    """要約・要望確定（遅延後）→ フロントへ配信"""
    if loop is None:
        return
    asyncio.run_coroutine_threadsafe(
        broadcast("analysis", {
            "speech_id": speech_id,
            "summary": summary,
            "intents": intents,
        }),
        loop,
    )


def on_goal(goal: str):
    """本題予測確定 → フロントへ配信"""
    if loop is None:
        return
    asyncio.run_coroutine_threadsafe(
        broadcast("goal", {"goal": goal, "confirmed": False}),
        loop,
    )


def on_topic(topic_tree: list):
    """話題ノード更新 → フロントへ配信"""
    if loop is None:
        return
    asyncio.run_coroutine_threadsafe(
        broadcast("topics", {"nodes": topic_tree}),
        loop,
    )


async def transcribe_and_process(audio_data_b64: str, ext: str = "webm"):
    """
    ブラウザから受け取った base64 音声を Groq Whisper で文字起こしし、
    DBに保存してフロントエンドへ配信する。
    """
    global parent_speech_id

    try:
        text = await asyncio.get_event_loop().run_in_executor(
            None, transcribe_audio, audio_data_b64, ext
        )
    except Exception as e:
        print(f"[Main] 文字起こし失敗 ({ext}): {e}")
        await broadcast("error", {"message": f"文字起こし失敗: {e}"})
        return

    if not text:
        print(f"[Main] 文字起こし結果が空 ({ext})")
        return

    speech_id = save_speech(
        speaker_id=session_speaker_id,
        text=text,
        parent_id=parent_speech_id,
    )
    parent_speech_id = speech_id

    await broadcast("speech", {
        "speech_id": speech_id,
        "speaker": "参加者1",
        "text": text,
    })

    if analyzer:
        analyzer.enqueue(speech_id, text)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop
    init_db()
    loop = asyncio.get_running_loop()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    # 再接続時に現在の録音状態を同期
    await websocket.send_text(
        json.dumps({"event": "status", "data": {"recording": is_recording}}, ensure_ascii=False)
    )
    try:
        while True:
            msg = await websocket.receive_text()
            data = json.loads(msg)
            await handle_client_message(data, websocket)
    except WebSocketDisconnect:
        if websocket in connected_clients:
            connected_clients.remove(websocket)
        _reset_recording_if_idle()


def _reset_recording_if_idle():
    """接続クライアントがいなくなったら録音状態をリセットする"""
    global is_recording, analyzer
    if not connected_clients and is_recording:
        is_recording = False
        analyzer = None
        print("[Main] 全クライアント切断 — 録音状態をリセット")


async def handle_client_message(data: dict, websocket: WebSocket):
    """フロントからのコマンドを処理する"""
    global analyzer, is_recording, session_speaker_id, parent_speech_id
    cmd = data.get("cmd")

    if cmd == "start":
        if not is_recording:
            session_speaker_id = f"spk_{str(uuid.uuid4())[:8]}"
            get_or_create_speaker(session_speaker_id, "参加者1")
            parent_speech_id = None
            analyzer = Analyzer(
                on_analysis_callback=on_analysis,
                on_goal_callback=on_goal,
                on_topic_callback=on_topic,
            )
            is_recording = True
            await broadcast("status", {"recording": True})
            print("[Main] 録音セッション開始")

    elif cmd == "stop":
        if is_recording:
            is_recording = False
            await broadcast("status", {"recording": False})
            print("[Main] 録音セッション停止")

    elif cmd == "audio_chunk":
        if is_recording:
            audio_data_b64 = data.get("data", "")
            ext = data.get("ext", "webm")
            if audio_data_b64:
                print(f"[Main] audio_chunk受信: ext={ext}, size={len(audio_data_b64)} chars")
                asyncio.create_task(transcribe_and_process(audio_data_b64, ext))
            else:
                print("[Main] audio_chunk: 空データを無視")
        else:
            print("[Main] audio_chunk: 録音中ではないため無視")

    elif cmd == "ping":
        try:
            await websocket.send_text(
                json.dumps({"event": "pong", "data": {}}, ensure_ascii=False)
            )
        except Exception:
            pass

    elif cmd == "set_goal":
        goal = data.get("goal", "").strip()
        if goal:
            set_meeting_goal(goal)
            if analyzer:
                analyzer.goal_predicted = True
            await broadcast("goal", {"goal": goal, "confirmed": True})

    elif cmd == "rename_speaker":
        from database import update_speaker_name
        update_speaker_name(data["speaker_id"], data["new_name"])
        await broadcast("speaker_renamed", {
            "speaker_id": data["speaker_id"],
            "new_name": data["new_name"],
        })
