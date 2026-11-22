"""Gaps that run after the ordered edge cases, still in one browser session.

Covers the owner SAML admin form, one server-side rejection on that form, the
published leaderboard embed control, and a second seeded organization. Edit,
disable, and delete stay skipped when Add SAML IdP does not store a row.
There is no GPU case.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select

from tests.test_regression import _click_named_item, _require
from tests.test_regression_edges import (
    _assert_export,
    _choose_select_text,
    _click_version,
    _detail,
    _login,
    _logout,
    _open_benchmark,
    _submit,
)
from tests.ui import (
    assert_no_error,
    browser_json,
    click_button,
    field,
    fill,
    form_by_heading,
    open_tab,
    react_set,
    visible_errors,
    wait,
    wait_error,
    wait_outcome,
)

_SAML_REGISTRATION = "selenium-mock-idp"
_SAML_NAME = "Selenium Mock IdP"
_SAML_EDITED = "Selenium Mock IdP edited"
_SAML_URL = "https://idp.example.invalid/metadata"
_SAML_BLOCKS = (
    "SAML SP is disabled",
    "SAML SP not configured",
    "Unable to load IdP metadata",
    "SAML IdP metadata is required",
)
_FILTER_QUERY = "selenium-filter-nomatch"
_SECRET_NAME = "Selenium Mock Secret"


def _secret_absent(driver, ctx) -> None:
    secret = str(ctx.get("hidden_fixture") or "").strip()
    if secret:
        assert secret not in driver.page_source


def _as_owner(driver, base_url: str, ctx) -> None:
    driver.get(base_url + "/")
    wait(driver, 30).until(
        lambda d: "Loading session" not in d.find_element(By.TAG_NAME, "body").text
    )
    _login(driver, ctx["email"], ctx["password"], ctx["display_name"])
    wait_outcome(driver, ctx["org_name"])
    _click_named_item(driver, ctx["org_name"])


def _org_list_text(driver) -> str:
    panels = driver.find_elements(
        By.XPATH,
        "//form[.//h3[normalize-space()='Create organization']]"
        "/ancestor::div[contains(@class,'panel')][1]",
    )
    if not panels:
        return ""
    parts: list[str] = []
    for element in panels[0].find_elements(By.CSS_SELECTOR, "button.list-item"):
        try:
            if element.is_displayed():
                parts.append(element.text)
        except StaleElementReferenceException:
            continue
    return "\n".join(parts)


def _idp_row_text(driver) -> str:
    for element in driver.find_elements(By.CSS_SELECTOR, "button.list-item"):
        try:
            if element.is_displayed() and _SAML_REGISTRATION in element.text:
                return element.text
        except StaleElementReferenceException:
            continue
    return ""


def _idp_listed(driver, registration_id: str) -> bool:
    for element in driver.find_elements(By.CSS_SELECTOR, "button.list-item"):
        try:
            if element.is_displayed() and registration_id in element.text:
                return True
        except StaleElementReferenceException:
            continue
    return False


def _consume_saml_response(driver) -> bool:
    try:
        entries = driver.get_log("performance")
    except Exception:
        return False
    for entry in entries:
        try:
            message = json.loads(entry["message"])["message"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if message.get("method") != "Network.responseReceived":
            continue
        response = message.get("params", {}).get("response", {})
        url = str(response.get("url", ""))
        status = response.get("status")
        if "/saml/idps" in url and isinstance(status, int) and status > 0:
            return True
    return False


def _open_sso(driver, registration_id: str) -> str:
    try:
        driver.get_log("performance")
    except Exception:
        pass
    open_tab(driver, "SSO", "Enterprise SSO")
    form_by_heading(driver, "Add SAML IdP")
    deadline = time.monotonic() + 20
    saw_response = False
    while time.monotonic() < deadline:
        if _consume_saml_response(driver):
            saw_response = True
            break
        if _idp_listed(driver, registration_id):
            return "present"
        time.sleep(0.2)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _idp_listed(driver, registration_id):
            return "present"
        body = driver.find_element(By.TAG_NAME, "body").text
        if saw_response and "No SAML IdPs configured for this organization." in body:
            return "empty"
        time.sleep(0.2)
    if _idp_listed(driver, registration_id):
        return "present"
    body = driver.find_element(By.TAG_NAME, "body").text
    if "No SAML IdPs configured for this organization." in body:
        return "empty"
    raise AssertionError("SAML IdP list did not settle")


def _panel(driver, heading_prefix: str):
    return driver.find_element(
        By.XPATH,
        f"//h2[starts-with(normalize-space(), '{heading_prefix}')]/ancestor::*[contains(@class,'panel')][1]",
    )


def _eval_rows(driver) -> list[str]:
    panel = _panel(driver, "Evaluations")
    rows: list[str] = []
    for element in panel.find_elements(By.CSS_SELECTOR, "ul.list button.list-item"):
        try:
            if element.is_displayed():
                rows.append(element.text)
        except StaleElementReferenceException:
            continue
    return rows


def _wait_search_param(driver, needle: str, started: float, min_delay: float) -> float:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if _saw_request(driver, needle):
            elapsed = time.monotonic() - started
            if elapsed < min_delay:
                raise AssertionError(
                    f"Evaluation search sent {needle} after {elapsed:.3f}s, before the 250 ms debounce"
                )
            return elapsed
        errors = visible_errors(driver)
        if errors:
            raise AssertionError("Evaluation filter failed: " + " | ".join(errors))
        time.sleep(0.05)
    raise AssertionError(f"Evaluation search did not send {needle}")


def _saw_request(driver, needle: str) -> bool:
    try:
        entries = driver.get_log("performance")
    except Exception:
        return False
    found = False
    for entry in entries:
        try:
            message = json.loads(entry["message"])["message"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if message.get("method") != "Network.responseReceived":
            continue
        url = str(message.get("params", {}).get("response", {}).get("url", ""))
        if needle in url:
            found = True
    return found


def _click_vote(driver, marker: str) -> None:
    def find(d):
        for element in d.find_elements(By.TAG_NAME, "button"):
            try:
                if element.is_displayed() and element.is_enabled() and element.text.strip().startswith(marker):
                    return element
            except StaleElementReferenceException:
                return False
        return False

    button = wait(driver, 20).until(find)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
    button.click()


def _assert_not_member(driver, org_id: str) -> None:
    denied = browser_json(driver, "GET", f"/api/v1/orgs/{org_id}/projects")
    assert denied["status"] == 403, denied
    assert "Not a member of this organization" in _detail(denied)


class TestAgentBenchGaps:
    def test_23_owner_saml_admin_authorized(self, driver, base_url, ctx):
        """Owner is allowed onto the SAML admin form. A 403 would be a failure."""
        _require(ctx, "shell", "org_id")
        _as_owner(driver, base_url, ctx)
        state = _open_sso(driver, _SAML_REGISTRATION)
        body = driver.find_element(By.TAG_NAME, "body").text
        assert "Only OWNER or ADMIN can manage SAML IdPs." not in body
        assert "Add SAML IdP" in body
        if state == "present":
            ctx["saml_idp_created"] = True
            ctx["saml_block_reason"] = ""
        else:
            form = form_by_heading(driver, "Add SAML IdP")
            fill(driver, form, "Registration id", _SAML_REGISTRATION)
            fill(driver, form, "Display name", _SAML_NAME)
            fill(driver, form, "Metadata URL", _SAML_URL)
            _submit(driver, form)

            def outcome(d):
                if _idp_listed(d, _SAML_REGISTRATION):
                    return ("created", None)
                errors = visible_errors(d)
                if errors:
                    return ("error", errors)
                return False

            kind, errors = wait(driver, 30).until(outcome)
            if kind == "created":
                ctx["saml_idp_created"] = True
                ctx["saml_block_reason"] = ""
                assert_no_error(driver)
            else:
                message = " | ".join(errors)
                assert "Insufficient role" not in message
                assert "Not a member of this organization" not in message
                assert "Access denied" not in message
                if "already exists" in message:
                    if not _idp_listed(driver, _SAML_REGISTRATION):
                        raise AssertionError(
                            "SAML registration id already exists but is not listed for this org: "
                            + message
                        )
                    ctx["saml_idp_created"] = True
                    ctx["saml_block_reason"] = ""
                elif any(marker in message for marker in _SAML_BLOCKS):
                    assert not _idp_listed(driver, _SAML_REGISTRATION)
                    ctx["saml_idp_created"] = False
                    ctx["saml_block_reason"] = message
                else:
                    raise AssertionError("Unexpected SAML admin result: " + message)
        _secret_absent(driver, ctx)
        ctx["saml_admin"] = True

    def test_24_saml_edit_disable_delete(self, driver, base_url, ctx):
        _require(ctx, "saml_admin")
        if not ctx.get("saml_idp_created"):
            reason = ctx.get("saml_block_reason") or "Add SAML IdP did not store a row"
            pytest.skip(
                "Disable, Save IdP, and Delete are not rendered until an IdP exists. " + reason
            )
        _as_owner(driver, base_url, ctx)
        assert _open_sso(driver, _SAML_REGISTRATION) == "present"
        _click_named_item(driver, _SAML_REGISTRATION)
        edit = form_by_heading(driver, "Edit IdP")
        fill(driver, edit, "Display name", _SAML_EDITED)
        _submit(driver, edit)
        wait_outcome(driver, _SAML_EDITED)
        if "disabled" not in _idp_row_text(driver).lower():
            click_button(driver, "Disable")
            wait_outcome(driver, "disabled")
        click_button(driver, "Delete")

        def gone(d):
            return not _idp_listed(d, _SAML_REGISTRATION)

        wait(driver, 20).until(gone)
        assert_no_error(driver)
        _secret_absent(driver, ctx)
        ctx["saml_idp_deleted"] = True

    def test_25_saml_registration_id_rejected(self, driver, base_url, ctx):
        """Server rejects a registration id that is not lowercase kebab-case."""
        _require(ctx, "shell")
        _as_owner(driver, base_url, ctx)
        _open_sso(driver, _SAML_REGISTRATION)
        form = form_by_heading(driver, "Add SAML IdP")
        registration = field(form, "Registration id")
        driver.execute_script("arguments[0].removeAttribute('pattern');", registration)
        fill(driver, form, "Registration id", "Bad_Id")
        fill(driver, form, "Display name", "Rejected IdP")
        _submit(driver, form)
        wait_error(driver, "registrationId must be lowercase kebab-case")
        assert not _idp_listed(driver, "Bad_Id")
        listed = [element.text for element in driver.find_elements(By.CSS_SELECTOR, "button.list-item")]
        assert not any("Rejected IdP" in item for item in listed)
        _secret_absent(driver, ctx)
        ctx["saml_rejected"] = True

    def test_26_published_leaderboard_embed(self, driver, base_url, ctx):
        _require(ctx, "shell", "public_slug", "public_title")
        _as_owner(driver, base_url, ctx)
        _open_benchmark(driver, ctx["benchmark_name"])
        _click_version(driver, ctx["version"])
        open_tab(driver, "Leaderboard", "Org leaderboard")
        wait(driver, 30).until(
            lambda d: d.find_elements(
                By.CSS_SELECTOR, f"a[href*='/public/leaderboards/{ctx['public_slug']}']"
            )
        )
        click_button(driver, "Copy embed snippet")

        def snippet_ready(d):
            errors = visible_errors(d)
            if errors:
                return ("error", " | ".join(errors))
            for box in d.find_elements(By.CSS_SELECTOR, "label.embed-snippet textarea"):
                value = box.get_attribute("value") or ""
                if "/embed?token=" in value:
                    return ("ok", value)
            return False

        kind, payload = wait(driver, 30).until(snippet_ready)
        assert kind == "ok", payload
        snippet = payload
        body = driver.find_element(By.TAG_NAME, "body").text.lower()
        assert "copied iframe snippet" in body or "embed ready" in body
        match = re.search(r'src="([^"]+)"', snippet)
        assert match, snippet
        src = match.group(1)
        assert f"/public/leaderboards/{ctx['public_slug']}/embed?token=" in src
        assert "format=html" in src
        secret = str(ctx.get("hidden_fixture") or "").strip()
        if secret:
            assert secret not in snippet
        if src.startswith("/"):
            src = base_url + src
        driver.get(src)
        wait(driver, 30).until(
            lambda d: ctx["public_title"] in d.find_element(By.TAG_NAME, "body").text
        )
        page = driver.find_element(By.TAG_NAME, "body").text
        assert ctx["comment"] in page
        assert ctx["benchmark_name"] in page
        assert "Post comment" not in page
        _secret_absent(driver, ctx)
        ctx["embed"] = True

    def test_27_other_org_is_hidden(self, driver, base_url, ctx):
        """The mock owner and the other org's owner cannot see each other's workspace."""
        _require(ctx, "shell", "org_id", "tenant_org_id", "tenant_email")
        _as_owner(driver, base_url, ctx)
        owner_orgs = _org_list_text(driver)
        assert ctx["org_name"] in owner_orgs
        assert ctx["tenant_org_name"] not in owner_orgs
        assert ctx["tenant_org_slug"] not in owner_orgs
        assert ctx["tenant_project_description"] not in driver.find_element(By.TAG_NAME, "body").text
        _assert_not_member(driver, ctx["tenant_org_id"])

        _login(driver, ctx["tenant_email"], ctx["tenant_password"], ctx["tenant_display_name"])
        wait_outcome(driver, ctx["tenant_org_name"])
        tenant_orgs = _org_list_text(driver)
        assert ctx["tenant_org_name"] in tenant_orgs
        assert ctx["org_name"] not in tenant_orgs
        assert ctx["org_slug"] not in tenant_orgs
        _click_named_item(driver, ctx["tenant_org_name"])
        open_tab(driver, "Projects", "Projects")
        wait_outcome(driver, ctx["tenant_project_description"])
        body = driver.find_element(By.TAG_NAME, "body").text
        assert ctx["project_name"] not in body
        assert ctx["project_description"] not in body
        _assert_not_member(driver, ctx["org_id"])
        _secret_absent(driver, ctx)
        ctx["tenant_isolated"] = True

    def test_28_export_pdf(self, driver, base_url, ctx, download_dir: Path):
        _require(ctx, "shell", "org_id")
        _as_owner(driver, base_url, ctx)
        _open_benchmark(driver, ctx["benchmark_name"])
        _click_version(driver, ctx["version"])
        open_tab(driver, "Leaderboard", "Org leaderboard")
        wait(driver, 30).until(
            lambda d: (
                "No completed scored evaluations" in d.find_element(By.TAG_NAME, "body").text
                or "Profile " in d.find_element(By.TAG_NAME, "body").text
            )
        )
        _assert_export(driver, download_dir, "Export PDF")

        open_tab(driver, "Evaluations", "Evaluations")
        if not driver.find_elements(By.XPATH, "//h2[normalize-space()='Evaluation detail']"):
            form = form_by_heading(driver, "Enqueue evaluation")
            version_id = field(form, "Published version ID").get_attribute("value") or ""
            task_id = field(form, "Task ID").get_attribute("value") or ""
            assert len(version_id) >= 32, f"Published version ID was not filled ({version_id!r})"
            assert len(task_id) >= 32, f"Task ID was not filled ({task_id!r})"
            agent = Select(field(form, "Agent"))
            matching = [option.text for option in agent.options if ctx["agent_name"] in option.text]
            assert matching, "FAKE agent was not offered on the evaluation form"
            agent.select_by_visible_text(matching[0])
            _submit(driver, form)
            wait_outcome(driver, "Evaluation detail", timeout=60)
        assert driver.find_elements(By.XPATH, "//button[normalize-space()='Export PDF']"), (
            "Evaluation detail does not render Export PDF"
        )
        _assert_export(driver, download_dir, "Export PDF")
        _secret_absent(driver, ctx)
        ctx["pdf"] = True

    def test_29_list_filters(self, driver, base_url, ctx):
        _require(ctx, "shell", "regression_suite", "regression_passed_case")
        _as_owner(driver, base_url, ctx)
        open_tab(driver, "Evaluations", "Evaluations")
        panel = _panel(driver, "Evaluations")
        try:
            driver.get_log("performance")
        except Exception:
            pass
        started = time.monotonic()
        react_set(driver, field(panel, "Search"), _FILTER_QUERY)
        seen = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if _saw_request(driver, "q=" + _FILTER_QUERY):
                seen = time.monotonic() - started
                break
            time.sleep(0.05)
        assert seen is not None, "Evaluation search did not send the query"
        assert seen >= 0.2, f"Evaluation search fired after {seen:.3f}s, before the 250 ms debounce"
        wait_outcome(driver, "No evaluations match these filters")
        _choose_select_text(driver, field(panel, "State"), "CANCELLED")
        wait_outcome(driver, "No evaluations match these filters")
        click_button(driver, "Clear")
        wait(driver, 10).until(lambda d: field(_panel(d, "Evaluations"), "Search").get_attribute("value") == "")

        _open_benchmark(driver, ctx["benchmark_name"])
        _click_version(driver, ctx["version"])
        open_tab(driver, "Leaderboard", "Org leaderboard")
        fill(driver, _panel(driver, "Org leaderboard"), "Search", _FILTER_QUERY)
        wait_outcome(driver, "No leaderboard rows match these filters")

        open_tab(driver, "Compare", "Model comparisons")
        fill(driver, _panel(driver, "Model comparisons"), "Search", _FILTER_QUERY)
        wait_outcome(driver, "No comparison rows match these filters")
        task_select = field(_panel(driver, "Model comparisons"), "Task")
        slugs = [option.text for option in Select(task_select).options if option.text == ctx["task_slug"]]
        assert slugs, "Comparison task filter did not list the seeded task"
        _choose_select_text(driver, task_select, ctx["task_slug"])

        open_tab(driver, "Regression runs", "Regression runs")
        runs = _panel(driver, "Regression runs")
        _choose_select_text(driver, field(runs, "Outcome"), "Failed")
        wait_outcome(driver, ctx["regression_suite"])
        _choose_select_text(driver, field(_panel(driver, "Regression runs"), "Outcome"), "Passed")
        wait_outcome(driver, "No regression runs match these filters")
        assert ctx["regression_suite"] not in _panel(driver, "Regression runs").text
        _secret_absent(driver, ctx)
        ctx["filters"] = True

    def test_30_costs_empty(self, driver, base_url, ctx):
        _require(ctx, "shell")
        _as_owner(driver, base_url, ctx)
        open_tab(driver, "Costs", "Cost analytics")
        wait_outcome(driver, "No token or cost records yet")
        fill(driver, _panel(driver, "Cost analytics"), "Search", _FILTER_QUERY)
        wait_outcome(driver, "No cost rows match these filters")
        click_button(driver, "Clear")
        wait_outcome(driver, "No token or cost records yet")
        assert_no_error(driver)
        _secret_absent(driver, ctx)
        ctx["costs"] = True

    def test_31_secret_and_fake_connection(self, driver, base_url, ctx):
        _require(ctx, "shell", "agent_name")
        _as_owner(driver, base_url, ctx)
        open_tab(driver, "Agents", "Agents")
        form = form_by_heading(driver, "Store agent API key")
        fill(driver, form, "Secret name", _SECRET_NAME)
        fill(driver, form, "Plaintext (write-only)", "not-a-provider-key")
        _submit(driver, form)

        def stored(d):
            errors = visible_errors(d)
            if any("Secret name already exists" in error for error in errors):
                return "exists"
            options = d.find_elements(By.XPATH, f"//option[contains(., '{_SECRET_NAME}')]")
            if options:
                return "created"
            return False

        state = wait(driver, 30).until(stored)
        if state == "created":
            assert_no_error(driver)
        assert driver.find_elements(By.XPATH, f"//option[contains(., '{_SECRET_NAME}')]")
        _click_named_item(driver, ctx["agent_name"])
        click_button(driver, "Test connection")
        alert = wait(driver, 20).until(lambda d: d.switch_to.alert)
        message = alert.text
        alert.accept()
        assert "FAKE" in message
        assert "Fake provider ready" in message
        _secret_absent(driver, ctx)
        ctx["secret"] = True

    def test_32_theme_toggle(self, driver, base_url, ctx):
        _require(ctx, "shell", "public_slug")
        _as_owner(driver, base_url, ctx)

        def theme(d):
            return d.execute_script(
                "return [document.documentElement.getAttribute('data-theme'), "
                "localStorage.getItem('agentbench.theme')];"
            )

        click_button(driver, "Dark")
        wait(driver, 10).until(lambda d: theme(d) == ["dark", "dark"])
        click_button(driver, "Light")
        wait(driver, 10).until(lambda d: theme(d) == ["light", "light"])
        driver.get(base_url + f"/public/leaderboards/{ctx['public_slug']}")
        wait_outcome(driver, ctx["public_title"])
        click_button(driver, "Dark")
        wait(driver, 10).until(lambda d: theme(d) == ["dark", "dark"])
        click_button(driver, "Light")
        wait(driver, 10).until(lambda d: theme(d) == ["light", "light"])
        _secret_absent(driver, ctx)
        ctx["theme"] = True

    def test_33_public_vote_click(self, driver, base_url, ctx):
        _require(ctx, "shell", "public_slug", "vote_click_fingerprint", "up_count")
        driver.get(base_url + "/")
        wait(driver, 30).until(
            lambda d: "Loading session" not in d.find_element(By.TAG_NAME, "body").text
        )
        _logout(driver)
        driver.execute_script(
            "localStorage.setItem('agentbench.publicVoterFingerprint', arguments[0]);",
            ctx["vote_click_fingerprint"],
        )
        driver.get(base_url + f"/public/leaderboards/{ctx['public_slug']}")
        wait_outcome(driver, f"▲ Up ({ctx['up_count']})")
        try:
            _click_vote(driver, "▼ Down")
            wait_outcome(driver, f"▲ Up ({ctx['up_count'] - 1})")
            wait_outcome(driver, "▼ Down (1)")
        finally:
            if "▼ Down (1)" in driver.find_element(By.TAG_NAME, "body").text:
                _click_vote(driver, "▲ Up")
                wait_outcome(driver, f"▲ Up ({ctx['up_count']})")
                wait_outcome(driver, "▼ Down (0)")
        _secret_absent(driver, ctx)
        ctx["vote_click"] = True

    def test_34_developer_role(self, driver, base_url, ctx):
        _require(ctx, "shell", "developer_email", "org_id")
        driver.get(base_url + "/")
        wait(driver, 30).until(
            lambda d: "Loading session" not in d.find_element(By.TAG_NAME, "body").text
        )
        _login(driver, ctx["developer_email"], ctx["developer_password"], ctx["developer_display_name"])
        wait_outcome(driver, ctx["org_name"])
        _click_named_item(driver, ctx["org_name"])
        open_tab(driver, "Projects", "Projects")
        wait_outcome(driver, "Your role: DEVELOPER")
        assert driver.find_elements(By.XPATH, "//h3[normalize-space()='Create project']")
        assert not driver.find_elements(By.XPATH, "//button[contains(., 'Delete project')]")
        open_tab(driver, "Evaluations", "Evaluations")
        assert driver.find_elements(By.XPATH, "//h3[normalize-space()='Enqueue evaluation']")
        open_tab(driver, "Agents", "Agents")
        assert driver.find_elements(By.XPATH, "//h3[normalize-space()='Register agent']")
        open_tab(driver, "SSO", "Enterprise SSO")
        wait_outcome(driver, "Only OWNER or ADMIN can manage SAML IdPs.")
        assert not driver.find_elements(By.XPATH, "//h3[normalize-space()='Add SAML IdP']")
        _open_benchmark(driver, ctx["benchmark_name"])
        _click_version(driver, ctx["version"])
        open_tab(driver, "Leaderboard", "Org leaderboard")
        wait_outcome(driver, "read-only for your role")
        assert not driver.find_elements(By.XPATH, "//button[normalize-space()='Unpublish']")
        _open_benchmark(driver, ctx["edge_benchmark_name"])
        _click_version(driver, ctx["draft_version"])
        _click_named_item(driver, ctx["draft_task_title"])
        wait_outcome(driver, "Hidden suite")
        assert ctx["hidden_fixture"].strip() not in driver.page_source
        denied = browser_json(
            driver,
            "POST",
            f"/api/v1/orgs/{ctx['org_id']}/flake-quarantines",
            {"caseKey": "selenium-developer-denied", "reason": "developer"},
        )
        assert denied["status"] == 403, denied
        assert "Insufficient role for this operation" in _detail(denied)
        _secret_absent(driver, ctx)
        ctx["developer"] = True

    def test_35_oidc_hash_consumed(self, driver, base_url, ctx):
        _require(ctx, "shell")
        # A hash-only change does not remount the app, so the parser would not run.
        driver.get("about:blank")
        driver.get(
            base_url
            + "/#access_token=seleniumfragmenttoken&refresh_token=seleniumfragmentrefresh&token_type=Bearer&expires_in=900"
        )
        wait(driver, 30).until(lambda d: d.execute_script("return window.location.hash") == "")
        wait_outcome(driver, "Sign in to manage organizations")
        stored = driver.execute_script("return localStorage.getItem('agentbench.accessToken')")
        assert not stored
        assert "seleniumfragmenttoken" not in driver.page_source
        ctx["oidc_hash"] = True

    def test_36_members_screen_absent(self, driver, base_url, ctx):
        _require(ctx, "shell")
        _as_owner(driver, base_url, ctx)
        tabs = [element.text.strip() for element in driver.find_elements(By.CSS_SELECTOR, "button.tab")]
        assert "Members" not in tabs
        pytest.skip("Members controls are not in the UI, so there is nothing to click.")

    def test_37_repository_form_absent(self, driver, base_url, ctx):
        _require(ctx, "shell")
        _as_owner(driver, base_url, ctx)
        open_tab(driver, "Projects", "Projects")
        assert not driver.find_elements(By.XPATH, "//h3[contains(., 'Repository')]")
        assert not driver.find_elements(By.XPATH, "//button[contains(., 'Snapshot')]")
        pytest.skip("Repository and snapshot forms are not in the UI, so there is nothing to click.")

    def _ensure_evaluation_row(self, driver, ctx) -> None:
        open_tab(driver, "Evaluations", "Evaluations")
        wait(driver, 30).until(
            lambda d: "Loading evaluations" not in _panel(d, "Evaluations").text
        )
        if _eval_rows(driver):
            return
        _open_benchmark(driver, ctx["benchmark_name"])
        _click_version(driver, ctx["version"])
        open_tab(driver, "Evaluations", "Evaluations")
        form = form_by_heading(driver, "Enqueue evaluation")
        version_id = field(form, "Published version ID").get_attribute("value") or ""
        task_id = field(form, "Task ID").get_attribute("value") or ""
        assert len(version_id) >= 32 and len(task_id) >= 32
        agent = Select(field(form, "Agent"))
        matching = [option.text for option in agent.options if ctx["agent_name"] in option.text]
        assert matching, "FAKE agent was not offered on the evaluation form"
        agent.select_by_visible_text(matching[0])
        _submit(driver, form)
        wait(driver, 30).until(lambda d: _eval_rows(d))

    def _apply_eval_filter(self, driver, apply, needle: str, min_delay: float) -> None:
        try:
            driver.get_log("performance")
        except Exception:
            pass
        started = time.monotonic()
        apply()
        _wait_search_param(driver, needle, started, min_delay)
        wait(driver, 15).until(
            lambda d: "No evaluations match these filters" in _panel(d, "Evaluations").text
            and not _eval_rows(d)
        )

    def _clear_eval_filters(self, driver) -> None:
        click_button(driver, "Clear")
        wait(driver, 15).until(
            lambda d: _eval_rows(d)
            and "No evaluations match these filters" not in _panel(d, "Evaluations").text
        )

    def test_38_evaluation_bound_filters(self, driver, base_url, ctx):
        """Failure class, score bounds, and created-time bounds from the evaluation list."""
        _require(ctx, "shell")
        _as_owner(driver, base_url, ctx)
        self._ensure_evaluation_row(driver, ctx)
        panel = lambda: _panel(driver, "Evaluations")  # noqa: E731

        self._apply_eval_filter(
            driver,
            lambda: _choose_select_text(driver, field(panel(), "Failure class"), "AGENT"),
            "failureClass=AGENT",
            0,
        )
        self._clear_eval_filters(driver)

        self._apply_eval_filter(
            driver,
            lambda: react_set(driver, field(panel(), "Min score"), "99"),
            "minScore=99",
            0.2,
        )
        self._clear_eval_filters(driver)

        self._apply_eval_filter(
            driver,
            lambda: react_set(driver, field(panel(), "Max score"), "0"),
            "maxScore=0",
            0.2,
        )
        self._clear_eval_filters(driver)

        self._apply_eval_filter(
            driver,
            lambda: react_set(driver, field(panel(), "Created from"), "2099-01-01T00:00"),
            "createdFrom=2099",
            0,
        )
        self._clear_eval_filters(driver)

        self._apply_eval_filter(
            driver,
            lambda: react_set(driver, field(panel(), "Created to"), "2000-01-01T00:00"),
            "createdTo=2000",
            0,
        )
        self._clear_eval_filters(driver)
        _choose_select_text(driver, field(panel(), "Failure class"), "No failure class")
        wait(driver, 15).until(lambda d: _eval_rows(d))
        assert "No evaluations match these filters" not in panel().text
        click_button(driver, "Clear")
        _secret_absent(driver, ctx)
        ctx["eval_bounds"] = True

    def test_39_failed_cases_only(self, driver, base_url, ctx):
        _require(ctx, "shell")
        _as_owner(driver, base_url, ctx)
        open_tab(driver, "Evaluations", "Evaluations")
        wait(driver, 30).until(lambda d: "Loading evaluations" not in _panel(d, "Evaluations").text)
        rows = _panel(driver, "Evaluations").find_elements(By.CSS_SELECTOR, "ul.list button.list-item")
        if not rows:
            pytest.skip("Failed cases only is not rendered until an evaluation has test runs.")
        rows[0].click()
        wait(driver, 20).until(
            lambda d: d.find_elements(By.XPATH, "//h2[normalize-space()='Evaluation detail']")
        )
        if not driver.find_elements(By.XPATH, "//label[contains(., 'Failed cases only')]"):
            pytest.skip(
                "Failed cases only is not rendered until an evaluation has test runs."
            )
        box = driver.find_element(By.XPATH, "//label[contains(., 'Failed cases only')]//input")
        box.click()

        def case_lines(d):
            return [
                item.text
                for item in d.find_elements(By.CSS_SELECTOR, "ul.list.plain ul li")
                if item.is_displayed() and item.text.strip()
            ]

        lines = wait(driver, 10).until(
            lambda d: case_lines(d)
            and all(": PASS" not in line and ": SKIP" not in line for line in case_lines(d))
        )
        assert lines
        ctx["failed_cases_only"] = True
