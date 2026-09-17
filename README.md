# lit-report — 科学文献批判性分析报告生成技能

把一个 PDF 论文变成**论点驱动的中文批判性分析报告**，而不是流水账式的翻译或 Figure 逐条描述。

输出的报告会回答：这篇文章到底声称了什么？证据链是否完整可靠？和已有文献是否矛盾？这个创新点的实际价值有多大？

## 功能

- **PDF 解析多后端自动回退**：MinerU / Marker / PaddleOCR / Docling / PyMuPDF4LLM / MarkItDown，按质量降序逐个尝试，缺哪个跳哪个，失败自动换下一个；扫描件自动判定并只走 OCR 后端
- **Figure 自动抽取，无需手工截图**：脚本从 PDF 文本层识别图注、定位图形区域、300 DPI 裁剪为 PNG，并输出置信度与框选 overlay；AI 逐张目视复核，不合格的按像素坐标重裁
- **双版本报告**：学术版深度批判性分析（Markdown）+ 微信公众号版（面向领域同行的严肃科学风格，独立撰写）
- **文献交叉验证**：通过 PubMed MCP 检索 DOI/PMID/MeSH、相关文献与标准引用格式
- 完全自包含：打包整个文件夹分享即可，任何用户、任何 agent 均可使用

## 依赖一览

| 依赖 | 类别 | 说明 |
|------|------|------|
| **PyMuPDF** | **必装** | 自动抽图、整页渲染、手工重裁 |
| **pubmed MCP**（`pubmed_*` 工具） | **必装** | 背景调研：DOI/PMID/MeSH/相关文献/引用格式。未安装时工作流会暂停引导安装 |
| PDF→Markdown 后端（任一） | **必装至少一个** | 最低成本：`pymupdf4llm`（纯 CPU、秒级） |
| WebSearch / puppeteer MCP | 推荐 | 补充检索（预印本等），缺失时自动降级 |
| GPU | 可选 | 装 MinerU / Marker 时加速 |

## 安装

1. **放置技能目录**：把 `lit-report/` 整个文件夹放进你的 agent 技能目录，例如：
   - Claude Code：`~/.claude/skills/`
   - 其他 agent：其约定的 skills 目录
2. **安装 pubmed MCP**：在 agent 的 MCP 配置中加入 pubmed server，然后重启会话。
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
3. **环境体检**（默认只检查，不安装任何东西）：
   ```bash
   cd lit-report
   python scripts/setup_env.py
   ```
4. **按需安装**（脚本会明确告知要装什么，体积多大）：
   ```bash
   python scripts/setup_env.py --install                        # 最小依赖
   python scripts/setup_env.py --install --with docling,markitdown
   python scripts/setup_env.py --install --with mineru          # 约 6GB，建议 GPU
   ```

## 使用

对任意 AI 助手说："帮我分析这篇文献 `<PDF 路径>`"，**不需要提供截图目录**。

底层脚本也可手动直接调用：

```bash
# 1) 看看本机有哪些 PDF 后端
python scripts/pdf2md.py --list-backends

# 2) PDF → Markdown（自动选择可用后端）
python scripts/pdf2md.py --pdf paper.pdf --out ./md_out

# 3) 自动抽图（--debug 输出框选 overlay 供核对）
python scripts/extract_figures.py auto --pdf paper.pdf --out ./md_out --debug --teaser

# 3b) 复核不通过时：整页渲染 + 按像素坐标重裁
python scripts/extract_figures.py render-pages --pdf paper.pdf --out ./md_out --pages 3 --dpi 200
python scripts/extract_figures.py crop --pdf paper.pdf --out ./md_out \
    --page 3 --px 60,120,540,1520 --dpi 200 --name fig1
```

抽图结果清单：`<out>/figures/figures.json`（每张图的 id、页码、bbox、绝对路径、图注、置信度）。

## 目录结构

```
lit-report/
├── SKILL.md                 技能主文档（agent 读取的完整工作流）
├── README.md                本文件
├── LICENSE
└── scripts/
    ├── setup_env.py         环境自检与可选安装（默认零副作用）
    ├── pdf2md.py            PDF→Markdown 多后端自动回退
    └── extract_figures.py   自动抽图（auto / list-captions / render-pages / crop）
```

## 扩展新的 PDF 识别器

在 `scripts/pdf2md.py` 中：

1. 在 `BACKENDS` 注册表加一条（质量分、探测模块名、是否具备 OCR、说明）；
2. 写一个 `run_xxx(pdf, target_dir, md_path) -> md_path` 函数，并登记进 `RUNNERS`。

主流程零改动，`auto` 模式会自动把它纳入回退链。

## 常见问题

- **没有 conda？** `setup_env.py --install` 会自动改用 `python -m venv`。
- **pubmed MCP 没装？** 工作流会在背景调研阶段暂停并提示安装。
- **抽出来的图被切了？** 这是启发式方法已知的边界（双栏跨栏图、纯矢量图、图注在图上方、跨页大图）。用 `--debug` 看 overlay，再用 `crop` 按像素坐标重裁；`SKILL.md` 的 STEP 2.3 列了完整对照表。
- **转换出来的 Markdown 是空的？** 大概率是扫描版 PDF（无文本层）。装 MinerU 或 Marker（都是 OCR 后端）。
- **表格也被当成图截了？** 表格默认按图注识别；不需要时删掉对应 PNG 即可。
- **需要换 conda 环境名？** `python scripts/setup_env.py --install --env-name 你的环境名`。

## 许可证

MIT，见 `LICENSE`。
