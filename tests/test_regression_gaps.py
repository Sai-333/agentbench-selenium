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

import pytest
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By

from tests.test_regression import _click_named_item, _require
from tests.test_regression_edges import (
    _click_version,
    _detail,
    _login,
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
