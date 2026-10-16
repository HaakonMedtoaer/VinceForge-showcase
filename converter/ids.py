"""
Live-format ID generation. Shared by build.py (fresh workflow creation)
and source_live_json.py (cloning an existing workflow with fresh IDs) -
split out so neither has to import the other just for this.
"""

import random
import string
import uuid


def new_id() -> str:
    """
    Generate an ID in the same shape Live uses for transaction instances,
    e.g. "xN9MwyGBpt". We don't know Live's exact generation algorithm,
    but the shape (leading 'x' + 10 mixed-case alphanumeric chars) matches
    every example we've captured, and Live has not indicated IDs need to
    follow any particular scheme - just be unique within the workflow.
    """
    chars = string.ascii_letters + string.digits
    return "x" + "".join(random.choice(chars) for _ in range(10))


def new_workflow_id() -> str:
    """
    Generate a workflowId in Live's format: "WORKFLOW-" followed by a
    32-character hex string (a UUID4 with the dashes stripped). Confirmed
    against real examples, e.g. "WORKFLOW-0998db275da791c3670ab16dbfa5b320".

    IMPORTANT: this satisfies Live's validation (the field must be
    present), but Live does NOT actually use this value - it assigns its
    own workflowId regardless of what's sent, confirmed by a real test
    where the created workflow's actual ID differed from what we
    generated. Any code that needs to reference "the workflow just
    created" must read the ID back from the API response, not assume it
    matches this generated value.
    """
    return "WORKFLOW-" + uuid.uuid4().hex
