#!/usr/bin/env python3
"""端到端转换测试 —— 直接走 conversions.build_argv + subprocess，验证真实产出。

用法：.venv/bin/python test_conversions.py
"""
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import conversions as cv

ROOT = Path(__file__).resolve().parent
FIX = ROOT / "testdata"


def make_fixtures() -> dict[str, Path]:
    """用本机已有工具造测试文件（不需要外网）。"""
    FIX.mkdir(exist_ok=True)
    made: dict[str, Path] = {}

    # 带中文的图（同时供 OCR 用）
    png = FIX / "样例图.png"
    subprocess.run([cv.CONVERT, "-size", "1000x300", "xc:white", "-font",
                    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
                    "-pointsize", "46", "-fill", "black",
                    "-annotate", "+50+120", "季度销售汇总",
                    "-annotate", "+50+220", "华东大区 1258.40 万元", str(png)],
                   check=True, capture_output=True)
    made["png"] = png

    # docx：用 LibreOffice 从纯文本转出（不依赖 officecli，避免其守护进程状态干扰测试）
    txt_src = FIX / "源文.txt"
    txt_src.write_text("格式转换测试文档\n第二行内容。\n", encoding="utf-8")
    subprocess.run([cv.SOFFICE, "--headless", "--norestore", "--convert-to", "docx",
                    "--outdir", str(FIX), str(txt_src)],
                   check=True, capture_output=True, timeout=180)
    docx = FIX / "源文.docx"
    made["docx"] = docx

    # mp4：ffmpeg 生成 2 秒彩条 + **带音轨**（否则 mp3 转换测不出来）
    mp4 = FIX / "样例视频.mp4"
    subprocess.run([cv.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(mp4)], check=True, capture_output=True)
    made["mp4"] = mp4

    # 无音轨视频：用来验证「没有声音轨道」的友好提示
    mp4_na = FIX / "无音轨视频.mp4"
    subprocess.run([cv.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(mp4_na)],
                   check=True, capture_output=True)
    made["mp4_na"] = mp4_na

    return made


def run_case(label: str, src: Path, target_ext: str,
             expect_reject: bool = False) -> tuple[bool, str]:
    ext = src.suffix.lstrip(".")
    rule = cv.find_rule(ext, target_ext)
    if rule is None:
        return False, f"白名单拒绝 {ext} → {target_ext}"

    # 轨道预检（app.py 里也走这一层）
    stream_err = cv.check_streams(src, rule)
    if expect_reject:
        return (True, f"已按预期拒绝：{stream_err}") if stream_err else \
               (False, "本该拒绝却通过了轨道检查")
    if stream_err:
        return False, f"轨道检查拦下：{stream_err}"

    work = Path(tempfile.mkdtemp(prefix="fctest-"))
    dst = work / f"{src.stem}.{target_ext}"
    try:
        argv = cv.build_argv(rule.builder, src, dst, work)
        t0 = time.time()
        p = subprocess.run(argv, capture_output=True, cwd=str(work), timeout=300)
        dt = time.time() - t0
        if p.returncode != 0:
            return False, f"退出码 {p.returncode}: {p.stderr.decode('utf-8','replace')[-200:]}"

        produced = cv.expected_output(rule.builder, work, dst, src)
        extra = ""
        if not produced.exists():
            cands = [x for x in work.iterdir()
                     if x.is_file() and x.suffix.lstrip(".") == target_ext and x != src]
            if not cands:
                return False, "没有产出文件"
            produced = cands[0]
            if len(cands) > 1:
                produced = __import__("app").pack_zip(work, cands, {})
                extra = f"（{len(cands)} 个文件打包）"

        size = produced.stat().st_size
        if size == 0:
            return False, "产出为空文件"
        return True, f"{size/1024:.0f} KB  {dt:.1f}s  → {produced.name}{extra}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    print("=== 造测试文件 ===")
    fx = make_fixtures()
    for k, v in fx.items():
        print(f"  {k:>5}: {v.name} ({v.stat().st_size/1024:.0f} KB)")

    print("\n=== 转换测试 ===")
    cases = [
        ("图片互转 (PNG→JPG)",      fx["png"],    "jpg",   False),
        ("图片互转 (PNG→WebP)",     fx["png"],    "webp",  False),
        ("图片→PDF",                fx["png"],    "pdf",   False),
        ("OCR 中文 (PNG→TXT)",      fx["png"],    "txt",   False),
        ("文档→PDF (docx→pdf)",     fx["docx"],   "pdf",   False),
        ("文档→TXT (docx→txt)",     fx["docx"],   "txt",   False),
        ("视频→音频 (mp4→mp3)",     fx["mp4"],    "mp3",   False),
        ("视频→GIF (mp4→gif)",      fx["mp4"],    "gif",   False),
        ("无音轨视频→mp3（应拒绝）",  fx["mp4_na"], "mp3",   True),
    ]
    ok = bad = 0
    for label, src, tgt, rej in cases:
        good, msg = run_case(label, src, tgt, expect_reject=rej)
        print(f"  {'✅' if good else '❌'} {label:<28} {msg}")
        ok, bad = (ok + 1, bad) if good else (ok, bad + 1)

    # 2 页 PDF：用来验证「多文件产出自动打包成 zip」这条路径
    p1, p2 = FIX / "页1.png", FIX / "页2.png"
    for tag, out in (("第一页", p1), ("第二页", p2)):
        subprocess.run([cv.CONVERT, "-size", "400x200", "xc:white", "-pointsize", "30",
                        "-annotate", "+20+100", tag, str(out)], check=True, capture_output=True)
    pdf2 = FIX / "两页.pdf"
    subprocess.run([cv.CONVERT, str(p1), str(p2), str(pdf2)], check=True, capture_output=True)

    good, msg = run_case("PDF→PNG（2页→打包zip）", pdf2, "png")
    print(f"  {'✅' if good else '❌'} {'PDF→PNG（2页→打包zip）':<28} {msg}")
    ok, bad = (ok + 1, bad) if good else (ok, bad + 1)

    print(f"\n通过 {ok} / 失败 {bad}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
