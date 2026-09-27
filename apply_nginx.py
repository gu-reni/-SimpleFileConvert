#!/usr/bin/env python3
"""把一个 nginx vhost 从「整站反代到某端口」改成「按子路径挂自研工具」。

改造后：
  · /convert/  → 127.0.0.1:<FC_PORT>（剥掉前缀）
  · /          → 静态索引页（自研工具列表）

安全措施：
  · 只改 --server-name 指定的那一个 server 块，其他 server 块一个字符都不动
  · 用花括号配对定位整个 location 块，不靠整段文本精确匹配（少个空格就失效）
  · 先备份到 /root/<conf 文件名>.bak-<时间戳>
  · 改完必须 `nginx -t` 通过才 reload；不通过自动回滚
  · 幂等：已经是新形态就什么都不做
  · --dry-run 只打印 diff，不写文件

用法：
  sudo python3 apply_nginx.py --dry-run \\
      --conf /etc/nginx/sites-available/mysite.conf \\
      --server-name tools.example.com \\
      --index-root /var/www/tools

不传 --conf 时会直接退出并打印用法（避免误改到别人的配置）。
"""
from __future__ import annotations

import difflib
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 下面三个都可以用命令行覆盖；默认值是刻意留的占位，必须显式指定才会动手。
CONF = Path("/etc/nginx/sites-available/CHANGE-ME.conf")
INDEX_ROOT = "/var/www/tools"
SERVER_NAME = "tools.example.com"
MARK = "location /convert/"


def parse_args() -> None:
    """解析 --conf / --server-name / --index-root。必须在 NEW_BLOCK 构造之前调用。"""
    global CONF, INDEX_ROOT, SERVER_NAME
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if i + 1 < len(argv):
            if a == "--conf":
                CONF = Path(argv[i + 1])
            elif a == "--server-name":
                SERVER_NAME = argv[i + 1]
            elif a == "--index-root":
                INDEX_ROOT = argv[i + 1]
    if "CHANGE-ME" in CONF.name:
        print("用法: sudo python3 apply_nginx.py --conf <你的 vhost 文件> "
              "--server-name <该文件里的 server_name> [--index-root <索引页目录>] [--dry-run]",
              file=sys.stderr)
        print("（不指定 --conf 就不会改动任何配置）", file=sys.stderr)
        sys.exit(2)


parse_args()


NEW_BLOCK = """    # ---- 自研工具按 <name>/ 挂在路径下 ----
    # 现成开源服务才单独开子域名。以后加自研工具 = 在这里加一组 location。
    location = /convert { return 301 /convert/; }   # 补斜杠，否则页面相对路径全歪
    location /convert/ {
        proxy_pass http://127.0.0.1:18080/;          # 末尾斜杠 = 剥掉 /convert 前缀
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;  # 应用靠它决定 Cookie 是否加 Secure
        client_max_body_size 512m;                   # 默认 1m，家人传视频必 413
        proxy_read_timeout 600s;                     # 转换慢，别被默认 60s 掐断
        proxy_request_buffering off;                 # 大文件边收边处理，不整份缓存
    }

    location / {
        root """ + INDEX_ROOT + """;  # 自研工具索引页
        index index.html;
        try_files $uri $uri/ =404;
    }
"""


def die(msg: str) -> None:
    print(f"✋ {msg}", file=sys.stderr)
    sys.exit(1)


def find_block_span(text: str, key: str) -> tuple[int, int]:
    """在 text 里找到 key 所在的 server{...} 范围（花括号配对）。"""
    at = text.find(key)
    if at < 0:
        die(f"找不到 {key}")
    start = text.rfind("server {", 0, at)
    if start < 0:
        die(f"{key} 之前找不到 server {{")
    depth, i = 0, start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return start, i + 1
        i += 1
    die("花括号不配对")


def find_location_span(block: str) -> tuple[int, int]:
    """找到块内 `location / {` 整体范围（花括号配对）。"""
    at = block.find("location / {")
    if at < 0:
        die("tools 块里找不到 `location / {`")
    depth, i = 0, at
    while i < len(block):
        if block[i] == "{":
            depth += 1
        elif block[i] == "}":
            depth -= 1
            if depth == 0:
                return at, i + 1
        i += 1
    die("location 块花括号不配对")


def main() -> int:
    dry = "--dry-run" in sys.argv
    text = CONF.read_text(encoding="utf-8")

    if MARK in text:
        print("已经是新形态（存在 location /convert/），无需改动。")
        return 0

    s, e = find_block_span(text, f"server_name {SERVER_NAME};")
    tools_block = text[s:e]

    if tools_block.count("location / {") != 1:
        die(f"tools 块里有 {tools_block.count('location / {')} 个 `location / {{`，需人工确认")

    ls, le = find_location_span(tools_block)

    # NEW_BLOCK 自带 4 空格缩进，所以要把原块前的行首缩进切掉，否则首行会变成 8 空格
    head = tools_block[:ls]
    head = head[:head.rfind("\n") + 1]
    new_tools = head + NEW_BLOCK.rstrip("\n") + tools_block[le:]
    new_text = text[:s] + new_tools + text[e:]

    if not Path(INDEX_ROOT, "index.html").exists():
        die(f"索引页不存在：{INDEX_ROOT}/index.html")

    if dry:
        print("=== --dry-run：只显示 diff，不写任何文件 ===")
        for ln in difflib.unified_diff(text.splitlines(), new_text.splitlines(),
                                       "旧配置", "新配置", lineterm="", n=4):
            print(ln)
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = Path("/root") / f"{CONF.name}.bak-{stamp}"
    shutil.copy2(CONF, bak)
    print(f"已备份 → {bak}")

    CONF.write_text(new_text, encoding="utf-8")
    print("已写入新配置")

    t = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    tail = (t.stderr or t.stdout).strip().splitlines()
    print("nginx -t:", tail[-1] if tail else "(无输出)")
    if t.returncode != 0:
        shutil.copy2(bak, CONF)
        die("nginx -t 未通过，已自动回滚。")

    r = subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True)
    if r.returncode != 0:
        shutil.copy2(bak, CONF)
        subprocess.run(["systemctl", "reload", "nginx"], capture_output=True)
        die(f"reload 失败，已回滚：{r.stderr.strip()}")

    print("✅ nginx 已 reload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
