"""S9 requirement/test/closure contract. No changes to approved numeric decisions."""
import copy


ISSUE_ACTIONS = {
    'SR35': ('B/C', '原理图/PCB 冻结及瞬态测试前', '核对全温钳位曲线、脉冲能量、布局过冲和电容耐压；不足时调整保护/耐压方案并回算。'),
    'SR34': ('B/D', '故障试验及上电前', '验证独立输入限流的响应、释放能量、直通路径、磁饱和及器件 SOA；输出 OCP 不可替代电感峰值保护。'),
    'SR38': ('A/B/C', '实物额定纹波/温升验收前', '记录偏离内部纹波目标；若实物不满足纹波或温升则增大电感，升级版本并重算。'),
    'SR44': ('B/D', 'CC 精度验收前', '在实际 CC 共模范围进行标定；补齐 ADC、参考源、温漂、CMRR、残余误差并实测稳态精度。'),
    'SR46': ('B/D', '模拟保护原理图冻结及上电前', '完成完整电压/电流误差预算、ADC/COMP 并用验证、独立 DAC 阈值及容差、BKIN 锁存路由；必要时分配独立引脚或外部比较器。'),
    'SR53': ('B/C', '插电及故障试验前', '实现并验证独立输入缓升与输入限流；记录输入斜率、输入电流和电感电流峰值，核对 PWM 禁止、电源释放能量与器件 SOA。'),
    'SR65': ('D', '动态/全负载验收前', '另行仿真和实测 DCM、空载、停脉冲、负载阶跃、CC/CV 切换、积分跟踪及恢复；考虑电子负载动态。'),
    'SR73': ('B/C/D', '布局冻结及系统验收前', '核对偏压/温度参数和热铜面积；测采样窗口、延迟、WCET；实际器件、滤波、平均算法或时序变化后重跑模型。'),
}

# 未在上表登记的 WARN/FAIL：仍以 open 项交接，由 A 指定责任方后补充到 ISSUE_ACTIONS
DEFAULT_ACTION = ('A', '交接前', '新出现的检查项：由 A 分析原因，指定责任模块与关闭方法后更新 ISSUE_ACTIONS 并重新导出。')


def issues(check):
    out = []
    for c in check['checks']:
        if c['verdict'] == 'PASS':
            continue
        owner, gate, closure = ISSUE_ACTIONS.get(c['id'], DEFAULT_ACTION)
        out.append(dict(id=c['id'], status='open', verdict=c['verdict'], rule=c['rule'],
                        evidence=c['detail'], owner=owner, required_before=gate,
                        closure=closure, closure_evidence=None))
    return out


def requirement_rows(req):
    rows = [
        ('REQ-IN', '输入电压及容差', 'must', 'vin'),
        ('REQ-CV', '恒压设定及稳态误差', 'must', 'vout'),
        ('REQ-CC', '恒流设定及稳态误差；限声明的可调区', 'must', 'iout'),
        ('REQ-PWR', '额定功率与连续运行', 'must', 'pout'),
        ('REQ-FSW', '开关频率范围', 'must', 'fsw.range_hz'),
        ('TGT-FSW', '建议开关频率', 'target', 'fsw.suggested_hz'),
        ('REQ-RIPPLE', '输出纹波基本上限', 'must', 'ripple.max_pct'),
        ('TGT-RIPPLE', '输出纹波建议目标', 'target', 'ripple.target_pct'),
        ('REQ-MCU', '使用 32 位 MCU', 'must', 'controller.type'),
        ('TGT-MCU', '优先 STM32', 'target', 'controller.preferred'),
        ('REQ-CTRL', 'MCU 数字 PI CC/CV', 'must', 'control'),
        ('REQ-PROT', 'OVP/OCP/占空比限幅', 'must', 'protection.required'),
        ('TGT-START', '建议软启动', 'target', 'protection.suggested'),
        ('REQ-ETA-MEAS', '效率必须实测', 'must', 'efficiency.measure'),
        ('TGT-ETA', '额定效率建议目标', 'target', 'efficiency.target_pct'),
        ('REQ-LOAD', '电子负载支持 CC/CR；CV 用 CC，CC 用 CR', 'must', 'test_load'),
        ('REQ-PCB', '两层 PCB，功率级与控制级布局合理', 'must', 'pcb'),
        ('REQ-BUILD', '制作实物并手工焊接', 'project_constraint', 'project_constraints.build_physical'),
        ('REQ-EDA', '使用 KiCad（原理图 + PCB）', 'project_constraint', 'project_constraints.eda'),
        ('REQ-SOURCE', '立创采购且可追溯', 'project_constraint', 'project_constraints.sourcing'),
    ]
    result = []
    for rid, text, level, path in rows:
        value = req
        for key in path.split('.'):
            value = value[key]
        origin = req[path.split('.')[0]].get('origin')
        result.append(dict(id=rid, text=text, level=level, source_field=path,
                           source_value=value, origin=origin))
    return result


def verification_plan(req, calc):
    v, i, cc, pr = calc['inputs']['vout_v'], calc['inputs']['iout_cc_a'], calc['cc_range'], calc['protection']
    vins = list(calc['inputs']['vin'].values())
    currents = [i * x for x in (0.2, 0.5, 0.9)]
    rs = [cc['r_min_ohm'], (cc['r_min_ohm'] + cc['r_max_ohm']) / 2, cc['r_max_ohm']]
    def t(tid, ids, title, condition, criterion, prerequisite='', **extra):
        return dict(id=tid, req=ids[0], requirements=ids, item=title, condition=condition,
                    criterion=criterion, status='计划，未实测', prerequisite=prerequisite, **extra)
    tests = [
        t('T01', ['REQ-CV', 'REQ-IN', 'REQ-LOAD'], 'CV 稳态精度',
          f'输入 {vins} V；电子负载 CC {currents} A；端子处用校准外部仪表测压',
          f'各 CV 稳态点 |Vout−{v:g}|/{v:g} ≤ {req["vout"]["steady_err_pct"]}%；轻载 DCM 点另验，不能引用 CCM 裕度作为证明',
          'SR34/SR46/SR53 对应上电条件已关闭，输入独立限流', load_mode='CC', input_v=vins, load_a=currents),
        t('T02', ['REQ-CC', 'REQ-IN', 'REQ-LOAD'], 'CC 稳态精度及边界',
          f'输入 {vins} V；电子负载 CR {rs} Ω；输出规划 {cc["vo_min_v"]:g}–{v:g} V；上端是 CC/CV 交界，向区内邻点复核',
          f'可调 CC 区 |Iout−{i:g}|/{i:g} ≤ {req["iout"]["steady_err_pct"]}%；低于电压/阻值下限属于不可控区，不保证恒流',
          '输入/保护实现条件关闭；完成电流标定和完整误差预算', load_mode='CR', input_v=vins, load_ohm=rs),
        t('T03', ['REQ-CTRL', 'REQ-CC', 'REQ-CV'], '模式切换与非线性动态',
          '电子负载 CR 从 CV 区跨过 CC/CV 交界到可调 CC 区，正反向阶跃；另测空载/DCM/停脉冲及饱和恢复',
          '记录过冲、恢复时间、主导环和积分跟踪；稳态回到相应误差范围；动态量化判据由 B/D 联调前另行确定，课程未给过冲限值',
          'SR65 待仿真和实测关闭'),
        t('T04', ['REQ-RIPPLE', 'TGT-RIPPLE'], '输出纹波',
          '额定及各声明工况；示波器短地弹簧，计划 20 MHz 限带；同时保留全带宽尖峰记录',
          f'Vpp/Vout ≤ {req["ripple"]["max_pct"]}% 为基本要求；≤ {req["ripple"]["target_pct"]}% 为建议目标'),
        t('T05', ['REQ-ETA-MEAS', 'TGT-ETA'], '效率实测',
          '额定及轻载同步测 Vin/Iin/Vout/Iout，电压在输入/输出端子测量；记录仪器误差和系统边界',
          f'必须提交实测效率；额定 ≥ {req["efficiency"]["target_pct"]}% 为建议目标，不以估算替代实测'),
        t('T06', ['REQ-PROT'], '软硬件 OVP 与锁存',
          f'在受控、限能平台独立注入软件/硬件采样信号；名义 SW/HW 阈值 {pr["ovp_sw"]["value"]:g}/{pr["ovp_hw"]["value"]:g} V',
          '软件超限封锁并锁存；停止控制程序时硬件仍能刹车并保持关断；阈值容差和延迟先预算再测量；故障解除不自动重启',
          'SR35/SR46 关闭；先验证保护通道，再考虑功率瞬态；不以直接断开反馈起步'),
        t('T07', ['REQ-PROT'], '输出 OCP 与输入故障保护',
          f'受控限能信号注入分别测试输出 SW {pr["ocp_sw"]["value"]:g} A/{pr["ocp_sw"]["delay_ms"]} ms 与 HW {pr["ocp_hw"]["value"]:g} A；独立输入保护另测',
          '验证动作、延迟和锁存；输出 OCP 不等于电感峰值限流，PWM 关闭不隔离输出；不把不可控区或短路当作 CC 合格点',
          'SR34/SR46/SR53 关闭；故障能源和 SOA 有依据后才安排功率故障测试'),
        t('T08', ['REQ-PROT'], '占空比限幅与默认关断',
          '审查限幅代码、定时器寄存器；用波形测复位/初始化/饱和/故障时 PWM，禁止用日志代替波形',
          f'实际 D∈[{pr["duty_limit"]["dmin"]:g},{pr["duty_limit"]["dmax"]:g}]，CCR≤{pr["duty_limit"]["ccr_max"]}；硬件刹车锁存，自动输出重使能关闭'),
        t('T09', ['TGT-START', 'REQ-PROT'], '软启动、UVLO 与显式恢复',
          f'从当前实测输出施加 {pr["soft_start"]["ramp_ms"]} ms 参考斜坡；输入跨越 UVLO {pr["uvlo"]["off_v"]:g}/{pr["uvlo"]["on_v"]:g} V',
          '记录参考/输出/输入电流；UVLO 恢复只解除禁止，需显式复位、自检、启动；插电直通浪涌另验，不能归入数字软启动保证'),
        t('T10', ['REQ-IN', 'REQ-CV'], '输入扰动',
          f'在输入范围 {min(vins):g}–{max(vins):g} V 内阶跃，电子负载 CC 保持 CV 工况',
          f'稳态恢复至输出 ±{req["vout"]["steady_err_pct"]}%；记录动态而不虚构课程恢复时间指标'),
        t('T11', ['REQ-PWR', 'REQ-BUILD'], '额定连续运行与温升',
          f'{v:g} V/{i:g} A 额定运行，记录环境、持续时长、温度及漂移；时长/环境/重复次数由下游开测前明确',
          '额定稳定运行，误差满足需求；各器件温度按有条件手册额定及设计降额核验；不虚设课程温度/时长标准'),
        t('T12', ['REQ-FSW', 'TGT-FSW', 'REQ-MCU', 'TGT-MCU', 'REQ-CTRL'], '控制与时序资源',
          '核对 MCU 手册和工程；测 PWM/SW 频率、ADC 窗口/帧新鲜度/截止时间、控制中断与最坏执行时间',
          f'采用所选 32 位 STM32 和数字 PI；频率在 {req["fsw"]["range_hz"]} Hz，设计值 {calc["inputs"]["fsw_hz"]} Hz；实现延迟与复核模型一致'),
        t('T13', ['REQ-PCB', 'REQ-EDA', 'REQ-BUILD', 'REQ-SOURCE'], '工程与装配审查',
          'KiCad 工程/ERC（符号/封装按 components[].kicad）；两层 PCB/DRC、功率环路/Kelvin/热铜审查；采购证据和手工焊接装配检查',
          '工程可打开、连接和布局规则通过；完整装配 BOM 的每个器件均有可追溯选型依据；当前主要器件 BOM 不能代替完整装配 BOM'),
        t('T14', ['REQ-CC', 'REQ-PROT'], '不可控区识别',
          '在独立输入限流及保护条件关闭后受控接近 CC 下限，记录越界识别和故障处理，不直接短接输出',
          f'低于 {cc["vo_min_v"]:g} V 或 CR {cc["r_min_ohm"]:g} Ω 不声明恒流；未误标为 CC 精度通过'),
    ]
    ramp = pr.get("input_ramp")
    if ramp:
        tests.append(t(
            "T15", ["REQ-IN", "REQ-PROT"], "独立输入缓升和冷插电电流",
            "在独立限能平台冷启动，记录保护器件后端 Vin、源电流、电感电流及 PWM；核对输入电容和开关 SOA",
            f"实测输入斜率 ≤ {ramp['max_slew_v_per_ms']:g} V/ms、源峰值 ≤ {ramp['source_peak_budget_a']:g} A，"
            "电感峰值低于适用 Isat，输入就绪前 PWM 保持关断",
            "完成缓升硬件、输入限流与实物接线审查；SR88 是仿真证据",
        ))
    return tests


def traceability(reqs, blocks, tests, check):
    # Explicit crosswalk: historical SDRC refs are not renamed or mistaken for measurements.
    refs = {
        'REQ-IN': ['PWR-AUX', 'PROT'], 'REQ-CV': ['REQ-ACC', 'IF-ADC', 'CTRL'],
        'REQ-CC': ['REQ-CC', 'REQ-ACC', 'IF-ADC', 'CTRL'], 'REQ-PWR': ['REQ-PWR', 'PWR', 'THERM'],
        'REQ-FSW': ['REQ-FSW', 'FW'], 'TGT-FSW': ['REQ-FSW'],
        'REQ-RIPPLE': ['REQ-RIP'], 'TGT-RIPPLE': ['REQ-RIP'],
        'REQ-MCU': ['REQ-MCU'], 'TGT-MCU': ['REQ-MCU'],
        'REQ-CTRL': ['CTRL', 'FW', 'CTRL-DUTY'], 'REQ-PROT': ['REQ-PROT', 'PROT', 'CTRL-DUTY', 'IF-ADC'],
        'TGT-START': ['PROT'], 'REQ-ETA-MEAS': [], 'TGT-ETA': ['REQ-EFF', 'DEC-EFF'],
        'REQ-LOAD': [], 'REQ-PCB': [], 'REQ-BUILD': ['SEL'], 'REQ-EDA': ['REQ-EDA'], 'REQ-SOURCE': ['SOURCING'],
    }
    return [dict(req=r['id'], text=r['text'], level=r['level'], source_field=r['source_field'],
                 blocks=[b['id'] for b in blocks if r['id'] in b['requirements']],
                 tests=[t['id'] for t in tests if r['id'] in t['requirements']],
                 checks=[c['id'] for c in check['checks'] if c.get('ref') in refs[r['id']]],
                 check_ref_aliases=refs[r['id']],
                 verification_note='SDRC 为计算/资料检查；未覆盖部分按测试或工程审查计划验证，均未实测') for r in reqs]


def proposed_pin_plan(mcu):
    pins = copy.deepcopy(mcu.get('pin_plan', {}))
    pins['_status'] = 'tentative_unverified_not_for_schematic_freeze'
    pins['_source'] = '器件库资源提案；B/D 必须核对 DS/RM/CubeMX、封装引脚和并用条件后冻结'
    for name in ('VOUT_SENSE', 'IOUT_SENSE'):
        if name in pins:
            pins[name]['note'] = '仅候选资源；ADC/COMP 并用、独立阈值 DAC 及 TIM1 刹车路径未核实，见 SR46'
    pins['unverified'] = ['ADC/COMP 同时使用及输入滤波/快速保护分支', '两路不同硬件阈值所需独立 DAC 通道、参考与容差',
                          'COMP→TIM1 break 路由及锁存，关闭自动重使能', '封装所有电源/复位/BOOT 和引脚复用需逐脚复核']
    return pins
