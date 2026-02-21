# Manual Test Checklist (Level 4)

Tests that verify the Claude Code integration works end-to-end.
Run these manually after all automated tests pass.

## Prerequisite

- [ ] `pip install -e .` in mdzen conda env
- [ ] `mdzen-mcp` starts without errors (Ctrl+C to exit)

## MCP Server

- [ ] `mcp dev servers/_mcp_main.py` opens Inspector
- [ ] Inspector shows tools from all 8 servers
- [ ] Can call `list_available_lipids` in Inspector (simple smoke test)

## Claude Code Interactive Mode

- [ ] Start `claude` in mdzen directory
- [ ] Run: `/md-prepare PDB 1AKE`
- [ ] Claude asks about chain selection (A, B detected)
- [ ] Claude asks about ligand inclusion (AP5 detected)
- [ ] After answering, Claude proceeds through remaining steps
- [ ] Job directory created with progress.json

## Claude Code Autonomous Mode

- [ ] Run: `/md-prepare PDB 1AKE, chain A, no ligands, run end-to-end with defaults`
- [ ] Claude proceeds without questions
- [ ] All 5 steps complete
- [ ] parm7 and rst7 files exist

## Claude Code Resume

- [ ] Note job_id from previous run
- [ ] Run: `/md-prepare resume job_XXXXXXXX`
- [ ] Claude reads progress.json and reports status
