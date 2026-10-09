# lit-report — 科学文献批判性分析报告生成技能

把一个 PDF 论文变成**论点驱动的中文批判性分析报告**，而不是流水账式的翻译或 Figure 逐条描述。

输出的报告会回答：这篇文章到底声称了什么？证据链是否完整可靠？和已有文献是否矛盾？这个创新点的实际价值有多大？

## 功能特性

- **PDF 解析多后端自动回退**：MinerU（默认优先）/ Marker / Docling / PyMuPDF4LLM / MarkItDown，按质量降序逐个尝试，缺哪个跳哪个，失败自动换下一个；扫描件自动判定并只走 OCR 后端
- **Figure 自动抽取**：脚本从 PDF 文本层识别图注、定位图形区域、300 DPI 裁剪为 PNG，并输出置信度与框选 overlay；AI 逐张目视复核，不合格的按像素坐标重裁
- **两阶段插图流程**：先生成报告主体（图片用占位符），再检查上下文确认后插入，避免"附图文字联系到正图"错误
- **增量更新**：缓存分析结果（核心创新点、证据链、文献交叉验证），PDF 未变时复用缓存，加速迭代
- **文献交叉验证**：**强制使用 PubMed MCP** 检索 DOI/PMID/MeSH、相关文献与标准引用格式（不可用其他工具替代）
- **质量检查**：自动检测图片路径、文字引用、数据数值、批判性关键词、图片上下文等常见问题
- **报告位置优化**：最终报告与原 PDF 在同一目录，方便查找
- 完全自包含：打包整个文件夹分享即可，任何用户、任何 agent 均可使用

## 依赖一览

| 依赖 | 类别 | 说明 |
|------|------|------|
| **PyMuPDF** | **必装** | 自动抽图、整页渲染、手工重裁 |
| **pubmed MCP**（`pubmed_*` 工具） | **必装** | 背景调研：DOI/PMID/MeSH/相关文献/引用格式。未安装时工作流会暂停引导安装，**不得用其他工具替代** |
| **MinerU** | **默认推荐** | 最高质量 PDF 转换（95 分），版面+公式+OCR+图片，~6GB，建议 GPU |
| PyMuPDF4LLM / Docling | 推荐 | 轻量 PDF→Markdown 后备方案（纯 CPU 可用） |

## 安装

### 1. 放置技能目录

把 `lit-report/` 整个文件夹放进你的 agent 技能目录，例如：
- Claude Code / DSH：`~/.agents/skills/`
- Claude Desktop：`~/.claude/skills/`
- 其他 agent：其约定的 skills 目录

### 2. 安装 PubMed MCP（必需）

在 agent 的 MCP 配置中加入 pubmed server，然后重启会话。

**Claude Desktop**（`claude_desktop_config.json`）：
```jsonc
{
  "mcpServers": {
    "pubmed": {
      "command": "npx",
      "args": ["-y", "@cyanheads/pubmed-mcp-server"]
    }
  }
}
```

**DSH / Claude Code**：在各自配置文件的 `mcpServers` 或 `mcp` 字段添加相同配置。

### 3. 环境体检（默认只检查，不安装任何东西）

```bash
cd lit-report

# 最小检查（只检查核心依赖）
python scripts/setup_env.py --minimal

# 完整检查（含所有可选后端）
python scripts/setup_env.py
```

### 4. 按需安装

脚本会明确告知要装什么、体积多大，取得你的同意后再安装：

```bash
# 方案一：默认安装（含 MinerU，推荐）
python scripts/setup_env.py --install

# 方案二：最小安装（不含 MinerU）
python scripts/setup_env.py --install --no-recommended

# 方案三：自定义安装（额外后端）
python scripts/setup_env.py --install --with docling,marker
```

**使用现有 conda 环境**：
```bash
# 在指定环境中安装
conda activate your_env
python scripts/setup_env.py --install
```

## 使用

对任意 AI 助手说："帮我分析这篇文献 `<PDF 路径>`"。

底层脚本也可手动直接调用：

```bash
# 1) 看看本机有哪些 PDF 后端
python scripts/pdf2md.py --list-backends

# 2) PDF → Markdown（默认 MinerU 优先，自动回退）
python scripts/pdf2md.py --pdf paper.pdf --out ./data/task01/_intermediate

# 3) 自动抽图（--debug 输出框选 overlay 供核对）
python scripts/extract_figures.py auto --pdf paper.pdf \
    --out ./data/task01/_intermediate --debug --teaser

# 3b) 复核不通过时：整页渲染 + 按像素坐标重裁
python scripts/extract_figures.py render-pages --pdf paper.pdf \
    --out ./data/task01/_intermediate --pages 3 --dpi 200
python scripts/extract_figures.py crop --pdf paper.pdf \
    --out ./data/task01/_intermediate --page 3 --px 60,120,540,1520 --dpi 200 --name fig1

# 4) 质量检查（生成报告后）
python scripts/qa_report.py --report <报告路径>
```

## 输出结构

```
<原 PDF 所在目录>/
├── paper.pdf                        输入 PDF
└── paper_文献报告.md                 最终报告（与 PDF 同目录，方便查找）

./data/<任务名>/_intermediate/       中间产物目录
├── paper/
│   ├── paper.md                     PDF 转换的 Markdown
│   └── _convert_meta.json           转换元数据（后端、耗时）
├── figures/
│   ├── fig1.png                     抽取的图片
│   ├── sfig1.png                    补充图
│   ├── figures.json                 图片清单（id、路径、置信度）
│   └── _debug/                      调试用 overlay
└── _analysis_cache.json             分析结果缓存
```

## 目录结构

```
lit-report/
├── SKILL.md                 技能主文档（agent 读取的完整工作流）
├── README.md                本文件
├── CHANGELOG.md             改进日志
├── LICENSE
└── scripts/
    ├── setup_env.py         环境自检与可选安装（默认零副作用）
    ├── pdf2md.py            PDF→Markdown 多后端自动回退
    ├── extract_figures.py   自动抽图（auto / list-captions / render-pages / crop）
    └── qa_report.py         报告质量检查
```

## 扩展新的 PDF 识别器

在 `scripts/pdf2md.py` 中：

1. 在 `BACKENDS` 注册表加一条（质量分、探测模块名、是否具备 OCR、说明）；
2. 写一个 `run_xxx(pdf, target_dir, md_path) -> md_path` 函数，并登记进 `RUNNERS`。

主流程零改动，`auto` 模式会自动把它纳入回退链。

## 常见问题

**环境相关**
- **没有 conda？** `setup_env.py --install` 会自动改用 `python -m venv`。
- **需要换 conda 环境名？** `python scripts/setup_env.py --install --env-name 你的环境名`。

**依赖相关**
- **PubMed MCP 没装？** 工作流会在背景调研阶段暂停并提示安装，不可用其他工具替代。
- **MinerU 太大不想装？** 用 `--no-recommended` 跳过，会自动用 PyMuPDF4LLM 或 Docling。

**PDF 转换相关**
- **转换出来的 Markdown 是空的？** 大概率是扫描版 PDF（无文本层）。装 MinerU 或 Marker（都是 OCR 后端）。
- **转换质量不满意？** 用 `--list-backends` 看看有哪些后端，再用 `--with` 安装更多。

**抽图相关**
- **抽出来的图被切了？** 这是启发式方法已知的边界（双栏跨栏图、纯矢量图、图注在图上方、跨页大图）。用 `--debug` 看 overlay，再用 `crop` 按像素坐标重裁；`SKILL.md` 的 STEP 2.3 列了完整对照表。
- **表格也被当成图截了？** 表格默认按图注识别；不需要时删掉对应 PNG 即可。

**报告相关**
- **报告在哪里？** 最终报告与原 PDF 在同一目录：`<原 PDF 名>_文献报告.md`。
- **想修改报告但不想重新分析？** 直接编辑报告，分析结果已缓存在 `_analysis_cache.json`。

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

MIT，见 [LICENSE](LICENSE)。
