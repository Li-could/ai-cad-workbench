# AI CAD Workbench

一个面向机械工程图的本地 AI 改图工作台。

上传 DWG / DXF，用中文描述修改要求，系统先识别目标实体、生成可审查计划，确认后再修改 CAD 几何或文字，并输出 DWG、DXF、质检报告和可缩放对照图。

> 这个仓库只包含可公开的源码、Web 界面、知识规则和测试。客户图纸、历史任务、生成产物、离线依赖包不会进入仓库。请使用你自己的图纸测试。

## 主要能力

- **真实几何修改**：按已定标图纸档案移动 / 拉伸实体，不是只改尺寸文字。
- **通用文字修改**：扫描 `TEXT / MTEXT / ATTRIB / DIMENSION`，按 CAD 句柄精确定位。
- **尺寸标注保护**：修改尺寸数字时必须明确“只改标注文字”，避免图形与标注静默不一致。
- **共享块保护**：块定义被多个实例引用时，必须明确“全部修改”。
- **执行前确认**：先生成修改计划，人工确认后执行。
- **独立质检**：重新打开成果复测、实体数检查、范围外改动检查、DXF 审计和 DWG 往返重开。
- **可缩放预览**：修改前后对照、局部放大和选中项定位，同时输出 PNG 与 SVG。
- **版本与回滚**：每次成功执行固化版本，可在工作台回滚。
- **本地优先**：任务数据保存在本机文件系统，不需要云数据库。

## 技术栈

- Python 3.11+
- FastAPI + Uvicorn
- ezdxf
- Aspose.CAD（DWG 读取兜底）
- matplotlib（PNG 渲染）
- ODA File Converter（DWG 写回，可选安装）
- 单页原生 HTML / CSS / JavaScript 工作台

## 快速开始

```bash
git clone https://github.com/Li-could/ai-cad-workbench.git
cd ai-cad-workbench
python -m venv .venv
```

Windows：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements.txt
python cli.py check
python app.py
```

macOS / Linux：

```bash
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
python cli.py check
python app.py
```

浏览器打开：

```text
http://127.0.0.1:8900/
```

也可以直接使用兼容入口：

```bash
python server.py
```

## 使用流程

1. 上传 DWG 或 DXF。
2. 等待系统扫描可修改的文字、尺寸标注和共享块。
3. 在“修改要求”中描述目标；可以勾选扫描项、批量填值并生成句柄指令。
4. 点击“解析并生成修改计划”。
5. 检查目标、原值、新值和风险提示。
6. 确认计划并执行。
7. 查看质检结果、对照图和局部放大图。
8. 下载 DWG、DXF 或质检报告。

仓库附带一个仅用于验证界面的合成示例：

```text
examples/sample.dxf
```

它不是完整产品图纸，不包含客户数据。

## 环境变量

所有环境变量都是可选的：

| 变量 | 用途 |
|---|---|
| `PORT` | 服务端口，默认 `8900` |
| `DASHSCOPE_API_KEY` | 使用 DashScope 进行自然语言意图解析 |

未配置 LLM Key 时，系统会使用关键词规则兜底。参考图不参与尺寸计算。

不要提交 `.env`、API Key、客户图纸或任务产物到 Git。

## DWG 写回

- 读取 DWG：优先 ODA，Aspose.CAD 可作为只读兜底。
- 导出 DWG：需要安装 ODA File Converter。
- 没有 ODA 时，仍可处理 DXF，但无法保证输出高质量 DWG。

安装 ODA 后运行：

```bash
python cli.py check
```

输出中会显示 ODA 是否可用。

## 测试

```bash
python -m pytest -q
```

公开仓库不包含来源于客户图纸的 golden fixture。依赖这些 fixture 的集成测试会自动跳过。

## 项目结构

```text
app.py                 FastAPI 入口与 API
cli.py                 校验、定标、安装档案、命令行任务
server.py              兼容启动入口
src/                   核心 CAD、意图、质检和自学习模块
workbench/             工作台编排与文件型任务存储
web/                   Web 工作台页面
knowledge/             通用变更知识卡、领域词典与约束
spec/                  修改计划 JSON Schema
skill/                 可复用的 CAD 规则与 Skill 说明
tests/                 单元与合成样例测试
examples/sample.dxf    可公开的合成测试图
docs/ARCHITECTURE.md   架构和执行链路说明
```

## 安全边界

- 这是一个本地单用户工作台，默认没有登录、权限和多租户隔离，不要直接暴露到公网。
- 原始上传文件只读保存并计算 SHA-256。
- 修改在任务副本上执行。
- 彩色标记只进入预览图，不写入交付 DWG。
- 修改完成后必须通过独立复测和 DWG 往返检查。
- 未定标的新图纸会拒绝几何修改，避免套用错误坐标。

## 已知限制

- 几何规则依赖图纸档案；新结构需要先定标并配置目标区域。
- 当前专注 2D CAD 工程图的受控变更。
- 文件型 JSON 存储适合单机，不适合高并发多用户部署。
- 自动识别的结果仍需工程人员审查，不能替代最终设计签审。

## 数据与许可证

仓库不包含任何客户图纸、上传文件、任务记录或生成成果。

当前未附加开源许可证。代码公开可查看和克隆；如需再分发、商用或二次授权，请先联系仓库作者。

