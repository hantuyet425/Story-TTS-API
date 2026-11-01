from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

import edge_tts
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("story-tts-api")

BASE_DIR = Path(__file__).resolve().parent
from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.getenv("TTS_DATA_DIR", str(BASE_DIR / "data"))).expanduser().resolve()
AUDIO_DIR = DATA_DIR / "audio"
DB_PATH = DATA_DIR / "jobs.sqlite3"

VOICES: dict[str, str] = {
    "vi-VN-NamMinhNeural": "Nam Minh",
    "vi-VN-HoaiMyNeural": "Hoài My",
}
RATES = (0.75, 1.0, 1.25, 1.5, 2.0)
MAX_QUEUED_JOBS = max(1, int(os.getenv("MAX_QUEUED_JOBS", "50")))
AUDIO_URL_TTL_SECONDS = max(60, int(os.getenv("AUDIO_URL_TTL_SECONDS", "86400")))
TTS_CHUNK_CHAR_LIMIT = max(500, int(os.getenv("TTS_CHUNK_CHAR_LIMIT", "5000")))
def parallel_chunk_limit(value: str | int | None = None) -> int:
    """Return a safe configured per-job concurrency, clamped to 1..100."""
    raw = os.getenv("MAX_PARALLEL_CHUNKS", "100") if value is None else value
    return min(100, max(1, int(raw)))


MAX_PARALLEL_CHUNKS = parallel_chunk_limit()
MAX_INPUT_CHARACTERS = max(5000, int(os.getenv("MAX_INPUT_CHARACTERS", "5000000")))
QUEUE_POLL_SECONDS = 1.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def database() -> Iterator[sqlite3.Connection]:
    """Open a short-lived SQLite connection and always close it."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    try:
        with con:
            yield con
    finally:
        con.close()


def init_database() -> None:
    with database() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                voice TEXT NOT NULL,
                rate REAL NOT NULL,
                chapter_id TEXT,
                cache_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT,
                audio_path TEXT,
                source_text TEXT,
                cache_hit INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_cache_key ON jobs(cache_key)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id)")
        columns = {row["name"] for row in con.execute("PRAGMA table_info(jobs)").fetchall()}
        if "chunk_count" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 0")
        if "completed_chunks" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN completed_chunks INTEGER NOT NULL DEFAULT 0")
        con.execute(
            """UPDATE jobs
               SET status='queued', updated_at=?, chunk_count=0, completed_chunks=0
               WHERE status='generating'""",
            (utc_now(),),
        )


def valid_rate(rate: float) -> bool:
    return any(abs(rate - supported) < 1e-9 for supported in RATES)


def build_cache_key(text: str, voice: str, rate: float) -> str:
    material = json.dumps(
        {"text": text, "voice": voice, "rate": rate},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def api_key() -> str:
    return os.getenv("TTS_API_KEY", "").strip()


async def require_api_key(authorization: str | None = Header(default=None)) -> None:
    expected = api_key()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="API chưa được cấu hình. Hãy đặt biến môi trường TTS_API_KEY.",
        )
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="API key không hợp lệ hoặc bị thiếu.")


def audio_secret() -> bytes:
    secret = os.getenv("AUDIO_URL_SECRET", "").strip() or api_key()
    if not secret:
        # Signed URLs are not issued unless an authenticated API request is possible.
        secret = "not-configured"
    return secret.encode("utf-8")


def sign_audio_url(job_id: int, expires: int) -> str:
    message = f"{job_id}:{expires}".encode("ascii")
    return hmac.new(audio_secret(), message, hashlib.sha256).hexdigest()


def audio_links(request: Request, job_id: int) -> tuple[str | None, str | None]:
    row = fetch_job(job_id)
    if row is None or row["status"] != "completed" or not row["audio_path"]:
        return None, None
    if not Path(row["audio_path"]).is_file():
        return None, None
    expires = int(time.time()) + AUDIO_URL_TTL_SECONDS
    token = sign_audio_url(job_id, expires)
    base = str(request.base_url).rstrip("/")
    audio = f"{base}/v1/jobs/{job_id}/audio?expires={expires}&token={token}"
    return audio, f"{audio}&download=1"


def fetch_job(job_id: int) -> sqlite3.Row | None:
    with database() as con:
        return con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def insert_job(
    *,
    status: str,
    voice: str,
    rate: float,
    chapter_id: str | None,
    cache_key: str,
    source_text: str | None,
    audio_path: str | None = None,
    cache_hit: bool = False,
) -> int:
    now = utc_now()
    with database() as con:
        cursor = con.execute(
            """
            INSERT INTO jobs
                (status, voice, rate, chapter_id, cache_key, created_at, updated_at,
                 audio_path, source_text, cache_hit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                status,
                voice,
                rate,
                chapter_id,
                cache_key,
                now,
                now,
                audio_path,
                source_text,
                int(cache_hit),
            ),
        )
        return int(cursor.lastrowid)


def set_job(
    job_id: int,
    status: str,
    *,
    error: str | None = None,
    audio_path: str | None = None,
    clear_text: bool = False,
    chunk_count: int | None = None,
    completed_chunks: int | None = None,
) -> None:
    with database() as con:
        con.execute(
            """
            UPDATE jobs
            SET status=?, updated_at=?, error=?, audio_path=COALESCE(?, audio_path),
                source_text=CASE WHEN ? THEN NULL ELSE source_text END,
                chunk_count=COALESCE(?, chunk_count),
                completed_chunks=COALESCE(?, completed_chunks)
            WHERE id=?
            """,
            (status, utc_now(), error, audio_path, int(clear_text),
             chunk_count, completed_chunks, job_id),
        )


def public_job(row: sqlite3.Row, request: Request, *, reused: bool = False) -> dict[str, Any]:
    job_id = int(row["id"])
    status = str(row["status"])
    audio_url, download_url = audio_links(request, job_id) if status == "completed" else (None, None)
    return {
        "job_id": job_id,
        "chapter_id": row["chapter_id"],
        "status": status,
        "voice": row["voice"],
        "voice_name": VOICES.get(row["voice"], row["voice"]),
        "rate": float(row["rate"]),
        "cache_hit": bool(row["cache_hit"]),
        "reused_request": reused,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "status_url": str(request.base_url).rstrip("/") + f"/v1/jobs/{job_id}",
        "audio_url": audio_url,
        "download_url": download_url,
        "error": row["error"],
        "chunk_count": int(row["chunk_count"] or 0),
        "completed_chunks": int(row["completed_chunks"] or 0),
        "progress_percent": (
            100 if status == "completed" else
            round(100 * int(row["completed_chunks"] or 0) / int(row["chunk_count"]))
            if int(row["chunk_count"] or 0) else 0
        ),
    }


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_INPUT_CHARACTERS)
    voice: str = "vi-VN-NamMinhNeural"
    rate: float = 1.0
    chapter_id: str | None = Field(default=None, max_length=300)




def split_text_for_tts(text: str, max_chars: int = TTS_CHUNK_CHAR_LIMIT) -> list[str]:
    """Split text into bounded segments without losing or reordering characters."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if not text:
        return []
    parts: list[str] = []
    start = 0
    length = len(text)
    preferred = ("\n\n", "\n", "。", "！", "？", "!", "?", ".", "；", ";", "，", ",", " ")
    while start < length:
        hard_end = min(start + max_chars, length)
        if hard_end == length:
            end = length
        else:
            min_end = start + max(1, int(max_chars * 0.60))
            window = text[min_end:hard_end]
            boundaries = []
            for marker in preferred:
                pos = window.rfind(marker)
                if pos >= 0:
                    boundaries.append(min_end + pos + len(marker))
            end = max(boundaries) if boundaries else hard_end
        if end <= start:
            end = hard_end
        parts.append(text[start:end])
        start = end
    return parts

async def synthesize_to_file(text: str, voice: str, rate: float, output: Path) -> None:
    edge_percent = round((rate - 1.0) * 100)
    edge_rate = f"{edge_percent:+d}%"
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + ".partial" + output.suffix)
    last_error: Exception | None = None

    for attempt in range(1, 4):
        partial.unlink(missing_ok=True)
        try:
            communicator = edge_tts.Communicate(text=text, voice=voice, rate=edge_rate)
            await communicator.save(str(partial))
            if not partial.is_file() or partial.stat().st_size < 128:
                raise RuntimeError("Edge-TTS không trả về tệp MP3 hợp lệ.")
            os.replace(partial, output)
            return
        except Exception as exc:
            last_error = exc
            partial.unlink(missing_ok=True)
            if attempt < 3:
                delay = 2 ** attempt
                logger.warning(
                    "TTS job retry %s/3 after %ss: %s",
                    attempt,
                    delay,
                    str(exc)[:500],
                )
                await asyncio.sleep(delay)

    raise RuntimeError(f"Tổng hợp thất bại sau 3 lần thử: {last_error}") from last_error


async def worker_loop(queue: asyncio.Queue[int]) -> None:
    """Process one AU at a time, with a bounded parallel pool for its segments."""
    while True:
        job_id = await queue.get()
        try:
            row = fetch_job(job_id)
            if row is None or row["status"] != "queued":
                continue
            source_text = row["source_text"]
            if not source_text or not source_text.strip():
                set_job(job_id, "failed", error="Không còn nội dung để tổng hợp.", clear_text=True)
                continue

            key = str(row["cache_key"])
            output = AUDIO_DIR / f"{key}.mp3"
            with database() as con:
                cached = con.execute(
                    """SELECT audio_path FROM jobs
                       WHERE cache_key=? AND status='completed' AND audio_path IS NOT NULL
                       ORDER BY id DESC LIMIT 1""",
                    (key,),
                ).fetchone()
            if cached and Path(cached["audio_path"]).is_file():
                set_job(job_id, "completed", audio_path=str(cached["audio_path"]),
                        clear_text=True, chunk_count=1, completed_chunks=1)
                continue

            chunks = split_text_for_tts(str(source_text), TTS_CHUNK_CHAR_LIMIT)
            if not chunks:
                raise RuntimeError("Không tách được nội dung thành các phần audio.")
            set_job(job_id, "generating", error=None, chunk_count=len(chunks), completed_chunks=0)

            # A job uses up to MAX_PARALLEL_CHUNKS simultaneous Edge-TTS segment syntheses.
            job_dir = AUDIO_DIR / f".job-{job_id}"
            if job_dir.exists():
                import shutil
                shutil.rmtree(job_dir, ignore_errors=True)
            job_dir.mkdir(parents=True, exist_ok=True)
            semaphore = asyncio.Semaphore(MAX_PARALLEL_CHUNKS)
            progress_lock = asyncio.Lock()
            completed_count = 0
            part_paths = [job_dir / f"part-{i:05d}.mp3" for i in range(len(chunks))]

            async def synthesize_part(index: int, text_part: str) -> Path:
                nonlocal completed_count
                async with semaphore:
                    await synthesize_to_file(
                        text_part, str(row["voice"]), float(row["rate"]), part_paths[index]
                    )
                async with progress_lock:
                    completed_count += 1
                    set_job(job_id, "generating",
                            chunk_count=len(chunks), completed_chunks=completed_count)
                return part_paths[index]

            try:
                outcomes = await asyncio.gather(
                    *(synthesize_part(i, part) for i, part in enumerate(chunks)),
                    return_exceptions=True,
                )
                failures = [(i + 1, result) for i, result in enumerate(outcomes)
                            if isinstance(result, BaseException)]
                if failures:
                    index, error = failures[0]
                    raise RuntimeError(f"Phần {index}/{len(chunks)} thất bại: {error}") from error

                # edge-tts emits the same raw CBR MP3 frame format for all segments.
                # Concatenate in source order, never in completion order.
                partial_output = output.with_name(output.stem + ".partial.mp3")
                partial_output.unlink(missing_ok=True)
                with partial_output.open("wb") as merged:
                    for part_path in part_paths:
                        if not part_path.is_file() or part_path.stat().st_size < 32:
                            raise RuntimeError(f"Thiếu tệp MP3 tạm: {part_path.name}")
                        with part_path.open("rb") as part_file:
                            while block := part_file.read(1024 * 1024):
                                merged.write(block)
                if partial_output.stat().st_size < 128:
                    raise RuntimeError("Tệp MP3 ghép cuối cùng rỗng hoặc không hợp lệ.")
                os.replace(partial_output, output)
                set_job(job_id, "completed", audio_path=str(output), error=None,
                        clear_text=True, chunk_count=len(chunks), completed_chunks=len(chunks))
            finally:
                import shutil
                shutil.rmtree(job_dir, ignore_errors=True)
                output.with_name(output.stem + ".partial.mp3").unlink(missing_ok=True)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("TTS job %s failed", job_id)
            try:
                set_job(job_id, "failed",
                        error=(str(exc) or exc.__class__.__name__)[:1500],
                        clear_text=True)
            except Exception:
                logger.exception("Unable to save failure for TTS job %s", job_id)
        finally:
            queue.task_done()


@asynccontextmanager
async def lifespan(application: FastAPI):
    init_database()
    queue: asyncio.Queue[int] = asyncio.Queue()
    with database() as con:
        pending_ids = con.execute(
            "SELECT id FROM jobs WHERE status='queued' ORDER BY id"
        ).fetchall()
    for row in pending_ids:
        queue.put_nowait(int(row["id"]))
    application.state.job_queue = queue
    worker_task = asyncio.create_task(worker_loop(queue), name="story-tts-worker")
    application.state.worker_task = worker_task
    try:
        yield
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Story TTS API",
    description="Vietnamese story chapter to MP3: Nam Minh, Hoài My and configurable reading speed.",
    version="1.0.0",
    lifespan=lifespan,
)

origins_raw = os.getenv("ALLOWED_ORIGINS", "").strip()
allowed_origins = [origin.strip() for origin in origins_raw.split(",") if origin.strip()]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "Story TTS API",
        "docs": "/docs",
        "health": "/health",
        "supported_voices": VOICES,
        "supported_rates": list(RATES),
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "story-tts-api"}


@app.get("/v1/voices", dependencies=[Depends(require_api_key)])
async def voices() -> dict[str, Any]:
    return {
        "voices": [{"id": voice_id, "name": name} for voice_id, name in VOICES.items()],
        "rates": list(RATES),
        "format": "audio/mpeg",
    }


@app.post("/v1/tts", status_code=202, dependencies=[Depends(require_api_key)])
async def create_tts_job(payload: TTSRequest, request: Request) -> JSONResponse:
    text = payload.text
    if not text.strip():
        raise HTTPException(status_code=422, detail="Nội dung chương không được để trống.")
    if payload.voice not in VOICES:
        raise HTTPException(
            status_code=422,
            detail=f"Giọng đọc không hợp lệ. Chọn một trong: {', '.join(VOICES)}",
        )
    if not valid_rate(payload.rate):
        raise HTTPException(status_code=422, detail=f"Tốc độ phải thuộc: {', '.join(map(str, RATES))}")

    cache_key = build_cache_key(text, payload.voice, payload.rate)
    queue: asyncio.Queue[int] = request.app.state.job_queue

    with database() as con:
        pending = con.execute(
            """
            SELECT * FROM jobs
            WHERE cache_key=? AND status IN ('queued', 'generating')
            ORDER BY id ASC LIMIT 1
            """,
            (cache_key,),
        ).fetchone()
        cached = con.execute(
            """
            SELECT * FROM jobs
            WHERE cache_key=? AND status='completed' AND audio_path IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (cache_key,),
        ).fetchone()
        waiting_count = int(
            con.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','generating')").fetchone()[0]
        )

    if pending is not None:
        return JSONResponse(
            status_code=202,
            content=public_job(pending, request, reused=True),
        )

    if cached is not None and Path(cached["audio_path"]).is_file():
        new_id = insert_job(
            status="completed",
            voice=payload.voice,
            rate=payload.rate,
            chapter_id=payload.chapter_id,
            cache_key=cache_key,
            source_text=None,
            audio_path=str(cached["audio_path"]),
            cache_hit=True,
        )
        return JSONResponse(status_code=200, content=public_job(fetch_job(new_id), request))

    if waiting_count >= MAX_QUEUED_JOBS:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "30"},
            content={"detail": "Hàng đợi đang đầy. Hãy thử lại sau.", "max_queued_jobs": MAX_QUEUED_JOBS},
        )

    job_id = insert_job(
        status="queued",
        voice=payload.voice,
        rate=payload.rate,
        chapter_id=payload.chapter_id,
        cache_key=cache_key,
        source_text=text,
    )
    queue.put_nowait(job_id)
    return JSONResponse(status_code=202, content=public_job(fetch_job(job_id), request))


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
async def get_job_status(job_id: int, request: Request) -> dict[str, Any]:
    row = fetch_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy tác vụ audio.")
    return public_job(row, request)


@app.get("/v1/jobs/{job_id}/audio", name="serve_audio")
async def serve_audio(
    job_id: int,
    expires: int = Query(...),
    token: str = Query(..., min_length=32, max_length=128),
    download: bool = Query(False),
) -> FileResponse:
    if expires < int(time.time()):
        raise HTTPException(status_code=403, detail="Liên kết audio đã hết hạn. Hãy tải lại trạng thái tác vụ.")
    expected = sign_audio_url(job_id, expires)
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Chữ ký liên kết audio không hợp lệ.")
    row = fetch_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy tác vụ audio.")
    if row["status"] != "completed" or not row["audio_path"]:
        raise HTTPException(status_code=409, detail="Audio chưa sẵn sàng.")
    path = Path(row["audio_path"])
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Tệp audio đã bị xóa khỏi bộ nhớ cache.")
    disposition = "attachment" if download else "inline"
    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename=f"chapter-{job_id}.mp3",
        content_disposition_type=disposition,
        headers={"Cache-Control": "private, max-age=300"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        workers=1,
    )
