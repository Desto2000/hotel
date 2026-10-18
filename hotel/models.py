"""Pydantic types for Reg payloads, annotations and lobby edit operations.

Edit-op fields are all required (no defaults) so the JSON schema is valid for
strict structured-output mode on OpenAI-compatible endpoints.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------- annotations

Relation = Literal["supports", "contradicts", "new", "unrelated"]


class Pointer(Strict):
    """Span inside the stored *text* blob of a retrieval. quote must equal
    text[start:end] exactly; the gate re-checks it against the blob."""

    blob: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote: str = Field(min_length=1)


class Claim(Strict):
    text: str = Field(min_length=1)
    pointer: Pointer
    relation: Relation
    lobby_refs: list[str] = []


class Annotation(Strict):
    target: int  # reg id of the retrieval being annotated
    claims: list[Claim]  # may be empty ("nothing useful") but caused must say so
    caused: str = Field(min_length=1)
    caused_refs: list[str] = []  # lobby entries this retrieval caused / affected


# --------------------------------------------------------------- edit ops

EntryType = Literal["monologue", "bounty", "goal", "reasoning", "idea", "log"]


class AppendOp(Strict):
    op: Literal["append"]
    room: str
    type: EntryType
    body: str
    refs: list[str]
    tags: list[str]


class TagOp(Strict):
    op: Literal["tag"]
    id: str
    tag: str


class LinkOp(Strict):
    op: Literal["link"]
    id: str
    target: str


class RefOp(Strict):
    op: Literal["ref"]
    id: str
    ref: str


class CloseOp(Strict):
    op: Literal["close"]
    id: str
    closed_by: str


class SupersedeOp(Strict):
    op: Literal["supersede"]
    id: str
    type: EntryType
    body: str
    reason: str


class ContestOp(Strict):
    op: Literal["contest"]
    id: str
    reason: str


class ReopenOp(Strict):
    op: Literal["reopen"]
    id: str
    reason: str


class PromoteOp(Strict):
    op: Literal["promote"]
    id: str
    text: str


class MoveOp(Strict):
    """Topics drift. A move re-files a live entry into another room: a copy (same
    type, origin, body, tags/refs) is appended there and the original is
    superseded by it. History stays in the old room."""
    op: Literal["move"]
    id: str
    room: str
    reason: str


EditOp = Annotated[
    Union[AppendOp, TagOp, LinkOp, RefOp, CloseOp, SupersedeOp, ContestOp, ReopenOp, PromoteOp, MoveOp],
    Field(discriminator="op"),
]
EDIT_OP = TypeAdapter(EditOp)


class LibrarianOutput(Strict):
    rationale: str
    ops: list[EditOp]


def op_dump(op) -> dict:
    return op.model_dump(mode="json")


def op_load(d: dict):
    return EDIT_OP.validate_python(d)
