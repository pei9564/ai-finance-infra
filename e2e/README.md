# Browser E2E tests

Playwright Chromium tests cover the real STT and scraper UIs, FastAPI routes,
PostgreSQL repositories, and background workers. Downloads, RSS parsing, Whisper,
and LLM inference use deterministic fixtures. No GPU, external APIs, or secrets
are needed. The integration flows in `flows.spec.ts` use real browser requests.
`reading-ui.spec.ts` and `stt-ui.spec.ts` use browser-routed fixtures for empty,
error, mobile, and response-race scenarios without mutating application data.

Coverage:

- Required input validation, file upload, transcript retrieval after reload.
- URL and RSS submission, job list and transcript selection.
- Failed upload retains audio, then succeeds through the retry button.
- Cancel queued jobs without cancelling the running transcription.
- Abrupt STT process death recovers running uploads and queued URLs from PG.
- Scraper media → STT HTTP → translation → summary → bookmark → delete,
  with persistence verified after browser reload.
- Reading workspace search, filters, empty states, load recovery, and mobile navigation.
- STT task search and filters, keyboard tabs, mobile layout, TXT download content,
  clipboard fallback, duplicate-submit prevention, stale-response protection,
  network recovery, and deletion confirmation.
- Timeline parsing/search/copy, text search navigation, reload recovery, missing
  jobs and corrupt local storage, upload progress/abort/error states, and opt-in
  completion notifications. Upload event timing uses a controlled transport;
  integration flows still exercise real multipart uploads.
- All three transcript AI note types, independent exports and PG persistence
  across an STT restart, plus article → transcript → reading workspace navigation.
- Unexpected browser JavaScript exceptions fail the test.

## Run locally

Prerequisites: Docker daemon, Node.js 20+, Python 3.12+, uv, and sibling repos:

```text
workspace/
  ai-finance-infra/
  finance-scraper-service/
  stt-ai-worker/
```

```bash
cd ai-finance-infra/e2e
npm ci
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.lock
npx playwright install --with-deps chromium
npm test
```

`serve.py` creates a uniquely named PostgreSQL container with a random localhost
port. It ignores application `.env` credentials and never connects to an existing
DB. Each test restarts the apps and reseeds this dedicated DB. Cleanup removes
the test container, children and temporary uploaded audio.

Default localhost ports: scraper 18797, STT 18798, controller 18799.
Override `E2E_SCRAPER_PORT`, `E2E_STT_PORT`, `E2E_CONTROL_PORT` if occupied.
`E2E_PYTHON` can select a different Python executable. Tests run with one worker
because they intentionally share and reset one isolated stack.

To inspect failures:

```bash
npm run report
npx playwright show-trace test-results/<test>/trace.zip
```

Screenshots, videos, traces, and server logs are retained under `test-results/`.
These tests verify integration and UI behavior, not actual Whisper/LLM quality,
YouTube download compatibility, or Kubernetes deployment.

## CI

`.github/workflows/e2e.yml` checks out all three repos side by side, installs the
locked Python/Node dependencies, and runs the same tests. Application repos must
contain the fixes before the infra workflow runs; push their commits first.
Workflow dispatch accepts `scraper_ref` and `stt_ref` for testing specific branches
or commits. Automatic runs use their `main` branches.

The seven browser scenarios also verify transcript downloads, source path privacy,
original text/provenance export after translation and summarization, duplicate
upload reuse, metrics/diagnostics, and invalid batch limits. Errors are asserted
using public actionable messages rather than raw model exceptions.
