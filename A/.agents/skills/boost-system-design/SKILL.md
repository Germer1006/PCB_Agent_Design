---
name: boost-system-design
description: 模块一·系统总体设计智能体。把“MCU 控制的 Boost 恒流恒压电源”课程需求转化为可实施的工程方案：需求解析、功能划分、拓扑选择、CC/CV 控制方案、关键参数计算、器件选型（立创可购、可手焊）、效率/环路复核、系统设计规则检查，并输出给原理图/PCB/程序模块的交接文件（design_spec.json、BOM、boost_params.h）。当用户提到模块一、系统设计、总体方案、Boost 参数计算、器件选型、CC/CV、design_spec 时使用。
---

# 模块一 · 系统总体设计智能体

## 定位

输入一份设计需求（`inputs/course_requirements.json`），输出一套**有依据、可复算、可交接**的系统方案。
四个课程智能体的流水线：**模块一（本 skill）→ 模块二 原理图 → 模块三 PCB → 模块四 程序**；
本 skill 的 `design_spec.json` / `bom_module1.csv` / `boost_params.h` 就是下游的输入。

## 三条铁律

1. **数值交给工具**：任何参数（占空比、电感、电容、损耗、PI 参数、ADC 码值……）都由 `scripts/` 计算，
   模型只负责决策与解释。报告里的数字必须能在 `calc.json / losses.json / loop.json` 中找到。
2. **器件只从知识库选**：`knowledge/parts_db.json` 中每个器件都带数据手册出处与立创编号证据。
   库里没有或数据缺失的器件不得被选中；需要新器件时按“§ 器件库扩充”流程查证后再加入。绝不凭记忆填写型号、编号或参数。
3. **每阶段落盘、关键决策确认**：每个阶段产物写入工作目录；标 ⏸ 的节点向用户说明决策与理由，
   得到确认（或用户明确授权自动执行）后再进入下一阶段。用户修改意见使流程停留在当前阶段重做。

## 目录

```
scripts/      calc_boost.py  select_parts.py  loss_thermal.py(+loss_model.py)  loop_design.py  validate_loop.py
              check_design.py  export_handoff.py(+handoff_contract.py, handoff_header.py)  run_all.py（一键）
              draw_figures.py（框图/工作区图）  md2docx.mjs（报告转 Word，可选）  test_tools.py（单元测试）
              _common.py  boost_model.py  stage_params.py（含容差场景 variants）  svgplot.py
knowledge/    parts_db.json（结构化器件库，含出处）  design_rules.md（工程规则与常见错误，按需检索）
templates/    design_choices.default.json（设计决策模板）  report_outline.md（报告结构）
```

工作目录默认 `outputs/module1/`。运行脚本前先 `cd` 到本 skill 的 `scripts/` 目录，或用绝对路径；
Windows 下建议设置 `PYTHONIOENCODING=utf-8`。只用 Python 标准库。

## 阶段流程（任务编排层）

| 阶段 | 做什么 | 工具 / 知识 | 产物 | 通过条件 |
|---|---|---|---|---|
| S0 准备 | 读需求文件与已有工作目录；新项目则复制需求与决策模板 | — | `requirements.json`, `design_choices.json` | 文件齐全 |
| S1 需求解析 ⏸ | 建需求矩阵（编号/出处/must·target/验证方法）；识别隐含约束与冲突（CC/CV 工作区、输入电流、测试负载模式、Boost 短路限制） | `design_rules.md §1` | 报告 §1 草稿 | 无未处理冲突 |
| S2 功能划分 | 划分功能块（MOD-IN/PWR/DRV/SENSE/MCU/AUX/OUT/DBG）、软硬件分工、能量流/信号流/保护流框图、系统状态机 | `design_rules.md §5,§8` | `system_architecture.mmd`, `operating_states.mmd`（S9 后可用 `draw_figures.py` 生成与 BOM 一致的正式框图） | 每条需求至少落到一个功能块 |
| S3 拓扑选择 ⏸ | 比较非同步/同步 Boost、SEPIC、专用 IC；确定开关频率 | `design_rules.md §2` | `design_choices.json`(topology, fsw) + rationale | 结论写明排除理由与适用边界 |
| S4 控制方案 ⏸ | 确定 CC/CV 结构、采样位置、控制频率、ADC 触发、Dmax/Dmin 策略、保护分层与阈值百分比、软启动 | `design_rules.md §4,§5,§6` | `design_choices.json`(control/sensing/protection) + rationale | 每个参数有理由 |
| S5 参数计算 | `python calc_boost.py <wd>` | 工具 | `calc.json` | 无异常；检查 notes 中的提示 |
| S6 器件选型 ⏸ | `python select_parts.py <wd>`；逐类阅读淘汰原因；某类无候选 → 扩充器件库或回 S3/S4 | `parts_db.json` | `selection.json` | 每类有入选器件 |
| S7 复核 | `python loss_thermal.py <wd>`（标称 + L/C 容差 × ESR 敏感性全组合：效率、结温、纹波、增益峰、浪涌包络）；若提示 η 偏差 → 改 `eta_assumed` 回 S5；`python loop_design.py <wd>`（全工况 × 容差场景的 PI 初值；DCM 点单列排除，不得据 CCM 结论宣称其稳定）；`python validate_loop.py <wd>`（用导出后的 PI 系数独立密扫复核） | 工具 | `losses.json`, `loop.json`, `loop_validation.json`, `bode_*.csv`, `bode_loops.svg` | η 一致；两个环路 ok；独立复核 PASS |
| S8 设计规则检查 | `python check_design.py <wd>` | 工具 | `check.json` | FAIL = 0；WARN 逐条解释。状态 `conditional_review` = 无设计错误，但仍有只能靠仿真/实测关闭的验证项（如 DCM 稳定性、TVS 配合、浪涌限制），它们以 open 项交接，不得宣称“已实测合格” |
| S9 交接与报告 ⏸ | `python export_handoff.py <wd>`；按 `templates/report_outline.md` 写 `system_design_report.md`（论证文字由模型写，数字只引用工具输出与 `report_tables.md`） | 工具 + 模板 | `design_spec.json`, `bom_module1.csv`, `boost_params.h`, `report_tables.md`, `system_design_report.md` | 报告与 JSON 版本号一致 |

快速路径：用户只要结果时，S3/S4 采用模板默认决策，`python run_all.py <wd> --requirements <需求文件>` 一次跑完 S5–S9 的工具部分
（含 η 自动迭代），然后由模型完成 S1–S4 的论证文字与报告。

## 阶段内执行（反应式执行层）

每个阶段内部循环：**计划 → 调用工具 → 读取输出 → 判断 → 修正/重跑**。
工具输出（JSON、打印的警告、SDRC 结果）就是观测；不要跳过读取直接写结论。

SDRC / 工具报错时按下表回溯，只改相关决策并重跑其下游：

| 现象 | 回到 | 典型修改 |
|---|---|---|
| 某类器件 0 通过 | S6 / 器件库 | 查证新增候选；或放宽不合理的决策参数（如纹波比 r） |
| SR20/SR21 占空比 | S4 | 调整 dmax_margin；或换更低损耗器件提高增益峰 |
| SR30 阈值次序 | S4 | 调整 ovp_sw/ovp_hw 百分比或更换 TVS |
| SR50 效率 / SR51 结温 | S6 | 换低 Rds/低 DCR/低 VF 器件；或回 S3 评估同步整流 |
| SR52 纹波 | S5/S6 | 降低 ripple_design_pct 或增加陶瓷电容 |
| SR60 环路 | S4/S6 | 降低控制带宽要求、增大电解电容（阻尼）、提高 f_ctrl |
| η 迭代提示 | S5 | 按提示更新 eta_assumed |

每次修改都在 `design_choices.json` 的 `rationale` 中记录“改了什么、为什么、依据哪个检查项”，并递增 `revision`。

## 器件库扩充

当需要新器件时：
1. 查原厂数据手册（优先厂商官网 PDF），记录 URL、页码/表号与测试条件；
2. 用网络搜索确认立创商城编号（搜索结果中出现的 C 编号即可作为证据，记录片段）；
3. 按 `parts_db.json` 中同类器件的字段名写入，参数按 `{"typ","max","min","condition","page"}` 填写，查不到的写 `null`；
4. `confidence` 如实填写：`high` = 读过原厂手册；`medium` = 读过镜像/规格书或部分数据来自商城列表；
   `low` = 未读到任何手册（**low 的器件不会被选中**）。缺货写 `stock_risk: true`，停产风险写 `lifecycle_risk: true`；
5. 重跑 S6 起的工具。

选型规则摘要（`select_parts.py`）：硬性规则不满足或数据缺失 → 淘汰；评分最优者入选，但评分相差 ≤10% 时视为等价，
按“无缺货风险 → 价格低/价格可查”优先。陶瓷电容数量会扣除 ESR 足够低的储能电容所计入的有效容量，且至少保留 2 只作高频去耦。

## 输出要求

- 报告结构见 `templates/report_outline.md`，要求覆盖课程评分点：方案合理、系统框图清楚、参数计算与器件选择有依据。
- 系统框图用 Mermaid（`system_architecture.mmd`），区分能量流（粗线）、采样/信号流、保护/故障流；需要出图时可用 draw.io 重绘。
- 所有“计算值 / 仿真值 / 实测值”分开标注；未实测的写“计划”。
- 交接文件的使用方式写在报告 §9，并告知下游模块的同学。

## 禁止

- 编造器件型号、立创编号、数据手册参数或实验结果。
- 手工改写工具生成的 JSON/CSV/.h（改设计决策后重跑）。
- 声称非同步 Boost 能对输出短路恒流。
- 在 SDRC 有 FAIL 时把方案标为可交付。
