# MDPrepBench Weak Baselines

These baselines are reference runners that establish the benchmark's
**discrimination floor**: deliberately weak or fabricated solvers whose scores
show that MDPrepBench separates real MD-prep capability from shortcuts. They are
all MDClaw-free on the solver side (`tooling_condition="mdclaw-free"`); only the
shared scorer (run separately) uses MDClaw. Running them across the suite is
operator-driven.
