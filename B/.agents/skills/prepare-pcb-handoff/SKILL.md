---
name: prepare-pcb-handoff
description: Validate a completed B KiCad schematic/netlist and prepare the versioned input for C PCB generation. Use for B-to-C handoff, footprint preflight, decoupling pin bindings and native schematic/netlist consistency checks.
---

# Prepare B input for C

1. Read the A design_spec and B's actual full netlist. Keep A's status and open
   issues. Require unique physical references, unambiguous pin nets and assigned
   footprints. Do not guess an empty footprint or approve an abstract component.
2. Supply the original modern schematic when available. Run the adapter below;
   it copies all subpages, exports a fresh native netlist, compares it against B's
   input and runs native ERC. Treat unavailable ERC as not assessed.
3. Bind each proposed decoupling capacitor to actual supply/ground pin numbers
   and confirm both ends are on the intended nets. Add design-specific distance
   limits only with evidence. Keep unknown limits null.
4. Supply actual procurement/footprint review records if available. Candidate
   footprints may enter a draft; they cannot receive prototype approval.
5. Inspect preflight.json, then pass project_input.json to C. Do not claim this
   adapter implements the rest of B's schematic-design or JLCEDA automation.

From repository root:

```bash
python B/prepare_pcb_input.py A_output/design_spec.json B_output/full.xml --schematic B_output/main.kicad_sch --output handoffs/B_to_C_r2 --bindings bindings.json --footprint-evidence footprints.json
python C/auto_pcb.py handoffs/B_to_C_r2/project_input.json --output C/results --target draft
```

Read ../../../README.md for the implemented scope and example binding format.
Retain failures; use a new revision directory instead of overwriting a valid handoff.
