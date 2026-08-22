from dataclasses import dataclass
from datetime import datetime
from typing import TypeAlias


@dataclass(frozen=True)
class FashionJob:
    job_id: int
    title: str
    company: str
    location: str
    contract: str
    published_at: datetime
    url: str


@dataclass(frozen=True)
class KnownJob:
    job_id: int


FashionItem: TypeAlias = FashionJob | KnownJob  # noqa: UP040
