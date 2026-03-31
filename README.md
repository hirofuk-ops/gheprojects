# GitHub Projects V2 Migration Tool

GitHub Enterprise Importer (GHE Importer) では **Projects V2 の移行がサポートされていない**ため、このツールを使用して GitHub GraphQL API 経由で Projects V2 を移行します。

## 対応環境

| 環境 | API ベース URL |
|---|---|
| GitHub Enterprise Cloud | `https://api.github.com` |
| GitHub Enterprise Cloud with Data Residency | `https://api.<subdomain>.ghe.com` |

## 移行対象

| 項目 | 対応状況 |
|---|---|
| プロジェクトメタデータ (タイトル・説明・README・公開設定・クローズ状態) | ✅ |
| カスタムフィールド: TEXT / NUMBER / DATE / SINGLE_SELECT (選択肢含む) | ✅ |
| カスタムフィールド: ITERATION (フィールド定義のみ) | ✅ |
| ITERATION のイテレーション値 (スプリント割り当て) | ❌ API 制限により不可 |
| アイテム: Issue / Pull Request (リポジトリ名+番号で照合) | ✅ |
| アイテム: ドラフト Issue | ✅ |
| アイテムのフィールド値: TEXT / NUMBER / DATE / SINGLE_SELECT | ✅ |
| ビュー: レイアウト (TABLE / BOARD / ROADMAP) | ✅ |
| ビュー: フィルター・グループ化 | ✅ |
| Workflows (自動化ルール) | ❌ GraphQL API 非対応 |

## 必要なトークンスコープ

- **ソース** (エクスポート): `read:org`、`project`
- **ターゲット** (インポート): `project`
  - 組織プロジェクトを作成する場合は `admin:org` も必要な場合があります

## インストール

```bash
pip install -r requirements.txt
```

## 使い方

### エクスポート

プロジェクトをソース環境から JSON ファイルに書き出します。

```bash
# GitHub Enterprise Cloud (GitHub.com)
python migrate_projects.py export \
    --source-token <TOKEN> \
    --source-org   <ORG> \
    --output projects.json

# GitHub Enterprise Cloud with Data Residency
python migrate_projects.py export \
    --source-token <TOKEN> \
    --source-org   <ORG> \
    --source-url   https://api.<subdomain>.ghe.com \
    --output projects.json

# 特定のプロジェクトのみエクスポート (番号はカンマ区切り)
python migrate_projects.py export \
    --source-token <TOKEN> \
    --source-org   <ORG> \
    --project-numbers 1,3,5 \
    --output projects.json
```

### インポート

JSON ファイルからターゲット環境にプロジェクトを作成します。

```bash
# Data Residency 環境へのインポート
python migrate_projects.py import \
    --target-token <TOKEN> \
    --target-org   <ORG> \
    --target-url   https://api.<subdomain>.ghe.com \
    --input projects.json \
    --repo-mapping mapping.json
```

### 一括移行 (エクスポート + インポート)

エクスポートとインポートをまとめて実行します。

```bash
# GitHub Enterprise Cloud → Data Residency
python migrate_projects.py migrate \
    --source-token <SOURCE_TOKEN> \
    --source-org   <SOURCE_ORG> \
    --target-token <TARGET_TOKEN> \
    --target-org   <TARGET_ORG> \
    --target-url   https://api.<subdomain>.ghe.com \
    --repo-mapping mapping.json

# 特定プロジェクトのみ移行
python migrate_projects.py migrate \
    --source-token <SOURCE_TOKEN> \
    --source-org   <SOURCE_ORG> \
    --target-token <TARGET_TOKEN> \
    --target-org   <TARGET_ORG> \
    --target-url   https://api.<subdomain>.ghe.com \
    --project-numbers 1,3,5
```

### ユーザープロジェクトの移行

`--owner-type user` を指定するとユーザーが所有するプロジェクトを対象にします。

```bash
python migrate_projects.py migrate \
    --source-token <TOKEN> \
    --source-org   <USERNAME> \
    --owner-type   user \
    --target-token <TOKEN> \
    --target-org   <USERNAME>
```

## オプション一覧

### 共通オプション

| オプション | 説明 |
|---|---|
| `--verbose`, `-v` | デバッグログを表示する |

### export

| オプション | 必須 | デフォルト | 説明 |
|---|---|---|---|
| `--source-token` | ✅ | | ソースの GitHub アクセストークン |
| `--source-org` | ✅ | | ソースの組織名 (ユーザー名) |
| `--source-url` | | `https://api.github.com` | ソースの API ベース URL |
| `--owner-type` | | `org` | `org` または `user` |
| `--project-numbers` | | 全件 | 移行するプロジェクト番号 (カンマ区切り) |
| `--output` | | `projects.json` | 出力 JSON ファイルパス |

### import

| オプション | 必須 | デフォルト | 説明 |
|---|---|---|---|
| `--target-token` | ✅ | | ターゲットの GitHub アクセストークン |
| `--target-org` | ✅ | | ターゲットの組織名 (ユーザー名) |
| `--target-url` | | `https://api.github.com` | ターゲットの API ベース URL |
| `--owner-type` | | `org` | `org` または `user` |
| `--input` | | `projects.json` | 入力 JSON ファイルパス |
| `--repo-mapping` | | | リポジトリマッピング JSON ファイル |

### migrate

| オプション | 必須 | デフォルト | 説明 |
|---|---|---|---|
| `--source-token` | ✅ | | ソースの GitHub アクセストークン |
| `--source-org` | ✅ | | ソースの組織名 (ユーザー名) |
| `--source-url` | | `https://api.github.com` | ソースの API ベース URL |
| `--target-token` | ✅ | | ターゲットの GitHub アクセストークン |
| `--target-org` | ✅ | | ターゲットの組織名 (ユーザー名) |
| `--target-url` | | `https://api.github.com` | ターゲットの API ベース URL |
| `--owner-type` | | `org` | `org` または `user` |
| `--repo-mapping` | | | リポジトリマッピング JSON ファイル |
| `--project-numbers` | | 全件 | 移行するプロジェクト番号 (カンマ区切り) |

## リポジトリマッピングファイル

Issue や Pull Request をプロジェクトアイテムとして移行する際、ソース側のリポジトリ名とターゲット側のリポジトリ名が異なる場合はマッピングファイルを用意します。

**mapping.json**
```json
{
  "source-org/repo-a": "target-org/repo-a",
  "source-org/repo-b": "target-org/repo-b-renamed"
}
```

マッピングファイルを省略した場合は、ソースと同名のリポジトリをターゲットから検索します。

## エクスポートファイルの形式

エクスポートされた `projects.json` の構造は以下のとおりです。

```json
{
  "version": "1.0",
  "source": {
    "org": "source-org",
    "url": "https://api.github.com",
    "owner_type": "org"
  },
  "projects": [
    {
      "number": 1,
      "title": "My Project",
      "shortDescription": "...",
      "readme": "...",
      "public": false,
      "closed": false,
      "fields": [...],
      "items": [...],
      "views": [...]
    }
  ]
}
```

## 注意事項

- **ITERATION フィールドの値**: APIの制限によりイテレーション (スプリント) への割り当ては移行できません。フィールド定義は作成されますが、各アイテムへの割り当ては移行後に手動で設定してください。
- **Workflows (自動化)**: GitHub GraphQL API では Workflows の読み書きがサポートされていないため移行できません。移行後に手動で再設定してください。
- **アイテムの照合**: Issue および Pull Request はリポジトリ名と番号で照合します。GHE Importer によるリポジトリ移行が完了してからプロジェクトを移行することを推奨します。
- **レート制限**: APIのレート制限に達した場合、自動的に 60 秒待機してリトライします。
