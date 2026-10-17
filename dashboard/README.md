# ChocoLog Dashboard

![CI](https://img.shields.io/badge/CI-passing-brightgreen)
![Python](https://img.shields.io/badge/Python-3.14%20%7C%203.13-blue.svg)
![Streamlit](https://img.shields.io/badge/Streamlit-1.66.0-FF4B4B.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

## Overview
Googleスプレッドシートに蓄積されたワークアウトログ（重量・回数・心拍・時間）をCSV経由で取得し、ローカルSQLiteデータベースへアトミックに同期してAltairデュアル軸チャートで可視化するStreamlit製分析ダッシュボードです。

## Quick Start (TL;DR)
```bash
# 1. 依存関係の同期（uv使用）
uv sync

# 2. 環境変数設定（スプレッドシートID）
cp .env.example .env

# 3. ダッシュボード起動
uv run streamlit run app.py
```
※ Windows環境では同梱の `run.bat`、macOS/Linux環境では `run.command` をダブルクリックするだけで全自動セットアップおよび起動が可能です。

## Architecture & Features
- **アトミックETLパイプライン (`sync.py`)**: Google Visualization APIのエクスポートエンドポイントからログを取得し、ステージングテーブル経由のトランザクション置換で全量更新を実施（検索用複合インデックス自動構築）。
- **日次集約 & ドリルダウン可視化 (`app.py`)**: 1日単位にセット回数や運動時間を自動合算し、Altairの2軸レイヤーチャートで推移を可視化。グラフ上のバーをクリックすることで該当日の全セット内訳レコード（日時、種目、負荷、回数、モード、総ボリューム）を下部テーブルに即時ドリルダウン表示。
- **筋力・有酸素自動判別可視化 (`app.py`)**: 種目名から筋トレ（最高重量・合計回数推移）と有酸素運動（最高心拍数・合計運動時間推移）を自動判定。
- **直近実績メトリクス & 履歴一覧**: 種目およびトレーニングモード（全件/両足/片足等）に応じた直近負荷・量・ボリューム値と前回セッション比の増減デルタを即時算出。
- **不変データ分離 & 破損検知バックアップ (`backup_manager.py`)**: 全永続状態を `./data` に隔離し、`testzip()` 整合性検証付きアトミックZIPバックアップをサイドバーから実行可能。

## Environment Variables
| 変数名 | デフォルト値 / サンプル値 | 説明 |
| :--- | :--- | :--- |
| `SPREADSHEET_ID` | `1Q1FID9mYqSYUs8UQejVj82JS-qqHC79nQalSDbM7w2c` | データ取得元GoogleスプレッドシートのID（共有設定：「リンクを知っている全員（閲覧者）」が必要） |
| `BACKUP_DIR` | `./backups` | （任意）自動バックアップZIPファイルの出力先ディレクトリパス |

## Limits & Known Trade-offs
- **スプレッドシートアクセス権限の前提**: Googleサービスアカウント等によるOAuth2認証ではなくCSV公開エンドポイントを利用するため、対象シートの「リンクを知っている全員が閲覧可」の設定が必須（非公開時はHTTP 401/302エラーを検知してUIに案内）。
- **全量洗い替え（Replace）モデル**: 現行バージョンではデータ整合性担保のため差分同期ではなく全量同期を採用しており、数万行を超える大規模ログでは同期時間の増大が想定されるため、将来的にはタイムスタンプ差分ETLへの移行が検討課題。
- **ネットワーク未接続時の同期**: オフライン環境ではスプレッドシート同期は行えず、ローカルSQLite (`./data/choco_fitness.db`) にキャッシュされた既存ログの閲覧のみ利用可能。

---
Licensed under the [MIT License](../LICENSE).
