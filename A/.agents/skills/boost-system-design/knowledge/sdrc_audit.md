# T4 SDRC 实现与证据审计

日期：2026-11-12。审阅者：Codex（代码与本地知识库审阅）。

本次核对规则元数据、脚本表达式及本地 design_rules.md/parts_db.json 的引用。没有重新下载并逐页阅读全部原厂 PDF。知识库已有的页码、条件、URL 不等于本次独立核验；任务书要求的原厂手册独立审计仍待完成。此改动适合作为功能完成、手册审计待补的草稿提交。

## 发现与处理

- 缺少 TVS、电容、LDO、驱动器、电感、放大器参数曾被跳过、以零代替或触发异常：改为明确的 NOT_ASSESSED。数据库缺少入选 ID 时也保留缺项。
- 未运行 T1、某项场景缺失、空交叉验证：NOT_ASSESSED。仿真格式或 revision 错误：SR79 FAIL。
- r6 未配置 input_ramp：SR88 为 N/A 并说明原因，SR53 的浪涌实物验证 WARN 保留。未选用热模型器件时相关 SR51 为 N/A，选型完整性仍由 SR10 检查。
- 未登记、重复、不完整或枚举非法的元数据报错。表 G 按层级排序，每条结果携带严重度、依据及来源。
- check_design CLI 与 run_all 传播退出码 0/2/3，程序异常为 1；直接导出也阻止 FAIL、关键缺证据和 needs_rework。

## 状态定义澄清

任务书同时要求 conditional_review 只有 simulation/measurement WARN，以及现有基线保持 conditional_review；但基线 SR38、SR44 是 automatic/minor WARN。实现保留真实 verify 类型，允许 minor/info 自动建议进入 conditional_review，并公开 status_policy。automatic critical/major WARN 为 needs_rework。此处是明确的实现取舍，需维护者审阅，不能声称逐字满足原定义。critical/major NOT_ASSESSED 同样进入 needs_rework，与退出码 3 一致。

## 验证与限制

33 项测试通过，其中新增 T4 测试 11 项，包含 CLI 退出码和直接导出门禁测试。复用真实 r6 T1 的 58 场景结果（54 PASS、4 WARN），重新运行 check_design/export_handoff：87 PASS、8 WARN、0 FAIL、0 NOT_ASSESSED、新增 1 N/A（SR88）。计算、损耗、环路数值未由 T4 改动。没有把这些软件测试称为实物验证。

## 逐规则记录

下表仅记录代码与本地依据引用审阅；涉及器件的原厂 PDF 独立复核均待补。

| 注册 ID/族 | 本地依据 | 审阅范围 |
|---|---|---|
| SR01 | course_requirements.json: rated output power, voltage and current | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR02 | design_rules.md §2 | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR03 | course_requirements.json: fsw.range_hz | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR04-* | course_requirements.json: protection.required and protection.suggested | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR05 | course_requirements.json: controller | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR10-* | parts_db.json and select_parts.py hard constraints | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR11-* | parts_db.json lcsc_evidence | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR20 | calc.json and losses.json operating corners | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR21 | boost_model.py gain_peak and protection duty limit | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR22 | design_rules.md §5 and calc.json protection envelope | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR30 | design_rules.md §5; parts_db.json tvs.params.vbr_min_v (page/condition) | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR31 | parts_db.json tvs.params.vc_v and mosfet.params.vds_max_v (page/condition) | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR32 | parts_db.json tvs.params.vc_v and cout_bulk/cout_ceramic.params.voltage_v (page/condition) | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR33 | course requirements and design_choices.json protection | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR34 | design_rules.md §5 and fault analysis | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR35 | TVS datasheet pulse conditions and hardware test plan | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR36 | parts_db.json inductor.params.isat_a (page/condition); losses.json envelope.IL_pk_max_A | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR37 | parts_db.json inductor.params.irms_a (page/condition); losses.json envelope.IL_rms_max_A | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR38 | design_choices.json and losses.json corner scan | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR39 | parts_db.json cout_bulk.params.ripple_current_ma_100khz (page/condition); losses.json corner_cases | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR40 | calc.json sensing and protection | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR41 | calc.json sensing and protection | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR42 | course requirements and calc.json sensing | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR43 | course requirements and calc.json sensing | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR44 | design_rules.md §6/§9.1; parts_db.json csa.params.vos_uv/gain_error_pct (page/condition); requirements.json iout.steady_err_pct | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR45 | course requirements and measurement design | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR46 | MCU, amplifier and reference datasheets; hardware routing review | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR50* | losses.json and course requirements | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR51-* | losses.json thermal model and component datasheet | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR52* | losses.json ripple model and course requirements | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR53 | design_rules.md §5 and input protection test plan | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR54 | fuse datasheet and losses.json inrush estimate | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR60-* | loop_validation.json and loop.json | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR61 | calc.json PWM resource budget | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR62 | calc.json timing budget | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR63 | calc.json timing budget | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR64 | calc.json CC range and design_rules.md §1 | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR65 | loop_validation.json and sim.json | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR70 | parts_db.json ldo.params.dropout_mv (page/condition); design_rules.md §9.1 | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR71 | parts_db.json gate_driver.params.uvlo_on_v/vdd_min_v and pmos/fuse resistance; design_rules.md §9.1 | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR72 | losses.json eta_consistency | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR73 | model assumptions and hardware validation plan | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR79 | sim.json revision and schema | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR8[0-5]* | sim.json scenario checks and T1 acceptance matrix | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR86 | sim.json cross_validation and losses.json | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR87 | sim.json SR87 scenarios and T1 protection validation | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
| SR88 | sim.json SR88 scenarios and input protection validation | 注册与本地引用；原厂独立审阅待补（若涉及器件） |
