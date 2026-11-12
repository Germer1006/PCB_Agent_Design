# A 工具链需求变体回归

仅运行 S5/S6；不是 C0–C3 模型消融实验，不代表完整设计通过。

| 用例 | Vin→Vo / Iout | 难度 | 结果 | 缺件类别 | 行为验证 |
|---|---|---|---|---|---|
| easy01 | 4.5→9 V / 0.3 A | easy | selection_blocked | inductor | True |
| easy02 | 5→9 V / 0.3 A | easy | selection_blocked | inductor | True |
| easy03 | 5.5→9 V / 0.5 A | easy | selected | — | True |
| typical01 | 4.5→12 V / 0.5 A | typical | selected | — | True |
| typical02 | 5→12 V / 0.5 A | typical | selected | — | True |
| typical03 | 5.5→12 V / 0.5 A | typical | selected | — | True |
| typical04 | 5→12 V / 0.3 A | typical | selection_blocked | inductor | True |
| hard01 | 4.5→15 V / 0.8 A | hard | selection_blocked | mosfet, pmos, inductor, cout_bulk, cout_ceramic, tvs, fuse | True |
| hard02 | 5→15 V / 0.8 A | hard | selection_blocked | mosfet, pmos, cout_bulk, cout_ceramic, tvs, fuse | True |
| hard03 | 5.5→15 V / 0.8 A | hard | selection_blocked | mosfet, pmos, cout_bulk, cout_ceramic, tvs, fuse | True |
| hard04 | 5→9 V / 0.8 A | hard | selected | — | True |
| impossible01 | 5.5→5.5 V / 0.5 A | impossible | correct_rejection | — | True |

行为验证 12/12。缺件是当前库在固定默认策略下的边界，不能据此宣称规格物理不可实现。

S7–S9、环路/仿真指标、完整标准答案和真实模型对照需另行运行。所有失败及 stdout/stderr 留在各用例目录。
