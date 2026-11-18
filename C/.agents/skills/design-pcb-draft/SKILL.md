---
name: design-pcb-draft
description: Generate and verify native two-layer KiCad PCB drafts from complete schematics, full netlists or B-to-C manifests. Use for constrained power PCB placement, optional explicit Freerouting repair, native DRC/readback, portable delivery and prototype release checks.
---

# Design and verify a PCB draft

1. Read C/使用说明.md and the B manifest/pcb_constraints. Preserve upstream issues.
   Reject missing footprints and unsupported versions. Keep all physical parts.
2. Define compact power/driver/sense groups, actual pad pairs, fixed placements
   and component regions. Require evidence for numeric limits. Do not invent a
   generic 2mm limit. Null limits need engineering review. Component keepouts do
   not imply copper keepouts; inspect actual return paths and noisy routing separately.
3. Run C/auto_pcb.py. Supply larger component/pad/grid budgets explicitly when
   required; never edit away a guard or silently reduce the circuit. Use the
   specified KiCad version and report unavailable tools honestly.
4. Inspect layout_check.json even if DRC is zero. Inspect native readback and raw
   DRC, including every track, warnings and schematic parity where available.
5. Use --freerouting-jar only for an explicitly supplied official Freerouting 2.x
   JAR. Record its hash and actual use. Control/GND rerouting needs renewed power
   return review; a thin GND neckdown is not a proven power-current path.
6. Inspect delivery.zip and roundtrip_check.json. The program checks extracted
   native files; do not claim this was a GUI test. Perform target-GUI reopening
   and engineering reviews separately, bind their evidence to current artifacts.
7. Run release_check.py independently. Report prototype_ready separately from
   routing completion. Never invent approvals or mark candidates as verified.
   Do not label the board as production-ready; production qualification is not implemented.

From repository root:

```bash
python C/auto_pcb.py B_output/project_input.json --constraints B_output/pcb_constraints.json --target draft --output C/results
python C/release_check.py RUN_DIRECTORY --write-review-template review_draft.json
python C/release_check.py RUN_DIRECTORY --review completed_review.json
python C/package_project.py RUN_DIRECTORY --review completed_review.json --target prototype
```

The empty review template deliberately fails. Complete review records only from
real dated review evidence; do not mechanically fill them to make the gate pass.
Recheck after changes to PCB, source netlist, rules, constraints or libraries.
