#!/usr/bin/env python3
"""
GitHub Projects V2 Migration Tool

GitHub Enterprise Importer (GHE Importer) では Projects V2 の移行がサポートされていないため、
このツールを使用して GitHub GraphQL API 経由で Projects V2 を移行します。

対応環境:
  - GitHub Enterprise Cloud (GitHub.com): https://api.github.com
  - GitHub Enterprise Cloud with Data Residency: https://api.<subdomain>.ghe.com

=== 移行対象 ===
  - プロジェクトのメタデータ (タイトル、説明、README、公開設定、クローズ状態)
  - カスタムフィールド: TEXT / NUMBER / DATE / SINGLE_SELECT (選択肢含む) / ITERATION
  - アイテム: Issue, Pull Request (リポジトリ名+番号で照合), ドラフトIssue
  - アイテムのフィールド値: TEXT / NUMBER / DATE / SINGLE_SELECT
  - ビュー: レイアウト (TABLE / BOARD / ROADMAP), フィルター文字列

=== 制限事項 ===
  - ITERATION フィールドの値 (スプリント割り当て) は API 制限により移行不可
    → イテレーションフィールド自体は作成されますが、各アイテムへの割り当ては手動で行ってください
  - ビューのグループ化・ソート設定は基本レイアウト/フィルターのみ移行
  - 組み込みフィールド (担当者, ラベル, マイルストーン等) の設定は Issue/PR 側の属性
  - Workflows (自動化) は移行不可

=== 使用方法 ===

  # エクスポート (GitHub Enterprise Cloud)
  python migrate_projects.py export \\
      --source-token ghp_xxx \\
      --source-org  my-org \\
      --output projects.json

  # エクスポート (GitHub Enterprise Cloud with Data Residency)
  python migrate_projects.py export \\
      --source-token ghp_xxx \\
      --source-org  my-org \\
      --source-url  https://api.mysubdomain.ghe.com \\
      --output projects.json

  # インポート (Data Residency 環境へ)
  python migrate_projects.py import \\
      --target-token ghp_yyy \\
      --target-org   new-org \\
      --target-url   https://api.mysubdomain.ghe.com \\
      --input projects.json \\
      --repo-mapping mapping.json

  # エクスポート + インポートを一括実行
  python migrate_projects.py migrate \\
      --source-token ghp_xxx --source-org src-org \\
      --target-token ghp_yyy --target-org tgt-org \\
      --target-url   https://api.mysubdomain.ghe.com \\
      --repo-mapping mapping.json \\
      --project-numbers 1,3,5

=== リポジトリマッピングファイル (mapping.json) の例 ===
  {
    "source-org/repo-a": "target-org/repo-a",
    "source-org/repo-b": "target-org/repo-b-renamed"
  }
"""

import json
import logging
import sys
import time
import argparse
from typing import Optional

try:
    import requests
except ImportError:
    print("Error: 'requests' パッケージが必要です。以下を実行してインストールしてください:")
    print("  pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# ロギング設定
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
MAX_RETRIES = 3
RATE_LIMIT_WAIT = 60   # レート制限時の待機秒数
REQUEST_TIMEOUT = 30   # HTTP タイムアウト秒数

# 組み込みフィールドタイプ (createProjectV2Field では作成不可)
BUILTIN_DATATYPES = frozenset({
    "ASSIGNEES",
    "LABELS",
    "LINKED_PULL_REQUESTS",
    "MILESTONE",
    "REPOSITORY",
    "REVIEWERS",
    "TITLE",
    "TRACKS",
    "TRACKED_BY",
})

# API で作成可能なカスタムフィールドタイプ
CREATABLE_DATATYPES = frozenset({"DATE", "ITERATION", "NUMBER", "SINGLE_SELECT", "TEXT"})

# ---------------------------------------------------------------------------
# GraphQL クエリ
# ---------------------------------------------------------------------------
Q_GET_ORG_ID = """
query GetOrgId($login: String!) {
  organization(login: $login) { id login }
}
"""

Q_GET_USER_ID = """
query GetUserId($login: String!) {
  user(login: $login) { id login }
}
"""

Q_LIST_ORG_PROJECTS = """
query ListOrgProjects($login: String!, $cursor: String) {
  organization(login: $login) {
    projectsV2(first: 20, after: $cursor) {
      edges {
        node {
          id number title shortDescription readme public closed url
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

Q_LIST_USER_PROJECTS = """
query ListUserProjects($login: String!, $cursor: String) {
  user(login: $login) {
    projectsV2(first: 20, after: $cursor) {
      edges {
        node {
          id number title shortDescription readme public closed url
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

Q_GET_FIELDS = """
query GetFields($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on ProjectV2 {
      fields(first: 50, after: $cursor) {
        edges {
          node {
            ... on ProjectV2Field {
              id name dataType
            }
            ... on ProjectV2SingleSelectField {
              id name dataType
              options { id name color description }
            }
            ... on ProjectV2IterationField {
              id name dataType
              configuration {
                duration
                startDay
                iterations          { id title startDate duration }
                completedIterations { id title startDate duration }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

Q_GET_VIEWS = """
query GetViews($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on ProjectV2 {
      views(first: 20, after: $cursor) {
        edges {
          node {
            id number name layout filter
            fields(first: 50) {
              nodes {
                ... on ProjectV2Field            { id name }
                ... on ProjectV2SingleSelectField { id name }
                ... on ProjectV2IterationField    { id name }
              }
            }
            groupByFields(first: 10) {
              nodes {
                ... on ProjectV2Field            { id name }
                ... on ProjectV2SingleSelectField { id name }
                ... on ProjectV2IterationField    { id name }
              }
            }
            sortByFields(first: 10) {
              nodes {
                direction
                field {
                  ... on ProjectV2Field            { id name }
                  ... on ProjectV2SingleSelectField { id name }
                  ... on ProjectV2IterationField    { id name }
                }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

Q_GET_ITEMS = """
query GetItems($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on ProjectV2 {
      items(first: 50, after: $cursor) {
        edges {
          node {
            id type
            content {
              ... on Issue {
                id number title
                repository { name owner { login } }
              }
              ... on PullRequest {
                id number title
                repository { name owner { login } }
              }
              ... on DraftIssue {
                id title body
              }
            }
            fieldValues(first: 30) {
              nodes {
                ... on ProjectV2ItemFieldTextValue {
                  text
                  field { ... on ProjectV2Field { id name } }
                }
                ... on ProjectV2ItemFieldNumberValue {
                  number
                  field { ... on ProjectV2Field { id name } }
                }
                ... on ProjectV2ItemFieldDateValue {
                  date
                  field { ... on ProjectV2Field { id name } }
                }
                ... on ProjectV2ItemFieldSingleSelectValue {
                  name optionId
                  field { ... on ProjectV2SingleSelectField { id name } }
                }
                ... on ProjectV2ItemFieldIterationValue {
                  iterationId title startDate duration
                  field { ... on ProjectV2IterationField { id name } }
                }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

Q_FIND_ISSUE = """
query FindIssue($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) { id }
  }
}
"""

Q_FIND_PR = """
query FindPR($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) { id }
  }
}
"""

# ---------------------------------------------------------------------------
# GraphQL ミューテーション
# ---------------------------------------------------------------------------
M_CREATE_PROJECT = """
mutation CreateProject($ownerId: ID!, $title: String!) {
  createProjectV2(input: { ownerId: $ownerId, title: $title }) {
    projectV2 { id number url }
  }
}
"""

M_UPDATE_PROJECT = """
mutation UpdateProject(
  $id: ID!
  $title: String
  $shortDescription: String
  $readme: String
  $public: Boolean
  $closed: Boolean
) {
  updateProjectV2(input: {
    projectId: $id
    title: $title
    shortDescription: $shortDescription
    readme: $readme
    public: $public
    closed: $closed
  }) {
    projectV2 { id }
  }
}
"""

M_CREATE_FIELD = """
mutation CreateField(
  $projectId: ID!
  $dataType: ProjectV2CustomFieldType!
  $name: String!
) {
  createProjectV2Field(input: {
    projectId: $projectId
    dataType: $dataType
    name: $name
  }) {
    projectV2Field {
      ... on ProjectV2Field         { id name dataType }
      ... on ProjectV2IterationField { id name dataType }
    }
  }
}
"""

M_CREATE_SINGLE_SELECT_FIELD = """
mutation CreateSingleSelectField(
  $projectId: ID!
  $name: String!
  $options: [ProjectV2SingleSelectFieldOptionInput!]!
) {
  createProjectV2Field(input: {
    projectId: $projectId
    dataType: SINGLE_SELECT
    name: $name
    singleSelectOptions: $options
  }) {
    projectV2Field {
      ... on ProjectV2SingleSelectField {
        id name dataType
        options { id name color description }
      }
    }
  }
}
"""

M_ADD_ITEM = """
mutation AddItem($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: { projectId: $projectId, contentId: $contentId }) {
    item { id }
  }
}
"""

M_ADD_DRAFT_ISSUE = """
mutation AddDraftIssue($projectId: ID!, $title: String!, $body: String) {
  addProjectV2DraftIssue(input: {
    projectId: $projectId
    title: $title
    body: $body
  }) {
    projectItem { id }
  }
}
"""

M_UPDATE_FIELD_VALUE = """
mutation UpdateFieldValue(
  $projectId: ID!
  $itemId: ID!
  $fieldId: ID!
  $value: ProjectV2FieldValue!
) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId
    itemId: $itemId
    fieldId: $fieldId
    value: $value
  }) {
    projectV2Item { id }
  }
}
"""

M_CREATE_VIEW = """
mutation CreateView($projectId: ID!, $name: String!, $layout: ProjectV2ViewLayout!) {
  createProjectV2View(input: {
    projectId: $projectId
    name: $name
    layout: $layout
  }) {
    projectV2View { id number name }
  }
}
"""

M_UPDATE_VIEW = """
mutation UpdateView(
  $viewId: ID!
  $name: String
  $layout: ProjectV2ViewLayout
  $filter: String
  $groupByFieldIds: [ID!]
  $visibleFieldIds: [ID!]
) {
  updateProjectV2View(input: {
    viewId: $viewId
    name: $name
    layout: $layout
    filter: $filter
    groupByFieldIds: $groupByFieldIds
    visibleFieldIds: $visibleFieldIds
  }) {
    projectV2View { id }
  }
}
"""


# ---------------------------------------------------------------------------
# GitHub GraphQL クライアント
# ---------------------------------------------------------------------------
class GitHubGraphQLClient:
    """GitHub GraphQL API クライアント (GitHub Enterprise Cloud / Data Residency 対応)"""

    def __init__(self, token: str, base_url: str = "https://api.github.com") -> None:
        self.token = token
        self.graphql_url = self._resolve_graphql_url(base_url)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # 新しい形式のグローバル Node ID を使用
            "X-Github-Next-Global-ID": "1",
        })

    @staticmethod
    def _resolve_graphql_url(base_url: str) -> str:
        """GraphQL エンドポイント URL を解決する。

        対応パターン:
          - https://api.github.com           → https://api.github.com/graphql
          - https://api.<subdomain>.ghe.com  → https://api.<subdomain>.ghe.com/graphql
          - https://.../graphql (末尾が /graphql) → そのまま使用
        """
        url = base_url.rstrip("/")
        if url.endswith("/graphql"):
            return url
        return f"{url}/graphql"

    def execute(self, query: str, variables: Optional[dict] = None) -> dict:
        """GraphQL クエリ / ミューテーションを実行する。レート制限・リトライ対応。"""
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables

        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.post(
                    self.graphql_url,
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                result = resp.json()

                if "errors" in result:
                    errors = result["errors"]
                    # レート制限
                    if any(e.get("type") == "RATE_LIMITED" for e in errors):
                        logger.warning(
                            "API レート制限に達しました。%d 秒待機します...", RATE_LIMIT_WAIT
                        )
                        time.sleep(RATE_LIMIT_WAIT)
                        continue
                    raise RuntimeError(
                        f"GraphQL エラー:\n{json.dumps(errors, indent=2, ensure_ascii=False)}"
                    )

                return result.get("data") or {}

            except requests.RequestException as exc:
                if attempt == MAX_RETRIES - 1:
                    raise
                wait = 2 ** attempt
                logger.warning(
                    "リクエスト失敗 (試行 %d/%d): %s — %d 秒後に再試行します...",
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                    wait,
                )
                time.sleep(wait)

        raise RuntimeError("最大再試行回数を超えました")

    def paginate(self, query: str, variables: dict, *path: str) -> list:
        """ページネーションを処理して全件のノードリストを返す。"""
        results: list = []
        cursor: Optional[str] = None

        while True:
            vars_copy = dict(variables)
            if cursor:
                vars_copy["cursor"] = cursor

            data = self.execute(query, vars_copy)

            # 指定パスを辿る
            node: dict = data
            for key in path:
                node = node.get(key) or {}

            for edge in node.get("edges") or []:
                if edge and edge.get("node") is not None:
                    results.append(edge["node"])

            page_info = node.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")

        return results


# ---------------------------------------------------------------------------
# エクスポーター
# ---------------------------------------------------------------------------
class ProjectExporter:
    """Projects V2 のデータを dict 形式にエクスポートする"""

    def __init__(self, client: GitHubGraphQLClient, owner_type: str = "org") -> None:
        self.client = client
        self.owner_type = owner_type  # "org" | "user"

    # ---- public -----------------------------------------------------------

    def export_projects(
        self,
        login: str,
        project_numbers: Optional[list] = None,
    ) -> list:
        """指定オーナーのプロジェクト一覧をエクスポートする。"""
        logger.info("プロジェクト一覧を取得中: %s", login)

        if self.owner_type == "org":
            raw = self.client.paginate(
                Q_LIST_ORG_PROJECTS, {"login": login}, "organization", "projectsV2"
            )
        else:
            raw = self.client.paginate(
                Q_LIST_USER_PROJECTS, {"login": login}, "user", "projectsV2"
            )

        if project_numbers:
            raw = [p for p in raw if p.get("number") in project_numbers]

        logger.info("%d 件のプロジェクトが見つかりました", len(raw))

        exported = []
        for project in raw:
            logger.info(
                "プロジェクト #%d: '%s' をエクスポート中...",
                project["number"],
                project["title"],
            )
            exported.append(self._export_project(project))

        return exported

    # ---- private ----------------------------------------------------------

    def _export_project(self, project: dict) -> dict:
        pid = project["id"]
        fields = self._export_fields(pid)
        items = self._export_items(pid)
        views = self._export_views(pid)

        logger.info(
            "  → フィールド: %d 件 / アイテム: %d 件 / ビュー: %d 件",
            len(fields),
            len(items),
            len(views),
        )

        return {
            "id": pid,
            "number": project["number"],
            "title": project["title"],
            "shortDescription": project.get("shortDescription") or "",
            "readme": project.get("readme") or "",
            "public": project.get("public", False),
            "closed": project.get("closed", False),
            "url": project.get("url", ""),
            "fields": fields,
            "items": items,
            "views": views,
        }

    def _export_fields(self, project_id: str) -> list:
        raw = self.client.paginate(Q_GET_FIELDS, {"id": project_id}, "node", "fields")
        fields = []
        for f in raw:
            if not f:
                continue
            entry: dict = {
                "id": f["id"],
                "name": f["name"],
                "dataType": f["dataType"],
            }
            if f["dataType"] == "SINGLE_SELECT":
                entry["options"] = f.get("options") or []
            elif f["dataType"] == "ITERATION":
                entry["configuration"] = f.get("configuration") or {}
            fields.append(entry)
        return fields

    def _export_items(self, project_id: str) -> list:
        raw = self.client.paginate(Q_GET_ITEMS, {"id": project_id}, "node", "items")
        items = []
        for item in raw:
            if not item:
                continue

            # フィールド値を {field_name: value_dict} でまとめる
            field_values: dict = {}
            for fv in (item.get("fieldValues") or {}).get("nodes") or []:
                if not fv:
                    continue
                field_meta = fv.get("field") or {}
                fname = field_meta.get("name", "")
                if not fname:
                    continue

                if fv.get("text") is not None:
                    field_values[fname] = {"type": "TEXT", "text": fv["text"]}
                elif fv.get("number") is not None:
                    field_values[fname] = {"type": "NUMBER", "number": fv["number"]}
                elif fv.get("date") is not None:
                    field_values[fname] = {"type": "DATE", "date": fv["date"]}
                elif "optionId" in fv:
                    field_values[fname] = {
                        "type": "SINGLE_SELECT",
                        "name": fv.get("name", ""),
                        "optionId": fv.get("optionId", ""),
                    }
                elif "iterationId" in fv:
                    field_values[fname] = {
                        "type": "ITERATION",
                        "iterationId": fv.get("iterationId", ""),
                        "title": fv.get("title", ""),
                        "startDate": fv.get("startDate", ""),
                        "duration": fv.get("duration", 0),
                    }

            item_entry: dict = {
                "id": item["id"],
                "type": item["type"],
                "fieldValues": field_values,
            }

            content = item.get("content") or {}
            itype = item["type"]
            if itype in ("ISSUE", "PULL_REQUEST"):
                repo = content.get("repository") or {}
                item_entry["content"] = {
                    "number": content.get("number"),
                    "title": content.get("title", ""),
                    "repo": repo.get("name", ""),
                    "owner": (repo.get("owner") or {}).get("login", ""),
                }
            elif itype == "DRAFT_ISSUE":
                item_entry["content"] = {
                    "title": content.get("title", ""),
                    "body": content.get("body", "") or "",
                }

            items.append(item_entry)

        return items

    def _export_views(self, project_id: str) -> list:
        raw = self.client.paginate(Q_GET_VIEWS, {"id": project_id}, "node", "views")
        views = []
        for v in raw:
            if not v:
                continue

            visible_fields = [
                {"id": f["id"], "name": f["name"]}
                for f in (v.get("fields") or {}).get("nodes") or []
                if f and f.get("id")
            ]
            group_by = [
                {"id": f["id"], "name": f["name"]}
                for f in (v.get("groupByFields") or {}).get("nodes") or []
                if f and f.get("id")
            ]
            sort_by = [
                {
                    "direction": s.get("direction", "ASC"),
                    "fieldName": (s.get("field") or {}).get("name", ""),
                }
                for s in (v.get("sortByFields") or {}).get("nodes") or []
                if s
            ]

            views.append({
                "id": v["id"],
                "number": v["number"],
                "name": v["name"],
                "layout": v.get("layout", "TABLE_LAYOUT"),
                "filter": v.get("filter") or "",
                "visibleFields": visible_fields,
                "groupByFields": group_by,
                "sortByFields": sort_by,
            })

        return sorted(views, key=lambda x: x["number"])


# ---------------------------------------------------------------------------
# インポーター
# ---------------------------------------------------------------------------
class ProjectImporter:
    """エクスポートされた Projects V2 データをターゲットに作成する"""

    def __init__(
        self,
        client: GitHubGraphQLClient,
        owner_type: str = "org",
    ) -> None:
        self.client = client
        self.owner_type = owner_type

    # ---- public -----------------------------------------------------------

    def get_owner_id(self, login: str) -> str:
        """組織またはユーザーのノード ID を取得する"""
        if self.owner_type == "org":
            data = self.client.execute(Q_GET_ORG_ID, {"login": login})
            org = data.get("organization")
            if not org:
                raise RuntimeError(f"組織 '{login}' が見つかりません (権限も確認してください)")
            return org["id"]
        else:
            data = self.client.execute(Q_GET_USER_ID, {"login": login})
            user = data.get("user")
            if not user:
                raise RuntimeError(f"ユーザー '{login}' が見つかりません")
            return user["id"]

    def import_projects(
        self,
        projects: list,
        target_login: str,
        repo_mapping: Optional[dict] = None,
    ) -> dict:
        """プロジェクト一覧をインポートしてサマリを返す"""
        owner_id = self.get_owner_id(target_login)
        repo_mapping = repo_mapping or {}
        stats = {"success": 0, "failed": 0, "skipped_items": 0}

        for project in projects:
            try:
                self._import_project(project, target_login, owner_id, repo_mapping, stats)
                stats["success"] += 1
            except Exception as exc:
                stats["failed"] += 1
                logger.error(
                    "プロジェクト '%s' のインポートに失敗しました: %s",
                    project.get("title", "?"),
                    exc,
                )

        return stats

    # ---- private ----------------------------------------------------------

    def _import_project(
        self,
        project: dict,
        target_login: str,
        owner_id: str,
        repo_mapping: dict,
        stats: dict,
    ) -> None:
        title = project["title"]
        logger.info("プロジェクト '%s' を作成中...", title)

        # 1. プロジェクトを新規作成
        data = self.client.execute(M_CREATE_PROJECT, {"ownerId": owner_id, "title": title})
        pv2 = data["createProjectV2"]["projectV2"]
        target_id = pv2["id"]
        logger.info("  → プロジェクト #%d を作成しました: %s", pv2["number"], pv2["url"])

        # 2. メタデータを更新
        self.client.execute(
            M_UPDATE_PROJECT,
            {
                "id": target_id,
                "title": title,
                "shortDescription": project.get("shortDescription") or None,
                "readme": project.get("readme") or None,
                "public": project.get("public", False),
                "closed": project.get("closed", False),
            },
        )

        # 3. カスタムフィールドを作成
        field_id_map, option_id_map = self._import_fields(
            target_id, project.get("fields", [])
        )

        # 4. ターゲットプロジェクトの全フィールドを取得 (組み込みフィールドも含む)
        target_fields_raw = self.client.paginate(
            Q_GET_FIELDS, {"id": target_id}, "node", "fields"
        )
        target_field_by_name: dict = {
            f["name"]: f for f in target_fields_raw if f and f.get("name")
        }

        # ソース→ターゲットのフィールド ID マッピングを補完 (同名フィールドで照合)
        for sf in project.get("fields", []):
            if sf["id"] not in field_id_map:
                tf = target_field_by_name.get(sf["name"])
                if tf:
                    field_id_map[sf["id"]] = tf["id"]

        # 5. アイテムを追加
        self._import_items(
            target_id,
            project.get("items", []),
            option_id_map,
            target_field_by_name,
            repo_mapping,
            stats,
        )

        # 6. ビューを作成/更新
        self._import_views(
            target_id,
            project.get("views", []),
            target_field_by_name,
        )

    def _import_fields(
        self, project_id: str, fields: list
    ) -> tuple:
        """カスタムフィールドを作成する。

        Returns:
            field_id_map  : {source_field_id: target_field_id}
            option_id_map : {source_option_id: target_option_id}
        """
        field_id_map: dict = {}
        option_id_map: dict = {}

        for field in fields:
            dtype = field.get("dataType", "")
            fname = field.get("name", "")

            if dtype not in CREATABLE_DATATYPES:
                # 組み込みフィールドはスキップ
                continue

            try:
                if dtype == "SINGLE_SELECT":
                    options_input = [
                        {
                            "name": opt["name"],
                            "color": opt.get("color", "GRAY"),
                            "description": opt.get("description") or "",
                        }
                        for opt in field.get("options", [])
                    ]
                    data = self.client.execute(
                        M_CREATE_SINGLE_SELECT_FIELD,
                        {"projectId": project_id, "name": fname, "options": options_input},
                    )
                    created = data["createProjectV2Field"]["projectV2Field"]
                    field_id_map[field["id"]] = created["id"]

                    # 選択肢 ID のマッピング (名前で照合)
                    for src_opt in field.get("options", []):
                        for tgt_opt in created.get("options", []):
                            if src_opt["name"] == tgt_opt["name"]:
                                option_id_map[src_opt["id"]] = tgt_opt["id"]
                                break

                    logger.info("  フィールド '%s' (SINGLE_SELECT, %d 選択肢) を作成しました", fname, len(options_input))

                else:
                    # TEXT / NUMBER / DATE / ITERATION
                    data = self.client.execute(
                        M_CREATE_FIELD,
                        {"projectId": project_id, "dataType": dtype, "name": fname},
                    )
                    created = data["createProjectV2Field"]["projectV2Field"]
                    field_id_map[field["id"]] = created["id"]

                    if dtype == "ITERATION":
                        logger.info(
                            "  フィールド '%s' (ITERATION) を作成しました"
                            " ※ イテレーション詳細は手動設定が必要です",
                            fname,
                        )
                    else:
                        logger.info("  フィールド '%s' (%s) を作成しました", fname, dtype)

            except Exception as exc:
                logger.warning("  フィールド '%s' の作成に失敗しました: %s", fname, exc)

        return field_id_map, option_id_map

    def _import_items(
        self,
        project_id: str,
        items: list,
        option_id_map: dict,
        target_field_by_name: dict,
        repo_mapping: dict,
        stats: dict,
    ) -> None:
        """プロジェクトアイテムをインポートする"""
        for item in items:
            try:
                target_item_id = self._create_item(project_id, item, repo_mapping)
                if target_item_id is None:
                    stats["skipped_items"] += 1
                    continue

                self._set_field_values(
                    project_id,
                    target_item_id,
                    item.get("fieldValues", {}),
                    option_id_map,
                    target_field_by_name,
                )
            except Exception as exc:
                stats["skipped_items"] += 1
                logger.warning(
                    "  アイテム '%s' のインポートに失敗しました: %s",
                    self._describe_item(item),
                    exc,
                )

    def _create_item(self, project_id: str, item: dict, repo_mapping: dict) -> Optional[str]:
        """アイテムを作成してターゲットのアイテム ID を返す。見つからない場合は None。"""
        itype = item.get("type", "")
        content = item.get("content", {})

        if itype == "DRAFT_ISSUE":
            data = self.client.execute(
                M_ADD_DRAFT_ISSUE,
                {
                    "projectId": project_id,
                    "title": content.get("title") or "(タイトルなし)",
                    "body": content.get("body") or None,
                },
            )
            return data["addProjectV2DraftIssue"]["projectItem"]["id"]

        if itype in ("ISSUE", "PULL_REQUEST"):
            src_owner = content.get("owner", "")
            src_repo = content.get("repo", "")
            number = content.get("number")

            if not src_owner or not src_repo or number is None:
                logger.warning("  リポジトリ情報が不足しているアイテムをスキップします: %s", content)
                return None

            # リポジトリマッピング適用
            src_full = f"{src_owner}/{src_repo}"
            tgt_full = repo_mapping.get(src_full, src_full)
            tgt_owner, tgt_repo = tgt_full.split("/", 1)

            content_id = self._find_content_id(itype, tgt_owner, tgt_repo, number)
            if content_id is None:
                logger.warning(
                    "  %s #%d (%s/%s) がターゲットに見つかりません。スキップします。",
                    itype,
                    number,
                    tgt_owner,
                    tgt_repo,
                )
                return None

            data = self.client.execute(
                M_ADD_ITEM,
                {"projectId": project_id, "contentId": content_id},
            )
            return data["addProjectV2ItemById"]["item"]["id"]

        logger.warning("  未知のアイテムタイプ: %s", itype)
        return None

    def _find_content_id(
        self, itype: str, owner: str, repo: str, number: int
    ) -> Optional[str]:
        """Issue または PR のノード ID をターゲットから検索する"""
        try:
            if itype == "ISSUE":
                data = self.client.execute(
                    Q_FIND_ISSUE, {"owner": owner, "repo": repo, "number": number}
                )
                issue = (data.get("repository") or {}).get("issue")
                return issue["id"] if issue else None
            else:
                data = self.client.execute(
                    Q_FIND_PR, {"owner": owner, "repo": repo, "number": number}
                )
                pr = (data.get("repository") or {}).get("pullRequest")
                return pr["id"] if pr else None
        except Exception as exc:
            logger.debug("コンテンツ検索エラー (%s #%d): %s", itype, number, exc)
            return None

    def _set_field_values(
        self,
        project_id: str,
        item_id: str,
        field_values: dict,
        option_id_map: dict,
        target_field_by_name: dict,
    ) -> None:
        """アイテムのカスタムフィールド値を設定する"""
        for field_name, fv in field_values.items():
            target_field = target_field_by_name.get(field_name)
            if target_field is None:
                logger.debug("  フィールド '%s' がターゲットに存在しません", field_name)
                continue

            target_field_id = target_field["id"]
            fv_type = fv.get("type", "")

            try:
                if fv_type == "TEXT":
                    value = {"text": fv["text"]}
                elif fv_type == "NUMBER":
                    value = {"number": fv["number"]}
                elif fv_type == "DATE":
                    value = {"date": fv["date"]}
                elif fv_type == "SINGLE_SELECT":
                    tgt_option_id = option_id_map.get(fv.get("optionId", ""))
                    if tgt_option_id is None:
                        # 名前でフォールバック照合
                        option_name = fv.get("name", "")
                        for opt in target_field.get("options", []):
                            if opt["name"] == option_name:
                                tgt_option_id = opt["id"]
                                break
                    if tgt_option_id is None:
                        logger.debug(
                            "  選択肢 '%s' のターゲット ID が見つかりません", fv.get("name")
                        )
                        continue
                    value = {"singleSelectOptionId": tgt_option_id}
                elif fv_type == "ITERATION":
                    # ITERATION は API 制限により設定不可 (ベストエフォート: スキップ)
                    logger.debug(
                        "  ITERATION 値はスキップします (フィールド: '%s', イテレーション: '%s')",
                        field_name,
                        fv.get("title", ""),
                    )
                    continue
                else:
                    continue

                self.client.execute(
                    M_UPDATE_FIELD_VALUE,
                    {
                        "projectId": project_id,
                        "itemId": item_id,
                        "fieldId": target_field_id,
                        "value": value,
                    },
                )
            except Exception as exc:
                logger.debug("  フィールド '%s' の値設定に失敗: %s", field_name, exc)

    def _import_views(
        self,
        project_id: str,
        views: list,
        target_field_by_name: dict,
    ) -> None:
        """ビューを作成/更新する"""
        if not views:
            return

        # プロジェクト作成時に自動生成されるデフォルトビューを取得
        existing_raw = self.client.paginate(
            Q_GET_VIEWS, {"id": project_id}, "node", "views"
        )
        existing_views = sorted(existing_raw, key=lambda x: x.get("number", 999))

        sorted_src_views = sorted(views, key=lambda x: x.get("number", 999))

        for i, view in enumerate(sorted_src_views):
            try:
                # グループ化フィールドの ID をターゲットにマッピング
                group_by_ids = [
                    target_field_by_name[f["name"]]["id"]
                    for f in view.get("groupByFields", [])
                    if f.get("name") and f["name"] in target_field_by_name
                ]
                # 表示フィールドの ID をターゲットにマッピング
                visible_ids = [
                    target_field_by_name[f["name"]]["id"]
                    for f in view.get("visibleFields", [])
                    if f.get("name") and f["name"] in target_field_by_name
                ] or None

                update_vars = {
                    "name": view["name"],
                    "layout": view.get("layout", "TABLE_LAYOUT"),
                    "filter": view.get("filter") or None,
                    "groupByFieldIds": group_by_ids or None,
                    "visibleFieldIds": visible_ids,
                }

                if i == 0 and existing_views:
                    # 最初のソースビュー → デフォルトビューを更新
                    target_view_id = existing_views[0]["id"]
                    self.client.execute(
                        M_UPDATE_VIEW,
                        {"viewId": target_view_id, **update_vars},
                    )
                    logger.info("  ビュー '%s' を更新しました", view["name"])
                else:
                    # 追加ビューを新規作成
                    data = self.client.execute(
                        M_CREATE_VIEW,
                        {
                            "projectId": project_id,
                            "name": view["name"],
                            "layout": view.get("layout", "TABLE_LAYOUT"),
                        },
                    )
                    new_view_id = data["createProjectV2View"]["projectV2View"]["id"]

                    # フィルター等を更新
                    if update_vars.get("filter") or update_vars.get("groupByFieldIds"):
                        self.client.execute(
                            M_UPDATE_VIEW,
                            {"viewId": new_view_id, **update_vars},
                        )
                    logger.info("  ビュー '%s' を作成しました", view["name"])

            except Exception as exc:
                logger.warning("  ビュー '%s' の処理に失敗しました: %s", view.get("name", "?"), exc)

    @staticmethod
    def _describe_item(item: dict) -> str:
        content = item.get("content", {})
        itype = item.get("type", "?")
        if itype == "DRAFT_ISSUE":
            return f"ドラフト: {content.get('title', '?')}"
        return f"{itype} #{content.get('number')} ({content.get('owner')}/{content.get('repo')})"


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------
def load_repo_mapping(path: Optional[str]) -> dict:
    """リポジトリマッピング JSON を読み込む"""
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        mapping = json.load(f)
    logger.info("リポジトリマッピング: %d 件読み込みました (%s)", len(mapping), path)
    return mapping


def parse_project_numbers(value: Optional[str]) -> Optional[list]:
    if not value:
        return None
    return [int(n.strip()) for n in value.split(",") if n.strip().isdigit()]


# ---------------------------------------------------------------------------
# サブコマンド実装
# ---------------------------------------------------------------------------
def cmd_export(args: argparse.Namespace) -> None:
    """export サブコマンド: プロジェクトを JSON ファイルにエクスポートする"""
    client = GitHubGraphQLClient(args.source_token, args.source_url)
    exporter = ProjectExporter(client, owner_type=args.owner_type)

    project_numbers = parse_project_numbers(getattr(args, "project_numbers", None))
    projects = exporter.export_projects(args.source_org, project_numbers)

    output = {
        "version": "1.0",
        "source": {
            "org": args.source_org,
            "url": args.source_url,
            "owner_type": args.owner_type,
        },
        "projects": projects,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info(
        "エクスポート完了: %s (%d プロジェクト)", args.output, len(projects)
    )


def cmd_import(args: argparse.Namespace) -> None:
    """import サブコマンド: JSON ファイルからプロジェクトをインポートする"""
    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    projects = data.get("projects", [])
    repo_mapping = load_repo_mapping(getattr(args, "repo_mapping", None))

    client = GitHubGraphQLClient(args.target_token, args.target_url)
    importer = ProjectImporter(client, owner_type=args.owner_type)

    logger.info(
        "インポート開始: %s に %d プロジェクトをインポートします",
        args.target_org,
        len(projects),
    )
    stats = importer.import_projects(projects, args.target_org, repo_mapping)
    _print_stats(stats)


def cmd_migrate(args: argparse.Namespace) -> None:
    """migrate サブコマンド: エクスポートとインポートを一括実行する"""
    src_client = GitHubGraphQLClient(args.source_token, args.source_url)
    exporter = ProjectExporter(src_client, owner_type=args.owner_type)

    project_numbers = parse_project_numbers(getattr(args, "project_numbers", None))

    logger.info("ソース '%s' からエクスポート中...", args.source_org)
    projects = exporter.export_projects(args.source_org, project_numbers)

    if not projects:
        logger.warning("移行するプロジェクトが見つかりませんでした")
        return

    repo_mapping = load_repo_mapping(getattr(args, "repo_mapping", None))

    tgt_client = GitHubGraphQLClient(args.target_token, args.target_url)
    importer = ProjectImporter(tgt_client, owner_type=args.owner_type)

    logger.info("ターゲット '%s' にインポート中...", args.target_org)
    stats = importer.import_projects(projects, args.target_org, repo_mapping)
    _print_stats(stats)


def _print_stats(stats: dict) -> None:
    logger.info(
        "完了: 成功=%d / 失敗=%d / スキップアイテム=%d",
        stats["success"],
        stats["failed"],
        stats["skipped_items"],
    )


# ---------------------------------------------------------------------------
# CLI パーサー
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_projects",
        description="GitHub Projects V2 移行ツール (GraphQL API 使用)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  # エクスポート (GitHub Enterprise Cloud)
  python migrate_projects.py export \\
      --source-token ghp_xxx --source-org my-org --output projects.json

  # エクスポート (GitHub Enterprise Cloud with Data Residency)
  python migrate_projects.py export \\
      --source-token ghp_xxx --source-org my-org \\
      --source-url https://api.mysubdomain.ghe.com --output projects.json

  # インポート (Data Residency 環境へ)
  python migrate_projects.py import \\
      --target-token ghp_yyy --target-org new-org \\
      --target-url https://api.mysubdomain.ghe.com \\
      --input projects.json --repo-mapping mapping.json

  # 一括移行 (GitHub.com → Data Residency)
  python migrate_projects.py migrate \\
      --source-token ghp_xxx --source-org src-org \\
      --target-token ghp_yyy --target-org tgt-org \\
      --target-url https://api.mysubdomain.ghe.com \\
      --repo-mapping mapping.json --project-numbers 1,3,5

  # ユーザープロジェクトの移行
  python migrate_projects.py migrate \\
      --source-token ghp_xxx --source-org my-username --owner-type user \\
      --target-token ghp_yyy --target-org my-username

リポジトリマッピングファイル (mapping.json) 例:
  {
    "source-org/repo-a": "target-org/repo-a",
    "source-org/repo-b": "target-org/repo-b-renamed"
  }
        """,
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="デバッグログを表示する",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ── export ──────────────────────────────────────────────────────────────
    ep = sub.add_parser("export", help="プロジェクトを JSON ファイルにエクスポート")
    ep.add_argument("--source-token", required=True, metavar="TOKEN",
                    help="ソース GitHub アクセストークン (read:org, project スコープ必須)")
    ep.add_argument("--source-org", required=True, metavar="ORG",
                    help="ソースの組織名 (--owner-type user の場合はユーザー名)")
    ep.add_argument("--source-url", default="https://api.github.com", metavar="URL",
                    help="ソースの API ベース URL。GitHub.com: https://api.github.com (デフォルト) / Data Residency: https://api.<subdomain>.ghe.com")
    ep.add_argument("--owner-type", choices=["org", "user"], default="org",
                    help="オーナータイプ (デフォルト: org)")
    ep.add_argument("--project-numbers", metavar="1,2,3",
                    help="移行するプロジェクト番号 (カンマ区切り。省略時は全件)")
    ep.add_argument("--output", default="projects.json", metavar="FILE",
                    help="出力 JSON ファイルパス (デフォルト: projects.json)")

    # ── import ──────────────────────────────────────────────────────────────
    ip = sub.add_parser("import", help="JSON ファイルからプロジェクトをインポート")
    ip.add_argument("--target-token", required=True, metavar="TOKEN",
                    help="ターゲット GitHub アクセストークン (project スコープ必須)")
    ip.add_argument("--target-org", required=True, metavar="ORG",
                    help="ターゲットの組織名 (--owner-type user の場合はユーザー名)")
    ip.add_argument("--target-url", default="https://api.github.com", metavar="URL",
                    help="ターゲットの API ベース URL。GitHub.com: https://api.github.com (デフォルト) / Data Residency: https://api.<subdomain>.ghe.com")
    ip.add_argument("--owner-type", choices=["org", "user"], default="org",
                    help="オーナータイプ (デフォルト: org)")
    ip.add_argument("--input", default="projects.json", metavar="FILE",
                    help="入力 JSON ファイルパス (デフォルト: projects.json)")
    ip.add_argument("--repo-mapping", metavar="FILE",
                    help="リポジトリマッピング JSON ファイル (省略時はリポジトリ名を維持)")

    # ── migrate ─────────────────────────────────────────────────────────────
    mp = sub.add_parser("migrate", help="エクスポートとインポートを一括実行")
    mp.add_argument("--source-token", required=True, metavar="TOKEN",
                    help="ソース GitHub アクセストークン")
    mp.add_argument("--source-org", required=True, metavar="ORG",
                    help="ソースの組織名 (--owner-type user の場合はユーザー名)")
    mp.add_argument("--source-url", default="https://api.github.com", metavar="URL",
                    help="ソースの API ベース URL。GitHub.com: https://api.github.com (デフォルト) / Data Residency: https://api.<subdomain>.ghe.com")
    mp.add_argument("--target-token", required=True, metavar="TOKEN",
                    help="ターゲット GitHub アクセストークン")
    mp.add_argument("--target-org", required=True, metavar="ORG",
                    help="ターゲットの組織名 (--owner-type user の場合はユーザー名)")
    mp.add_argument("--target-url", default="https://api.github.com", metavar="URL",
                    help="ターゲットの API ベース URL。GitHub.com: https://api.github.com (デフォルト) / Data Residency: https://api.<subdomain>.ghe.com")
    mp.add_argument("--owner-type", choices=["org", "user"], default="org",
                    help="オーナータイプ (デフォルト: org)")
    mp.add_argument("--repo-mapping", metavar="FILE",
                    help="リポジトリマッピング JSON ファイル")
    mp.add_argument("--project-numbers", metavar="1,2,3",
                    help="移行するプロジェクト番号 (カンマ区切り。省略時は全件)")

    return parser


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------
def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        if args.command == "export":
            cmd_export(args)
        elif args.command == "import":
            cmd_import(args)
        elif args.command == "migrate":
            cmd_migrate(args)
    except KeyboardInterrupt:
        logger.info("中断されました")
        sys.exit(1)
    except Exception as exc:
        logger.error("致命的なエラー: %s", exc)
        if args.verbose:
            raise
        sys.exit(1)


if __name__ == "__main__":
    main()
