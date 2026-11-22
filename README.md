# AgentBench Selenium regression

Python 3 + Selenium 4 + pytest drive the real AgentBench UI in headless Chrome. This is AgentBench *application* regression (platform-scoped results), not a per-consumer suite. The suite does not live in the application repo. It expects a Compose stack started from [Sai-333/agentbench](https://github.com/Sai-333/agentbench) (verified against `main` at `12228c5`).

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

Every `pytest` run seeds [fixtures/mock-data.json](fixtures/mock-data.json) through the API before any case. The seed is idempotent: fixed emails and slugs are registered or reused, projects and benchmarks are created or updated, the published Java task is reused, and the public board's comments are reset to the fixture rows. A second run does not invent new users. The same seed registers `selenium-viewer@example.com` as a VIEWER, keeps a draft benchmark with a visible suite and a hidden suite, publishes then unpublishes `selenium-edge-unpublished`, upserts one flake quarantine for `maven-junit.runner`, reuses one platform regression run (`selenium-mock` at git SHA `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`), registers `selenium-tenant@example.com` as the only member of `selenium-other-org`, and registers `selenium-developer@example.com` as a DEVELOPER. The public board also keeps an up vote for `seleniumclickvoter01`.

To seed without opening Chrome:

```bash
python tests/seed.py
```

That command uses `AGENTBENCH_API_URL` (default `http://localhost:8080`) and prints the seeded email, slugs, and public-board vote tally. It does not print the mock password.

Pytest waits up to `AGENTBENCH_STARTUP_TIMEOUT` seconds (default 180) for those three checks, seeds, then opens Chrome. Tests share one browser session and must run in definition order in a single process. Do not shard or randomize them.

Headless Chrome is the default (`--headless=new`, `--no-sandbox`, `--disable-dev-shm-usage`). Set `HEADLESS=0` to watch a local browser. `CHROME_BIN` overrides the Chrome binary (default `/usr/local/bin/google-chrome`). Selenium Manager downloads a matching ChromeDriver on first run.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `AGENTBENCH_BASE_URL` | `http://localhost:3000` | UI origin |
| `AGENTBENCH_API_URL` | `http://localhost:8080` | Health probes and the mock-data seed |
| `AGENTBENCH_STARTUP_TIMEOUT` | `180` | Seconds to wait for Compose before seeding |
| `AGENTBENCH_EVAL_TIMEOUT` | `240` | Seconds to wait for an evaluation to leave `QUEUED` or finish |
| `HEADLESS` | `1` | Set `0` to show Chrome |
| `CHROME_BIN` | `/usr/local/bin/google-chrome` | Browser binary |

No API keys, cloud credentials, or `.env` secrets are required. The signed-in owner is `selenium-mock@example.com` and the VIEWER is `selenium-viewer@example.com` (both use the mock password `password123`). Tests do not generate a unique email per run.

## What is covered

The suite drives `http://localhost:3000` only (the UI proxies `/api`). The seed creates the rows below; the cases sign in as that user and read them:

1. App shell loads and the browser console has no fatal error.
2. Log in as the seeded user, log out, and log in again.
3. The seeded organization, project, and published benchmark are visible, including the Java task and visible JUnit suite (`mvn -B -q test`).
4. The seeded **FAKE** agent is visible. The suite enqueues an evaluation against that agent and the seeded published task. The test passes when the detail view reaches `COMPLETED`, `FAILED`, `CANCELLED`, or `TIMED_OUT`, or when a stage past `QUEUED` is visible. The terminal state is printed as `EVAL_STATE`.
5. Overview shows evaluation activity, and the org leaderboard page loads (empty is valid when nothing has been scored yet).
6. **Export CSV** on the org leaderboard returns HTTP 200 or a downloaded `.csv`.
7. The seeded public board link opens. The fixture title, comment, and up-vote tally are visible.
8. The Ops health panel loads.
9. Top-bar **Regression runs** (AgentBench application / platform surface — not an org workspace tab) opens after the fixture run exists. The mock-seeded case rows show `mock-seed::test_passes` as passed and `mock-seed::test_fails` as failed.

`tests/test_regression_edges.py` then runs, still in the same browser session:

10. Blank names are blocked or rejected (`name: must not be blank`). Duplicate org slug, project, benchmark, agent, and version show the conflict text. An invalid version string shows the version pattern error.
11. The owner opens the draft task and sees both suites by framework. The hidden fixture text is not rendered.
12. Publishing a draft version that has no tasks shows `Cannot publish a version with no tasks`.
13. The newest evaluation's score panel shows an incomplete withheld total or a numeric total, the F2P/P2P/P2F/F2F line, and the seeded flake case marked quarantined. Creating a quarantine with no scope, a past `expiresAt`, an oversized reason, or the same case key again returns the API validation or conflict text.
14. Cancel on a non-terminal evaluation reaches `CANCELLED`, and the button is gone afterward.
15. Leaderboard JSON export returns HTTP 200, including the empty draft version. Compare with the agent selected and no scored rows shows the empty comparison state.
16. On the public board, an empty comment cannot be posted, a 2001-character comment is rejected, a vote value other than 1 or -1 is rejected, and the seeded tally does not change. The unpublished slug shows `Public leaderboard not found` and no community form.
17. Logging out while a project name is typed discards that name. Empty login is blocked by the form, a wrong password shows `Invalid credentials`, registering the seeded email shows `Email already registered`, and a short password shows the size error.
18. The VIEWER sees `Your role: VIEWER`, no create/enqueue/hidden-suite/unpublish controls, hidden content as `present (restricted)` without the fixture text, a read-only public board, a working CSV export, and `Insufficient role for this operation` when creating a quarantine.

`tests/test_regression_gaps.py` continues in the same session. Method names start at `test_23_` so they do not reuse `test_14_`:

- `test_23_owner_saml_admin_authorized`: the owner opens **SSO** and sees **Add SAML IdP**. The result is not an authorization failure. When the SAML service provider is disabled, the IdP is not stored.
- `test_24_saml_edit_disable_delete`: **Disable**, **Save IdP**, and **Delete** run only after an IdP row exists. Otherwise the case is skipped.
- `test_25_saml_registration_id_rejected`: a registration id that is not lowercase kebab-case is rejected and is not listed.
- `test_26_published_leaderboard_embed`: **Copy embed snippet** on the published org leaderboard shows an iframe for the seeded public slug. The embed page shows the fixture title and comment and not the hidden fixture text.
- `test_27_other_org_is_hidden`: `selenium-other-org` is absent from the mock owner's org list. That tenant does not see the mock project. Each session receives `Not a member of this organization` when it reads the other org's projects.
- `test_28_export_pdf`: **Export PDF** on the org leaderboard and on an evaluation detail.
- `test_29_list_filters`: evaluation search waits for the debounce, then leaderboard, comparison, and regression outcome filters.
- `test_30_costs_empty`: when no tokens or cost have been reported, the Costs tab shows the empty copy and a search shows the filtered empty state. When usage rows are already on screen, that empty copy is not required.
- `test_31_secret_and_fake_connection`: an org secret is stored without a live model key, and **Test connection** on the FAKE agent reports ready.
- `test_32_theme_toggle`: **Dark** and **Light** set `agentbench.theme` on the app shell and the public page.
- `test_33_public_vote_click`: the public **Down** and **Up** buttons use the seeded `seleniumclickvoter01` fingerprint and leave that vote up.
- `test_34_developer_role`: `selenium-developer@example.com` can open create and enqueue forms, sees the hidden-suite editor, and cannot manage SAML or create a quarantine.
- `test_35_oidc_hash_consumed`: a `/#access_token=...` fragment is consumed and does not leave a session.
- `test_42_members_list_and_admin_control`: the owner opens **Members**, sees the seeded viewer and developer, and sees an invite or role control. The case is skipped when that screen is not in the UI.
- `test_38_evaluation_bound_filters`: failure class, minimum and maximum weighted score, and created-time bounds on the evaluation list.
- `test_39_failed_cases_only`: **Failed cases only** on an evaluation that has test runs. The case is skipped when that checkbox is not rendered.
- `test_40_evaluation_stage_timeline`: evaluation detail shows Baseline, Model call, Patch, Tests, and Score. A queued run keeps every stage pending. The case is skipped when the timeline is not rendered.
- `test_41_session_refresh`: moving the stored access-token expiry into the refresh window makes **Refresh health** call `POST /api/v1/auth/refresh` and rotate the tokens. A rejected refresh token returns to `Your session ended. Sign in again to continue.` The case is skipped when **Refresh health** is not rendered.
- `test_42_members_list_and_admin_control`: the owner opens **Members**, sees the seeded viewer and developer, and sees an invite or role control. The case is skipped when that screen is not in the UI.
- `test_43_project_repository_form`: on the seeded project, save a non-GitHub repository URL, validate it, and create a snapshot. The case is skipped when that form is not on the Projects tab.
- `test_44_costs_when_reported`: the Costs tab shows the agent row and the visible uppercase token and by-agent labels when tokens or estimated cost were reported. If the screen is empty, the case enqueues a FAKE evaluation first. It is skipped only when that run still does not produce a row.
- `test_45_quality_findings`: an evaluation detail shows a Checkstyle, SpotBugs, or PMD finding when the run recorded one. The case is skipped when those findings never appear in the mock.

Failure screenshots and page HTML are written under `reports/` (gitignored).

## Out of scope

- Oracle Cloud Always Free, live AWS, S3/MinIO, and Object Lock / Compliance retention.
- Firecracker, gVisor, and any claim that Docker isolation ran. The default worker image does not mount the Docker socket; the worker falls back to a local process. A `FAILED` evaluation whose UI still reaches a terminal state is a passed UI flow, not a skipped test. `COMPLETED` needs a Maven-capable runner (Docker socket on the worker, or Maven on that machine).
- Python (pytest) and Node (Jest) benchmark paths, and the optional ruff/eslint images. Only the Java/JUnit task path is created.
- OIDC, live SAML login, paid model providers, agent-facing hidden-suite APIs, and cost charts. The viewer task page's hidden-suite redaction is covered. The owner SSO form is covered. **Disable**, **Save IdP**, and **Delete** run only when **Add SAML IdP** stores a row; default Compose leaves the SAML service provider disabled, so those buttons are skipped. There is no GPU screen and no GPU case.

## Adding a test for a new feature

Add the row the case needs to `fixtures/mock-data.json` and teach `tests/seed.py` to upsert it. Then add one ordered method to `tests/test_regression.py` (prefix `test_NN_`) that reuses the session `driver` and `ctx`, calls `_require` for the flags it depends on, and drives the new control through the real UI at `AGENTBENCH_BASE_URL`. Find forms by their `h3` and fields by the label text, wait until the new success text is visible, and fail on `p.error` instead of sleeping. Read emails, slugs, and comments from `ctx` (the fixture). Do not invent a unique email inside a test. If the feature needs Oracle, Firecracker, live AWS, or a Python/Node runner image, do not pretend the default Compose stack covers it: guard the test with an explicit environment opt-in and document the skip in this section. Keep the new step in the same single-process order; later steps should `_require` the new flag so a broken feature stays one failure rather than a cascade.
