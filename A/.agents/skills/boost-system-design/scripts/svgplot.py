"""Minimal dependency-free SVG Bode plot (log-frequency axis, magnitude + phase panels)."""

import math

COLORS = ["#2563eb", "#dc2626", "#059669", "#d97706"]


def bode_svg(series, title="", width=760, height=560, ref_lines=((0, -180),)):
    """series: list of dict(label, f, mag_db, phase_deg)."""
    ml, mr, mt, gap = 70, 20, 40, 40
    ph = (height - mt - gap - 50) / 2
    pw = width - ml - mr
    fmin = min(min(s["f"]) for s in series)
    fmax = max(max(s["f"]) for s in series)
    lmin, lmax = math.floor(math.log10(fmin)), math.ceil(math.log10(fmax))

    def rng(key, step):
        lo = min(min(s[key]) for s in series)
        hi = max(max(s[key]) for s in series)
        return math.floor(lo / step) * step, math.ceil(hi / step) * step

    mlo, mhi = rng("mag_db", 20)
    plo, phi = rng("phase_deg", 90)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'font-family="Microsoft YaHei, SimHei, Arial" font-size="12">',
           f'<rect width="{width}" height="{height}" fill="white"/>',
           f'<text x="{width/2}" y="22" text-anchor="middle" font-size="15">{title}</text>']

    def x(f):
        return ml + (math.log10(f) - lmin) / (lmax - lmin) * pw

    for panel, (key, lo, hi, step, ylab, ref) in enumerate(
            (("mag_db", mlo, mhi, 20, "幅值 / dB", ref_lines[0][0]),
             ("phase_deg", plo, phi, 90, "相位 / °", ref_lines[0][1]))):
        top = mt + panel * (ph + gap)

        def y(v, top=top, lo=lo, hi=hi):
            return top + (hi - v) / (hi - lo) * ph

        out.append(f'<rect x="{ml}" y="{top}" width="{pw}" height="{ph}" fill="none" stroke="#333"/>')
        for d in range(lmin, lmax + 1):
            for m in range(1, 10):
                f = m * 10 ** d
                if f > 10 ** lmax:
                    break
                xx = x(f)
                out.append(f'<line x1="{xx:.1f}" y1="{top}" x2="{xx:.1f}" y2="{top+ph}" stroke="#e5e7eb" '
                           f'stroke-width="{1 if m == 1 else 0.5}"/>')
            if panel == 1:
                out.append(f'<text x="{x(10**d):.1f}" y="{top+ph+16}" text-anchor="middle">{10**d:g}</text>')
        v = lo
        while v <= hi + 1e-9:
            out.append(f'<line x1="{ml}" y1="{y(v):.1f}" x2="{ml+pw}" y2="{y(v):.1f}" stroke="#e5e7eb"/>')
            out.append(f'<text x="{ml-6}" y="{y(v)+4:.1f}" text-anchor="end">{v:g}</text>')
            v += step
        if lo <= ref <= hi:
            out.append(f'<line x1="{ml}" y1="{y(ref):.1f}" x2="{ml+pw}" y2="{y(ref):.1f}" stroke="#111" '
                       f'stroke-dasharray="5,4"/>')
        out.append(f'<text x="18" y="{top+ph/2}" transform="rotate(-90 18 {top+ph/2})" text-anchor="middle">{ylab}</text>')
        for i, s in enumerate(series):
            pts = " ".join(f"{x(f):.1f},{y(v):.1f}" for f, v in zip(s["f"], s[key]))
            out.append(f'<polyline points="{pts}" fill="none" stroke="{COLORS[i % 4]}" stroke-width="1.8"/>')
    out.append(f'<text x="{ml+pw/2}" y="{height-8}" text-anchor="middle">频率 / Hz</text>')
    for i, s in enumerate(series):
        yy = mt + 16 + i * 16
        out.append(f'<line x1="{ml+pw-250}" y1="{yy-4}" x2="{ml+pw-226}" y2="{yy-4}" stroke="{COLORS[i % 4]}" stroke-width="2"/>')
        out.append(f'<text x="{ml+pw-220}" y="{yy}">{s["label"]}</text>')
    out.append("</svg>")
    return "\n".join(out)
