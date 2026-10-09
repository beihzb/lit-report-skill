#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup_env.py —— lit-report 环境自检（默认只检查，不安装任何东西）

用法
----
  python setup_env.py                      # 只体检：报告各 PDF 识别后端是否可用
  python setup_env.py --json               # 机器可读输出（供 agent 解析）
  python setup_env.py --install            # 显式同意后再安装最小依赖（pymupdf + pymupdf4llm）
  python setup_env.py --install --with docling,markitdown
  python setup_env.py --env-name <名字>    # 指定 conda 环境名（默认 litreport）

设计原则
--------
1. **默认零副作用**：不加 --install 时绝不安装、升级、删除任何包，也不创建环境。
2. **不硬编码个人环境**：环境名通过 --env-name 传入；本机已有可用环境时直接复用。
3. **conda 优先、venv 兜底**：--install 时若有 conda 则建 conda 环境，否则建 venv。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

# 最小依赖：PyMuPDF 负责自动抽图，pymupdf4llm 负责轻量 PDF→Markdown，mineru 默认推荐
MIN_DEPS = ["pymupdf", "pymupdf4llm"]
RECOMMENDED = ["mineru"]  # 默认推荐安装但不强制

# 可选增强后端：模块名 → pip 包名
OPTIONAL = {
    "mineru": "mineru[core]",
    "marker": "marker-pdf",
    "paddleocr": "paddleocr",
    "docling": "docling",
    "markitdown": "markitdown",
}

OPTIONAL_NOTE = {
    "mineru": "版面+公式+OCR+图片，质量最高；模型约 6GB，建议 GPU（默认推荐）",
    "marker": "Surya 版面+OCR，质量接近 MinerU；CPU 可用、GPU 更快",
    "paddleocr": "PP-StructureV3，中文版面/表格识别强",
    "docling": "IBM 版面/表格识别好，纯 CPU 可用",
    "markitdown": "最轻量纯文本兜底",
}


def has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def check_pubmed_mcp() -> bool:
    """检查 PubMed MCP 是否可用（通过尝试列举资源）。"""
    try:
        # 这里只是标记，实际检查由 agent 调用 list_mcp_resources 完成
        # setup_env.py 无法直接调用 MCP 工具
        return None  # 标记为"需要 agent 检查"
    except Exception:
        return False


def probe(check_mcp: bool = False) -> dict:
    result = {
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "pymupdf": has("fitz") or has("pymupdf"),
        "pymupdf4llm": has("pymupdf4llm"),
        "mineru": has("mineru"),
        "marker": has("marker"),
        "paddleocr": has("paddleocr"),
        "docling": has("docling"),
        "markitdown": has("markitdown"),
    }
    if check_mcp:
        result["pubmed_mcp"] = check_pubmed_mcp()
    return result


def conda_bin() -> str | None:
    return shutil.which("conda") or os.environ.get("CONDA_EXE") or None


def env_python(exe: str, env_name: str) -> str | None:
    """由 conda 可执行文件路径推导 <root>/envs/<name>/python(.exe)。"""
    exe_dir = os.path.dirname(exe)
    py = "python.exe" if os.name == "nt" else "bin/python"
    bases = {exe_dir}
    for tail in ("Library\\bin", "Library/bin", "Scripts", "bin"):
        if exe_dir.endswith(tail):
            bases.add(exe_dir[: -len(tail)])
    for b in bases:
        p = os.path.join(b, "envs", env_name, py)
        if os.path.exists(p):
            return p
    return None


def run(cmd: list[str]) -> int:
    print("  $", " ".join(cmd))
    return subprocess.run(cmd).returncode


def do_install(env_name: str, extra: list[str], include_recommended: bool = True) -> int:
    target = sys.executable
    create_hint = ""
    if not (has("fitz") or has("pymupdf")) or not has("pymupdf4llm"):
        cb = conda_bin()
        if cb:
            print(f"[setup] 检测到 conda: {cb}")
            if run([cb, "create", "-n", env_name, "python=3.11", "-y"]) != 0:
                print("[setup] conda create 失败，请你手动建好环境后重跑本脚本")
                return 2
            py = env_python(cb, env_name)
            if py is None:
                print(f"[setup] 已创建环境 {env_name}，请激活后重跑：conda activate {env_name}")
                return 2
            target = py
            create_hint = f"conda activate {env_name}"
        else:
            venv_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".venv")
            if run([sys.executable, "-m", "venv", venv_dir]) != 0:
                print("[setup] venv 创建失败，请安装 Python 3.9+ 后重试")
                return 2
            target = os.path.join(venv_dir, "Scripts" if os.name == "nt" else "bin", "python")
            create_hint = target

    pkgs = [p for p in MIN_DEPS if not has(p.replace("pymupdf", "fitz"))]
    if include_recommended:
        pkgs += [OPTIONAL[r] for r in RECOMMENDED if not has(r)]
    pkgs += [OPTIONAL[e] for e in extra]
    pkgs = list(dict.fromkeys(pkgs))
    if not pkgs:
        print("[setup] 依赖已齐备，无需安装")
        return 0
    print(f"[setup] 安装: {pkgs}")
    run([target, "-m", "pip", "install", "--upgrade", "pip"])
    if run([target, "-m", "pip", "install"] + pkgs) != 0:
        print("[setup] 安装失败，请检查网络 / pip 源")
        return 1
    print("[setup] ✅ 安装完成")
    if create_hint:
        print(f"[setup] 后续使用前先激活环境: {create_hint}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="lit-report 环境自检（默认不安装任何东西）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--minimal", action="store_true", help="最小检查模式：只检查 pymupdf + pymupdf4llm")
    ap.add_argument("--install", action="store_true",
                    help="显式确认后才安装最小依赖（及 --with 指定的可选后端）")
    ap.add_argument("--with", dest="with_", default="",
                    help="逗号分隔的可选后端: " + ",".join(OPTIONAL))
    ap.add_argument("--no-recommended", action="store_true", 
                    help="--install 时不自动安装推荐后端（mineru）")
    ap.add_argument("--env-name", default="litreport", help="--install 时新建的 conda 环境名")
    args = ap.parse_args()

    info = probe(check_mcp=not args.minimal)
    info["conda"] = conda_bin()
    info["min_ok"] = bool(info["pymupdf"] and info["pymupdf4llm"])

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print("=" * 66)
        print("lit-report 环境体检" + (" [最小模式]" if args.minimal else ""))
        print("=" * 66)
        print(f"Python        : {info['python_version']}  ({info['python']})")
        print(f"conda         : {info['conda'] or '未检测到（--install 时改用 venv）'}")
        print(f"PyMuPDF       : {'OK' if info['pymupdf'] else '缺失'}   ← 自动抽图必需")
        print(f"pymupdf4llm   : {'OK' if info['pymupdf4llm'] else '缺失'}   ← 轻量 PDF→MD 必需")
        
        if not args.minimal:
            print("-" * 66)
            for k, note in OPTIONAL_NOTE.items():
                mark = "OK  " if info.get(k) else "未装"
                print(f"  {k:<12}{mark}  {note}")
            
            if "pubmed_mcp" in info:
                mcp_status = "需要 agent 检查" if info["pubmed_mcp"] is None else ("OK" if info["pubmed_mcp"] else "不可用")
                print(f"\nPubMed MCP    : {mcp_status}   ← 背景调研必需（agent 级别检查）")
        
        print("=" * 66)
        if info["min_ok"]:
            print("最小依赖齐备，可以直接使用 lit-report。")
        else:
            print("最小依赖不完整。安装（需你明确同意）：")
            print("  python setup_env.py --install")
        
        if not args.minimal:
            print("可选增强后端示例：")
            print("  python setup_env.py --install --with docling,markitdown")
            print("  python setup_env.py --install --no-recommended  # 不安装 MinerU")

    if args.install:
        extra = [x.strip() for x in args.with_.split(",") if x.strip()]
        bad = [x for x in extra if x not in OPTIONAL]
        if bad:
            print(f"[setup] 未知的可选后端: {bad}；可选: {sorted(OPTIONAL)}", file=sys.stderr)
            return 1
        return do_install(args.env_name, extra, include_recommended=not args.no_recommended)
    return 0


if __name__ == "__main__":
    sys.exit(main())
