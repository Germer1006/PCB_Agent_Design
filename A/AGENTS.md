# A 模块（系统总体设计）— Codex 项目说明

本文件夹是 PCB_Agent_Design 仓库中的 A 模块。请把 `A` 作为项目根目录打开。

课程四个智能体按流水线衔接：

```
inputs/course_requirements.json
   │
   ▼  A 系统设计  $boost-system-design → outputs/module1/
   │     design_spec.json  bom_module1.csv  boost_params.h  system_design_report.md
   ▼  B 原理图（嘉立创 EDA）   读 design_spec.json + bom_module1.csv
   ▼  C PCB（两层板）          读原理图 + design_spec.json.pcb_guidance
   ▼  D 程序（STM32）          读 boost_params.h + design_spec.json.control / mcu_pin_plan
```

## 约定

- 本模块的工作由 skill `boost-system-design`（`.agents/skills/boost-system-design/SKILL.md`）完成，严格遵守其中的阶段流程与三条铁律。
- 默认工作目录 `outputs/module1/`；试验性运行请用新的工作目录（如 `outputs/test1/`），不要覆盖已交付结果。
- 交接文件由工具生成，不要手改；修改设计决策后重跑工具并升级 `revision`。
- 器件参数与立创编号只能来自 `knowledge/parts_db.json`（带数据手册出处）；新增器件按 SKILL.md“器件库扩充”流程查证。
- Windows 下运行 Python 前设置 `PYTHONIOENCODING=utf-8`；脚本只依赖 Python 标准库。
