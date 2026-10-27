"""Conservative answer policy for owner-memory chat surfaces."""

NO_APPROVED_MEMORY_RESPONSE = (
    "I couldn't find relevant owner-approved memory for this question, "
    "so I can't answer it from your Second Brain yet. Add or approve a source, "
    "then ask again."
)


def answer_from_memory(memory_context: str, generate):
    """Generate only when approved memory context exists; otherwise abstain."""
    if not memory_context.strip():
        return NO_APPROVED_MEMORY_RESPONSE
    return generate()
