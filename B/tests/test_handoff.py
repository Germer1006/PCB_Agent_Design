import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'B'));sys.path.insert(0,str(ROOT/'C/tests'));sys.path.insert(0,str(ROOT/'C'))
from prepare_pcb_input import erc_violation_count, prepare
from helpers import netlist,contract,write_json
from auto_pcb import _input_paths


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.spec=self.root/'design_spec.json';self.net=netlist(self.root/'source.xml')
        self.data={'revision':'synthetic-test-1','status':'needs_rework','open_issues':['startup unresolved'],'pcb_constraints':contract()}
        write_json(self.spec,self.data)

    def test_draft_handoff_cannot_erase_upstream_blockers(self):
        out=self.root/'handoff';r=prepare(self.spec,self.net,out)
        self.assertEqual(r['component_count'],2);self.assertFalse(r['ready_for_prototype'])
        c=json.loads((out/'pcb_constraints.json').read_text());self.assertEqual(c['upstream']['status'],'needs_rework')
        self.assertIn('startup unresolved',c['upstream']['open_issues']);self.assertIn('B_NATIVE_ERC_NOT_ASSESSED',c['upstream']['open_issues'])
        self.assertTrue(all(x['status']=='candidate' for x in c['footprints'].values()))
        paths=_input_paths(out/'project_input.json',None,None)
        self.assertEqual(paths[0],out/'input.xml')

    def test_unassigned_footprint_is_blocked_not_substituted(self):
        netlist(self.net,missing_footprint=True);out=self.root/'missing';r=prepare(self.spec,self.net,out)
        self.assertEqual(r['status'],'blocked');self.assertFalse((out/'project_input.json').exists())

    def test_even_ready_a_draft_remains_an_explicit_blocker(self):
        self.data.update(status='ready_for_downstream',open_issues=[],formal_handoff=False)
        write_json(self.spec,self.data);out=self.root/'draft-a'
        prepare(self.spec,self.net,out)
        c=json.loads((out/'pcb_constraints.json').read_text())
        self.assertIn('A_DRAFT_NOT_FORMAL',c['upstream']['open_issues'])

    def test_mixed_revisions_are_rejected(self):
        self.data['pcb_constraints']['design_revision']='stale';write_json(self.spec,self.data)
        r=prepare(self.spec,self.net,self.root/'revision')
        self.assertEqual(r['status'],'blocked')

    def test_pin_binding_must_match_actual_nets(self):
        binding={'id':'supply','ic':'R1','capacitor':'R2','supply_pin':'1','ground_pin':'2',
                 'capacitor_supply_pin':'2','capacitor_ground_pin':'1','evidence':['synthetic']}
        path=self.root/'bindings.json';write_json(path,{'decoupling':[binding]})
        r=prepare(self.spec,self.net,self.root/'bindings-out',bindings=path)
        self.assertEqual(r['status'],'blocked')

    def test_verified_footprint_must_be_the_actual_assigned_footprint(self):
        path=self.root/'evidence.json';write_json(path,{'R1':{'status':'verified','footprint':'Wrong:Package','mpn':'fixture','reviewer':'test','evidence':['fixture']}})
        r=prepare(self.spec,self.net,self.root/'evidence-out',footprint_evidence=path)
        self.assertEqual(r['status'],'blocked')

    def test_existing_handoff_is_not_partially_overwritten(self):
        out=self.root/'output';prepare(self.spec,self.net,out)
        before=(out/'input.xml').read_bytes()
        with self.assertRaises(ValueError):prepare(self.spec,self.net,out)
        self.assertEqual((out/'input.xml').read_bytes(),before)

    def test_manifest_cannot_escape_its_project(self):
        out=self.root/'handoff';prepare(self.spec,self.net,out)
        p=out/'project_input.json';entry=json.loads(p.read_text());entry['netlist']='../source.xml';write_json(p,entry)
        with self.assertRaises(ValueError):_input_paths(p,None,None)

    def test_native_erc_reads_every_sheet_and_rejects_incomplete_reports(self):
        clean = {"path": "/", "violations": []}
        child = {"path": "/child", "violations": [{"type": "pin_not_connected"}]}
        self.assertEqual(erc_violation_count({"sheets": [clean]}), 0)
        self.assertEqual(erc_violation_count({"violations": []}), 0)
        self.assertEqual(erc_violation_count({"violations": [], "sheets": [clean, child]}), 1)
        for bad in ({}, {"sheets": []}, {"sheets": [clean, {"path": "/child"}]},
                    {"violations": [], "sheets": [{"violations": "unknown"}]}):
            with self.subTest(bad=bad):
                self.assertIsNone(erc_violation_count(bad))

if __name__=='__main__':unittest.main()
