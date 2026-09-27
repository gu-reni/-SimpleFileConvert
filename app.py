"""
fileconvert —— 文件格式转换服务

部署形态：systemd 常驻，默认监听 127.0.0.1:18080（给 nginx 反代）。
需要局域网直连时用环境变量开第二个地址，例如 FC_HOST=127.0.0.1,192.168.1.10。
对外地址由部署者决定（nginx 反代到某个子路径或子域名都可以）。

重要：nginx 用 `proxy_pass http://127.0.0.1:18080/;`（末尾带斜杠）剥掉 /convert 前缀，
所以本应用自身的路由都是 / 开头的「相对根」；前端页面一律用相对 URL（style.css、api/...），
这样被挂在 /convert/ 下也能正确解析。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import conversions as cv

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
# 数据目录可用 FC_DATA 覆盖 —— 测试时指向临时目录，避免测试和线上服务共用同一个
# data/work 而互相干扰（断言“工作目录已清空”会被线上残留的目录搞假失败）。
DATA = Path(os.environ["FC_DATA"]) if os.environ.get("FC_DATA") else HERE / "data"
WORK = DATA / "work"
AUTH_FILE = HERE / "auth.json"

JOBS: dict[str, dict] = {}
MAX_UPLOAD = 500 * 1024 * 1024          # 500 MB
MAX_CONCURRENT = 2                      # 4 核机器：同时最多 2 个转换
JOB_TIMEOUT = 600                       # 单任务 10 分钟
JOB_TTL = 2 * 3600                      # 结果保留 2 小时
SESSION_TTL = 30 * 24 * 3600            # 登录 30 天
SEM = asyncio.Semaphore(MAX_CONCURRENT)

DATA.mkdir(parents=True, exist_ok=True)
WORK.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# 后台清理：结果只保留 2 小时
# --------------------------------------------------------------------------
async def _janitor_loop() -> None:
    while True:
        await asyncio.sleep(600)
        cutoff = time.time() - JOB_TTL
        for jid, job in list(JOBS.items()):
            if job.get("created", 0) < cutoff and job["status"] in ("done", "failed"):
                shutil.rmtree(Path(job["workdir"]), ignore_errors=True)
                JOBS.pop(jid, None)
        # 兜底：清掉磁盘上遗留的孤儿目录（进程被 kill 时可能留下）
        for d in WORK.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # 用 lifespan 而不是已弃用的 @app.on_event("startup")，否则每次启动
    # journalctl 里都会多一条 DeprecationWarning，掩盖真正的日志。
    task = asyncio.create_task(_janitor_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="fileconvert", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=_lifespan)


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    """页面与静态资源必须显式声明 Cache-Control。

    不写的话浏览器启用"启发式缓存"（按 Last-Modified 自己猜一个新鲜期），
    结果是：前端已改好，用户看到的还是旧页面 —— 这个项目已经因为这件事出过事故。
    no-cache 不是"不缓存"，而是"每次都要回源确认"：命中 ETag 就返回 304，不浪费流量。
    下载结果不加这个头（那是用户立刻要取走的东西，缓存与否无所谓）。
    """
    resp = await call_next(request)
    ct = resp.headers.get("content-type", "")
    if request.url.path.startswith("/static/") or ct.startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


# --------------------------------------------------------------------------
# 认证：单一口令（家人共用），PBKDF2 存哈希，会话用 HMAC 签名 cookie
# --------------------------------------------------------------------------
def load_auth() -> dict:
    if not AUTH_FILE.exists():
        raise RuntimeError(f"缺少 {AUTH_FILE}，请先运行 make_password.py 生成口令")
    return json.loads(AUTH_FILE.read_text())


def verify_password(pw: str) -> bool:
    a = load_auth()
    got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(a["salt"]), a["iterations"])
    return hmac.compare_digest(got.hex(), a["hash"])


def _secret() -> bytes:
    return bytes.fromhex(load_auth()["session_secret"])


def make_session() -> str:
    # 去掉 base64 的 '=' 填充：Cookie 值里 '=' 不合法，会被浏览器或中间代理截断，
    # 表现为「登录返回 200 但下一个请求仍是未登录」。
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + SESSION_TTL}).encode()
    ).decode().rstrip("=")
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def session_ok(request: Request) -> bool:
    raw = request.cookies.get("fc_session")
    if not raw or "." not in raw:
        return False
    payload, _, sig = raw.rpartition(".")
    expect = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return False
    try:
        # 填充可带可不带，两种都容忍
        data = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        return json.loads(data)["exp"] > time.time()
    except Exception:
        return False


def require_auth(request: Request):
    if not session_ok(request):
        raise HTTPException(status_code=401, detail="未登录")


# --------------------------------------------------------------------------
# 校验与转换
# --------------------------------------------------------------------------
SAFE_EXT = re.compile(r"^[a-z0-9]{1,10}$")


def safe_ext(name: str) -> str:
    ext = Path(name).suffix.lower().lstrip(".")
    if not SAFE_EXT.match(ext):
        raise HTTPException(400, f"不支持的文件扩展名: {ext!r}")
    return ext


async def run_job(job_id: str, src: Path, dst: Path, rule: cv.Rule, workdir: Path):
    """在信号量保护下执行一次转换。全程参数数组，无 shell。"""
    job = JOBS[job_id]
    async with SEM:
        job.update(status="running", started=time.time())
        try:
            # 先查轨道：没有声音的视频要转 MP3 之类，给中文提示而不是 ffmpeg 原始报错
            stream_err = cv.check_streams(src, rule)
            if stream_err:
                job.update(status="failed", error=stream_err)
                return

            argv = cv.build_argv(rule.builder, src, dst, workdir)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
            )
            try:
                _out, err = await asyncio.wait_for(proc.communicate(), timeout=JOB_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                job.update(status="failed", error=f"转换超时（超过 {JOB_TIMEOUT // 60} 分钟）")
                return

            if proc.returncode != 0:
                job.update(status="failed",
                           error=f"转换失败：{err.decode('utf-8', 'replace')[-400:].strip()}")
                return

            produced = cv.expected_output(rule.builder, workdir, dst, src)
            if not produced.exists():
                # 多文件产物（PDF→图片）或工具改了名字：兜底搜一遍
                cands = [p for p in workdir.iterdir()
                         if p.is_file() and p.stem not in {"lo-profile"} and p != src]
                cands = [p for p in cands if p.suffix.lstrip(".") == rule.target_ext]
                if not cands:
                    job.update(status="failed", error="转换未产出文件")
                    return
                produced = cands[0]
                if len(cands) > 1:
                    produced = pack_zip(workdir, cands, job)

            size = produced.stat().st_size
            if size == 0:
                job.update(status="failed", error="产出文件为空（可能是格式不受支持或字体缺失）")
                return

            job.update(status="done", file=str(produced), filename=produced.name,
                       size=size, finished=time.time())
        except Exception as e:                                  # noqa: BLE001
            job.update(status="failed", error=f"内部错误：{type(e).__name__}: {e}")


def pack_zip(workdir: Path, files: list[Path], job: dict) -> Path:
    """多页产物（PDF→PNG 等）打包成 zip，避免一次返回几百张图。"""
    zpath = workdir / "结果.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(files):
            z.write(p, p.name)
    job["multi"] = len(files)
    return zpath


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True, "jobs": len(JOBS)}


# --------------------------------------------------------------------------
# 会话 Cookie 的两个「想当然就会踩」的坑（实测踩过，局域网直连会直接登录失败）
# --------------------------------------------------------------------------
#  1. secure=True 写死 → Cookie 只走 https。而局域网明文直连（http://<lan-ip>:18080）时，
#     浏览器**不会回传** Secure 的 Cookie，
#     表现为「输入口令 → 页面刷新 → 又回到登录页」，而且不报任何错。
#     → 按实际请求的协议决定：经 nginx 是 https（X-Forwarded-Proto），局域网是 http。
#  2. path="/convert" → 外网经 nginx 时外部路径是 /convert/，但局域网直连是根路径 /，
#     路径不匹配同样不回传 Cookie。用 path="/" 两种入口都对。
#     （本服务独占端口，放宽到根路径没有副作用。）
def _is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "")
    if proto:
        return proto.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


COOKIE_PATH = "/"


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    if not session_ok(request):
        login = (STATIC / "login.html").read_text(encoding="utf-8")
        return HTMLResponse(login)
    return HTMLResponse(html)


@app.post("/api/login")
async def api_login(request: Request, password: str = Form(...)):
    if not verify_password(password):
        await asyncio.sleep(1.0)                    # 抑制暴力破解
        raise HTTPException(401, "口令不对")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("fc_session", make_session(), httponly=True, samesite="lax",
                    secure=_is_https(request), max_age=SESSION_TTL, path=COOKIE_PATH)
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("fc_session", path=COOKIE_PATH)
    return resp


@app.post("/api/targets")
async def api_targets(request: Request, filename: str = Form(...)):
    """选中文件后，前端拿可选目标格式。"""
    require_auth(request)
    ext = safe_ext(filename)
    rules = cv.targets_for(ext)
    return {"ext": ext, "targets": [{"key": r.key, "ext": r.target_ext} for r in rules]}


@app.post("/api/convert")
async def api_convert(request: Request,
                      file: UploadFile,
                      target: str = Form(...)):
    require_auth(request)

    orig = file.filename or "unnamed"
    ext = safe_ext(orig)
    rule = cv.find_rule(ext, target)
    if rule is None:
        raise HTTPException(400, f"不支持 {ext} → {target} 的转换")

    job_id = uuid.uuid4().hex
    workdir = WORK / job_id
    workdir.mkdir(parents=True)
    src = workdir / f"输入.{ext}"
    stem = Path(orig).stem[:40].replace("/", "_") or "结果"
    dst = workdir / f"{stem}.{rule.target_ext}"

    # 流式落盘 + 大小上限（避免把大文件读进内存）
    total = 0
    with src.open("wb") as fh:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD:
                fh.close()
                shutil.rmtree(workdir, ignore_errors=True)
                raise HTTPException(413, f"文件超过 {MAX_UPLOAD // 1024 // 1024} MB 上限")
            fh.write(chunk)

    JOBS[job_id] = {"status": "queued", "orig": orig, "target": rule.target_ext,
                    "size_in": total, "created": time.time(), "workdir": str(workdir)}
    asyncio.create_task(run_job(job_id, src, dst, rule, workdir))
    return {"ok": True, "job": job_id, "queued": len([j for j in JOBS.values()
                                                     if j["status"] in ("queued", "running")])}


@app.get("/api/status/{job_id}")
async def api_status(request: Request, job_id: str):
    require_auth(request)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或已过期")
    out = {k: job[k] for k in ("status", "orig", "target", "size_in", "error", "size", "multi")
           if k in job}
    if job["status"] == "done":
        out["download"] = f"download/{job_id}"
    return out


@app.get("/download/{job_id}")
async def download(request: Request, job_id: str):
    require_auth(request)
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        raise HTTPException(404, "结果不存在或已过期")
    path = Path(job["file"])
    if not path.exists():
        raise HTTPException(410, "结果文件已被清理")
    return FileResponse(path, filename=job["filename"],
                        media_type="application/octet-stream")


@app.post("/api/delete/{job_id}")
async def api_delete(request: Request, job_id: str):
    """删除一个任务（含磁盘上的输入与产出）。

    只能删"知道任务号"的任务：任务号是 122 位随机的 UUID，而且**服务端不提供
    任何列举接口**（见下面 delete-batch 的注释），所以只有创建者本人手里才有它。
    """
    require_auth(request)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或已被清理")
    if job.get("status") == "running":
        raise HTTPException(409, "正在转换中，等它结束再删")
    # workdir 是服务自己按 job_id 拼的（WORK / job_id），不是用户输入，安全
    shutil.rmtree(Path(job["workdir"]), ignore_errors=True)
    JOBS.pop(job_id, None)
    return {"ok": True, "deleted": job_id}


@app.post("/api/delete-batch")
async def api_delete_batch(request: Request):
    """按客户端给出的任务号批量删除。

    ⚠️ 这里**故意不提供 /api/history 那种"列出任务"的接口**。
    有列举接口时，因为全家共用一个口令、服务端分不清谁是谁，
    任何登录的人都能看到别人的文件名、并下载别人的文件（已实测）。
    改为由浏览器自己记「本次处理的文件」列表后，服务端不再有任何列举能力，
    泄露面从根上消失——而任务号是 122 位随机 UUID，猜不到、也无法枚举。
    """
    require_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体必须是 JSON")
    jobs = body.get("jobs") if isinstance(body, dict) else None
    if not isinstance(jobs, list):
        raise HTTPException(400, "需要 jobs 数组")
    if len(jobs) > 200:
        raise HTTPException(400, "一次最多 200 个")

    removed, skipped = 0, 0
    for jid in jobs:
        if not isinstance(jid, str):
            continue
        job = JOBS.get(jid)
        if not job or job.get("status") == "running":
            skipped += 1
            continue
        shutil.rmtree(Path(job["workdir"]), ignore_errors=True)
        JOBS.pop(jid, None)
        removed += 1
    return {"ok": True, "removed": removed, "skipped": skipped}


app.mount("/static", StaticFiles(directory=STATIC), name="static")

if __name__ == "__main__":
    import socket

    import uvicorn

    port = int(os.environ.get("FC_PORT", "18080"))

    # 默认只绑回环（给 nginx 反代）。需要局域网直连时用 FC_HOST 开第二个地址，
    # 例如 FC_HOST=127.0.0.1,192.168.1.10。
    # 关键坑：uvicorn.run() 只能绑一个地址；多地址必须自己建好 socket，
    # 再交给 Server.serve(sockets=[...])，否则第二个绑定会被静默忽略。
    # 绝不绑 0.0.0.0 —— 对外只经 nginx，避免绕过防火墙直连。
    want = os.environ.get("FC_HOST", "127.0.0.1")
    socks = []
    for h in (x.strip() for x in want.split(",") if x.strip()):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((h, port))
        s.listen(128)
        socks.append(s)
        print(f"[fileconvert] 监听 {h}:{port}", file=sys.stderr, flush=True)

    asyncio.run(uvicorn.Server(uvicorn.Config(app, log_level="warning")).serve(sockets=socks))
