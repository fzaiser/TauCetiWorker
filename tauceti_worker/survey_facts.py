"""Shared survey reductions with worker support for multiple roadmap areas."""

from tauceti_tools.survey_facts import *  # noqa: F403
from tauceti_tools.survey_facts import in_scope as _in_scope

from .roadmap_scope import roadmap_selection


def in_scope(labels, targets=(), branch="", roadmap="any", skip=()):
    """Include maintenance belonging to any selected area, plus shared maintenance."""
    return any(_in_scope(labels, targets, branch, area, skip) for area in roadmap_selection(roadmap) or ("any",))
