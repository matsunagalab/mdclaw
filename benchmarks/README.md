# Benchmarks

| Suite | Location | Version | Focus |
|---|---|---|---|
| MDPrepBench | [matsunagalab/MDPrepBench](https://github.com/matsunagalab/MDPrepBench) | `MDPrepBench-v0.3` | MD system preparation; extracted to its own repository. |
| MDStudyBench | `benchmarks/mdstudybench/` | `MDStudyBench-v0.4` | Scientific question answering with runner-certified confirmatory MD. |

MDStudyBench task contracts are generated from
`benchmarks/mdstudybench/task_specs/`; edit the specs and run
`python benchmarks/mdstudybench/scripts/generate_tasks.py`, never the generated
`tasks/*/task.json`. See `docs/benchmark/mdstudybench.md` for the evaluation
contract.

`benchmarks/tools/` keeps the shared batch runner
(`run_mdprepbench_all_agents.py`, canonical copy in the MDPrepBench repository)
that the MDStudyBench wrapper builds on, plus submission validation and
packaging helpers.
