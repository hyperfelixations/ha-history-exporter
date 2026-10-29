"""What a history request asks Home Assistant for, and the fidelity it yields."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class HistoryQuery:
    """The options of ``/api/history/period``.

    ``minimal_response``, ``no_attributes`` and ``significant_changes_only``
    reduce what Home Assistant returns; ``skip_initial_state`` omits the
    synthetic state at the window start.
    """

    minimal_response: bool = False
    no_attributes: bool = False
    significant_changes_only: bool = False
    skip_initial_state: bool = True

    def options(self) -> dict[str, bool]:
        """The options in their contractual order, as recorded in the manifest."""
        return asdict(self)

    def capture_profile(self) -> dict[str, str]:
        """The fidelity of the captured data, as recorded in the manifest."""
        if self.no_attributes:
            attributes = "omitted"
        elif self.minimal_response:
            attributes = "initial_only"
        else:
            attributes = "full"
        return {
            "response": "minimal" if self.minimal_response else "full",
            "attributes": attributes,
            "state_changes": "significant_only" if self.significant_changes_only else "all",
            "initial_state": "omitted" if self.skip_initial_state else "included",
        }
