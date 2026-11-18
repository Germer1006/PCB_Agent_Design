from pathlib import Path
import json
import xml.etree.ElementTree as ET


def netlist(path, count=2, *, missing_footprint=False, hierarchical=True):
    root = ET.Element("export", version="E")
    components = ET.SubElement(root, "components")
    nets = ET.SubElement(root, "nets")
    supply = ET.SubElement(nets, "net", code="1", name="VIN")
    ground = ET.SubElement(nets, "net", code="2", name="GND")
    for index in range(1, count+1):
        comp = ET.SubElement(components, "comp", ref=f"R{index}")
        ET.SubElement(comp, "value").text = "10k"
        if not missing_footprint or index > 1:
            ET.SubElement(comp, "footprint").text = "Resistor_SMD:R_0603_1608Metric"
        ET.SubElement(comp, "sheetpath", names="/child/" if hierarchical else "/",
                      tstamps="/11111111-1111-1111-1111-111111111111/22222222-2222-2222-2222-222222222222/" if hierarchical else "/")
        ET.SubElement(comp, "tstamps").text = f"00000000-0000-0000-0000-{index:012d}"
        ET.SubElement(supply, "node", ref=f"R{index}", pin="1")
        ET.SubElement(ground, "node", ref=f"R{index}", pin="2")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return Path(path)


def model(count=2):
    parts = []
    for index in range(1, count+1):
        ref = f"R{index}"
        parts.append({"ref": ref, "value": "10k", "footprint": "Resistor_SMD:R_0603_1608Metric",
                      "min_x": -1, "min_y": -.6, "max_x": 1, "max_y": .6, "width": 2, "height": 1.2,
                      "pads": [{"ref": ref, "number": str(pin), "net": name, "x": x, "y": 0,
                                "w": .5, "h": .6, "layers": [0], "shape": "rect", "drill": 0}
                               for pin, x, name in [(1, -.7, "VIN"), (2, .7, "GND")]]})
    return {"components": parts, "nets": {"VIN": {}, "GND": {}}}


def contract():
    return {"schema_version": "pcb-constraints-1", "design_revision": "synthetic-test-1",
            "upstream": {"status": "verified", "open_issues": []}, "footprints": {},
            "groups": [], "pad_pairs": [{"id": "decoupling", "a": ["R1", "1"], "b": ["R2", "1"],
                "kind": "decoupling", "max_distance_mm": 3, "weight": 8,
                "evidence": ["Synthetic test threshold; NOT a universal electrical limit"]}],
            "fixed": {}, "regions": [], "keepouts": [], "required_reviews": []}


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
