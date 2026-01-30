"""Prompt loader for MDZen agents.

Prompts are stored as separate .md files in the prompts/ directory.
This allows non-engineers to edit prompts without touching Python code.

Directory structure:
    prompts/
    ├── clarification.md  # Phase 1: Requirements gathering
    ├── setup.md          # Phase 2: MD workflow execution (full)
    ├── validation.md     # Phase 3: QC and report generation
    └── steps/            # Step-specific prompts (Best Practice #3)
        ├── prepare_complex.md
        ├── solvate.md
        ├── build_topology.md
        └── run_simulation.md
"""

from functools import lru_cache
from pathlib import Path

from mdzen.utils import get_today_str

# Prompts directory location
PROMPTS_DIR = Path(__file__).parent / "prompts"


@lru_cache(maxsize=None)
def _load_prompt(filename: str) -> str:
    """Load a prompt file from the prompts directory.

    Args:
        filename: Name of the prompt file (e.g., "clarification.md")

    Returns:
        Raw prompt text from the file

    Raises:
        FileNotFoundError: If the prompt file doesn't exist
    """
    filepath = PROMPTS_DIR / filename
    if not filepath.exists():
        raise FileNotFoundError(f"Prompt file not found: {filepath}")
    return filepath.read_text(encoding="utf-8")


def get_clarification_instruction() -> str:
    """Get clarification agent instruction with current date.

    Returns:
        Formatted instruction string for Phase 1 agent
    """
    template = _load_prompt("clarification.md")
    return template.replace("{date}", get_today_str())


def get_clarification_instruction_simple() -> str:
    """Get simplified clarification prompt for testing.

    This prompt is optimized for direct action with explicit PDB ID detection.
    It's ~200 lines vs ~800+ lines in the full prompt, reducing reasoning loops.

    Returns:
        Formatted instruction string for Phase 1 agent (simple mode)
    """
    template = _load_prompt("clarification_simple.md")
    return template.replace("{date}", get_today_str())


def get_setup_instruction() -> str:
    """Get setup agent instruction with current date.

    Returns:
        Formatted instruction string for Phase 2 agent
    """
    template = _load_prompt("setup.md")
    return template.replace("{date}", get_today_str())


def get_setup_simple_instruction() -> str:
    """Get simplified setup agent instruction for scratchpad mode.

    This prompt is optimized for smaller models (e.g., qwen2.5:14b)
    that benefit from explicit state tracking via the scratchpad file.

    Returns:
        Formatted instruction string for Phase 2 agent (scratchpad mode)
    """
    template = _load_prompt("setup_simple.md")
    return template.replace("{date}", get_today_str())


def get_validation_instruction() -> str:
    """Get validation agent instruction.

    Returns:
        Instruction string for Phase 3 agent
    """
    return _load_prompt("validation.md")


# Step-specific prompts directory
STEPS_DIR = PROMPTS_DIR / "steps"


def get_step_instruction(step: str) -> str:
    """Get step-specific instruction for focused agent execution.

    Implements Best Practice #3 (Avoid Overloading Agents) by providing
    focused prompts for each workflow step.

    Args:
        step: Step name ("prepare_complex", "solvate", "build_topology", "run_simulation")

    Returns:
        Formatted instruction string for the specific step.
        Falls back to general setup.md if step-specific prompt doesn't exist.
    """
    step_file = STEPS_DIR / f"{step}.md"

    if step_file.exists():
        template = step_file.read_text(encoding="utf-8")
        return template.replace("{date}", get_today_str())

    # Fallback to general setup instruction
    return get_setup_instruction()
