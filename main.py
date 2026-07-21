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
from groq_client import get_groq_api_key
from database import (
    init_db,
    get_or_create_speaker,
    save_speech,
    set_meeting_goal,
    get_meeting_goal,
    clear_session_data,
    get_all_speeches,
    get_all_topics,
    get_session_generation,
)

# 接続中のWebSocketクライアント一覧
connected_clients: list[WebSocket] = []
analyzer: Analyzer = None
is_recording: bool = False
session_speaker_id: str = None
parent_speech_id: str = None
session_token: int = 0
session_generation: int = 0
active_session_id: str = None
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


def _session_active(token: int) -> bool:
    return loop is not None and is_recording and token == session_token


def on_analysis(speech_id: str, summary: str, intents: list[str], *, _token: int):
    """要約・要望確定（遅延後）→ フロントへ配信"""
    if not _session_active(_token):
        return
    asyncio.run_coroutine_threadsafe(
        broadcast("analysis", {
            "speech_id": speech_id,
            "summary": summary,
            "intents": intents,
        }),
        loop,
    )


def on_goal(goal: str, *, _token: int):
    """本題予測確定 → フロントへ配信"""
    if not _session_active(_token):
        return
    asyncio.run_coroutine_threadsafe(
        broadcast("goal", {"goal": goal, "confirmed": False}),
        loop,
    )


def on_topic(topic_tree: list, *, _token: int):
    """話題ノード更新 → フロントへ配信"""
    if not _session_active(_token):
        return
    asyncio.run_coroutine_threadsafe(
        broadcast("topics", {"nodes": topic_tree}),
        loop,
    )


def _build_topic_tree() -> list:
    return [
        {
            "id": t[0],
            "label": t[1],
            "parent": t[2],
            "digression": bool(t[3]),
        }
        for t in get_all_topics()
    ]


def _reset_session():
    """録音状態とDBをリセットする"""
    global is_recording, analyzer, session_speaker_id, parent_speech_id
    global session_token, session_generation, active_session_id
    is_recording = False
    if analyzer is not None:
        analyzer.shutdown()
    analyzer = None
    session_speaker_id = None
    parent_speech_id = None
    active_session_id = None
    session_token += 1
    session_generation = clear_session_data()


async def _send_empty_session_state(websocket: WebSocket):
    """会話・話題フローを空にした状態をクライアントへ送る"""
    await _send_to_client(websocket, "session_reset", {})
    await _send_to_client(websocket, "topics", {"nodes": []})
    await _send_to_client(websocket, "status", {"recording": False, "session_id": None})


def _make_analyzer_callbacks(token: int):
    def on_analysis_cb(speech_id: str, summary: str, intents: list[str]):
        on_analysis(speech_id, summary, intents, _token=token)

    def on_goal_cb(goal: str):
        on_goal(goal, _token=token)

    def on_topic_cb(topic_tree: list):
        on_topic(topic_tree, _token=token)

    return on_analysis_cb, on_goal_cb, on_topic_cb


async def _send_to_client(websocket: WebSocket, event: str, data: dict):
    await websocket.send_text(
        json.dumps({"event": event, "data": data}, ensure_ascii=False)
    )


async def _replay_session_state(websocket: WebSocket):
    """録音中の再接続クライアントへ現在のセッション状態を再送する"""
    await _send_to_client(
        websocket,
        "status",
        {"recording": True, "session_id": active_session_id},
    )
    await _send_to_client(websocket, "topics", {"nodes": _build_topic_tree()})

    goal = get_meeting_goal()
    if goal:
        await _send_to_client(
            websocket,
            "goal",
            {"goal": goal, "confirmed": analyzer.goal_predicted if analyzer else False},
        )

    for speech_id, speaker, text, _timestamp, summary, intents, _topic_id in get_all_speeches():
        await _send_to_client(
            websocket,
            "speech",
            {"speech_id": speech_id, "speaker": speaker, "text": text},
        )
        if summary:
            intent_list = json.loads(intents) if intents else []
            await _send_to_client(
                websocket,
                "analysis",
                {"speech_id": speech_id, "summary": summary, "intents": intent_list},
            )


async def transcribe_and_process(
    audio_data_b64: str,
    ext: str = "webm",
    *,
    token: int,
    generation: int,
):
    """
    ブラウザから受け取った base64 音声を Groq Whisper で文字起こしし、
    DBに保存してフロントエンドへ配信する。
    """
    global parent_speech_id

    if not _session_active(token) or generation != get_session_generation():
        return

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

    if not _session_active(token) or generation != get_session_generation():
        return

    speech_id = save_speech(
        speaker_id=session_speaker_id,
        text=text,
        parent_id=parent_speech_id,
        session_generation=generation,
    )
    if speech_id is None:
        return

    parent_speech_id = speech_id

    await broadcast("speech", {
        "speech_id": speech_id,
        "speaker": "参加者1",
        "text": text,
    })

    if analyzer and analyzer.session_generation == generation:
        analyzer.enqueue(speech_id, text)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop
    init_db()
    try:
        get_groq_api_key()
        print("[Main] GROQ_API_KEY を確認しました")
    except RuntimeError as e:
        print(f"[Main] 警告: {e}")
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
    try:
        while True:
            msg = await websocket.receive_text()
            try:
                data = json.loads(msg)
                await handle_client_message(data, websocket)
            except json.JSONDecodeError as e:
                print(f"[Main] JSON解析エラー: {e}")
                await websocket.send_text(
                    json.dumps({"event": "error", "data": {"message": "不正なメッセージ形式"}}, ensure_ascii=False)
                )
            except Exception as e:
                print(f"[Main] メッセージ処理エラー: {e}")
                await websocket.send_text(
                    json.dumps({"event": "error", "data": {"message": str(e)}}, ensure_ascii=False)
                )
    except WebSocketDisconnect:
        if websocket in connected_clients:
            connected_clients.remove(websocket)
        asyncio.create_task(_delayed_reset_recording())
    except Exception as e:
        print(f"[Main] WebSocketエラー: {e}")
        if websocket in connected_clients:
            connected_clients.remove(websocket)
        asyncio.create_task(_delayed_reset_recording())


async def _delayed_reset_recording():
    """モバイルの一時切断を考慮し、30秒後に録音状態をリセットする"""
    await asyncio.sleep(30)
    _reset_recording_if_idle()


def _reset_recording_if_idle():
    """接続クライアントがいなくなったら録音状態をリセットする"""
    if not connected_clients and is_recording:
        _reset_session()
        print("[Main] 全クライアント切断 — 録音状態をリセット")


async def handle_client_message(data: dict, websocket: WebSocket):
    """フロントからのコマンドを処理する"""
    global analyzer, is_recording, session_speaker_id, parent_speech_id
    global session_generation, active_session_id
    cmd = data.get("cmd")

    if cmd == "hello":
        _reset_session()
        await _send_empty_session_state(websocket)
        print("[Main] ページ読み込み — セッションをリセット")

    elif cmd == "start":
        resume = data.get("resume", False)
        client_session_id = data.get("session_id")
        if (
            resume
            and is_recording
            and active_session_id
            and client_session_id == active_session_id
        ):
            await _replay_session_state(websocket)
            print("[Main] 録音セッション再開（再接続）")
            return

        _reset_session()
        token = session_token
        generation = session_generation
        active_session_id = str(uuid.uuid4())
        session_speaker_id = f"spk_{str(uuid.uuid4())[:8]}"
        get_or_create_speaker(session_speaker_id, "参加者1")
        parent_speech_id = None
        on_analysis_cb, on_goal_cb, on_topic_cb = _make_analyzer_callbacks(token)
        analyzer = Analyzer(
            on_analysis_callback=on_analysis_cb,
            on_goal_callback=on_goal_cb,
            on_topic_callback=on_topic_cb,
            session_generation=generation,
        )
        is_recording = True
        await broadcast("session_reset", {})
        await broadcast("topics", {"nodes": []})
        await broadcast(
            "status",
            {"recording": True, "session_id": active_session_id},
        )
        print("[Main] 録音セッション開始")

    elif cmd == "stop":
        if is_recording:
            is_recording = False
            await broadcast("status", {"recording": False, "session_id": active_session_id})
            print("[Main] 録音セッション停止")

    elif cmd == "audio_chunk":
        if is_recording:
            audio_data_b64 = data.get("data", "")
            ext = data.get("ext", "webm")
            if audio_data_b64:
                print(f"[Main] audio_chunk受信: ext={ext}, size={len(audio_data_b64)} chars")
                asyncio.create_task(
                    transcribe_and_process(
                        audio_data_b64,
                        ext,
                        token=session_token,
                        generation=session_generation,
                    )
                )
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
            set_meeting_goal(goal, session_generation=session_generation)
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
