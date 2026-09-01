"""Regression coverage for source chains split into separate range pieces."""

from mdclaw.structure.disulfide import _reconcile_cyx_cys_in_pdb
from mdclaw.structure.phosphorylation import (
    _build_source_to_merged_chain_map,
    _build_source_to_topology_index_map,
    _remap_detected_ptm_chains,
    _remap_disulfide_chains,
)


def _range(start: int, end: int) -> dict:
    return {
        "range": f"A:{start}-{end}",
        "start": start,
        "start_icode": "",
        "end": end,
        "end_icode": "",
    }


def _cys_sg(serial: int, chain: str, resnum: int) -> str:
    return (
        f"ATOM  {serial:>5} {'SG':<4} {'CYS':>3} {chain}{resnum:>4}    "
        "   0.000   0.000   0.000  1.00 20.00            S\n"
    )


def test_two_range_pieces_remap_sites_and_cyx_to_their_merged_chains(tmp_path):
    first_range = _range(16, 214)
    second_range = _range(380, 458)
    chain_file_info = [
        {
            "chain_id": "A",
            "author_chain": "A",
            "file": "/x/A_16_214.pdb",
            "residue_range": first_range,
        },
        {
            "chain_id": "A",
            "author_chain": "A",
            "file": "/x/A_380_458.pdb",
            "residue_range": second_range,
        },
    ]
    proteins = [
        {
            "success": True,
            "chain_id": "A",
            "input_file": "/x/A_16_214.pdb",
            "output_file": "/x/A_16_214.amber.pdb",
            "residue_range": first_range,
        },
        {
            "success": True,
            "chain_id": "A",
            "input_file": "/x/A_380_458.pdb",
            "output_file": "/x/A_380_458.amber.pdb",
            "residue_range": second_range,
        },
    ]
    chain_map = _build_source_to_merged_chain_map(
        chain_file_info,
        proteins,
        {
            "/x/A_16_214.amber.pdb": {"A": "A"},
            "/x/A_380_458.amber.pdb": {"A": "B"},
        },
    )
    topology_map = _build_source_to_topology_index_map(
        chain_file_info,
        proteins,
        [
            {
                "source_file": "/x/A_16_214.amber.pdb",
                "topology_chain_index": 0,
            },
            {
                "source_file": "/x/A_380_458.amber.pdb",
                "topology_chain_index": 1,
            },
        ],
    )

    remapped_ptms, dropped = _remap_detected_ptm_chains(
        [{"chain": "A", "resnum": 413, "name": "SEP"}],
        chain_map,
        topology_map,
    )
    assert dropped == []
    assert remapped_ptms == [
        {
            "chain": "B",
            "original_chain": "A",
            "resnum": 413,
            "name": "SEP",
            "topology_chain_index": 1,
        }
    ]

    bonds = [
        {
            "cys1": {"chain": "A", "resnum": 96},
            "cys2": {"chain": "A", "resnum": 176},
        },
        {
            "cys1": {"chain": "A", "resnum": 413},
            "cys2": {"chain": "A", "resnum": 416},
        },
    ]
    _remap_disulfide_chains(bonds, chain_map)
    assert [
        (bond["cys1"]["chain"], bond["cys2"]["chain"])
        for bond in bonds
    ] == [("A", "A"), ("B", "B")]

    merged = tmp_path / "merged.pdb"
    merged.write_text(
        _cys_sg(1, "A", 96)
        + _cys_sg(2, "A", 176)
        + _cys_sg(3, "B", 413)
        + _cys_sg(4, "B", 416)
        + "END\n"
    )
    reconciliation = _reconcile_cyx_cys_in_pdb(str(merged), bonds)
    assert reconciliation["unresolved_endpoints"] == []
    assert reconciliation["renamed_to_cyx"] == 4
    cyx_sites = {
        (line[21].strip(), int(line[22:26]), line[17:20].strip())
        for line in merged.read_text().splitlines()
        if line.startswith("ATOM")
    }
    assert cyx_sites == {
        ("A", 96, "CYX"),
        ("A", 176, "CYX"),
        ("B", 413, "CYX"),
        ("B", 416, "CYX"),
    }


def test_joined_range_group_maps_each_piece_to_the_same_merged_chain():
    first_range = _range(29, 173)
    second_range = _range(183, 227)
    third_range = _range(365, 443)
    chain_file_info = [
        {
            "chain_id": "A",
            "author_chain": "A",
            "file": "/x/A_joined.pdb",
            "residue_ranges": [first_range, second_range],
        },
        {
            "chain_id": "A",
            "author_chain": "A",
            "file": "/x/A_365_443.pdb",
            "residue_range": third_range,
        },
    ]
    proteins = [
        {
            "success": True,
            "chain_id": "A",
            "input_file": "/x/A_joined.pdb",
            "output_file": "/x/A_joined.amber.pdb",
            "residue_ranges": [first_range, second_range],
        },
        {
            "success": True,
            "chain_id": "A",
            "input_file": "/x/A_365_443.pdb",
            "output_file": "/x/A_365_443.amber.pdb",
            "residue_range": third_range,
        },
    ]
    chain_map = _build_source_to_merged_chain_map(
        chain_file_info,
        proteins,
        {
            "/x/A_joined.amber.pdb": {"A": "A"},
            "/x/A_365_443.amber.pdb": {"A": "B"},
        },
    )
    topology_map = _build_source_to_topology_index_map(
        chain_file_info,
        proteins,
        [
            {"source_file": "/x/A_joined.amber.pdb", "topology_chain_index": 0},
            {"source_file": "/x/A_365_443.amber.pdb", "topology_chain_index": 1},
        ],
    )

    bonds = [
        {
            "cys1": {"chain": "A", "resnum": 106},
            "cys2": {"chain": "A", "resnum": 188},
        }
    ]
    _remap_disulfide_chains(bonds, chain_map)

    assert bonds[0]["cys1"]["chain"] == "A"
    assert bonds[0]["cys2"]["chain"] == "A"
    remapped_ptms, dropped = _remap_detected_ptm_chains(
        [{"chain": "A", "resnum": 400, "name": "SEP"}],
        chain_map,
        topology_map,
    )
    assert dropped == []
    assert remapped_ptms[0]["chain"] == "B"
    assert remapped_ptms[0]["topology_chain_index"] == 1
