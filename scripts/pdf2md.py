#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf2md.py —— PDF → Markdown 多后端转换器（质量降序 + 自动回退）

设计目标
--------
1. 不绑定任何单一识别器：MinerU / Marker / PaddleOCR / Docling / PyMuPDF4LLM / MarkItDown
   全部以「后端插件」形式注册，缺哪个跳哪个，失败即换下一个。
2. 新增识别器只需写一个 run_xxx() 函数并在 BACKENDS 里注册一行，无需改动主流程。
3. 自动判定扫描件：无文本层的 PDF 强制走 OCR 后端，避免轻量后端输出空文件。

用法
----
  python pdf2md.py --list-backends
  python pdf2md.py --pdf paper.pdf --out ./md_out
  python pdf2md.py --pdf paper.pdf --out ./md_out --backend auto
  python pdf2md.py --pdf paper.pdf --out ./md_out --backend docling,pymupdf4llm

输出
----
  <out>/<pdf 文件名>/<pdf 文件名>.md
  <out>/<pdf 文件名>/_convert_meta.json   （使用后端、耗时、文本层判定、后端产出的图片目录）
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import traceback

# Windows 控制台默认 GBK，强制 UTF-8 避免打印中文时崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

# --------------------------------------------------------------------------- #
# 后端注册表
#   quality : 综合质量分（越大越好），auto 模式按此降序尝试
#   probe   : 用于判断"是否已安装"的顶层模块名（可为多个候选）
#   ocr     : 是否具备 OCR 能力（无文本层 PDF 只有这些后端可用）
#   note    : 打印给人看的说明
# --------------------------------------------------------------------------- #
BACKENDS: dict[str, dict] = {
    "mineru": {
        "quality": 95,
        "probe": ["mineru"],
        "ocr": True,
        "note": "版面+公式+OCR+图片抽取，质量最高；模型约 6GB，建议 GPU",
    },
    "marker": {
        "quality": 90,
        "probe": ["marker"],
        "ocr": True,
        "note": "Surya 版面+OCR，质量接近 MinerU；GPU 更快，CPU 可用",
    },
    "paddleocr": {
        "quality": 85,
        "probe": ["paddleocr"],
        "ocr": True,
        "note": "PP-StructureV3 中文版面/表格识别强；GPU 可选",
    },
    "docling": {
        "quality": 75,
        "probe": ["docling"],
        "ocr": True,
        "note": "IBM 版面/表格识别好；纯 CPU 可用，部分 PDF 有 mupdf 兼容性 bug",
    },
    "pymupdf4llm": {
        "quality": 60,
        "probe": ["pymupdf4llm"],
        "ocr": False,
        "note": "轻量首选：纯 CPU、秒级、对已有文本层的论文足够用",
    },
    "markitdown": {
        "quality": 40,
        "probe": ["markitdown"],
        "ocr": False,
        "note": "最轻量纯文本兜底，丢失版面信息",
    },
}

# 无文本层判定阈值：前 N 页平均每页可提取字符数低于该值即视为扫描件
SCAN_PROBE_PAGES = 3
SCAN_CHAR_THRESHOLD = 120

MIN_OUTPUT_BYTES = 200  # 输出小于该字节数视为转换失败


def has_module(names: list[str] | str) -> bool:
    if isinstance(names, str):
        names = [names]
    return any(importlib.util.find_spec(n) is not None for n in names)


def installed(bname: str) -> bool:
    return has_module(BACKENDS[bname]["probe"])


def list_backends() -> int:
    print("=" * 74)
    print(f"{'后端':<13}{'质量分':<8}{'OCR':<6}{'状态':<10}说明")
    print("-" * 74)
    for name, spec in sorted(BACKENDS.items(), key=lambda kv: -kv[1]["quality"]):
        ok = "已安装" if installed(name) else "未安装"
        print(f"{name:<13}{spec['quality']:<8}{'是' if spec['ocr'] else '否':<6}{ok:<10}{spec['note']}")
    print("=" * 74)
    print("auto 模式顺序（跳过未安装/失败者）: "
          + " → ".join(order_backends("auto", scanned=False)))
    print("扫描件 auto 顺序（仅 OCR 后端）: "
          + " → ".join(order_backends("auto", scanned=True)))
    return 0


def order_backends(backend: str, scanned: bool) -> list[str]:
    if backend != "auto":
        names = [b.strip() for b in backend.split(",") if b.strip()]
        unknown = [b for b in names if b not in BACKENDS]
        if unknown:
            raise SystemExit(f"[pdf2md] 未知后端: {unknown}；可选: {sorted(BACKENDS)}")
        return names
    pool = [n for n, s in BACKENDS.items() if (s["ocr"] or not scanned)]
    return sorted(pool, key=lambda n: -BACKENDS[n]["quality"])


# --------------------------------------------------------------------------- #
# 文本层探测
# --------------------------------------------------------------------------- #
def text_layer_stats(pdf: str, pages: int = SCAN_PROBE_PAGES) -> dict:
    """返回前若干页的文本层统计，用于判断是否为扫描件。"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return {"available": False, "chars_per_page": None, "sampled": 0}

    doc = fitz.open(pdf)
    n = min(pages, doc.page_count)
    chars = 0
    for i in range(n):
        chars += len(doc[i].get_text("text").strip())
    doc.close()
    cpp = chars / n if n else 0
    return {
        "available": True,
        "chars_per_page": round(cpp, 1),
        "sampled": n,
        "is_scanned": cpp < SCAN_CHAR_THRESHOLD,
    }


# --------------------------------------------------------------------------- #
# 各后端实现
# --------------------------------------------------------------------------- #
def run_mineru(pdf: str, target_dir: str, md_path: str) -> str:
    """MinerU 官方 CLI；兼容不同版本的输出层级。"""
    cmd = [sys.executable, "-m", "mineru", "-p", pdf, "-o", target_dir]
    subprocess.run(cmd, check=True)
    if os.path.exists(md_path):
        return md_path
    for root, _dirs, files in os.walk(target_dir):
        for fn in sorted(files):
            if fn.endswith(".md"):
                return os.path.join(root, fn)
    raise FileNotFoundError("MinerU 未输出 .md 文件")


def run_marker(pdf: str, target_dir: str, md_path: str) -> str:
    """Marker：优先 CLI（跨版本最稳），CLI 不可用时退到 Python API。"""
    exe = shutil.which("marker_single")
    if exe:
        cmd = [exe, pdf, "--output_dir", target_dir, "--output_format", "markdown"]
        subprocess.run(cmd, check=True)
        for root, _dirs, files in os.walk(target_dir):
            for fn in sorted(files):
                if fn.endswith(".md"):
                    return os.path.join(root, fn)
        raise FileNotFoundError("marker_single 未输出 .md 文件")

    from marker.converters.pdf import PdfConverter            # type: ignore
    from marker.models import create_model_dict               # type: ignore
    from marker.output import text_from_rendered              # type: ignore

    converter = PdfConverter(artifact_dict=create_model_dict())
    rendered = converter(pdf)
    text, _ext, _images = text_from_rendered(rendered)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(text)
    return md_path


def run_paddleocr(pdf: str, target_dir: str, md_path: str) -> str:
    """PaddleOCR PP-StructureV3 → Markdown。"""
    from paddleocr import PPStructureV3  # type: ignore

    pipeline = PPStructureV3()
    outputs = pipeline.predict(pdf)
    chunks: list[str] = []
    for res in outputs:
        md = None
        for attr in ("markdown", "save_to_markdown"):
            obj = getattr(res, attr, None)
            if obj is None:
                continue
            md = obj() if callable(obj) else obj
            if md:
                break
        if not md:
            d = res.json if isinstance(res.json, dict) else {}
            md = d.get("markdown") or ""
        chunks.append(str(md))
    text = "\n\n".join(c for c in chunks if c)
    if not text.strip():
        raise RuntimeError("PaddleOCR 未返回 Markdown 内容（版本 API 可能不同）")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(text)
    return md_path


def run_docling(pdf: str, target_dir: str, md_path: str) -> str:
    """Docling；OCR 按需开启（扫描件才需要）。"""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.do_ocr = bool(_STATE.get("scanned"))
    opts.do_table_structure = True
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )
    result = converter.convert(pdf)
    text = result.document.export_to_markdown()
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(text)
    return md_path


def run_pymupdf4llm(pdf: str, target_dir: str, md_path: str) -> str:
    import pymupdf4llm  # type: ignore

    text = pymupdf4llm.to_markdown(pdf)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(text)
    return md_path


def run_markitdown(pdf: str, target_dir: str, md_path: str) -> str:
    from markitdown import MarkItDown  # type: ignore

    text = MarkItDown().convert(pdf).text_content
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(text)
    return md_path


RUNNERS = {
    "mineru": run_mineru,
    "marker": run_marker,
    "paddleocr": run_paddleocr,
    "docling": run_docling,
    "pymupdf4llm": run_pymupdf4llm,
    "markitdown": run_markitdown,
}

_STATE: dict = {}  # 运行时状态（是否扫描件等），供后端读取


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def convert(pdf: str, out_dir: str, backend: str) -> tuple[str, str]:
    stem = os.path.splitext(os.path.basename(pdf))[0]
    target_dir = os.path.join(out_dir, stem)
    os.makedirs(target_dir, exist_ok=True)
    md_path = os.path.join(target_dir, stem + ".md")

    stats = text_layer_stats(pdf)
    scanned = bool(stats.get("is_scanned"))
    _STATE["scanned"] = scanned
    print(f"[pdf2md] 文本层探测: {stats}")

    order = order_backends(backend, scanned=scanned)
    if scanned:
        print("[pdf2md] 判定为扫描件/无文本层 → 仅尝试具备 OCR 能力的后端")

    attempts: list[dict] = []
    for b in order:
        if not installed(b):
            print(f"[pdf2md] 后端 {b} 未安装，跳过")
            attempts.append({"backend": b, "status": "skipped"})
            continue
        if scanned and not BACKENDS[b]["ocr"]:
            print(f"[pdf2md] 后端 {b} 无 OCR 能力，扫描件模式下跳过")
            attempts.append({"backend": b, "status": "skipped-no-ocr"})
            continue

        t0 = time.time()
        print(f"[pdf2md] 尝试后端: {b} …")
        try:
            out = RUNNERS[b](pdf, target_dir, md_path)
            size = os.path.getsize(out)
            if size < MIN_OUTPUT_BYTES:
                raise RuntimeError(f"输出过小({size}B)，视为转换失败")
            dt = round(time.time() - t0, 1)
            print(f"[pdf2md] ✅ 成功: {out} ({size} bytes, {dt}s) via {b}")
            attempts.append({"backend": b, "status": "ok", "bytes": size, "seconds": dt})
            meta = {
                "pdf": os.path.abspath(pdf),
                "markdown": os.path.abspath(out),
                "backend": b,
                "seconds": dt,
                "bytes": size,
                "text_layer": stats,
                "attempts": attempts,
            }
            with open(os.path.join(target_dir, "_convert_meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            return out, b
        except Exception as exc:  # noqa: BLE001 —— 任何后端异常都只降级，不中断
            dt = round(time.time() - t0, 1)
            print(f"[pdf2md] ❌ 后端 {b} 失败（{dt}s）: {exc}")
            traceback.print_exc(limit=1)
            attempts.append({"backend": b, "status": "failed", "error": str(exc), "seconds": dt})

    raise RuntimeError(
        "所有后端均转换失败。请检查 PDF 有效性，或安装任一后端后重试。\n"
        "最小依赖:  pip install pymupdf4llm\n"
        "扫描件建议: pip install mineru   # 或 marker / paddleocr"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="PDF → Markdown 多后端转换器（质量降序自动回退）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--pdf", help="输入 PDF 路径")
    ap.add_argument("--out", help="输出目录")
    ap.add_argument("--backend", default="auto",
                    help="auto（默认）| 单个后端 | 逗号分隔的自定义顺序，如 docling,pymupdf4llm")
    ap.add_argument("--list-backends", action="store_true", help="打印后端检测矩阵后退出")
    ap.add_argument("--force", action="store_true", help="即使 .md 已存在也重新转换")
    args = ap.parse_args()

    if args.list_backends:
        return list_backends()

    if not args.pdf or not args.out:
        ap.error("需要 --pdf 与 --out（或用 --list-backends 查看后端）")
    if not os.path.exists(args.pdf):
        print(f"[pdf2md] PDF 不存在: {args.pdf}", file=sys.stderr)
        return 1

    stem = os.path.splitext(os.path.basename(args.pdf))[0]
    md_path = os.path.join(args.out, stem, stem + ".md")
    if os.path.exists(md_path) and not args.force:
        print(f"[pdf2md] 已存在，直接复用（--force 可强制重转）: {md_path}")
        return 0

    os.makedirs(args.out, exist_ok=True)
    out, used = convert(args.pdf, args.out, args.backend)
    print(f"[pdf2md] 完成。使用后端: {used}\n[pdf2md] 输出: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
