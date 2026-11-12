"""KiCad 官方库快照与核对（模块一 → KiCad 原理图/PCB 交接）.

parts_db.json 中每个器件带 kicad = {symbol, footprint, pinmap, status, note, derive?}。
本工具把这些 lib_id 与 KiCad 官方库逐一核对，结论落盘为 knowledge/kicad_lib_index.json（快照），
之后 check_design.py / export_handoff.py 只读快照，离线可复算。

子命令:
  snapshot [--tag 10.0.8] [--local <KiCad share/kicad 目录>]
      从 gitlab.com/kicad/libraries（指定发布标签）或本机 KiCad 安装目录读取被引用的符号与封装，
      记录：是否存在、符号引脚（含 extends 继承）、默认封装、封装焊盘号、描述；
      同时按 kicad.derive 生成项目符号（knowledge/kicad_symbols/*.kicad_sym）。
  check
      离线核对 parts_db 与快照：符号/封装存在、pinmap 引脚号 ⊆ 符号引脚、符号引脚号 ⊆ 封装焊盘。

用法: python kicad_lib.py snapshot | check   （只用标准库）
"""

import argparse
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from _common import KNOWLEDGE_DIR, load_json, save_json

INDEX = KNOWLEDGE_DIR / "kicad_lib_index.json"
DERIVED_DIR = KNOWLEDGE_DIR / "kicad_symbols"
PROJECT_LIB = "boost_module1"  # 项目符号库昵称（derive 生成的符号用 boost_module1:<name>）
DEFAULT_TAG = "10.0.8"
RAW = "https://gitlab.com/kicad/libraries/{repo}/-/raw/{tag}/{path}"

# 通用阻容等不在 parts_db 中、但 export_handoff 会引用的 lib_id
EXTRA = {
    "symbols": ["Device:R", "Device:C"],
    "footprints": ["Resistor_SMD:R_0603_1608Metric_Pad0.98x0.95mm_HandSolder",
                   "Capacitor_SMD:C_0603_1608Metric_Pad1.08x0.95mm_HandSolder"],
}
PASSIVE_FP = {"R": EXTRA["footprints"][0], "C": EXTRA["footprints"][1]}


# ------------------------------------------------------------------ s-expression
def tokenize(t):
    i, n = 0, len(t)
    while i < n:
        c = t[i]
        if c in "()":
            yield c
            i += 1
        elif c.isspace():
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while t[j] != '"':
                if t[j] == "\\":
                    j += 1
                buf.append(t[j])
                j += 1
            yield ("s", "".join(buf))
            i = j + 1
        else:
            j = i
            while j < n and not t[j].isspace() and t[j] not in '()"':
                j += 1
            yield ("a", t[i:j])
            i = j


def parse(text):
    stack, cur = [], []
    for tok in tokenize(text):
        if tok == "(":
            stack.append(cur)
            cur = []
        elif tok == ")":
            done, cur = cur, stack.pop()
            cur.append(done)
        else:
            cur.append(tok[1])
    return cur[0] if cur else []


def kids(node, head):
    return [x for x in node[1:] if isinstance(x, list) and x and x[0] == head]


def first(node, head):
    k = kids(node, head)
    return k[0] if k else None


def walk(node, head):
    if isinstance(node, list):
        if node and node[0] == head:
            yield node
        for x in node[1:]:
            yield from walk(x, head)


def prop(node, name):
    for p in kids(node, "property"):
        if len(p) > 2 and p[1] == name:
            return p[2]
    return None


def block_text(text, head, name):
    """返回 '(head "name"' 开始的完整括号块原文（用于派生符号时保留原格式）。"""
    m = re.search(r'\(%s "%s"' % (re.escape(head), re.escape(name)), text)
    if not m:
        return None
    depth, i, in_s = 0, m.start(), False
    while True:
        c = text[i]
        if in_s:
            if c == "\\":
                i += 1
            elif c == '"':
                in_s = False
        elif c == '"':
            in_s = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[m.start():i + 1]
        i += 1


# ------------------------------------------------------------------ sources
class Source:
    def __init__(self, tag, local):
        self.tag, self.local, self.cache = tag, Path(local) if local else None, {}

    def _get(self, repo, rel):
        key = (repo, rel)
        if key not in self.cache:
            if self.local:
                sub = {"kicad-symbols": "symbols", "kicad-footprints": "footprints"}[repo]
                f = self.local / sub / rel
                self.cache[key] = f.read_text(encoding="utf-8") if f.exists() else None
            else:
                url = RAW.format(repo=repo, tag=self.tag, path=urllib.parse.quote(rel))
                for attempt in range(5):
                    try:
                        with urllib.request.urlopen(url, timeout=60) as r:
                            self.cache[key] = r.read().decode("utf-8")
                        break
                    except urllib.error.HTTPError as e:
                        if e.code == 404:
                            self.cache[key] = None
                            break
                        if attempt == 4:
                            raise
                    except (urllib.error.URLError, TimeoutError, ConnectionError):
                        if attempt == 4:
                            raise
                    time.sleep(2 * (attempt + 1))
        return self.cache[key]

    def symbol_text(self, lib, name):
        """KiCad 10 仓库为 <lib>.kicad_symdir/<name>.kicad_sym；旧版安装为单文件 <lib>.kicad_sym。"""
        t = self._get("kicad-symbols", f"{lib}.kicad_symdir/{name}.kicad_sym")
        if t is None:
            whole = self._get("kicad-symbols", f"{lib}.kicad_sym")
            if whole is not None:
                b = block_text(whole, "symbol", name)
                t = f"(kicad_symbol_lib {b})" if b else None
        return t

    def footprint_text(self, lib, name):
        return self._get("kicad-footprints", f"{lib}.pretty/{name}.kicad_mod")

    @property
    def label(self):
        return f"local:{self.local}" if self.local else f"gitlab.com/kicad/libraries tag {self.tag}"


def read_symbol(src, lib_id, depth=0):
    lib, name = lib_id.split(":", 1)
    t = src.symbol_text(lib, name)
    if t is None:
        return {"exists": False}
    root = parse(t)
    sym = next((s for s in kids(root, "symbol") if s[1] == name), None)
    if sym is None:
        return {"exists": False}
    ext = first(sym, "extends")
    pins = {}
    if ext and depth < 4:
        parent = read_symbol(src, f"{lib}:{ext[1]}", depth + 1)
        pins = dict(parent.get("pins", {}))
    for p in walk(sym, "pin"):
        nm, no = first(p, "name"), first(p, "number")
        if no:
            pins[no[1]] = {"name": nm[1] if nm else "", "type": p[1]}
    return {"exists": True, "extends": ext[1] if ext else None, "pins": dict(sorted(pins.items(), key=lambda kv: _num(kv[0]))),
            "default_footprint": prop(sym, "Footprint"), "description": prop(sym, "Description"),
            "datasheet": prop(sym, "Datasheet")}


def read_footprint(src, lib_id):
    lib, name = lib_id.split(":", 1)
    t = src.footprint_text(lib, name)
    if t is None:
        return {"exists": False}
    root = parse(t)
    pads = sorted({p[1] for p in kids(root, "pad") if len(p) > 1 and p[1]}, key=_num)
    d = first(root, "descr")
    return {"exists": True, "pads": pads, "descr": d[1] if d else None,
            "smd_only": all(p[2] == "smd" for p in kids(root, "pad") if p[1])}


def _num(s):
    return (0, int(s), "") if str(s).isdigit() else (1, 0, str(s))


# ------------------------------------------------------------------ derived symbols
def derive_symbol(src, spec):
    """按 spec 复制官方符号并改名/改引脚名，返回单符号块文本。"""
    lib, base = spec["from"].split(":", 1)
    t = src.symbol_text(lib, base)
    if t is None:
        raise SystemExit(f"派生源 {spec['from']} 不存在")
    if parse(t) and first(next(s for s in kids(parse(t), "symbol") if s[1] == base), "extends"):
        raise SystemExit(f"派生源 {spec['from']} 本身是 extends 符号，请改用其父符号")
    b = block_text(t, "symbol", base)
    new = spec["name"]
    b = b.replace(f'(symbol "{base}"', f'(symbol "{new}"', 1)
    b = re.sub(r'\(symbol "%s_(\d+_\d+)"' % re.escape(base), lambda m: f'(symbol "{new}_{m.group(1)}"', b)
    for old, nw in spec.get("pin_rename", {}).items():
        n_before = b.count(f'(name "{old}"')
        if n_before != 1:
            raise SystemExit(f"{spec['from']} 中引脚名 {old} 出现 {n_before} 次，无法安全改名")
        b = b.replace(f'(name "{old}"', f'(name "{nw}"')
    for k, v in spec.get("props", {}).items():
        b, n = re.subn(r'(\(property "%s" )"[^"]*"' % re.escape(k), lambda m: m.group(1) + '"' + v.replace('"', "'") + '"', b, count=1)
        if not n:
            raise SystemExit(f"{spec['from']} 无属性 {k}")
    head = re.match(r"\(kicad_symbol_lib\s*\(version (\d+)\)", t)
    return b, (head.group(1) if head else None)


def write_lib(path, blocks, version):
    body = "\n".join(blocks)
    Path(path).write_text(f'(kicad_symbol_lib\n\t(version {version})\n\t(generator "boost_system_design_kicad_lib")\n'
                          f'\t(generator_version "1")\n{body}\n)\n', encoding="utf-8")


# ------------------------------------------------------------------ commands
def collect(db):
    syms, fps = set(EXTRA["symbols"]), set(EXTRA["footprints"])
    for p in db["parts"]:
        k = p.get("kicad") or {}
        if k.get("symbol") and not k["symbol"].startswith(PROJECT_LIB + ":"):
            syms.add(k["symbol"])
        if k.get("derive"):
            syms.add(k["derive"]["from"])
        if k.get("footprint"):
            fps.add(k["footprint"])
    return sorted(syms), sorted(fps)


def snapshot(a):
    db = load_json(KNOWLEDGE_DIR / "parts_db.json")
    src = Source(a.tag, a.local)
    syms, fps = collect(db)
    idx = {"schema": "kicad-lib-index/1", "source": src.label, "fetched": date.today().isoformat(),
           "note": "由 kicad_lib.py snapshot 生成，勿手改；换 KiCad 版本后重新 snapshot", "symbols": {}, "footprints": {}, "derived": {}}
    for s in syms:
        idx["symbols"][s] = read_symbol(src, s)
        print(f"  sym {'OK ' if idx['symbols'][s]['exists'] else 'NO '} {s}")
    for f in fps:
        idx["footprints"][f] = read_footprint(src, f)
        print(f"  fp  {'OK ' if idx['footprints'][f]['exists'] else 'NO '} {f}")
    DERIVED_DIR.mkdir(exist_ok=True)
    for p in db["parts"]:
        d = (p.get("kicad") or {}).get("derive")
        if not d:
            continue
        block, ver = derive_symbol(src, d)
        out = DERIVED_DIR / f"{d['name']}.kicad_sym"
        write_lib(out, [block], ver or "20251024")
        pins = read_symbol(_FileSource(out), f"x:{d['name']}")["pins"]
        idx["derived"][f"{PROJECT_LIB}:{d['name']}"] = {"exists": True, "from": d["from"], "file": f"kicad_symbols/{out.name}",
                                                         "pins": pins, "format_version": ver}
        idx["symbols"][f"{PROJECT_LIB}:{d['name']}"] = {"exists": True, "pins": pins, "derived_from": d["from"],
                                                          "default_footprint": p["kicad"].get("footprint")}
        print(f"  derived {PROJECT_LIB}:{d['name']} ← {d['from']}  → {out.name}")
    save_json(INDEX, idx)
    print(f"snapshot → {INDEX}  ({len(syms)} symbols, {len(fps)} footprints)")


class _FileSource:
    def __init__(self, path):
        self.t = Path(path).read_text(encoding="utf-8")

    def symbol_text(self, lib, name):
        return self.t


def verify_part(p, idx):
    """返回 (verdict, detail)；verdict ∈ PASS/WARN/FAIL。供 check_design 调用。"""
    k = p.get("kicad")
    if not k:
        return "FAIL", "parts_db 无 kicad 映射"
    msgs, verdict = [], "PASS"
    sym, fp = idx["symbols"].get(k.get("symbol") or ""), idx["footprints"].get(k.get("footprint") or "")
    if not k.get("symbol") or not sym or not sym.get("exists"):
        return "FAIL", f"符号 {k.get('symbol')} 不在 KiCad 库快照中（status={k.get('status')}：{k.get('note')}）"
    if not k.get("footprint") or not fp or not fp.get("exists"):
        return "FAIL", f"封装 {k.get('footprint')} 不在 KiCad 库快照中（{k.get('note')}）"
    spins = set(sym["pins"])
    bad = [f"{fn}→{no}" for fn, no in (k.get("pinmap") or {}).items() if no not in spins]
    if bad:
        return "FAIL", f"pinmap 引脚号不在符号 {k['symbol']} 中：{bad}"
    missing = sorted(spins - set(fp["pads"]), key=_num)
    if missing:
        return "FAIL", f"符号引脚 {missing} 在封装 {k['footprint']} 中无对应焊盘"
    extra = sorted(set(fp["pads"]) - spins, key=_num)
    if extra:
        msgs.append(f"封装多出焊盘 {extra}（散热/机械焊盘需在原理图确认连接）")
        verdict = "WARN"
    if k.get("status") in ("equivalent", "pin-compatible-base", "generic"):
        verdict = "WARN" if k["status"] != "generic" else verdict
        msgs.append(f"{k['status']}：{k.get('note')}")
    if not k.get("pinmap") and len(spins) > 2 and "MCU" not in k["symbol"]:
        msgs.append("无 pinmap，原理图按符号引脚名连接")
    return verdict, f"{k['symbol']} / {k['footprint']}（{len(spins)} 引脚 ↔ {len(fp['pads'])} 焊盘）" + ("；" + "；".join(msgs) if msgs else "")


def verify_mcu_pins(p, idx):
    """MCU pin_plan 的引脚号/名称与 KiCad 符号一致。"""
    sym = idx["symbols"].get((p.get("kicad") or {}).get("symbol") or "", {})
    if not sym.get("exists"):
        return False, "MCU 符号不在快照中"
    pins, bad, n = sym["pins"], [], 0
    for sig, v in (p.get("pin_plan") or {}).items():
        if not isinstance(v, dict) or "pin_no" not in v:
            continue
        n += 1
        want = v["pin"].split("-")[0]
        got = pins.get(str(v["pin_no"]), {}).get("name")
        if got != want:
            bad.append(f"{sig}: pin {v['pin_no']} 规划 {v['pin']}，符号为 {got}")
    pw = (p.get("pin_plan") or {}).get("power", {})
    for nm, nos in pw.items():
        for no in (nos if isinstance(nos, list) else [nos] if isinstance(nos, int) else []):
            n += 1
            if pins.get(str(no), {}).get("name") != nm:
                bad.append(f"{nm}: pin {no} 符号为 {pins.get(str(no), {}).get('name')}")
    return not bad, (f"{n} 个规划引脚与 {sym and (p['kicad']['symbol'])} 一致" if not bad else "；".join(bad))


def check(_a):
    db, idx = load_json(KNOWLEDGE_DIR / "parts_db.json"), load_json(INDEX)
    print(f"快照来源: {idx['source']}（{idx['fetched']}）")
    cnt = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for p in db["parts"]:
        v, d = verify_part(p, idx)
        cnt[v] += 1
        print(f"  [{v}] {p['id']:28s} {d}")
    for p in db["parts"]:
        if p.get("role") == "mcu" and p.get("pin_plan"):
            ok, d = verify_mcu_pins(p, idx)
            print(f"  [{'PASS' if ok else 'FAIL'}] pin_plan {p['id']}: {d}")
    print(cnt)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--tag", default=DEFAULT_TAG)
    s.add_argument("--local", help="本机 KiCad 的 share/kicad 目录（含 symbols/ 与 footprints/）")
    sub.add_parser("check")
    a = ap.parse_args()
    {"snapshot": snapshot, "check": check}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
