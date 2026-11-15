# T3 计算依据账本与 r6 适配

> 下文数值记录 T3 独立分支当时的验证，先于 T4 手册界限审计。整合后真实 r6 的 SR70/SR71 为 NOT_ASSESSED、状态 needs_rework，不能沿用下文 87 PASS 作为当前交接结论。最新整体验证见 [A-agent-validation.md](A-agent-validation.md)。

此改动迁移已有 T3 成果到 r6，保留 PWM_SW、VOUT_SENSE、IOUT_SENSE、唯一 GND 网络及 net_naming 字段。新增来源记录与检查，不调整电源参数或器件选择。

## 使用方式

在 skill 的 scripts 目录执行，工作目录建议使用独立的试验目录。

```text
python run_all.py <workdir>
python ledger.py <workdir>
python ledger.py <workdir> --stale L_MWSA1206S_680MT
python ledger.py <workdir> --stale ao3400a
python ledger.py <workdir> --stale inductor_ripple_ratio
python check_consistency.py <workdir>
```

账本输出 ledger.json/ledger.md，记录需求、设计决策、器件参数（含页码与条件）、前序计算和跨阶段依赖。来源不存在、输入记录过期或 revision 不一致会报错。已有旧 calc/loss/loop 文件不含来源记录，需要重跑对应阶段。

报告正文可在 system_design_report.template.md 中使用 `{{calc:L2}}`、`{{loss:envelope.eta_min}}` 等占位符；run_all 会保留源文件、渲染报告，再核对最终交接文件。来源追踪不等于独立验证公式本身，也不等于器件手册重新审计。依赖图中的规则/交接关系是显式维护的映射，新规则与交接字段加入时应同步维护。

## 验证范围

test_ledger 覆盖输入来源解析、缺记录/断引用拒绝，以及电感、MOSFET、纹波目标三个变更传播例子。test_consistency 覆盖生成结果、BOM 立创编号篡改、CV_KP 篡改、报告占位符和 revision 漂移。

2026-11-12 验证：`test_tools test_sim_switching test_ledger test_consistency` 共 32 项通过；`run_all.py outputs/test_t3_r6` 完整运行到交接一致性通过。账本覆盖 80/80 步；仿真 58 工况（54 PASS、4 WARN），SDRC 87 PASS、8 WARN、0 FAIL，状态 conditional_review。环路独立复核 PASS。

与 T1/r6 基线比较，calc、selection、losses、loop、loop_validation 除新增 steps 来源记录外数据一致。电感变更影响 21 个计算步/36 条规则/43 个交接字段；MOSFET 为 21/37/43；纹波目标为 40/46/74。网络名、net_naming 和唯一 GND 约定保留。

上述结果属于计算和仿真证据；4 个仿真 WARN 是无独立输入缓升的冷插电对照。r6 未配置 input_ramp，不能据此宣称 SR88 或实物浪涌保护通过。T4 在独立 PR 中维护，本分支基于 T1，后续合并时需保留 T4 的退出码与导出门禁。
