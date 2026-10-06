# STOCK ASSISTANT PRO

Pythonだけで作る株式監視・通知アプリです。

## 実装済み

- 保有株の登録
- 複数回購入の平均取得単価
- 評価額・含み損益・損益率
- 購入目標価格 / 売却目標価格
- 株価の自動更新
- RSI / SMA / 価格変化 / 出来高を使った買い・売りシグナル
- LINE Messaging APIによるスマホ通知
- PCデスクトップ通知
- 取引終了後の自動レポート
- 株価ニュース取得
- SQLiteでのデータ保存
- 監視ログ保存
- 一度出した同じ通知の重複抑制

## 重要

このプロジェクトは「投資判断を自動で保証するもの」ではありません。
買い・売りシグナルは設定したルールによる候補判定です。

### 株価データについて

初期設定は `YFINANCE` です。これは開発・検証を始めやすくするためのものですが、
日本株の取引所リアルタイム配信を保証するものではありません。

真のリアルタイム日本株監視を行う場合は、立花証券e支店APIなど、
利用条件に適合したデータ提供元へ切り替えてください。

### LINE通知について

LINE公式アカウントのMessaging APIを利用します。
`.env` の `LINE_CHANNEL_ACCESS_TOKEN` と `LINE_USER_ID` を設定してください。

## 起動

1. Python 3.11以上を推奨
2. 仮想環境を作成
3. `pip install -r requirements.txt`
4. `.env.example` を `.env` にコピー
5. `.env`を設定
6. `python app.py`

Windows:
```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## 画面

- 「銘柄追加」で保有株を登録
- 「価格更新」で現在価格を更新
- 「監視開始」でバックグラウンド監視
- 「今すぐ分析」で全銘柄を分析
- 「今日のまとめ」でレポートを表示・送信

## 24時間監視

PCをスリープ・終了するとPython監視も止まります。
PCを閉じても通知したい場合は、同じPythonアプリをVPS等の常時稼働環境へ配置します。

## LINE設定

LINE DevelopersでMessaging APIチャネルを作成し、
チャネルアクセストークンを発行します。
アプリからのpush messageには送信先のLINE user IDが必要です。

## 注意

このソフトは自動売買を行いません。
注文機能は実装していません。

## GitHubへの公開前の注意

- `.env` は絶対にGitHubへアップロードしないでください。
- LINEのアクセストークンなどの秘密情報はコードに直接書かないでください。
- `stocks.db` などの個人データもGitHubへ公開しないでください。
- `.env.example` は設定項目の見本として公開できます。
