# AgentBench Selenium regression

Python 3 + Selenium 4 + pytest drive the real AgentBench UI in headless Chrome. The suite does not live in the application repo. It expects a Compose stack started from [Sai-333/agentbench](https://github.com/Sai-333/agentbench) (verified against `main` at `4956497`).

## Run against Compose

From a checkout of the app (not this repo):

```bash
cd agentbench
docker compose up -d --build
```

Default Compose is enough: local artifact storage and plain AMQP on port 5672. Do not enable the AMQPS overlay, the S3 profile, gVisor, or Firecracker for this suite.

Wait until the API is up and the UI returns 200:

```bash
curl -sf http://localhost:8080/actuator/health/liveness
curl -sf http://localhost:8080/actuator/health/readiness
curl -sf -o /dev/null -w '%{http_code}\n' http://localhost:3000/
```

Then, in this repo:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```

Pytest waits up to `AGENTBENCH_STARTUP_TIMEOUT` seconds (default 180) for those three checks before opening Chrome. Tests share one browser session and must run in definition order in a single process. Do not shard or randomize them.

Headless Chrome is the default (`--headless=new`, `--no-sandbox`, `--disable-dev-shm-usage`). Set `HEADLESS=0` to watch a local browser. `CHROME_BIN` overrides the Chrome binary (default `/usr/local/bin/google-chrome`). Selenium Manager downloads a matching ChromeDriver on first run.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `AGENTBENCH_BASE_URL` | `http://localhost:3000` | UI origin |
| `AGENTBENCH_API_URL` | `http://localhost:8080` | Liveness and readiness probes |
| `AGENTBENCH_STARTUP_TIMEOUT` | `180` | Seconds to wait for Compose |
| `AGENTBENCH_PASSWORD` | `password123` | Password used for the fresh account (min 8) |
| `AGENTBENCH_RUN_ID` | time + pid | Suffix for emails, slugs, and names so reruns do not collide |
| `AGENTBENCH_EVAL_TIMEOUT` | `240` | Seconds to wait for an evaluation to leave `QUEUED` or finish |
| `HEADLESS` | `1` | Set `0` to show Chrome |
| `CHROME_BIN` | `/usr/local/bin/google-chrome` | Browser binary |

No API keys, cloud credentials, or `.env` secrets are required. Each run registers `selenium-<id>@example.com`.

## What is covered

The suite drives `http://localhost:3000` only (the UI proxies `/api`):

1. App shell loads and the browser console has no fatal error.
2. Register, log out, and log in with a unique email.
3. Create an organization, a project, and a versioned benchmark.
4. Add a Java task, save a visible JUnit suite (`mvn -B -q test`), and publish the version.
5. Register a **FAKE** agent and enqueue an evaluation. The test passes when the detail view reaches `COMPLETED`, `FAILED`, `CANCELLED`, or `TIMED_OUT`, or when a stage past `QUEUED` is visible. The terminal state is printed as `EVAL_STATE`.
6. Overview shows evaluation activity, and the org leaderboard page loads (empty is valid when nothing has been scored yet).
7. **Export CSV** on the org leaderboard returns HTTP 200 or a downloaded `.csv`.
8. OWNER publishes the board, the public page opens, an up vote is counted, and a comment is visible.
9. The Ops health panel loads.

Failure screenshots and page HTML are written under `reports/` (gitignored).

## Out of scope

- Oracle Cloud Always Free, live AWS, S3/MinIO, and Object Lock / Compliance retention.
- Firecracker, gVisor, and any claim that Docker isolation ran. The default worker image does not mount the Docker socket; the worker falls back to a local process. A `FAILED` evaluation whose UI still reaches a terminal state is a passed UI flow, not a skipped test. `COMPLETED` needs a Maven-capable runner (Docker socket on the worker, or Maven on that machine).
- Python (pytest) and Node (Jest) benchmark paths, and the optional ruff/eslint images. Only the Java/JUnit task path is created.
- OIDC, SAML/SSO, paid model providers, hidden-suite confidentiality checks, embed signing, and compare/cost charts beyond the pages above.

## Adding a test for a new feature

Add one ordered method to `tests/test_regression.py` (prefix `test_NN_`) that reuses the session `driver` and `ctx`, calls `_require` for the flags it depends on, and drives the new control through the real UI at `AGENTBENCH_BASE_URL`. Find forms by their `h3` and fields by the label text, wait until the new success text is visible, and fail on `p.error` instead of sleeping. Use a unique email, slug, and comment from `ctx` so a second run does not collide with data left in Postgres. If the feature needs Oracle, Firecracker, live AWS, or a Python/Node runner image, do not pretend the default Compose stack covers it: guard the test with an explicit environment opt-in and document the skip in this section. Keep the new step in the same single-process order; later steps should `_require` the new flag so a broken feature stays one failure rather than a cascade.
