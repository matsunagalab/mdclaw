# Equilibration node is one stage

MDClaw treats each Equilibration Node as one ensemble/stage condition, such as NVT or NPT, rather than one call that can silently bundle several equilibration stages. We removed the old combined NVT/NPT duration and step fields in favor of `stage`, `stage_time_ns`, and `stage_steps` so node identity, restart evidence, and downstream production handoff all refer to one physically clear stage; multi-stage protocols are represented by chaining eq nodes.
