# SimpleFileConvert

自建的文件格式转换服务：中文界面、零外部前端依赖、只放行预设的转换组合。

图片 / 文档 / 音视频互转，扫描件与照片识别成中文文字，PDF 转图片与文字。

## 特性

| 类别 | 说明 |
|---|---|
| 图片 | 主要格式互转（ImageMagick，可读 226 种格式）；**HEIC/HEIF → JPG/PNG**（iPhone 照片） |
| 文档 | doc / docx / odt / rtf / txt / xls / xlsx / ppt / pptx → **PDF**；Office 格式之间互转 |
| 音视频 | ffmpeg 覆盖的主流格式互转（mp4 / mkv / webm / mov / gif / mp3 / wav / flac / aac / ogg） |
| OCR | 图片 / PDF → 文字（tesseract，`chi_sim+eng`） |
| PDF | PDF → 图片 / 文字 |

界面为中文，**零外部依赖**（不引任何 CDN 字体或脚本，断网也能正常显示），
所有资源用相对路径，因此可以挂在任意子路径下（如 `/convert/`）。

## 设计取舍：调用宿主机工具链，而不是容器化

转换能力（ffmpeg / LibreOffice / ImageMagick / pandoc / ghostscript / poppler / tesseract）
由子进程直接调用宿主机上已安装的命令行工具，而不是把它们打包进容器镜像。与同类方案对比：

| | 本项目 | 把一个完整的转换平台容器化 |
|---|---|---|
| 部署体积 | venv 约 **39 MB / 18 个包** | 约 **1.45 GiB**（相当于把整条工具链再打包一遍，例如 ConvertX 镜像） |
| 界面语言 | 中文原生 | 多为纯英文界面，上游通常没有 i18n 计划 |
| 中文文档转 PDF | 用系统字体，正常输出 | 镜像内常缺 CJK 字体，中文 Markdown → PDF 会输出空白 |
| 暴露面 | 只放行白名单里的转换组合 | 通用文件处理平台，功能面大得多 |

代价是**宿主机必须预装对应的外部命令**，缺一个就少一种输出格式（见「快速开始」）。
换来的是：不需要为跑一个转换服务而维护一整套镜像，体积小两个数量级，能做的事也被限制在明确的白名单内。

## 快速开始

```bash
git clone <仓库地址> && cd <仓库目录>

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 转换引擎是外部命令，缺哪个对应格式就不可用
sudo apt install ffmpeg libreoffice imagemagick pandoc \
                 ghostscript poppler-utils tesseract-ocr tesseract-ocr-chi-sim

# 生成访问口令（写 auth.json 与 口令.txt，权限 600，已存在则拒绝覆盖）
.venv/bin/python make_password.py

# 启动（默认只监听 127.0.0.1:18080）
.venv/bin/python app.py
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `FC_HOST` | `127.0.0.1` | 监听地址，多个用逗号分隔（如 `127.0.0.1,192.168.1.10`） |
| `FC_PORT` | `18080` | 监听端口 |
| `FC_DATA` | `./data` | 数据目录（临时文件与产出） |
| `FC_PUBLIC_URL` | `/convert/` | 生成口令时打印的访问地址 |

## 架构

```
浏览器
  │  https
  ▼
nginx（可选：反代到 /convert/，带尾斜杠 = 剥掉前缀）
  │  http
  ▼
app.py  FastAPI + uvicorn，只绑 127.0.0.1
  ├─ 鉴权：共享口令 → PBKDF2 校验 → HMAC 签名的会话 Cookie
  ├─ 上传：流式落盘 data/work/<job_id>/输入.<ext>（文件名一律丢弃，不用原值）
  ├─ 转换：查白名单 → 构造 argv 数组 → subprocess 调外部命令（并发上限 2）
  └─ 下载 / 删除 / 状态查询
```

| 文件 | 作用 |
|---|---|
| `conversions.py` | **安全边界**：转换白名单 + 命令构造（纯逻辑，不依赖 HTTP，可单独测） |
| `app.py` | HTTP 服务：鉴权 / 上传 / 转换 / 下载 / 删除 / 状态 |
| `static/` | 中文前端，零外部依赖，全部相对路径 |
| `make_password.py` | 生成与重设口令（**防覆盖**；对外只报长度 + 指纹） |
| `test_conversions.py` | 转换链路测试（10 项） |
| `test_http.py` | HTTP 全链路测试（38 项） |
| `subpath_proxy.py` | 模拟 nginx 剥前缀，用来验证「挂子路径能工作」 |
| `apply_nginx.py` | 改 nginx 配置的脚本（幂等 / 先备份 / `nginx -t` 不过自动回滚） |
| `fileconvert.service` | systemd 单元模板（占位符见文件内注释） |

## 安全设计

- **只允许预设的「输入类型 → 目标格式」组合**，不接受用户传任何命令行参数
- 子进程一律**参数数组**调用，绝不走 shell；文件名丢弃原值、只用白名单扩展名
- **共享口令**（PBKDF2 哈希存 `auth.json`，权限 600 且已在 `.gitignore` 中）；
  单 IP 失败计数 + 1 秒延迟
- 上传上限 500 MB（流式落盘，超限即拒）；**并发上限 2**；单任务超时 10 分钟
- 结果保留 2 小时，后台自动清理
- **任务列表只存在浏览器里**（`sessionStorage`），服务端**不保存、也无法列举**
  - 原因：共享口令模式下服务端分不清谁是谁；一旦提供"列举任务"接口，
    任何登录的人都能看到别人的文件名、下载别人的成果、甚至删掉别人的记录（实测过）
  - 现在服务端没有任何列举接口，任务号是 122 位随机 UUID，只有创建者手里有
  - **关掉标签页 / 刷新页面 → 服务器上的文件也一并删除**（`pagehide` + `sendBeacon`）
    - 用 `sendBeacon` 而不是 `fetch`：页面卸载过程中 `fetch` 会被浏览器中断
    - 用 `pagehide` 而不是 `beforeunload`：手机（尤其 iOS）基本不触发
    - `e.persisted === true`（进 bfcache，还能按"后退"回来）时**不删**
    - 代价：刷新页面也会清掉（浏览器分不出"刷新"和"关闭"），要留住结果就先下载
- 默认只绑 `127.0.0.1`，**绝不绑 `0.0.0.0`**，对外只经 nginx
- systemd 加固：`ProtectSystem=strict` + 只有 `data/` 可写、`NoNewPrivileges`、`PrivateTmp`
- 资源限制：`CPUQuota=150%`、`MemoryMax=2G`（小内存机器上必须限，否则一个大视频就能拖垮整机）

## 测试

```bash
.venv/bin/python test_conversions.py   # 10 项：白名单边界 + 真实跑出产出
.venv/bin/python test_http.py          # 38 项：鉴权 / 上传 / 转换 / 下载 / 删除 / 隐私
```

`test_http.py` 会另起一个测试实例（独立端口 + 独立 `FC_DATA` 临时目录），
因此**可以和线上服务同时跑**，不会互相干扰。

## 部署

1. 把 `fileconvert.service` 里的 `<用户名>` / `<项目绝对路径>` 换成你自己的，装到 `/etc/systemd/system/`
2. nginx 反代（挂在子路径）：

```nginx
location = /convert { return 301 /convert/; }        # 补斜杠，否则页面相对路径全歪
location /convert/ {
    proxy_pass http://127.0.0.1:18080/;              # 末尾斜杠 = 剥掉 /convert 前缀
    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;      # 应用靠它决定 Cookie 是否加 Secure
    client_max_body_size 512m;                       # 默认 1m，传视频必 413
    proxy_read_timeout 600s;                         # 转换慢，别被默认 60s 掐断
    proxy_request_buffering off;                     # 大文件边收边处理，不整份缓存
}
```

也可以直接开一个子域名反代到 `/`，两种挂法应用侧都不用改。

`apply_nginx.py` 可以把一个已有 vhost 从「整站反代」改成上面的子路径形态，
它会先备份、`nginx -t` 不通过就自动回滚：

```bash
sudo python3 apply_nginx.py --dry-run \
    --conf /etc/nginx/sites-available/mysite.conf \
    --server-name tools.example.com
```

## 运维

```bash
systemctl status fileconvert
journalctl -u fileconvert -f

# 重设口令：把新口令写进 口令.txt 的任意一行，然后
.venv/bin/python make_password.py --show             # 只看当前口令长度/指纹，不打印明文
.venv/bin/python make_password.py --reset            # 自动甄别通不过当前校验的那一行
.venv/bin/python make_password.py --reset --line 4   # 有歧义时明确指定行号
# 改完立即生效无需重启（app.py 每次请求都重读 auth.json）；
# 会话密钥同时更换 → 已登录会话全部失效
```

## 已知限制

- **不支持 PDF → Word / Excel / PPT**：PDF 里没有段落与表格结构，只有一堆文字坐标，
  要还原成可编辑文档需要专门的版面重建引擎，本项目不含这个能力（PDF 只能转图片或纯文字）
- 表格只支持 **XLSX** 作为产出，不支持导出 CSV / TSV
- 不支持电子书互转（未集成 Calibre）
- HEIC 只能读不能写（ImageMagick 的限制），所以 `JPG → HEIC` 不可用
- 共享口令模式**不区分用户**；同一台电脑同一个浏览器上的两个人会共用一份列表
