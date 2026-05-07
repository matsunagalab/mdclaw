"""Tests for mdclaw.openmm_system_server.build_openmm_system."""

import textwrap
from pathlib import Path

import pytest

pytest.importorskip("openff.pablo")
pytest.importorskip("openmm")
pytest.importorskip("openmmforcefields")

from mdclaw.openmm_system_server import build_openmm_system


def _hydrogenated_dipeptide(tmp_path: Path) -> Path:
    """ALA-ALA dipeptide PDB hydrogenated by PDBFixer."""
    raw = tmp_path / "diala_raw.pdb"
    raw.write_text(textwrap.dedent("""\
        ATOM      1  N   ALA A   1      -1.057   2.012   0.000  1.00  0.00           N
        ATOM      2  CA  ALA A   1       0.000   1.012   0.000  1.00  0.00           C
        ATOM      3  C   ALA A   1       1.230   1.860   0.000  1.00  0.00           C
        ATOM      4  O   ALA A   1       1.230   3.080   0.000  1.00  0.00           O
        ATOM      5  CB  ALA A   1       0.000   0.181  -1.247  1.00  0.00           C
        ATOM      6  N   ALA A   2       2.323   1.180   0.000  1.00  0.00           N
        ATOM      7  CA  ALA A   2       3.553   2.028   0.000  1.00  0.00           C
        ATOM      8  C   ALA A   2       4.610   1.028   0.000  1.00  0.00           C
        ATOM      9  O   ALA A   2       4.396  -0.196   0.000  1.00  0.00           O
        ATOM     10  CB  ALA A   2       3.553   2.860   1.247  1.00  0.00           C
        ATOM     11  OXT ALA A   2       5.825   1.668   0.000  1.00  0.00           O
        TER
        END
        """))

    pytest.importorskip("pdbfixer")
    from pdbfixer import PDBFixer
    from openmm.app import PDBFile

    fixer = PDBFixer(filename=str(raw))
    fixer.findMissingResidues()
    fixer.findMissingAtoms()
    fixer.addMissingAtoms()
    fixer.addMissingHydrogens(7.0)
    out = tmp_path / "diala_h.pdb"
    with out.open("w") as fh:
        PDBFile.writeFile(fixer.topology, fixer.positions, fh, keepIds=True)
    return out


def test_build_openmm_system_with_amber14_xml(tmp_path):
    """Smoke test the happy path: a small protein PDB with amber14 + tip3p
    XMLs produces a valid system.xml + topology.pdb + state.xml."""
    pdb = _hydrogenated_dipeptide(tmp_path)
    out_dir = tmp_path / "topo"

    result = build_openmm_system(
        pdb_file=str(pdb),
        forcefield_xml=["amber/protein.ff14SB.xml"],
        nonbonded_method="NoCutoff",
        constraints="HBonds",
        output_dir=str(out_dir),
    )

    assert result["success"] is True, result.get("errors")
    assert result["code"] == "openmm_system_built"
    assert Path(result["system_xml"]).is_file()
    assert Path(result["topology_pdb"]).is_file()
    assert Path(result["state_xml"]).is_file()
    assert result["num_atoms"] == 23
    provenance = result["forcefield_provenance"]
    assert provenance["kind"] == "openmm_xml"
    assert "amber/protein.ff14SB.xml" in provenance["forcefield_xml"]
    assert provenance["method"]["nonbonded"] == "NoCutoff"
    assert provenance["method"]["constraints"] == "HBonds"


def test_build_openmm_system_requires_forcefield_xml(tmp_path):
    pdb = _hydrogenated_dipeptide(tmp_path)
    result = build_openmm_system(
        pdb_file=str(pdb),
        forcefield_xml=[],
        output_dir=str(tmp_path / "topo"),
    )
    assert result["success"] is False
    assert any("forcefield_xml" in e for e in result["errors"])


def test_build_openmm_system_rejects_unknown_nonbonded_method(tmp_path):
    pdb = _hydrogenated_dipeptide(tmp_path)
    result = build_openmm_system(
        pdb_file=str(pdb),
        forcefield_xml=["amber/protein.ff14SB.xml"],
        nonbonded_method="MagicMethod",
        output_dir=str(tmp_path / "topo"),
    )
    assert result["success"] is False
    assert any("nonbonded_method" in e for e in result["errors"])


def test_build_openmm_system_blocks_gb99_with_old_openmm(tmp_path, monkeypatch):
    """If a forcefield_xml name contains 'GB99' and OpenMM is < 8.0, the
    build must abort with the openmm_version_too_old code."""
    # Fake OpenMM version 7.7 by patching the openmm module.
    import openmm

    class _FakeVersion:
        full_version = "7.7.0"
        short_version = "7.7"

    monkeypatch.setattr(openmm, "version", _FakeVersion(), raising=False)

    pdb = _hydrogenated_dipeptide(tmp_path)
    result = build_openmm_system(
        pdb_file=str(pdb),
        forcefield_xml=["GB99dms.xml"],
        output_dir=str(tmp_path / "topo"),
    )
    assert result["success"] is False
    assert result.get("code") == "openmm_version_too_old"


def test_build_openmm_system_missing_pdb_returns_file_not_found(tmp_path):
    result = build_openmm_system(
        pdb_file=str(tmp_path / "does_not_exist.pdb"),
        forcefield_xml=["amber/protein.ff14SB.xml"],
        output_dir=str(tmp_path / "topo"),
    )
    assert result.get("success", False) is False


def test_build_openmm_system_default_hmr_bakes_4amu_into_system_xml(tmp_path):
    """``build_openmm_system()`` defaults to ``hmr=True`` so that the
    default downstream ``run_equilibration`` / ``run_production`` call
    (also ``hmr=True``) clears the shim's modern-system contract check.

    Verify by deserializing the saved system.xml and inspecting the H
    atom mass — under HMR every hydrogen carries 4 amu, otherwise 1.008.
    """
    from openmm import XmlSerializer
    from openmm.unit import dalton
    from openmm.app import PDBFile

    pdb = _hydrogenated_dipeptide(tmp_path)
    out_dir = tmp_path / "topo_default_hmr"

    result = build_openmm_system(
        pdb_file=str(pdb),
        forcefield_xml=["amber/protein.ff14SB.xml"],
        nonbonded_method="NoCutoff",
        constraints="HBonds",
        # No hmr=! Verifying the default propagates.
        output_dir=str(out_dir),
    )
    assert result["success"] is True, result.get("errors")

    sys_xml = Path(result["system_xml"]).read_text()
    system = XmlSerializer.deserialize(sys_xml)
    topology = PDBFile(result["topology_pdb"]).topology

    # Find a hydrogen and verify its mass is the HMR-tagged 4 amu (matches
    # build_amber_system's default contract).
    for atom in topology.atoms():
        if atom.element is not None and atom.element.symbol == "H":
            mass_amu = system.getParticleMass(atom.index).value_in_unit(dalton)
            assert abs(mass_amu - 4.0) < 0.05, (
                f"Default build_openmm_system must bake HMR into system.xml, "
                f"but H atom {atom.index} has mass {mass_amu} amu (expected ~4)."
            )
            break
    else:
        pytest.fail("Hydrogenated dipeptide topology unexpectedly has no H atoms")

    # Provenance should also surface the choice.
    method = result["forcefield_provenance"]["method"]
    assert method["hmr"] is True
    assert abs(method["hydrogen_mass_amu"] - 4.0) < 1e-9


def test_build_openmm_system_hmr_false_keeps_standard_h_mass(tmp_path):
    """Explicit ``hmr=False`` produces a System where H atoms carry the
    standard 1.008 amu — useful for non-HMR research workflows."""
    from openmm import XmlSerializer
    from openmm.unit import dalton
    from openmm.app import PDBFile

    pdb = _hydrogenated_dipeptide(tmp_path)
    result = build_openmm_system(
        pdb_file=str(pdb),
        forcefield_xml=["amber/protein.ff14SB.xml"],
        nonbonded_method="NoCutoff",
        constraints="HBonds",
        hmr=False,
        output_dir=str(tmp_path / "topo_no_hmr"),
    )
    assert result["success"] is True, result.get("errors")
    sys_xml = Path(result["system_xml"]).read_text()
    system = XmlSerializer.deserialize(sys_xml)
    topology = PDBFile(result["topology_pdb"]).topology
    for atom in topology.atoms():
        if atom.element is not None and atom.element.symbol == "H":
            mass_amu = system.getParticleMass(atom.index).value_in_unit(dalton)
            assert abs(mass_amu - 1.008) < 0.05
            break
    assert result["forcefield_provenance"]["method"]["hmr"] is False


def test_build_openmm_system_pdb_file_required_validation_error(tmp_path):
    """Calling without ``pdb_file`` (and without enough DAG context to
    auto-resolve one) must return a structured validation error keyed by
    ``code="missing_pdb_file"``, not a confusing "file None not found"
    file-not-found error. (Review fix 4 of openmmforcefields-unification.)"""
    result = build_openmm_system(
        pdb_file=None,
        forcefield_xml=["amber/protein.ff14SB.xml"],
        output_dir=str(tmp_path / "topo"),
    )
    assert result.get("success", False) is False
    assert result.get("code") == "missing_pdb_file"
    assert any("pdb_file" in e for e in result.get("errors", []))


# ----------------------------------------------------------------------------
# Node-mode regression tests (Bug 2 of openmmforcefields-unification)
# ----------------------------------------------------------------------------


class TestBuildOpenmmSystemNodeMode:
    """In node mode, build_openmm_system must:
      - write outputs under ``job_dir/nodes/<node_id>/artifacts/``
      - call ``begin_node()`` so the node enters ``running``
      - auto-resolve ``pdb_file`` from the prep ancestor when not provided
      - mark the node ``failed`` (never leave it ``running``) on validation
        / build error so the DAG never sees a half-built artifact
    """

    def _setup_topo_node(self, tmp_path):
        """Build a (source -> prep -> topo) DAG with a hydrogenated dipeptide
        merged.pdb on the prep node, then return the topo node id."""
        from mdclaw._node import complete_node, create_node

        pdb = _hydrogenated_dipeptide(tmp_path)
        job_dir = tmp_path / "job"

        create_node(str(job_dir), "source")
        complete_node(
            str(job_dir),
            "source_001",
            artifacts={"structure_file": str(pdb)},
        )
        create_node(str(job_dir), "prep", parent_node_ids=["source_001"])
        prep_artifacts = job_dir / "nodes" / "prep_001" / "artifacts"
        prep_artifacts.mkdir(parents=True, exist_ok=True)
        merged = prep_artifacts / "merged.pdb"
        merged.write_bytes(pdb.read_bytes())
        complete_node(
            str(job_dir),
            "prep_001",
            artifacts={"merged_pdb": "artifacts/merged.pdb"},
        )
        create_node(str(job_dir), "topo", parent_node_ids=["prep_001"])
        return job_dir, "topo_001"

    def test_node_mode_writes_artifacts_under_node_dir(self, tmp_path):
        from mdclaw._node import read_node

        job_dir, topo_id = self._setup_topo_node(tmp_path)

        result = build_openmm_system(
            job_dir=str(job_dir),
            node_id=topo_id,
            forcefield_xml=["amber/protein.ff14SB.xml"],
            nonbonded_method="NoCutoff",
            constraints="HBonds",
        )

        assert result["success"] is True, result.get("errors")
        # Outputs under the node's own artifacts dir, not WORKING_DIR/openmm_system_*
        node_artifacts = job_dir / "nodes" / topo_id / "artifacts"
        for key in ("system_xml", "topology_pdb", "state_xml"):
            recorded = Path(result[key])
            assert recorded.is_file(), f"{key} not written: {recorded}"
            assert node_artifacts in recorded.parents, (
                f"{key} written outside the node artifacts dir: {recorded}"
            )

        # Node transitioned to completed and the relative artifact paths exist.
        topo_node = read_node(str(job_dir), topo_id)
        assert topo_node["status"] == "completed"
        for key in ("system_xml", "topology_pdb", "state_xml"):
            rel = topo_node["artifacts"].get(key)
            assert rel and (node_artifacts / Path(rel).name).is_file()

    def test_node_mode_auto_resolves_pdb_from_prep(self, tmp_path):
        """When pdb_file is omitted, build_openmm_system must look up
        merged_pdb on the prep ancestor through resolve_node_inputs."""
        job_dir, topo_id = self._setup_topo_node(tmp_path)

        result = build_openmm_system(
            job_dir=str(job_dir),
            node_id=topo_id,
            forcefield_xml=["amber/protein.ff14SB.xml"],
            nonbonded_method="NoCutoff",
        )

        assert result["success"] is True, result.get("errors")

    def test_node_mode_honors_topo_hmr_condition_match(self, tmp_path):
        """When the topo node was created with ``conditions={"hmr": True}``,
        a ``build_openmm_system(... hmr=True)`` invocation must satisfy the
        condition and complete the node. Mirrors build_amber_system's
        contract so research-mode and curated builders behave identically
        under DAG validation."""
        from mdclaw._node import create_node, read_node

        job_dir, _ = self._setup_topo_node(tmp_path)
        # Replace the conditions-free topo with one declaring hmr=True.
        # _setup_topo_node already created topo_001; we drop it and
        # re-create with conditions on a fresh prep parent.
        new_topo = create_node(
            str(job_dir),
            "topo",
            parent_node_ids=["prep_001"],
            conditions={"hmr": True},
        )
        assert new_topo["success"] is True
        topo_id = new_topo["node_id"]

        result = build_openmm_system(
            job_dir=str(job_dir),
            node_id=topo_id,
            forcefield_xml=["amber/protein.ff14SB.xml"],
            nonbonded_method="NoCutoff",
            constraints="HBonds",
            hmr=True,
        )
        assert result["success"] is True, result.get("errors")
        assert read_node(str(job_dir), topo_id)["status"] == "completed"

    def test_node_mode_blocks_topo_hmr_condition_mismatch(self, tmp_path):
        """Conversely, a topo node declaring ``conditions={"hmr": True}``
        must reject a ``build_openmm_system(... hmr=False)`` call up front
        — before the OpenMM build runs and before the node flips out of
        ``pending``."""
        from mdclaw._node import create_node, read_node

        job_dir, _ = self._setup_topo_node(tmp_path)
        new_topo = create_node(
            str(job_dir),
            "topo",
            parent_node_ids=["prep_001"],
            conditions={"hmr": True},
        )
        assert new_topo["success"] is True
        topo_id = new_topo["node_id"]

        result = build_openmm_system(
            job_dir=str(job_dir),
            node_id=topo_id,
            forcefield_xml=["amber/protein.ff14SB.xml"],
            nonbonded_method="NoCutoff",
            constraints="HBonds",
            hmr=False,
        )
        assert result.get("success", False) is False
        assert any("condition" in e.lower() for e in result.get("errors", [])), (
            f"Expected condition-mismatch error, got: {result.get('errors')!r}"
        )
        # Node must end up failed, never completed with the wrong hmr.
        node_status = read_node(str(job_dir), topo_id)["status"]
        assert node_status in {"failed", "pending"}, (
            f"After mismatch the node must NOT be completed; got {node_status!r}"
        )

    def test_node_mode_marks_node_failed_on_invalid_input(self, tmp_path):
        """forcefield_xml empty under node mode: node ends up failed, not
        stuck in ``running`` (build_openmm_system must run the failure
        through fail_node)."""
        from mdclaw._node import read_node

        job_dir, topo_id = self._setup_topo_node(tmp_path)
        result = build_openmm_system(
            job_dir=str(job_dir),
            node_id=topo_id,
            forcefield_xml=[],  # invalid
            nonbonded_method="NoCutoff",
        )
        assert result.get("success", False) is False
        topo_node = read_node(str(job_dir), topo_id)
        assert topo_node["status"] == "failed", (
            f"Node should be failed, got {topo_node['status']!r}"
        )
