"""Pydantic schema for the workflow inputs file (inputs.yaml / inputs.json).

This module validates the user-provided inputs file and reports problems in a
clear, per-field way before the workflow proceeds. Validation is intentionally
lenient with unknown top-level fields (they are preserved and only warned about)
so legacy or forward-compatible files keep working, while the structured nested
fields (ligands, interactions, mds, links, customs...) are strongly typed since
mistakes there are the hardest to debug.
"""

import re
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from mddb_workflow.utils.auxiliar import InputError, warn
from mddb_workflow.utils.constants import PDB_ID_FORMAT, AVAILABLE_CHECKINGS, AVAILABLE_FAILURES, AUTOMATIC_FLAG


class InputsBaseModel(BaseModel):
    """Base model tolerating (and preserving) unknown fields."""

    model_config = ConfigDict(extra='allow')

    @model_validator(mode='before')
    @classmethod
    def check_is_mapping(cls, data):
        if not isinstance(data, dict):
            required_fields = [name for name, field in cls.model_fields.items() if field.is_required()]
            fields_hint = f' with fields: {", ".join(required_fields)}' if required_fields else ''
            raise ValueError(f'Each "{cls.__name__.lower()}" entry must be a dictionary{fields_hint}.')
        return data


class Ligand(InputsBaseModel):
    """A ligand defined in the simulation.

    Each ligand must have at least one database accession so it can be mapped.
    As a shorthand, a ligand may also be given as a bare PubChem CID (an int or
    a numeric string), which is expanded into a full entry during validation.
    """

    pubchem: Optional[Union[int, str]] = None
    drugbank: Optional[str] = None
    chembl: Optional[str] = None
    # Optional forced mapping through a list of VMD selections
    vmd_selection: Optional[Union[str, list[str]]] = None

    @model_validator(mode='before')
    @classmethod
    def expand_pubchem_shorthand(cls, data):
        """Expand a bare PubChem CID (int or numeric string) into a full entry.
        A non-numeric bare string (a ligand name) is rejected.
        """
        if type(data) is int or (type(data) is str and data.isnumeric()):
            return {'pubchem': str(data)}
        if type(data) is str:
            raise ValueError(f'A ligand name has been identified: {data}. '
                'Please provide at least one of the following IDs: PubChem, DrugBank, ChEMBL.')
        # The forced selection field was previously read as 'selection'
        if type(data) is dict and 'selection' in data:
            raise ValueError('The ligand field "selection" is not supported. Use "vmd_selection" instead.')
        return data

    @model_validator(mode='after')
    def check_has_accession(self) -> 'Ligand':
        if self.pubchem is None and self.drugbank is None and self.chembl is None:
            raise ValueError('Each ligand must have at least one of the following '
                'attributes: "pubchem", "drugbank" or "chembl".')
        return self


class Interaction(InputsBaseModel):
    """A pairwise interaction between two agents to be analyzed."""

    name: str
    agent_1: str
    selection_1: str
    agent_2: str
    selection_2: str
    # Distance used to determine interface atoms (in Å)
    distance_cutoff: Optional[float] = None


class Link(InputsBaseModel):
    """An external link related to the simulation."""

    name: str
    url: str
    ping: Optional[bool] = None


class Representation(InputsBaseModel):
    """A single molecular viewer representation inside a custom view."""

    name: str
    selection: str
    type: str
    color: str


class Custom(InputsBaseModel):
    """A custom molecular viewer view for the web client."""

    name: str
    representations: list[Representation]


class MDConfig(InputsBaseModel):
    """Configuration for a single Molecular Dynamics (MD) run.

    Besides the fields below, an MD entry may carry any project metadata field
    to override the project value for this specific MD (e.g. temp), hence the
    permissive base model.
    """

    name: Optional[str] = None
    mdir: Optional[str] = None
    removed: Optional[bool] = None
    input_topology_filepath: Optional[str] = None
    input_structure_filepath: Optional[str] = None
    input_trajectory_filepaths: Optional[Union[str, list[str]]] = None


class WorkflowInputs(InputsBaseModel):
    """Full schema for the workflow inputs file."""

    # Project metadata
    name: Optional[str] = None
    description: Optional[str] = None
    authors: Optional[Union[str, list[str]]] = None
    groups: Optional[Union[str, list[str]]] = None
    contact: Optional[str] = None
    program: Optional[str] = None
    version: Optional[Union[str, float, int]] = None
    type: Optional[Literal['trajectory', 'ensemble']] = None
    method: Optional[str] = None
    license: Optional[str] = None
    linkcense: Optional[str] = None
    citation: Optional[str] = None
    thanks: Optional[str] = None
    accession: Optional[str] = None

    # References
    links: Optional[list[Link]] = None
    pdb_ids: Optional[Union[str, list[str]]] = None
    # Forced references may be a list of accessions or a per-chain mapping
    forced_references: Optional[Union[list[str], dict[str, str]]] = None
    ligands: Optional[list[Ligand]] = None

    # Simulation metadata
    framestep: Optional[float] = None
    timestep: Optional[float] = None
    temp: Optional[float] = None
    ensemble: Optional[str] = None
    ff: Optional[Union[str, list[str]]] = None
    wat: Optional[str] = None

    # Analysis parameters
    interactions: Optional[list[Interaction]] = None
    pbc_selection: Optional[str] = None
    cg_selection: Optional[str] = None
    ua_selection: Optional[str] = None
    qm_selection: Optional[str] = None
    dummy_selection: Optional[str] = None
    forced_class_selections: Optional[dict[str, str]] = None

    # Representation parameters
    chainnames: Optional[dict[str, str]] = None
    customs: Optional[list[Custom]] = None
    orientation: Optional[list[float]] = None

    # Collections
    collections: Optional[Union[str, list[str]]] = None
    cv19_unit: Optional[str] = None
    cv19_startconf: Optional[str] = None
    cv19_abs: Optional[bool] = None
    cv19_nanobs: Optional[bool] = None

    # Input files
    input_topology_filepath: Optional[str] = None
    input_structure_filepath: Optional[str] = None
    input_trajectory_filepaths: Optional[Union[str, list[str]]] = None
    mds: Optional[list[MDConfig]] = None
    mdref: Optional[int] = None
    dataset_path: Optional[str] = None

    # Additional metadata
    metadditions: Optional[dict] = None

    # Workflow control parameters
    # These values have an effect in the workflow but they are not propagated to the database
    filter_selection: Optional[Union[bool, str]] = None
    image: Optional[bool] = None
    fit: Optional[bool] = None
    translation: Optional[Union[str, list[float]]] = None
    chainer: Optional[dict[str, str]] = None
    skip_frames: Optional[dict[int, int]] = None
    mercy: Optional[Union[bool, list[str]]] = None
    trust: Optional[Union[bool, list[str]]] = None
    faith: Optional[bool] = None

    @field_validator('orientation')
    @classmethod
    def check_orientation_length(cls, value):
        if value is not None and len(value) != 16:
            raise ValueError(f'Orientation must be a list of 16 numbers, got {len(value)}.')
        return value

    @field_validator('framestep', 'timestep', 'temp')
    @classmethod
    def check_positive(cls, value, info):
        if value is not None and value <= 0:
            raise ValueError(f'"{info.field_name}" must be a positive number.')
        return value

    @field_validator('translation')
    @classmethod
    def check_translation(cls, value):
        if value is None:
            return value
        if type(value) is str:
            if value != AUTOMATIC_FLAG:
                raise ValueError(f'Translation must be "{AUTOMATIC_FLAG}" or a list of 3 numbers, got "{value}".')
            return value
        if len(value) != 3:
            raise ValueError(f'Translation must be a list of 3 numbers (x y z), got {len(value)}.')
        return value

    @field_validator('skip_frames')
    @classmethod
    def check_skip_frames(cls, value):
        if value is None:
            return value
        for md_index, frames in value.items():
            if md_index < 0:
                raise ValueError(f'Wrong MD index {md_index} in "skip_frames": it must not be a negative number.')
            if frames < 0:
                raise ValueError(f'Wrong number of frames for MD {md_index} in "skip_frames": it must not be a negative number.')
        return value

    @field_validator('mercy', 'trust')
    @classmethod
    def check_test_flags(cls, value, info):
        if value is None or type(value) is bool:
            return value
        available = AVAILABLE_FAILURES if info.field_name == 'mercy' else AVAILABLE_CHECKINGS
        for flag in value:
            if flag not in available:
                raise ValueError(f'"{flag}" is not a valid "{info.field_name}" value. '
                    f'Available values: {", ".join(available)}.')
        return value

    @field_validator('pdb_ids')
    @classmethod
    def check_pdb_ids(cls, value):
        if value is None:
            return value
        pdb_ids = [value] if isinstance(value, str) else value
        for pdb_id in pdb_ids:
            if not re.match(PDB_ID_FORMAT, pdb_id):
                raise ValueError(f'"{pdb_id}" does not look like a PDB id.')
        return value


# The set of fields explicitly declared in the schema
KNOWN_INPUT_FIELDS = set(WorkflowInputs.model_fields.keys())


def format_validation_error(error: ValidationError) -> str:
    """Format a pydantic ValidationError into a readable, per-field message."""
    lines = [f'Found {error.error_count()} problem(s) in the inputs file:']
    for problem in error.errors():
        # Build a dotted location path (e.g. "ligands -> 0 -> pubchem")
        location = ' -> '.join(str(part) for part in problem['loc']) or '(root)'
        # Custom validators raise plain ValueErrors, which pydantic prefixes with
        # "Value error, "; strip it since our messages are already self-contained
        message = problem['msg'].removeprefix('Value error, ')
        # Clarify the type and value that caused the error, when available
        detail = ''
        if 'input' in problem:
            value = problem['input']
            detail = f' (got {type(value).__name__}: {value!r})'
        lines.append(f' - {location}: {message}{detail}')
    return '\n'.join(lines)


def validate_inputs(inputs_data: dict, strict_unknown: bool = False) -> dict:
    """Validate a loaded inputs dictionary against the workflow schema.

    Args:
        inputs_data (dict): The loaded inputs dictionary to validate.
        strict_unknown (bool): If True, unknown top-level fields will raise an
            InputError. If False, they will be preserved and only warned about.

    """
    # Report unknown top-level fields (legacy or typos)
    unknown_fields = [key for key in inputs_data if key not in KNOWN_INPUT_FIELDS]
    if unknown_fields:
        listed_fields = ', '.join(sorted(unknown_fields))
        if strict_unknown:
            available_fields = ', '.join(sorted(KNOWN_INPUT_FIELDS))
            raise InputError(f'Unrecognized input field(s): {listed_fields}.\n'
                f' Available inputs: {available_fields}')
        warn('Unknown field(s) in the inputs file (they will be ignored by the '
            f'workflow): {listed_fields}')
    # Run the actual validation
    try:
        validated = WorkflowInputs.model_validate(inputs_data)
    except ValidationError as error:
        raise InputError(format_validation_error(error))
    # Write back the normalized ligands
    if validated.ligands is not None:
        inputs_data['ligands'] = [ligand.model_dump(exclude_none=True) for ligand in validated.ligands]
    return inputs_data
