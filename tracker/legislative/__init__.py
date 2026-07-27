SUBJECTS = ("tax", "transportation", "food_security", "affordable_housing")
COUNCILS = ("honolulu", "maui", "hawaii", "kauai")

# Councils publish far more than legislation. Everything is ingested so that it
# is searchable, but each matter is bucketed so the dashboard can default to
# LEGISLATION — otherwise Maui's ~4,300 county communications and ~1,600
# committee reports drown actual bills in every result set.
MATTER_CLASSES = ("legislation", "communication", "procedural")
DEFAULT_MATTER_CLASS = "legislation"

_MATTER_CLASS_BY_TYPE = {
    # Substantive legislation.
    "bill": "legislation",
    "resolution": "legislation",
    "ordinance": "legislation",
    "charter amendment": "legislation",
    # Correspondence routed to the council. Often the vehicle carrying a
    # substantive request (a department's proposed bill arrives this way), so
    # it stays searchable rather than being dropped.
    "county communication": "communication",
    "misc communication": "communication",
    "general communication": "communication",
    "communications": "communication",
    "direct referral": "communication",
    "rule 7(b)": "communication",
    # Administrative traffic — real records, but not things anyone searches for
    # when asking "what is my county doing about X".
    "committee report": "procedural",
    "minutes": "procedural",
    "agenda": "procedural",
    "ceremonial resolution": "procedural",
    "comments from the public": "procedural",
    "public hearing notice": "procedural",
}


def matter_class(bill_type: str | None) -> str:
    """Bucket a council's matter type into legislation / communication / procedural.

    Unknown types fall back to "legislation" deliberately: an unrecognized type
    is far more likely to be a council-specific name for real legislation than
    it is to be noise, and the cost of guessing wrong is a stray row in the
    default view rather than a bill silently missing from it.
    """
    if not bill_type:
        return DEFAULT_MATTER_CLASS
    return _MATTER_CLASS_BY_TYPE.get(bill_type.strip().lower(), DEFAULT_MATTER_CLASS)
