"""Isolated browser E2E stack: real HTTP/PG/queue, fake download and models.

This controller stays in the test repo and is never shipped in app images.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent.parent
sys.path[:0] = [str(WORKSPACE / "stt-ai-worker"), str(WORKSPACE / "finance-scraper-service/src")]
STT_PORT = int(os.environ.get("E2E_STT_PORT", "18798"))
SCRAPER_PORT = int(os.environ.get("E2E_SCRAPER_PORT", "18797"))
CONTROL_PORT = int(os.environ.get("E2E_CONTROL_PORT", "18799"))
TRANSCRIPT = "Earnings increased by 20 percent. Demand remains strong."
TRANSLATION = "營收成長百分之二十，需求持續強勁。"
SUMMARY = "營收成長 20%，需求持續強勁。"


def wait_ready(url, process=None, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"service exited: {process.returncode}; inspect test-results/*.log")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, TimeoutError):
            time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {url}")


def stt_app():
    from app.config import load_settings
    from app.domain.transcript import TranscriptResult, TranscriptSegment
    from app.infrastructure.audio_sources import DirectUrlSource, FeedEpisode, YtDlpSource
    from app.api import routes
    from app.main import create_app

    @contextmanager
    def audio(source):
        # Preserve source objects to exercise PG serialization/reconstruction.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "audio.mp3"
            path.write_bytes(b"fixture")
            yield path
    YtDlpSource.acquire = audio
    DirectUrlSource.acquire = audio
    routes.parse_feed = lambda url, limit: [FeedEpisode("Fixture episode", "https://fixture.invalid/episode.mp3")][:limit or None]

    class FixtureTranscriber:
        def __init__(self):
            self.failed = set()
        def transcribe(self, path, language, batch_size):
            content = path.read_bytes()
            if content == b"fail-once" and str(path) not in self.failed:
                self.failed.add(str(path))
                raise RuntimeError("Fixture temporary inference failure")
            if content == b"hold":
                while Path(os.environ["E2E_HOLD_MARKER"]).exists():
                    time.sleep(0.05)
            time.sleep(0.1)
            return TranscriptResult("en", 12.5, (TranscriptSegment(0, 12.5, TRANSCRIPT),))
    return create_app(load_settings(), transcriber=FixtureTranscriber())


def scraper_app():
    from finance_scraper import server
    from finance_scraper.domain import AIConfig
    from finance_scraper.domain.config import SummaryProfile
    from finance_scraper.infrastructure import STTClient
    import finance_scraper.infrastructure as infrastructure

    class FastSTTClient(STTClient):
        def __init__(self):
            super().__init__(poll_interval_seconds=0.1, timeout_seconds=15)
    infrastructure.STTClient = FastSTTClient

    class FixtureLLM:
        def chat(self, prompt, max_tokens=600, **kwargs):
            if "英文標題" in prompt:
                return "測試財報"
            if "英文文章內文" in prompt:
                return TRANSLATION
            return SUMMARY
    async def load_llm():
        return FixtureLLM(), SimpleNamespace(ai=AIConfig(llm_call_delay_seconds=0, summary_profiles={"youtube_fin": SummaryProfile(instruction="摘要逐字稿")}))
    server._load_llm = load_llm
    return server.app


async def seed():
    from finance_scraper.storage import close_pool, daily, init_schema
    await init_schema()
    now = datetime.now(timezone.utc)
    await daily.upsert_run(now.date().isoformat(), "Fixture daily summary", [{
        "uid": "e2e-media", "source": "youtube_fin", "type": "Video",
        "title": "Fixture earnings", "url": "https://fixture.invalid/earnings",
        "created_at": now.isoformat(), "feed_name": "E2E Finance", "impact_level": "Low",
        "body": "", "body_lang": None, "ai_summary": "",
    }])
    await close_pool()


def supervise():
    import psycopg
    import uvicorn
    from fastapi import FastAPI

    container = f"finance-e2e-{uuid.uuid4().hex[:12]}"
    children = {}
    logs = []
    folder = tempfile.TemporaryDirectory(prefix="finance-e2e-")
    marker = Path(folder.name) / "hold"
    uploads = Path(folder.name) / "uploads"
    uploads.mkdir()
    artifacts = HERE / "test-results"
    artifacts.mkdir(exist_ok=True)

    def launch(name):
        log = (artifacts / f"{name}-server.log").open("a")
        logs.append(log)
        children[name] = subprocess.Popen([sys.executable, str(__file__), name], env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT)
        wait_ready(f"http://127.0.0.1:{STT_PORT if name == 'stt' else SCRAPER_PORT}/readyz", children[name])

    def stop(name, force=False):
        child = children.pop(name, None)
        if child and child.poll() is None:
            child.kill() if force else child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    controller = FastAPI()

    @controller.get("/readyz")
    def ready():
        return {"status": "ready"}

    @controller.post("/reset")
    def reset():
        stop("scraper")
        stop("stt", force=True)
        marker.unlink(missing_ok=True)
        with psycopg.connect(os.environ["DB_URL"]) as conn:
            conn.execute("TRUNCATE jobs, transcripts, events, daily_runs, bookmarks, monthly_digests RESTART IDENTITY CASCADE")
        for path in uploads.iterdir():
            path.unlink()
        asyncio.run(seed())
        launch("stt")
        launch("scraper")
        return {"status": "reset"}

    @controller.post("/hold")
    def hold():
        marker.touch()
        return {"status": "holding"}

    @controller.post("/release")
    def release():
        marker.unlink(missing_ok=True)
        return {"status": "released"}

    @controller.post("/restart-stt")
    def restart():
        stop("stt", force=True)  # actual process death, not an API mock
        marker.unlink(missing_ok=True)
        launch("stt")
        return {"status": "restarted"}

    @controller.get("/state")
    def state():
        with psycopg.connect(os.environ["DB_URL"]) as conn:
            transcripts = conn.execute("SELECT count(*) FROM transcripts").fetchone()[0]
            jobs = conn.execute("SELECT id, status FROM jobs ORDER BY created_at").fetchall()
        return {"transcripts": transcripts, "jobs": jobs, "uploads": len(list(uploads.iterdir()))}

    try:
        subprocess.run(["docker", "run", "--detach", "--rm", "--name", container,
                        "-e", "POSTGRES_USER=e2e", "-e", "POSTGRES_PASSWORD=e2e",
                        "-e", "POSTGRES_DB=e2e", "-p", "127.0.0.1::5432", "postgres:15-alpine"], check=True, stdout=subprocess.DEVNULL)
        binding = subprocess.check_output(["docker", "port", container, "5432/tcp"], text=True).strip()
        dsn = f"postgresql://e2e:e2e@{binding}/e2e"
        os.environ.update(DB_URL=dsn, PG_DSN=dsn, DEVICE="cpu", MODEL="fixture", UPLOAD_DIR=str(uploads),
                          E2E_HOLD_MARKER=str(marker), STT_BASE_URL=f"http://127.0.0.1:{STT_PORT}", STT_LANGUAGE="", RETENTION_DAYS="7")
        deadline = time.monotonic() + 30
        while True:
            try:
                with psycopg.connect(dsn, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        launch("stt")
        asyncio.run(seed())
        launch("scraper")
        uvicorn.run(controller, host="127.0.0.1", port=CONTROL_PORT, log_level="warning")
    finally:
        for name in list(children):
            stop(name, force=True)
        subprocess.run(["docker", "rm", "--force", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for log in logs:
            log.close()
        folder.cleanup()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "supervisor"
    if mode == "supervisor":
        def interrupted(signum, frame):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        supervise()
    else:
        import uvicorn
        uvicorn.run(stt_app() if mode == "stt" else scraper_app(), host="127.0.0.1", port=STT_PORT if mode == "stt" else SCRAPER_PORT, log_level="warning")
