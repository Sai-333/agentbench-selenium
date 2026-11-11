"""Session browser pointed at a running AgentBench Compose stack."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from tests.ui import browser_severe


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _url_ok(url: str, timeout: float = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _body_contains(url: str, needle: str, timeout: float = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return False
            body = response.read().decode("utf-8", errors="replace")
            return needle in body
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get("AGENTBENCH_BASE_URL", "http://localhost:3000").rstrip("/")


@pytest.fixture(scope="session")
def api_url() -> str:
    return os.environ.get("AGENTBENCH_API_URL", "http://localhost:8080").rstrip("/")


@pytest.fixture(scope="session", autouse=True)
def require_stack(base_url: str, api_url: str) -> None:
    """Wait until API liveness/readiness are UP and the frontend returns 200."""
    deadline = time.monotonic() + _env_int("AGENTBENCH_STARTUP_TIMEOUT", 180)
    last = "stack not checked"
    while time.monotonic() < deadline:
        live = _body_contains(f"{api_url}/actuator/health/liveness", "UP")
        ready = _body_contains(f"{api_url}/actuator/health/readiness", "UP")
        front = _url_ok(f"{base_url}/")
        if live and ready and front:
            return
        last = f"liveness={live} readiness={ready} frontend={front}"
        time.sleep(3)
    pytest.fail(
        "AgentBench Compose stack was not ready. "
        f"Last check: {last}. Start it with `docker compose up -d --build` "
        "in the agentbench checkout, then rerun pytest."
    )


@pytest.fixture(scope="session")
def download_dir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="agentbench-selenium-downloads-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session")
def driver(download_dir: Path):
    options = Options()
    if os.environ.get("HEADLESS", "1") != "0":
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1600,1400")
    options.add_argument("--disable-extensions")
    chrome = os.environ.get("CHROME_BIN", "/usr/local/bin/google-chrome")
    if Path(chrome).exists():
        options.binary_location = chrome
    options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": str(download_dir),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": False,
        },
    )
    options.set_capability("goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"})
    options.add_experimental_option(
        "perfLoggingPrefs", {"enableNetwork": True, "enablePage": False}
    )
    browser = webdriver.Chrome(options=options)
    browser.set_page_load_timeout(60)
    browser.set_script_timeout(30)
    _allow_downloads(browser, download_dir)
    yield browser
    browser.quit()


def _allow_downloads(browser, download_dir: Path) -> None:
    for command in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
        try:
            browser.execute_cdp_cmd(
                command,
                {
                    "behavior": "allow",
                    "downloadPath": str(download_dir),
                    "eventsEnabled": True,
                },
            )
            return
        except Exception:
            continue


@pytest.fixture(scope="session", autouse=True)
def seeded(require_stack, api_url: str) -> dict:
    """Upsert fixtures/mock-data.json before any case. Reruns reuse the same rows."""
    from tests.seed import seed_mock_data

    ctx = seed_mock_data(api_url)
    print(
        "\nSEEDED "
        f"email={ctx['email']} org={ctx['org_slug']} project={ctx['project_name']} "
        f"benchmark={ctx['benchmark_name']} version={ctx['version']} "
        f"task={ctx['task_slug']} agent={ctx['agent_name']} "
        f"board={ctx['public_slug']} up={ctx['up_count']}"
    )
    return ctx


@pytest.fixture(scope="session")
def ctx(seeded: dict) -> dict:
    """Fixture identities shared by the ordered session. Tests record flags on this dict."""
    return seeded


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    browser = item.funcargs.get("driver")
    if browser is None:
        return
    out = Path("reports")
    out.mkdir(exist_ok=True)
    try:
        browser.save_screenshot(str(out / f"{item.name}.png"))
        (out / f"{item.name}.html").write_text(browser.page_source, encoding="utf-8")
        severe = browser_severe(browser)
        if severe:
            (out / f"{item.name}.console.txt").write_text("\n".join(severe), encoding="utf-8")
    except Exception:
        return
