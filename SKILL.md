---
name: lit-report
description: "从 PDF 科学文献生成深度中文批判性文献分析报告。当用户要求阅读文献、分析 PDF 论文、生成文献报告、文献分享、论文笔记时触发此技能。即使用户只是说'帮我看看这篇论文'、'总结一下这个 PDF'、'写个文献笔记'、'分析一下这篇文章'、'帮我读一下这篇文献'，也应使用此技能。与简单总结不同，本技能生成的是论点驱动的批判性分析报告：先识别文章的核心创新点和声称，再按逻辑论证结构（而非 Figure 编号顺序）组织报告，逐一评估每条证据的可靠性，并与已有文献交叉验证。PDF 解析使用多后端自动回退（MinerU 优先，次级 Marker / Docling / PyMuPDF4LLM），Figure 截图由脚本自动定位抽取 + 交互式复核完成，无需用户手工提供截图；**强制使用 PubMed MCP** 检索论文元数据和相关文献用于交叉对比。涉及 PDF 论文分析、文献总结、论文阅读笔记、文献批判性分析、文献分享准备等场景时，都应触发。"
---

# 文献批判性分析报告

## 概述

将 PDF 科学文献转化为**论点驱动的中文批判性分析报告**。报告不是原文的翻译或 Figure 的逐一描述，而是回答以下问题：

- 这篇文章到底声称了什么？
- 证据链是否完整、可靠？有没有逻辑跳跃或方法缺陷？
- 和其他已知文献是否矛盾或相互印证？
- 这个创新点的实际价值有多大？

**写作原则**：按论证逻辑组织，而非按 Figure 编号顺序。Figure 只是证据的载体——在需要引用某 Figure 的数据来支持分析时插入该图，而不是机械地按图号排列。这确保了图片上下文天然正确，不会出现附图文字联系到正图的错误。

**产出物**：
- **最终报告**：`<原 PDF 同目录>/<PDF 名>_文献报告.md`（与输入 PDF 在同一目录，方便查找）
- **中间产物**：`./data/<任务名>/_intermediate/`（Markdown、figures、cache）

**图片来源**：**不需要用户提供截图**。`scripts/extract_figures.py` 自动从 PDF 文本层识别图注并裁剪出每张 Figure，AI 通过逐张 `read_image` 复核；复核不通过的用 `crop` 子命令按像素坐标重裁。

---

## 工具依赖矩阵（分享前必读）

| 依赖 | 类别 | 用途 | 缺失后果 |
|------|------|------|---------|
| **PyMuPDF**（`pymupdf`） | **必装** | 自动抽图（读文本层、定位图注、渲染裁剪）、整页渲染、手工重裁 | **无法自动抽图**，报告将没有配图 |
| **pubmed MCP**（`pubmed_*` 工具） | **必装** | STEP 3 背景调研：DOI/PMID 检索、MeSH 术语、相关文献交叉验证、标准引用格式 | **立即暂停工作流**，引导用户安装 MCP，不得用其他工具替代 |
| **MinerU**（`mineru`） | **默认推荐** | PDF→Markdown，版面+公式+OCR+图片抽取，质量最高 | 降级到 Marker / Docling / PyMuPDF4LLM |
| PyMuPDF4LLM / Docling | 推荐 | 轻量 PDF→Markdown 后备方案 | MinerU 失败时需要至少一个 |

**首次使用先体检**（默认只检查、不安装任何东西）：

```bash
python scripts/setup_env.py --minimal   # 只检查必需项（pymupdf + pymupdf4llm + PubMed MCP）
python scripts/setup_env.py             # 完整检查（含所有可选后端）
```

**安装依赖前必须先取得用户同意**，再执行：

```bash
python scripts/setup_env.py --install                         # 默认安装 pymupdf + pymupdf4llm + mineru
python scripts/setup_env.py --install --no-recommended        # 不安装 MinerU（体积大）
python scripts/setup_env.py --install --with docling,marker   # 额外安装其他后端
```

**pubmed MCP 安装示例**（写入 agent 的 MCP 配置后重启会话）：

```jsonc
// Claude Desktop: claude_desktop_config.json
// Claude Code / DSH: 各自配置文件的 mcpServers / mcp 字段
{
  "mcpServers": {
    "pubmed": {
      "command": "npx",
      "args": ["-y", "@cyanheads/pubmed-mcp-server"]
    }
  }
}
```

**PubMed MCP 检查（STEP 0 的一部分）**：

```python
# Agent 在 STEP 0 执行
try:
    list_mcp_resources(server="pubmed")
    # 成功则继续
except:
    # 失败则暂停，告知用户安装步骤，等待确认后再继续
    暂停工作流
```

pubmed MCP 未安装时，**立即暂停工作流并告知用户安装步骤，用户确认后再继续**；绝不可用 `web_search` / `puppeteer` 等其他工具替代其背景调研职责。

---

## 技能目录结构（可整体打包分享）

```
lit-report/
├── SKILL.md                 本文件（agent 读取的完整工作流）
├── README.md                分享接收者的安装说明
├── LICENSE
└── scripts/
    ├── setup_env.py         环境自检（默认零副作用，--install 才安装）
    ├── pdf2md.py            PDF→Markdown 多后端自动回退转换器
    ├── extract_figures.py   PDF 自动抽图（图注定位 + 裁剪 + 复核/重裁）
    └── qa_report.py         报告质量检查（自动检测常见问题）
```

脚本全部以命令行参数输入输出、**无硬编码路径、无个人环境名**，任何 agent 均可直接调用。

工作目录约定：中间产物放 `./data/<任务名>/_intermediate/`，最终报告放在**与输入 PDF 同目录**。

---

## 执行流程

按 STEP 0 至 STEP 5 顺序执行，**用户一次性提供所有输入时直接开始**。仅在关键错误时暂停。

### STEP 0：环境自检（新机器/首次运行）

```bash
python scripts/setup_env.py --minimal
```

检查项：
1. **PyMuPDF + pymupdf4llm**：必需，缺失时告知用户安装
2. **PubMed MCP**：调用 `list_mcp_resources(server="pubmed")` 检查，失败时**立即暂停**并提供安装指引
3. **MinerU**（可选）：推荐但不强制

若关键依赖缺失，**先告知用户需要安装哪些、多大体积，取得同意后再跑 `--install`**。

---

### STEP 1：PDF 转 Markdown（MinerU 优先，多后端自动回退）

```bash
python scripts/pdf2md.py --list-backends                  # 查看本机可用后端
python scripts/pdf2md.py --pdf <PDF 路径> --out ./data/<任务名>/_intermediate
```

- 输出：`./data/<任务名>/_intermediate/<pdf 文件名>/<pdf 文件名>.md`，并附 `_convert_meta.json`
- 默认 `--backend auto`：按质量分降序 `mineru → marker → docling → pymupdf4llm → markitdown` 逐个尝试
- 脚本会先探测文本层：前 3 页平均字符数低于阈值即判定为**扫描件**，自动只走 OCR 后端

| 后端 | 定位 | 何时选它 |
|------|------|---------|
| **MinerU** | 版面+公式+OCR+图片，质量最高，约 6GB，建议 GPU | **默认首选** |
| Marker | Surya 版面+OCR，质量接近 MinerU | MinerU 失败且有 GPU |
| Docling | IBM，纯 CPU 版面/表格识别好 | 无 GPU 但需要版面结构 |
| PyMuPDF4LLM | 纯 CPU、秒级、轻量 | 已排版论文的文本层足够 |
| MarkItDown | 最轻量纯文本 | 兜底 |

**Markdown 太短或为空**（< 200 字节）时，视为该后端失败：换后端重试。

---

### STEP 2：自动抽图 + AI 目视复核（替代手工截图）

```bash
python scripts/extract_figures.py auto --pdf <PDF 路径> --out ./data/<任务名>/_intermediate --debug --teaser
```

- 输出：`./data/<任务名>/_intermediate/figures/fig1.png`、`sfig1.png`、清单 `figures.json`
- `--debug` 输出 `figures/_debug/pageNNN_overlay.png`（红框=抽图区域，蓝框=图注区域）
- `--teaser` 额外尝试抓取前两页**无图注**的图形摘要

#### 2.1 强制目视复核（不可跳过）

**必须对 `figures.json` 里每一张图调用 `read_image` 逐张检查**，确认：
- 图是否完整（上下左右有没有被切掉）
- 是否包含全部子面板（A/B/C…）
- 分辨率是否可读

#### 2.2 复核不通过时的手工重裁（兜底）

```bash
# 整页渲染，供目视定位
python scripts/extract_figures.py render-pages --pdf <PDF> --out ./data/<任务名>/_intermediate --pages 3,5 --dpi 200

# 用像素坐标重裁
python scripts/extract_figures.py crop --pdf <PDF> --out ./data/<任务名>/_intermediate \
    --page 3 --px 60,120,540,1520 --dpi 200 --name fig1
```

重裁后**必须再次 `read_image` 确认**，通过后覆盖 `figures.json` 中对应条目。

---

### STEP 3：背景调研与报告生成

#### 阶段 A：背景调研（强制使用 PubMed MCP）

1. **提取元数据**：从 STEP 1 的 Markdown 中提取论文标题、作者、期刊、年份、DOI。
2. **PubMed 检索**（**不得用其他工具替代**）：
   - 优先用 DOI 搜索；如无 DOI，用标题关键词
   - 获取 PMID 后用 `pubmed_fetch_articles` 获取元数据和 MeSH 术语
   - 用 `pubmed_find_related` 查找 3-5 篇相关文献
   - 用 `pubmed_format_citations` 获取标准引用格式
3. **PubMed 无结果时**：报告中 MeSH 标注为"不适用"，PMID 标注为"N/A"。**不使用其他工具补充检索**。

#### 阶段 B：理解论文——核心分析

**在写报告之前，先完成以下分析步骤。这是报告质量的根基，不可跳过。**

**B.1 识别核心创新点**

通读 Markdown（重点关注 Abstract、Introduction 结尾段、Discussion 开头段），回答：
- 这篇文章的核心声称是什么？
- 这个声称属于什么类型？（新发现/新技术/新解释/新应用）

**B.2 拆解证据链**

逐一列出作者用来支持核心声称的主要证据。对每条证据：
- 作者用这个实验/数据想证明什么？
- 实验设计是否合理？
- 数据是否支持作者的结论？
- 有没有其他解释也能说明同样的数据？
- 这条证据本身是否存在方法学上的弱点？

**B.3 文献交叉验证**

利用背景调研获取的相关文献，检查：
- 本文的核心发现与已有文献是否一致？
- 如果矛盾，哪一方的证据更充分？
- 本文填补了什么真正的空白？

**B.4 评估创新程度**

- 如果是新发现：这个发现改变了什么？
- 如果是新技术：相比已有方法到底好在哪里？
- 如果是新解释：新解释比旧解释好在哪里？

**B.5 缓存分析结果（新增）**

将阶段 B 的分析结果保存为 `./data/<任务名>/_intermediate/_analysis_cache.json`：

```json
{
  "innovation": "核心创新点描述",
  "innovation_type": "新发现/新技术/新解释/新应用",
  "evidence_chain": [
    {
      "id": "E1",
      "claim": "论点描述",
      "figure": "Figure 1",
      "reliability": "high/medium/low"
    }
  ],
  "literature_cross_check": {...},
  "pdf_md5": "abc123...",
  "timestamp": "2025-01-13T10:30:00"
}
```

后续生成报告时，如果 cache 存在且 PDF 未修改（MD5 校验），复用缓存。

---

### STEP 4：撰写报告（两阶段插图流程）

#### 4.1 图片引用规范

统一格式——图片行 + 图注行：

```markdown
![Figure 1](<截图绝对路径>)

*图 1: 图注描述。*
```

关键规则：
- 图片路径必须使用**绝对路径**
- 图注必须放在图片下方**独立段落**，使用斜体格式
- 同一张截图只在首次引用时插入
- 多子面板 Figure 只有整图截图时，在讨论第一个子面板处插入整图

#### 4.2 两阶段插图流程（新增）

**第一阶段**：生成报告主体，图片用占位符：

```markdown
<!-- TODO_FIG: Figure 1, 用于论证 XXX -->
```

**第二阶段**：
1. Agent 重新审视每个占位符的上下文（前后 2 段）
2. 确认该位置正在讨论该图数据（包含具体数值/子面板引用）
3. 确认后替换为图片 Markdown
4. 如果上下文不匹配，移动占位符到正确位置或改为文字引用

#### 4.3 学术版报告结构

```markdown
# 文献分享 [编号]：[期刊名] | [核心发现一句话]

## 一、文献资料
- **文献标题**：（原文）
- **发表期刊**：期刊全称
- **DOI**：
- **PMID**：（如适用，否则 N/A）
- **MeSH 术语**：3-5 个

## 二、核心创新点
用 2-3 个连贯段落。

## 三、证据链分析
按论证逻辑顺序，每条证据形成子章节。

### 3.x [证据对应的论点]
**证据概述**：实验设计和分组。
**数据分析**：关键数据引用具体数值。
**可靠性评估**：证据是否充分？

## 四、技术创新深度分析（如适用）

## 五、与已有文献的关系
2-3 个连贯段落。

## 六、综合评估与结论
2-3 个连贯段落。

## 七、参考文献
[1] 作者列表。论文标题。期刊名，年份，卷(期): 页码。DOI
```

**第二阶段处理占位符**：逐个检查占位符上下文，确认后替换为实际图片 Markdown。

**保存位置**：`<原 PDF 同目录>/<PDF 名>_文献报告.md`

---

### STEP 5：质量检查与交付

#### 5.1 自动质量检查（新增）

```bash
python scripts/qa_report.py --report <报告 MD 路径>
```

检查项：
- 图片路径存在且可访问
- 无"图 X"文字引用但缺失截图
- 数据描述包含具体数值
- 批判性评估关键词存在
- 图片上下文匹配

输出：`<报告名>_qa_report.txt`

#### 5.2 向用户报告

**向用户报告以下内容**：
- 最终报告文件路径（与原 PDF 同目录）
- 中间产物目录
- 识别出的核心创新点
- PDF 转换使用的后端
- 自动抽图结果：张数、置信度
- 证据链中发现的关键薄弱环节
- PubMed 获取的关键信息
- QA 检查结果摘要

---

## 撰写核心原则

- **论点驱动**：每一节、每一段都围绕核心创新点和支撑证据展开
- **批判性思维**：评估原文的论证质量，不是翻译
- **具体数值**：所有数据描述引用具体数值，禁用模糊词汇
- **段落式写作**：连贯学术段落，不滥用列表
- **图片上下文正确**：图片只在其数据被具体分析讨论时插入
- **忠实但不盲从**：基于原文但对论证薄弱环节必须指出
