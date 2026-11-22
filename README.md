# PCB_Agent_Design

支持 MCU 控制的非同步 Boost CC/CV 设计，以及 RC 低通和电阻分压的 KiCad 草稿流程。各阶段独立记录设计与验证状态。

| 模块 | 本仓库可执行能力 | 交接内容 |
| --- | --- | --- |
| A | 非同步 Boost 计算与验证，RC 低通和电阻分压的标称计算与原理图连接意图 | `design_spec.json`；Boost 另有 BOM 和固件参数 |
| B | 根据明确的符号与引脚连接生成单页 KiCad 原理图，也可接收已有原理图和网表。对照原理图重导网表并运行 ERC。 | `project_input.json`、完整网表、约束与源工程 |
| C | 小型两层板布局布线、项目库冻结、原生 DRC/读回、解压复查、打样状态检查 | `delivery.zip` 与原始检查记录 |
| D | 本仓库尚未实现完整固件模块 | A 的参数头文件不等于经过验证的固件 |

B 的生成入口接收已明确器件和引脚连接的设计。需要复杂分层、多单元符号或嘉立创 EDA 时，需另行准备原理图。C 默认容量为 30 器件/150 焊盘，更大的电路需显式给出预算。

## 使用

先阅读 [A 的项目约定](A/AGENTS.md)、[B 的交接说明](B/README.md) 和 [C 的使用说明](C/使用说明.md)。需要 Python 3.10+、KiCad CLI、与 CLI 匹配的 `pcbnew` Python 和实际封装库。Windows 图形入口在 C 中；命令行不依赖 Tk。

```bash
# A 按自己的流程产生正式交接；关键问题未解决时会拒绝正式导出。
# B 要求输入已经存在的完整原理图/网表，不会凭空生成电路。
python B/prepare_pcb_input.py A_output/design_spec.json B_output/full.xml --schematic B_output/main.kicad_sch --output handoffs/B_to_C_r2
python C/auto_pcb.py handoffs/B_to_C_r2/project_input.json --output C/results --target draft
python C/release_check.py RUN_DIRECTORY
```

`RUN_DIRECTORY` 替换为程序打印的具体运行目录。A 的探索草稿只能通过显式 `--draft-output NEW_DIRECTORY` 导出，保留原状态与阻塞项，不产生正式 BOM/固件头文件。

### 两个非 Boost 端到端示例

从项目根目录执行。将输入文件替换为 `A/inputs/voltage_divider.example.json` 可运行分压示例。每次使用新的输出路径。

```bash
python A/design_circuit.py A/inputs/rc_lowpass.example.json --output rc_design.json
python B/generate_schematic.py rc_design.json --output rc_handoff
python C/auto_pcb.py rc_handoff/project_input.json --ground-planes --size 36 26 --output rc_pcb --target draft
```

原理图输入见 [schematic-intent-1](schemas/schematic-intent-1.schema.json)。草稿保留上游审核事项，另查 `release_status.json` 确认放行状态。

## 约束与交付状态

[pcb-constraints-1 JSON Schema](schemas/pcb-constraints-1.schema.json) 定义分组、真实焊盘对、固定位置、器件允许区域/禁放区和封装审核记录。A 输出布局意图，B 绑定真实引脚，C 计算几何结果。距离限值缺少设计依据时保留 `null`，检查结果为 `not_assessed`。直线距离检查不能证明实际回路面积、Kelvin 接法、载流或温升达标。

| 结果 | 含义 |
| --- | --- |
| `routing_complete` | 原生检查无未连接且引脚网络读回一致，仍可能存在 DRC/布局问题 |
| `complete`（API）/草稿命令返回 0 | 原生 DRC 通过、读回一致、无已失败的几何约束；待评估项仍需审核 |
| `prototype_ready` | 上游无阻塞、封装已核实、规则与几何检查通过、真实审核绑定当前文件、交付包解压复查通过 |
| `production_ready` | 当前固定为 false，量产资格与实物性能验证未实现 |

修改 PCB、源网表、规则或项目库后，旧验证及审核不能继续使用。CLI 解压重读不等于 GUI 重开。`release_check.py` 会列出阻塞项，不会自动补写批准记录。审核完成后可用 `C/package_project.py` 重查并打包现有工程，无需重新生成板子。

## 验证

```bash
python -B -m unittest discover -s C/tests -v
python -B -m unittest discover -s B/tests -v
cd A/.agents/skills/boost-system-design/scripts
python -B -m unittest discover -p 'test_*.py' -v
```

C 包含实际调用 KiCad CLI/pcbnew 的临时工程测试；没有 CLI 时明确跳过。原有 `C/demo/Boost练习.kicad_sch` 是 KiCad 10 格式，不兼容 KiCad 9，不能通过改写版本号假装转换。测试使用可在 KiCad 9 上读入的独立完整网表，不以旧演示截图证明新工程通过。
