"""Code-generation support: validate agent-written code and emit MotionWorks code."""

from __future__ import annotations

from . import conventions
from .validate import (
    ValidationResult,
    fb_signature,
    fb_usage_catalog,
    observed_fb_signature,
    project_symbols,
    render_pou_source,
    render_type_source,
    validate,
)

__all__ = [
    "ValidationResult",
    "conventions",
    "fb_signature",
    "fb_usage_catalog",
    "observed_fb_signature",
    "project_symbols",
    "render_pou_source",
    "render_type_source",
    "validate",
]
