# ChocoLog Google Apps Script (GAS)

## Overview
このディレクトリは、ワークアウトログをGoogleスプレッドシート等と連携・自動処理するための Google Apps Script (GAS) プロジェクトを管理します。

## Structure
- `src/`: Google Apps Script のソースコード (`.js` / `.ts` / `.gs`)
- `appsscript.json`: GAS プロジェクト設定マニフェストファイル

## Setup & Deployment
[clasp](https://github.com/google/clasp) (Command Line Apps Script Projects) を利用したローカル開発および同期を推奨します。

```bash
# clasp のインストール
npm install -g @google/clasp

# ログイン
clasp login

# 既存プロジェクトのクローンまたは新規作成
clasp clone <SCRIPT_ID>
```
