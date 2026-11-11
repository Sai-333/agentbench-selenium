"""Edge cases that run after the ordered regression in the same browser session.

Identities come from the seeded fixture. API setup is used where the UI has no
control (flake quarantine, invalid vote). Assertions are on the rendered UI or
on the response the signed-in page receives. Product failures are assertions,
not skips.
"""

from __future__ import annotations

import time
from pathlib import Path

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select

from tests.test_regression import _auth_form, _click_named_item, _require
from tests.ui import (
    TERMINAL_STATES,
    assert_no_error,
    browser_json,
    click_button,
    click_submit,
    evaluation_state,
    export_statuses,
    field,
    fill,
    form_by_heading,
    open_tab,
    scroll,
    visible_errors,
    wait,
    wait_error,
    wait_for_evaluation,
    wait_outcome,
)

_REJECTED_ORG = "Edge Rejected Org"
_MIDFLOW_PROJECT = "Logout Midflow Project"


def _home(driver, base_url: str, display_name: str) -> None:
    driver.get(base_url + "/")
    wait(driver, 30).until(
        lambda d: "Loading session" not in d.find_element(By.TAG_NAME, "body").text
    )
    wait_outcome(driver, display_name, timeout=60)


def _logged_in(driver) -> bool:
    return bool(driver.find_elements(By.XPATH, "//button[normalize-space()='Log out']"))


def _logout(driver) -> None:
    if not _logged_in(driver):
        return
    click_button(driver, "Log out")
    wait_outcome(driver, "Sign in to manage organizations")


def _login(driver, email: str, password: str, display_name: str) -> None:
    if _logged_in(driver):
        body = driver.find_element(By.TAG_NAME, "body").text
        if email in body and display_name in body:
            return
        _logout(driver)
    click_button(driver, "Log in")
    form = _auth_form(driver)
    fill(driver, form, "Email", email)
    fill(driver, form, "Password", password)
    click_submit(driver, form, expected=display_name, timeout=60)


def _click_version(driver, version: str):
    def find(d):
        for element in d.find_elements(By.CSS_SELECTOR, "button.list-item"):
            try:
                if not element.is_displayed():
                    continue
                first = element.text.splitlines()[0].strip()
                token = first.split()[0] if first else ""
                if token == version:
                    return element
            except StaleElementReferenceException:
                return False
        return False

    button = wait(driver, 20).until(find)
    scroll(driver, button)
    button.click()


def _version_listed(driver, version: str) -> bool:
    for element in driver.find_elements(By.CSS_SELECTOR, "button.list-item"):
        try:
            first = element.text.splitlines()[0].strip()
        except StaleElementReferenceException:
            continue
        token = first.split()[0] if first else ""
        if token == version:
            return True
    return False


def _open_benchmark(driver, name: str) -> None:
    open_tab(driver, "Benchmarks", "Benchmarks")
    _click_named_item(driver, name)
    wait_outcome(driver, name)


def _submit(driver, form) -> None:
    button = form.find_element(By.CSS_SELECTOR, "button[type='submit']")
    scroll(driver, button)
    wait(driver, 20).until(lambda d: button.is_enabled())
    button.click()


def _clear_banner(driver, ctx) -> None:
    if not visible_errors(driver):
        return
    _click_named_item(driver, ctx["org_name"])
    wait(driver, 30).until(lambda d: not visible_errors(d))


def _assert_html_blocked(driver, form) -> None:
    _submit(driver, form)
    blocked = driver.execute_script("return arguments[0].checkValidity() === false;", form)
    assert blocked, "Browser accepted a form that should be blocked"
    assert not visible_errors(driver), "Blocked form still showed an API error: " + " | ".join(
        visible_errors(driver)
    )


def _detail(payload: dict) -> str:
    body = payload.get("body") if isinstance(payload, dict) else None
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("title") or body)
    return str(body or payload)


def _choose_select_text(driver, select_el, text: str) -> None:
    driver.execute_script(
        """
        const el = arguments[0];
        const text = arguments[1];
        const option = Array.from(el.options).find((item) => item.text === text);
        if (!option) {
          throw new Error('missing option ' + text);
        }
        const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
        setter.call(el, option.value);
        el.dispatchEvent(new Event('change', { bubbles: true }));
        """,
        select_el,
        text,
    )


def _assert_export(driver, download_dir: Path, label: str) -> None:
    export_statuses(driver)
    before = set(download_dir.iterdir())
    click_button(driver, label)
    deadline = time.monotonic() + 25
    statuses: list[tuple[str, int]] = []
    while time.monotonic() < deadline:
        errors = visible_errors(driver)
        if errors:
            raise AssertionError("Export failed: " + " | ".join(errors))
        statuses.extend(export_statuses(driver))
        if any(status == 200 and "export" in url for url, status in statuses):
            return
        fresh = [
            path
            for path in download_dir.iterdir()
            if path not in before
            and path.suffix in {".csv", ".json", ".pdf"}
            and path.stat().st_size > 0
        ]
        if fresh:
            return
        time.sleep(0.4)
    raise AssertionError(f"{label} did not return HTTP 200 or a download (statuses={statuses})")


def _panel(driver, heading_prefix: str):
    return driver.find_element(
        By.XPATH,
        f"//h2[starts-with(normalize-space(), '{heading_prefix}')]/ancestor::div[contains(@class,'panel')][1]",
    )


class TestAgentBenchEdges:
    def test_14_blank_duplicate_and_invalid_names(self, driver, base_url, ctx):
        _require(ctx, "signed_in")
        _home(driver, base_url, ctx["display_name"])

        org = form_by_heading(driver, "Create organization")
        fill(driver, org, "Name", "")
        _assert_html_blocked(driver, org)

        fill(driver, org, "Name", "  ")
        fill(driver, org, "Slug", "ab")
        _submit(driver, org)
        wait_error(driver, "name: must not be blank")

        fill(driver, org, "Name", ctx["org_name"])
        fill(driver, org, "Slug", "")
        _submit(driver, org)
        wait_error(driver, "Organization slug already exists")

        fill(driver, org, "Name", _REJECTED_ORG)
        slug = field(org, "Slug")
        driver.execute_script("arguments[0].removeAttribute('pattern');", slug)
        fill(driver, org, "Slug", "Bad_Slug")
        _submit(driver, org)
        wait_error(driver, "slug must be lowercase kebab-case")
        listed = [item.text for item in driver.find_elements(By.CSS_SELECTOR, "button.list-item")]
        assert not any(_REJECTED_ORG in item for item in listed)

        _clear_banner(driver, ctx)
        open_tab(driver, "Projects", "Projects")
        project = form_by_heading(driver, "Create project")
        fill(driver, project, "Name", "")
        _assert_html_blocked(driver, project)
        fill(driver, project, "Name", " ")
        _submit(driver, project)
        wait_error(driver, "name: must not be blank")
        fill(driver, project, "Name", ctx["project_name"])
        _submit(driver, project)
        wait_error(driver, "Project name already exists in organization")
        assert driver.find_element(By.TAG_NAME, "body").text.count(ctx["project_name"]) >= 1

        open_tab(driver, "Benchmarks", "Benchmarks")
        benchmark = form_by_heading(driver, "Create benchmark")
        fill(driver, benchmark, "Name", " ")
        _submit(driver, benchmark)
        wait_error(driver, "name: must not be blank")
        fill(driver, benchmark, "Name", ctx["benchmark_name"])
        _submit(driver, benchmark)
        wait_error(driver, "Benchmark name already exists in organization")

        _open_benchmark(driver, ctx["benchmark_name"])
        version_form = form_by_heading(driver, "Create draft version")
        fill(driver, version_form, "Version", "bad version")
        _submit(driver, version_form)
        wait_error(driver, "version must be alphanumeric")
        assert not _version_listed(driver, "bad version")
        fill(driver, version_form, "Version", ctx["version"])
        _submit(driver, version_form)
        wait_error(driver, "Version already exists for this benchmark")

        open_tab(driver, "Agents", "Agents")
        agent = form_by_heading(driver, "Register agent")
        fill(driver, agent, "Name", ctx["agent_name"])
        _submit(driver, agent)
        wait_error(driver, "Agent name already exists in organization")
        ctx["edges_validation"] = True

    def test_15_owner_sees_hidden_and_visible(self, driver, base_url, ctx):
        _require(ctx, "signed_in")
        _home(driver, base_url, ctx["display_name"])
        _open_benchmark(driver, ctx["edge_benchmark_name"])
        _click_version(driver, ctx["draft_version"])
        wait_outcome(driver, "DRAFT")
        wait_outcome(driver, ctx["draft_task_title"])
        _click_named_item(driver, ctx["draft_task_title"])
        wait_outcome(driver, f"Visible suite: {ctx['visible_framework']}")
        body = driver.find_element(By.TAG_NAME, "body").text
        hidden_at = body.find("Hidden suite:")
        assert hidden_at >= 0, body
        assert ctx["hidden_framework"] in body[hidden_at : hidden_at + 40]
        assert "present (restricted)" not in body
        assert "visible" in body and "hidden" in body
        assert ctx["hidden_fixture"].strip() not in driver.page_source
        assert driver.find_elements(By.XPATH, "//h3[normalize-space()='Hidden suite']")
        assert driver.find_elements(By.XPATH, "//h3[normalize-space()='Visible suite']")
        assert_no_error(driver)
        ctx["edges_suites"] = True

    def test_16_empty_version_cannot_publish(self, driver, base_url, ctx):
        _require(ctx, "signed_in")
        _home(driver, base_url, ctx["display_name"])
        _open_benchmark(driver, ctx["edge_benchmark_name"])
        version = ctx["empty_version"]
        if not _version_listed(driver, version):
            form = form_by_heading(driver, "Create draft version")
            fill(driver, form, "Version", version)
            _submit(driver, form)

            def created_or_conflict(d):
                errors = visible_errors(d)
                if any("Version already exists" in error for error in errors):
                    return "exists"
                if _version_listed(d, version):
                    return "created"
                return False

            outcome = wait(driver, 30).until(created_or_conflict)
            assert outcome in {"created", "exists"}
        _click_version(driver, version)
        wait_outcome(driver, "DRAFT")
        wait_outcome(driver, "No tasks in this version")
        click_button(driver, "Publish version (immutable)")
        wait_error(driver, "Cannot publish a version with no tasks")
        assert _version_listed(driver, version)
        ctx["edges_empty_publish"] = True

    def test_17_score_and_flake_quarantine(self, driver, base_url, ctx):
        _require(ctx, "signed_in", "evaluation")
        _home(driver, base_url, ctx["display_name"])
        open_tab(driver, "Evaluations", "Evaluations")
        panel = _panel(driver, "Evaluations")
        first = wait(driver, 20).until(
            lambda d: _panel(d, "Evaluations").find_elements(By.CSS_SELECTOR, "button.list-item")
        )[0]
        scroll(driver, first)
        first.click()
        wait_outcome(driver, "Evaluation detail")
        state = evaluation_state(driver)
        if state not in TERMINAL_STATES:
            state, _seen = wait_for_evaluation(driver, 90)
        detail = driver.find_element(
            By.XPATH, "//h2[normalize-space()='Evaluation detail']/ancestor::section[1]"
        ).text
        assert "Score" in detail, detail[:800]
        assert "F2P" in detail and "P2P" in detail and "P2F" in detail and "F2F" in detail
        if "INCOMPLETE" in detail:
            assert "weighted total withheld" in detail
            assert (
                "No target cases configured" in detail or "quarantined case" in detail.lower()
            ), detail[:800]
        else:
            assert "/ 100" in detail, detail[:800]
        assert ctx["flake_case_key"] in detail, detail[:800]
        assert "quarantined" in detail, detail[:800]
        ctx["edges_score"] = state

        org_id = ctx["org_id"]
        missing = browser_json(driver, "POST", f"/api/v1/orgs/{org_id}/flake-quarantines", {})
        assert missing["status"] == 400, missing
        assert "Provide suiteId and/or caseKey" in _detail(missing)

        expired = browser_json(
            driver,
            "POST",
            f"/api/v1/orgs/{org_id}/flake-quarantines",
            {
                "caseKey": "selenium-edge-expired",
                "reason": "past",
                "expiresAt": "2020-01-01T00:00:00Z",
            },
        )
        assert expired["status"] == 400, expired
        assert "expiresAt must be in the future" in _detail(expired)

        long_reason = browser_json(
            driver,
            "POST",
            f"/api/v1/orgs/{org_id}/flake-quarantines",
            {"caseKey": "selenium-edge-long-reason", "reason": "r" * 1001},
        )
        assert long_reason["status"] == 400, long_reason
        assert "reason: size must be between 0 and 1000" in _detail(long_reason)

        duplicate = browser_json(
            driver,
            "POST",
            f"/api/v1/orgs/{org_id}/flake-quarantines",
            {"caseKey": ctx["flake_case_key"], "reason": ctx["flake_reason"]},
        )
        assert duplicate["status"] == 409, duplicate
        assert "Active quarantine already exists" in _detail(duplicate)

        listed = browser_json(driver, "GET", f"/api/v1/orgs/{org_id}/flake-quarantines")
        assert listed["status"] == 200, (
            "Active flake list should return the seeded case. "
            f"Client response: {listed}"
        )
        keys = [row.get("caseKey") for row in (listed.get("body") or [])]
        assert ctx["flake_case_key"] in keys
        ctx["edges_flake"] = True

    def test_18_cancel_evaluation(self, driver, base_url, ctx):
        _require(ctx, "signed_in")
        _home(driver, base_url, ctx["display_name"])
        _open_benchmark(driver, ctx["benchmark_name"])
        _click_version(driver, ctx["version"])
        wait_outcome(driver, ctx["task_title"])
        open_tab(driver, "Evaluations", "Evaluations")
        form = form_by_heading(driver, "Enqueue evaluation")
        fill(driver, form, "Published version ID", ctx["version_id"])
        fill(driver, form, "Task ID", ctx["task_id"])
        agent = Select(field(form, "Agent"))
        matching = [option.text for option in agent.options if ctx["agent_name"] in option.text]
        assert matching, "FAKE agent was not offered"
        agent.select_by_visible_text(matching[0])
        click_submit(driver, form, expected="Evaluation detail", timeout=60)

        def cancel_or_terminal(d):
            for button in d.find_elements(By.XPATH, "//button[normalize-space()='Cancel']"):
                try:
                    if button.is_displayed() and button.is_enabled():
                        return button
                except StaleElementReferenceException:
                    return False
            try:
                state = evaluation_state(d)
            except Exception:
                return False
            if state in TERMINAL_STATES:
                return state
            return False

        found = wait(driver, 20).until(cancel_or_terminal)
        assert not isinstance(found, str), (
            f"Evaluation reached {found} before Cancel was offered"
        )
        scroll(driver, found)
        found.click()
        deadline = time.monotonic() + 90
        state = ""
        while time.monotonic() < deadline:
            errors = visible_errors(driver)
            if any("already terminal" in error for error in errors):
                break
            try:
                state = evaluation_state(driver)
            except (StaleElementReferenceException, Exception):
                state = ""
            if state == "CANCELLED":
                break
            time.sleep(1)
        assert state == "CANCELLED", (
            "Cancel did not reach CANCELLED. "
            f"state={state!r} errors={visible_errors(driver)}"
        )
        assert not any(
            button.is_displayed() and button.text.strip() == "Cancel"
            for button in driver.find_elements(By.XPATH, "//button[normalize-space()='Cancel']")
        )
        ctx["edges_cancelled"] = True

    def test_19_exports_and_empty_compare(self, driver, base_url, ctx, download_dir: Path):
        _require(ctx, "signed_in")
        _home(driver, base_url, ctx["display_name"])
        _open_benchmark(driver, ctx["benchmark_name"])
        _click_version(driver, ctx["version"])
        open_tab(driver, "Leaderboard", "Org leaderboard")
        wait(driver, 30).until(
            lambda d: (
                "No completed scored evaluations" in d.find_element(By.TAG_NAME, "body").text
                or "Profile " in d.find_element(By.TAG_NAME, "body").text
            )
            and "Loading…" not in d.find_element(By.TAG_NAME, "body").text
        )
        _assert_export(driver, download_dir, "Export JSON")

        _open_benchmark(driver, ctx["edge_benchmark_name"])
        _click_version(driver, ctx["draft_version"])
        open_tab(driver, "Leaderboard", "Org leaderboard")
        panel = _panel(driver, "Org leaderboard")
        _choose_select_text(driver, field(panel, "Benchmark version"), f"{ctx['draft_version']} (DRAFT)")
        wait_outcome(driver, "No completed scored evaluations for this benchmark version yet.")
        assert driver.find_elements(By.XPATH, "//button[normalize-space()='Publish publicly']")
        _assert_export(driver, download_dir, "Export JSON")

        open_tab(driver, "Compare", "Model comparisons")
        compare = _panel(driver, "Model comparisons")
        run = compare.find_element(By.XPATH, ".//button[normalize-space()='Run comparison']")
        assert not run.is_enabled()
        checkbox = compare.find_element(
            By.XPATH,
            f".//label[contains(., '{ctx['agent_name']}')]//input[@type='checkbox']",
        )
        if not checkbox.is_selected():
            scroll(driver, checkbox)
            checkbox.click()
        click_button(driver, "Run comparison")
        wait_outcome(driver, "Select agents and a published benchmark version to compare identical runs.")
        assert_no_error(driver)
        ctx["edges_export"] = True

    def test_20_public_vote_comment_and_unpublished(self, driver, base_url, ctx):
        _require(ctx, "signed_in", "public")
        driver.get(f"{base_url}/public/leaderboards/{ctx['public_slug']}")
        wait_outcome(driver, ctx["public_title"])
        wait_outcome(driver, f"▲ Up ({ctx['up_count']})")
        post = wait(driver, 20).until(
            lambda d: d.find_element(By.XPATH, "//button[normalize-space()='Post comment']")
        )
        assert not post.is_enabled()

        form = driver.find_element(By.CSS_SELECTOR, "form.comment-form")
        comment = field(form, "Comment")
        driver.execute_script("arguments[0].removeAttribute('maxLength');", comment)
        fill(driver, form, "Display name", "Selenium Edge")
        fill(driver, form, "Comment", "E" * 2001)
        wait(driver, 10).until(
            lambda d: d.find_element(By.XPATH, "//button[normalize-space()='Post comment']").is_enabled()
        )
        click_button(driver, "Post comment")
        wait_error(driver, "Comment body must be at most 2000 characters")
        comments = driver.find_elements(By.CSS_SELECTOR, "article.comment-item")
        joined = "\n".join(item.text for item in comments)
        assert ctx["comment"] in joined
        assert "E" * 80 not in joined

        invalid_vote = browser_json(
            driver,
            "POST",
            f"/api/v1/public/leaderboards/{ctx['public_slug']}/votes",
            {"value": 0, "voterFingerprint": "seleniumedgevote"},
            token=False,
        )
        assert invalid_vote["status"] == 400, invalid_vote
        assert "Vote value must be 1 (up) or -1 (down)" in _detail(invalid_vote)
        short_fingerprint = browser_json(
            driver,
            "POST",
            f"/api/v1/public/leaderboards/{ctx['public_slug']}/votes",
            {"value": 1, "voterFingerprint": "short"},
            token=False,
        )
        assert short_fingerprint["status"] == 400, short_fingerprint
        assert "voterFingerprint must be 8–64" in _detail(short_fingerprint)

        driver.get(f"{base_url}/public/leaderboards/{ctx['public_slug']}")
        wait_outcome(driver, f"▲ Up ({ctx['up_count']})")
        assert f"Score {ctx['score']}" in driver.find_element(By.TAG_NAME, "body").text

        driver.get(f"{base_url}/public/leaderboards/{ctx['unpublished_slug']}")
        wait_error(driver, "Public leaderboard not found")
        body = driver.find_element(By.TAG_NAME, "body").text
        assert "Community" not in body
        assert ctx["unpublished_title"] not in body
        assert not driver.find_elements(By.CSS_SELECTOR, "form.comment-form")
        ctx["edges_public"] = True

    def test_21_logout_mid_flow_and_auth_errors(self, driver, base_url, ctx):
        _require(ctx, "shell")
        _home(driver, base_url, ctx["display_name"])
        open_tab(driver, "Projects", "Projects")
        form = form_by_heading(driver, "Create project")
        fill(driver, form, "Name", _MIDFLOW_PROJECT)
        _logout(driver)
        assert not _logged_in(driver)
        _login(driver, ctx["email"], ctx["password"], ctx["display_name"])
        open_tab(driver, "Projects", "Projects")
        assert _MIDFLOW_PROJECT not in driver.find_element(By.TAG_NAME, "body").text

        _logout(driver)
        login = _auth_form(driver)
        _assert_html_blocked(driver, login)
        assert "Sign in to manage organizations" in driver.find_element(By.TAG_NAME, "body").text

        fill(driver, login, "Email", ctx["email"])
        fill(driver, login, "Password", "wrongpass")
        _submit(driver, login)
        wait_error(driver, "Invalid credentials")
        assert not _logged_in(driver)

        click_button(driver, "Register")
        register = _auth_form(driver)
        fill(driver, register, "Display name", ctx["display_name"])
        fill(driver, register, "Email", ctx["email"])
        fill(driver, register, "Password", ctx["password"])
        _submit(driver, register)
        wait_error(driver, "Email already registered")

        password = field(register, "Password")
        driver.execute_script(
            "arguments[0].removeAttribute('minLength'); arguments[0].required = true;",
            password,
        )
        fill(driver, register, "Password", "short")
        _submit(driver, register)
        wait_error(driver, "password: size must be between 8 and 128")
        assert not _logged_in(driver)
        ctx["edges_auth"] = True

    def test_22_viewer_role_hidden_and_forbidden(self, driver, base_url, ctx, download_dir: Path):
        _require(ctx, "shell")
        driver.get(base_url + "/")
        wait(driver, 30).until(
            lambda d: "Loading session" not in d.find_element(By.TAG_NAME, "body").text
        )
        _login(driver, ctx["viewer_email"], ctx["viewer_password"], ctx["viewer_display_name"])
        wait_outcome(driver, ctx["org_name"])
        _click_named_item(driver, ctx["org_name"])

        open_tab(driver, "Projects", "Projects")
        wait_outcome(driver, "Your role: VIEWER")
        assert not driver.find_elements(By.XPATH, "//h3[normalize-space()='Create project']")
        assert ctx["project_name"] in driver.find_element(By.TAG_NAME, "body").text

        open_tab(driver, "Evaluations", "Evaluations")
        wait_outcome(driver, "Your role: VIEWER")
        assert not driver.find_elements(By.XPATH, "//h3[normalize-space()='Enqueue evaluation']")

        open_tab(driver, "Agents", "Agents")
        assert ctx["agent_name"] in driver.find_element(By.TAG_NAME, "body").text
        assert not driver.find_elements(By.XPATH, "//h3[normalize-space()='Register agent']")

        open_tab(driver, "SSO", "Enterprise SSO")
        wait_outcome(driver, "Only OWNER or ADMIN can manage SAML IdPs.")
        assert not driver.find_elements(By.XPATH, "//h3[normalize-space()='Add SAML IdP']")

        _open_benchmark(driver, ctx["benchmark_name"])
        _click_version(driver, ctx["version"])
        open_tab(driver, "Leaderboard", "Org leaderboard")
        wait_outcome(driver, "read-only for your role")
        wait_outcome(driver, ctx["public_slug"])
        assert not driver.find_elements(By.XPATH, "//button[normalize-space()='Unpublish']")
        assert not driver.find_elements(By.XPATH, "//button[normalize-space()='Publish publicly']")
        _assert_export(driver, download_dir, "Export CSV")

        _open_benchmark(driver, ctx["edge_benchmark_name"])
        _click_version(driver, ctx["draft_version"])
        wait_outcome(driver, ctx["draft_task_title"])
        _click_named_item(driver, ctx["draft_task_title"])
        wait_outcome(driver, "present (restricted)")
        body = driver.find_element(By.TAG_NAME, "body").text
        assert f"Visible suite: {ctx['visible_framework']}" in body
        hidden_at = body.find("Hidden suite:")
        assert hidden_at >= 0
        assert "present (restricted)" in body[hidden_at : hidden_at + 48]
        assert ctx["hidden_fixture"].strip() not in driver.page_source
        assert not driver.find_elements(By.XPATH, "//h3[normalize-space()='Hidden suite']")
        assert not driver.find_elements(By.XPATH, "//h3[normalize-space()='Add task']")

        denied = browser_json(
            driver,
            "POST",
            f"/api/v1/orgs/{ctx['org_id']}/flake-quarantines",
            {"caseKey": "selenium-viewer-denied", "reason": "viewer"},
        )
        assert denied["status"] == 403, denied
        assert "Insufficient role for this operation" in _detail(denied)
        assert_no_error(driver)
        ctx["edges_viewer"] = True
