"""
Worded-prompt source - NOT YET DESIGNED.

Deliberately left as a stub. Per project direction: hold off on this until
the Classic-XML and Live-JSON paths are refactored and confirmed working
end to end. When picked up, this needs an LLM-assisted parse step (Claude
drafting the IR conversationally, asking clarifying questions about
ambiguous fields and confirming M3 program/transaction names) rather than
a deterministic parser like the other two sources - see the project
conversation for the fuller rationale.
"""


def parse_prompt(prompt_text: str):
    raise NotImplementedError(
        "Worded-prompt parsing is not designed yet - see this module's docstring."
    )
