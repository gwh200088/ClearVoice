"""HTTP 接口层：同步降噪 / 异步任务 / 健康检查 / 运行状态 / 配置查看"""

from __future__ import annotations

import base64
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask

from app.core import audio_io, clearvoice_bridge
from app.core.engine import DenoiseEngine, ModelLoadError, ResourceBusy
from app.core.executor import CapacityExceeded, TaskExecutor
from app.core.resource import ResourceGovernor
from app.logging_setup import bind_request_id, get_request_id, reset_request_id
from app.schemas import TaskAccepted, TaskStatus

SERVICE_NAME = "clearvoice-denoise"
SERVICE_VERSION = "1.0.0"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


@dataclass
class Task:
    task_id: str
    filename: str
    ext: str
    size_bytes: int
    workdir: str
    result_path: Optional[str] = None
    status: str = STATUS_PENDING
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class TaskManager:
    def __init__(self, ttl_seconds: int, max_tasks: int, temp_root: str, logger):
        self._ttl = max(60, int(ttl_seconds))
        self._max_tasks = max(10, int(max_tasks))
        self._temp_root = temp_root
        self._logger = logger
        self._tasks: Dict[str, Task] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._cleaner = threading.Thread(target=self._clean_loop, name="cv-task-cleaner", daemon=True)
        self._cleaner.start()

    def create(self, filename: str, ext: str, size_bytes: int, workdir: str) -> Task:
        with self._lock:
            if len(self._tasks) >= self._max_tasks:
                self._evict_locked(force=True)
            task = Task(
                task_id=uuid.uuid4().hex,
                filename=filename,
                ext=ext,
                size_bytes=size_bytes,
                workdir=workdir,
            )
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, limit: int = 100) -> List[Task]:
        with self._lock:
            items = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
        return items[:limit]

    def delete(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.pop(task_id, None)
        if not task:
            return False
        safe_rmtree(task.workdir, self._temp_root, self._logger)
        return True

    def _evict_locked(self, force: bool = False) -> None:
        now = time.time()
        expired = [
            tid
            for tid, t in self._tasks.items()
            if force or (now - t.updated_at > self._ttl)
        ]
        if force and not expired:
            oldest = sorted(self._tasks.items(), key=lambda kv: kv[1].updated_at)[:1]
            expired = [oldest[0][0]] if oldest else []
        for tid in expired:
            task = self._tasks.pop(tid, None)
            if task:
                safe_rmtree(task.workdir, self._temp_root, self._logger)

    def _clean_loop(self) -> None:
        while not self._stop.wait(30.0):
            try:
                with self._lock:
                    self._evict_locked()
            except Exception as exc:  # pragma: no cover
                self._logger.warning("任务清理异常: %s", exc)

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            for task in list(self._tasks.values()):
                safe_rmtree(task.workdir, self._temp_root, self._logger)
            self._tasks.clear()


class ServiceContext:
    def __init__(self, cfg, logger):
        self.cfg = cfg
        self.logger = logger
        self.started_at = time.time()
        self.governor = ResourceGovernor(
            cfg=cfg.resource,
            device_kind="cuda" if cfg.runtime.device != "cpu" else "cpu",
            max_concurrency=cfg.concurrency.max_concurrency,
            logger=logger,
        )
        self.executor = TaskExecutor(
            max_workers=cfg.concurrency.max_concurrency,
            max_queue_size=cfg.concurrency.max_queue_size,
            logger=logger,
        )
        self.tasks = TaskManager(cfg.server.task_ttl_s, cfg.server.max_tasks, cfg.audio.temp_dir, logger)
        self.engine = DenoiseEngine(cfg, self.governor, logger)
        self.ready = False
        self.fatal_error: Optional[str] = None

    def start(self) -> None:
        try:
            self.engine.start()
            self.ready = True
            self.logger.info("服务就绪，监听 %s:%s", self.cfg.server.host, self.cfg.server.port)
        except Exception as exc:  # pragma: no cover
            self.fatal_error = str(exc)
            self.logger.exception("模型初始化失败: %s", exc)

    def stop(self) -> None:
        self.ready = False
        self.governor.shutdown()
        self.executor.shutdown(wait=False)
        self.tasks.shutdown()
        self.engine.stop()

    def uptime(self) -> float:
        return round(time.time() - self.started_at, 3)


# ---------------------------------------------------------------------------


def safe_rmtree(path: str, allowed_root: str, logger) -> None:
    """只删除 allowed_root 之下的目录。

    临时目录名虽由服务端生成，清理时仍要做边界校验：
    一旦路径因故变成空串或脱离工作根目录，绝不能顺着删下去。
    """
    if not path or not isinstance(path, str):
        return
    target = os.path.abspath(path)
    root = os.path.abspath(allowed_root)
    try:
        if os.path.commonpath([target, root]) != root:
            logger.warning("拒绝删除非工作目录: %s（允许范围 %s）", target, root)
            return
    except ValueError:
        logger.warning("无法校验待删除路径: %s", target)
        return
    shutil.rmtree(target, ignore_errors=True)


class Pipeline:
    """把一次降噪请求所需的工作目录管理与引擎调用封装起来"""

    def __init__(self, ctx: ServiceContext):
        self.ctx = ctx

    def prepare(self, filename: str) -> tuple:
        cfg = self.ctx.cfg
        ext = audio_io.normalize_extension(filename)
        allowed = [str(x).lower() for x in cfg.audio.allowed_formats]
        if ext not in allowed:
            raise HTTPException(
                status_code=415,
                detail=f"不支持的音频格式 .{ext}，当前仅支持 {allowed}",
            )
        os.makedirs(cfg.audio.temp_dir, exist_ok=True)
        workdir = os.path.join(cfg.audio.temp_dir, uuid.uuid4().hex)
        os.makedirs(workdir, exist_ok=True)
        in_path = os.path.join(workdir, f"input.{ext}")
        out_path = os.path.join(workdir, f"output.{ext}")
        return ext, workdir, in_path, out_path

    def run(
        self,
        in_path: str,
        out_path: str,
        ext: str,
        *,
        model: Optional[str] = None,
        auto_detect: Optional[bool] = None,
        bitrate: Optional[str] = None,
        sample_rate: Optional[int] = None,
        channels: Optional[int] = None,
    ) -> Dict[str, Any]:
        cfg = self.ctx.cfg
        try:
            return self.ctx.engine.denoise(
                in_path,
                out_path,
                ext,
                model=model,
                auto_detect=auto_detect,
                bitrate=bitrate,
                sample_rate=sample_rate,
                channels=channels,
                timeout_s=cfg.concurrency.queue_timeout_s,
            )
        except ResourceBusy as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ModelLoadError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except audio_io.AudioError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


def _response_headers(result: Dict[str, Any], elapsed_ms: int) -> Dict[str, str]:
    headers = {
        "X-Denoise-Applied": "true" if result.get("denoised") else "false",
        "X-Processing-Ms": str(elapsed_ms),
        "X-Request-Id": get_request_id(),
        "X-Model": str(result.get("model", "")),
    }
    detect = result.get("detect") or {}
    if detect.get("snr_db") is not None:
        headers["X-Noise-Snr-Db"] = str(detect["snr_db"])
    if result.get("skip_reason"):
        headers["X-Skip-Reason"] = str(result["skip_reason"])
    return headers


def build_app(cfg, logger) -> FastAPI:
    ctx = ServiceContext(cfg, logger)
    pipeline = Pipeline(ctx)
    api_key = cfg.server.api_key or ""
    temp_root = cfg.audio.temp_dir

    def _cleanup_dir(path: str) -> None:
        safe_rmtree(path, temp_root, logger)

    PUBLIC_PATHS = {"/", "/health", "/ready", "/docs", "/redoc", "/openapi.json"}

    def _check_auth(request: Request) -> None:
        if not api_key:
            return
        path = request.url.path.rstrip("/") or "/"
        if path in PUBLIC_PATHS or path.startswith("/docs") or path.startswith("/redoc"):
            return
        provided = request.headers.get("X-API-Key")
        if not provided:
            auth = request.headers.get("Authorization") or ""
            parts = auth.split()
            provided = parts[-1] if parts else None
        if provided != api_key:
            raise HTTPException(status_code=401, detail="API Key 无效")

    app = FastAPI(
        title="ClearerVoice 降噪服务",
        version=SERVICE_VERSION,
        description="基于 ClearerVoice-Studio 的语音降噪服务，支持并发与 GPU/CPU 资源准入控制",
        root_path=cfg.server.root_path or "",
    )
    app.state.ctx = ctx

    @app.middleware("http")
    async def _logging_middleware(request: Request, call_next):
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        token = bind_request_id(rid)
        start = time.time()
        status_code = 500
        try:
            _check_auth(request)
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-Id"] = rid
            return response
        except HTTPException as exc:
            status_code = exc.status_code
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail, "request_id": rid},
            )
        finally:
            cost = (time.time() - start) * 1000
            if cfg.logging.access_log:
                logger.info(
                    "%s %s -> %s 耗时=%.1fms",
                    request.method,
                    request.url.path,
                    status_code,
                    cost,
                )
            reset_request_id(token)

    @app.on_event("startup")
    def _on_startup() -> None:
        threading.Thread(target=ctx.start, name="cv-engine-start", daemon=True).start()

    @app.on_event("shutdown")
    def _on_shutdown() -> None:
        ctx.stop()

    # ---------------- 同步接口 ----------------

    @app.post(
        "/api/v1/denoise",
        summary="上传音频并降噪（同步）",
        responses={200: {"content": {"audio/mpeg": {}, "audio/wav": {}, "audio/mp4": {}}}},
    )
    async def denoise(
        request: Request,
        file: UploadFile = File(..., description="音频文件，支持 mp3 / wav / m4a"),
        model: Optional[str] = Form(None),
        auto_detect: Optional[bool] = Form(None),
        bitrate: Optional[str] = Form(None),
        sample_rate: Optional[int] = Form(None),
        channels: Optional[int] = Form(None),
        response_format: str = Form("binary", description="binary 返回音频字节；json 返回元信息"),
    ):
        _check_ready(ctx)
        _check_upload_size(request, cfg)
        ext, workdir, in_path, out_path = pipeline.prepare(file.filename or "input.wav")
        try:
            await _save_upload(file, in_path, cfg)
            started = time.monotonic()

            def job():
                return pipeline.run(
                    in_path,
                    out_path,
                    ext,
                    model=model,
                    auto_detect=auto_detect,
                    bitrate=bitrate,
                    sample_rate=sample_rate,
                    channels=channels,
                )

            result = _submit_and_wait(ctx, job, cfg.server.sync_timeout_s)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            result["elapsed_ms"] = elapsed_ms
            headers = _response_headers(result, elapsed_ms)
            logger.info("降噪完成 applied=%s result=%s", result.get("denoised"), result)

            if str(response_format).lower() == "json":
                payload = dict(result)
                with open(out_path, "rb") as fp:
                    payload["audio_base64"] = base64.b64encode(fp.read()).decode("ascii")
                payload["audio_format"] = ext
                _cleanup_dir(workdir)
                return JSONResponse(payload, headers=headers)
            return FileResponse(
                out_path,
                media_type=audio_io.content_type_of(ext),
                filename=_output_name(file.filename or f"input.{ext}", result.get("denoised", False)),
                headers=headers,
                background=BackgroundTask(_cleanup_dir, workdir),
            )
        except Exception:
            _cleanup_dir(workdir)
            raise

    @app.post("/api/v1/denoise/base64", summary="base64 音频降噪（同步）")
    async def denoise_base64(payload: dict, request: Request):
        _check_ready(ctx)
        workdir = ""
        try:
            raw = payload.get("audio_base64") or ""
            filename = payload.get("filename") or "input.wav"
            ext, workdir, in_path, out_path = pipeline.prepare(filename)
            data = base64.b64decode(raw, validate=False)
            if not data:
                raise HTTPException(status_code=400, detail="audio_base64 为空")
            if len(data) > cfg.server.max_upload_mb * 1024 * 1024:
                raise HTTPException(status_code=413, detail="音频文件过大")
            with open(in_path, "wb") as fp:
                fp.write(data)
            started = time.monotonic()

            def job():
                return pipeline.run(
                    in_path,
                    out_path,
                    ext,
                    model=payload.get("model"),
                    auto_detect=payload.get("auto_detect"),
                    bitrate=payload.get("bitrate"),
                    sample_rate=payload.get("sample_rate"),
                    channels=payload.get("channels"),
                )

            result = _submit_and_wait(ctx, job, cfg.server.sync_timeout_s)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            result["elapsed_ms"] = elapsed_ms
            headers = _response_headers(result, elapsed_ms)

            if str(payload.get("response_format", "binary")).lower() == "json":
                body = dict(result)
                with open(out_path, "rb") as fp:
                    body["audio_base64"] = base64.b64encode(fp.read()).decode("ascii")
                body["audio_format"] = ext
                _cleanup_dir(workdir)
                return JSONResponse(body, headers=headers)
            with open(out_path, "rb") as fp:
                content = fp.read()
            _cleanup_dir(workdir)
            return Response(content=content, media_type=audio_io.content_type_of(ext), headers=headers)
        except HTTPException:
            _cleanup_dir(workdir)
            raise
        except Exception as exc:
            _cleanup_dir(workdir)
            logger.exception("base64 降噪失败: %s", exc)
            raise HTTPException(status_code=500, detail=f"降噪失败: {exc}") from exc

    # ---------------- 异步任务接口 ----------------

    @app.post("/api/v1/tasks", summary="提交异步降噪任务", status_code=202, response_model=TaskAccepted)
    async def submit_task(
        request: Request,
        file: UploadFile = File(...),
        model: Optional[str] = Form(None),
        auto_detect: Optional[bool] = Form(None),
        bitrate: Optional[str] = Form(None),
        sample_rate: Optional[int] = Form(None),
        channels: Optional[int] = Form(None),
    ):
        _check_ready(ctx)
        _check_upload_size(request, cfg)
        ext, workdir, in_path, out_path = pipeline.prepare(file.filename or "input.wav")
        try:
            size = await _save_upload(file, in_path, cfg)
        except Exception:
            _cleanup_dir(workdir)
            raise
        task = ctx.tasks.create(file.filename or f"input.{ext}", ext, size, workdir)

        def job():
            task.status = STATUS_RUNNING
            task.updated_at = time.time()
            try:
                result = pipeline.run(
                    in_path,
                    out_path,
                    ext,
                    model=model,
                    auto_detect=auto_detect,
                    bitrate=bitrate,
                    sample_rate=sample_rate,
                    channels=channels,
                )
                task.result = result
                task.result_path = out_path
                task.status = STATUS_SUCCEEDED
            except HTTPException as exc:
                task.status = STATUS_FAILED
                task.error = str(exc.detail)
            except Exception as exc:
                logger.exception("异步任务 %s 失败", task.task_id)
                task.status = STATUS_FAILED
                task.error = str(exc)
            finally:
                task.updated_at = time.time()

        try:
            ctx.executor.submit(job)
        except CapacityExceeded as exc:
            ctx.tasks.delete(task.task_id)
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        return TaskAccepted(task_id=task.task_id, status=STATUS_PENDING)

    @app.get("/api/v1/tasks", summary="任务列表")
    def list_tasks(limit: int = 50):
        items = ctx.tasks.list(limit=min(500, max(1, limit)))
        return {"total": len(items), "items": [_task_view(t) for t in items]}

    @app.get("/api/v1/tasks/{task_id}", summary="查询任务状态", response_model=TaskStatus)
    def get_task(task_id: str):
        task = ctx.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在或已过期")
        return TaskStatus(**_task_view(task))

    @app.get("/api/v1/tasks/{task_id}/result", summary="下载任务结果音频")
    def get_task_result(task_id: str):
        task = ctx.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在或已过期")
        if task.status != STATUS_SUCCEEDED or not task.result_path or not os.path.isfile(task.result_path):
            raise HTTPException(status_code=409, detail=f"任务尚未完成，当前状态: {task.status}")
        headers = _response_headers(task.result or {}, int((task.result or {}).get("elapsed_ms", 0)))
        return FileResponse(
            task.result_path,
            media_type=audio_io.content_type_of(task.ext),
            filename=_output_name(task.filename, (task.result or {}).get("denoised", False)),
            headers=headers,
        )

    @app.delete("/api/v1/tasks/{task_id}", summary="删除任务及其临时文件")
    def delete_task(task_id: str):
        if not ctx.tasks.delete(task_id):
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"task_id": task_id, "deleted": True}

    # ---------------- 运维接口 ----------------

    @app.get("/health", summary="存活探针")
    def health():
        return {
            "status": "ok",
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "uptime_seconds": ctx.uptime(),
        }

    @app.get("/ready", summary="就绪探针")
    def ready():
        if ctx.fatal_error:
            return JSONResponse(
                status_code=503,
                content={"status": "error", "detail": ctx.fatal_error},
            )
        if not ctx.ready:
            return JSONResponse(status_code=503, content={"status": "loading", "detail": "模型加载中"})
        return {"status": "ready", "service": SERVICE_NAME, "uptime_seconds": ctx.uptime()}

    @app.get("/api/v1/status", summary="运行时状态（资源/队列/任务）")
    def status():
        return {
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "uptime_seconds": ctx.uptime(),
            "ready": ctx.ready,
            "device": _runtime_info(),
            "resource": ctx.governor.snapshot(),
            "scheduler": {
                **ctx.executor.stats(),
                "max_concurrency": cfg.concurrency.max_concurrency,
                "max_queue_size": cfg.concurrency.max_queue_size,
                "queue_timeout_s": cfg.concurrency.queue_timeout_s,
            },
            "engine": ctx.engine.status(),
            "tasks": {
                "count": len(ctx.tasks.list(limit=10000)),
                "ttl_seconds": cfg.server.task_ttl_s,
            },
        }

    @app.get("/api/v1/config", summary="查看当前生效配置")
    def get_config():
        from app.settings import to_dict

        return {"config": to_dict(cfg), "config_file": os.environ.get("CV_CONFIG_FILE", "")}

    @app.get("/", summary="服务信息")
    def index():
        return {
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "docs": "/docs",
            "endpoints": [
                "POST /api/v1/denoise",
                "POST /api/v1/denoise/base64",
                "POST /api/v1/tasks",
                "GET  /api/v1/tasks/{task_id}",
                "GET  /api/v1/tasks/{task_id}/result",
                "GET  /api/v1/status",
                "GET  /api/v1/config",
                "GET  /health",
                "GET  /ready",
            ],
        }

    return app


# ---------------------------------------------------------------------------


def _runtime_info() -> Dict[str, Any]:
    try:
        return clearvoice_bridge.describe_runtime()
    except Exception as exc:
        return {"error": str(exc)}


def _check_ready(ctx: ServiceContext) -> None:
    if ctx.fatal_error:
        raise HTTPException(status_code=503, detail=f"模型未就绪: {ctx.fatal_error}")
    if not ctx.ready:
        raise HTTPException(status_code=503, detail="模型加载中，请稍后重试")


def _check_upload_size(request: Request, cfg) -> None:
    limit = cfg.server.max_upload_mb * 1024 * 1024
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > limit:
        raise HTTPException(
            status_code=413, detail=f"上传文件超过限制 {cfg.server.max_upload_mb}MB"
        )


async def _save_upload(file: UploadFile, dest: str, cfg) -> int:
    limit = cfg.server.max_upload_mb * 1024 * 1024
    total = 0
    try:
        with open(dest, "wb") as fp:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise HTTPException(
                        status_code=413, detail=f"上传文件超过限制 {cfg.server.max_upload_mb}MB"
                    )
                fp.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"保存上传文件失败: {exc}") from exc
    finally:
        await file.close()
    if total == 0:
        raise HTTPException(status_code=400, detail="上传文件为空")
    return total


def _submit_and_wait(ctx: ServiceContext, job, timeout_s: float):
    try:
        future = ctx.executor.submit(job)
    except CapacityExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    try:
        return future.result(timeout=timeout_s)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        # concurrent.futures 会把业务异常包在 future 里，这里原样透出
        if isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError":
            future.cancel()
            raise HTTPException(status_code=504, detail="处理超时，请改用异步任务接口") from exc
        raise


def _output_name(filename: str, denoised: bool) -> str:
    base, ext = os.path.splitext(os.path.basename(filename or "input.wav"))
    suffix = "_denoised" if denoised else "_original"
    return f"{base}{suffix}{ext or '.wav'}"


def _task_view(task: Task) -> Dict[str, Any]:
    return {
        "task_id": task.task_id,
        "status": task.status,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "filename": task.filename,
        "ext": task.ext,
        "size_bytes": task.size_bytes,
        "result": task.result,
        "error": task.error,
        "has_result_file": bool(task.result_path and os.path.isfile(task.result_path)),
    }
