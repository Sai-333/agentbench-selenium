"""Small Selenium helpers for the AgentBench single-page UI.

The app has no data-testid attributes. Forms are found by their h3, and fields
by the label's own text node so nested select options do not confuse the match.
"""

from __future__ import annotations

import time

from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import Select, WebDriverWait

TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}
PROGRESS_STATES = {
    "PREPARING_REPO",
    "RUNNING_BASELINE",
    "INVOKING_AGENT",
    "COLLECTING_PATCH",
    "VALIDATING_PATCH",
    "APPLYING_PATCH",
    "RUNNING_VISIBLE_TESTS",
    "RUNNING_HIDDEN_TESTS",
    "ANALYZING_QUALITY",
    "SCORING",
}


def wait(driver: WebDriver, timeout: float = 30) -> WebDriverWait:
    return WebDriverWait(driver, timeout, ignored_exceptions=(StaleElementReferenceException,))


def scroll(driver: WebDriver, element: WebElement) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)


def visible_errors(driver: WebDriver) -> list[str]:
    texts: list[str] = []
    for element in driver.find_elements(By.CSS_SELECTOR, "p.error"):
        try:
            if element.is_displayed():
                text = element.text.strip()
                if text:
                    texts.append(text)
        except StaleElementReferenceException:
            continue
    return texts


def assert_no_error(driver: WebDriver) -> None:
    errors = visible_errors(driver)
    assert not errors, "UI error: " + " | ".join(errors)


def wait_not_busy(driver: WebDriver, timeout: float = 45) -> None:
    def idle(d: WebDriver) -> bool:
        for button in d.find_elements(By.TAG_NAME, "button"):
            try:
                label = button.text.strip()
            except StaleElementReferenceException:
                return False
            if label.startswith("Working") or label.startswith("Posting"):
                return False
        return True

    wait(driver, timeout).until(idle)


def browser_severe(driver: WebDriver) -> list[str]:
    """Return console SEVERE entries, ignoring known browser noise."""
    try:
        entries = driver.get_log("browser")
    except Exception:
        return []
    ignored = ("favicon", "Download the React DevTools", "net::ERR_BLOCKED_BY_CLIENT")
    severe: list[str] = []
    for entry in entries:
        if entry.get("level") != "SEVERE":
            continue
        message = str(entry.get("message", ""))
        if any(token in message for token in ignored):
            continue
        severe.append(message)
    return severe


def open_tab(driver: WebDriver, label: str, heading_prefix: str) -> None:
    def find_tab(d: WebDriver) -> WebElement:
        return d.find_element(
            By.XPATH,
            f"//button[contains(@class,'tab') and normalize-space()='{label}']",
        )

    button = wait(driver, 20).until(find_tab)
    scroll(driver, button)
    button.click()
    wait(driver, 20).until(
        lambda d: d.find_elements(
            By.XPATH, f"//h2[starts-with(normalize-space(), '{heading_prefix}')]"
        )
    )


def form_by_heading(driver: WebDriver, heading: str) -> WebElement:
    return wait(driver, 20).until(
        lambda d: d.find_element(By.XPATH, f"//form[.//h3[normalize-space()='{heading}']]")
    )


def field(scope: WebElement, label_text: str) -> WebElement:
    label = scope.find_element(
        By.XPATH, f".//label[normalize-space(text()[1])='{label_text}']"
    )
    return label.find_element(By.XPATH, ".//input|.//textarea|.//select")


def react_set(driver: WebDriver, element: WebElement, value: str) -> None:
    """Set an input or textarea in a way React controlled fields observe."""
    scroll(driver, element)
    driver.execute_script(
        """
        const el = arguments[0];
        const value = arguments[1];
        const proto = el instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        setter.call(el, value);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        """,
        element,
        value,
    )


def fill(driver: WebDriver, scope: WebElement, label_text: str, value: str) -> None:
    element = field(scope, label_text)
    if element.tag_name == "select":
        Select(element).select_by_visible_text(value)
        return
    react_set(driver, element, value)


def wait_outcome(driver: WebDriver, expected: str, timeout: float = 30) -> None:
    """Wait until `expected` is visible, and fail immediately if the UI shows an error."""

    def check(d: WebDriver):
        errors = visible_errors(d)
        if errors:
            return ("error", errors)
        # CSS text-transform (stat labels are uppercase) changes Selenium's visible text.
        if expected.lower() in d.find_element(By.TAG_NAME, "body").text.lower():
            return ("ok", None)
        return False

    kind, errors = wait(driver, timeout).until(check)
    if kind == "error":
        raise AssertionError("UI error: " + " | ".join(errors))


def click_submit(
    driver: WebDriver,
    form: WebElement,
    expected: str | None = None,
    timeout: float = 45,
) -> None:
    button = form.find_element(By.CSS_SELECTOR, "button[type='submit']")
    scroll(driver, button)
    wait(driver, 20).until(lambda d: button.is_enabled())
    button.click()
    if expected:
        wait_outcome(driver, expected, timeout=timeout)
    else:
        wait_not_busy(driver, timeout)
        assert_no_error(driver)


def click_button(driver: WebDriver, label: str, timeout: float = 20) -> None:
    def find(d: WebDriver):
        matches = [
            match
            for match in d.find_elements(By.XPATH, f"//button[normalize-space()='{label}']")
            if match.is_displayed() and match.is_enabled()
        ]
        return matches[0] if matches else False

    button = wait(driver, timeout).until(find)
    scroll(driver, button)
    button.click()


def wait_text(driver: WebDriver, text: str, timeout: float = 30) -> None:
    wait(driver, timeout).until(lambda d: text in d.find_element(By.TAG_NAME, "body").text)


def evaluation_state(driver: WebDriver) -> str:
    section = driver.find_element(
        By.XPATH, "//h2[normalize-space()='Evaluation detail']/ancestor::section[1]"
    )
    return section.find_element(By.CSS_SELECTOR, "span.badge").text.strip()


def wait_for_evaluation(driver: WebDriver, timeout: float) -> tuple[str, list[str]]:
    """Poll the detail badge until a terminal state, or progress past QUEUED."""
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    last = ""
    while time.monotonic() < deadline:
        try:
            state = evaluation_state(driver)
        except (StaleElementReferenceException, NoSuchElementException):
            time.sleep(0.5)
            continue
        if state and state != last:
            seen.append(state)
            last = state
        if state in TERMINAL_STATES:
            return state, seen
        if state in PROGRESS_STATES:
            # Keep waiting for a terminal state, but remember progress was visible.
            pass
        time.sleep(2)
    if last in PROGRESS_STATES or any(state in PROGRESS_STATES for state in seen):
        return last, seen
    raise AssertionError(
        f"Evaluation stayed at {last or 'unknown'} for {timeout:.0f}s; observed {seen}. "
        "Expected a terminal state (COMPLETED, FAILED, CANCELLED, TIMED_OUT) "
        "or a stage past QUEUED."
    )


def export_statuses(driver: WebDriver) -> list[tuple[str, int]]:
    """Network responses recorded since the last performance-log drain."""
    found: list[tuple[str, int]] = []
    try:
        entries = driver.get_log("performance")
    except Exception:
        return found
    import json

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
        if "export" in url and isinstance(status, int):
            found.append((url, status))
    return found
