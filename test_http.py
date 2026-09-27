#!/usr/bin/env python3
"""HTTP 全链路测试 —— 起真服务器、走真请求。

口令从 口令.txt 读取，**绝不打印**（对话走钉钉 = 外部通道）。
用法：.venv/bin/python test_http.py
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 18089                      # 测试端口，避开 18080
BASE = f"http://127.0.0.1:{PORT}"
# 测试实例用**独立的数据目录**，不和线上服务共用 data/work
# （否则“工作目录已清空”的断言会被线上残留目录搞成假失败）
TEST_DATA = Path(tempfile.mkdtemp(prefix="fctest-data-"))

PASS, FAIL = [], []


def check(label: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(label)
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  {detail}" if detail else ""))


def multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    """手工拼 multipart/form-data（不引额外依赖）。files: {name: (filename, bytes, ctype)}"""
    b = uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, (fn, data, ct) in files.items():
        out += (f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                f"filename=\"{fn}\"\r\nContent-Type: {ct}\r\n\r\n").encode()
        out += data + b"\r\n"
    out += f"--{b}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={b}"


def main() -> int:
    if not (ROOT / "口令.txt").exists():
        print("缺少 口令.txt，先跑 make_password.py")
        return 1
    from make_password import read_password
    password = read_password()          # 口令.txt 是给人看的，用共用函数取，别整文件读
    if not password:
        print("口令.txt 里没找到 '口令：' 行"); return 1

    env = {**os.environ, "FC_HOST": "127.0.0.1", "FC_PORT": str(PORT),
           "FC_DATA": str(TEST_DATA)}
    proc = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(f"{BASE}/", timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        else:
            print("服务器起不来"); return 1

        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def req(path, data=None, ctype=None, method=None):
            r = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
            if ctype:
                r.add_header("Content-Type", ctype)
            return op.open(r, timeout=120)

        def code_of(fn):
            try:
                return fn().status
            except urllib.error.HTTPError as e:
                return e.code

        print("=== 1. 未登录访问 ===")
        html = req("/").read().decode()
        # 断言登录页的**结构**（有密码输入框），不要去断言文案——
        # 之前断言「共享口令」这几个字，文案改成「请输入访问口令」后测试就假失败了。
        check("未登录返回登录页",
              'type="password"' in html and 'id="pw"' in html)
        check("未登录访问 API 被拒 (401)",
              code_of(lambda: req("/api/targets", b"filename=a.png",
                                  "application/x-www-form-urlencoded")) == 401)

        print("=== 2. 口令校验 ===")
        check("错误口令被拒 (401)",
              code_of(lambda: req("/api/login",
                                  urllib.parse.urlencode({"password": "definitely-wrong"}).encode(),
                                  "application/x-www-form-urlencoded")) == 401)
        r = req("/api/login", urllib.parse.urlencode({"password": password}).encode(),
                "application/x-www-form-urlencoded")
        check("正确口令登录成功", r.status == 200)
        check("拿到了会话 Cookie", any(c.name for c in jar))
        # 回归断言：Cookie 值里出现 '=' 或需要 URL 编码都会导致「登录成功却立刻退回登录页」
        sc = r.headers.get("set-cookie", "")
        val = sc.split("fc_session=", 1)[1].split(";", 1)[0] if "fc_session=" in sc else ""
        check("会话值对 Cookie 安全（无 '=' 填充）",
              bool(val) and "=" not in val and "%" not in val, f"长度 {len(val)}")

        print("=== 3. 登录后界面 ===")
        html = req("/").read().decode()
        check("返回主界面", 'id="drop"' in html and 'id="file"' in html)
        check("引用了相对路径的静态资源", 'href="static/style.css"' in html)
        check("静态资源可取", req("/static/style.css").status == 200)
        check("前端脚本可取", req("/static/app.js").status == 200)
        # 静态文件必须显式声明 Cache-Control：否则浏览器启用启发式缓存，
        # 前端改了但用户看到的还是旧页面（本项目已因此出过事故）
        cc = req("/static/app.js").headers.get("cache-control", "")
        check("静态资源带 no-cache（防浏览器拿旧前端）",
              cc == "no-cache, must-revalidate", cc or "响应里没有这个头")
        # 「已处理文件」表头按需求固定：文件名 / 原格式 / 转为 / 状态 / 大小（+操作）
        ths = re.findall(r"<th>([^<]*)</th>", html)
        check("已处理文件表头正确",
              ths == ["文件名", "原格式", "转为", "状态", "大小", "操作"], str(ths))
        check("不再出现「最近转换」的旧说法", "最近转换" not in html)
        js = req("/static/app.js").read().decode()
        # 注释里会提到旧接口名（解释为什么删掉），所以先剥掉注释再断言真实代码
        code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", js, flags=re.S))
        check("前端不再调用服务端的列举接口",
              "api/history" not in code and "delete-all" not in code)
        check("前端用 sessionStorage 保存列表（关掉标签页即消失）", "sessionStorage" in code)

        print("=== 4. 目标格式查询 ===")
        body = urllib.parse.urlencode({"filename": "照片.heic"}).encode()
        d = json.loads(req("/api/targets", body, "application/x-www-form-urlencoded").read())
        keys = [t["key"] for t in d["targets"]]
        check("HEIC 可转出格式含 JPG", "JPG" in keys, f"共 {len(keys)} 种: {','.join(keys[:6])}…")
        body = urllib.parse.urlencode({"filename": "x.docx"}).encode()
        d = json.loads(req("/api/targets", body, "application/x-www-form-urlencoded").read())
        check("DOCX 可转 PDF", "PDF" in [t["key"] for t in d["targets"]])
        # 未知扩展名：设计上返回 200 + 空列表（前端据此显示"暂时不支持"），不是报错
        body = urllib.parse.urlencode({"filename": "a.xyz"}).encode()
        d = json.loads(req("/api/targets", body, "application/x-www-form-urlencoded").read())
        check("不支持的扩展名返回空列表", d["targets"] == [] and d["ext"] == "xyz")

        created = []            # 本次创建的任务号由"客户端"自己保管（服务端不再有列举接口）
        print("=== 5. 真实转换：图片 → JPG ===")
        png = (ROOT / "testdata" / "样例图.png").read_bytes()
        data, ct = multipart({"target": "jpg"}, {"file": ("样例图.png", png, "image/png")})
        r = req("/api/convert", data, ct)
        job = json.loads(r.read())["job"]
        created.append(job)
        check("提交转换成功", bool(job))
        for _ in range(60):
            st = json.loads(req(f"/api/status/{job}").read())
            if st["status"] in ("done", "failed"):
                break
            time.sleep(0.5)
        check("任务完成", st["status"] == "done", st.get("error", "")[:80])
        # download 字段是相对 URL（子路径挂载必须如此，前端直接 a.href 就能用），
        # 测试里要按服务根拼，否则会被拼成 http://127.0.0.1:18089download/xxx
        blob = req("/" + st["download"].lstrip("/")).read()
        check("下载到了 JPG", blob[:2] == b"\xff\xd8", f"{len(blob)} 字节")
        check("产物大小合理", len(blob) > 1000)

        print("=== 6. 真实转换：PDF → 多页 zip ===")
        pdf = (ROOT / "testdata" / "两页.pdf").read_bytes()
        data, ct = multipart({"target": "png"}, {"file": ("两页.pdf", pdf, "application/pdf")})
        job = json.loads(req("/api/convert", data, ct).read())["job"]
        created.append(job)
        for _ in range(60):
            st = json.loads(req(f"/api/status/{job}").read())
            if st["status"] in ("done", "failed"):
                break
            time.sleep(0.5)
        check("多页任务完成", st["status"] == "done", st.get("error", "")[:80])
        # download 字段是相对 URL（子路径挂载必须如此，前端直接 a.href 就能用），
        # 测试里要按服务根拼，否则会被拼成 http://127.0.0.1:18089download/xxx
        blob = req("/" + st["download"].lstrip("/")).read()
        check("产出是 zip", blob[:2] == b"PK", f"{len(blob)} 字节")

        print("=== 7. 非法请求拦截 ===")
        data, ct = multipart({"target": "exe"}, {"file": ("a.png", png, "image/png")})
        check("不在白名单的目标格式被拒 (400)", code_of(lambda: req("/api/convert", data, ct)) == 400)
        data, ct = multipart({"target": "mp3"}, {"file": ("a.xyz", b"x", "application/octet-stream")})
        check("不支持的输入格式被拒 (400)", code_of(lambda: req("/api/convert", data, ct)) == 400)
        check("伪造/穿越的任务号被拒 (404)",
              code_of(lambda: req("/download/%2e%2e%2f%2e%2e%2fetc%2fpasswd")) == 404)

        print("=== 8. 隐私：服务端不再提供任何「列举任务」的接口 ===")
        # 这是本次改动的核心保证。全家共用一个口令、服务端分不清谁是谁；
        # 只要存在列举接口，任何登录的人都能看到别人的文件名、并下载别人的文件（已实测）。
        # 现在列表只存在浏览器里，服务端连"列出任务"的能力都没有。
        check("列举接口 /api/history 已删除（应 404）",
              code_of(lambda: req("/api/history")) == 404)
        check("旧的「全清」接口 /api/delete-all 已删除（应 404/405）",
              code_of(lambda: req("/api/delete-all", b"", None, "POST")) in (404, 405))

        print("=== 9. 删除：单条 / 批量（只提交自己手里的任务号）===")
        check("删除单条成功", req(f"/api/delete/{created[0]}", b"", None, "POST").status == 200)
        # 关键：磁盘上的输入/产出要一起删掉，不能只从界面消失
        check("磁盘上的工作目录随之删除", not (TEST_DATA / "work" / created[0]).exists())
        check("删除不存在的任务 → 404",
              code_of(lambda: req("/api/delete/deadbeefdeadbeef", b"", None, "POST")) == 404)

        r = req("/api/delete-batch", json.dumps({"jobs": created[1:]}).encode(), "application/json")
        check("批量删除成功", r.status == 200)
        n = json.loads(r.read()).get("removed")
        check("批量删除返回条数正确", n == len(created[1:]), f"removed={n}")
        wd = TEST_DATA / "work"
        left = list(wd.iterdir()) if wd.exists() else []
        check("磁盘工作目录已清空", not left, f"剩 {len(left)} 个目录")
        check("批量接口拒绝非 JSON 请求体",
              code_of(lambda: req("/api/delete-batch", b"notjson", "text/plain")) == 400)
        check("批量接口拒绝缺 jobs 字段的请求体",
              code_of(lambda: req("/api/delete-batch", b'{"x":1}', "application/json")) == 400)

        print("=== 10. 退出 ===")
        req("/api/logout", b"", None, "POST")
        check("退出后 API 再次被拒 (401)",
              code_of(lambda: req("/api/targets", b"filename=a.png",
                                  "application/x-www-form-urlencoded")) == 401)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read().decode("utf-8", "replace")
        noise = [ln for ln in err.splitlines() if "[fileconvert]" in ln]
        if noise:
            print("\n--- 服务端日志（仅 fileconvert 行）---")
            for ln in noise[:10]:
                print("   ", ln)
        shutil.rmtree(TEST_DATA, ignore_errors=True)          # 收拾临时数据目录

    print(f"\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
