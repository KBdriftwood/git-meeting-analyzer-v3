import os

from groq import Groq


def get_groq_api_key() -> str:
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY が設定されていません。"
            " ローカルでは .env に、Railway では Variables に GROQ_API_KEY を設定してください。"
        )
    return api_key


def get_groq_client() -> Groq:
    return Groq(api_key=get_groq_api_key())
