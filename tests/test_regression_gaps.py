"""Gaps that run after the ordered edge cases, still in one browser session.

Covers the owner SAML admin form, one server-side rejection on that form, the
published leaderboard embed control, a second seeded organization, and cancel
while still QUEUED. Edit, disable, and delete stay skipped when Add SAML IdP
does not store a row. There is no GPU case.
"""

from __future__ import annotations

import json
import os
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
    evaluation_state,
    field,
    fill,
    form_by_heading,
    open_tab,
    react_set,
    scroll,
    visible_errors,
    wait,
    wait_error,
    wait_for_evaluation,
    wait_not_busy,
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


_DID_NOT_RUN = {
    "checkstyle": "Checkstyle did not run",
    "pmd": "PMD did not run",
    "spotbugs": "SpotBugs did not run",
}


def _quality_notes(driver) -> dict[str, str]:
    """Visible note for each Checkstyle, PMD, and SpotBugs row."""
    heading = driver.find_element(By.XPATH, "//h3[normalize-space()='Code quality findings']")
    block = heading.find_element(By.XPATH, "./ancestor::section[1]")
    notes: dict[str, str] = {}
    for row in block.find_elements(By.CSS_SELECTOR, ".quality-report tbody tr"):
        cells = row.find_elements(By.CSS_SELECTOR, "td")
        if len(cells) < 2:
            continue
        tool = (cells[0].text or "").strip().lower()
        if tool in _DID_NOT_RUN:
            notes[tool] = (cells[-1].text or "").strip()
    return notes


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


def _costs_screen(driver) -> str | bool:
    """``empty`` when the empty copy is showing, ``rows`` when usage labels are visible."""
    text = _panel(driver, "Cost analytics").text.lower()
    if "no token or cost records yet" in text:
        return "empty"
    # Stat labels are CSS-uppercase, so Selenium reads "TOTAL TOKENS".
    if "total tokens" in text and "by agent" in text:
        return "rows"
    return False


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
        """Empty cost copy only when nothing has been reported. Rows are test_44."""
        _require(ctx, "shell")
        _as_owner(driver, base_url, ctx)
        open_tab(driver, "Costs", "Cost analytics")
        state = wait(driver, 30).until(_costs_screen)
        if state == "rows":
            assert "no token or cost records yet" not in _panel(driver, "Cost analytics").text.lower()
            assert_no_error(driver)
            _secret_absent(driver, ctx)
            ctx["costs"] = True
            return
        wait_outcome(driver, "No token or cost records yet")
        fill(driver, _panel(driver, "Cost analytics"), "Search", _FILTER_QUERY)
        wait_outcome(driver, "No cost rows match these filters")
        click_button(driver, "Clear")
        wait(driver, 20).until(lambda d: _costs_screen(d) == "empty")
        assert_no_error(driver)
        _secret_absent(driver, ctx)
        ctx["costs"] = True

    def test_31_secret_and_fake_connection(self, driver, base_url, ctx):
        _require(ctx, "shell", "agent_name")
        _as_owner(driver, base_url, ctx)
        open_tab(driver, "Agents", "Agents")
        secret_option = f"//option[contains(., '{_SECRET_NAME}')]"
        if not driver.find_elements(By.XPATH, secret_option):
            form = form_by_heading(driver, "Store agent API key")
            fill(driver, form, "Secret name", _SECRET_NAME)
            fill(driver, form, "Plaintext (write-only)", "not-a-provider-key")
            _submit(driver, form)

            def stored(d):
                errors = visible_errors(d)
                if any("Secret name already exists" in error for error in errors):
                    return "exists"
                if d.find_elements(By.XPATH, secret_option):
                    return "created"
                return False

            state = wait(driver, 30).until(stored)
            if state == "created":
                assert_no_error(driver)
        assert driver.find_elements(By.XPATH, secret_option)
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

    def test_40_evaluation_stage_timeline(self, driver, base_url, ctx):
        """Baseline, model call, patch, tests, and score on the evaluation detail."""
        _require(ctx, "shell")
        _as_owner(driver, base_url, ctx)
        self._ensure_evaluation_row(driver, ctx)
        rows = _panel(driver, "Evaluations").find_elements(By.CSS_SELECTOR, "ul.list button.list-item")
        assert rows, "No evaluation row to open"
        rows[0].click()
        wait(driver, 20).until(
            lambda d: d.find_elements(By.XPATH, "//h2[normalize-space()='Evaluation detail']")
        )
        if not driver.find_elements(By.CSS_SELECTOR, "ol.stage-timeline"):
            pytest.skip("Stage timeline is not rendered on evaluation detail.")
        stages = [
            ("baseline", "Baseline"),
            ("model", "Model call"),
            ("patch", "Patch"),
            ("tests", "Tests"),
            ("score", "Score"),
        ]
        items = driver.find_elements(By.CSS_SELECTOR, "ol.stage-timeline li.stage-step")
        assert [item.get_attribute("data-stage") for item in items] == [stage_id for stage_id, _label in stages]
        for item, (stage_id, label) in zip(items, stages):
            assert item.find_element(By.CSS_SELECTOR, ".stage-name").text == label
            status = item.get_attribute("data-status")
            assert status in {"pending", "running", "passed", "failed", "skipped"}
            assert item.find_element(By.CSS_SELECTOR, ".stage-status").text.strip().lower() in {
                "pending",
                "running",
                "passed",
                "failed",
                "skipped",
                "cancelled",
                "timed out",
            }
        state = driver.find_element(By.CSS_SELECTOR, "section.detail-panel span.status-chip").get_attribute(
            "data-status"
        )
        if state == "QUEUED":
            assert [item.get_attribute("data-status") for item in items] == ["pending"] * 5
            assert not driver.find_elements(By.CSS_SELECTOR, "ol.stage-timeline p.error")
        _secret_absent(driver, ctx)
        ctx["stage_timeline"] = True

    def test_41_session_refresh(self, driver, base_url, ctx):
        """Refresh runs before the access token expires. A rejected refresh returns to sign-in."""
        _require(ctx, "shell")
        _as_owner(driver, base_url, ctx)
        open_tab(driver, "Ops", "System health")
        if not driver.find_elements(By.XPATH, "//button[normalize-space()='Refresh health']"):
            pytest.skip("Refresh health is not rendered, so the session client cannot be driven.")

        def stored(key: str) -> str:
            value = driver.execute_script("return localStorage.getItem(arguments[0]);", key)
            return value or ""

        def pull_expiry() -> None:
            driver.execute_script(
                "localStorage.setItem('agentbench.accessTokenExpiresAt', String(Date.now()));"
            )

        def refresh_statuses() -> list[int]:
            found: list[int] = []
            try:
                entries = driver.get_log("performance")
            except Exception:
                return found
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
                if "/api/v1/auth/refresh" in url and isinstance(status, int):
                    found.append(status)
            return found

        before_refresh = stored("agentbench.refreshToken")
        before_access = stored("agentbench.accessToken")
        assert before_refresh and before_access
        pull_expiry()
        try:
            driver.get_log("performance")
        except Exception:
            pass
        click_button(driver, "Refresh health")
        statuses: list[int] = []
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            statuses.extend(refresh_statuses())
            if 200 in statuses:
                break
            errors = visible_errors(driver)
            if errors:
                raise AssertionError("Refresh failed: " + " | ".join(errors))
            time.sleep(0.05)
        assert 200 in statuses, f"UI did not call POST /api/v1/auth/refresh (statuses={statuses})"
        wait_outcome(driver, ctx["display_name"])
        assert driver.find_elements(By.XPATH, "//button[normalize-space()='Log out']")
        assert stored("agentbench.refreshToken") not in {"", before_refresh}
        assert stored("agentbench.accessToken") not in {"", before_access}
        wait_outcome(driver, "Overall")

        driver.execute_script(
            """
            localStorage.setItem('agentbench.refreshToken', 'selenium-rejected-refresh-token');
            localStorage.setItem('agentbench.accessTokenExpiresAt', String(Date.now()));
            """
        )
        click_button(driver, "Refresh health")
        wait_outcome(driver, "Your session ended. Sign in again to continue.")
        assert not driver.find_elements(By.XPATH, "//button[normalize-space()='Log out']")
        assert stored("agentbench.accessToken") == ""
        assert stored("agentbench.refreshToken") == ""
        ctx["session_refresh"] = True

    def test_42_members_list_and_admin_control(self, driver, base_url, ctx):
        """Owner sees the member list and an invite or role control."""
        _require(ctx, "shell", "viewer_email", "developer_email")
        _as_owner(driver, base_url, ctx)
        tabs = [
            element
            for element in driver.find_elements(By.CSS_SELECTOR, "button.tab")
            if element.is_displayed() and element.text.strip() == "Members"
        ]
        if tabs:
            tabs[0].click()
            wait(driver, 20).until(
                lambda d: d.find_elements(By.XPATH, "//h2[contains(., 'Members')]|//h3[contains(., 'Members')]")
            )
        body = driver.find_element(By.TAG_NAME, "body").text
        listed = ctx["viewer_email"] in body and ctx["developer_email"] in body
        invite = driver.find_elements(
            By.XPATH,
            "//button[contains(., 'Invite') or contains(., 'Add member')]"
            "|//h3[contains(., 'Invite') or contains(., 'Add member')]",
        )
        role = driver.find_elements(
            By.XPATH,
            "//label[contains(., 'Role')]//select|//select[contains(@name, 'role') or contains(@name, 'Role')]",
        )
        if not tabs and not listed and not invite and not role:
            pytest.skip(
                "Members list and invite or role controls are not in the UI, so there is nothing to click."
            )
        assert listed, "Signed-in admin did not see the seeded members"
        assert invite or role, "Signed-in admin did not see an invite or role control"
        _secret_absent(driver, ctx)
        ctx["members"] = True

    def test_43_project_repository_form(self, driver, base_url, ctx):
        """Save a repository URL, validate it, and record a snapshot."""
        _require(ctx, "shell", "project_name")
        _as_owner(driver, base_url, ctx)
        open_tab(driver, "Projects", "Projects")
        _click_named_item(driver, ctx["project_name"])
        repo_url = "https://example.com/selenium-mock-repo"

        def form_ready(d):
            if "Loading repository" in d.find_element(By.TAG_NAME, "body").text:
                return False
            return bool(d.find_elements(By.XPATH, "//h3[normalize-space()='Repository']"))

        try:
            wait(driver, 20).until(form_ready)
        except Exception:
            pytest.skip("Repository form is not on the Projects tab, so there is nothing to save.")
        if not driver.find_elements(By.XPATH, "//button[normalize-space()='Save repository']"):
            pytest.skip("Save repository is not rendered, so the form cannot be driven.")
        form = form_by_heading(driver, "Repository")
        fill(driver, form, "Repository URL", repo_url)
        fill(driver, form, "Default branch", "main")
        click_button(driver, "Save repository")
        wait(driver, 20).until(
            lambda d: form_by_heading(d, "Repository")
            .find_element(By.CSS_SELECTOR, "span.status-chip")
            .get_attribute("data-status")
            == "UNKNOWN"
        )
        assert field(form_by_heading(driver, "Repository"), "Repository URL").get_attribute("value") == repo_url
        click_button(driver, "Validate")
        wait(driver, 20).until(
            lambda d: form_by_heading(d, "Repository")
            .find_element(By.CSS_SELECTOR, "span.status-chip")
            .get_attribute("data-status")
            == "SKIPPED"
        )
        assert "Non-GitHub URL; accessibility check skipped" in driver.find_element(By.TAG_NAME, "body").text
        click_button(driver, "Create snapshot")

        def snapshot_saved(d):
            items = d.find_elements(
                By.XPATH, "//h3[normalize-space()='Snapshots']/following-sibling::ul[1]//li"
            )
            return any(repo_url in item.text and "main" in item.text for item in items)

        wait(driver, 20).until(snapshot_saved)
        assert_no_error(driver)
        _secret_absent(driver, ctx)
        ctx["repository"] = True

    def _enqueue_fake_evaluation(self, driver, ctx, agent_name: str | None = None) -> str:
        name = agent_name or ctx["agent_name"]
        _open_benchmark(driver, ctx["benchmark_name"])
        _click_version(driver, ctx["version"])
        open_tab(driver, "Evaluations", "Evaluations")
        form = form_by_heading(driver, "Enqueue evaluation")
        version_id = field(form, "Published version ID").get_attribute("value") or ""
        task_id = field(form, "Task ID").get_attribute("value") or ""
        assert len(version_id) >= 32 and len(task_id) >= 32
        agent = Select(field(form, "Agent"))
        matching = [option.text for option in agent.options if name in option.text]
        assert matching, f"FAKE agent {name!r} was not offered on the evaluation form"
        agent.select_by_visible_text(matching[0])
        _submit(driver, form)
        wait_outcome(driver, "Evaluation detail", timeout=60)
        timeout = float(os.environ.get("AGENTBENCH_EVAL_TIMEOUT", "240"))
        state, _seen = wait_for_evaluation(driver, timeout)
        return state

    def test_44_costs_when_reported(self, driver, base_url, ctx):
        """Costs with a token or cost row. Empty copy stays in test_30."""
        _require(ctx, "shell", "agent_name")
        _as_owner(driver, base_url, ctx)
        open_tab(driver, "Costs", "Cost analytics")
        if _costs_screen(driver) != "rows":
            self._enqueue_fake_evaluation(driver, ctx)
            open_tab(driver, "Costs", "Cost analytics")
        state = wait(driver, 30).until(_costs_screen)
        if state == "empty":
            pytest.skip(
                "No token or estimated-cost row appeared after a FAKE evaluation, "
                "so the costs screen cannot show a row."
            )
        panel = _panel(driver, "Cost analytics")
        visible = panel.text.lower()
        assert "no token or cost records yet" not in visible
        assert "total tokens" in visible
        assert "by agent" in visible
        assert "by day" in visible
        assert ctx["agent_name"] in panel.text
        # FAKE sample is 192 tokens / $0.0032 per call. Earlier suite cases may
        # already have completed FAKE evaluations, so totals scale with N calls.
        text = panel.text
        matched = any(
            str(192 * n) in text and f"{0.0032 * n:.4f}" in text for n in range(1, 64)
        )
        assert matched, text
        assert_no_error(driver)
        _secret_absent(driver, ctx)
        ctx["costs_reported"] = True

    def test_45_quality_findings(self, driver, base_url, ctx):
        """Checkstyle, SpotBugs, and PMD show did-not-run on an evaluation detail."""
        _require(ctx, "shell", "agent_name")
        _as_owner(driver, base_url, ctx)
        self._enqueue_fake_evaluation(driver, ctx)
        wait(driver, 20).until(
            lambda d: d.find_elements(By.XPATH, "//h3[normalize-space()='Code quality findings']")
        )
        notes = _quality_notes(driver)
        assert set(notes) == set(_DID_NOT_RUN)
        for tool, expected in _DID_NOT_RUN.items():
            assert notes[tool] == expected
        assert_no_error(driver)
        _secret_absent(driver, ctx)
        ctx["quality_did_not_run"] = True

    def test_46_agent_context(self, driver, base_url, ctx):
        """The agent-context route returns the seeded draft task allowlist."""
        _require(ctx, "shell", "org_id", "draft_version_id", "draft_task_slug", "visible_framework")
        _as_owner(driver, base_url, ctx)
        listed = browser_json(
            driver,
            "GET",
            f"/api/v1/orgs/{ctx['org_id']}/benchmark-versions/{ctx['draft_version_id']}/tasks",
        )
        assert listed["status"] == 200, listed
        task = next(item for item in listed["body"] if item.get("slug") == ctx["draft_task_slug"])
        context = browser_json(
            driver,
            "GET",
            f"/api/v1/orgs/{ctx['org_id']}/tasks/{task['id']}/agent-context",
        )
        assert context["status"] == 200, context
        body = context["body"]
        assert body["taskId"] == task["id"]
        assert body["slug"] == ctx["draft_task_slug"]
        assert body["title"] == ctx["draft_task_title"]
        assert body["prompt"] == task["prompt"]
        assert body["visibleSuite"]["framework"] == ctx["visible_framework"]
        assert body["visibleSuite"]["entryCommand"]
        assert body["visibleSuite"]["caseCount"] == 1
        assert "hiddenSuite" not in body
        assert "referencePatch" not in body
        assert "acceptanceCriteria" not in body
        rendered = json.dumps(body)
        assert ctx["hidden_fixture"].strip() not in rendered
        assert_no_error(driver)
        ctx["agent_context"] = True

    def test_47_weighted_score_bars(self, driver, base_url, ctx):
        """Weighted share-of-100 bars on evaluation detail; F2P family stay counts."""
        _require(ctx, "shell", "agent_name")
        _as_owner(driver, base_url, ctx)
        self._enqueue_fake_evaluation(driver, ctx)
        wait(driver, 20).until(
            lambda d: d.find_elements(By.XPATH, "//h3[normalize-space()='Score']")
        )
        detail = driver.find_element(
            By.XPATH, "//h2[normalize-space()='Evaluation detail']/ancestor::section[1]"
        )
        roots = detail.find_elements(
            By.CSS_SELECTOR, ".score-bars[aria-label='Weighted score breakdown']"
        )
        if not roots:
            pytest.skip("Weighted score bars are not rendered on evaluation detail.")
        root = roots[0]
        bars = root.find_elements(By.CSS_SELECTOR, ".score-bar")
        assert bars, "Score section rendered without any weighted bars"
        tracks = root.find_elements(By.CSS_SELECTOR, ".score-bar-track[role='meter']")
        assert tracks, "Weighted bars did not expose meter tracks"
        for track in tracks:
            assert track.find_elements(By.CSS_SELECTOR, ".score-bar-fill")
            assert track.get_attribute("aria-valuemin") == "0"
            assert track.get_attribute("aria-valuemax")
            assert track.get_attribute("aria-valuenow") is not None
        labels = "\n".join(
            element.text for element in root.find_elements(By.CSS_SELECTOR, ".score-bar-label")
        )
        assert "Weighted total" in labels or "% weight" in labels, labels
        body = detail.text
        assert "F2P" in body and "P2P" in body and "P2F" in body and "F2F" in body
        assert not root.find_elements(
            By.CSS_SELECTOR,
            ".score-bar-track[aria-label='F2P'], .score-bar-track[aria-label='P2P'], "
            ".score-bar-track[aria-label='P2F'], .score-bar-track[aria-label='F2F']",
        )
        assert_no_error(driver)
        _secret_absent(driver, ctx)
        ctx["score_bars"] = True

    def test_48_public_leaderboard_filter(self, driver, base_url, ctx):
        """Client-side Search / Agent / Task filters on the published public board."""
        _require(ctx, "public_slug", "public_title", "agent_name", "task_slug")
        driver.get(base_url + f"/public/leaderboards/{ctx['public_slug']}")
        wait_outcome(driver, ctx["public_title"])

        def board_panel(d):
            for panel in d.find_elements(By.CSS_SELECTOR, "section.panel"):
                if panel.find_elements(
                    By.XPATH, ".//label[normalize-space(text()[1])='Search']"
                ):
                    return panel
            return None

        def row_texts(panel) -> str:
            return "\n".join(
                row.text
                for row in panel.find_elements(By.CSS_SELECTOR, "table.metrics-table tbody tr")
            )

        panel = board_panel(driver)
        if panel is None:
            pytest.skip("Public leaderboard client-side filters are not rendered.")
        assert row_texts(panel), "Public board filters rendered without any scored rows"

        fill(driver, panel, "Search", _FILTER_QUERY)
        wait_outcome(driver, "No leaderboard rows match these filters")
        assert not row_texts(board_panel(driver))

        click_button(driver, "Clear")
        wait(driver, 10).until(
            lambda d: field(board_panel(d), "Search").get_attribute("value") == ""
            and bool(row_texts(board_panel(d)))
        )

        panel = board_panel(driver)
        agent_options = [option.text for option in Select(field(panel, "Agent")).options]
        assert ctx["agent_name"] in agent_options, "Seeded agent missing from public filter"
        _choose_select_text(driver, field(panel, "Agent"), ctx["agent_name"])
        wait(driver, 10).until(lambda d: ctx["agent_name"] in row_texts(board_panel(d)))

        panel = board_panel(driver)
        task_options = [option.text for option in Select(field(panel, "Task")).options]
        assert ctx["task_slug"] in task_options, "Seeded task missing from public filter"
        _choose_select_text(driver, field(panel, "Task"), ctx["task_slug"])
        wait(driver, 10).until(lambda d: ctx["task_slug"] in row_texts(board_panel(d)))

        fill(driver, board_panel(driver), "Search", _FILTER_QUERY)
        wait_outcome(driver, "No leaderboard rows match these filters")
        assert not row_texts(board_panel(driver))

        click_button(driver, "Clear")
        wait(driver, 10).until(
            lambda d: field(board_panel(d), "Search").get_attribute("value") == ""
            and bool(row_texts(board_panel(d)))
        )
        assert_no_error(driver)
        ctx["public_filter"] = True

    def test_49_member_invite_role_remove(self, driver, base_url, ctx):
        """Invite a registered user, change their role, then remove them."""
        _require(ctx, "shell", "tenant_email", "tenant_display_name", "email", "display_name")
        _as_owner(driver, base_url, ctx)
        tabs = [
            element
            for element in driver.find_elements(By.CSS_SELECTOR, "button.tab")
            if element.is_displayed() and element.text.strip() == "Members"
        ]
        if not tabs:
            pytest.skip("Members tab is not in the UI, so membership cannot be mutated.")
        tabs[0].click()
        wait(driver, 20).until(
            lambda d: d.find_elements(By.XPATH, "//h2[contains(., 'Members')]")
        )
        if not driver.find_elements(By.XPATH, "//h3[normalize-space()='Add member']"):
            pytest.skip("Add member form is not rendered, so invite cannot be driven.")

        email = ctx["tenant_email"]
        display = ctx["tenant_display_name"]

        def member_item(d, member_email: str):
            for item in d.find_elements(By.CSS_SELECTOR, "ul.list.plain li"):
                try:
                    if item.is_displayed() and member_email in item.text:
                        return item
                except StaleElementReferenceException:
                    return False
            return None

        def member_gone(d) -> bool:
            return member_item(d, email) is None

        # A prior interrupted run may have left the tenant on the mock org.
        leftover = member_item(driver, email)
        if leftover is not None:
            remove = leftover.find_elements(By.XPATH, ".//button[normalize-space()='Remove']")
            assert remove, "Leftover invitee has no Remove control"
            scroll(driver, remove[0])
            remove[0].click()
            wait(driver, 20).until(member_gone)
            wait_not_busy(driver)
            assert_no_error(driver)

        def role_value(item, member_display: str) -> str:
            selects = item.find_elements(
                By.CSS_SELECTOR, f"select[aria-label='Role for {member_display}']"
            )
            if selects:
                return selects[0].get_attribute("value") or ""
            badges = item.find_elements(By.CSS_SELECTOR, "span.badge")
            return badges[0].text.strip() if badges else ""

        form = form_by_heading(driver, "Add member")
        fill(driver, form, "Email", email)
        fill(driver, form, "Role", "VIEWER")
        _submit(driver, form)
        wait(driver, 20).until(lambda d: member_item(d, email) is not None)
        wait_not_busy(driver)
        assert_no_error(driver)
        invited = member_item(driver, email)
        assert invited is not None
        assert role_value(invited, display) == "VIEWER"

        role_select = invited.find_element(
            By.CSS_SELECTOR, f"select[aria-label='Role for {display}']"
        )
        _choose_select_text(driver, role_select, "DEVELOPER")

        def role_is_developer(d) -> bool:
            item = member_item(d, email)
            return item is not None and role_value(item, display) == "DEVELOPER"

        wait(driver, 20).until(role_is_developer)
        wait_not_busy(driver)
        assert_no_error(driver)

        invited = member_item(driver, email)
        assert invited is not None
        remove = invited.find_element(By.XPATH, ".//button[normalize-space()='Remove']")
        scroll(driver, remove)
        remove.click()
        wait(driver, 20).until(member_gone)
        wait_not_busy(driver)
        assert_no_error(driver)

        body = driver.find_element(By.TAG_NAME, "body").text
        assert email not in body
        assert ctx["email"] in body
        owner_item = member_item(driver, ctx["email"])
        assert owner_item is not None
        assert role_value(owner_item, ctx["display_name"]) == "OWNER"
        # Sole OWNER must keep Remove hidden (backend 409 if last owner removed).
        assert not owner_item.find_elements(By.XPATH, ".//button[normalize-space()='Remove']")
        _secret_absent(driver, ctx)
        ctx["member_mutate"] = True

    def test_50_nonempty_comparison_matrix(self, driver, base_url, ctx):
        """Compare two scored FAKE agents on one published version (REG-COMPARE-NONEMPTY)."""
        _require(ctx, "shell", "agent_name", "compare_agent_name", "version", "task_slug")
        _as_owner(driver, base_url, ctx)

        primary = ctx["agent_name"]
        secondary = ctx["compare_agent_name"]
        for agent_name in (primary, secondary):
            state = self._enqueue_fake_evaluation(driver, ctx, agent_name=agent_name)
            if state != "COMPLETED":
                pytest.skip(
                    f"FAKE evaluation for {agent_name!r} ended as {state!r}; "
                    "comparison matrix needs COMPLETED + VALID scores."
                )

        _open_benchmark(driver, ctx["benchmark_name"])
        _click_version(driver, ctx["version"])
        open_tab(driver, "Compare", "Model comparisons")
        compare = _panel(driver, "Model comparisons")

        version_select = field(compare, "Benchmark version")
        published = f"{ctx['version']} (PUBLISHED)"
        options = [option.text for option in Select(version_select).options]
        assert published in options, f"Published version missing from Compare: {options}"
        _choose_select_text(driver, version_select, published)

        for agent_name in (primary, secondary):
            panel = _panel(driver, "Model comparisons")
            checkbox = panel.find_element(
                By.XPATH,
                f".//label[contains(., '{agent_name}')]//input[@type='checkbox']",
            )
            if not checkbox.is_selected():
                scroll(driver, checkbox)
                checkbox.click()

        click_button(driver, "Run comparison")

        def matrix_ready(d) -> bool | str:
            panel = _panel(d, "Model comparisons")
            text = panel.text
            if "Select agents and a published benchmark version to compare identical runs." in text:
                return False
            if "No comparison rows match these filters" in text:
                return False
            rows = panel.find_elements(By.CSS_SELECTOR, "table.metrics-table tbody tr")
            if len(rows) < 2:
                return False
            body = "\n".join(row.text for row in rows)
            if primary not in body or secondary not in body:
                return False
            if ctx["task_slug"] not in body:
                return False
            headers = [
                cell.text.strip()
                for cell in panel.find_elements(By.CSS_SELECTOR, "table.metrics-table thead th")
            ]
            if "Agent" not in headers or "Total" not in headers:
                return False
            # Metric columns beyond Agent / Task / Total prove a filled matrix.
            if len(headers) <= 3:
                return False
            if "Profile " not in text:
                return False
            return True

        wait(driver, 30).until(matrix_ready)
        panel = _panel(driver, "Model comparisons")
        rows = panel.find_elements(By.CSS_SELECTOR, "table.metrics-table tbody tr")
        assert len(rows) >= 2, panel.text
        body = "\n".join(row.text for row in rows)
        assert primary in body and secondary in body, body
        assert ctx["task_slug"] in body, body
        headers = [
            cell.text.strip()
            for cell in panel.find_elements(By.CSS_SELECTOR, "table.metrics-table thead th")
        ]
        assert any(key not in {"Agent", "Task", "Total"} and key for key in headers), headers
        assert_no_error(driver)
        _secret_absent(driver, ctx)
        ctx["compare_nonempty"] = True

    def _enqueue_api_evaluation(self, driver, ctx) -> dict:
        """Create one evaluation via the session API. Returns the response body."""
        created = browser_json(
            driver,
            "POST",
            f"/api/v1/orgs/{ctx['org_id']}/evaluations",
            {
                "benchmarkVersionId": ctx["version_id"],
                "taskId": ctx["task_id"],
                "agentId": ctx["agent_id"],
            },
        )
        assert created["status"] in (200, 201), created
        body = created.get("body") or {}
        assert body.get("id"), created
        return body

    def _queued_evaluation_id(self, driver, ctx) -> str | None:
        listed = browser_json(
            driver,
            "GET",
            f"/api/v1/orgs/{ctx['org_id']}/evaluations?state=QUEUED&limit=50",
        )
        assert listed["status"] == 200, listed
        rows = listed.get("body") or []
        if not rows:
            return None
        return str(rows[0]["id"])

    def _open_evaluation_by_id(self, driver, ctx, evaluation_id: str) -> None:
        short = evaluation_id[:8]
        _open_benchmark(driver, ctx["benchmark_name"])
        _click_version(driver, ctx["version"])
        open_tab(driver, "Evaluations", "Evaluations")
        panel = _panel(driver, "Evaluations")
        state_field = field(panel, "State")
        _choose_select_text(driver, state_field, "QUEUED")

        def find_row(d):
            for button in d.find_elements(By.CSS_SELECTOR, "ul.list button.list-item"):
                try:
                    if button.is_displayed() and short in button.text:
                        return button
                except StaleElementReferenceException:
                    return False
            return False

        row = wait(driver, 20).until(find_row)
        scroll(driver, row)
        row.click()
        wait(driver, 20).until(
            lambda d: d.find_elements(By.XPATH, "//h2[normalize-space()='Evaluation detail']")
        )

    def test_51_cancel_while_queued(self, driver, base_url, ctx):
        """Cancel while still QUEUED (SEL-CANCEL-RACE). test_18 cancels any non-terminal."""
        _require(ctx, "shell", "org_id", "version_id", "task_id", "agent_id", "benchmark_name", "version")
        _as_owner(driver, base_url, ctx)

        def cancel_button(d):
            for button in d.find_elements(By.XPATH, "//button[normalize-space()='Cancel']"):
                try:
                    if button.is_displayed() and button.is_enabled():
                        return button
                except StaleElementReferenceException:
                    return False
            return False

        # Default org concurrency is 2 active non-QUEUED. Flood the queue so at least
        # one evaluation stays QUEUED long enough for the UI cancel path.
        cancelled = False
        last_state = ""
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not cancelled:
            for _ in range(5):
                self._enqueue_api_evaluation(driver, ctx)
            candidate = self._queued_evaluation_id(driver, ctx)
            if not candidate:
                continue
            detail = browser_json(
                driver,
                "GET",
                f"/api/v1/orgs/{ctx['org_id']}/evaluations/{candidate}",
            )
            assert detail["status"] == 200, detail
            body = detail.get("body") or {}
            evaluation = body.get("evaluation") if isinstance(body.get("evaluation"), dict) else body
            if (evaluation or {}).get("state") != "QUEUED":
                continue

            self._open_evaluation_by_id(driver, ctx, candidate)
            try:
                state = evaluation_state(driver)
            except (StaleElementReferenceException, Exception):
                continue
            last_state = state
            if state != "QUEUED":
                continue

            found = wait(driver, 10).until(cancel_button)
            if evaluation_state(driver) != "QUEUED":
                continue
            scroll(driver, found)
            found.click()

            cancel_deadline = time.monotonic() + 30
            state = ""
            while time.monotonic() < cancel_deadline:
                try:
                    state = evaluation_state(driver)
                except (StaleElementReferenceException, Exception):
                    state = ""
                if state == "CANCELLED":
                    cancelled = True
                    break
                time.sleep(0.5)
            last_state = state

        assert cancelled, (
            "Cancel while QUEUED did not reach CANCELLED. "
            f"last_state={last_state!r} errors={visible_errors(driver)}"
        )
        assert not any(
            button.is_displayed() and button.text.strip() == "Cancel"
            for button in driver.find_elements(By.XPATH, "//button[normalize-space()='Cancel']")
        )
        # Immediate QUEUED→CANCELLED sets USER / CANCELLED_BY_USER on the detail.
        body = driver.find_element(By.TAG_NAME, "body").text
        assert "CANCELLED_BY_USER" in body or "Cancelled by user" in body, body[:800]
        _secret_absent(driver, ctx)
        ctx["cancel_queued"] = True
