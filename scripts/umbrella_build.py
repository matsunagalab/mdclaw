#!/usr/bin/env python
"""Create the umbrella window nodes and the array task list for both states.

One window is one `prod` node branching from the state's completed `eq` node,
run with scripts/umbrella_bias_tas1r.py. Emits the submit_array_job payload and
the MBAR manifest side by side so the two can never disagree about which CSV
belongs to which umbrella centre.
"""
import argparse
import json
import subprocess


def centres(lo, hi, step):
    n = int(round((hi - lo) / step)) + 1
    return [round(lo + i * step, 4) for i in range(n)]


def create_node(mdc, job_dir, parent, label, ns):
    out = subprocess.run(
        [mdc, "create_node", "--job-dir", job_dir, "--node-type", "prod",
         "--parent-node-ids", parent, "--label", label,
         "--conditions", json.dumps({"simulation_time_ns": ns})],
        capture_output=True, text=True, check=True).stdout
    payload = json.loads(out[out.index("{"):])
    return payload["node_id"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mdc", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--job-dir", required=True)
    ap.add_argument("--eq-node", required=True)
    ap.add_argument("--selection-json", required=True)
    ap.add_argument("--bias-script", required=True)
    ap.add_argument("--cv1", type=float, nargs=3, required=True,
                    metavar=("LO", "HI", "STEP"), help="nm")
    ap.add_argument("--cv2", type=float, nargs=3, required=True,
                    metavar=("LO", "HI", "STEP"), help="nm")
    ap.add_argument("--k1", type=float, required=True)
    ap.add_argument("--k2", type=float, required=True)
    ap.add_argument("--window-ns", type=float, required=True)
    ap.add_argument("--output-frequency-ps", type=float, required=True)
    ap.add_argument("--out-prefix", required=True)
    args = ap.parse_args()

    c1 = centres(*args.cv1)
    c2 = centres(*args.cv2)
    tasks, manifest = [], []
    for i, x in enumerate(c1):
        for j, y in enumerate(c2):
            label = f"umbrella {args.state} cv1={x:.3f} cv2={y:.3f} k={args.k1:.0f}"
            node = create_node(args.mdc, args.job_dir, args.eq_node, label,
                               args.window_ns)
            params = json.dumps({
                "selection_json": args.selection_json,
                "cv1_center_nm": x, "cv2_center_nm": y,
                "k1": args.k1, "k2": args.k2,
            })
            tasks.append({
                "job_dir": args.job_dir, "node_id": node,
                "command": (
                    f"mdclaw --job-dir {args.job_dir} --node-id {node} "
                    f"run_production --simulation-time-ns {args.window_ns} "
                    f"--temperature-kelvin 300 "
                    f"--output-frequency-ps {args.output_frequency_ps} "
                    f"--platform CUDA "
                    f"--custom-force-script {args.bias_script} "
                    f"--custom-force-parameters '{params}'"),
            })
            manifest.append({
                "window_id": f"{args.state}_{i:02d}_{j:02d}",
                "state": args.state, "node_id": node,
                "cv1_center_nm": x, "cv2_center_nm": y,
                "k1": args.k1, "k2": args.k2,
                "cv_csv": f"{args.job_dir}/nodes/{node}/artifacts/"
                          "collective_variables.csv",
            })
    json.dump(tasks, open(f"{args.out_prefix}.tasks.json", "w"), indent=1)
    json.dump(manifest, open(f"{args.out_prefix}.manifest.json", "w"), indent=1)
    print(json.dumps({"state": args.state, "n_windows": len(tasks),
                      "cv1_centers_nm": c1, "cv2_centers_nm": c2,
                      "first_node": manifest[0]["node_id"],
                      "last_node": manifest[-1]["node_id"]}))


if __name__ == "__main__":
    main()
