# A：系统总体设计智能体（模块一）

把课程需求“MCU 控制的 Boost 恒流恒压电源（5 V → 12 V / 0.5 A）”转化为一套**有依据、可复算、可交接**的工程方案，作为 B（原理图）、C（PCB）、D（程序）的输入。

## 在 Codex 中使用

1. 克隆本仓库，在 Codex（桌面版/CLI/IDE 均可）中**把 `A` 文件夹作为项目打开**。
   skill 位于 `A/.agents/skills/boost-system-design/`，Codex 从当前项目目录查找 `.agents/skills`。
2. 在输入框输入 `$` 选择 `boost-system-design`，例如：
   ```
   $boost-system-design 按 inputs/course_requirements.json 完成模块一系统设计，工作目录用 outputs/test2
   ```
   默认为**新手模式**：只在需求确认（S1）、出现只有你能回答的问题（偏好、采购、焊接能力等）以及交接前（S9）停下；
   拓扑、控制、选型阶段用白话汇报后继续，并附“答辩可能被问到的问题”。工程正确性由工具计算和规则检查把关，不需要你判断。
   可在指令里加：“每个阶段都确认” / “专家模式” / “自动推进，最后统一确认”。
3. 不经过 Codex、只验证工具链：
   ```bash
   cd .agents/skills/boost-system-design/scripts
   set PYTHONIOENCODING=utf-8          # Windows
   python run_all.py ../../../../outputs/test0 --requirements ../../../../inputs/course_requirements.json
   python -m unittest test_tools -v
   ```
   只依赖 Python 3 标准库；`draw_figures.py --png` 需要本机有 Chrome/Edge；`md2docx.mjs` 需要 Node.js（见文件头说明）。

## 智能体做什么

| 阶段 | 内容 | 工具 |
|---|---|---|
| S1 需求解析 | 需求矩阵、约束强度、隐含约束（CC/CV 工作区、Boost 短路边界、测试负载模式） | `knowledge/design_rules.md` |
| S2 功能划分 | 功能块、软硬件分工、系统框图、状态机 | `draw_figures.py` |
| S3 拓扑选择 | 非同步/同步 Boost、SEPIC、专用 IC 对比 | `design_rules.md` |
| S4 控制方案 | CC/CV 双 PI 最小值选择、采样时序、分层保护 | `design_choices.json` |
| S5 参数计算 | 占空比、电感、电容、应力、采样网络、保护阈值、PWM/资源预算 | `calc_boost.py` |
| S6 器件选型 | 规则筛选 + 评分，器件只来自带数据手册出处的库 | `select_parts.py` + `parts_db.json` |
| S7 复核 | 平均模型、容差扫描、PI 设计及独立复核；T1 开关级仿真验证启动、负载阶跃、轻载和保护 | `loss_thermal.py`、`loop_design.py`、`validate_loop.py`、`sim_switching.py` |
| S8 规则检查 | 系统设计规则检查（SDRC），类似 PCB 的 DRC | `check_design.py` |
| S9 交接 | 设计基线 JSON、BOM（立创编号）、固件参数头文件、报告数据表 | `export_handoff.py` |

设计原则：**数值全部由脚本计算；器件只从知识库选取（缺数据则淘汰）；关键阶段向用户确认**。
对应参考论文《基于大语言模型的 PCB 自动化设计智能体》的分层编排、检索增强抑制幻觉和结构化输出。

## 工具链 v2 回归基线（计算值，未实测）

| 项目 | 结果 |
|---|---|
| 拓扑 / 频率 | 非同步 Boost，100 kHz |
| MCU | STM32G431CBT6（片内比较器 + DAC → TIM1 刹车，硬件保护） |
| 关键器件 | L1 MWSA1206S-680MT · Q1 AO3400A · D1 SS54 · Cout 25SVPF100M + 2×10 µF · 驱动 LM5114B · 采样 50 mΩ + INA180A2 |
| 效率 | 标称 84.85 %，L/C 容差与 ESR 全组合最低 83.92 %（目标 ≥ 75 %） |
| 纹波 | 容差场景最大 0.77 %（上限 5 %，建议 ≤ 3 %） |
| 环路 | CV 489 个 CCM 工况、CC 225 个工况全部 PM ≥ 60°、GM ≥ 6 dB；36 个 DCM 点单列，待仿真/实测 |
| CC 范围 | 6.25–12 V，对应 CR 12.5–24 Ω；低于下限为不可控区 |
| SDRC | 78 PASS / 8 WARN / 0 FAIL → `conditional_review`（无设计错误；8 项为仿真/实测才能关闭的验证项，已指定责任模块） |

> 注：`outputs/module1/` 目前仍是 r1 旧版（工具链 v1），将用 v2 重新生成；数值以上表为准。

## 交接给其他模块

| 文件 | 给谁 | 内容 |
|---|---|---|
| `outputs/module1/design_spec.json` | B / C / D | 设计基线：功能块、网络名、接口、器件与连接、MCU 引脚规划、保护与控制参数、PCB 布局要点、测试计划 |
| `outputs/module1/bom_module1.csv` | B | 位号、值、封装、**立创编号**、连接说明 |
| `outputs/module1/boost_params.h` | D | PWM、采样换算系数、PI 参数、Dmax、保护阈值 |
| `outputs/module1/system_block.png` 等 | 全组 | 系统框图、控制框图、状态机、工作区图、Bode 图 |

上游文件由工具生成，请不要手改；需要修改时提出，由 A 修改设计决策后重新生成并升级 `revision`。

## 后续改进

见 [docs/改进任务书_v3.md](docs/改进任务书_v3.md)。T1 已接入工具链；T4 已增加结构化规则与自动门禁，原厂手册独立复核的边界见下文。后续还有消融评测、计算依据账本和 skill 规范化。

### T4 规则检查与门禁

规则定义位于 `knowledge/sdrc_rules.json`，检查结果含层级、严重度、理由、来源、验证方式和关闭方法。未知规则、重复或不完整的元数据会报错。报告表 G 按层级排序。

`check_design.py` 退出码：0 无 FAIL 且无 critical/major NOT_ASSESSED；2 有 FAIL；3 无 FAIL 但关键证据缺失；1 程序错误。`run_all.py` 原样传播退出码并停止，直接导出也检查门禁。缺仿真、缺器件或缺参数会保留 NOT_ASSESSED；未配置输入缓升时 SR88 明确为 N/A，浪涌实物关闭项 SR53 仍然保留。

状态策略对任务书作显式澄清：基线 SR38（内部纹波目标）和 SR44（部分误差预算）原本就是 automatic 的 minor WARN，继续作为公开的建议项，基线保持 conditional_review。automatic 的 critical/major WARN 会进入 needs_rework。不能把自动检查重新标成实测来绕过状态策略。检查退出码为 0 也不等于硬件已验证或允许制作。审计范围、规则来源及未完成的原厂手册复核见 [sdrc_audit.md](.agents/skills/boost-system-design/knowledge/sdrc_audit.md)。

### T1 开关级仿真

`run_all.py` 在环路独立复核后自动运行 T1。已有计算、选型、损耗和环路结果的工作目录也可以直接执行 `python sim_switching.py <workdir>`。完整仿真输出 `sim.json`、各工况降采样 CSV 和 `sim_waveforms.svg`，系统规则检查从 `sim.json` 读取指标。

r2 将过流工况分为不过阈值、软件延时动作及硬件快速动作，并增加独立输入缓升工况。带缓升的工况参与 SR88，未加保护的直插波形在 `sim.json` 的 `reference_cases` 中保留。验证方法及实物边界见 [T1 保护验证](docs/T1-protection-validation.md)。

在 `scripts/` 中，对已完成参数计算和选型的试验工作目录运行：

```bash
python sim_switching.py <workdir> --duty <0到1之间的占空比> --load-ohm <负载电阻> --cycles <周期数>
python sim_switching.py <workdir> --closed-loop --load-ohm <负载电阻> --cycles <周期数>
python -m unittest test_tools test_sim_switching -v
```

脚本复用 `stage_params.build()` 的功率级参数，输出 `sim_open_loop.csv`，记录时间、电感电流、输出电压、输出电流、占空比和开关状态。PWM 翻转处可有相同时间戳的两行，用于记录电容 ESR 造成的电压跳变。
单工况闭环命令还需要同版本的 `loop.json`。它使用与固件参数头文件相同的计算数据，在设定周期采样，更新双 PI，并将量化后的占空比用于下一 PWM 周期。输出 `sim_closed_loop.csv`，包含采样事件、主导环、待写入的比较计数和故障原因。硬件比较器在积分步上检查，实际 MCU 路由和保护延时仍需验证。

## 目录

```
A/
├── .agents/skills/boost-system-design/   智能体（SKILL.md、scripts/、knowledge/、templates/）
├── inputs/course_requirements.json       输入：课程指标 + 项目约束
├── outputs/module1/                      输出：设计结果与交接文件
├── AGENTS.md                             Codex 项目说明
└── README.md
```

数据手册与参考论文 PDF 因版权与体积未放入仓库；每个器件的数据手册链接见 `parts_db.json` 的 `datasheet_url`。
