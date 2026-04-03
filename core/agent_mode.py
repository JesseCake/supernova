"""
core/agent_mode.py — Agent mode dataclass for Supernova.

Agent modes define what Supernova is focused on — a complete personality
and toolset for a specific working context. Loaded from config/modes.yaml
by ModeRegistry and stored on the session.

Unlike InterfaceMode (which is fixed at session creation), agent mode can
change mid-session via the switch_agent_mode tool.

Usage:
    from core.agent_mode import AgentMode

    # Typically you get AgentMode instances from ModeRegistry, not directly
    from core.mode_registry import ModeRegistry
    registry = ModeRegistry(config_dir)
    mode     = registry.get('deep_research')

    # Compare by name
    if agent_mode == 'general':
        ...
    if agent_mode == other_mode:
        ...

    # Use in f-strings
    f"Current mode: {agent_mode}"   # → "Current mode: deep_research"

    # Access properties
    agent_mode.name              # → 'deep_research'
    agent_mode.description       # → 'Heavy analysis and web research'
    agent_mode.precontext        # → 'personality/agent_deep_research.md'
    agent_mode.max_tool_loops    # → 20
    agent_mode.is_default        # → False
    agent_mode.trigger_phrases   # → 'research mode, deep research, investigate'
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentMode:
    """
    A named working mode that defines Supernova's personality and focus.

    Frozen dataclass — instances are immutable after creation. All instances
    are created by ModeRegistry from config/modes.yaml. Tools should never
    create AgentMode instances directly.

    Equality supports comparison against both AgentMode instances and strings:
        agent_mode == 'general'     # True if name is 'general'
        agent_mode == other_mode    # True if names match
    """
    name:            str
    description:     str
    precontext:      str          # relative path to .md personality file
    max_tool_loops:  int  = 5
    is_default:      bool = False
    trigger_phrases: str  = ""    # hint for LLM — injected via provide_context

    def __str__(self) -> str:
        """Return the mode name — useful for system message injection."""
        return self.name

    def __eq__(self, other) -> bool:
        """
        Support equality comparison against both AgentMode and str.

        Usage:
            mode == 'general'      # True if mode.name == 'general'
            mode == other_mode     # True if mode.name == other_mode.name
        """
        if isinstance(other, AgentMode):
            return self.name == other.name
        if isinstance(other, str):
            return self.name == other.lower().strip()
        return NotImplemented

    def __hash__(self) -> int:
        """Hash by name so AgentMode can be used in sets and dict keys."""
        return hash(self.name)

    @classmethod
    def from_dict(cls, name: str, data: dict) -> 'AgentMode':
        """
        Create an AgentMode from a modes.yaml entry.
        Called by ModeRegistry — not intended for direct use.

        Args:
            name: The mode key from modes.yaml, e.g. 'deep_research'
            data: The dict of fields for that mode
        """
        return cls(
            name            = name,
            description     = data.get('description', ''),
            precontext      = data.get('precontext', ''),
            max_tool_loops  = int(data.get('max_tool_loops', 5)),
            is_default      = bool(data.get('is_default', False)),
            trigger_phrases = data.get('trigger_phrases', ''),
        )