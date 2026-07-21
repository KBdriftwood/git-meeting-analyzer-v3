# Git風 Meeting Analyzer v3

リアルタイム文字起こし・要約・要望抽出・話題フロー可視化ツール。

## セットアップ

```bash
# 1. 依存パッケージをインストール
pip install -r requirements.txt

# 2. Groq APIキーを設定
cp .env.example .env
# .env を編集して GROQ_API_KEY を入力（https://console.groq.com/keys）

# 3. サーバーを起動
uvicorn main:app --reload
```

## 使い方

1. ブラウザで `http://localhost:8000` を開く
2. **録音開始** ボタンを押す
3. マイクに向かって話す
4. 3分後に本題が自動予測される（違う場合は ✏️ 修正）
5. 発言から30秒後に要約・要望が各行に表示される
6. 右ペインの話題フローで会話の流れを確認
7. **録音停止** ボタンで終了

## 構成

| ファイル | 役割 |
|---|---|
| `main.py` | FastAPI + WebSocket サーバー |
| `engine.py` | マイク入力 + Whisper 文字起こし |
| `analyzer.py` | LLM による要約・要望・話題分類 |
| `database.py` | SQLite 操作（V2 ベース改良） |
| `static/index.html` | 5ペイン UI |
| `static/app.js` | フロントエンド制御 + SVG フロー描画 |

## Railway へのデプロイ

1. Railway ダッシュボードで対象サービスを開く
2. **Variables** タブで環境変数を追加する
   - 変数名: `GROQ_API_KEY`
   - 値: Groq Console で発行した API キー
3. デプロイを再実行する（変数追加後は自動再デプロイされる場合あり）

`GROQ_API_KEY` が未設定の場合、録音開始時または音声チャンク送信時にサーバーエラーになります。

## V2 からの変化

- マイク入力 + Whisper によるリアルタイム文字起こし追加
- 要約・要望を30秒遅延で各発言行に表示
- 話題フロー（Git風）をリアルタイム描画
- 3分観察 → 本題を自動予測 → 人間が確認・修正
