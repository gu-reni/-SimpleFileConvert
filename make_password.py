#!/usr/bin/env python3
"""
生成 / 重设 fileconvert 的共享口令。

按 daifamily 约定：
  - 明文只写进 600 权限的 口令.txt，**绝不打印到对话**（对话走钉钉 = 外部通道）
  - 对外只报「长度 + sha256 前 8 位指纹」供核对
  - **防覆盖**：auth.json 已存在就拒绝，防止静默作废已发出的口令

用法：
  python3 make_password.py                 首次生成（已存在 auth.json 时会拒绝）
  python3 make_password.py --reset         重设：自动从 口令.txt 里找出「不是旧口令」的那一行
  python3 make_password.py --reset --line 4   明确指定用第 4 行（0 基）
  python3 make_password.py --show         只报告当前口令的长度与指纹（不打印明文）

为什么 --reset 要自动甄别：
  用户会直接在 口令.txt 里手写新口令（可能顺手把旧口令留在上面）。
  若盲取第一行或最后一行，都可能把旧口令重新设进去、或设成一个说明文字。
  所以判断口径是：候选行里**通不过当前 auth.json 校验**的那一行才是新口令；
  若候选有 0 个或多个，宁可拒绝并要求人工指定行号，也不猜。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTH = HERE / "auth.json"
PLAIN = HERE / "口令.txt"

# 生成口令后打印出来的访问地址，由部署者按自己的域名设置；不设就只打印相对路径。
ADDR = os.environ.get("FC_PUBLIC_URL", "/convert/")
LABELS = ("口令：", "口令:", "地址", "fileconvert", "（", "#")

WORDS = [
    "xiang", "shan", "hai", "tian", "feng", "yun", "song", "zhu", "lan", "yue",
    "chun", "qiu", "dong", "xia", "he", "hu", "lin", "sen", "guang", "ming",
]


def fingerprint(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()[:8]


def verifies(pw: str) -> bool:
    """口令是否匹配当前生效的 auth.json（本地实现，不依赖 app.py）。"""
    if not AUTH.exists():
        return False
    try:
        d = json.loads(AUTH.read_text(encoding="utf-8"))
        digest = hashlib.pbkdf2_hmac("sha256", pw.encode(),
                                     bytes.fromhex(d["salt"]), int(d["iterations"]))
        return digest.hex() == d["hash"]
    except Exception:                                           # noqa: BLE001
        return False


def _write_auth(password: str) -> None:
    salt = secrets.token_bytes(16)
    iterations = 240_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    AUTH.write_text(json.dumps({
        "salt": salt.hex(),
        "iterations": iterations,
        "hash": digest.hex(),
        # 换口令时**同时换会话密钥** → 所有已登录的旧会话立即失效
        "session_secret": secrets.token_bytes(32).hex(),
    }, indent=2), encoding="utf-8")
    os.chmod(AUTH, 0o600)


def _write_plain(password: str) -> None:
    """口令.txt 用规范格式重写：只留一条口令，不留旧的。"""
    PLAIN.write_text(
        "fileconvert 共享口令\n"
        f"地址：{ADDR}\n"
        f"口令：{password}\n"
        "\n（此文件 600 权限，仅本机可读。）\n",
        encoding="utf-8",
    )
    os.chmod(PLAIN, 0o600)


def _is_candidate(line: str) -> bool:
    """像口令的行：非空、无空格、无中文、无标签、长度 >= 8。"""
    s = line.strip()
    if len(s) < 8 or " " in s:
        return False
    if any("\u4e00" <= c <= "\u9fff" for c in s):
        return False
    return not s.startswith(LABELS)


def read_password() -> str | None:
    """从 口令.txt 里取出明文口令，供测试/运维脚本使用（不要打印返回值）。"""
    if not PLAIN.exists():
        return None
    lines = PLAIN.read_text(encoding="utf-8").splitlines()
    for line in lines:                                  # 规范格式优先
        if line.startswith("口令："):
            v = line[len("口令："):].strip()
            if v:
                return v
    for line in lines:                                  # 兜底：第一个像口令的行
        if _is_candidate(line):
            return line.strip()
    return None


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    b = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, b)
    os.chmod(b, 0o600)
    return b


def do_reset(line_no: int | None) -> int:
    if not PLAIN.exists():
        print(f"✋ 找不到 {PLAIN}；请先把新口令写进去。")
        return 1

    lines = PLAIN.read_text(encoding="utf-8").splitlines()
    if line_no is not None:
        if not (0 <= line_no < len(lines)):
            print(f"✋ 行号 {line_no} 超出范围（0~{len(lines)-1}）")
            return 1
        new_pw = lines[line_no].strip()
        print(f"按指定行 {line_no} 取值（长度 {len(new_pw)}）")
    else:
        cands = [(i, l.strip()) for i, l in enumerate(lines)
                 if _is_candidate(l) and not verifies(l.strip())]
        if len(cands) != 1:
            print(f"✋ 无法唯一确定新口令：找到 {len(cands)} 个候选。")
            for i, s in cands:
                print(f"     行{i}: 长度 {len(s)} 指纹 {fingerprint(s)}")
            print("   → 用 --line <行号> 明确指定。")
            return 1
        line_no, new_pw = cands[0]
        print(f"自动甄别：第 {line_no} 行是新口令（长度 {len(new_pw)}）")
        print("   （判据：候选行里唯一通不过当前 auth.json 校验的那个）")

    if verifies(new_pw):
        print("✋ 该内容**就是当前生效的口令**，不需要重设。")
        return 1
    if len(new_pw) < 8:
        print(f"✋ 太短（{len(new_pw)} 字符），至少 8 位。")
        return 1

    for f in (AUTH, PLAIN):
        b = _backup(f)
        if b:
            print(f"   已备份 {f.name} → {b.name}")

    _write_auth(new_pw)
    _write_plain(new_pw)                 # 规范格式重写：旧口令不再留在文件里

    print("✅ 口令已重设")
    print(f"   口令长度: {len(new_pw)}")
    print(f"   指纹前8位: {fingerprint(new_pw)}   ← 转告家人时用这个核对，别贴明文")
    print("   注意：会话密钥已同时更换，所有已登录的旧会话已失效，需要重新登录。")
    print("   下一步必需：重启服务才会生效 → sudo systemctl restart fileconvert")
    return 0


def main() -> int:
    args = sys.argv[1:]

    if "--show" in args:
        pw = read_password()
        if not pw:
            print("✋ 读不出口令")
            return 1
        print(f"当前 口令.txt 里的口令：长度 {len(pw)}  指纹 {fingerprint(pw)}")
        print(f"与 auth.json 一致: {verifies(pw)}")
        return 0

    if "--reset" in args:
        ln = None
        if "--line" in args:
            try:
                ln = int(args[args.index("--line") + 1])
            except (IndexError, ValueError):
                print("✋ --line 后面要跟一个整数")
                return 1
        return do_reset(ln)

    if AUTH.exists():
        print(f"✋ {AUTH.name} 已存在，拒绝覆盖（防止作废已发出的口令）。")
        print("   重设口令请用：python3 make_password.py --reset")
        return 1

    password = "-".join(secrets.choice(WORDS) for _ in range(3)) + f"-{secrets.randbelow(10000):04d}"
    _write_auth(password)
    _write_plain(password)

    print("✅ 已生成")
    print(f"   口令文件: {PLAIN}  (600)")
    print(f"   校验文件: {AUTH}  (600)")
    print(f"   口令长度: {len(password)}")
    print(f"   指纹前8位: {fingerprint(password)}   ← 转告家人时用这个核对，别贴明文")
    return 0


if __name__ == "__main__":
    sys.exit(main())
