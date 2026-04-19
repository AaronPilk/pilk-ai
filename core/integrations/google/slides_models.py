"""Input models for the slides_create tool.

Kept separate from :mod:`core.integrations.google.slides` so unit tests
can validate the shape without importing the Google API client. Matches
the compact per-slide vocabulary the pitch_deck_agent uses to draft an
outline before committing to the expensive generate call.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Slide layouts mirror the predefined layouts Google Slides exposes via
# its `predefinedLayout` enum. We narrow to a handful that make sense
# for pitch-deck generation — the pitch agent's prompt lists these by
# name so the planner doesn't invent unsupported layouts.
SlideLayout = Literal[
    "title",
    "title_and_body",
    "title_and_two_columns",
    "section_header",
    "caption",
    "blank",
]

# Map PILK-internal names to Google's enum strings. Kept in this module
# so tests can assert the mapping without pulling in the Slides client.
LAYOUT_TO_GOOGLE: dict[str, str] = {
    "title": "TITLE",
    "title_and_body": "TITLE_AND_BODY",
    "title_and_two_columns": "TITLE_AND_TWO_COLUMNS",
    "section_header": "SECTION_HEADER",
    "caption": "CAPTION_ONLY",
    "blank": "BLANK",
}


class Slide(BaseModel):
    """One slide in the deck. Freeform text only — the Slides API does
    the heavy lifting of fitting content into the layout's placeholders."""

    model_config = ConfigDict(extra="forbid")

    layout: SlideLayout
    title: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, max_length=4000)
    image_url: str | None = Field(default=None, max_length=2048)
    # Speaker notes appear in the presenter view only. Strongly
    # encouraged (the pitch agent's hard rule is "speaker notes on
    # every content slide") but not enforced at this layer.
    speaker_notes: str | None = Field(default=None, max_length=4000)

    @field_validator("title", "body", "speaker_notes", mode="before")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class Deck(BaseModel):
    """Top-level input to ``slides_create``. Title lands on the Drive
    file + first-slide title. A deck with zero slides is refused —
    creating an empty presentation is almost always an operator error."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    slides: list[Slide]

    @field_validator("slides")
    @classmethod
    def _at_least_one_slide(cls, v: list[Slide]) -> list[Slide]:
        if not v:
            raise ValueError("deck must contain at least one slide")
        return v


__all__ = [
    "LAYOUT_TO_GOOGLE",
    "Deck",
    "Slide",
    "SlideLayout",
]
