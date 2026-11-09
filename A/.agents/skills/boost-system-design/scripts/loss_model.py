"""CCM 损耗、容差、纹波与被动上电浪涌复核模型（估计值，非保证；由 loss_thermal.py 调用）."""
import cmath
import math
from _common import Steps, param, r3
from stage_params import variants, selected


def ripple(d, pk, valley, fsw, caps, R, N=1024, nh=60):
    wave=[0. if k/N<d else pk-(pk-valley)*(k/N-d)/(1-d) for k in range(N)]
    coeff, rms2=[],[0.]*len(caps)
    for n in range(1,nh+1):
        inc=sum(v*cmath.exp(-2j*math.pi*n*k/N) for k,v in enumerate(wave))/N
        zc=[r+1/(2j*math.pi*n*fsw*C) for C,r in caps]
        vn=inc/(sum(1/z for z in zc)+1/R)
        coeff.append(vn)
        for j,z in enumerate(zc):
            rms2[j]+=2*abs(vn/z)**2
    out=[sum(2*(v*cmath.exp(2j*math.pi*n*k/N)).real for n,v in enumerate(coeff,1)) for k in range(0,N,4)]
    return max(out)-min(out),sum(i2*r for i2,(_,r) in zip(rms2,caps)),[math.sqrt(x) for x in rms2]


def run(calc,ch,sel,db):
    S=Steps(); lc=ch['losses']
    vo,io,fsw=(calc['inputs'][k] for k in ('vout_v','iout_cc_a','fsw_hz'))
    po,ta=vo*io,lc['ambient_c']
    parts={k:selected(sel,db,k)[0] for k in ('mosfet','diode','gate_driver','pmos','fuse','inductor')}
    defaults=[]
    def get(role,key,fallback,fields=('max','typ','min')):
        value=param(parts[role],key,fields)
        if value is None: defaults.append(key)
        return fallback if value is None else value
    qg,qgd,qgs=(get('mosfet',k,v) for k,v in (('qg_nc',8),('qgd_nc',2),('qgs_nc',1.3)))
    coss,rgint=get('mosfet','coss_pf',80),get('mosfet','rg_ohm',4.5)
    cj,iq=get('diode','cj_pf',500),get('gate_driver','iq_ma',1.9)
    isrc,isnk=get('gate_driver','i_source_a',1.3),get('gate_driver','i_sink_a',7.6)
    thermal={name:get(role,'rth_ja_cw',fallback) for name,role,fallback in (('MOSFET','mosfet',125),('二极管','diode',80),('防反接 PMOS','pmos',125))}
    rp=get('pmos','rds_on_mohm_vgs4v5',60)*1e-3*lc['rds_hot_factor']
    rf=get('fuse','r_mohm',19)*1e-3
    rs=calc['sensing']['iout']['shunt_ohm']; vb=vo+io*rs
    cases,all_cases,inrush_cases={ },[],[]
    models=list(variants(sel,db,calc,ch))
    for variant,model,mi in models:
        for key,vin in calc['inputs']['vin'].items():
            dc=model.solve_dc(vin,vb,io)
            if dc is None: raise ValueError(f'{variant}/{key}: rated point unreachable')
            d,il=dc
            di=(vin-il*(model.dcr+model.rds))*d/(fsw*model.L)
            pk,valley=il+di/2,il-di/2
            rms2=il*il+di*di/12
            vdrv=vin-il*(rp+rf); vsw=vb+model.vf0+model.rd*pk
            tr=(qgd+qgs/2)*1e-9*(vdrv/isrc+lc['gate_resistor_ohm']+rgint)/(vdrv-lc['miller_plateau_v'])
            tf=(qgd+qgs/2)*1e-9*(vdrv/isnk+lc['gate_resistor_ohm']+rgint)/lc['miller_plateau_v']
            vpp,pesr,icap=ripple(d,pk,valley,fsw,model.caps,vb/io)
            p={'MOSFET 导通':d*rms2*model.rds,
               'MOSFET 开关':.5*vsw*(valley*tr+pk*tf)*fsw*lc['switching_loss_factor'],
               'MOSFET Coss':.5*coss*1e-12*vsw**2*fsw,
               '二极管导通':model.vf0*io+model.rd*(1-d)*rms2,
               '二极管结电容':.5*cj*1e-12*vsw**2*fsw,
               '电感铜损':rms2*mi['inductor_dcr_mohm']*1e-3,
               '输出电容 ESR':pesr,'采样电阻':io*io*rs,
               '分压电阻':calc['sensing']['vout']['divider_loss_w']+vin**2/(calc['sensing']['vin']['r_top_ohm']+calc['sensing']['vin']['r_bot_ohm']),
               '防反接 PMOS':rms2*rp,'保险丝':rms2*rf,
               '栅极驱动':qg*1e-9*vdrv*fsw+vdrv*iq*1e-3,
               '3.3V 辅助电源':vin*calc['aux']['i_3v3_ma']*1e-3}
            p['电感磁损(估)']=lc['core_loss_frac_of_copper']*p['电感铜损']
            total=sum(p.values())
            tj={'MOSFET':ta+sum(p[k] for k in ('MOSFET 导通','MOSFET 开关','MOSFET Coss'))*thermal['MOSFET'],
                '二极管':ta+(p['二极管导通']+p['二极管结电容'])*thermal['二极管'],
                '防反接 PMOS':ta+p['防反接 PMOS']*thermal['防反接 PMOS']}
            c={'variant':variant,'factors':mi['factors'],'vin_key':key,'vin_v':vin,'D':r3(d),
               'IL_avg_A':r3(il),'IL_pk_A':r3(pk),'IL_valley_A':r3(valley),'IL_rms_A':r3(math.sqrt(rms2)),
               'dIL_A':r3(di),'ripple_ratio':r3(di/il),'VBOOST_v':r3(vb),'driver_vdd_v':r3(vdrv),
               't_rise_ns':r3(tr*1e9),'t_fall_ns':r3(tf*1e9),'losses_W':{n:r3(v) for n,v in p.items()},
               'p_total_W':r3(total),'eta_total':r3(po/(po+total)),
               'eta_power_stage':r3(po/(po+total-p['栅极驱动']-p['3.3V 辅助电源'])),
               'iin_avg_A':r3((po+total)/vin),'tj_C':{n:r3(v) for n,v in tj.items()},
               'vpp_harmonic_mv':r3(vpp*1e3),'ripple_pct':r3(vpp/vo*100),'capacitor_branch_rms_A':[r3(v) for v in icap]}
            all_cases.append(c)
            if variant=='nominal': cases[key]=c
        ctot=sum(c for c,_ in model.caps); z0=math.sqrt(model.L/ctot)
        rseries=model.dcr+model.rd; zeta=rseries/(2*z0)
        ve=calc['inputs']['vin']['max']-model.vf0
        if zeta<1:
            wd=math.sqrt(1-zeta*zeta)/math.sqrt(model.L*ctot)
            tpk=math.acos(zeta)/wd
            ipk=ve/z0*math.exp(-rseries/(2*model.L)*tpk)
        else: tpk,ipk=None,ve/rseries
        inrush_cases.append({'variant':variant,'z0_ohm':r3(z0),'zeta':r3(zeta),
                             'i_peak_a':r3(ipk),'i2t_a2s':r3(ve*ve*ctot/(2*rseries)),
                             't_peak_us':r3(tpk*1e6) if tpk else None})
    we=min(all_cases,key=lambda x:x['eta_total']); wr=max(all_cases,key=lambda x:x['ripple_pct'])
    wi=max(inrush_cases,key=lambda x:x['i_peak_a'])
    isat=param(parts['inductor'],'isat_a',('min','typ'))
    inrush={**wi,'i2t_a2s':max(x['i2t_a2s'] for x in inrush_cases),
            'fuse_melting_i2t_a2s':param(parts['fuse'],'melting_i2t_a2s'),'inductor_isat_a':isat,
            'linear_model_exceeds_isat':wi['i_peak_a']>isat,'mitigation_verified':False,
            'note':'线性 L/恒定二极管压降/零初始电压/无负载估计；超过 Isat 时不是可靠上界。外部电源限流瞬态及其输出电容、或 PMOS 缓启动 SOA 须验证。保险丝 I²t 为 nominal，非重复浪涌寿命保证。'}
    nom=cases['nom']
    for sid,name,val,unit,formula,note in (
        ('E1','额定点总损耗',nom['p_total_W'],'W','ΣP','见 cases.nom.losses_W'),
        ('E2','额定整机效率',100*nom['eta_total'],'%','Po/(Po+ΣP)','非实测'),
        ('E3','额定功率级效率',100*nom['eta_power_stage'],'%','Po/(Po+ΣP−Paux)',''),
        ('E4','容差场景最低效率',100*we['eta_total'],'%','min η',we['variant']),
        ('R2','容差场景最大纹波',wr['vpp_harmonic_mv'],'mV','二极管电流谐波×并联支路阻抗','60次谐波，未计ESL/布局尖峰'),
        ('R3','容差场景最大纹波比',wr['ripple_pct'],'%','Vpp/Vo',''),
        ('U1','被动充电浪涌线性估计',inrush['i_peak_a'],'A','(Vin−VF)/Z0·exp(−α·tpeak)','超过 Isat 时不是可靠上界'),
        ('U2','被动充电 I²t 线性估计',inrush['i2t_a2s'],'A²s','C·(Vin−VF)^2/(2R)','完整充电过程的能量积分')):
        S.add(sid,name,formula,'',val,unit,note)
    gain=[(*m.gain_peak(calc['inputs']['vin']['min'],vo/io+rs),name) for name,m,_ in models]
    gp=min(gain,key=lambda x:x[0]); mi=models[0][2]
    S.add('G1','最低增益峰占空比','max Vo(D)','含输入串联电阻',gp[0],'','线性寄生模型')
    return {'schema':'boost-losses/2','revision':ch['revision'],'model':mi,
            'assumptions':{**lc,'defaults_used_for_missing_data':sorted(set(defaults)),
                'limitations':['ESR 0.5/1/2倍为敏感性场景，非手册全温界限。',
                '驱动峰值电流仅有12V条件，开关损耗乘显式估计系数，非5V保证。',
                'MOS 热阻用稳态max，仅适用于手册铜面积；二极管热阻为typ。',
                '1.3倍热Rds、磁损比例、MLCC保持率为规划假设。',
                '输入路径按电感电流近似，忽略辅助支路对该路径的增量压降；未计走线、电感温升增阻、漏电全温最大值及电容漏电。']},
            'cases':cases,'corner_cases':all_cases,
            'envelope':{'eta_min':we['eta_total'],'eta_min_case':we['variant']+'/'+we['vin_key'],
                'IL_pk_max_A':max(x['IL_pk_A'] for x in all_cases),'IL_rms_max_A':max(x['IL_rms_A'] for x in all_cases),
                'ripple_ratio_max':max(x['ripple_ratio'] for x in all_cases),'driver_vdd_min_v':min(x['driver_vdd_v'] for x in all_cases),
                'D_max':max(x['D'] for x in all_cases),'tj_max_C':{n:max(x['tj_C'][n] for x in all_cases) for n in thermal}},
            'ripple':{'vpp_harmonic_mv':wr['vpp_harmonic_mv'],'ripple_pct':wr['ripple_pct'],'case':wr['variant']+'/'+wr['vin_key']},
            'gain_peak':{'d_peak':r3(gp[0]),'vo_peak_v':r3(gp[1]),'variant':gp[2]},
            'inrush':inrush,'inrush_cases':inrush_cases,
            'eta_consistency':{'eta_assumed':ch['power_stage']['eta_assumed'],'eta_model_nom':nom['eta_total'],
                'needs_iteration':abs(nom['eta_total']-ch['power_stage']['eta_assumed'])>.03,
                'advice':'偏差超过3个百分点时回 S5 迭代；保持 eta_worst 独立的保守预算。'},'steps':S.items}
