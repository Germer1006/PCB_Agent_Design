"""Real CLI/pcbnew integration, skipped explicitly when native KiCad is unavailable."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_pcb import generate
from kicad_io import load_design
from release_check import evaluate
from release_check import seal_verification, review_template
from constraints import check_layout
from delivery import package_delivery
from helpers import netlist, contract, write_json
from verify import run_drc
from kicad_io import native_operation


@unittest.skipUnless(shutil.which("kicad-cli"), "Native KiCad CLI not installed")
class NativePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.source = netlist(cls.root / "hierarchical.xml")
        cls.cli = Path(shutil.which("kicad-cli"))
        cls.result = generate(cls.source, cls.root / "runs", cli_path=cls.cli, dimensions=[24, 18],
                              attempts=1, router_timeout=5, preview=False, progress=lambda s: None)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def test_hierarchical_xml_and_frozen_footprints_roundtrip(self):
        r = self.result
        self.assertEqual(r["drc"]["status"], "passed", r["drc"])
        self.assertTrue(r["complete"])
        self.assertFalse(r["prototype_ready"])
        self.assertTrue(r["roundtrip"]["same_project_hashes"])
        with zipfile.ZipFile(r["delivery"]) as archive:
            self.assertIn("project/fp-lib-table", archive.namelist())
            self.assertTrue(any(name.endswith(".kicad_mod") for name in archive.namelist()))
            self.assertIn("roundtrip_check.json", archive.namelist())
            self.assertFalse(any(name.startswith("candidates/") for name in archive.namelist()))
            self.assertFalse(any(Path(name).name.startswith('.') for name in archive.namelist()))

    def test_changed_project_invalidates_previous_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with zipfile.ZipFile(self.result["delivery"]) as archive: archive.extractall(root)
            original = evaluate(root)
            self.assertNotIn("ARTIFACT_CHANGED", [b["id"] for b in original["blockers"]])
            project = root / "project/hierarchical.kicad_pro"
            data = json.loads(project.read_text()); data["board"]["design_settings"]["rules"]["min_clearance"] = .01
            project.write_text(json.dumps(data))
            self.assertIn("ARTIFACT_CHANGED", [b["id"] for b in evaluate(root)["blockers"]])

    def test_extra_native_component_cannot_hide_in_readback(self):
        proof = json.loads((self.result["directory"] / "verification.json").read_text())
        from release_check import check_readback, read_expected
        audit = proof["audit"]
        audit["components"].append({**audit["components"][0], "ref": "EXTRA"})
        self.assertEqual(check_readback(*read_expected(self.result["directory"] / "project"), audit)["status"], "failed")

    def test_missing_footprint_stops_without_substitution(self):
        source = netlist(self.root / "missing.xml", missing_footprint=True)
        with self.assertRaisesRegex(ValueError, "缺少封装"):
            load_design(source, kicad_cli=self.cli, work_dir=self.root / "missing_metrics")

    def test_larger_design_hits_budget_before_layout(self):
        source = netlist(self.root / "31parts.xml", count=31)
        with self.assertRaisesRegex(ValueError, "31 components"):
            generate(source, self.root / "budget", cli_path=self.cli, attempts=1, preview=False, progress=lambda s: None)

    def qualified_synthetic_fixture(self, root):
        with zipfile.ZipFile(self.result["delivery"]) as archive: archive.extractall(root)
        proof=json.loads((root/'verification.json').read_text());project=root/'project';board=project/proof['board']
        c=contract();c['pad_pairs'][0]['max_distance_mm']=20
        for part in proof['audit']['components']:
            ref=part['ref'];c['footprints'][ref]={'status':'verified','footprint':proof['footprint_origins'][ref],
                'mpn':'SYNTHETIC-TEST-ONLY','reviewer':'synthetic fixture','evidence':['synthetic test, not procurement evidence']}
        write_json(project/'pcb_constraints.json',c)
        d=load_design(project/'input.net.xml',kicad_cli=self.cli,work_dir=root/'fixture_metrics')
        positions={p['ref']:{'x':p['x'],'y':p['y'],'angle':int(round(p['angle']))%360} for p in proof['audit']['components']}
        layout=check_layout(d,positions,c);self.assertEqual(layout['status'],'passed')
        seal_verification(root,project,board,proof['drc'],proof['audit'],layout,proof['footprint_origins'])
        review=review_template(root);review.update(reviewer='SYNTHETIC TEST FIXTURE',reviewed_at='2026-01-01T00:00:00+00:00')
        for decision in review['decisions'].values():decision.update(status='approved',notes='Synthetic gate behavior test; not a real engineering approval',evidence=['synthetic fixture'])
        write_json(root/'release_review.json',review)
        return d

    def test_release_requires_reviews_footprints_and_zip_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);d=self.qualified_synthetic_fixture(root)
            before=evaluate(root)
            self.assertFalse(before['prototype_ready'])
            self.assertIn('PACKAGE_NOT_VERIFIED',[b['id'] for b in before['blockers']])
            package_delivery(root,cli=self.cli,design=d)
            qualified=evaluate(root)
            self.assertTrue(qualified['prototype_ready'],qualified)
            self.assertFalse(qualified['production_ready'])
            with zipfile.ZipFile(root/'delivery.zip') as archive:
                self.assertTrue(json.loads(archive.read('release_status.json'))['prototype_ready'])
                self.assertTrue(json.loads(archive.read('execution_status.json'))['prototype_ready'])
                self.assertTrue(json.loads(archive.read('布局与布线.json'))['prototype_ready'])
            review=json.loads((root/'release_review.json').read_text());review['decisions']['power_layout']['status']='not_assessed'
            write_json(root/'release_review.json',review)
            self.assertIn('REVIEW:power_layout',[b['id'] for b in evaluate(root)['blockers']])

    def test_stale_review_cannot_approve_changed_libraries(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);d=self.qualified_synthetic_fixture(root);package_delivery(root,cli=self.cli,design=d)
            footprint=next((root/'project/PCB_Project.pretty').glob('*.kicad_mod'))
            footprint.write_text(footprint.read_text()+'\n; changed after review\n')
            result=evaluate(root)
            self.assertFalse(result['prototype_ready'])
            self.assertIn('ARTIFACT_CHANGED',[b['id'] for b in result['blockers']])

    @unittest.skipUnless(os.environ.get('FREEROUTING_JAR'), 'Optional official Freerouting JAR not supplied')
    def test_explicit_external_router_keeps_power_width_and_native_rules(self):
        from freerouting_adapter import reroute
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with zipfile.ZipFile(self.result['delivery']) as archive: archive.extractall(root)
            proof=json.loads((root/'verification.json').read_text());project=root/'project'
            board=project/proof['board']
            d=load_design(project/'input.net.xml',kicad_cli=self.cli,work_dir=root/'metrics')
            repaired,provenance=reroute(board,d,root/'repair',jar=os.environ['FREEROUTING_JAR'],
                signal_width=.25,power_width=.45,clearance=.2,power_nets={'VIN','GND'},timeout=60)
            shutil.copyfile(repaired,board)
            drc=run_drc(board,root/'repaired_DRC.json',self.cli)
            audit=native_operation(d,'audit',{'board':str(board)},root)
            self.assertEqual(provenance['engine'],'freerouting')
            self.assertEqual(drc['status'],'passed',drc)
            power=[s for s in audit['route']['segments'] if s['net']=='VIN']
            self.assertTrue(power)
            self.assertTrue(all(s['width']>=.45-1e-6 for s in power))


if __name__ == "__main__": unittest.main()
