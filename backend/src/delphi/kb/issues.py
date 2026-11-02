"""Validation issue type shared by the loader and the validator (docs/07 §3 rule 9)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Issue:
    path: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: [{self.code}] {self.message}"


class KbValidationError(Exception):
    """Raised when a KB fails loading or validation. Carries every issue found."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        summary = "\n".join(str(i) for i in issues[:20])
        more = f"\n… and {len(issues) - 20} more" if len(issues) > 20 else ""  # noqa: PLR2004
        super().__init__(f"KB invalid ({len(issues)} issue(s)):\n{summary}{more}")
