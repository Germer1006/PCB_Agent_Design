import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_files import configure_project
from kicad_io import _parse, emit
from helpers import write_json
from freerouting_adapter import configure_dsn
from kicad_io import _items


class RulesTests(unittest.TestCase):
    def test_source_constraints_must_not_be_relaxed_to_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);source = root/'original.kicad_pro'
            write_json(source, {"board": {"design_settings": {"rules": {"min_track_width": .25}}}})
            with self.assertRaisesRegex(ValueError, "do not relax"):
                configure_project(root/'output.kicad_pro', source, .15, .45, .2)

    def test_excluded_errors_and_disabled_clearance_are_rejected(self):
        for settings in ({"drc_exclusions": ["clearance|some-id"]}, {"rule_severities": {"clearance": "ignore"}}):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory);source=root/'source.kicad_pro'
                write_json(source, {"board": {"design_settings": settings}})
                with self.assertRaises(ValueError): configure_project(root/'output.kicad_pro', source, .25, .45, .2)

    def test_rules_are_explicit_and_keep_stricter_manufacturing_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.kicad_pro';out=root/'out.kicad_pro'
            write_json(source, {"board": {"design_settings": {"rules": {"min_hole_to_hole": .4}}}})
            configure_project(out, source, .25, .45, .2)
            data=json.loads(out.read_text());rules=data['board']['design_settings']['rules']
            self.assertEqual(rules['min_hole_to_hole'], .4)
            self.assertEqual(rules['min_track_width'], .25)
            self.assertEqual(data['net_settings']['meta']['version'], 4)

    def test_unset_source_minimum_does_not_bypass_requested_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.kicad_pro';out=root/'out.kicad_pro'
            write_json(source, {"board": {"design_settings": {"rules": {"min_clearance": 0, "min_track_width": 0}}}})
            configure_project(out, source, .25, .45, .2)
            rules=json.loads(out.read_text())['board']['design_settings']['rules']
            self.assertEqual(rules['min_clearance'], .2)
            self.assertEqual(rules['min_track_width'], .25)

    def test_serializer_preserves_quoted_types_and_unicode(self):
        text='(symbol (uuid "1234") (property "Value" "中文\\n10k") (at 1 2 0))'
        tree=_parse(text);self.assertEqual(_parse(emit(tree)),tree)
        self.assertIn('(uuid "1234")',emit(tree))

    def test_external_router_gets_power_class_without_reduced_clearance(self):
        source = '''(pcb test (parser (string_quote ")) (unit um)
          (structure (rule (width 150) (clearance 150)
          (clearance 25 (type smd_to_turn_gap))))
          (network (class default "VIN" "GND" (circuit (use_via via))
          (rule (width 150) (clearance 150)))))'''
        result = configure_dsn(source, {"VIN"}, .15, .6, .2)
        self.assertIn('(string_quote ")', result)
        parsed = _parse(result.replace('(string_quote ")', '(string_quote "QUOTE")'))
        classes = _items(_items(parsed, 'network')[0], 'class')
        self.assertNotIn('VIN', classes[0][2:])
        power = next(c for c in classes if c[1] == 'pcb_power')
        self.assertIn('VIN', power)
        rule = _items(power, 'rule')[0]
        self.assertEqual(float(_items(rule, 'width')[0][1]), 600)
        self.assertEqual(float(_items(rule, 'clearance')[0][1]), 200)
        self.assertIn('(clearance 200.0 (type smd_to_turn_gap))', result)
        with self.assertRaisesRegex(ValueError, 'unit'):
            configure_dsn(source.replace('(unit um)', '(unit mil)'), {'VIN'}, .15, .6, .2)


if __name__ == '__main__':unittest.main()
