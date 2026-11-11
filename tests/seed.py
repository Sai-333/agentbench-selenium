"""Idempotent API seed for the committed Selenium mock fixture.

Every pytest session runs this before cases. ``python tests/seed.py`` runs it
alone. Fixed emails and slugs are registered or reused; child rows are created
or updated. Public-board comments are reset to the fixture set. Votes are
upserted by the fixture fingerprint (the API has no vote-delete).

Optional fixture rows are also upserted when present: a VIEWER member, a
draft benchmark with visible and hidden suites, a published version whose
public board is left unpublished, and one active flake quarantine.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "mock-data.json"


class ApiError(RuntimeError):
    def __init__(self, status: int, payload: object):
        self.status = status
        self.payload = payload if isinstance(payload, dict) else {"detail": str(payload)}
        detail = self.payload.get("detail") or self.payload.get("title") or self.payload
        super().__init__(f"HTTP {status}: {detail}")


def load_fixture(path: Path = FIXTURE_PATH) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not data.get("users") or not data.get("organizations"):
        raise RuntimeError(f"{path} must include users and organizations")
    return data


def _request(
    method: str,
    url: str,
    body: dict | None = None,
    token: str | None = None,
    timeout: float = 60,
) -> tuple[int, object]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    if payload is None and method in {"POST", "PUT", "PATCH", "DELETE"}:
        payload = b""
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else None
            return response.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"detail": raw}
        raise ApiError(exc.code, parsed) from None


def _api(base: str, method: str, path: str, body: dict | None = None, token: str | None = None):
    status, parsed = _request(method, base + path, body=body, token=token)
    return status, parsed


def _owner(fixture: dict) -> dict:
    owners = [user for user in fixture["users"] if user.get("role", "owner") == "owner"]
    if len(owners) != 1:
        raise RuntimeError("fixtures/mock-data.json needs exactly one user with role owner")
    return owners[0]


def _login_or_register(base: str, user: dict) -> str:
    login_body = {"email": user["email"], "password": user["password"], "provider": "local"}
    try:
        _, tokens = _api(base, "POST", "/api/v1/auth/login", login_body)
        return tokens["accessToken"]
    except ApiError as exc:
        if exc.status != 401:
            raise
    try:
        _, tokens = _api(
            base,
            "POST",
            "/api/v1/auth/register",
            {
                "email": user["email"],
                "password": user["password"],
                "displayName": user["displayName"],
            },
        )
        return tokens["accessToken"]
    except ApiError as exc:
        if exc.status == 409:
            raise RuntimeError(
                f"{user['email']} is already registered, but the fixture password was rejected. "
                "The account password must match fixtures/mock-data.json."
            ) from exc
        raise


def _ensure_org(base: str, token: str, org: dict) -> str:
    _, orgs = _api(base, "GET", "/api/v1/orgs", token=token)
    match = next((item for item in orgs if item.get("slug") == org["slug"]), None)
    if match is None:
        try:
            _, created = _api(
                base,
                "POST",
                "/api/v1/orgs",
                {"name": org["name"], "slug": org["slug"]},
                token=token,
            )
        except ApiError as exc:
            if exc.status == 409:
                raise RuntimeError(
                    f"Organization slug {org['slug']} is already taken by another account."
                ) from exc
            raise
        return created["id"]
    if match.get("name") != org["name"]:
        raise RuntimeError(
            f"Organization {org['slug']} exists as {match.get('name')!r}, "
            f"not the fixture name {org['name']!r}. The API cannot rename it."
        )
    return match["id"]


def _ensure_project(base: str, token: str, org_id: str, project: dict) -> str:
    _, projects = _api(base, "GET", f"/api/v1/orgs/{org_id}/projects", token=token)
    match = next((item for item in projects if item.get("name") == project["name"]), None)
    body = {"name": project["name"], "description": project.get("description") or ""}
    if match is None:
        _, created = _api(base, "POST", f"/api/v1/orgs/{org_id}/projects", body, token=token)
        return created["id"]
    if (match.get("description") or "") != body["description"]:
        _api(base, "PUT", f"/api/v1/orgs/{org_id}/projects/{match['id']}", body, token=token)
    return match["id"]


def _ensure_benchmark(base: str, token: str, org_id: str, benchmark: dict, project_id: str) -> str:
    _, benchmarks = _api(base, "GET", f"/api/v1/orgs/{org_id}/benchmarks", token=token)
    match = next((item for item in benchmarks if item.get("name") == benchmark["name"]), None)
    body = {
        "name": benchmark["name"],
        "description": benchmark.get("description") or "",
        "projectId": project_id,
    }
    if match is None:
        _, created = _api(base, "POST", f"/api/v1/orgs/{org_id}/benchmarks", body, token=token)
        return created["id"]
    if (match.get("description") or "") != body["description"] or match.get("projectId") != project_id:
        _api(
            base,
            "PUT",
            f"/api/v1/orgs/{org_id}/benchmarks/{match['id']}",
            body,
            token=token,
        )
    return match["id"]


def _ensure_published_version(base: str, token: str, org_id: str, benchmark_id: str, benchmark: dict) -> tuple[str, str]:
    task = benchmark["task"]
    suite = benchmark["visibleSuite"]
    _, versions = _api(
        base,
        "GET",
        f"/api/v1/orgs/{org_id}/benchmarks/{benchmark_id}/versions",
        token=token,
    )
    version = next((item for item in versions if item.get("version") == benchmark["version"]), None)
    if version is None:
        _, version = _api(
            base,
            "POST",
            f"/api/v1/orgs/{org_id}/benchmarks/{benchmark_id}/versions",
            {"version": benchmark["version"], "changelog": benchmark.get("changelog") or ""},
            token=token,
        )
    version_id = version["id"]
    _, tasks = _api(
        base,
        "GET",
        f"/api/v1/orgs/{org_id}/benchmark-versions/{version_id}/tasks",
        token=token,
    )
    existing = next((item for item in tasks if item.get("slug") == task["slug"]), None)
    if version.get("status") == "PUBLISHED":
        _require_published_task(existing, task, suite)
        return version_id, existing["id"]

    task_body = {
        "title": task["title"],
        "prompt": task["prompt"],
        "acceptanceCriteria": task.get("acceptanceCriteria") or "",
        "category": task.get("category") or "",
        "difficulty": task.get("difficulty") or "EASY",
        "languageTags": task.get("languageTags") or ["java"],
        "timeoutSec": task.get("timeoutSec") or 90,
    }
    if existing is None:
        _, existing = _api(
            base,
            "POST",
            f"/api/v1/orgs/{org_id}/benchmark-versions/{version_id}/tasks",
            {"slug": task["slug"], **task_body},
            token=token,
        )
    elif existing.get("title") != task["title"] or existing.get("prompt") != task["prompt"]:
        _, existing = _api(
            base,
            "PUT",
            f"/api/v1/orgs/{org_id}/tasks/{existing['id']}",
            task_body,
            token=token,
        )
    _, suite_row = _api(
        base,
        "PUT",
        f"/api/v1/orgs/{org_id}/tasks/{existing['id']}/suites/visible",
        {
            "framework": suite["framework"],
            "entryCommand": suite.get("entryCommand") or "",
            "fixtureContent": suite.get("fixtureContent") or "",
            "caseCount": suite.get("caseCount") or 1,
        },
        token=token,
    )
    if suite_row.get("framework") != suite["framework"]:
        raise RuntimeError(
            f"Visible suite framework is {suite_row.get('framework')!r}, expected {suite['framework']!r}"
        )
    _, published = _api(
        base,
        "POST",
        f"/api/v1/orgs/{org_id}/benchmark-versions/{version_id}/publish",
        token=token,
    )
    if published.get("status") != "PUBLISHED":
        raise RuntimeError(f"Benchmark version {benchmark['version']} did not publish: {published}")
    return version_id, existing["id"]


def _require_published_task(existing: dict | None, task: dict, suite: dict) -> None:
    label = f"{task['slug']} on published version"
    if existing is None:
        raise RuntimeError(
            f"{label} is missing. Published versions are immutable; "
            "the fixture cannot add it. Reset this benchmark version or use a fresh database."
        )
    if existing.get("title") != task["title"]:
        raise RuntimeError(
            f"{label} title is {existing.get('title')!r}, fixture expects {task['title']!r}."
        )
    visible = existing.get("visibleSuite") or {}
    if not existing.get("hasVisibleSuite") or visible.get("framework") != suite["framework"]:
        raise RuntimeError(
            f"{label} visible suite is {visible.get('framework')!r}, fixture expects {suite['framework']!r}."
        )


def _ensure_agent(base: str, token: str, org_id: str, agent: dict) -> str:
    _, agents = _api(base, "GET", f"/api/v1/orgs/{org_id}/agents", token=token)
    match = next((item for item in agents if item.get("name") == agent["name"]), None)
    if match is None:
        _, created = _api(
            base,
            "POST",
            f"/api/v1/orgs/{org_id}/agents",
            {"name": agent["name"], "adapterType": agent["adapterType"], "config": {}},
            token=token,
        )
        return created["id"]
    if match.get("adapterType") != agent["adapterType"]:
        raise RuntimeError(
            f"Agent {agent['name']!r} is {match.get('adapterType')}, not {agent['adapterType']}."
        )
    if not match.get("enabled", True):
        _, match = _api(
            base,
            "PATCH",
            f"/api/v1/orgs/{org_id}/agents/{match['id']}",
            {"enabled": True},
            token=token,
        )
    return match["id"]


def _ensure_publication(base: str, token: str, org_id: str, version_id: str, board: dict) -> None:
    _, status = _api(
        base,
        "GET",
        f"/api/v1/orgs/{org_id}/benchmark-versions/{version_id}/leaderboard/publication",
        token=token,
    )
    if status.get("published") and status.get("publicSlug") != board["slug"]:
        _api(
            base,
            "POST",
            f"/api/v1/orgs/{org_id}/benchmark-versions/{version_id}/leaderboard/unpublish",
            token=token,
        )
    _, published = _api(
        base,
        "POST",
        f"/api/v1/orgs/{org_id}/benchmark-versions/{version_id}/leaderboard/publish",
        {"publicSlug": board["slug"], "title": board["title"]},
        token=token,
    )
    if not published.get("published") or published.get("publicSlug") != board["slug"]:
        raise RuntimeError(f"Public board did not publish at {board['slug']}: {published}")
    if published.get("title") != board["title"]:
        raise RuntimeError(
            f"Public board title is {published.get('title')!r}, fixture expects {board['title']!r}."
        )


def _list_comments(base: str, slug: str) -> list[dict]:
    comments: list[dict] = []
    page = 0
    while True:
        _, body = _api(base, "GET", f"/api/v1/public/leaderboards/{slug}/comments?page={page}&size=100")
        batch = list(body.get("comments") or [])
        comments.extend(batch)
        total = int((body.get("page") or {}).get("total") or 0)
        if not batch or len(comments) >= total:
            return comments
        page += 1


def _reset_comments(base: str, token: str, org_id: str, version_id: str, slug: str, wanted: list[dict]) -> None:
    desired = [(item["displayName"], item["body"]) for item in wanted]
    desired_set = set(desired)
    kept: set[tuple[str, str]] = set()
    for comment in _list_comments(base, slug):
        key = (comment.get("displayName"), comment.get("body"))
        if key in desired_set and key not in kept:
            kept.add(key)
            continue
        _api(
            base,
            "DELETE",
            f"/api/v1/orgs/{org_id}/benchmark-versions/{version_id}/leaderboard/comments/{comment['id']}",
            token=token,
        )
    for display_name, body in desired:
        if (display_name, body) in kept:
            continue
        try:
            _api(
                base,
                "POST",
                f"/api/v1/public/leaderboards/{slug}/comments",
                {"displayName": display_name, "body": body},
                token=token,
            )
        except ApiError as exc:
            if exc.status == 429 and (display_name, body) in {
                (item.get("displayName"), item.get("body")) for item in _list_comments(base, slug)
            }:
                continue
            raise
    remaining = [(item.get("displayName"), item.get("body")) for item in _list_comments(base, slug)]
    if remaining != desired:
        raise RuntimeError(f"Public comments are {remaining!r}, fixture expects {desired!r}.")


def _upsert_votes(base: str, slug: str, votes: list[dict]) -> dict:
    summary = {"upCount": 0, "downCount": 0, "score": 0}
    for vote in votes:
        body = {"value": vote["value"], "voterFingerprint": vote["voterFingerprint"]}
        path = f"/api/v1/public/leaderboards/{slug}/votes"
        try:
            _, summary = _api(base, "POST", path, body)
        except ApiError as exc:
            if exc.status != 429:
                raise
            _, summary = _api(
                base,
                "GET",
                f"{path}?voterFingerprint={vote['voterFingerprint']}",
            )
            if summary.get("myVote") != vote["value"]:
                raise RuntimeError(
                    f"Vote for {vote['voterFingerprint']} was rate-limited and is not {vote['value']}."
                ) from exc
        if summary.get("myVote") != vote["value"]:
            raise RuntimeError(f"Vote upsert did not stick: {summary}")
    expected_up = sum(1 for vote in votes if vote["value"] == 1)
    expected_down = sum(1 for vote in votes if vote["value"] == -1)
    if summary.get("upCount") != expected_up or summary.get("downCount") != expected_down:
        raise RuntimeError(
            "Public board votes are "
            f"up={summary.get('upCount')} down={summary.get('downCount')}, "
            f"fixture expects up={expected_up} down={expected_down}. "
            "The API cannot delete stray votes."
        )
    return summary


def _user_by_role(fixture: dict, role: str) -> dict:
    matches = [user for user in fixture["users"] if user.get("role") == role]
    if len(matches) != 1:
        raise RuntimeError(f"fixtures/mock-data.json needs exactly one user with role {role}")
    return matches[0]


def _ensure_account(base: str, user: dict) -> str:
    token = _login_or_register(base, user)
    _, me = _api(base, "GET", "/api/v1/me", token=token)
    if me.get("email") != user["email"]:
        raise RuntimeError(f"Logged in as {me.get('email')}, expected {user['email']}")
    if me.get("displayName") != user["displayName"]:
        raise RuntimeError(
            f"User {user['email']} display name is {me.get('displayName')!r}, "
            f"fixture expects {user['displayName']!r}. Registration display names cannot be changed."
        )
    return token


def _ensure_member(base: str, token: str, org_id: str, email: str, role: str) -> str:
    try:
        _, created = _api(
            base,
            "POST",
            f"/api/v1/orgs/{org_id}/members",
            {"email": email, "role": role},
            token=token,
        )
        return created["userId"]
    except ApiError as exc:
        if exc.status != 409:
            raise
    _, members = _api(base, "GET", f"/api/v1/orgs/{org_id}/members", token=token)
    match = next((item for item in members if item.get("email") == email), None)
    if match is None:
        raise RuntimeError(f"{email} is not a member of the seeded organization")
    if match.get("role") != role:
        _, match = _api(
            base,
            "PUT",
            f"/api/v1/orgs/{org_id}/members/{match['userId']}",
            {"role": role},
            token=token,
        )
    if match.get("role") != role:
        raise RuntimeError(f"{email} role is {match.get('role')!r}, fixture expects {role!r}")
    return match["userId"]


def _upsert_suite(base: str, token: str, org_id: str, task_id: str, visibility: str, suite: dict) -> dict:
    _, suite_row = _api(
        base,
        "PUT",
        f"/api/v1/orgs/{org_id}/tasks/{task_id}/suites/{visibility}",
        {
            "framework": suite["framework"],
            "entryCommand": suite.get("entryCommand") or "",
            "fixtureContent": suite.get("fixtureContent") or "",
            "caseCount": suite.get("caseCount") or 1,
        },
        token=token,
    )
    return suite_row


def _task_body(task: dict) -> dict:
    return {
        "title": task["title"],
        "prompt": task["prompt"],
        "acceptanceCriteria": task.get("acceptanceCriteria") or "",
        "category": task.get("category") or "",
        "difficulty": task.get("difficulty") or "EASY",
        "languageTags": task.get("languageTags") or ["java"],
        "timeoutSec": task.get("timeoutSec") or 90,
    }


def _ensure_edge_benchmark(base: str, token: str, org_id: str, project_id: str, edge: dict) -> dict:
    benchmark = {
        "name": edge["benchmarkName"],
        "description": edge.get("benchmarkDescription") or "",
    }
    benchmark_id = _ensure_benchmark(base, token, org_id, benchmark, project_id)
    draft_version_id, draft_task_id = _ensure_draft_with_suites(
        base, token, org_id, benchmark_id, edge
    )
    published_spec = {
        "version": edge["publishedVersion"],
        "changelog": edge.get("publishedChangelog") or "",
        "task": edge["publishedTask"],
        "visibleSuite": edge["visibleSuite"],
    }
    published_version_id, _published_task_id = _ensure_published_version(
        base, token, org_id, benchmark_id, published_spec
    )
    _ensure_unpublished(base, token, org_id, published_version_id, edge["unpublishedBoard"])
    return {
        "benchmark_id": benchmark_id,
        "draft_version_id": draft_version_id,
        "draft_task_id": draft_task_id,
        "published_version_id": published_version_id,
    }


def _ensure_draft_with_suites(base: str, token: str, org_id: str, benchmark_id: str, edge: dict) -> tuple[str, str]:
    task = edge["draftTask"]
    _, versions = _api(
        base,
        "GET",
        f"/api/v1/orgs/{org_id}/benchmarks/{benchmark_id}/versions",
        token=token,
    )
    version = next((item for item in versions if item.get("version") == edge["draftVersion"]), None)
    if version is None:
        _, version = _api(
            base,
            "POST",
            f"/api/v1/orgs/{org_id}/benchmarks/{benchmark_id}/versions",
            {"version": edge["draftVersion"], "changelog": edge.get("draftChangelog") or ""},
            token=token,
        )
    if version.get("status") != "DRAFT":
        raise RuntimeError(
            f"Edge version {edge['draftVersion']} is {version.get('status')}, not DRAFT. "
            "Published versions are immutable, so the hidden suite cannot be refreshed."
        )
    version_id = version["id"]
    _, tasks = _api(
        base,
        "GET",
        f"/api/v1/orgs/{org_id}/benchmark-versions/{version_id}/tasks",
        token=token,
    )
    existing = next((item for item in tasks if item.get("slug") == task["slug"]), None)
    body = _task_body(task)
    if existing is None:
        _, existing = _api(
            base,
            "POST",
            f"/api/v1/orgs/{org_id}/benchmark-versions/{version_id}/tasks",
            {"slug": task["slug"], **body},
            token=token,
        )
    elif existing.get("title") != task["title"] or existing.get("prompt") != task["prompt"]:
        _, existing = _api(
            base,
            "PUT",
            f"/api/v1/orgs/{org_id}/tasks/{existing['id']}",
            body,
            token=token,
        )
    visible = _upsert_suite(base, token, org_id, existing["id"], "visible", edge["visibleSuite"])
    hidden = _upsert_suite(base, token, org_id, existing["id"], "hidden", edge["hiddenSuite"])
    if visible.get("framework") != edge["visibleSuite"]["framework"]:
        raise RuntimeError(
            f"Draft visible suite is {visible.get('framework')!r}, "
            f"expected {edge['visibleSuite']['framework']!r}"
        )
    if hidden.get("contentRestricted"):
        raise RuntimeError("Owner seed received a restricted hidden suite")
    fixture_body = (edge["hiddenSuite"].get("fixtureContent") or "").strip()
    if fixture_body and fixture_body not in (hidden.get("fixtureContent") or ""):
        raise RuntimeError("Hidden suite fixture was not stored")
    if hidden.get("framework") != edge["hiddenSuite"]["framework"]:
        raise RuntimeError(
            f"Draft hidden suite is {hidden.get('framework')!r}, "
            f"expected {edge['hiddenSuite']['framework']!r}"
        )
    return version_id, existing["id"]


def _ensure_unpublished(base: str, token: str, org_id: str, version_id: str, board: dict) -> None:
    _, published = _api(
        base,
        "POST",
        f"/api/v1/orgs/{org_id}/benchmark-versions/{version_id}/leaderboard/publish",
        {"publicSlug": board["slug"], "title": board["title"]},
        token=token,
    )
    if not published.get("published") or published.get("publicSlug") != board["slug"]:
        raise RuntimeError(f"Edge board did not publish at {board['slug']}: {published}")
    _, unpublished = _api(
        base,
        "POST",
        f"/api/v1/orgs/{org_id}/benchmark-versions/{version_id}/leaderboard/unpublish",
        token=token,
    )
    if unpublished.get("published"):
        raise RuntimeError(f"Edge board stayed published: {unpublished}")
    if unpublished.get("publicSlug") != board["slug"]:
        raise RuntimeError(
            f"Unpublished board slug is {unpublished.get('publicSlug')!r}, expected {board['slug']!r}"
        )
    try:
        _api(base, "GET", f"/api/v1/public/leaderboards/{board['slug']}")
    except ApiError as exc:
        if exc.status != 404:
            raise
    else:
        raise RuntimeError(f"Unpublished board {board['slug']} is still readable")


def _ensure_flake(base: str, token: str, org_id: str, flake: dict) -> str:
    _, rows = _api(
        base,
        "GET",
        f"/api/v1/orgs/{org_id}/flake-quarantines?includeInactive=true",
        token=token,
    )
    case_key = flake["caseKey"]
    active = next(
        (
            row
            for row in rows
            if row.get("active") and row.get("caseKey") == case_key and not row.get("suiteId")
        ),
        None,
    )
    if active is not None:
        return active["id"]
    _, created = _api(
        base,
        "POST",
        f"/api/v1/orgs/{org_id}/flake-quarantines",
        {"caseKey": case_key, "reason": flake.get("reason") or ""},
        token=token,
    )
    if created.get("caseKey") != case_key or not created.get("active"):
        raise RuntimeError(f"Flake quarantine was not created: {created}")
    return created["id"]


def _case_pairs(cases: list[dict]) -> list[tuple[str, str]]:
    return [(item["name"], item["status"]) for item in cases]


def _ensure_regression_run(base: str, token: str, run: dict) -> dict:
    """Create the fixture run once. A later seed reuses the same git SHA and case rows."""
    _, listing = _api(base, "GET", "/api/v1/regression-runs?limit=100", token=token)
    if not isinstance(listing, list):
        raise RuntimeError(f"Regression run list was {type(listing).__name__}, expected a list")
    wanted = _case_pairs(run.get("cases") or [])
    for item in listing:
        if item.get("suiteName") != run["suiteName"] or item.get("gitSha") != run["gitSha"]:
            continue
        _, detail = _api(base, "GET", f"/api/v1/regression-runs/{item['id']}", token=token)
        actual = _case_pairs(detail.get("cases") or [])
        if actual == wanted:
            return detail
        break
    _, created = _api(
        base,
        "POST",
        "/api/v1/regression-runs",
        {
            "gitRepo": run["gitRepo"],
            "gitBranch": run["gitBranch"],
            "gitSha": run["gitSha"],
            "suiteName": run["suiteName"],
            "cases": run["cases"],
        },
        token=token,
    )
    return created


def seed_mock_data(api_url: str, fixture: dict | None = None) -> dict:
    """Upsert the committed fixture. Returns the identities the UI tests use."""
    data = fixture if fixture is not None else load_fixture()
    base = api_url.rstrip("/")
    user = _owner(data)
    if len(data["organizations"]) != 1:
        raise RuntimeError("This suite seeds exactly one organization from the fixture")
    org = data["organizations"][0]
    if org.get("ownerEmail") != user["email"]:
        raise RuntimeError("organization.ownerEmail must match the owner user email")
    if len(org.get("projects") or []) != 1 or len(org.get("benchmarks") or []) != 1:
        raise RuntimeError("The suite fixture needs one project and one benchmark")
    if len(org.get("agents") or []) != 1 or len(org.get("publicBoards") or []) != 1:
        raise RuntimeError("The suite fixture needs one agent and one public board")

    project = org["projects"][0]
    benchmark = org["benchmarks"][0]
    agent = org["agents"][0]
    board = org["publicBoards"][0]
    if benchmark.get("projectName") != project["name"]:
        raise RuntimeError("benchmark.projectName must match the seeded project name")
    if board.get("benchmarkName") != benchmark["name"] or board.get("version") != benchmark["version"]:
        raise RuntimeError("public board must target the seeded benchmark version")

    token = _ensure_account(base, user)

    org_id = _ensure_org(base, token, org)
    project_id = _ensure_project(base, token, org_id, project)
    benchmark_id = _ensure_benchmark(base, token, org_id, benchmark, project_id)
    version_id, task_id = _ensure_published_version(base, token, org_id, benchmark_id, benchmark)
    agent_id = _ensure_agent(base, token, org_id, agent)
    _ensure_publication(base, token, org_id, version_id, board)
    _reset_comments(base, token, org_id, version_id, board["slug"], board.get("comments") or [])
    summary = _upsert_votes(base, board["slug"], board.get("votes") or [])
    comment = (board.get("comments") or [{}])[0]
    regression = data.get("regressionRun")
    regression_detail = _ensure_regression_run(base, token, regression) if regression else None
    regression_cases = (regression or {}).get("cases") or []
    passed_case = next((item["name"] for item in regression_cases if item.get("status") == "passed"), "")
    failed_case = next((item["name"] for item in regression_cases if item.get("status") == "failed"), "")

    edge = org.get("edge") or {}
    viewer = _user_by_role(data, "viewer") if any(user.get("role") == "viewer" for user in data["users"]) else None
    if viewer is None or not edge:
        raise RuntimeError("fixtures/mock-data.json needs a viewer user and organizations[0].edge")
    _ensure_account(base, viewer)
    member = next(item for item in (org.get("members") or []) if item.get("email") == viewer["email"])
    _ensure_member(base, token, org_id, viewer["email"], member["role"])
    edge_ids = _ensure_edge_benchmark(base, token, org_id, project_id, edge)
    flake_id = _ensure_flake(base, token, org_id, edge["flake"])

    return {
        "email": user["email"],
        "password": user["password"],
        "display_name": user["displayName"],
        "org_name": org["name"],
        "org_slug": org["slug"],
        "org_id": org_id,
        "project_name": project["name"],
        "project_description": project.get("description") or "",
        "project_id": project_id,
        "benchmark_name": benchmark["name"],
        "benchmark_id": benchmark_id,
        "version": benchmark["version"],
        "version_id": version_id,
        "task_slug": benchmark["task"]["slug"],
        "task_title": benchmark["task"]["title"],
        "task_id": task_id,
        "agent_name": agent["name"],
        "agent_id": agent_id,
        "public_slug": board["slug"],
        "public_title": board["title"],
        "comment_author": comment.get("displayName", ""),
        "comment": comment.get("body", ""),
        "up_count": int(summary.get("upCount") or 0),
        "score": int(summary.get("score") or 0),
        "regression_suite": (regression or {}).get("suiteName", ""),
        "regression_passed_case": passed_case,
        "regression_failed_case": failed_case,
        "regression_run_id": (regression_detail or {}).get("id", ""),
        "viewer_email": viewer["email"],
        "viewer_password": viewer["password"],
        "viewer_display_name": viewer["displayName"],
        "viewer_role": member["role"],
        "edge_benchmark_name": edge["benchmarkName"],
        "draft_version": edge["draftVersion"],
        "empty_version": edge["emptyVersion"],
        "edge_published_version": edge["publishedVersion"],
        "draft_task_title": edge["draftTask"]["title"],
        "draft_task_slug": edge["draftTask"]["slug"],
        "visible_framework": edge["visibleSuite"]["framework"],
        "hidden_framework": edge["hiddenSuite"]["framework"],
        "hidden_fixture": edge["hiddenSuite"].get("fixtureContent") or "",
        "unpublished_slug": edge["unpublishedBoard"]["slug"],
        "unpublished_title": edge["unpublishedBoard"]["title"],
        "flake_case_key": edge["flake"]["caseKey"],
        "flake_reason": edge["flake"].get("reason") or "",
        "flake_id": flake_id,
        "edge_benchmark_id": edge_ids["benchmark_id"],
        "draft_version_id": edge_ids["draft_version_id"],
    }


def main() -> int:
    api_url = os.environ.get("AGENTBENCH_API_URL", "http://localhost:8080").rstrip("/")
    try:
        ctx = seed_mock_data(api_url)
    except Exception as exc:
        print(f"Seed failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "email": ctx["email"],
                "displayName": ctx["display_name"],
                "org": ctx["org_slug"],
                "project": ctx["project_name"],
                "benchmark": ctx["benchmark_name"],
                "version": ctx["version"],
                "task": ctx["task_slug"],
                "agent": ctx["agent_name"],
                "publicSlug": ctx["public_slug"],
                "comment": ctx["comment"],
                "upCount": ctx["up_count"],
                "score": ctx["score"],
                "viewer": ctx["viewer_email"],
                "edgeBenchmark": ctx["edge_benchmark_name"],
                "draftVersion": ctx["draft_version"],
                "unpublishedSlug": ctx["unpublished_slug"],
                "flakeCaseKey": ctx["flake_case_key"],
                "regressionSuite": ctx.get("regression_suite") or None,
                "regressionRunId": ctx.get("regression_run_id") or None,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
