"""Fields the AI employees need in each mapped Notion database (from the master brief).

Existing properties are reused when an equivalent exists (see EQUIVALENTS); only genuinely
missing ones are proposed, and they are only added by `python -m eunaria notion-plan --apply`.
"""

from __future__ import annotations

AI_EMPLOYEES = ["Atlas", "Sofia", "Victor", "Chloe", "Clara", "Theo", "Luca", "Gabriel", "Grace", "Felix",
                "Enzo", "Leo", "Ava", "Maya"]


def _select(*names: str) -> dict:
    return {"select": {"options": [{"name": n} for n in names]}}


def _multi(*names: str) -> dict:
    return {"multi_select": {"options": [{"name": n} for n in names]}}


TEXT = {"rich_text": {}}
DATE = {"date": {}}

REQUIRED: dict[str, dict[str, dict]] = {
    "content_ideas": {
        "Platform": _multi("Instagram", "Facebook", "TikTok", "YouTube", "LinkedIn"),
        "Content Pillar": _select("Education", "Authority", "Proof", "Behind the Scenes", "Offer", "Community"),
        "Funnel Stage": _select("TOFU", "MOFU", "BOFU"),
        "Content Format": _select("Reel", "Carousel", "Static", "Story", "Talking Head", "Long-form"),
        "Target Audience": TEXT,
        "Hook": TEXT,
        "Objective": TEXT,
        "Creative Direction": TEXT,
        "Priority": _select("High", "Medium", "Low"),
        "Idea Status": _select("New", "Shortlisted", "Approved", "In Calendar", "Rejected"),
        "Suggested Publish Date": DATE,
        "Assigned AI": _select(*AI_EMPLOYEES),
        "Campaign": TEXT,
    },
    "content_calendar": {
        "Hook": TEXT,
        "CTA": TEXT,
        "Hashtags": TEXT,
        "Platform": _multi("Instagram", "Facebook", "TikTok", "YouTube", "LinkedIn"),
        "Revision Notes": TEXT,
        "Assigned AI": _select(*AI_EMPLOYEES),
    },
}

# Required field -> existing property names that already serve that purpose.
EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "Content Pillar": ("Content Pillar", "Pillar"),
    "Idea Status": ("Status", "Idea Status"),
    "Suggested Publish Date": ("Posting Date", "Publish Date", "Suggested Publish Date"),
    "Content Format": ("Video Footage", "Format", "Content Format"),
    "Revision Notes": ("Revision Notes", "Notes"),
    "Platform": ("Platform", "Platforms"),
}

# Implied by the database itself (each client has its own databases).
IMPLIED = {"content_ideas": ["Client or brand (separate database per client)",
                             "Content title (the database's title property)"]}


def gaps(database_key: str, existing: dict[str, dict]) -> dict[str, dict]:
    """Missing properties for a database given its current schema {name: {type: ...}}."""
    have = {n.strip().lower() for n in existing}
    out = {}
    for name, definition in REQUIRED.get(database_key, {}).items():
        names = EQUIVALENTS.get(name, (name,))
        if not any(n.lower() in have for n in names):
            out[name] = definition
    return out
