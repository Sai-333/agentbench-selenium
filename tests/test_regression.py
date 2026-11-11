"""End-to-end UI regression against a running AgentBench Compose stack.

Tests share one browser session and run in definition order. Identities come
from the seeded mock fixture, not from per-run emails. Each step records a
flag on `ctx`; later steps skip with an explicit reason when an earlier step
did not finish, so a single product failure is not reported as a cascade.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select

from tests.ui import (
    TERMINAL_STATES,
    assert_no_error,
    browser_severe,
    click_button,
    click_submit,
    export_statuses,
    field,
    fill,
    form_by_heading,
    open_tab,
    scroll,
    visible_errors,
    wait,
    wait_for_evaluation,
    wait_outcome,
)


def _require(ctx: dict, *flags: str) -> None:
    missing = [flag for flag in flags if not ctx.get(flag)]
    if missing:
        pytest.skip("blocked by earlier step: " + ", ".join(missing))


def _click_named_item(driver, text: str) -> None:
    def find(d):
        for element in d.find_elements(By.CSS_SELECTOR, "button.list-item"):
            try:
                if element.is_displayed() and text in element.text:
                    return element
            except StaleElementReferenceException:
                return False
        return False

    button = wait(driver, 20).until(find)
    scroll(driver, button)
    button.click()


def _auth_form(driver):
    return wait(driver).until(
        lambda d: d.find_element(By.XPATH, "//form[.//input[@name='email']]")
    )


class TestAgentBenchRegression:
    def test_01_shell_loads(self, driver, base_url, ctx):
        driver.get(base_url + "/")
        wait(driver).until(lambda d: d.find_elements(By.XPATH, "//h1[normalize-space()='AgentBench']"))
        wait(driver).until(lambda d: "Loading session" not in d.find_element(By.TAG_NAME, "body").text)
        assert "Sign in to manage organizations" in driver.find_element(By.TAG_NAME, "body").text
        severe = browser_severe(driver)
        assert not severe, "Console SEVERE on app shell:\n" + "\n".join(severe)
        ctx["shell"] = True

    def test_02_login_seeded_user(self, driver, ctx):
        _require(ctx, "shell")
        click_button(driver, "Log in")
        form = _auth_form(driver)
        fill(driver, form, "Email", ctx["email"])
        fill(driver, form, "Password", ctx["password"])
        click_submit(driver, form, expected=ctx["display_name"], timeout=60)
        assert ctx["email"] in driver.find_element(By.TAG_NAME, "body").text
        assert driver.find_elements(By.XPATH, "//button[normalize-space()='Log out']")
        ctx["signed_in"] = True

    def test_03_logout(self, driver, ctx):
        _require(ctx, "signed_in")
        click_button(driver, "Log out")
        wait_outcome(driver, "Sign in to manage organizations")
        assert not driver.find_elements(By.XPATH, "//button[normalize-space()='Log out']")
        ctx["signed_in"] = False
        ctx["logged_out"] = True

    def test_04_login(self, driver, ctx):
        _require(ctx, "logged_out")
        click_button(driver, "Log in")
        form = _auth_form(driver)
        fill(driver, form, "Email", ctx["email"])
        fill(driver, form, "Password", ctx["password"])
        click_submit(driver, form, expected=ctx["display_name"], timeout=60)
        assert ctx["email"] in driver.find_element(By.TAG_NAME, "body").text
        ctx["signed_in"] = True

    def test_05_seeded_org(self, driver, ctx):
        _require(ctx, "signed_in")
        wait_outcome(driver, ctx["org_name"])
        _click_named_item(driver, ctx["org_name"])
        wait_outcome(driver, ctx["org_slug"])
        assert ctx["org_slug"] in driver.find_element(By.TAG_NAME, "body").text
        ctx["org"] = True

    def test_06_seeded_project(self, driver, ctx):
        _require(ctx, "org")
        open_tab(driver, "Projects", "Projects")
        wait_outcome(driver, ctx["project_name"])
        body = driver.find_element(By.TAG_NAME, "body").text
        assert ctx["project_description"] in body
        assert_no_error(driver)
        ctx["project"] = True

    def test_07_seeded_benchmark(self, driver, ctx):
        _require(ctx, "project")
        open_tab(driver, "Benchmarks", "Benchmarks")
        _click_named_item(driver, ctx["benchmark_name"])
        wait_outcome(driver, ctx["version"])
        wait_outcome(driver, "PUBLISHED")
        wait_outcome(driver, ctx["task_title"])
        wait_outcome(driver, "Visible suite: junit")
        assert ctx["task_slug"] in driver.find_element(By.TAG_NAME, "body").text
        assert_no_error(driver)
        ctx["benchmark"] = True

    def test_08_seeded_agent(self, driver, ctx):
        _require(ctx, "benchmark")
        open_tab(driver, "Agents", "Agents")
        wait_outcome(driver, ctx["agent_name"])
        assert "FAKE" in driver.find_element(By.TAG_NAME, "body").text
        assert_no_error(driver)
        ctx["agent"] = True

    def test_09_start_evaluation(self, driver, ctx):
        _require(ctx, "agent")
        open_tab(driver, "Evaluations", "Evaluations")
        form = form_by_heading(driver, "Enqueue evaluation")
        version_id = field(form, "Published version ID").get_attribute("value") or ""
        task_id = field(form, "Task ID").get_attribute("value") or ""
        assert len(version_id) >= 32, f"Published version ID was not filled ({version_id!r})"
        assert len(task_id) >= 32, f"Task ID was not filled ({task_id!r})"
        agent = Select(field(form, "Agent"))
        matching = [option.text for option in agent.options if ctx["agent_name"] in option.text]
        assert matching, "FAKE agent was not offered on the evaluation form"
        agent.select_by_visible_text(matching[0])
        click_submit(driver, form, expected="Evaluation detail")

        timeout = float(os.environ.get("AGENTBENCH_EVAL_TIMEOUT", "240"))
        state, seen = wait_for_evaluation(driver, timeout)
        ctx["eval_state"] = state
        ctx["eval_seen"] = seen
        detail = driver.find_element(
            By.XPATH, "//h2[normalize-space()='Evaluation detail']/ancestor::section[1]"
        ).text
        ctx["eval_detail"] = detail[:1500]
        assert state in TERMINAL_STATES or state != "QUEUED", (
            f"Evaluation did not leave QUEUED; observed {seen}"
        )
        # A terminal failure is still a completed UI flow. The report records the state.
        Path("reports").mkdir(exist_ok=True)
        Path("reports/eval-state.txt").write_text(
            f"EVAL_STATE={state}\nSEEN={seen}\n\n{detail}\n",
            encoding="utf-8",
        )
        print(f"\nEVAL_STATE={state} SEEN={seen}")
        ctx["evaluation"] = True

    def test_10_analytics_and_leaderboard(self, driver, ctx):
        _require(ctx, "evaluation")
        open_tab(driver, "Overview", "Evaluation overview")
        wait_outcome(driver, "Total")
        assert_no_error(driver)
        body = driver.find_element(By.TAG_NAME, "body").text
        assert "No evaluation activity yet" not in body

        open_tab(driver, "Leaderboard", "Org leaderboard")
        wait(driver, 30).until(
            lambda d: (
                "No completed scored evaluations" in d.find_element(By.TAG_NAME, "body").text
                or "Profile " in d.find_element(By.TAG_NAME, "body").text
            )
            and not visible_errors(d)
        )
        assert_no_error(driver)
        ctx["analytics"] = True

    def test_11_export_csv(self, driver, ctx, download_dir: Path):
        _require(ctx, "analytics")
        # Drain earlier network logs so the export response is the one we observe.
        export_statuses(driver)
        before = set(download_dir.iterdir())
        click_button(driver, "Export CSV")
        deadline = time.monotonic() + 20
        downloaded = False
        statuses: list[tuple[str, int]] = []
        while time.monotonic() < deadline:
            errors = visible_errors(driver)
            if errors:
                raise AssertionError("Export failed: " + " | ".join(errors))
            statuses.extend(export_statuses(driver))
            if any(status == 200 and "export" in url for url, status in statuses):
                downloaded = True
                break
            fresh = [
                path
                for path in download_dir.iterdir()
                if path not in before and path.suffix == ".csv" and path.stat().st_size > 0
            ]
            if fresh:
                downloaded = True
                ctx["export_file"] = fresh[0].name
                break
            time.sleep(0.5)
        assert downloaded, f"Export CSV did not return a download or HTTP 200 (statuses={statuses})"
        ctx["export_statuses"] = statuses
        ctx["exported"] = True

    def test_12_public_leaderboard(self, driver, base_url, ctx):
        _require(ctx, "org")
        open_tab(driver, "Leaderboard", "Org leaderboard")
        link = wait(driver, 30).until(
            lambda d: d.find_elements(
                By.CSS_SELECTOR, f"a[href*='/public/leaderboards/{ctx['public_slug']}']"
            )
        )[0]
        href = link.get_attribute("href")
        assert href, "Public leaderboard link had no href"
        driver.get(href)
        wait_outcome(driver, "Community")
        wait_outcome(driver, ctx["public_title"])
        wait_outcome(driver, ctx["comment"])
        wait_outcome(driver, f"▲ Up ({ctx['up_count']})")
        body = driver.find_element(By.TAG_NAME, "body").text
        assert ctx["comment_author"] in body
        assert f"Score {ctx['score']}" in body
        assert "No comments yet." not in body
        assert_no_error(driver)
        ctx["public"] = True

    def test_13_ops_health(self, driver, base_url, ctx):
        _require(ctx, "signed_in")
        driver.get(base_url + "/")
        wait_outcome(driver, ctx["display_name"])
        open_tab(driver, "Ops", "System health")
        click_button(driver, "Refresh health")
        wait_outcome(driver, "Overall")
        assert_no_error(driver)
        severe = browser_severe(driver)
        # Ignore resource failures for optional third-party assets; keep exceptions.
        fatal = [line for line in severe if "Uncaught" in line or "TypeError" in line or "ReferenceError" in line]
        assert not fatal, "Console-fatal on health view:\n" + "\n".join(fatal)
        ctx["health"] = True

    def test_14_regression_runs(self, driver, base_url, ctx):
        """AgentBench application regression (platform top-bar), not an org tab."""
        _require(ctx, "signed_in", "regression_suite", "regression_passed_case", "regression_failed_case")
        driver.get(base_url + "/")
        wait_outcome(driver, ctx["display_name"])
        # Top-bar platform control (button.tab); outside consumer org workspace tabs.
        open_tab(driver, "Regression runs", "Regression runs")
        wait_outcome(driver, "application regression")
        wait_outcome(driver, "Not consumer")
        wait_outcome(driver, ctx["regression_suite"])

        def find_run(d):
            matches = d.find_elements(
                By.XPATH,
                "//button[contains(@class,'list-item')]"
                f"[.//span[normalize-space()='{ctx['regression_suite']}']]",
            )
            for element in matches:
                try:
                    if element.is_displayed():
                        return element
                except StaleElementReferenceException:
                    return False
            return False

        run_button = wait(driver, 20).until(find_run)
        scroll(driver, run_button)
        run_button.click()
        passed = wait(driver, 20).until(
            lambda d: d.find_element(
                By.XPATH,
                "//li[contains(@class,'regression-case')]"
                f"[contains(., '{ctx['regression_passed_case']}')]",
            )
        )
        failed = wait(driver, 20).until(
            lambda d: d.find_element(
                By.XPATH,
                "//li[contains(@class,'regression-case')]"
                f"[contains(., '{ctx['regression_failed_case']}')]",
            )
        )
        assert "passed" in passed.text.lower()
        assert "failed" in failed.text.lower()
        assert "passed" not in failed.text.lower()
        assert_no_error(driver)
        ctx["regression"] = True
