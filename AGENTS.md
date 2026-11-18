# PCB design workflow

Work in the requested module and preserve its existing instructions. Treat A's
system design, B's physical schematic, C's native PCB and D's firmware as different
verification scopes. Do not turn a downstream DRC pass into an upstream approval.

- Read A/AGENTS.md before changing or running A. Preserve its numerical/evidence
  checks and formal handoff refusal; never hand-edit generated A outputs to pass.
- Use A/design_circuit.py for RC low-pass and voltage-divider drafts. Use the
  existing A Boost path for async_boost.
- Use B/generate_schematic.py for explicit single-sheet schematic intent and
  B/prepare_pcb_input.py for an existing schematic or netlist. Verify the exported
  KiCad netlist against declared pins before feeding C.
- Use C/auto_pcb.py for generation, then C/release_check.py for release status.
  Always retain failed/not_assessed results. A generated draft is not a released board.
- Bind reviews to the actual PCB, rules, complete netlist and project libraries.
  Do not invent reviewers, approval notes, measurements or GUI test evidence.
- Do not guess missing footprints, replace 5.00mm pitch by 5.08mm, disable a rule,
  lower a source constraint, delete a component or clear an upstream issue to get a pass.
- Millimetre thresholds require design-specific evidence. Null means not assessed.
  Straight pad distances do not establish routed loop area, Kelvin topology or thermal safety.
- Keep project rules and complete libraries in deliverables. Check the actual ZIP
  after extraction. Record external routing/compatibility adapters honestly.
- Preserve default resource limits; larger trials must request explicit budgets.
  No resource override proves the solver can handle the larger design.

Run relevant tests before committing:

```bash
python -B -m unittest discover -s C/tests -v
python -B -m unittest discover -s B/tests -v
cd A/.agents/skills/boost-system-design/scripts
python -B -m unittest discover -p 'test_*.py' -v
```

Native integration tests require KiCad CLI and its pcbnew Python. Report skipped
native tests as skipped. Never label mock-only tests as an EDA roundtrip.
