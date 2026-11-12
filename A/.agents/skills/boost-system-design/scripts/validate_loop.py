"""阶段 7c — 独立复核：用导出（四舍五入后）的 PI 系数，在全部 CCM 工况与容差场景上做密集频率扫描，
并校验 DC 工作点残差与损耗求和一致性。任一项不满足即报错（assert）。

输出 loop_validation.json。用法: python validate_loop.py <workdir>
"""
import argparse
import math
from pathlib import Path
from _common import KNOWLEDGE_DIR, load_json, save_json, param
from stage_params import variants, selected
from boost_model import Boost, logspace
from loop_design import corners, ctrl_resp, margins

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('workdir')
    ap.add_argument('--db', default=str(KNOWLEDGE_DIR/'parts_db.json')); a=ap.parse_args()
    w=Path(a.workdir)
    ch,ca,se,lo,lp=(load_json(w/n) for n in ('design_choices.json','calc.json','selection.json','losses.json','loop.json'))
    db=load_json(a.db)
    assert all(x['revision']==ch['revision'] for x in (ca,se,lo,lp))
    Tc=1/ch['control']['f_ctrl_hz']; lc=ch['losses']
    csa,_=selected(se,db,'csa'); rs=ca['sensing']['iout']['shunt_ohm']
    fs=logspace(.01,.999*ch['control']['f_ctrl_hz']/2,1800)
    results={}; max_res=0.; checked_dc=0
    for mode,cs in zip(('CV','CC'),corners(ca)):
        poles=ca['sensing']['vout']['filter_fc_hz'] if mode=='CV' else [1/(2*math.pi*lc['current_filter_r_ohm']*lc['current_filter_c_f']),param(csa,'bandwidth_khz',('typ','max'))*1000]
        kp,ki=lp[mode]['Kp'],lp[mode]['Ki']
        ki_tc=lp[mode]['Ki_Tc']
        assert math.isclose(ki_tc,ki*Tc,rel_tol=1e-7)
        dyn=[(kp+ki_tc/(1-z))*dz for z,dz in (ctrl_resp(f,Tc,poles) for f in fs)]
        vals=[]; excluded=[]
        for name,m,mi in variants(se,db,ca,ch):
            for c in cs:
                io=c['I0']+(c['vo']/c['Rac'] if c['Rac'] else 0)
                vb=c['vo']+io*rs; rac=c['Rac']+rs if c['Rac'] else None
                dc=m.solve_dc(c['vin'],vb,io)
                assert dc is not None, (name,c)
                d,il=dc; max_res=max(max_res,abs(m.dc_residual(d,c['vin'],vb,io))); checked_dc+=1
                di=(c['vin']-il*(m.dcr+m.rds))*d/(ca['inputs']['fsw_hz']*m.L)
                if il-di/2<=0:
                    excluded.append({'variant':name,'vin_key':c['vin_key'],'load':c['load']}); continue
                assert 0<=d<ca['protection']['duty_limit']['dmax']
                lin=m.linearize(c['vin'],d,il,vb,c['I0'],rac)
                scale=1/rac if mode=='CC' else (c['Rac']/rac if rac else 1.)
                ms=margins(fs,[dg*Boost.tf(lin,f)*scale for f,dg in zip(fs,dyn)])
                assert ms is not None, (mode,name,c,'invalid crossing')
                fc,pm,gm=ms
                assert pm>=ch['control']['pm_min_deg'] and gm>=ch['control']['gm_min_db'],(mode,name,c,ms)
                vals.append({'variant':name,'vin_key':c['vin_key'],'load':c['load'],'fc_Hz':fc,'PM_deg':pm,'GM_dB':gm})
        assert len(vals)==len(lp[mode]['corners']) and len(excluded)==len(lp[mode]['excluded_corners'])
        results[mode]={'checked_ccm':len(vals),'excluded_dcm':len(excluded),'fc_min_Hz':min(x['fc_Hz'] for x in vals),
                       'PM_min_deg':min(x['PM_deg'] for x in vals),'GM_min_dB':min(x['GM_dB'] for x in vals),
                       'worst_pm':min(vals,key=lambda x:x['PM_deg']),'corners':vals}
        print(mode, {k:v for k,v in results[mode].items() if k!='corners'},flush=True)
    loss_res=max(abs(sum(c['losses_W'].values())-c['p_total_W']) for c in lo['corner_cases'])
    assert loss_res<.002
    assert max_res<1e-9
    out={'schema':'boost-s7-validation/1','revision':ch['revision'],'ok':True,'frequency_points':len(fs),
         'frequency_range_hz':[fs[0],fs[-1]],'dc_points':checked_dc,'dc_max_residual_v':max_res,
         'loss_rounding_max_residual_w':loss_res,'loops':results,
         'scope':'Exported PI coefficients; same CCM averaged plant; dense sweep through 0.999 Nyquist. No DCM/nonlinear/hardware proof.'}
    save_json(w/'loop_validation.json',out)
    print('loop_validation.json: PASS')

if __name__=='__main__': main()
