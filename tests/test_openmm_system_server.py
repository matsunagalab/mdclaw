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


# ----------------------------------------------------------------------------
# Implicit-solvent metadata contract (Phase 14)
# ----------------------------------------------------------------------------


class TestBuildOpenmmSystemImplicitSolvent:
    """``build_openmm_system`` is the research escape hatch — it does not
    silently inject XMLs the caller did not bring, but it must keep the
    same topology metadata contract as ``build_amber_system`` so the
    run-side topology guard recognises the build choice on either path.

    Three failure codes are exercised here:
      - ``implicit_solvent_xml_missing`` — declared model, but no matching
        ``implicit/<model>.xml`` in ``forcefield_xml``.
      - ``implicit_solvent_xml_ambiguous`` — multiple shipped GB XMLs
        bundled and no explicit ``implicit_solvent`` to disambiguate.
      - ``implicit_solvent_model_unsupported`` — typo / unknown model name.
    """

    @staticmethod
    def _setup_topo(tmp_path):
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
        return job_dir, "topo_001", pdb

    def test_declared_obc2_with_matching_xml_records_canonical_metadata(
        self, tmp_path
    ):
        """``forcefield_xml=[..., "implicit/gbn2.xml"]`` paired with
        ``implicit_solvent="gbneck2"`` (an alias) must canonicalize to
        ``"GBn2"`` in node metadata + provenance, and the saved
        system.xml must carry a Generalized-Born force."""
        from openmm import XmlSerializer
        from mdclaw._node import read_node

        job_dir, topo_id, _pdb = self._setup_topo(tmp_path)
        result = build_openmm_system(
            job_dir=str(job_dir),
            node_id=topo_id,
            forcefield_xml=[
                "amber/protein.ff14SBonlysc.xml",
                "implicit/gbn2.xml",
            ],
            nonbonded_method="NoCutoff",
            constraints="HBonds",
            implicit_solvent="gbneck2",  # alias of GBn2
        )
        assert result["success"] is True, result.get("errors")
        assert result["parameters"]["implicit_solvent"] == "GBn2"

        topo_node = read_node(str(job_dir), topo_id)
        meta = topo_node["metadata"]
        assert meta["implicit_solvent"] == "GBn2"
        assert meta["solvent_type"] == "implicit"
        assert meta["hmr"] is True

        prov = meta["forcefield_provenance"]
        assert prov["method"]["implicit_solvent"] == "GBn2"
        assert prov["method"]["solvent_type"] == "implicit"

        # GB force is actually present in the saved system.xml.
        with open(result["system_xml"]) as fh:
            system = XmlSerializer.deserialize(fh.read())
        gb_classes = {
            "GBSAOBCForce", "CustomGBForce", "AmoebaGeneralizedKirkwoodForce",
        }
        present = {type(f).__name__ for f in system.getForces()}
        assert present & gb_classes, (
            f"system.xml has no GB force; forces={present}"
        )

    def test_declared_model_missing_xml_fails(self, tmp_path):
        """``implicit_solvent="GBn2"`` but the GBn2 XML is not in the
        bundle: surface the structured ``implicit_solvent_xml_missing``
        code with rebuild guidance, and never reach SystemGenerator."""
        from mdclaw._node import read_node

        job_dir, topo_id, _pdb = self._setup_topo(tmp_path)
        result = build_openmm_system(
            job_dir=str(job_dir),
            node_id=topo_id,
            forcefield_xml=["amber/protein.ff14SBonlysc.xml"],
            nonbonded_method="NoCutoff",
            constraints="HBonds",
            implicit_solvent="GBn2",
        )
        assert result.get("success", False) is False
        assert result.get("code") == "implicit_solvent_xml_missing"
        joined = " ".join(result.get("errors", []))
        assert "implicit/gbn2.xml" in joined.lower() or "GBn2" in joined
        topo_node = read_node(str(job_dir), topo_id)
        assert topo_node["status"] == "failed"

    def test_unknown_declared_model_fails(self, tmp_path):
        """Typos like ``MAGIC_GB`` get caught with the same code the run
        side uses, so the failure mode is consistent across the build /
        run sides."""
        pdb = _hydrogenated_dipeptide(tmp_path)
        result = build_openmm_system(
            pdb_file=str(pdb),
            forcefield_xml=["amber/protein.ff14SB.xml"],
            nonbonded_method="NoCutoff",
            constraints="HBonds",
            implicit_solvent="MAGIC_GB",
            output_dir=str(tmp_path / "topo"),
        )
        assert result.get("success", False) is False
        assert result.get("code") == "implicit_solvent_model_unsupported"
        joined = " ".join(result.get("errors", []))
        assert "MAGIC_GB" in joined
        # Supported list surfaces so agents can recover.
        assert "OBC2" in joined and "GBn2" in joined

    def test_inferred_obc2_from_xml_bundle(self, tmp_path):
        """When ``implicit_solvent`` is omitted but the bundle ships a
        single ``implicit/<model>.xml``, the canonical model is recorded
        on node metadata so the run-side topology guard recognises it."""
        from mdclaw._node import read_node

        job_dir, topo_id, _pdb = self._setup_topo(tmp_path)
        result = build_openmm_system(
            job_dir=str(job_dir),
            node_id=topo_id,
            forcefield_xml=[
                "amber/protein.ff14SBonlysc.xml",
                "implicit/obc2.xml",
            ],
            nonbonded_method="NoCutoff",
            constraints="HBonds",
        )
        assert result["success"] is True, result.get("errors")
        assert result["parameters"]["implicit_solvent"] == "OBC2"
        meta = read_node(str(job_dir), topo_id)["metadata"]
        assert meta["implicit_solvent"] == "OBC2"
        assert meta["solvent_type"] == "implicit"

    def test_ambiguous_xml_fails(self, tmp_path):
        """Two shipped ``implicit/*.xml`` files in the same bundle without
        an explicit ``implicit_solvent`` is irresolvable — both could not
        be the active GB model. Surface as
        ``implicit_solvent_xml_ambiguous``."""
        from mdclaw._node import read_node

        job_dir, topo_id, _pdb = self._setup_topo(tmp_path)
        result = build_openmm_system(
            job_dir=str(job_dir),
            node_id=topo_id,
            forcefield_xml=[
                "amber/protein.ff14SBonlysc.xml",
                "implicit/obc2.xml",
                "implicit/gbn2.xml",
            ],
            nonbonded_method="NoCutoff",
            constraints="HBonds",
        )
        assert result.get("success", False) is False
        assert result.get("code") == "implicit_solvent_xml_ambiguous"
        topo_node = read_node(str(job_dir), topo_id)
        assert topo_node["status"] == "failed"

    def test_no_implicit_xml_means_explicit_or_vacuum(self, tmp_path):
        """No shipped GB XML present → metadata.implicit_solvent stays
        ``None``; ``solvent_type`` reflects the nonbonded regime
        (``vacuum`` for NoCutoff, ``explicit`` for PME). The run-side
        topology guard then skips the implicit-solvent check."""
        from mdclaw._node import read_node

        job_dir, topo_id, _pdb = self._setup_topo(tmp_path)
        result = build_openmm_system(
            job_dir=str(job_dir),
            node_id=topo_id,
            forcefield_xml=["amber/protein.ff14SB.xml"],
            nonbonded_method="NoCutoff",
            constraints="HBonds",
        )
        assert result["success"] is True, result.get("errors")
        meta = read_node(str(job_dir), topo_id)["metadata"]
        assert meta["implicit_solvent"] is None
        assert meta["solvent_type"] == "vacuum"

    def test_topology_metadata_passes_run_side_guard_with_alias(self, tmp_path):
        """Integration: a topo node built via build_openmm_system with
        ``implicit_solvent="GBn2"`` must let ``run_production
        --implicit-solvent gbneck2`` past the topology mismatch guard.
        The simulation will fail later because the placeholder
        deserialization step does not run a real System, but the
        specific assertion is that the failure code is *not*
        ``implicit_solvent_topology_mismatch``."""
        from mdclaw._node import create_node, read_node
        from mdclaw.md_simulation_server import run_production

        job_dir, topo_id, _pdb = self._setup_topo(tmp_path)
        result = build_openmm_system(
            job_dir=str(job_dir),
            node_id=topo_id,
            forcefield_xml=[
                "amber/protein.ff14SBonlysc.xml",
                "implicit/gbn2.xml",
            ],
            nonbonded_method="NoCutoff",
            constraints="HBonds",
            implicit_solvent="GBn2",
        )
        assert result["success"] is True, result.get("errors")
        # eq + prod nodes downstream so run_production can resolve the
        # topo metadata. The eq state is a placeholder so the run will
        # fail at deserialization — we only care about the guard verdict.
        from mdclaw._node import complete_node
        create_node(str(job_dir), "eq", parent_node_ids=[topo_id])
        eq_artifacts = job_dir / "nodes" / "eq_001" / "artifacts"
        eq_artifacts.mkdir(parents=True, exist_ok=True)
        (eq_artifacts / "equilibrated.xml").write_text("<placeholder/>")
        complete_node(
            str(job_dir),
            "eq_001",
            artifacts={"state": "artifacts/equilibrated.xml"},
            metadata={"final_step": 0},
        )
        create_node(str(job_dir), "prod", parent_node_ids=["eq_001"])

        prod_result = run_production(
            simulation_time_ns=0.001,
            implicit_solvent="gbneck2",  # alias of GBn2 — must canonicalize
            pressure_bar=0,
            job_dir=str(job_dir),
            node_id="prod_001",
        )
        # prod_001 will fail later (placeholder system.xml is unparseable),
        # but specifically NOT with the topology-mismatch code.
        assert prod_result.get("code") != "implicit_solvent_topology_mismatch"
        # Sanity: the topo node carries the canonical metadata that fed
        # the guard.
        topo_meta = read_node(str(job_dir), topo_id)["metadata"]
        assert topo_meta["implicit_solvent"] == "GBn2"
