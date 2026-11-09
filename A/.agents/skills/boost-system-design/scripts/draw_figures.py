"""绘制报告用系统框图与控制框图（SVG，坐标布局，数据取自 design_spec.json，保证与 BOM 一致）.

输出：system_block.svg（系统框图：能量流/信号流/保护流分色）、control_block.svg（CC/CV 控制框图）、
vi_region.svg（CC/CV 输出工作区）。
可选 --png：若本机有 Chrome/Edge，用无头浏览器渲染 2× PNG（供 Word 报告插图）。

用法: python draw_figures.py <workdir> [--png]
"""

import argparse
import shutil
import subprocess
from pathlib import Path

from xml.sax.saxutils import escape as esc

from _common import load_json

FONT = "Microsoft YaHei, SimHei, Arial"
STYLE = {  # 线型：颜色, 线宽, 虚线
    "power": ("#b91c1c", 4.0, None),
    "aux": ("#15803d", 2.4, None),
    "sig": ("#1d4ed8", 1.8, None),
    "prot": ("#ea580c", 2.2, "7,5"),
    "pin": ("#1d4ed8", 1.6, "4,4"),
}


class Svg:
    def __init__(self, w, h, title):
        self.w, self.h, self.out = w, h, []
        defs = "".join(
            f'<marker id="m-{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="{7 if k != "power" else 4.5}" '
            f'markerHeight="{7 if k != "power" else 4.5}" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>' for k, (c, _, _) in STYLE.items())
        self.out.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
                        f'font-family="{FONT}"><defs>{defs}</defs><rect width="{w}" height="{h}" fill="white"/>')
        if title:
            self.out.append(f'<text x="{w/2}" y="28" text-anchor="middle" font-size="20" font-weight="bold">{esc(title)}</text>')

    def group(self, x, y, w, h, label, color="#64748b"):
        self.out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="#f8fafc" stroke="{color}" '
                        f'stroke-width="1.4" stroke-dasharray="6,4"/>')
        self.out.append(f'<text x="{x+10}" y="{y+19}" font-size="13" fill="{color}" font-weight="bold">{esc(label)}</text>')

    def box(self, x, y, w, h, title, sub="", fill="#ffffff", stroke="#334155", tag=""):
        self.out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>')
        cy = y + h / 2 + (-7 if sub else 5)
        self.out.append(f'<text x="{x+w/2}" y="{cy}" text-anchor="middle" font-size="15" font-weight="bold">{esc(title)}</text>')
        if sub:
            self.out.append(f'<text x="{x+w/2}" y="{cy+19}" text-anchor="middle" font-size="12.5" fill="#334155">{esc(sub)}</text>')
        if tag:
            self.out.append(f'<text x="{x+w-5}" y="{y+13}" text-anchor="end" font-size="10" fill="#64748b">{esc(tag)}</text>')

    def circle(self, cx, cy, r, label):
        self.out.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="white" stroke="#334155" stroke-width="1.6"/>')
        self.out.append(f'<text x="{cx}" y="{cy+5}" text-anchor="middle" font-size="14" font-weight="bold">{esc(label)}</text>')

    def line(self, pts, kind="sig", label="", at=None, anchor="start", arrow=True):
        c, wdt, dash = STYLE[kind]
        d = " ".join(f"{x},{y}" for x, y in pts)
        self.out.append(f'<polyline points="{d}" fill="none" stroke="{c}" stroke-width="{wdt}" stroke-linejoin="round"'
                        + (f' stroke-dasharray="{dash}"' if dash else "")
                        + (f' marker-end="url(#m-{kind})"' if arrow else "") + "/>")
        if label:
            x, y = at if at else pts[0]
            self.out.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="12" fill="{c}" '
                            f'font-weight="bold" paint-order="stroke" stroke="white" stroke-width="4">{esc(label)}</text>')

    def text(self, x, y, s, size=12, color="#334155", anchor="start", bold=False):
        self.out.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" fill="{color}"'
                        + (' font-weight="bold"' if bold else "") + f">{esc(s)}</text>")

    def legend(self, x, y):
        items = [("power", "能量流"), ("aux", "辅助供电"), ("sig", "采样/控制信号"), ("prot", "硬件保护路径")]
        self.out.append(f'<rect x="{x}" y="{y}" width="200" height="{22*len(items)+14}" rx="6" fill="white" stroke="#cbd5e1"/>')
        for i, (k, s) in enumerate(items):
            yy = y + 20 + 22 * i
            self.line([(x + 12, yy), (x + 56, yy)], k)
            self.text(x + 66, yy + 4, s, 12.5)

    def save(self, path):
        Path(path).write_text("\n".join(self.out + ["</svg>"]), encoding="utf-8")


def comp(spec, role):
    c = next((c for c in spec["components"] if c["role"] == role), None)
    return (c["designator"], c["mpn"], c["value"]) if c else ("?", "?", "?")


def system_block(spec, path):
    s = Svg(1700, 990, "")
    mcu = comp(spec, "mcu")[1]
    s.box(20, 105, 110, 70, "实验电源", "5 V ±5%", fill="#f1f5f9")
    s.group(150, 60, 420, 160, "MOD-IN 输入接口与保护")
    s.box(165, 105, 80, 70, "J1", "输入端子")
    s.box(265, 105, 80, 70, "F1", "保险丝 3 A")
    s.box(365, 105, 85, 70, "Q2", comp(spec, "pmos")[1])
    s.box(470, 105, 88, 70, "Cin", "2×22+100 µF")
    s.group(590, 60, 520, 330, "MOD-PWR Boost 功率级 · 100 kHz")
    s.box(605, 105, 90, 70, "L1", comp(spec, "inductor")[2])
    s.circle(745, 140, 18, "SW")
    s.box(800, 105, 90, 70, "D1", comp(spec, "diode")[1])
    s.box(915, 105, 180, 70, "Cout", "100µF 固态 + 2×10µF")
    s.box(700, 250, 90, 70, "Q1", comp(spec, "mosfet")[1])
    s.text(745, 374, "PGND", 13, anchor="middle", bold=True)
    s.group(1130, 60, 340, 160, "MOD-SENSE / MOD-OUT")
    s.box(1145, 105, 90, 70, "R1", "50 mΩ 分流")
    s.box(1265, 105, 190, 70, "J2 / D2", "输出端子 / TVS " + comp(spec, "tvs")[1])
    s.box(1510, 105, 150, 70, "电子负载", "CC / CR 模式", fill="#f1f5f9")

    s.box(170, 425, 130, 70, "Vin 分压", "10k/10k + RC", tag="MOD-SENSE")
    s.box(330, 425, 140, 70, "U4 LDO", comp(spec, "ldo")[1].split("C33")[0] + " → 3.3 V", tag="MOD-AUX")
    s.box(640, 425, 150, 70, "U2 栅极驱动", comp(spec, "gate_driver")[1].split("MFX")[0], tag="MOD-DRV")
    s.box(1100, 425, 150, 70, "U3 电流放大", comp(spec, "csa")[1].split("IDBVR")[0] + " ×50", tag="MOD-SENSE")
    s.box(1290, 425, 150, 70, "Vout 分压", "43k/10k + RC", tag="MOD-SENSE")

    s.group(480, 600, 960, 310, f"MOD-MCU · U1 {mcu}", color="#7c3aed")
    s.box(510, 660, 190, 70, "TIM1_CH1 PWM", "PA8 · Dmax 0.80", fill="#faf5ff")
    s.box(760, 660, 220, 70, "CV/CC 双 PI", "最小值选择 · 软启动 · 状态机", fill="#faf5ff")
    s.box(1040, 660, 190, 70, "ADC1 / ADC2", "TIM1 触发 · 硬件过采样", fill="#faf5ff")
    s.box(510, 790, 190, 70, "TIM1 刹车", "硬件封锁 PWM", fill="#faf5ff")
    s.box(760, 790, 220, 70, "USART1", "日志 / 命令（PA9/PA10）", fill="#faf5ff")
    s.box(1040, 790, 190, 70, "COMP1 / COMP3", "+ 内部 DAC3 阈值", fill="#faf5ff")
    s.box(1480, 690, 190, 150, "", "", fill="#f1f5f9")
    s.text(1575, 715, "MOD-DBG 调试/人机", 14, "#0f172a", "middle", True)
    for i, t in enumerate(["SWD 排针 J3", "UART 排针 J4", "LED 运行/CC/故障", "按键 使能/清故障"]):
        s.text(1575, 745 + 23 * i, t, 12.5, anchor="middle")

    # 能量流
    P = [(130, 140), (165, 140)]
    s.line(P, "power")
    for a, b in ((245, 265), (345, 365), (450, 470), (558, 605), (695, 727), (763, 800), (890, 915), (1095, 1145),
                 (1235, 1265), (1455, 1510)):
        s.line([(a, 140), (b, 140)], "power")
    s.line([(745, 158), (745, 250)], "power")
    s.line([(745, 320), (745, 356)], "power", arrow=False)
    # 辅助供电
    s.line([(530, 175), (530, 460), (470, 460)], "aux", "VIN", (536, 300))
    s.line([(580, 140), (580, 400), (680, 400), (680, 425)], "aux", "VIN→VDD", (600, 420))
    s.line([(400, 495), (400, 750), (480, 750)], "aux", "+3V3", (406, 740))
    # 采样/控制
    s.line([(490, 175), (490, 405), (235, 405), (235, 425)], "sig")
    s.line([(235, 495), (235, 930), (1250, 930), (1250, 715), (1230, 715)], "sig", "VIN_SNS (PA2)", (250, 924))
    s.line([(1190, 175), (1190, 425)], "sig", "开尔文", (1196, 300))
    s.line([(1365, 175), (1365, 425)], "sig")
    s.line([(1175, 495), (1175, 660)], "sig", "IOUT_SNS (PA1)", (1181, 590))
    s.line([(1365, 495), (1365, 695), (1230, 695)], "sig", "VOUT_SNS (PA0)", (1371, 590))
    s.line([(1040, 695), (980, 695)], "sig")
    s.line([(760, 695), (700, 695)], "sig")
    s.line([(870, 730), (870, 790)], "sig")
    s.line([(605, 660), (605, 460), (640, 460)], "sig", "PWM_H", (611, 560))
    s.line([(770, 425), (770, 408), (840, 408), (840, 285), (790, 285)], "sig", "GATE", (846, 350))
    s.line([(1440, 760), (1480, 760)], "sig")
    s.line([(1135, 730), (1135, 790)], "pin", "同一引脚→比较器", (1141, 765))
    # 保护
    s.line([(1135, 860), (1135, 885), (605, 885), (605, 860)], "prot", "硬件 OVP 13.8 V / OCP 1.0 A", (860, 880), "middle")
    s.line([(605, 790), (605, 730)], "prot", "封锁", (611, 765))
    s.legend(20, 840)
    s.save(path)


def control_block(spec, path):
    c = spec["control"]
    s = Svg(1480, 520, "")
    cv, cc = c["cv_pi"], c["cc_pi"]
    s.box(20, 120, 170, 60, "Iref = 0.5 A", "恒流设定")
    s.circle(260, 150, 22, "Σ")
    s.box(320, 120, 190, 60, "CC-PI", f"Kp={cc['Kp']}, Ki={cc['Ki']}")
    s.box(20, 270, 170, 60, "Vref", "软启动斜坡 → 12 V")
    s.circle(260, 300, 22, "Σ")
    s.box(320, 270, 190, 60, "CV-PI", f"Kp={cv['Kp']}, Ki={cv['Ki']}")
    s.box(560, 185, 140, 80, "min(Di, Dv)", "最小值选择")
    s.box(750, 185, 190, 80, f"限幅 [{c['duty_limit'][0]}, {c['duty_limit'][1]}]", "非激活环积分钳位")
    s.box(990, 185, 150, 80, "TIM1 PWM", f"{c['f_pwm_hz']/1000:g} kHz")
    s.box(1190, 185, 170, 80, "Boost 功率级", "L1 · Q1 · D1 · Cout")
    s.box(1030, 20, 250, 60, "电流采样", "分流 + INA180 + RC + ADC")
    s.box(1030, 420, 250, 60, "电压采样", "分压 + RC + ADC")
    s.line([(190, 150), (238, 150)], "sig", "+", (222, 142))
    s.line([(190, 300), (238, 300)], "sig", "+", (222, 292))
    s.line([(282, 150), (320, 150)], "sig", "eI", (290, 142))
    s.line([(282, 300), (320, 300)], "sig", "eV", (290, 292))
    s.line([(510, 150), (535, 150), (535, 210), (560, 210)], "sig", "Di", (516, 142))
    s.line([(510, 300), (535, 300), (535, 240), (560, 240)], "sig", "Dv", (516, 318))
    s.line([(700, 225), (750, 225)], "sig")
    s.line([(940, 225), (990, 225)], "sig", "D", (958, 217))
    s.line([(1140, 225), (1190, 225)], "sig")
    s.line([(1360, 225), (1460, 225)], "power", "输出", (1366, 213))
    s.line([(1410, 225), (1410, 50), (1280, 50)], "sig", "Iout", (1416, 140))
    s.line([(1410, 225), (1410, 450), (1280, 450)], "sig", "Vout", (1416, 340))
    s.line([(1030, 50), (260, 50), (260, 128)], "sig", "Iout_meas", (600, 44), "middle")
    s.line([(1030, 450), (260, 450), (260, 322)], "sig", "Vout_meas", (600, 444), "middle")
    s.text(268, 128, "−", 16, "#1d4ed8", bold=True)
    s.text(268, 336, "−", 16, "#1d4ed8", bold=True)
    s.text(20, 505, f"控制频率 {c['f_ctrl_hz']/1000:g} kHz（TIM1 RCR={c['tim1_rcr']}），ADC 在导通中点触发；"
                    "Kp 单位 1/V、1/A，Ki 单位 1/(V·s)、1/(A·s)；PI 输出为占空比", 13)
    s.save(path)


def vi_region(spec, path):
    """CC/CV 输出工作区（V-I 平面）."""
    cc = spec["cc_range"]
    vo = 12.0
    io = 0.5
    s = Svg(900, 470, "")
    x0, y0, xs, ys = 90, 410, 820, 26  # 原点像素, A→px, V→px
    X = lambda i: x0 + i * xs
    Y = lambda v: y0 - v * ys
    s.out.append(f'<rect x="{X(0)}" y="{Y(vo)}" width="{X(io)-X(0)}" height="{Y(0)-Y(vo)}" fill="#dbeafe" opacity="0.55"/>')
    s.out.append(f'<rect x="{X(io)-6}" y="{Y(vo)}" width="12" height="{Y(cc["vo_min_v"])-Y(vo)}" fill="#fde68a"/>')
    s.out.append(f'<rect x="{X(io)-6}" y="{Y(cc["vo_min_v"])}" width="12" height="{Y(0)-Y(cc["vo_min_v"])}" fill="#fecaca"/>')
    s.line([(X(0), Y(0)), (X(0.62), Y(0))], "sig", "", arrow=True)
    s.line([(X(0), Y(0)), (X(0), Y(14))], "sig", "", arrow=True)
    s.text(X(0.62), Y(0) + 22, "Iout / A", 13, anchor="end")
    s.text(X(0) - 10, Y(14) + 4, "Vout / V", 13, anchor="end")
    for i in (0.1, 0.2, 0.3, 0.4, 0.5):
        s.text(X(i), Y(0) + 18, f"{i:g}", 12, anchor="middle")
    for v in (2, 4, 6, 8, 10, 12):
        s.text(X(0) - 8, Y(v) + 4, str(v), 12, anchor="end")
    s.line([(X(0), Y(vo)), (X(io), Y(vo)), (X(io), Y(cc["vo_min_v"]))], "power", arrow=False)
    s.out.append(f'<line x1="{X(io)}" y1="{Y(cc["vo_min_v"])}" x2="{X(io)}" y2="{Y(0)}" stroke="#b91c1c" stroke-width="3" stroke-dasharray="6,5"/>')
    s.text(X(0.22), Y(vo) - 10, "CV 区：Vout = 12 V（R ≥ 24 Ω）", 14, "#1e3a8a", "middle", True)
    s.text(X(io) + 14, Y(9.3), "CC 区：Iout = 0.5 A", 14, "#92400e", bold=True)
    s.text(X(io) + 14, Y(9.3) + 20, f"CR {cc['r_min_ohm']:g}–{cc['r_max_ohm']:g} Ω", 13, "#92400e")
    s.text(X(io) + 14, Y(3.4), f"Vout < {cc['vo_min_v']:g} V：不可控区", 14, "#991b1b", bold=True)
    s.text(X(io) + 14, Y(3.4) + 20, "输入经 L、D 直通，靠保险丝/电源限流", 13, "#991b1b")
    s.out.append(f'<circle cx="{X(io)}" cy="{Y(vo)}" r="6" fill="#b91c1c"/>')
    s.text(X(io) - 8, Y(vo) + 22, "额定点 12 V/0.5 A（24 Ω）", 12.5, "#0f172a", "end")
    s.save(path)


def to_png(svg, png, w, h):
    for exe in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                shutil.which("chrome"), shutil.which("msedge"), shutil.which("google-chrome"), shutil.which("chromium")):
        if exe and Path(exe).exists():
            subprocess.run([exe, "--headless", "--disable-gpu", "--hide-scrollbars", f"--screenshot={png}",
                            f"--window-size={w},{h}", "--force-device-scale-factor=2", Path(svg).resolve().as_uri()],
                           capture_output=True, timeout=120)
            return Path(png).exists()
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir")
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()
    w = Path(a.workdir)
    spec = load_json(w / "design_spec.json")
    system_block(spec, w / "system_block.svg")
    control_block(spec, w / "control_block.svg")
    vi_region(spec, w / "vi_region.svg")
    print("system_block.svg / control_block.svg / vi_region.svg written")
    if a.png:
        for name, (W, H) in (("system_block", (1700, 990)), ("control_block", (1480, 520)), ("vi_region", (900, 470))):
            ok = to_png(w / f"{name}.svg", w / f"{name}.png", W, H)
            print(f"{name}.png", "written" if ok else "skipped（未找到 Chrome/Edge）")


if __name__ == "__main__":
    main()
