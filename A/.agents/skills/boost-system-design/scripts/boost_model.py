"""非同步 Boost CCM 平均模型（含寄生参数），供 loss_thermal.py 与 loop_design.py 使用.

状态量 x = [iL, vC1, ..., vCn]；每个输出电容支路为 Ck 串 ESR rk；负载 = 恒流 I0 + 电阻 Rac。
  L·diL/dt = Vin − iL·(DCR + d·Rds + d'·Rd) − d'·(VF0 + vo)
  Ck·dvCk/dt = (vo − vCk)/rk
  vo = (d'·iL − I0 + Σ vCk/rk) / (Σ 1/rk + 1/Rac)
小信号传递函数由数值雅可比线性化后在 jω 上直接求解，不依赖手推公式。
仅适用于 CCM；DCM（轻载）不在本模型范围内。
"""

import cmath
import math


class Boost:
    def __init__(self, L, dcr, rds, vf0, rd, caps):
        self.L, self.dcr, self.rds, self.vf0, self.rd = L, dcr, rds, vf0, rd
        self.caps = caps  # list of (C, esr)

    # ---------- algebra ----------
    def _vo(self, x, d, I0, Rac):
        g = sum(1 / r for _, r in self.caps) + (1 / Rac if Rac else 0)
        return ((1 - d) * x[0] - I0 + sum(x[k + 1] / r for k, (_, r) in enumerate(self.caps))) / g

    def f(self, x, d, vin, I0, Rac):
        vo = self._vo(x, d, I0, Rac)
        rser = self.dcr + d * self.rds + (1 - d) * self.rd
        dx = [(vin - x[0] * rser - (1 - d) * (self.vf0 + vo)) / self.L]
        for k, (C, r) in enumerate(self.caps):
            dx.append((vo - x[k + 1]) / (r * C))
        return dx

    # ---------- DC operating point ----------
    def dc_residual(self, d, vin, vo, io):
        il = io / (1 - d)
        return vin - il * (self.dcr + d * self.rds + (1 - d) * self.rd) - (1 - d) * (self.vf0 + vo)

    def solve_dc(self, vin, vo, io):
        """Return (D, IL) for given output voltage and total output current; None if unreachable."""
        ds = [i / 2000 for i in range(0, 1990)]
        res = [self.dc_residual(d, vin, vo, io) for d in ds]
        k = max(range(len(ds)), key=lambda i: res[i])
        if res[k] < 0:
            return None
        lo, hi = 0.0, ds[k]
        if self.dc_residual(lo, vin, vo, io) >= 0:
            return 0.0, io
        for _ in range(80):
            mid = (lo + hi) / 2
            if self.dc_residual(mid, vin, vo, io) < 0:
                lo = mid
            else:
                hi = mid
        d = (lo + hi) / 2
        return d, io / (1 - d)

    def vo_of_d(self, vin, d, R):
        """Steady-state Vo for resistive load R at duty d (gain curve with losses)."""
        dp = 1 - d
        rser = self.dcr + d * self.rds + dp * self.rd
        return (vin - dp * self.vf0) / (dp + rser / (R * dp))

    def gain_peak(self, vin, R):
        ds = [i / 1000 for i in range(0, 995)]
        vs = [self.vo_of_d(vin, d, R) for d in ds]
        k = max(range(len(ds)), key=lambda i: vs[i])
        return ds[k], vs[k]

    # ---------- small signal ----------
    def linearize(self, vin, d, il, vo, I0, Rac):
        x0 = [il] + [vo] * len(self.caps)
        n = len(x0)
        f0 = self.f(x0, d, vin, I0, Rac)
        A = [[0.0] * n for _ in range(n)]
        for j in range(n):
            h = 1e-6 * max(1.0, abs(x0[j]))
            xp = list(x0)
            xp[j] += h
            fp = self.f(xp, d, vin, I0, Rac)
            for i in range(n):
                A[i][j] = (fp[i] - f0[i]) / h
        hd = 1e-7
        fd = self.f(x0, d + hd, vin, I0, Rac)
        B = [(fd[i] - f0[i]) / hd for i in range(n)]
        vo0 = self._vo(x0, d, I0, Rac)
        Cx = []
        for j in range(n):
            h = 1e-6 * max(1.0, abs(x0[j]))
            xp = list(x0)
            xp[j] += h
            Cx.append((self._vo(xp, d, I0, Rac) - vo0) / h)
        Dd = (self._vo(x0, d + hd, I0, Rac) - vo0) / hd
        return A, B, Cx, Dd

    @staticmethod
    def tf(lin, freq):
        """G(j2πf) = C (sI − A)^-1 B + D via Gaussian elimination (complex)."""
        A, B, Cx, Dd = lin
        n = len(B)
        s = 2j * math.pi * freq
        M = [[(s if i == j else 0) - A[i][j] for j in range(n)] + [complex(B[i])] for i in range(n)]
        for c in range(n):
            piv = max(range(c, n), key=lambda r: abs(M[r][c]))
            M[c], M[piv] = M[piv], M[c]
            for r in range(n):
                if r != c:
                    fct = M[r][c] / M[c][c]
                    for k in range(c, n + 1):
                        M[r][k] -= fct * M[c][k]
        xs = [M[i][n] / M[i][i] for i in range(n)]
        return sum(Cx[i] * xs[i] for i in range(n)) + Dd


def logspace(f1, f2, n):
    a, b = math.log10(f1), math.log10(f2)
    return [10 ** (a + (b - a) * i / (n - 1)) for i in range(n)]


def unwrap_deg(zs):
    out, prev = [], None
    for z in zs:
        ph = math.degrees(cmath.phase(z))
        if prev is not None:
            while ph - prev > 180:
                ph -= 360
            while ph - prev < -180:
                ph += 360
        out.append(ph)
        prev = ph
    return out
