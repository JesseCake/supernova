"""
home_automation_action tool — control switches and activate scenes via Home Assistant.
Config (including API key and URL) lives in config/home_automation.yaml.
"""
from typing import Annotated
from pydantic import Field

from core.tool_base import ToolBase

log = ToolBase.logger('home_automation')


# ── Lazy-loaded HA client ─────────────────────────────────────────────────────
# Created once on first use and cached for the lifetime of the process.

_ha_client = None

def _get_client(tool_config: dict):
    """Return a cached Home Assistant client, creating it on first call."""
    global _ha_client
    if _ha_client is None:
        from homeassistant_api import Client as HAClient
        _ha_client = HAClient(tool_config['url'], tool_config['api_key'])
    return _ha_client


# ── Schema function ───────────────────────────────────────────────────────────

def home_automation_action(
    action_type: Annotated[str, Field(
        description="The type of action to perform. Use 'set_switch' to turn a switch on or off, or 'activate_scene' to activate a lighting scene. Required."
    )],
    entity_id: Annotated[str, Field(
        description="The entity ID to act on — use the switch or scene name without the domain prefix e.g. 'desk_lamp' not 'switch.desk_lamp'. Required."
    )],
    state: Annotated[str, Field(
        default=None,
        description="Required for set_switch only. The desired state: 'on' or 'off'."
    )] = None,
) -> str:
    """
    Control a Home Assistant switch or activate a scene.
    Use set_switch to turn switches on or off, or activate_scene to trigger a lighting scene.
    Entity IDs and scene names are listed in the system context.
    """
    ...


# ── Executor ──────────────────────────────────────────────────────────────────

def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params      = ToolBase.params(tool_args)
    action_type = params.get('action_type')
    entity_id   = params.get('entity_id')
    state       = params.get('state')

    log.info("Home automation action", extra={'data': f"action={action_type!r} entity={entity_id!r} state={state!r}"})

    try:
        ha = _get_client(tool_config)

        if action_type == "set_switch":
            switch = ha.get_domain("switch")
            ToolBase.speak(core, session, f"{entity_id} {state}.")
            if state == "on":
                switch.turn_on(entity_id=f"switch.{entity_id}")
            else:
                switch.turn_off(entity_id=f"switch.{entity_id}")
            return ToolBase.result(core, 'home_automation_action', {
                "text": f"Successfully switched {entity_id} {state}",
            })

        elif action_type == "activate_scene":
            ToolBase.speak(core, session, f"Activating scene '{entity_id}'.")
            scene = ha.get_domain("scene")
            scene.turn_on(entity_id=f"scene.{entity_id}")
            return ToolBase.result(core, 'home_automation_action', {
                "text": f"Successfully activated scene {entity_id}",
            })

        else:
            return ToolBase.error(core, 'home_automation_action',
                "Invalid action type. Use 'set_switch' or 'activate_scene'.")

    except Exception as e:
        log.error("Home automation action failed", exc_info=True)
        return ToolBase.error(core, 'home_automation_action',
            f"Error performing {action_type} on {entity_id}: {e}. Check the entity name is correct.")