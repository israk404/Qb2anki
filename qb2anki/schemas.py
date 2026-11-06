"""Response schemas sent to Gemini (plain dicts) and pydantic models that validate what comes back."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------- schemas for Gemini
_BOX = {
    "type": "ARRAY",
    "items": {"type": "INTEGER"},
    "description": "[ymin, xmin, ymax, xmax] integers 0-1000 normalised to the image",
}


def _nullable(d: dict) -> dict:
    return {**d, "nullable": True}


ORIENTATION_SCHEMA = {
    "type": "OBJECT",
    "properties": {"rotate_ccw_degrees": {"type": "STRING", "enum": ["0", "90", "180", "270"]}},
    "required": ["rotate_ccw_degrees"],
}

LAYOUT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "text_upright": {"type": "BOOLEAN"},
        "pages": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"printed_page": _nullable({"type": "INTEGER"}), "page_box": _BOX},
                "required": ["page_box"],
            },
        },
    },
    "required": ["text_upright", "pages"],
}


def page_schema(subjects: list[str]) -> dict:
    """ONE call per page returns the questions, the answer-key rows and the exam header together."""
    return {
        "type": "OBJECT",
        "properties": {
            "questions": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "number": {"type": "STRING"},
                        "stem": {"type": "STRING"},
                        "options": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {"label": {"type": "STRING"}, "text": {"type": "STRING"}},
                                "required": ["label", "text"],
                            },
                        },
                        "explanation": {"type": "STRING"},
                        "answer_in_explanation": _nullable({"type": "STRING"}),
                        "subject": {"type": "STRING", "enum": list(subjects) + ["Other"]},
                        "has_figure": {"type": "BOOLEAN"},
                        "figure_box": _nullable(_BOX),
                        "question_box": _nullable(_BOX),
                        "truncated_top": {"type": "BOOLEAN"},
                        "truncated_bottom": {"type": "BOOLEAN"},
                    },
                    "required": ["number", "stem", "options", "explanation", "subject", "has_figure"],
                },
            },
            "answer_key": {
                "type": "ARRAY",
                "items": {"type": "OBJECT",
                          "properties": {"number": {"type": "STRING"}, "answer": {"type": "STRING"}},
                          "required": ["number", "answer"]},
            },
            "exam_header": _nullable({
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "exam_date": _nullable({"type": "STRING"}),
                    "duration": _nullable({"type": "STRING"}),
                    "total_marks": _nullable({"type": "STRING"}),
                    "other_text": _nullable({"type": "STRING"}),
                },
                "required": ["title"],
            }),
        },
        "required": ["questions", "answer_key"],
    }


FILL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "results": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "answer": _nullable({"type": "STRING"}),
                    "confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
                    "explanation": {"type": "STRING"},
                },
                "required": ["id", "confidence"],
            },
        }
    },
    "required": ["results"],
}

MATH_CHECK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "results": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "consistent": {"type": "BOOLEAN"},
                    "fixed_explanation": {"type": "STRING"},
                    "note": {"type": "STRING"},
                },
                "required": ["id", "consistent"],
            },
        }
    },
    "required": ["results"],
}

REREAD_SCHEMA = {
    "type": "OBJECT",
    "properties": {"explanation": {"type": "STRING"}},
    "required": ["explanation"],
}

DETECT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "kind": {"type": "STRING", "enum": ["spread", "page", "screenshot"]},
        "rotate_ccw_degrees": {"type": "STRING", "enum": ["0", "90", "180", "270"]},
    },
    "required": ["kind", "rotate_ccw_degrees"],
}

PROOF_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "results": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "stem": {"type": "STRING"},
                    "options": {
                        "type": "ARRAY",
                        "items": {"type": "OBJECT",
                                  "properties": {"label": {"type": "STRING"}, "text": {"type": "STRING"}},
                                  "required": ["label", "text"]},
                    },
                    "explanation": {"type": "STRING"},
                    "changes": {"type": "STRING"},
                },
                "required": ["id"],
            },
        }
    },
    "required": ["results"],
}

CHECK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "results": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "text_ok": {"type": "BOOLEAN"},
                    "fixed_stem": {"type": "STRING"},
                    "fixed_options": {
                        "type": "ARRAY",
                        "items": {"type": "OBJECT",
                                  "properties": {"label": {"type": "STRING"}, "text": {"type": "STRING"}},
                                  "required": ["label", "text"]},
                    },
                    "answer": _nullable({"type": "STRING"}),
                    "answer_confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
                    "agrees": {"type": "STRING", "enum": ["yes", "no", "no_source"]},
                    "outdated": {"type": "BOOLEAN"},
                    "outdated_note": {"type": "STRING"},
                    "explanation_ok": {"type": "BOOLEAN"},
                    "explanation_issue": {"type": "STRING"},
                    "explanation": {"type": "STRING"},
                    "note": {"type": "STRING"},
                },
                "required": ["id", "text_ok", "answer_confidence", "agrees", "outdated", "explanation_ok"],
            },
        }
    },
    "required": ["results"],
}

# --------------------------------------------------------------------------- validators


def _clean(v):
    """Nulls are dropped (so defaults apply) and null list items removed: one odd value must not sink a whole page."""
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [_clean(x) for x in v if x is not None]
    return v


def _int_or_none(v):
    if v is None or isinstance(v, int):
        return v
    from .textnorm import parse_number     # "৫৬৮", "568.", "90°" -> number
    return parse_number(v)


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data):
        return _clean(data) if isinstance(data, (dict, list)) else data


class Orientation(_M):
    rotate_ccw_degrees: int = 0

    @field_validator("rotate_ccw_degrees", mode="before")
    @classmethod
    def _deg(cls, v):
        return _int_or_none(v) or 0


class PageLayoutModel(_M):
    printed_page: Optional[int] = None
    page_box: Optional[list[float]] = None

    @field_validator("printed_page", mode="before")
    @classmethod
    def _page(cls, v):
        return _int_or_none(v)


class LayoutModel(_M):
    text_upright: bool = True
    pages: list[PageLayoutModel] = Field(default_factory=list)


class HeaderModel(_M):
    title: str = ""
    exam_date: Optional[str] = None
    duration: Optional[str] = None
    total_marks: Optional[str] = None
    other_text: Optional[str] = None


class KeyEntry(_M):
    number: str = ""
    answer: str = ""


class OptionModel(_M):
    label: str = ""
    text: str = ""


class QuestionModel(_M):
    number: str = ""
    stem: str = ""
    options: list[OptionModel] = Field(default_factory=list)
    explanation: str = ""
    answer_in_explanation: Optional[str] = None
    subject: str = "Other"
    has_figure: bool = False
    figure_box: Optional[list[float]] = None
    question_box: Optional[list[float]] = None
    truncated_top: bool = False
    truncated_bottom: bool = False


class PageModel(_M):
    questions: list[QuestionModel] = Field(default_factory=list)
    answer_key: list[KeyEntry] = Field(default_factory=list)
    exam_header: Optional[HeaderModel] = None


class FillItem(_M):
    id: str = ""
    answer: Optional[str] = None
    confidence: str = "low"
    explanation: str = ""


class FillModel(_M):
    results: list[FillItem] = Field(default_factory=list)


class MathItem(_M):
    id: str = ""
    consistent: bool = True
    fixed_explanation: str = ""
    note: str = ""


class MathModel(_M):
    results: list[MathItem] = Field(default_factory=list)


class RereadModel(_M):
    explanation: str = ""


class Detection(_M):
    kind: str = "page"
    rotate_ccw_degrees: int = 0

    @field_validator("rotate_ccw_degrees", mode="before")
    @classmethod
    def _deg(cls, v):
        return (_int_or_none(v) or 0) % 360

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v):
        v = str(v or "").strip().lower()
        return v if v in ("spread", "page", "screenshot") else "page"


class ProofItem(_M):
    id: str = ""
    stem: Optional[str] = None
    options: Optional[list[OptionModel]] = None
    explanation: Optional[str] = None
    changes: str = ""


class ProofModel(_M):
    results: list[ProofItem] = Field(default_factory=list)


class CheckItem(_M):
    id: str = ""
    text_ok: bool = True
    fixed_stem: str = ""
    fixed_options: list[OptionModel] = Field(default_factory=list)
    answer: Optional[str] = None
    answer_confidence: str = "low"
    agrees: str = ""
    outdated: bool = False
    outdated_note: str = ""
    explanation_ok: bool = True
    explanation_issue: str = ""
    explanation: str = ""
    note: str = ""


class CheckModel(_M):
    results: list[CheckItem] = Field(default_factory=list)
