# CAD 图纸智能修改工作台 · 实现说明（v2.5）

这不是「只加彩色标注」的演示版。当前主链路会真实移动 / 拉伸 DXF 几何实体，
再用 ODA 写回 AC1032 DWG，并从成果文件重新打开、独立复测目标尺寸。

主路径只需要原始 DWG + 一句明确的中文要求。参考图是可选辅助材料；系统不会从
参考图或截图里目测尺寸，缺少参考图也不会阻止已定标型号改图。

## 1. 快速开始

```powershell
cd D:\ai全栈开发\engineering-drawing-ai
python -m pip install -r requirements.txt
python cli.py check
python app.py
# http://127.0.0.1:8900/
```

`cli.py check` 会输出依赖来源、ODA、LLM 配置和已安装档案。核心 CAD 依赖优先用
全局环境；缺少时整体回退到 `src/` 下的 vendored 副本，不混用来源。

## 2. 真实运行证据

| 链路 | 结果 |
|---|---|
| TD50-40G 几何链 | `executed`，约 30 秒，2873 → 2873 实体 |
| TD50 独立复测 | H2 443.0 / D2 294.03 / 轴伸 20.01 mm |
| TD50 质检 | audit、范围外改动、实体数、复测、原标注、总高联动、DWG 往返全通过 |
| 25QYL 标注链 | `executed`，约 13 秒 |
| 25QYL 真实毫米链 | `executed`，约 15 秒 |
| 通用文字链 | 真实 DWG 扫描 17 处；未说明语义时 execute=409；明确“只改标注文字”后 DWG 回读通过 |
| 真实 HTTP 验收 | `tools/http_acceptance.py` 通过：TD50 全链路 + 未定标 409 |
| 无参考图验收 | TD50 `reference=null` 全链路通过；缺信息需求 approve=400 / execute=409 |
| 高清预览 | 修改前后 / 局部 / 定位图同时输出 PNG + SVG；查看器最高 20×，历史任务按需补 SVG |
| 自改进基线 | 4 条 sealed；隔离重放 4/4 通过；0 回归 |
| pytest | 54 passed |

定版产物：

- `output/TD50-40G_最终定版.dwg`（AC1032）
- `output/TD50-40G_最终定版.dxf`
- `output/TD50-40G_最终定版_report.json`
- `output/对照_最终定版_全图.png`
- `output/对照_最终定版_放大图.png`

## 3. 主链路

```text
原始 DWG（只读 + SHA256）
  → ODA 转 DXF（无 ODA 时 aspose 兜底只读）
  → 意图分流（LLM / 关键词兜底；参考图可选且不参与数值计算）
  → 需求不完整 → needs_instruction，拒绝执行并追问
  → 图纸档案匹配（不匹配 → needs_calibration，拒绝执行）
  → 白名单几何变形（ezdxf）
  → 重新打开成果独立复测
  → 五类质检 + DWG 往返重开
  → ODA 写回 AC1032 DWG
  → 黑白交付 DXF/DWG + 彩色对照 PNG + 可缩放 SVG
  → 版本固化
```

各执行链共用 `workbench/engine.py` 的编排：

- `geometry`：H2 / D2 / 轴伸，档案驱动。
- `annotation`：标注换算、竖向标注重叠修复。
- `real_mm`：真实毫米 1:1 标定与两视图外廓等高。
- `text_edit`：任意文字/标注的唯一定位替换；先扫描真实句柄，尺寸数字和共享块有额外语义门禁。

### 上传扫描与文字修改边界

上传 DWG / DXF 后，`GET /api/files/{file_id}/inspect` 会返回可改内容清单：

- `TEXT / MTEXT / ATTRIB / DIMENSION` 的真实句柄、文字、图层、布局或块名。
- `GS_BlockDim*` 中的文字以及原生 `DIMENSION` 会标记为 `is_dimension=true`。
- 块定义被多个实例引用时标记 `shared=true` 和 `insert_count`。
- 标题栏 `ATTRIB` 同时返回隐藏 tag；可按“图号 / 材料 / REV”等字段名直接定位值。
- 扫描结果附带 `suggestions`，前端可一键填入“把材料改成…”等指令模板。
- 扫描项支持多选和逐项填新值；可一键批量填入同一个值，再生成多条句柄指令。
  尺寸项自动补“只改标注文字”，共享块自动补“全部修改”，也可展开全部可改项。
- 选中后同时生成“全图红框 + 局部放大”两张定位图；块内文字会递归展开全部
  INSERT 实例并换算到世界坐标，不会只标第一个块引用。
- 定位图与修改前后对照图均提供 SVG 矢量版；前端查看器支持滚轮缩放、拖拽平移、
  双击和最大 20× 放大，SVG 加载失败自动回退 PNG。
- 高亮只写入 `data/uploads/locate_previews/` 预览副本，不修改上传原图或交付 DWG。

自然语言必须唯一落到句柄才能执行。安全规则：

1. **尺寸数字默认不静默改**：只说“把 610 改成 620”会返回
   `needs_instruction`；明确“只改标注文字”才允许改显示值。
2. **要真实尺寸就走几何链**：说“连图形一起改 / 真实尺寸 / 1:1”不会被
   通用文字链冒充，必须提供真实总长、总高和基准走 `real_mm`。
3. **共享块必须明确“全部修改”**：否则拒绝，防止改一个块定义影响所有实例。
4. **执行后独立复测**：重新打开成果，检查 audit、实体数、几何逐条不变、
   目标文字写入、非目标文字未动，再导出 DWG 并 ODA 回读。
5. **字段只改值**：“把材料改成 304”会把“材料：Q235”改成“材料：304”，
   不会生成错误的“304：Q235”；新值与现值相同会直接返回无可执行修改。
6. **歧义必须落到唯一句柄**：重复文字会返回候选；可用“句柄 BC5 的 610 改成
   620”“第 2 个 610 改成 620”“图层 A 层里的 610 改成 620”锁定，也可说
   “全部修改”。前端候选按钮会自动填入句柄定位指令，尺寸与共享块提示会一并带上。

### 无参考图模式

参考图不是模型输入，也不是定位依据：

- 创建任务时 `files.reference` 可以为 `null`，不影响解析、审批、执行和交付。
- 用户写出对象与数值（如“风罩高度443、接线盒伸出294、轴伸加20”）即可生成计划。
- 只说“按参考图改图”而没给对象/数值时，状态为 `needs_instruction`，
  `approve` 返回 400，`execute` 返回 409，避免套默认值擅自改图。
- 未定标图纸仍返回 `needs_calibration`；这是换型号安全边界，不是参考图依赖。

## 4. 为什么换型号不会改错

旧版把 TD50 的 `hood_top_y`、`box_outer_x`、handle 等坐标写死在
`geometry_modifier.py`，换图会把 TD50 常量套到别的图纸上。现在全部搬到
`knowledge/drawings/<id>.profile.json`：

```text
fingerprint  这张图是谁（SHA256、文件名、实体数、关键 handle、特征文字、bbox）
calibration  1mm 等于多少图面单位（纵向 / 横向分开）
datums       风罩顶、法兰面、电机轴线、接线盒外端、轴端等基准
original     原图 H2 / D2 / 轴伸 / 总高 / 下段尺寸链
rules        三条规则各自的区域、排除 handle、测量 handle、联动尺寸
```

执行前会做指纹校验：

- 硬校验：关键 handle 全在、特征文字全命中、实体数在范围内。
- 软校验：bbox、纵横向定标自洽、SHA256、文件名。
- 硬校验失败 → 拒绝执行，不拿别的图的坐标硬套。

修改前还会做基线校验：如果源图已经是改过的版本（H2/D2 不在原值、轴伸已加长），
直接抛 `SourceNotBaseline`，避免二次位移。

## 5. 质检与交付边界

| 检查 | 说明 |
|---|---|
| `audit` | 成果 DXF 重新打开，audit 0 错误 / 0 修复 |
| `side_effects` | handle 级对比，范围外实体零修改、零删除 |
| `entity_count` | 修改前后实体数一致 |
| `verification` | 成果文件独立复测 H2 / D2 / 轴伸，容差 0.5mm |
| `annotations` | 297 / 175 / 105 / 400 / 200 / 118kg 等原标注保留 |
| `total_height_link` | H2 改动后总高按 `H2 + 280` 联动写回 |
| `reopen` | ODA 写回 DWG → 再转 DXF，handle 集合与实体数核对 |

彩色标注使用独立图层，只存在于 `annotated.dxf` / 对照图；交付文件
`result.dxf` / `result.dwg` 不包含 `变更_*` 图层。

## 6. 自改进 Loop

工作台负责执行、质检和版本；自改进 Loop 负责把每次执行结果变成下一次改动的护栏。
它采用证据驱动的保守闭环，不会让模型自动改生产代码，也不会自动降低质检门槛。

```text
观察任务 / 报告 / 用户验收
  → 归类失败模式
  → 生成回归候选或失败复核提案
  → 在隔离目录重放全部硬检查（含 DWG 往返）
  → 人工批准
  → 写入回归基线
  → 代码 / 门禁指纹变化后，旧回归结论自动过期
```

关键边界：

- 成功结果先成为候选，不能在原任务目录里直接“自证通过”。
- 候选重放失败会进入 `quarantine/`，不污染回归基线。
- 基线入档时会封存源图、预期 DXF/DWG、报告和用例元数据的 SHA256；任何文件、
  目标值或预期标注被改动，重放都会直接拒绝，不能通过修改基线“自证通过”。
- 用户标记 `needs_work` / `rejected` 时，对应该任务版本的旧基线立即失效，
  必须重新执行、验收并隔离重放通过后才能再次入基线。
- 缺少任务类型/新旧报告结构不明的历史样本归为 `legacy`，只保留追溯，
  不会被误算成真实失败或自动生成回归候选。
- `needs_calibration` 是安全边界正常生效，单独归类，不计入失败模式。
- `observe`、`feedback`、封存、提案、重放等操作写入 SQLite 哈希链账本；
  `/api/learn/ledger` 会校验整条链，篡改历史事件会被直接识别。
- 回归机制已覆盖 `real_mm`、`annotation`、`geometry`、`text_edit` 四条执行链；
  当前生产基线为 4 条，四条链路各有 1 条 sealed 用例；文字重放同时检查
  替换片段、最终整句、尺寸语义保护和共享块保护。
- 代码指纹包含生产引擎、独立复测、档案匹配、知识库、图纸档案和规则配置；
  任一项变化都会让 `replay_stale` 变为真，必须重新跑完整回归才能恢复
  `baseline_ready`。
- 门禁指纹单独覆盖重放检查、容差、基线完整性和通过判定代码；只改面板、
  审计账本等非交付逻辑不会误伤基线，但任何放宽或跳过硬检查都会强制重放。

CLI：

```powershell
python cli.py learn status
python cli.py learn cycle
python cli.py learn replay
python cli.py learn seal
python cli.py learn audit --limit 20
python cli.py learn feedback <task_id> accepted --note "验收通过"
python cli.py learn feedback <task_id> needs_work --note "右视图底边仍不一致"
python cli.py learn approve <proposal_id> --note "批准进入基线"
python cli.py learn reject <proposal_id> --note "证据不足"
```

可视化入口：`http://127.0.0.1:8900/learning`

接口：

| 接口 | 作用 |
|---|---|
| `GET /learning` | 自改进 Loop 可视化页面 |
| `GET /api/learn/status` | 样本、基线、失效基线、提案和重放就绪状态 |
| `GET /api/learn/episodes` | 任务结果快照 |
| `GET /api/learn/cases` | 全部基线（含已失效项） |
| `POST /api/learn/cases/seal` | 为历史未封存基线记录 SHA256 |
| `GET /api/learn/proposals` | 人工待审提案 |
| `GET /api/learn/ledger` | 哈希链审计事件与完整性校验 |
| `POST /api/learn/observe` | 扫描任务并生成结果快照 |
| `POST /api/learn/propose` | 根据验收与失败模式生成候选提案 |
| `POST /api/learn/cycle` | 观察 → 诊断 → 提案 → 重放 |
| `POST /api/learn/replay` | 重放全部有效基线，含 DWG 往返 |
| `POST /api/learn/proposals/{id}/approve` | 隔离重放通过后晋升基线 |
| `POST /api/learn/proposals/{id}/reject` | 拒绝提案并保留证据 |
| `POST /api/tasks/{id}/feedback` | 记录 `accepted / needs_work / rejected` |

学习状态保存在 `data/learning/`：`episodes/`、`proposals/`、`cases/`、
`replays/`、`quarantine/`、`feedback.jsonl`、`state.json`、`ledger.db` 和
`ledger_errors.jsonl`。状态接口会给出 `next_action`，明确当前应封存、重放、
审核提案还是可以继续交付。

## 7. Web API

FastAPI，默认端口 8900。主要接口：

| 接口 | 作用 |
|---|---|
| `GET /api/health` | 服务健康检查 |
| `GET /api/env` | 依赖来源、ODA、LLM、可写性体检 |
| `GET /api/profiles` | 已安装图纸档案 |
| `GET /api/profiles/{id}` | 档案详情 |
| `POST /api/profiles` | 保存档案（需完整结构） |
| `GET /api/skill` | 知识卡与规则 |
| `POST /api/parse-intent` | 自然语言 → 意图（不建任务） |
| `POST /api/files` | 上传 DWG / DXF；参考图可选 |
| `GET /api/files/{file_id}/inspect` | 上传后扫描可改文字/标注、尺寸标记与共享块 |
| `GET /api/files/{file_id}/locate` | 按句柄返回世界坐标框和两张高亮图地址 |
| `GET /api/files/{file_id}/locate.png` | 全图 / 局部放大高亮 PNG（`mode=full/zoom`） |
| `GET /api/files/{file_id}/locate.svg` | 全图 / 局部放大高亮 SVG，放大不失真 |
| `POST /api/tasks` | 建任务并解析 |
| `GET /api/tasks` | 任务列表 |
| `GET /api/tasks/{id}` | 任务详情 |
| `GET /api/tasks/{id}/entities` | 实体浏览 |
| `POST /api/tasks/{id}/approve` | 人工确认计划 |
| `POST /api/tasks/{id}/execute` | 执行；未定标或需求不完整返回 409 |
| `GET /api/tasks/{id}/preview` | 对照图地址 |
| `GET /api/tasks/{id}/report` | 质检报告 |
| `GET /api/tasks/{id}/download` | 下载产物；支持 DWG/DXF、报告、PNG/SVG 预览 |
| `POST /api/tasks/{id}/rollback` | 版本回滚 |
| `POST /api/demo/td50` | TD50 几何示例 |
| `POST /api/demo/25qyl` | 25QYL 标注示例 |
| `POST /api/demo/25qyl-real-mm` | 25QYL 真实毫米示例 |

## 8. CLI

```powershell
python cli.py check
python cli.py profiles list
python cli.py profiles show td50-40g
python cli.py measure output\TD50-40G_最终定版.dxf

python cli.py run input\TD50-40G.dwg "H2=443, D2=294, 轴伸加20" `
  --targets "H2=443,D2=294,shaft=20" --no-llm --out output\cli_td50
```

`run` 会完整走解析、档案匹配、确认、执行、质检、导出、对照图，并把产物复制到
`--out`。未定标图纸返回 2，执行失败返回 3。

## 9. 新图纸定标流程

```powershell
python cli.py calibrate input\新图纸.dwg --id new-model --title "新图纸安装图"
```

输出：

- `knowledge/drawings/new-model.draft.json`：档案骨架（不会自动参与匹配）。
- `knowledge/drawings/new-model.scan.json`：候选尺寸文字、DIMENSION、最长线、
  最大圆/弧、bbox。块内文字会展开 INSERT 一并扫描。
- `output/_calibration/new-model.dxf`：ODA 转出的工作 DXF。

人工要确认：

1. 从 `text_candidates` / `block_text_candidates` / `dimensions` 找到 H2、D2、
   轴伸对应的尺寸文字与 handle。
2. 用两条已知尺寸线反算 `calibration.vertical/horizontal.units_per_mm`。
3. 填写 `datums`、`original`、`rules` 的区域与排除 handle。
4. 填 `fingerprint.required_handles`（至少 3 个）和 `markers`。
5. 把 `review.status` 改为 `confirmed`，再安装：

```powershell
python cli.py profiles install knowledge\drawings\new-model.draft.json
```

安装门槛会拒绝未确认、缺少关键 handle、定标/基准/原值为空或规则区域为空的档案。

## 10. 测试

```powershell
python -m pytest -q
```

真实 HTTP 全链路验收（先启动 `python app.py`）：

```powershell
python tools\http_acceptance.py
```

脚本只调用公开 API，完成上传、建任务、人工确认、执行、下载 DXF/DWG/报告和
对照图；随后独立回读交付 DWG，核对 AC1032、handle 集合、audit、黑白图层，
并复测 H2 / D2 / 轴伸。整个 TD50 验收不传参考图，并显式断言
`files.reference is null`。它还会验证：

- 缺信息需求进入 `needs_instruction`，`approve=400`、`execute=409`；
- 未定标图纸的几何请求返回 409；
- 证据写入 `output/http_acceptance.json`。

覆盖：

- TD50 档案命中；未知图纸拒绝。
- 新引擎产物与 golden `result.dxf` 逐 handle 几何对齐（1e-6 容差）。
- 交付 DXF 不含 `变更_*` 彩色图层。
- 独立复测通过；已改图再次修改抛 `SourceNotBaseline`。
- 意图解析：H2/D2/轴伸数值、`297 不是 D2` 否定语境、annotation / real_mm 分流。
- 无参考图创建任务：`files.reference is None`，TD50 仍进入 `planned`。
- 只写“按参考图改图”不会启用默认规则，任务进入 `needs_instruction`。
- 上传侧车索引可在未建任务时找回文件。
- 尺寸数字未说明语义时拒绝静默改标签；明确“只改标注文字”后才执行。
- 共享块文字未说明“全部修改”时拒绝；明确全部后才写块定义。
- 字段式指令只替换字段值；标题栏 `ATTRIB` 可按 tag 定位，无需知道当前显示值。
- 批量句柄指令可一次修改多项，并分别保留尺寸“只改标注文字”和共享块
  “全部修改”的语义；新值外层中文引号不会写进 CAD 文字。
- 块内文字定位会组合嵌套 INSERT 矩阵，并返回共享块的全部可见实例框；
  定位接口可生成全图与局部放大 PNG/SVG，上传原文件哈希保持不变。
- 历史任务没有 SVG 时不重新执行，只从现有 `clean.dxf` / `result.dxf` 按需补渲染；
  预览高亮只写缓存副本，交付 DWG/DXF 保持原样。
- 文字子串替换按“替换片段 + 最终整句”分别固化，回归重放不会错把整句再替换一次。
- FastAPI `/api/health`、`/api/env`、`/api/profiles`、learning API 冒烟。
- 无 `task_type` / 报告元数据的历史任务归为 `legacy`，不进入失败模式。
- 未定标图纸归为 `needs_calibration`，不进入失败模式。
- 需求不完整归为 `needs_instruction`，不进入失败模式。
- SQLite 哈希链能检测审计事件被篡改。

## 11. 目录

```text
app.py                 FastAPI 入口（主）
cli.py                 命令行入口
server.py              v1 兼容入口（已弃用，启动同一 FastAPI app）
src/runtime.py         依赖引导与环境体检
src/intent_parser.py   意图解析（LLM + 确定性兜底）
src/edit_intent.py     文字/标注替换意图与句柄唯一匹配
src/text_edit_engine.py 通用文字/标注执行、尺寸语义与共享块保护
src/text_locate.py     文字/标注句柄定位、块实例展开与世界坐标包围盒
src/drawing_profile.py 图纸档案加载 / 校验 / 匹配
src/drawing_measure.py 从成果文件独立复测
src/geometry_modifier.py 档案驱动几何变形
src/dwg_bridge.py      DWG → DXF（ODA 优先，aspose 兜底）
src/dwg_export.py      DXF → DWG（独立临时目录 + 往返校验）
src/calibration.py     新图纸扫描与档案骨架
src/gen_compare.py     SVG 对照图（handle 级 diff）
src/learning_ledger.py SQLite 哈希链审计账本
workbench/engine.py    三条链统一编排
workbench/store.py     文件型 JSON 存储 / 版本
knowledge/             知识卡、词典、免疫约束、图纸档案
web/workbench.html     工作台前端
tools/http_acceptance.py 真实 HTTP 全链路验收
tests/                 pytest 回归
legacy/                已归档旧代码与旧前端
```

## 12. 环境与已知边界

- Python 3.11；依赖见 `requirements.txt`。
- DWG 写回必须有 ODA File Converter。没有 ODA 时不能交付 DWG。
- LLM 使用 DashScope 兼容接口，环境变量 `DASHSCOPE_API_KEY`；
  `DASHSCOPE_BASE_URL` / `DASHSCOPE_MODEL` 可选。未配置时自动用关键词兜底。
- 存储是文件型 JSON，适合单机；没有用户体系、权限、队列和数据库。
- 几何修改目前只直接处理模型空间实体；块内几何需要先展开或补规则。
- 真实毫米链的 610 / 284 锚点目前面向 25QYL 图纸；换图需同步锚点。
- 静态方案 HTML 是历史设计文档；以本 README 与 `项目介绍.md` 为准。

## 13. Codex Skill 一键入口

真实毫米 1:1 流程已封装为个人 Skill `cad-real-mm`，位置：

```text
C:\Users\李展宇\.agents\skills\cad-real-mm
```

它只做入口、参数校验和交付守门；几何算法仍只保留在本项目的
`src/real_mm_engine.py`，不会出现两套实现。

```powershell
python "C:\Users\李展宇\.agents\skills\cad-real-mm\scripts\run_real_mm.py" `
  "input\25QYL-2SL.dwg" `
  --out-dir "output\25QYL-2SL_真实毫米交付" `
  --stem "25QYL-2SL_1比1_真实毫米610"
```

脚本会完成 DWG→DXF、水印清理、毫米单位、两视图实体外廓归一化、标注回写、
AC1032 DWG 导出、DWG 回读、13 项硬检查，并输出质检报告与说明。

工作台仍保留上传、计划确认、任务历史、版本回滚和可视化预览；推荐把 Skill
作为日常入口，把工作台作为多人或演示场景的可视化底座。
