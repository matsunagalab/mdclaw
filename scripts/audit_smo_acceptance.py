"""Independent raw PDB/System/State/DCD audit for the opt-in SMO acceptance."""

import argparse
from collections import Counter, OrderedDict
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np

PROTEIN = set(
    "ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL HID HIE HIP ASH GLH CYX CYM LYN".split()
)


def atoms(path):
    return [
        line
        for line in Path(path).read_text().splitlines()
        if line.startswith(("ATOM  ", "HETATM"))
    ]


def residues(rows):
    out = OrderedDict()
    for index, line in enumerate(rows):
        name = line[17:20].strip()
        if name in PROTEIN:
            site = (line[21].strip(), line[22:26].strip(), line[26].strip())
            out.setdefault(site, {"name": name, "atoms": {}})["atoms"][line[12:16].strip()] = index
    return out


def state(path):
    root = ET.parse(path).getroot()
    xyz = np.array([[float(p.get(k)) for k in ("x", "y", "z")] for p in root.find("Positions")])
    box = np.array(
        [[float(p.get(k)) for k in ("x", "y", "z")] for p in root.find("PeriodicBoxVectors")]
    )
    assert np.isfinite(xyz).all() and np.isfinite(box).all()
    return xyz, box


def distances(xyz, box, pairs):
    delta = xyz[pairs[:, 0]] - xyz[pairs[:, 1]]
    frac = delta @ np.linalg.inv(box)
    return np.linalg.norm((frac - np.round(frac)) @ box, axis=-1) * 10


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", type=Path)
    args = parser.parse_args()
    study = args.study
    fixture = json.loads((study / "fixture.json").read_text())
    job = study / "jobs/main"
    nodes = json.loads((study / "validation/stage_nodes.json").read_text())

    def artifact(kind, key):
        directory = job / "nodes" / nodes[kind]
        node = json.loads((directory / "node.json").read_text())
        assert node["status"] == "completed"
        return directory / node["artifacts"][key]

    prep = job / "nodes" / fixture["prep_id"] / "artifacts/merge/merged.pdb"
    original = residues(atoms(prep))
    topfile = artifact("topo", "topology_pdb")
    rows = atoms(topfile)
    protein = residues(rows)
    assert len(protein) == len(original) == 476
    assert {k: v["name"] for k, v in protein.items()} == {k: v["name"] for k, v in original.items()}
    aliases = []
    for key in original:
        before, after = set(original[key]["atoms"]), set(protein[key]["atoms"])
        assert {a for a in before if not a.startswith("H")} == {
            a for a in after if not a.startswith("H")
        }
        if before != after:
            assert before - after == {"H1"} and after - before == {"H"}
            assert key == next(k for k in original if k[0] == key[0])
            aliases.append(
                {
                    "site": key,
                    "before": "H1",
                    "after": "H",
                    "reason": "OpenMM terminal hydrogen alias",
                }
            )
    plans = json.loads(
        (job / "nodes" / fixture["prep_id"] / "artifacts/disulfide_bonds.json").read_text()
    )
    ss = []
    for pair in plans:
        sites = [
            (str(pair[k]["chain"]), str(pair[k]["resnum"]), str(pair[k].get("icode", "")))
            for k in ("cys1", "cys2")
        ]
        ss.append([protein[site]["atoms"]["SG"] for site in sites])
    ss = np.array(ss)
    backbone = []
    items = list(protein.items())
    for (left, one), (right, two) in zip(items, items[1:]):
        if left[0] == right[0]:
            backbone.append([one["atoms"]["C"], two["atoms"]["N"]])
    backbone = np.array(backbone)
    root = ET.parse(artifact("topo", "system_xml")).getroot()
    nb = next(f for f in root.find("Forces") if f.get("type") == "NonbondedForce")
    charges = np.array([float(p.get("q")) for p in nb.find("Particles")])
    assert abs(charges.sum()) < 0.001
    system_bonds = set()
    for force in root.find("Forces"):
        if force.get("type") == "HarmonicBondForce":
            system_bonds |= {
                tuple(sorted((int(b.get("p1")), int(b.get("p2"))))) for b in force.find("Bonds")
            }
    system_bonds |= {
        tuple(sorted((int(b.get("p1")), int(b.get("p2"))))) for b in root.find("Constraints")
    }
    assert all(tuple(sorted(p)) in system_bonds for p in ss)
    assert all(tuple(sorted(p)) in system_bonds for p in backbone)
    sg_indices = {v["atoms"]["SG"] for v in protein.values() if "SG" in v["atoms"]}
    assert len([p for p in system_bonds if set(p) <= sg_indices]) == 9
    report = {
        "protein_residues": len(protein),
        "protein_names": dict(Counter(v["name"] for v in protein.values())),
        "allowed_atom_aliases": aliases,
        "atoms": len(rows),
        "system_charge_e": float(charges.sum()),
        "disulfide_pairs": ss.tolist(),
        "backbone_bonds": len(backbone),
        "stages": {},
        "trajectory_frames": 0,
    }
    coordinates = {}
    for kind, key in [("min", "state"), ("eq", "state")]:
        xyz, box = state(artifact(kind, key))
        assert len(xyz) == len(rows) == len(charges)
        sd = distances(xyz, box, ss)
        cd = distances(xyz, box, backbone)
        assert sd.min() >= 1.8 and sd.max() <= 2.4, (kind, sd)
        assert cd.min() >= 1.15 and cd.max() <= 1.7, (kind, cd)
        report["stages"][kind] = {
            "ss_angstrom": [float(sd.min()), float(sd.max())],
            "backbone_cn_angstrom": [float(cd.min()), float(cd.max())],
            "box_volume_nm3": float(np.linalg.det(box)),
        }
        coordinates[kind] = (xyz, box)
    series = {}
    for stage in ("nvt", "npt"):
        data = np.loadtxt(artifact("eq", stage + "_energy"), delimiter=",", skiprows=1)
        assert np.isfinite(data).all()
        tail = data[-max(1, len(data) // 10) :]
        # Reporter columns: step,time,potential,kinetic,total,temperature,volume,density.
        report[stage] = {
            "last_step": int(data[-1, 0]),
            "temperature_last10_K": float(tail[:, 5].mean()),
            "volume_last10_nm3": float(tail[:, 6].mean()),
            "density_last10_g_ml": float(tail[:, 7].mean()),
        }
        series[stage] = data
    eq_result = json.loads(
        next((study / "validation").glob("cli-*-run_equilibration.json")).read_text()
    )
    warmup = eq_result["low_temperature_warmup_steps"]
    report["nvt"]["warmup_steps"] = warmup
    assert report["nvt"]["last_step"] - warmup == 50000
    assert report["npt"]["last_step"] == 1000000
    assert 290 <= report["npt"]["temperature_last10_K"] <= 310
    assert 0.95 <= report["npt"]["density_last10_g_ml"] <= 1.15
    telemetry = study / "telemetry"
    if telemetry.exists():
        import mdtraj as md

        report["telemetry"] = []
        for meta in telemetry.glob("*.json"):
            r = json.loads(meta.read_text())
            p = meta.with_suffix(".dcd")
            xyz, box = state(meta.with_suffix(".xml"))
            sd = distances(xyz, box, ss)
            cd = distances(xyz, box, backbone)
            assert sd.min() >= 1.8 and sd.max() <= 2.4
            assert cd.min() >= 1.15 and cd.max() <= 1.7
            bounds = [float("inf"), 0.0, float("inf"), 0.0]
            count = 0
            if p.exists() and p.stat().st_size:
                for chunk in md.iterload(str(p), top=str(topfile), chunk=10):
                    for xyz, box in zip(chunk.xyz, chunk.unitcell_vectors):
                        assert np.isfinite(xyz).all()
                        sd = distances(xyz, box, ss)
                        cd = distances(xyz, box, backbone)
                        bounds = [
                            min(bounds[0], float(sd.min())),
                            max(bounds[1], float(sd.max())),
                            min(bounds[2], float(cd.min())),
                            max(bounds[3], float(cd.max())),
                        ]
                        count += 1
                assert (
                    bounds[0] >= 1.8 and bounds[1] <= 2.4 and bounds[2] >= 1.15 and bounds[3] <= 1.7
                ), bounds
            r.update(frames=count, ss_angstrom=bounds[:2], backbone_cn_angstrom=bounds[2:])
            report["telemetry"].append(r)
            report["trajectory_frames"] += count
    report["status"] = "passed"
    out = study / "validation"
    (out / "independent-audit.json").write_text(json.dumps(report, indent=2))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(12, 3.4))
    data = series["npt"]
    for ax, index, label in zip(
        axes, (5, 6, 7), ("Temperature (K)", "Volume (nm³)", "Density (g/mL)")
    ):
        ax.plot(data[:, 1] / 1000, data[:, index])
        ax.set(xlabel="NPT time (ns)", ylabel=label)
    fig.tight_layout()
    fig.savefig(out / "npt-statistics.png", dpi=180)
    plt.close(fig)
    headgroup = [
        i
        for i, line in enumerate(rows)
        if line[17:20].strip() == "PC" and line[76:78].strip() == "P"
    ]
    assert len(headgroup) == 221, len(headgroup)
    ca = [v["atoms"]["CA"] for v in protein.values()]
    fig, axes = plt.subplots(2, 2, figsize=(8, 8))
    for column, (kind, (xyz, box)) in enumerate(coordinates.items()):
        for row, (u, v, label) in enumerate(((0, 1, "top"), (0, 2, "side"))):
            ax = axes[row, column]
            ax.scatter(xyz[headgroup, u], xyz[headgroup, v], s=9, alpha=0.6, label="Lipid P")
            ax.scatter(xyz[ca, u], xyz[ca, v], s=9, label="Protein CA")
            ax.set(
                title=f"{kind}: {label}",
                xlabel="x (nm)",
                ylabel=("y" if v == 1 else "z") + " (nm)",
                aspect="equal",
            )
    axes[0, 0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "membrane-views.png", dpi=180)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
