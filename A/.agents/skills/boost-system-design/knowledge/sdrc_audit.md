# T4 SDRC 实现与证据审计

日期：2026-11-12。审阅者：Codex。对象：当前 r6 的规则元数据、检查表达式、知识库与所选器件证据。用户确认尚未采购、未更换器件，本次保持 BOM、器件库数值和设计决策。

本轮已下载原厂 PDF 与镜像规格书，针对关键表格、条件和脚注进行核对，不是全部 PDF 逐页审阅，也不是实物验证。URL、SHA-256、下载结果见 sdrc_evidence.json；下载成功不等于厂商、精确型号或所有参数已核实。下文页码为下载 PDF 的物理页。T4 继续保持 Draft。

## 发现与修正

1. SR70 原先以典型压差证明最坏供电，证据不足。ME6211C33 V22 镜像 PDF p9 的120 mV @100 mA、260 mV @200 mA 均为 typ，max 栏为空。现改为只接受明确的 dropout.max，并使用保险丝/反接 PMOS 后 VIN。缺项为 NOT_ASSESSED；未向真实数据库补造 max。
2. SR71 原先使用保险丝标称电阻。库中19 mΩ为 nominal，不能证明最大压降。SR70/SR71 均要求 PMOS/保险丝电阻 max，电流采用损耗工况包络，供电取路径计算和现有电压包络的较低者；无上下界标签的标量也不能关闭检查。温度系数、布线与瞬态仍为模型/实测边界。
3. SS54 C22452 的既有镜像封面为 MDD、内页为 NextGen，不能作为 MDD 原厂证据。商城链接后续下载未取得完整有效 PDF。SS54B 为不同后缀，不替代 SS54。保留器件与数值，明确来源不一致待补。
4. Sunlord 电感表的不同电流列对应不同判据。p13 的3.6 A对应电感下降20%，4.5 A对应下降30%；3.2 A对应温升20°C，4.0 A对应温升40°C，参考环境20°C。库内 min 是模型判据归一化，不是全温生产最低保证。
5. 已有 T4 修正继续保留：缺器件/参数/仿真不静默遗漏、不以零代替；未登记、重复、不完整元数据报错；表G按层级排序；CLI/run_all与直接导出执行门禁。

## 器件证据核对

| 器件、来源与已读范围 | 本轮发现和边界 |
|---|---|
| AO3400A，AOS官网 Rev3.1 July2023，p2表格已目视 | 30 V、32 mΩ max @4.5 V/5 A、26.5 mΩ max @10 V、Qg 7 nC max @4.5 V。热阻依赖规定铜面积，不能证明本板散热 |
| AO3401A，AOS官网 Rev3.1 Dec2023，p1–2文本 | −30 V、60 mΩ max @−4.5 V；稳态热阻及板条件需区别短时指标 |
| LM5114，TI官网 SNVS790F Nov2015，p5已目视、p6文本 | 工作4–12.6 V；UVLO开启3.25min/3.6typ/4max V；B版VIH 2.4 V。峰值驱动和延时的测试电压不能直接当成5 V最坏损耗保证 |
| ME6211C33，iceasy镜像V22，PDF p9已目视，页脚Page9of9 | 压差只有typ，max为空。Microne产品页已取得，未解析出可核对原厂PDF；SR70未闭合 |
| MWSA1206S-680MT，DigiKey镜像，p13表格及脚注已目视 | 68µH±20%、DCR140mΩmax；电流下降量/温升判据见上文；全温饱和和本板温升未证明 |
| INA180A2，TI官网 SBOS741H July2022，p8文本 | 增益50、带宽210kHz为typ；12 V共模Vos500µVmax，0 V时150µV。增益误差有输出摆幅条件，SR44仍是部分预算 |
| PA2512FKE7W0R05E，Yageo官网V10 Jan31 2018，p3–5文本 | 系列功率、容差及70°C条件已读；精确订货码仍需结合编码页逐项核对 |
| CL31B106KAHNNNE，Samsung/DigiKey镜像，p1–2文本 | 10µF/25V/X7R/±10%；参考日期March1 2013。偏压保持率0.3仍为假设 |
| CL31A226KOHNNNE，LCSC镜像，p1文本 | 22µF/16V/X5R/±10%；偏压下有效容量仍需条件曲线 |
| 25SVPF100M，Panasonic官网产品页 | 页面已取得，完整规格表未解析；ESR/纹波/温度条件未独立闭合 |
| EEEFK1C101P，Octopart镜像旧FK系列April2005，p1/p3文本 | 链接文件名为EEE-FK1H221P；精确选中料号行尚未核实，不凭系列名认定匹配 |
| SMAJ15A，LCSC镜像Littelfuse rev11/20/15，p2文本 | VRWM15V、VBR16.7–18.5V@1mA、VC24.4V@16.4A；指定脉冲不证明25V电容全条件保护。官网请求403 |
| 0466003.NRHF保险丝 | 镜像SSL验证失败、官网403，无有效独立PDF；19mΩ及0.576A²s为nominal，不是最大保证 |
| SS54 C22452，分销镜像/商城页 | 封面和内页厂商不一致；精确MDD料号及测试条件待补 |
| STM32G431CB DS12589 Rev6 Oct2021与RM0440，ST官网 | 两份资料已取得，未完成ADC/COMP/DAC/BKIN并用路由、精度和时序的联合审计 |
| 端子/SWD/UART排针，LCSC镜像 | PDF已取得，尺寸及厂商身份未完成核对，不能按下载成功关闭审计 |

## 逐规则与规则族审阅

已核对注册信息和代码表达式。数值PASS表示在输入数据和模型假设下表达式成立，不等于全部来源已独立核实。课程、数学和软件规则不需要虚构器件PDF来源。

| 注册ID/族 | 依据、审阅范围与未闭合项 |
|---|---|
| SR01/SR03/SR04-*/SR05 | 结构化课程需求中的功率、频率、保护、控制器；代码映射核对，非器件PDF项 |
| SR02 | design_rules §2拓扑适用条件；不能证明短路恒流 |
| SR10-*/SR11-* | 选型完整性、库约束、商城来源字段；来源存在性不等于真实性，SS54及连接器等仍待补 |
| SR20/SR21/SR22 | calc/losses工况、boost_model增益峰与占空比；数学/模型一致性，参数来源边界保留 |
| SR30/SR31/SR32 | TVS p2、AOS p2及电容额定值；指定条件比较，25SVPF精确规格待补 |
| SR33 | 课程要求与design_choices保护策略映射 |
| SR34/SR35 | 故障分析、TVS脉冲、T1输出OCP；仍WARN，输入限流、故障能量和SOA需硬件验证 |
| SR36/SR37 | Sunlord p13和电流包络；限20%电感下降/20°C温升判据，不是全温最低生产保证 |
| SR38 | 内部纹波目标与工况扫描；minor WARN，非课程硬限制 |
| SR39 | 电容纹波与损耗工况；25SVPF精确料号、频率、温度条件待补 |
| SR40/SR41/SR42/SR43/SR45 | calc采样范围、保护、课程指标；数学与需求检查，不替代完整误差证明 |
| SR44 | INA180 p8与分流容差；部分预算约4%，minor WARN，未含ADC/基准/温漂/完整CMRR |
| SR46 | MCU、放大器、阈值源与并用路由；WARN，完整误差和联合路由未审完 |
| SR50* | losses效率与课程门槛；二极管来源等缺口影响结论可信边界 |
| SR51-* | AOS热参数及板条件、losses热模型；实际铜面积和温升需验证 |
| SR52* | 纹波模型和课程指标；MLCC偏压保持率/ESR仍是假设 |
| SR53 | 冷插电及输入保护计划；WARN，未采购、无实测，不关闭 |
| SR54 | 保险丝nominal I²t与浪涌估算；不证明不误熔断，原厂资料及容差待补 |
| SR60-* | loop/loop_validation的CCM独立复核；数学和软件证据，DCM转SR65 |
| SR61/SR62/SR63 | PWM分辨率、CPU/串口预算；最坏执行时间及硬件时序另见SR73 |
| SR64 | CC工作区与design_rules §1；非同步Boost不能只靠关PWM隔离输出短路 |
| SR65 | CCM排除点及T1轻载/切换；WARN，仿真不关闭实测项 |
| SR70 | ME6211 V22 p9与保护后供电；NOT_ASSESSED，缺dropout.max及路径电阻max；已修正typ/原始VIN误用 |
| SR71 | LM5114 p5、AO3401A p2及保险丝路径；NOT_ASSESSED，缺保险丝电阻max；已修正nominal误用 |
| SR72 | η假设与losses迭代一致性 |
| SR73 | 模型和实测计划；WARN，热/磁损/采样时序/偏压容量待验证 |
| SR79 | sim schema/revision与完整性；软件数据契约，异常不得通过 |
| SR8[0-5]*/SR86/SR87 | 仿真指标、平均模型交叉验证与独立输出OCP轨迹；聚合逻辑已核对，不关闭实测 |
| SR88 | r6未配置input_ramp，为N/A；SR53义务仍保留 |

## 状态定义与验收差异

任务书同时要求conditional_review只有simulation/measurement WARN，以及旧基线保持conditional_review；但SR38/SR44本来就是automatic/minor WARN。实现保留真实verify类型，允许minor/info自动建议进入conditional_review，并公开status_policy；automatic critical/major WARN阻止就绪。critical/major NOT_ASSESSED同样needs_rework，与退出码3一致。这是公开的实现取舍，需维护者审阅。

手册审计推翻了真实基线退出0的旧预期：缺保证值应退出3，不能为满足旧计数编造max。单元测试用明确的合成qualified_supply参数测试退出0成功路径，绝不将夹具写入真实器件库或称为r6合格。

## 验证与复现

- python -m unittest test_tools test_sim_switching test_sdrc：37项通过，其中T4 15项；覆盖缺参数、元数据错误、0/2/3退出码、直接导出门禁、run_all传播、保护后供电和typ/标量不得替代保证值。
- 新目录A/outputs/test_t4_audit_r6复用test_t1_r6的8份上游JSON，再运行check_design：85 PASS / 8 WARN / 0 FAIL / 2 NOT_ASSESSED / 1 N/A，needs_rework、退出3。随后直接导出被阻止，未生成design_spec/BOM/header；未覆盖outputs/r6。
- 相对旧87 PASS，恰有SR70/SR71从PASS改为NOT_ASSESSED；不是两个已证实的电路FAIL，其余计数不变。
- 复用真实T1的58个仿真场景（54 PASS、4 WARN），本次未重跑开关仿真；不改功率/损耗/环路模型、器件库数值或设计revision。

后续先补LDO最大压差、保险丝电阻max/容差与I²t、MDD SS54原厂资料、Panasonic精确料号、连接器及分流订货码、MCU并用路由。只有typ的资料不能关闭全温生产保证；如无法补证应保留未闭合并通过可追溯设计修订处理。当前无需用户先采购，也没有证据要求立即更换器件。实物获得后再按SR34/35/46/53/65/73计划验证。
