import base64
import os
import tempfile
from groq import Groq

MIME_BY_EXT = {
    "webm": "audio/webm",
    "mp4": "audio/mp4",
    "m4a": "audio/mp4",
    "ogg": "audio/ogg",
    "wav": "audio/wav",
}


def transcribe_audio(audio_data_b64: str, ext: str = "webm") -> str:
    """
    base64エンコードされた音声をGroq Whisper APIで文字起こしする。
    短すぎる・空の場合は空文字列を返す。
    """
    if not audio_data_b64:
        return ""

    ext = (ext or "webm").lower().lstrip(".")
    if ext not in MIME_BY_EXT:
        print(f"[Engine] 未対応の拡張子 '{ext}' — webmとして処理")
        ext = "webm"

    mime_type = MIME_BY_EXT[ext]

    try:
        audio_bytes = base64.b64decode(audio_data_b64)
    except Exception as e:
        print(f"[Engine] base64デコードエラー: {e}")
        return ""

    if len(audio_bytes) < 2000:  # 2KB未満はノイズとして無視
        print(f"[Engine] 音声チャンクが短すぎるためスキップ ({len(audio_bytes)} bytes)")
        return ""

    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name

    try:
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        with open(tmp_path, "rb") as f:
            result = client.audio.transcriptions.create(
                file=(f"audio.{ext}", f, mime_type),
                model="whisper-large-v3-turbo",
                language="ja",
                response_format="text",
                prompt="会議、文字起こし、発言者、営業、顧客、案件",
            )
        text = result.strip() if isinstance(result, str) else str(result).strip()
        print(f"[Engine] 文字起こし完了 ({ext}, {len(audio_bytes)} bytes): {text[:50]}...")
        return text
    except Exception as e:
        print(f"[Engine] 文字起こしエラー ({ext}): {e}")
        raise
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
