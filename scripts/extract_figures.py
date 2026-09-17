#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_figures.py —— 从 PDF 自动抽取 Figure / 附图 / 表格为 PNG（无需手工截图）

思路
----
1. 在 PDF 文本层里按期刊常见格式识别图注行（Figure / Fig. / Supplementary Figure /
   Fig. S1 / Extended Data Fig. / Table / 图 / 附表 …），并合并续行得到图注块。
2. 以图注块为锚点：收集页面上与该图注水平重叠的「图形元素」（位图 XObject +
   矢量绘图），按 y 方向间隙链式聚合出图形核心区；再跨面板行的空白把同一张图
   补齐，遇到整栏宽的正文段落则停止（防止吞掉正文）。
3. 300 DPI 渲染裁剪为 PNG，输出 figures.json 记录编号 → 绝对路径、页码、bbox、置信度。

可靠性设计
----------
* 脚本只做「自动定位 + 输出置信度」，不宣称 100% 正确：
  high   找到图形元素、区域未超页高 75%
  medium 找到图形元素但区域较大，边界可能偏松
  low    未找到图形元素，退化为空白带切分（多为纯矢量图或异常版面）
* `--debug` 输出每页框选 overlay，供 AI/人目视复核。
* `crop` 子命令支持按像素坐标手工重裁，作为复核不通过时的兜底。
* 自动排除每页重复出现的页眉/页脚文本带，避免被当作图内轴标题吸收。

子命令
------
  auto           自动抽图（默认）
  list-captions  只列出识别到的图注（dry-run，不写图）
  render-pages   整页渲染 PNG（供 AI 目视定位后手工裁剪）
  crop           按指定 bbox 手工裁剪

示例
----
  python extract_figures.py auto --pdf paper.pdf --out ./out --debug --teaser
  python extract_figures.py list-captions --pdf paper.pdf
  python extract_figures.py render-pages --pdf paper.pdf --out ./out --pages 3,4 --dpi 200
  python extract_figures.py crop --pdf paper.pdf --out ./out --page 3 \
      --px 60,120,540,430 --dpi 200 --name fig1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    sys.stderr.write("[figures] 需要 PyMuPDF：pip install pymupdf\n")
    raise SystemExit(2)

# Windows 控制台默认 GBK，强制 UTF-8，避免打印中文/特殊字符时崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

# 软连字符、零宽字符、连字等常见 PDF 噪声
_TEXT_CLEAN = {0x00AD: None, 0x200B: None, 0xFEFF: None, 0x2010: "-", 0x2011: "-",
               0xFB01: "fi", 0xFB02: "fl"}


def clean_text(s: str) -> str:
    return (s or "").translate(_TEXT_CLEAN).strip()


# --------------------------------------------------------------------------- #
# 图注识别
# --------------------------------------------------------------------------- #
CAPTION_RE = re.compile(
    r"^\s*"
    r"(?P<prefix>(?:Supplementary|Supplemental|Supporting|Extended\s+Data|Extended)\s+)?"
    r"(?P<kind>Figure|FIGURE|Fig|FIG|Table|TABLE|Tab|Chart|Scheme|附图|图|表|附表)"
    r"\.?\s*"
    r"(?P<num>S?\d+|[IVXLC]+)"
    r"\s*(?P<sep>[:.|\u2013\u2014\-]?)"
    r"\s*(?P<rest>.*)$"
)

# 行内出现这些词说明是「图形摘要/目录」而非正文图注
FALSE_POSITIVE_HINTS = ("graphical abstract", "table of contents")


def classify(prefix: str | None, kind: str) -> str:
    kind_l = kind.lower()
    prefix_l = (prefix or "").lower()
    if "extended" in prefix_l:
        return "extfig"
    if "supp" in prefix_l or "supporting" in prefix_l:
        return "sfig"
    if kind_l in ("table", "tab", "表", "附表"):
        return "table"
    return "fig"


def caption_kind_label(prefix: str | None, kind: str, num: str) -> str:
    parts = []
    if prefix:
        parts.append(prefix.strip())
    parts.append(kind.strip())
    return " ".join(parts) + " " + num


# --------------------------------------------------------------------------- #
# 几何工具
# --------------------------------------------------------------------------- #
def merge_intervals(iv: list[tuple[float, float]], tol: float = 1.0) -> list[tuple[float, float]]:
    if not iv:
        return []
    iv = sorted(iv)
    out = [list(iv[0])]
    for a, b in iv[1:]:
        if a <= out[-1][1] + tol:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def rect_gap(a: "fitz.Rect", b: "fitz.Rect") -> float:
    """两个矩形的最近距离；重叠返回 0。"""
    dx = max(a.x0 - b.x1, b.x0 - a.x1, 0.0)
    dy = max(a.y0 - b.y1, b.y0 - a.y1, 0.0)
    return (dx * dx + dy * dy) ** 0.5


def y_gap(a: "fitz.Rect", b: "fitz.Rect") -> float:
    """仅按 y 方向计算间距（x 已由栏带约束，不应参与距离）。"""
    return max(a.y0 - b.y1, b.y0 - a.y1, 0.0)


def x_overlap(r: "fitz.Rect", a: float, b: float) -> float:
    return max(0.0, min(r.x1, b) - max(r.x0, a))


def safe_rect(r) -> "fitz.Rect | None":
    try:
        rr = fitz.Rect(r)
    except Exception:
        return None
    if rr.is_empty or rr.is_infinite:
        return None
    return rr


# --------------------------------------------------------------------------- #
# 页面元素采集
# --------------------------------------------------------------------------- #
def text_lines(page: "fitz.Page") -> list[dict]:
    lines = []
    d = page.get_text("dict")
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        blk_lines = b.get("lines", [])
        for idx, line in enumerate(blk_lines):
            spans = line.get("spans", [])
            if not spans:
                continue
            txt = clean_text("".join(s.get("text", "") for s in spans))
            if not txt:
                continue
            r = safe_rect(line.get("bbox"))
            if r is None:
                continue
            lines.append({
                "text": txt,
                "rect": r,
                "size": max(float(s.get("size", 0)) for s in spans),
                "line_idx": idx,
                "n_lines": len(blk_lines),
                "blk_bbox": safe_rect(b.get("bbox")) or r,
            })
    return lines


def running_bands(doc, probe: int = 25, frac: float = 0.5, zone: float = 72.0) -> list[tuple]:
    """检测每页重复出现的页眉/页脚文本带（按 y 聚类 + 出现页数占比）。

    期刊 PDF 的页眉页脚出现在几乎所有页面同一 y 位置，若不排除，会被当成
    「图内轴标题」吸收进图形区域（实测造成图形顶部多出约 17pt 噪声）。
    """
    n = min(probe, doc.page_count)
    if n < 2:
        return []
    hits: dict[int, set[int]] = {}
    for i in range(n):
        page = doc[i]
        h = page.rect.height
        seen = set()
        for ln in text_lines(page):
            r = ln["rect"]
            if r.y1 > zone and r.y0 < h - zone:
                continue
            seen.add(int(round(r.y0 / 4.0)) * 4)
        for key in seen:
            hits.setdefault(key, set()).add(i)
    need = max(2, int(frac * n))
    bands = [(k - 3.0, k + 16.0) for k, pages in hits.items() if len(pages) >= need]
    return merge_intervals(bands, tol=6.0)


def filter_running(lines: list[dict], bands: list[tuple]) -> list[dict]:
    if not bands:
        return lines
    keep = []
    for ln in lines:
        mid = (ln["rect"].y0 + ln["rect"].y1) / 2.0
        if any(a <= mid <= b for a, b in bands):
            continue
        keep.append(ln)
    return keep


def graphic_rects(page: "fitz.Page") -> list["fitz.Rect"]:
    """位图 XObject + 矢量绘图，过滤明显装饰性元素。"""
    out: list["fitz.Rect"] = []
    pr = page.rect
    page_area = max(1.0, pr.width * pr.height)

    for info in page.get_image_info():
        r = safe_rect(info.get("bbox"))
        if r is None or r.width < 6 or r.height < 6:
            continue
        out.append(r)

    for d in page.get_drawings():
        r = safe_rect(d.get("rect"))
        if r is None or r.width < 2 or r.height < 2:
            continue
        if abs(r) > 0.92 * page_area:
            continue  # 整页背景/外框
        if r.height < 3 and r.width > 0.8 * pr.width:
            continue  # 页眉/页脚横线
        if r.width < 3 and r.height > 0.8 * pr.height:
            continue  # 分栏竖线
        out.append(r)
    return out


def find_captions(lines: list[dict]) -> list[dict]:
    caps = []
    for i, ln in enumerate(lines):
        m = CAPTION_RE.match(ln["text"])
        if not m:
            continue
        # 图注一般为文本块首行；正文里的"如图 1 所示"不会出现在行首
        if ln["line_idx"] != 0:
            continue
        low = ln["text"].lower()
        if any(h in low for h in FALSE_POSITIVE_HINTS):
            continue
        rest = (m.group("rest") or "").strip()
        if len(rest) < 8 and ln["n_lines"] < 2:
            continue
        num = m.group("num") or ""
        if not num:
            continue
        caps.append({
            "idx": i,
            "kind_raw": m.group("kind"),
            "prefix_raw": m.group("prefix"),
            "num": num,
            "text": ln["text"],
            "rect": fitz.Rect(ln["rect"]),
            "size": ln["size"],
            "blk_bbox": fitz.Rect(ln["blk_bbox"]),
        })
    return caps


def expand_caption_block(cap: dict, lines: list[dict], max_gap: float = 7.0) -> "fitz.Rect":
    """把图注块本身以及紧邻的续行/续块并入图注矩形。"""
    rect = fitz.Rect(cap["rect"])
    cur = rect
    for _ in range(24):
        best = None
        for i, ln in enumerate(lines):
            if i == cap["idx"]:
                continue
            if CAPTION_RE.match(ln["text"]) and ln["line_idx"] == 0:
                continue  # 新的图注，不能并进来
            gap = ln["rect"].y0 - cur.y1
            if -1.0 <= gap <= max_gap and ln["rect"].x0 <= rect.x0 + 4:
                if ln["size"] > cap["size"] * 1.25:
                    continue
                if best is None or ln["rect"].y0 < best[1]["rect"].y0:
                    best = (i, ln)
        if best is None:
            break
        cur = cur | best[1]["rect"]
    return cur


# --------------------------------------------------------------------------- #
# 图形区域定位
# --------------------------------------------------------------------------- #
def band_for(cap: "fitz.Rect", graphics: list["fitz.Rect"], reach: float = 420.0) -> tuple[float, float]:
    """确定该图注所在「栏带」的 x 范围。"""
    x0, x1 = cap.x0, cap.x1
    for g in graphics:
        if rect_gap(g, cap) > reach:
            continue
        x0 = min(x0, g.x0)
        x1 = max(x1, g.x1)
    return x0, x1


def chain_core(cands: list["fitz.Rect"], cap: "fitz.Rect", side: str,
               gap: float, adjacent: float) -> tuple["fitz.Rect | None", str]:
    """从离图注最近的图形元素出发，按 y 间隙迭代吸收，得到图形核心区。"""
    if not cands:
        return None, "no-graphics"

    key = (lambda r: cap.y0 - r.y1) if side == "above" else (lambda r: r.y0 - cap.y1)
    cands = sorted(cands, key=key)
    core = fitz.Rect(cands[0])
    if y_gap(core, cap) > adjacent:
        return None, f"nearest-graphic-too-far({y_gap(core, cap):.0f}pt)"

    for _ in range(200):
        changed = False
        for r in cands:
            if y_gap(core, r) <= gap:
                merged = core | r
                if merged != core:
                    core = merged
                    changed = True
        if not changed:
            break

    if side == "above":
        core = fitz.Rect(core.x0, core.y0, core.x1, min(core.y1, cap.y0))
    else:
        core = fitz.Rect(core.x0, max(core.y0, cap.y1), core.x1, core.y1)
    return core, "ok"


def ink_intervals(lines: list[dict], graphics: list["fitz.Rect"],
                  bx0: float, bx1: float, band_w: float,
                  exclude: list["fitz.Rect"]) -> list[list]:
    """把栏带内的墨迹（文本行 + 图形）并成 y 方向区间。

    每个区间: [y0, y1, 是否含图形元素, 文本最大宽度占比]
    宽度占比用于区分「图内轴标题/面板字母」（窄）与「正文段落」（几乎满栏）。
    """
    iv: list[tuple] = []
    for g in graphics:
        if x_overlap(g, bx0, bx1) <= 0:
            continue
        iv.append((g.y0, g.y1, True, 0.0))
    for ln in lines:
        r = ln["rect"]
        if x_overlap(r, bx0, bx1) <= 0:
            continue
        if any(r.intersects(c) for c in exclude):
            continue
        iv.append((r.y0, r.y1, False, min(1.0, r.width / max(1.0, band_w))))
    if not iv:
        return []
    iv.sort()
    merged: list[list] = []
    for y0, y1, isg, wr in iv:
        if merged and y0 <= merged[-1][1] + 1.0:
            m = merged[-1]
            m[1] = max(m[1], y1)
            m[2] = m[2] or isg
            m[3] = max(m[3], wr)
        else:
            merged.append([y0, y1, isg, wr])
    return merged


def extend_region(region: "fitz.Rect", cap: "fitz.Rect", side: str,
                  intervals: list[list], opt, page_rect: "fitz.Rect") -> "fitz.Rect":
    """沿 y 方向把图形核心区补齐成完整的图。

    期刊图常由多个面板行拼成，行间空白远大于 --chain-gap；只要间隙里仍是
    图形就继续桥接，遇到整栏宽的正文段落则停止。
    """
    out = fitz.Rect(region)
    max_h = opt.max_fig_height * page_rect.height

    def accept(iv: list, gap: float) -> bool:
        if iv[2]:
            return True
        return (gap <= opt.absorb and iv[3] < 0.75
                and (iv[1] - iv[0]) < 0.06 * page_rect.height)

    def step_up() -> bool:
        nonlocal out
        # 合并后的区间可能是"图形+紧邻文本"的混合带，与当前区域重叠时也要吸收
        for iv in intervals:
            if iv[2] and iv[1] > out.y0 + 1.0 and iv[0] < out.y0 - 1.0:
                new = fitz.Rect(out.x0, iv[0], out.x1, out.y1)
                if new.height <= max_h:
                    out = new
                    return True
        cands = [iv for iv in intervals if iv[1] <= out.y0 + 1.0]
        if not cands:
            return False
        iv = max(cands, key=lambda t: t[1])
        gap = out.y0 - iv[1]
        if gap > opt.bridge_gap or not accept(iv, gap):
            return False
        new = fitz.Rect(out.x0, iv[0], out.x1, out.y1)
        if new.height > max_h:
            return False
        out = new
        return True

    def step_down() -> bool:
        nonlocal out
        for iv in intervals:
            if iv[2] and iv[0] < out.y1 - 1.0 and iv[1] > out.y1 + 1.0:
                new = fitz.Rect(out.x0, out.y0, out.x1, iv[1])
                if new.height <= max_h:
                    out = new
                    return True
        cands = [iv for iv in intervals if iv[0] >= out.y1 - 1.0]
        if not cands:
            return False
        iv = min(cands, key=lambda t: t[0])
        gap = iv[0] - out.y1
        if gap > opt.bridge_gap or not accept(iv, gap):
            return False
        new = fitz.Rect(out.x0, out.y0, out.x1, iv[1])
        if new.height > max_h:
            return False
        out = new
        return True

    fn = step_up if side == "above" else step_down
    for _ in range(200):
        if not fn():
            break
    return out


def cut_at_paragraph(region: "fitz.Rect", intervals: list[list], side: str,
                     page_h: float) -> tuple["fitz.Rect", str]:
    """若区域里混进了整栏宽的正文段落，就在段落处切掉。

    最后一道闸：纯文本、接近满栏宽、且有段落高度的墨迹区间才认定为正文屏障。
    """
    min_h = max(24.0, 0.035 * page_h)
    barriers = [iv for iv in intervals
                if (not iv[2]) and iv[3] >= 0.8 and (iv[1] - iv[0]) >= min_h
                and iv[0] >= region.y0 + 2 and iv[1] <= region.y1 - 2]
    if not barriers:
        return region, ""
    if side == "above":
        b = min(barriers, key=lambda t: t[0])
        if b[1] + 2 < region.y1 - 20:
            return fitz.Rect(region.x0, b[1] + 2, region.x1, region.y1), "cut-at-paragraph"
    else:
        b = max(barriers, key=lambda t: t[1])
        if b[0] - 2 > region.y0 + 20:
            return fitz.Rect(region.x0, region.y0, region.x1, b[0] - 2), "cut-at-paragraph"
    return region, ""


def fallback_whitespace(cap: "fitz.Rect", lines: list[dict], graphics: list["fitz.Rect"],
                        bx0: float, bx1: float, gap: float,
                        page_rect: "fitz.Rect") -> "fitz.Rect | None":
    """无图形元素时的退化方案：从图注向上做 1D 间隔链式合并。"""
    iv = []
    for r in [ln["rect"] for ln in lines] + graphics:
        if x_overlap(r, bx0, bx1) <= 0.15 * max(1.0, bx1 - bx0):
            continue
        if r.y1 > cap.y0:
            continue
        iv.append((r.y0, r.y1))
    bands = merge_intervals(iv, tol=gap)
    top = cap.y0
    for a, b in sorted(bands, key=lambda t: -t[1]):
        if b > top + 1:
            continue
        if (top - b) <= gap:
            top = a
        else:
            break
    if cap.y0 - top < 30:
        return None
    return fitz.Rect(min(bx0, cap.x0), max(page_rect.y0, top), max(bx1, cap.x1), cap.y0)


def locate_figure(page: "fitz.Page", cap: "fitz.Rect", cap_lines: list["fitz.Rect"],
                  lines: list[dict], graphics: list["fitz.Rect"], opt) -> dict:
    bx0, bx1 = band_for(cap, graphics)
    band_w = max(1.0, bx1 - bx0)
    intervals = ink_intervals(lines, graphics, bx0, bx1, band_w, exclude=cap_lines)
    above = [g for g in graphics if g.y1 <= cap.y0 + 3 and x_overlap(g, bx0, bx1) > 0]
    below = [g for g in graphics if g.y0 >= cap.y1 - 3 and x_overlap(g, bx0, bx1) > 0]

    results = []
    for side, cands in (("above", above), ("below", below)):
        core, why = chain_core(cands, cap, side, opt.chain_gap, opt.adjacent)
        if core is None:
            continue
        region = extend_region(core, cap, side, intervals, opt, page.rect)
        if side == "above":
            region = fitz.Rect(region.x0, region.y0, region.x1, min(region.y1, cap.y0) - 1)
        else:
            region = fitz.Rect(region.x0, max(region.y0, cap.y1) + 1, region.x1, region.y1)
        # X 方向对齐图注栏宽：期刊图的宽度基本等于图注宽度
        region = fitz.Rect(min(region.x0, cap.x0), region.y0,
                           max(region.x1, cap.x1), region.y1)
        region, cut = cut_at_paragraph(region, intervals, side, page.rect.height)
        if region.height < 25 or region.width < 40:
            continue
        results.append((y_gap(region, cap), side, region,
                        "; ".join(x for x in (why, cut) if x)))

    if results:
        results.sort(key=lambda t: t[0])
        _, side, region, note = results[0]
        conf = "high" if region.height <= 0.75 * page.rect.height else "medium"
        return {"region": region, "side": side, "confidence": conf, "note": note}

    region = fallback_whitespace(cap, lines, graphics, bx0, bx1, opt.whitespace_gap, page.rect)
    if region is not None:
        return {"region": region, "side": "above", "confidence": "low",
                "note": "no-graphic-element; whitespace fallback"}
    return {"region": None, "side": None, "confidence": "none", "note": "region-not-found"}


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def figure_id(kind: str, prefix: str | None, num: str, caption_text: str) -> str:
    if "graphical abstract" in caption_text.lower():
        return "graphical_abstract"
    low_num = num.lower().lstrip("s")
    if kind == "sfig":
        return f"sfig{low_num}"
    if kind == "extfig":
        return f"extfig{low_num}"
    if kind == "table":
        return f"table{low_num}"
    return f"fig{low_num}"


def clip_region(region: "fitz.Rect", page_rect: "fitz.Rect", margin: float = 2.0) -> "fitz.Rect":
    return fitz.Rect(
        max(page_rect.x0 + margin, region.x0),
        max(page_rect.y0 + margin, region.y0),
        min(page_rect.x1 - margin, region.x1),
        min(page_rect.y1 - margin, region.y1),
    )


def render_region(page: "fitz.Page", region: "fitz.Rect", path: str, dpi: int) -> tuple[int, int]:
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix = page.get_pixmap(matrix=mat, clip=region, alpha=False)
    pix.save(path)
    return pix.width, pix.height


def find_teaser(doc, used_pages: set[int], min_side: float = 140.0) -> dict | None:
    """前两页中「无图注的最大图形」——多为 graphical abstract / teaser 大图。

    这类图通常没有任何图注，无法靠图注锚定，只能按「最大且成块」挑选，
    结果一律标 confidence=low，必须目视确认。
    """
    best = None
    for pno in range(min(2, doc.page_count)):
        if pno in used_pages:
            continue
        page = doc[pno]
        for r in graphic_rects(page):
            if r.width < min_side or r.height < min_side:
                continue
            if r.width * r.height < 0.04 * page.rect.width * page.rect.height:
                continue
            if best is None or r.width * r.height > best[1].width * best[1].height:
                best = (pno, fitz.Rect(r))
    if best is None:
        return None
    pno, r = best
    return {"page": pno, "region": clip_region(r, doc[pno].rect, margin=1.0)}


def parse_pages(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def run_auto(args) -> int:
    doc = fitz.open(args.pdf)
    out_fig = os.path.join(args.out, "figures")
    os.makedirs(out_fig, exist_ok=True)
    dbg_dir = os.path.join(out_fig, "_debug")
    if args.debug:
        os.makedirs(dbg_dir, exist_ok=True)

    pages = range(doc.page_count)
    if args.pages:
        pages = [p - 1 for p in parse_pages(args.pages) if 1 <= p <= doc.page_count]

    bands = running_bands(doc)
    if bands:
        print(f"[figures] 检测到页眉/页脚带 {len(bands)} 处，已排除: "
              + ", ".join(f"y{a:.0f}-{b:.0f}" for a, b in bands))

    figures: list[dict] = []
    warnings: list[str] = []
    seen: dict[str, int] = {}

    for pno in pages:
        page = doc[pno]
        lines = filter_running(text_lines(page), bands)
        graphics = graphic_rects(page)
        caps = find_captions(lines)
        page_regions: list[tuple["fitz.Rect", dict]] = []

        for cap in caps:
            cap_rect = expand_caption_block(cap, lines)
            cap_lines = [ln["rect"] for ln in lines if ln["rect"].intersects(cap_rect)]
            kind = classify(cap["prefix_raw"], cap["kind_raw"])
            fid = figure_id(kind, cap["prefix_raw"], cap["num"], cap["text"])
            loc = locate_figure(page, cap_rect, cap_lines, lines, graphics, args)
            if loc["region"] is None:
                warnings.append(f"p{pno+1} {fid}: 未定位到图形区域（{loc['note']}）")
                continue
            page_regions.append((fitz.Rect(loc["region"]), {
                "id": fid, "kind": kind, "cap": cap, "cap_rect": cap_rect, "loc": loc,
            }))

        # 同页区域去重：相邻图注不应切出互相重叠的块
        page_regions.sort(key=lambda t: t[0].y0)
        for i, (r, meta) in enumerate(page_regions):
            if i + 1 < len(page_regions):
                nxt = page_regions[i + 1][0]
                if r.y1 > nxt.y0 > r.y0:
                    r.y1 = nxt.y0 - 2
            meta["region"] = r

        for r, meta in page_regions:
            region = clip_region(meta["region"], page.rect)
            if region.height < 20 or region.width < 30:
                warnings.append(f"p{pno+1} {meta['id']}: 裁剪区域过小，已跳过")
                continue
            fid = meta["id"]
            if fid in seen:
                warnings.append(f"p{pno+1} {fid}: 重复图注（首次出现在 p{seen[fid]+1}），此处忽略")
                continue
            path = os.path.join(out_fig, f"{fid}.png")
            w, h = render_region(page, region, path, args.dpi)
            seen[fid] = pno
            figures.append({
                "id": fid,
                "label": caption_kind_label(meta["cap"]["prefix_raw"],
                                            meta["cap"]["kind_raw"], meta["cap"]["num"]),
                "kind": meta["kind"],
                "page": pno + 1,
                "bbox": [round(v, 2) for v in (region.x0, region.y0, region.x1, region.y1)],
                "dpi": args.dpi,
                "pixel_size": [w, h],
                "path": os.path.abspath(path),
                "caption": meta["cap"]["text"],
                "caption_above": meta["loc"]["side"] == "below",
                "confidence": meta["loc"]["confidence"],
                "note": meta["loc"]["note"],
            })

        if args.debug:
            for r, meta in page_regions:
                page.draw_rect(r, color=(1, 0, 0), width=1.2)
                page.draw_rect(meta["cap_rect"], color=(0, 0.5, 1), width=0.8)
            pix = page.get_pixmap(matrix=fitz.Matrix(1.4, 1.4), alpha=False)
            pix.save(os.path.join(dbg_dir, f"page{pno+1:03d}_overlay.png"))

    # 可选的 graphical abstract / teaser（无图注，纯启发式，必须目视确认）
    if args.teaser:
        used = {f["page"] - 1 for f in figures if f["page"] <= 2}
        teaser = find_teaser(doc, used)
        if teaser is None:
            warnings.append("teaser: 前两页未找到符合条件的无图注大图")
        else:
            page = doc[teaser["page"]]
            path = os.path.join(out_fig, "graphical_abstract.png")
            w, h = render_region(page, teaser["region"], path, args.dpi)
            figures.append({
                "id": "graphical_abstract",
                "label": "Graphical Abstract / Teaser",
                "kind": "teaser",
                "page": teaser["page"] + 1,
                "bbox": [round(v, 2) for v in tuple(teaser["region"])],
                "dpi": args.dpi,
                "pixel_size": [w, h],
                "path": os.path.abspath(path),
                "caption": "(无图注，启发式挑选的前两页最大图形，必须目视确认)",
                "caption_above": False,
                "confidence": "low",
                "note": "uncaptioned-largest-graphic",
            })

    def sort_key(f):
        m = re.search(r"(\d+)", f["id"])
        return (f["kind"] != "fig", f["kind"], int(m.group(1)) if m else 0, f["page"])

    figures.sort(key=sort_key)

    manifest = {
        "pdf": os.path.abspath(args.pdf),
        "page_count": doc.page_count,
        "dpi": args.dpi,
        "figure_count": len(figures),
        "figures": figures,
        "warnings": warnings,
    }
    with open(os.path.join(out_fig, "figures.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"[figures] 共抽取 {len(figures)} 张：")
    for f in figures:
        print(f"  {f['id']:<20} p{f['page']:<3} {f['pixel_size'][0]}x{f['pixel_size'][1]:<6} "
              f"[{f['confidence']}]  {f['caption'][:58]}")
    if warnings:
        print(f"[figures] ⚠ {len(warnings)} 条警告：")
        for w in warnings[:30]:
            print("  -", w)
    print(f"[figures] 清单: {os.path.join(out_fig, 'figures.json')}")
    print("[figures] 请务必逐张目视复核（read_image），必要时用 crop 子命令重裁。")
    return 0


def run_list_captions(args) -> int:
    doc = fitz.open(args.pdf)
    bands = running_bands(doc)
    total = 0
    for pno in range(doc.page_count):
        page = doc[pno]
        for cap in find_captions(filter_running(text_lines(page), bands)):
            total += 1
            print(f"p{pno+1:<3} {classify(cap['prefix_raw'], cap['kind_raw']):<6} "
                  f"num={cap['num']:<4} y={cap['rect'].y0:7.1f} | {cap['text'][:100]}")
    print(f"[figures] 识别到 {total} 条图注（共 {doc.page_count} 页）")
    return 0


def run_render_pages(args) -> int:
    doc = fitz.open(args.pdf)
    out = os.path.join(args.out, "pages")
    os.makedirs(out, exist_ok=True)
    pages = parse_pages(args.pages) if args.pages else list(range(1, doc.page_count + 1))
    mat = fitz.Matrix(args.dpi / 72.0, args.dpi / 72.0)
    for p in pages:
        if not (1 <= p <= doc.page_count):
            continue
        pix = doc[p - 1].get_pixmap(matrix=mat, alpha=False)
        path = os.path.join(out, f"page{p:03d}.png")
        pix.save(path)
        print(f"  p{p:<3} {pix.width}x{pix.height}  {os.path.abspath(path)}")
    print(f"[figures] 整页图输出目录: {os.path.abspath(out)}  (dpi={args.dpi})")
    print("[figures] 提示: crop 时 --dpi 必须与此一致，--px 用该 PNG 的像素坐标。")
    return 0


def run_crop(args) -> int:
    doc = fitz.open(args.pdf)
    if not (1 <= args.page <= doc.page_count):
        print(f"[figures] 页码越界: {args.page}", file=sys.stderr)
        return 1
    page = doc[args.page - 1]
    if args.px:
        x0, y0, x1, y1 = [float(v) for v in args.px.split(",")]
        s = 72.0 / args.dpi
        rect = fitz.Rect(x0 * s, y0 * s, x1 * s, y1 * s)
    elif args.bbox:
        x0, y0, x1, y1 = [float(v) for v in args.bbox.split(",")]
        rect = fitz.Rect(x0, y0, x1, y1)
    else:
        print("[figures] 需要 --bbox（PDF 点）或 --px（像素，配合 --dpi）", file=sys.stderr)
        return 1
    rect = clip_region(rect, page.rect, margin=0.0)
    out_fig = os.path.join(args.out, "figures")
    os.makedirs(out_fig, exist_ok=True)
    name = args.name or f"crop_p{args.page}"
    path = os.path.join(out_fig, f"{name}.png")
    w, h = render_region(page, rect, path, args.dpi)
    print(f"[figures] 已输出 {os.path.abspath(path)}  {w}x{h}  bbox={list(rect)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="PDF 自动抽图（Figure / 附图 / 表格 → PNG）")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--pdf", required=True)
        p.add_argument("--out", default="./figure_out")

    p_auto = sub.add_parser("auto", help="自动定位图注并抽图")
    common(p_auto)
    p_auto.add_argument("--dpi", type=int, default=300)
    p_auto.add_argument("--pages", help="限定页范围，如 3,5-9")
    p_auto.add_argument("--debug", action="store_true", help="输出每页框选 overlay 供目视复核")
    p_auto.add_argument("--chain-gap", type=float, default=30.0,
                        help="图形元素链式聚合的 y 间隙(pt)")
    p_auto.add_argument("--adjacent", type=float, default=60.0,
                        help="图形核心与图注的最大贴近距离(pt)")
    p_auto.add_argument("--bridge-gap", type=float, default=46.0,
                        help="跨面板行的桥接间隙(pt)：仅当间隙内仍是图形才继续并区")
    p_auto.add_argument("--absorb", type=float, default=20.0,
                        help="吸收图内轴标题/面板字母的间隙(pt)")
    p_auto.add_argument("--max-fig-height", type=float, default=0.92,
                        help="单图高度上限（页高占比），防止吞掉整页正文")
    p_auto.add_argument("--teaser", action="store_true",
                        help="额外尝试抓取前两页无图注的图形摘要/teaser 大图")
    p_auto.add_argument("--whitespace-gap", type=float, default=11.0,
                        help="退化方案的空白带阈值(pt)")

    p_list = sub.add_parser("list-captions", help="只列出图注（dry-run）")
    common(p_list)

    p_rp = sub.add_parser("render-pages", help="整页渲染 PNG")
    common(p_rp)
    p_rp.add_argument("--dpi", type=int, default=180)
    p_rp.add_argument("--pages", help="如 3,5-9；缺省全部")

    p_crop = sub.add_parser("crop", help="按 bbox 手工裁剪")
    common(p_crop)
    p_crop.add_argument("--page", type=int, required=True)
    p_crop.add_argument("--bbox", help="PDF 坐标 x0,y0,x1,y1")
    p_crop.add_argument("--px", help="像素坐标 x0,y0,x1,y1（配合 --dpi）")
    p_crop.add_argument("--dpi", type=int, default=300)
    p_crop.add_argument("--name", help="输出文件名（不含扩展名）")

    argv = sys.argv[1:]
    if not argv or argv[0] not in ("auto", "list-captions", "render-pages", "crop"):
        argv = ["auto"] + argv
    args = ap.parse_args(argv)

    if not os.path.exists(args.pdf):
        print(f"[figures] PDF 不存在: {args.pdf}", file=sys.stderr)
        return 1
    if getattr(args, "dpi", 300) < 72:
        print("[figures] dpi 建议 ≥ 150（正文图 300）", file=sys.stderr)

    return {
        "auto": run_auto,
        "list-captions": run_list_captions,
        "render-pages": run_render_pages,
        "crop": run_crop,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
