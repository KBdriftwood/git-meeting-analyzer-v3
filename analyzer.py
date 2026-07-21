import json
import re
import time
import threading
import traceback
import queue
from groq_client import get_groq_client
from database import (
    update_speech_analysis,
    save_topic,
    get_all_topics,
    set_meeting_goal,
    get_meeting_goal,
)

ANALYSIS_DELAY_SECONDS = 5    # デバッグ用（本番は30）
GOAL_PREDICTION_WINDOW = 30   # デバッグ用（本番は180秒）


class Analyzer:
    def __init__(self, on_analysis_callback, on_goal_callback, on_topic_callback):
        """
        on_analysis_callback(speech_id, summary, intents) : 要約・要望確定時
        on_goal_callback(goal)                             : 本題予測確定時
        on_topic_callback(topic_node)                     : 話題ノード追加時
        """
        self.client = get_groq_client()
        self.model = "llama-3.3-70b-versatile"
        self.on_analysis_callback = on_analysis_callback
        self.on_goal_callback = on_goal_callback
        self.on_topic_callback = on_topic_callback

        self.analysis_queue = queue.Queue()  # (speech_id, text, enqueued_at)
        self.speech_log = []                 # 本題予測用バッファ
        self.goal_predicted = False
        self.start_time = time.time()
        self.current_topic_id = None
        self._shutdown = False

        self._worker = threading.Thread(target=self._analysis_loop, daemon=True)
        self._worker.start()

    def shutdown(self):
        """セッション終了時にキューを破棄し、以降のDB更新・コールバックを止める"""
        self._shutdown = True
        while True:
            try:
                self.analysis_queue.get_nowait()
            except queue.Empty:
                break

    def enqueue(self, speech_id: str, text: str):
        """発言を受け取り、30秒後に分析するようキューに積む"""
        if self._shutdown:
            return
        self.speech_log.append(text)
        self.analysis_queue.put((speech_id, text, time.time()))

        # 3分経過後に本題を予測（まだ予測していない場合）
        elapsed = time.time() - self.start_time
        if not self.goal_predicted and elapsed >= GOAL_PREDICTION_WINDOW:
            self._predict_goal()

    def _analysis_loop(self):
        print("[Analyzer] 分析ループ開始")
        while True:
            try:
                speech_id, text, enqueued_at = self.analysis_queue.get(timeout=1)
            except queue.Empty:
                continue

            if self._shutdown:
                continue

            print(f"[Analyzer] キューから取得: speech_id={speech_id}, text={text[:30]}...")

            # 遅延待機
            wait = ANALYSIS_DELAY_SECONDS - (time.time() - enqueued_at)
            if wait > 0:
                print(f"[Analyzer] {wait:.1f}秒待機中...")
                time.sleep(wait)

            if self._shutdown:
                continue

            print(f"[Analyzer] 分析開始: speech_id={speech_id}")
            try:
                summary, intents = self._analyze_speech(text)
                print(f"[Analyzer] 要約完了: summary={summary}, intents={intents}")

                topic_label = self._classify_topic(text)
                print(f"[Analyzer] 話題分類完了: topic_label={topic_label}")

                if self._shutdown:
                    continue

                update_speech_analysis(speech_id, summary, json.dumps(intents, ensure_ascii=False))

                # 話題ノードの更新
                topic_id = self._update_topic_flow(topic_label)

                print(f"[Analyzer] コールバック呼び出し: speech_id={speech_id}")
                self.on_analysis_callback(speech_id, summary, intents)
                self.on_topic_callback(self._build_topic_tree())
                print(f"[Analyzer] コールバック完了: speech_id={speech_id}")

            except Exception as e:
                print(f"[Analyzer] 分析エラー: {e}")
                traceback.print_exc()

    def _analyze_speech(self, text: str) -> tuple[str, list[str]]:
        """発言から要約と要望（3つ）を生成する"""
        prompt = (
            "以下の会議の発言を分析して、必ずJSON形式のみで回答してください。説明・コードフェンス不要。\n"
            '{"summary": "発言の要約（20字以内）", "intents": ["要望1", "要望2", "要望3"]}\n\n'
            f"発言：{text}"
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        return data["summary"], data["intents"]

    def _classify_topic(self, text: str) -> str:
        """発言が何の話題かを一言で分類する"""
        prompt = (
            "発言の話題を5〜10字で一言に要約してください。"
            "前の話題との関連がわかるよう、具体的な名詞を使ってください。"
            "回答は話題の名前のみ。説明不要。\n\n"
            f"発言：{text}"
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()

    def _update_topic_flow(self, topic_label: str) -> str:
        """話題ノードをGit風に追加する"""
        topics = get_all_topics()

        if not topics:
            topic_id = save_topic(topic_label, parent_id=None, is_digression=False)
            self.current_topic_id = topic_id
            return topic_id

        # 直前の話題と比較して「逸脱か継続か」を判断
        last_topic = topics[-1]
        last_label = last_topic[1]

        is_digression = self._is_digression(last_label, topic_label)
        topic_id = save_topic(
            topic_label,
            parent_id=self.current_topic_id,
            is_digression=is_digression
        )
        self.current_topic_id = topic_id
        return topic_id

    def _is_digression(self, prev_topic: str, current_topic: str) -> bool:
        """前の話題と現在の話題が逸脱かどうかを判断する"""
        goal = get_meeting_goal()
        context = f"会議の本題：{goal}\n" if goal else ""
        prompt = (
            f"{context}"
            "前の話題と現在の話題が会議の流れとして自然につながっているか判断してください。"
            "「yes」（自然な流れ）か「no」（逸脱・脱線）のみで回答してください。\n\n"
            f"前の話題：{prev_topic}\n現在の話題：{current_topic}"
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip().lower() == "no"

    def _predict_goal(self):
        """3分間の発言から会議の本題を予測する"""
        if self._shutdown:
            return
        self.goal_predicted = True
        combined = "\n".join(self.speech_log[-20:])

        try:
            prompt = (
                "以下の会議の冒頭発言から、この会議の本題・目的を15字以内で予測してください。"
                "アイスブレイクや雑談は無視して、仕事上の本題を答えてください。"
                "回答は本題の名前のみ。説明不要。\n\n"
                f"{combined}"
            )
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
            )
            goal = response.choices[0].message.content.strip()
            if self._shutdown:
                return
            set_meeting_goal(goal)
            self.on_goal_callback(goal)
        except Exception as e:
            print(f"[Analyzer] 本題予測エラー: {e}")

    def force_predict_goal(self):
        """ユーザーが手動で本題を再予測させるとき"""
        self.goal_predicted = False
        self._predict_goal()

    def _build_topic_tree(self) -> list:
        """話題ノードをツリー構造に変換してフロント向けに返す"""
        topics = get_all_topics()
        return [
            {
                "id": t[0],
                "label": t[1],
                "parent": t[2],
                "digression": bool(t[3])
            }
            for t in topics
        ]
