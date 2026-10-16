"""
Workflow name auto-increment, shared across every source (Classic XML,
Live JSON clone, worded prompt) - unchanged from the original script,
just relocated. Still keyed by base label, still persisted in
converter_state.json.
"""

import json
import os

# Kept in RUN/, alongside the other run artifacts (sample inputs, last
# output), not in converter/ with the source code - this is state a run
# reads and writes, not part of the tool itself. Pre-existing counts in
# the current file carry over unchanged.
_DEFAULT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "RUN", "converter_state.json"
)


def next_labeled_name(base_label: str, state_file: str = _DEFAULT_STATE_FILE) -> str:
    """
    Append an incrementing number to a workflow name each time this is
    called for the same base label, so repeated runs get distinct,
    easy-to-track names in the Live UI instead of colliding or requiring
    a manual rename (e.g. "List Lines", then "List Lines 2", "List Lines 3", ...).
    """
    state = {}
    if os.path.exists(state_file):
        try:
            with open(state_file) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            state = {}

    count = state.get(base_label, 0) + 1
    state[base_label] = count

    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)

    return base_label if count == 1 else f"{base_label} {count}"
