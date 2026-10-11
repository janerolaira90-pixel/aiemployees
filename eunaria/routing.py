"""Named-agent routing for incoming owner messages.

"Sofia, create a strategy for NULA"  -> sofia
"@chloe add 20 ideas"                -> chloe
"/theo write five scripts"           -> theo
"Hey Grace: today's GHL report?"     -> grace
anything else                        -> atlas (Chief of Staff)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .agents.registry import AGENTS, Agent, get, lookup

_NAMES = sorted({a.id for a in AGENTS} | {al for a in AGENTS for al in a.aliases}, key=len, reverse=True)
_PATTERN = re.compile(
    r"^\s*(?:(?:hey|hi|hello|ok|okay)\s+)?[@/]?(" + "|".join(_NAMES) + r")(?:@\w+)?\b\s*[,:;\-–—!]?\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class Route:
    agent: Agent
    text: str
    explicit: bool


def route(message: str) -> Route:
    m = _PATTERN.match(message)
    if m:
        agent = lookup(m.group(1))
        rest = m.group(2).strip()
        if agent and rest:
            return Route(agent=agent, text=rest, explicit=True)
    return Route(agent=get("atlas"), text=message.strip(), explicit=False)


def title_from(text: str, limit: int = 80) -> str:
    first = text.strip().splitlines()[0] if text.strip() else "Untitled request"
    first = first[0].upper() + first[1:] if first else first
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"
