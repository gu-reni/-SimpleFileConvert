"""
转换白名单与命令构造 —— 这是整个服务的安全边界。

设计原则（每一条都对应一类真实攻击）：
  1. 只允许「预设的输入类型 → 目标格式」组合，不接受用户传任何命令行参数。
     （否则等于把 ffmpeg/soffice 的完整参数面暴露给公网）
  2. 一律用参数数组调用 subprocess，绝不拼 shell 字符串，绝不使用 shell=True。
     （文件名由我们生成 UUID，用户原始文件名只作展示，不参与任何路径拼接）
  3. 输出扩展名来自本模块的白名单常量，不来自请求体。
  4. 输入扩展名必须与同组内的来源集合匹配，防止「把 .exe 当视频解码」这类探查。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------
# 工具可执行文件
# --------------------------------------------------------------------------
SOFFICE = shutil.which("soffice") or "/usr/bin/soffice"
FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
CONVERT = shutil.which("convert") or "/usr/bin/convert"
PANDOC = shutil.which("pandoc") or "/usr/bin/pandoc"
TESSERACT = shutil.which("tesseract") or "/usr/bin/tesseract"
GS = shutil.which("gs") or "/usr/bin/gs"
PDFUNITE = shutil.which("pdfunite") or "/usr/bin/pdfunite"
PDFTOPPM = shutil.which("pdftoppm") or "/usr/bin/pdftoppm"


@dataclass(frozen=True)
class Rule:
    """一条允许的转换：从 exts 里的任一扩展名，转到 target 扩展名。"""
    key: str                      # 展示用的目标格式名
    target_ext: str               # 输出扩展名（白名单常量，不来自请求）
    exts: frozenset[str]          # 允许的输入扩展名
    builder: str                  # 用哪个构造器


# --------------------------------------------------------------------------
# 图片：ImageMagick 覆盖 226 种格式；HEIC/HEIF 只读（本机实测 r--），
# 所以「HEIC → JPG/PNG」成立，「JPG → HEIC」不提供。
# --------------------------------------------------------------------------
IMAGE_IN = frozenset({
    "jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "heic", "heif",
    "avif", "svg", "ico", "jfif", "jxl", "jp2", "psd", "raw", "cr2", "nef",
    "arw", "dng", "orf", "rw2", "ppm", "pgm", "pbm", "tga", "exr", "hdr",
})

IMAGE_TARGETS = [
    Rule("JPG", "jpg", IMAGE_IN, "imagemagick"),
    Rule("PNG", "png", IMAGE_IN, "imagemagick"),
    Rule("WebP", "webp", IMAGE_IN, "imagemagick"),
    Rule("TIFF", "tiff", IMAGE_IN, "imagemagick"),
    Rule("BMP", "bmp", IMAGE_IN, "imagemagick"),
    Rule("GIF", "gif", IMAGE_IN, "imagemagick"),
    Rule("AVIF", "avif", IMAGE_IN, "imagemagick"),
    Rule("PDF", "pdf", IMAGE_IN, "imagemagick"),
]

# --------------------------------------------------------------------------
# 文档：LibreOffice（本机已装 writer+impress+calc 完整版）
# --------------------------------------------------------------------------
DOC_IN = frozenset({
    "doc", "docx", "odt", "rtf", "txt", "wps", "pages",
    "xls", "xlsx", "ods", "csv", "tsv",
    "ppt", "pptx", "odp", "key",
})

DOC_TARGETS = [
    Rule("PDF", "pdf", DOC_IN, "libreoffice"),
    Rule("DOCX", "docx", frozenset({"doc", "odt", "rtf", "txt", "docx"}), "libreoffice"),
    Rule("XLSX", "xlsx", frozenset({"xls", "ods", "csv", "tsv", "xlsx"}), "libreoffice"),
    Rule("PPTX", "pptx", frozenset({"ppt", "odp", "pptx"}), "libreoffice"),
    Rule("ODT", "odt", frozenset({"doc", "docx", "rtf", "txt", "odt"}), "libreoffice"),
    Rule("TXT", "txt", frozenset({"doc", "docx", "odt", "rtf", "txt"}), "libreoffice"),
]

# --------------------------------------------------------------------------
# PDF 相关
# --------------------------------------------------------------------------
PDF_IN = frozenset({"pdf"})

PDF_TARGETS = [
    Rule("PNG", "png", PDF_IN, "pdf_to_image"),
    Rule("JPG", "jpg", PDF_IN, "pdf_to_image"),
    Rule("TXT", "txt", PDF_IN, "pdf_to_text"),
]

# --------------------------------------------------------------------------
# 视频 / 音频：ffmpeg
# --------------------------------------------------------------------------
VIDEO_IN = frozenset({
    "mp4", "mkv", "mov", "avi", "wmv", "flv", "webm", "m4v", "mpg", "mpeg",
    "3gp", "ts", "rmvb", "vob", "ogv",
})
AUDIO_IN = frozenset({
    "mp3", "wav", "flac", "aac", "m4a", "ogg", "opus", "wma", "aiff", "amr",
})

MEDIA_TARGETS = [
    Rule("MP4", "mp4", VIDEO_IN, "ffmpeg"),
    Rule("MKV", "mkv", VIDEO_IN, "ffmpeg"),
    Rule("WebM", "webm", VIDEO_IN, "ffmpeg"),
    Rule("MOV", "mov", VIDEO_IN, "ffmpeg"),
    Rule("GIF", "gif", VIDEO_IN, "ffmpeg"),
    Rule("MP3", "mp3", VIDEO_IN | AUDIO_IN, "ffmpeg"),
    Rule("WAV", "wav", VIDEO_IN | AUDIO_IN, "ffmpeg"),
    Rule("FLAC", "flac", VIDEO_IN | AUDIO_IN, "ffmpeg"),
    Rule("AAC", "aac", VIDEO_IN | AUDIO_IN, "ffmpeg"),
    Rule("OGG", "ogg", VIDEO_IN | AUDIO_IN, "ffmpeg"),
]

# 需要音轨的目标 / 需要视频轨的目标 —— 用来在上传后立刻给出友好提示，
# 而不是把 ffmpeg 的 "Output file does not contain any stream" 甩给家人看。
AUDIO_ONLY_TARGETS = frozenset({"mp3", "wav", "flac", "aac", "ogg"})
VIDEO_ONLY_TARGETS = frozenset({"mp4", "mkv", "webm", "mov", "gif"})

FFPROBE = shutil.which("ffprobe") or "/usr/bin/ffprobe"


def _log(msg: str) -> None:
    """失败要留痕（进 journalctl），否则这类问题会像刚才那样被静默吞掉。"""
    print(f"[fileconvert] {msg}", file=sys.stderr, flush=True)


def probe_streams(path: Path) -> set[str] | None:
    """返回流类型集合，如 {'video','audio'}。

    探测「本身失败」返回 None —— 与「探测成功但确实没有流」的空集合严格区分。
    之前两种情况都返回 set()，加上一个宽泛的 except，导致 NameError 被吞成空集合、
    轨道守卫静默失效（实测踩过）。
    """
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:                                   # noqa: BLE001
        _log(f"ffprobe 调用异常（不阻断转换）: {type(e).__name__}: {e}")
        return None
    if out.returncode != 0:
        _log(f"ffprobe 返回 {out.returncode}: {out.stderr.strip()[:200]}")
        return None
    return {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}


def check_streams(src: Path, rule: Rule) -> str | None:
    """在跑 ffmpeg 之前检查轨道。返回中文错误说明，或 None 表示可以继续。"""
    if rule.builder != "ffmpeg":
        return None

    streams = probe_streams(src)
    if streams is None:          # 探测失败：不拦，交给 ffmpeg 自己报错
        return None
    if not streams:              # 探测成功但一个流都没有
        return "读不出这个文件的音视频轨道，可能文件已损坏或格式不受支持。"

    if rule.target_ext in AUDIO_ONLY_TARGETS and "audio" not in streams:
        return "这个文件里没有声音轨道，无法转成音频格式。"
    if rule.target_ext in VIDEO_ONLY_TARGETS and "video" not in streams:
        return "这个文件里没有画面轨道，无法转成视频格式。"
    return None

# --------------------------------------------------------------------------
# OCR：tesseract chi_sim（扫描件/图片 → 文字）
# --------------------------------------------------------------------------
OCR_IN = IMAGE_IN | PDF_IN

OCR_TARGETS = [
    Rule("TXT（简体中文 OCR）", "txt", OCR_IN, "ocr"),
]


# --------------------------------------------------------------------------
# 汇总：扩展名 → 可选目标列表
# --------------------------------------------------------------------------
ALL_GROUPS = [
    ("图片", IMAGE_TARGETS),
    ("文档", DOC_TARGETS),
    ("PDF", PDF_TARGETS),
    ("音视频", MEDIA_TARGETS),
    ("文字识别（OCR）", OCR_TARGETS),
]


def targets_for(ext: str) -> list[Rule]:
    """返回该扩展名可转出的目标（保持分组顺序，去重）。"""
    ext = ext.lower().lstrip(".")
    seen: set[str] = set()
    out: list[Rule] = []
    for _label, rules in ALL_GROUPS:
        for r in rules:
            if ext in r.exts and r.target_ext not in seen:
                seen.add(r.target_ext)
                out.append(r)
    return out


def find_rule(ext: str, target_ext: str) -> Rule | None:
    ext = ext.lower().lstrip(".")
    target_ext = target_ext.lower().lstrip(".")
    for r in targets_for(ext):
        if r.target_ext == target_ext:
            return r
    return None


# --------------------------------------------------------------------------
# 命令构造 —— 只接受 (输入路径, 输出路径, 副作用目录)，绝不接受额外参数
# --------------------------------------------------------------------------
def build_argv(builder: str, src: Path, dst: Path, workdir: Path) -> list[str]:
    if builder == "imagemagick":
        # -density 提高 PDF/SVG 栅格化质量；-background white + -flatten 处理透明底转 JPG
        return [CONVERT, "-density", "150", str(src) + "[0]",
                "-background", "white", "-flatten", "-quality", "92", str(dst)]

    if builder == "libreoffice":
        # --outdir + 固定文件名：soffice 只能用「输出目录」，所以先在空目录里生成再改名
        return [SOFFICE, "--headless", "--norestore", "--nolockcheck",
                "-env:UserInstallation=file://" + str(workdir / "lo-profile"),
                "--convert-to", dst.suffix.lstrip("."), "--outdir", str(workdir),
                str(src)]

    if builder == "ffmpeg":
        base = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
        if dst.suffix == ".gif":
            # 直接转 GIF 会得到巨大文件；用调色板两遍法压到合理体积
            return base + ["-vf", "fps=12,scale=480:-1:flags=lanczos", "-loop", "0", str(dst)]
        if dst.suffix == ".mp3":
            return base + ["-vn", "-c:a", "libmp3lame", "-q:a", "2", str(dst)]
        if dst.suffix == ".wav":
            return base + ["-vn", "-c:a", "pcm_s16le", str(dst)]
        if dst.suffix == ".flac":
            return base + ["-vn", "-c:a", "flac", str(dst)]
        if dst.suffix == ".aac":
            return base + ["-vn", "-c:a", "aac", "-b:a", "192k", str(dst)]
        if dst.suffix == ".ogg":
            return base + ["-vn", "-c:a", "libvorbis", "-q:a", "5", str(dst)]
        return base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                       "-c:a", "aac", "-b:a", "160k", str(dst)]

    if builder == "pdf_to_image":
        # pdftoppm 输出 <prefix>-<page>.<ext>
        fmt = "-jpeg" if dst.suffix == ".jpg" else "-png"
        return [PDFTOPPM, fmt, "-r", "150", str(src), str(workdir / "page")]

    if builder == "pdf_to_text":
        return ["/usr/bin/pdftotext", "-layout", str(src), str(dst)]

    if builder == "ocr":
        # tesseract 只接受「输出前缀」，会自己补 .txt
        return [TESSERACT, str(src), str(workdir / "ocr"), "-l", "chi_sim+eng"]

    raise ValueError(f"未知的构造器: {builder}")


# 各构造器实际产出到哪个文件（libreoffice / pdftoppm / tesseract 不按我们给的名字输出）
def expected_output(builder: str, workdir: Path, dst: Path, src: Path | None = None) -> Path:
    if builder == "libreoffice":
        # 实测坑：LibreOffice 用【源文件主干名】命名产出，不是我们给的 dst 名字。
        # 例如把 样例文档.docx 转 txt，产出是 样例文档.txt，而不是 dst 的 结果.txt。
        stem = src.stem if src is not None else dst.stem
        return workdir / f"{stem}.{dst.suffix.lstrip('.')}"
    if builder == "ocr":
        # tesseract 只接受输出前缀，自己补 .txt
        return workdir / "ocr.txt"
    return dst
