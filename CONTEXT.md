# MDClaw Context

MDClaw describes scientific molecular-dynamics work as studies made of jobs,
where each job records one source-rooted workflow and its evidence.

## Language

**Study**:
A scientific investigation frame that may compare one or more Jobs under a shared question, MD goal, analysis intent, and decision criteria. A Study is the normal context for new scientific work, even when the software can run a simple Job by itself.
_Avoid_: project, campaign, experiment folder

**Job**:
A source-rooted workflow for one structural Source Bundle, including all branches derived from that source. A Job is not one production run and is not a cluster submission.
_Avoid_: run, SLURM job, task

**Source Bundle**:
The structural input set at the root of a Job. A Source Bundle may contain multiple Candidates only when they come from the same acquisition intent and represent the same system identity, such as NMR models, assembly candidates, or ranked predictions from one prediction input.
_Avoid_: input file, structure folder, source root

**Source Node**:
A Node that acquires structural input, records the Source Bundle, and normalizes downstream-selectable Candidates. Raw downloaded, copied, or generated files are provenance; Candidates are what Preparation Nodes select.
_Avoid_: downloader, raw file node

**System Identity**:
The scientific identity of the molecular system being considered as one source, including its origin, construct, and intended biological state. Different experimental structures, prediction inputs, homologs, or constructs usually have different System Identities.
_Avoid_: filename, PDB code when biological identity is what matters

**Candidate**:
One selectable structure within a Source Bundle, such as an NMR model, assembly option, prediction member, or normalized downloaded structure.
_Avoid_: model when it means the whole source, structure when selection matters

**Node**:
One recorded workflow step within a Job, such as source, Preparation, Solvation, Topology, Equilibration, Production, or Analysis. A Node may be attempted again only when its declared scientific choices and run conditions stay the same. Once completed, its node.json scientific record is sealed.
_Avoid_: one-off attempt, step when the recorded state matters, job

**Preparation Node**:
A Node that selects one Candidate, chooses MD-relevant molecular components, cleans and standardizes them, records chemistry and provenance needed for topology building, and produces a Prepared System.
_Avoid_: prep as the formal design term

**Solvation Node**:
A Node that produces a Solvated System from a Prepared System.
_Avoid_: solv as the formal design term

**Topology Node**:
A Node that applies force-field, template, and parameter choices to a Prepared System or Solvated System and produces a Topology.
_Avoid_: topo as the formal design term

**Equilibration Node**:
A Node that records one equilibration ensemble or stage condition. A setup minimization or warmup that prepares that stage may be included, but distinct equilibration stages such as NVT and NPT should be separate Equilibration Nodes.
_Avoid_: eq as the formal design term

**Production Node**:
A Node that records one Production Segment. A continuation creates a new Production Node, not another Attempt of the previous one.
_Avoid_: prod as the formal design term

**Analysis Node**:
A Node that consumes Production Segment, Production Chain, or Analysis artifacts to produce derived scientific evidence. Its Analysis Data Scope must be clear because segment-only, production-chain-level, and comparison analyses answer different questions.
_Avoid_: analyze as the formal design term

**Condition**:
A declared scientific or execution choice that defines a Node's identity, such as candidate selection, ligand selection, force field, solvent model, ensemble, timestep, simulation length, random seed, restraint choice, or analysis metric. Runtime bookkeeping such as hostnames, scheduler IDs, timestamps, log paths, and elapsed time are not Conditions.
_Avoid_: runtime metadata, log detail, scheduler field

**Attempt**:
One try to carry out a Node. A transient execution failure may lead to another Attempt of the same Node, but changing the scientific choice or run condition creates a new Node or Branch.
_Avoid_: node, retry when the identity changed

**Branch**:
A derived path within a Job that explores a variant after a concrete Candidate has been selected or prepared. Branches may start from preparation, solvation, topology, equilibration, production, or analysis, but a new Source Bundle means a new Job.
_Avoid_: separate job when the same Source Bundle remains the root, branch when the source identity changed

**Physical System**:
The molecular system as a target for MD time evolution, including the chosen molecular components, coordinates, solvent or box state, and force-field parameters. A raw structure or Candidate is not yet a Physical System.
_Avoid_: candidate, raw structure, source

**Prepared System**:
A selected, cleaned, component-processed, and merged structure set derived from a Candidate before force-field application. A Prepared System is ready for solvation or topology building, but equilibration and production do not consume it directly.
_Avoid_: topology, physical system when force-field application matters

**Solvated System**:
A Prepared System with explicit environment components such as solvent, ions, membrane, and box state added before topology building. Implicit-solvent workflows may skip this concept and build a Topology directly from a Prepared System.
_Avoid_: topology, implicit solvent model

**Topology**:
A force-field-applied, MD-ready Physical System that equilibration and production Nodes can read without reconstructing the system.
_Avoid_: raw structure, force-field recipe, parameter source

**Topology-Time Minimization**:
A short minimization performed while creating a Topology to validate the force-field-applied system and write a consistent initial state. It is not Equilibration.
_Avoid_: equilibration, MD protocol, production warmup

**Production Segment**:
One production-MD execution recorded by one Production Node. A continuation is a new Production Segment that reads a previous segment's Artifact and writes its own Artifacts.
_Avoid_: run when it hides the recorded segment boundary, continuation attempt

**Production Chain**:
A sequence of Production Segments connected by continuation. The physical simulation timeline continues across the chain, but evidence remains owned by each segment's Production Node.
_Avoid_: single artifact, appended run, retry

**Analysis Data Scope**:
The declared extent of MD data interpreted by an Analysis Node: one Production Segment, one Production Chain, or a comparison across branches or Jobs.
_Avoid_: latest trajectory, implicit parent meaning, unscoped analysis

**Analysis Subject**:
The molecular object or relationship measured by an Analysis Node, such as one chain, a ligand, an interface, a residue range, or a whole protein selection.
_Avoid_: selection when the scientific subject is what matters

**Comparison Mapping**:
The explicit correspondence that makes comparison across different Analysis Subjects or Topologies meaningful. Initial supported mapping forms are residue-number pairs and explicit atom selections.
_Avoid_: implicit residue equivalence, same atom index assumption, automatically inferred mapping

**Artifact**:
A durable, immutable output owned by a Node and used as evidence or as input to later Nodes. Later Nodes may read an Artifact, but changing the scientific result means creating a new Artifact on a new Node.
_Avoid_: loose file, output, latest file

**Operational Event**:
An append-only observation about execution, scheduling, or agent activity around a Node. Operational Events may be added after a Node is completed, but they do not change the Node's sealed node.json scientific record.
_Avoid_: artifact, node condition, post-hoc evidence edit

**Work Hint**:
A temporary operational note used to route unfinished or retryable work, such as an agent claim or an open need for follow-up. Work Hints are not Conditions, Artifacts, or scientific evidence, and they do not remain part of a completed Node's sealed node.json record.
_Avoid_: node evidence, completed-node metadata, scientific decision

**Index**:
A mutable convenience summary that helps people or agents find current workflow state. An Index is not evidence and must not replace Node-owned Artifacts.
_Avoid_: artifact, source of truth

**Progress Index**:
The Job-level Index that summarizes Node status, lightweight workflow state, and Work Hints. The Progress Index is rebuilt from Node records when needed; it is not the authoritative scientific record.
_Avoid_: node record, evidence, artifact store

**Guardrail**:
A structured MDClaw judgment that protects safe, reproducible workflow execution. A Guardrail has a stable code for agent and skill branching; human-readable messages explain the judgment but do not define it.
_Avoid_: raw exception, log message, prose-only error

**SLURM Submission**:
A request sent to a cluster scheduler to execute work. It may be linked to MDClaw Nodes, but it is not a Job.
_Avoid_: job

## Example Dialogue

Developer: Should this apo/holo comparison be one Job?

Domain expert: If apo and holo are branches from the same Source Bundle, keep them in one Job. If they come from independent source roots, such as different PDB structures or a prediction versus an experimental structure, put them in separate Jobs under the same Study.

Developer: The production crashed. Do we create a new Job?

Domain expert: No. Create or repair a production Node within the same Job unless the source identity itself changed.

Developer: Can I rerun the failed production Node with a longer simulation time?

Domain expert: No. That changes the Node identity, so create a new production Node or Branch. Reuse the same Node only for another Attempt with the same choices and conditions.

Developer: The scheduler assigned a different job ID on retry. Is that a new Node?

Domain expert: No. A scheduler ID is runtime metadata, not a Condition. It is another Attempt of the same Node if the scientific and run choices are unchanged.

Developer: Can a continuation append to the previous production Artifact?

Domain expert: No. It reads the previous Artifact and writes a new Artifact on the continuation Node. A summary may point at the latest result, but the evidence stays Node-owned.

Developer: Is `prod_002` an extension of `prod_001` or a new Production Run?

Domain expert: It is a new Production Segment in the same Production Chain. The physical timeline continues, but the recorded evidence boundary is a new Production Node.

Developer: Should an RMSD Analysis Node on `prod_002` analyze only `prod_002`?

Domain expert: Only if its Analysis Data Scope is the segment. If the scope is the Production Chain, it should interpret the continuous trajectory through `prod_001` and `prod_002`.

Developer: Can we compare chain A and chain B if they were simulated with different Topologies?

Domain expert: Yes, but only when the Analysis Subjects and Comparison Mapping are explicit. Do not assume atom indices or residue numbers are comparable across different Topologies unless the mapping says so.

Developer: Can MDClaw infer that mapping automatically from sequence alignment?

Domain expert: Not initially. Use only an explicit mapping supplied by the user or agent, because automatic mappings can hide truncations, mutations, missing loops, and renumbering.

Developer: Should production rebuild the force field from the original structure?

Domain expert: No. Production reads the Topology produced by the topology Node; changing force-field choices means creating a new topology Node.

Developer: Does preparation decide the ligand force-field template?

Domain expert: No. Preparation records ligand chemistry and provenance. The Topology Node resolves and records the force-field or template path that makes the system MD-ready.

Developer: Is the minimization during topology building an Equilibration Node?

Domain expert: No. It is Topology-Time Minimization: a topology-building validation step that writes the initial state consumed by later MD Nodes.

Developer: Is a downloaded PDB already a Physical System?

Domain expert: No. It is a source Candidate. It becomes a Physical System only after MDClaw has selected components and applied the needed preparation, solvation, and topology choices.

Developer: Is the merged prepared PDB a Topology?

Domain expert: No. It is a Prepared System. The Topology exists only after force-field application produces the MD-ready Physical System consumed by equilibration and production.

Developer: Where does implicit solvent fit?

Domain expert: It usually skips a Solvated System and creates a Topology directly from the Prepared System with the implicit-solvent choice as a Condition.

Developer: The Progress Index and a Node record disagree. Which one do we trust?

Domain expert: Trust the Node record and its Artifacts, then rebuild the Progress Index from them.

Developer: The tool returned a long error message. Should the skill parse it?

Domain expert: No. It should branch on the Guardrail code and use the message only to explain the situation to the user.

Developer: Do we need a Study for a quick single-system run?

Domain expert: It can run as one Job, but a Study is still the usual place to record why that Job exists and how its result will be interpreted.

Developer: The cluster job failed.

Domain expert: Say the SLURM Submission failed. Then inspect the linked MDClaw Node to see whether the workflow evidence should be marked failed or retried through a new branch.
