---
name: design-pcb-draft
description: Generate and verify native two-layer KiCad PCB drafts from complete schematics, full netlists or B-to-C manifests. Use for constrained power PCB placement, optional explicit Freerouting repair, native DRC/readback, portable delivery and prototype release checks.
---

# Design and verify a PCB draft

1. Read C/使用说明.md and the B manifest/pcb_constraints. Preserve upstream issues.
   Reject missing footprints and unsupported versions. Keep all physical parts.
2. Identify the switch loop, driver turn-on and turn-off paths, shunt inputs,
   and decouplers from actual schematic pins. Bind them in `groups` and
   `pad_pairs`. For Kelvin pairs use `a` for the shunt pad and `b` for the sense pin.
   When reworking a layout, place and orient these clusters first. Use `fixed`
   to retain positions after checking pin access, courtyards and local return.
   Set numerical limits only from design evidence. Leave unsupported limits null.
3. Run C/auto_pcb.py with the required KiCad version and explicit resource budgets
   for designs above the defaults. Check `fanout` and `pad_failures` in the
   routing diagnostics. For `no_landing`, inspect pin-row escape and nearby
   placement. For `no_path`, inspect which existing tracks block completion.
   Compare reruns with the same rules and plane fill settings. Verify ground
   returns and keep critical copper checked after any local repair.
4. Inspect `layout_check.json` and use `critical_routes.py` when reviewing an
   existing file. Check the actual driver path length and layer changes,
   both shunt takeoffs, power return, switch copper and supply decoupling.
   Verify ground copper across layers in KiCad. Component keepouts only
   restrict placement. Inspect raw DRC for every track, warning and available
   schematic-parity result, then check the native pin-network readback.
5. Use --freerouting-jar only for an explicitly supplied official Freerouting 2.x
   JAR. Record its hash and actual use. Recheck the critical copper after
   rerouting and inspect current-carrying GND paths.
6. Inspect delivery.zip and roundtrip_check.json. The program checks extracted
   native files; do not claim this was a GUI test. Perform target-GUI reopening
   and engineering reviews separately, bind their evidence to current artifacts.
7. Run release_check.py independently. Report prototype_ready separately from
   routing completion. Never invent approvals or mark candidates as verified.
   Do not label the board as production-ready; production qualification is not implemented.

From repository root:

```bash
python C/auto_pcb.py B_output/project_input.json --constraints B_output/pcb_constraints.json --target draft --output C/results
python C/critical_routes.py RUN_DIRECTORY/project/BOARD.kicad_pcb RUN_DIRECTORY/project/pcb_constraints.json
python C/release_check.py RUN_DIRECTORY --write-review-template review_draft.json
python C/release_check.py RUN_DIRECTORY --review completed_review.json
python C/package_project.py RUN_DIRECTORY --review completed_review.json --target prototype
```

The empty review template deliberately fails. Complete review records only from
real dated review evidence; do not mechanically fill them to make the gate pass.
Recheck after changes to PCB, source netlist, rules, constraints or libraries.
