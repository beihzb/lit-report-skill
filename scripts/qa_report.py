#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qa_report.py —— 文献报告质量检查脚本

自动检查生成的文献报告是否符合质量标准：
- 图片路径存在且可访问
- 无"图 X"文字引用但缺失截图
- 数据描述包含具体数值
- 批判性评估关键词存在
- 图片上下文匹配（图片前后段落是否讨论该图数据）

用法
----
  python qa_report.py --report <报告 MD 路径>
  python qa_report.py --report <报告 MD 路径> --strict  # 严格模式

输出
----
  <报告名>_qa_report.txt（通过项 / 问题清单）
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Windows 控制台强制 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
    except Exception:
        pass

# --------------------------------------------------------------------------- #
# 检查项
# --------------------------------------------------------------------------- #
def check_image_paths(content: str, report_dir: Path) -> list[dict]:
    """检查所有图片路径是否存在。"""
    issues = []
    # 匹配 ![...](<path>) 或 ![...](path)
    pattern = r'!\[([^\]]*)\]\(<?([^>)\s]+)>?\)'
    for match in re.finditer(pattern, content):
        alt, path = match.groups()
        path = path.strip()
        # 尝试绝对路径
        if os.path.exists(path):
            continue
        # 尝试相对路径
        rel_path = report_dir / path
        if rel_path.exists():
            continue
        issues.append({
            "type": "missing_image",
            "severity": "error",
            "alt": alt,
            "path": path,
            "msg": f"图片不存在: {alt} → {path}"
        })
    return issues


def check_text_references(content: str) -> list[dict]:
    """检查文字引用（如"图 1""图 2A"）是否有对应的图片插入。"""
    issues = []
    # 提取所有已插入的图片 alt 文本
    inserted = set()
    for match in re.finditer(r'!\[([^\]]*)\]', content):
        alt = match.group(1).lower()
        inserted.add(alt)
    
    # 检查文字引用
    # 匹配：图 1、图1、Figure 1、附图 1、图 1A 等
    ref_pattern = r'(?:图|附图|表|Figure|Fig\.|Supplementary Figure)\s*\d+[A-Z]?'
    for match in re.finditer(ref_pattern, content):
        ref = match.group(0)
        # 检查是否在 ![...] 内部（已插入）
        start = match.start()
        # 向前找最近的 ![ 和 ]
        before = content[:start]
        last_img_start = before.rfind('![')
        last_bracket = before.rfind(']')
        if last_img_start > last_bracket:
            continue  # 在图片 alt 内，跳过
        
        # 检查是否有对应插图
        ref_lower = ref.lower()
        found = False
        for alt in inserted:
            if ref_lower in alt or any(part in alt for part in ref_lower.split()):
                found = True
                break
        
        if not found:
            issues.append({
                "type": "missing_figure",
                "severity": "warning",
                "ref": ref,
                "msg": f"文字引用'{ref}'可能缺少对应截图（或截图未按约定命名）"
            })
    return issues


def check_data_values(content: str) -> list[dict]:
    """检查数据描述是否包含具体数值，标记模糊表述。"""
    issues = []
    # 模糊词汇模式（不含数值时报警）
    vague_patterns = [
        (r'显著[提高|增加|降低|减少|上调|下调](?![^。]{0,30}\d)', '显著XXX'),
        (r'明显[提高|增加|降低|减少|改善|恶化](?![^。]{0,30}\d)', '明显XXX'),
        (r'大幅[提高|增加|降低|减少](?![^。]{0,30}\d)', '大幅XXX'),
        (r'急剧[上升|下降|增加|减少](?![^。]{0,30}\d)', '急剧XXX'),
    ]
    
    for pattern, desc in vague_patterns:
        for match in re.finditer(pattern, content):
            context = content[max(0, match.start()-50):match.end()+50]
            issues.append({
                "type": "vague_data",
                "severity": "warning",
                "phrase": match.group(0),
                "context": context,
                "msg": f"使用了'{desc}'但附近未见具体数值（50字内）"
            })
    return issues


def check_critical_keywords(content: str) -> list[dict]:
    """检查批判性评估关键词是否存在。"""
    issues = []
    required_keywords = [
        ("可靠性", ["可靠性", "证据强度", "置信度", "可信度"]),
        ("替代性解释", ["替代", "其他解释", "另一种可能", "也可能"]),
        ("文献交叉验证", ["已有文献", "先前研究", "与.*文献", "相关研究"]),
    ]
    
    for category, keywords in required_keywords:
        found = any(re.search(kw, content) for kw in keywords)
        if not found:
            issues.append({
                "type": "missing_critical_section",
                "severity": "warning",
                "category": category,
                "msg": f"未检测到'{category}'相关表述（关键词: {', '.join(keywords[:2])}...）"
            })
    return issues


def check_image_context(content: str) -> list[dict]:
    """检查图片上下文：图片前后2段是否讨论该图数据。"""
    issues = []
    # 分段
    paragraphs = [p.strip() for p in content.split('\n\n') if p.strip()]
    
    # 找到所有图片位置
    for i, para in enumerate(paragraphs):
        if not para.startswith('!['):
            continue
        
        # 提取图片 alt
        match = re.match(r'!\[([^\]]*)\]', para)
        if not match:
            continue
        alt = match.group(1)
        
        # 提取图号（如 "Figure 1" → "1"）
        fig_num = re.search(r'\d+', alt)
        if not fig_num:
            continue
        num = fig_num.group(0)
        
        # 检查前后2段
        context_start = max(0, i - 2)
        context_end = min(len(paragraphs), i + 3)
        context = '\n'.join(paragraphs[context_start:context_end])
        
        # 是否提到该图号
        if not re.search(rf'(?:图|Figure|Fig\.)\s*{num}', context):
            issues.append({
                "type": "image_context_mismatch",
                "severity": "warning",
                "alt": alt,
                "msg": f"图片'{alt}'前后2段未明确讨论该图（未提及'图{num}'等）"
            })
    return issues


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="lit-report 报告质量检查")
    ap.add_argument("--report", required=True, help="报告 Markdown 文件路径")
    ap.add_argument("--strict", action="store_true", help="严格模式：warning 也视为失败")
    args = ap.parse_args()
    
    report_path = Path(args.report)
    if not report_path.exists():
        print(f"[QA] 报告文件不存在: {report_path}", file=sys.stderr)
        return 1
    
    content = report_path.read_text(encoding="utf-8")
    report_dir = report_path.parent
    
    print("=" * 70)
    print(f"文献报告质量检查: {report_path.name}")
    print("=" * 70)
    
    all_issues = []
    
    # 检查项 1: 图片路径
    print("\n[1/5] 检查图片路径...")
    issues = check_image_paths(content, report_dir)
    all_issues.extend(issues)
    if issues:
        print(f"  ⚠ 发现 {len(issues)} 个问题")
        for issue in issues[:5]:
            print(f"    - {issue['msg']}")
        if len(issues) > 5:
            print(f"    ... 还有 {len(issues)-5} 个")
    else:
        print("  ✅ 通过")
    
    # 检查项 2: 文字引用
    print("\n[2/5] 检查文字引用与截图匹配...")
    issues = check_text_references(content)
    all_issues.extend(issues)
    if issues:
        print(f"  ⚠ 发现 {len(issues)} 个潜在问题")
        for issue in issues[:3]:
            print(f"    - {issue['msg']}")
        if len(issues) > 3:
            print(f"    ... 还有 {len(issues)-3} 个")
    else:
        print("  ✅ 通过")
    
    # 检查项 3: 数据数值
    print("\n[3/5] 检查数据描述具体性...")
    issues = check_data_values(content)
    all_issues.extend(issues)
    if issues:
        print(f"  ⚠ 发现 {len(issues)} 处模糊表述")
        for issue in issues[:3]:
            print(f"    - {issue['phrase']}")
    else:
        print("  ✅ 通过")
    
    # 检查项 4: 批判性关键词
    print("\n[4/5] 检查批判性评估关键词...")
    issues = check_critical_keywords(content)
    all_issues.extend(issues)
    if issues:
        print(f"  ⚠ 缺失 {len(issues)} 个关键类别")
        for issue in issues:
            print(f"    - {issue['msg']}")
    else:
        print("  ✅ 通过")
    
    # 检查项 5: 图片上下文
    print("\n[5/5] 检查图片上下文匹配...")
    issues = check_image_context(content)
    all_issues.extend(issues)
    if issues:
        print(f"  ⚠ 发现 {len(issues)} 处上下文不匹配")
        for issue in issues[:3]:
            print(f"    - {issue['msg']}")
    else:
        print("  ✅ 通过")
    
    # 汇总
    print("\n" + "=" * 70)
    errors = [i for i in all_issues if i['severity'] == 'error']
    warnings = [i for i in all_issues if i['severity'] == 'warning']
    
    print(f"检查完成: {len(errors)} 个错误, {len(warnings)} 个警告")
    
    # 写入报告
    qa_report = report_path.with_name(report_path.stem + "_qa_report.txt")
    with open(qa_report, "w", encoding="utf-8") as f:
        f.write(f"文献报告质量检查报告\n")
        f.write(f"报告文件: {report_path}\n")
        f.write(f"检查时间: {Path(__file__).stat().st_mtime}\n")
        f.write(f"\n{'='*70}\n")
        f.write(f"错误: {len(errors)}, 警告: {len(warnings)}\n")
        f.write(f"{'='*70}\n\n")
        
        if errors:
            f.write("## 错误 (Error)\n\n")
            for i, issue in enumerate(errors, 1):
                f.write(f"{i}. [{issue['type']}] {issue['msg']}\n")
        
        if warnings:
            f.write("\n## 警告 (Warning)\n\n")
            for i, issue in enumerate(warnings, 1):
                f.write(f"{i}. [{issue['type']}] {issue['msg']}\n")
        
        if not all_issues:
            f.write("所有检查项通过！\n")
    
    print(f"详细报告已保存: {qa_report}")
    print("=" * 70)
    
    if errors or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
