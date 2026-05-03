"""
core/tool_loader.py — Dynamic tool loader for Supernova.

Scans the tools/ folder for .py files and the config/ folder for matching
.yaml sidecar files. Reloads automatically when any file changes.

Each tool module must follow this convention:

    tools/my_tool.py
    ├── A schema function (or get_schema hook for dynamic schemas)
    ├── execute(tool_args, session, core, tool_config) — the implementation
    ├── provide_context(core, tool_config, session) -> str  (optional)
    └── TOOLS list for multi-tool files (optional)

    config/my_tool.yaml  (optional sidecar)
    ├── enabled:          true
    ├── interfaces:       []           # [] = all, ['speaker','phone','general']
    ├── agent_modes:      []           # [] or absent = general only, [all] = all modes, ['deep_research'] = whitelist
    ├── blocked_modes:    []           # ['deep_research'] to exclude from specific modes
    ├── requires_config:  ptv          # skip if AppConfig lacks this attribute
    ├── context_priority: 50           # lower = earlier in system prompt
    └── ... any other tool-specific config values

Multi-tool files export a TOOLS list:
    TOOLS = [
        {'schema': fn, 'name': 'tool_name', 'execute': execute_fn},
        ...
    ]

Dynamic schema hook — for tools that need runtime data in descriptions:
    def get_schema(tool_config: dict, core) -> callable:
        # Return a schema function built with current data
        ...

The loader passes tool_config into execute() and provide_context() so tools
can read their own settings without touching AppConfig directly.
"""

import importlib
import importlib.util
import os
import sys
import yaml

from core.interface_mode import InterfaceMode
from core.logger import get_logger

log = get_logger('tool_loader')


class ToolLoader:
    def __init__(self, tools_dir: str, config_dir: str, app_config):
        self.tools_dir  = tools_dir
        self.config_dir = config_dir
        self.app_config = app_config

        # Cached state — rebuilt whenever files change.
        # Tools are stored as dicts with all metadata rather than split lists,
        # so filtering by interface/agent_mode can happen at get_tools() time.
        self._tools:            list[dict] = []   # all loaded tool entries
        self._executors:        dict       = {}   # name → callable(tool_args, session, core)
        self._tool_configs:     dict       = {}   # name → yaml dict
        self._context_providers: list      = []   # (priority, name, fn, tool_config)
        self._turn_context_providers: list = []   # for those tools that inject some context just before agent turn
        self._session_end_handlers:   list = []   # for those tools that need to clean up after a session ends
        self._last_mtime:       float      = 0.0
        self._core_ref                     = None  # set on first get_context_injections call

        # Force an initial load
        self._reload()

    # ── Public interface ──────────────────────────────────────────────────────

    def get_tools(
        self,
        interface_mode: InterfaceMode = InterfaceMode.GENERAL,
        agent_mode = None,
    ) -> list:
        """
        Return schema functions for Ollama's tools= parameter, filtered by
        interface_mode and agent_mode.

        Args:
            interface_mode: Current session interface (SPEAKER, PHONE, GENERAL).
            agent_mode:     Current AgentMode instance, or None for all modes.
        """
        self._reload_if_changed()
        result = []
        for entry in self._tools:
            if not self._matches_interface(entry, interface_mode):
                continue
            if not self._matches_agent_mode(entry, agent_mode):
                continue
            result.append(entry['schema'])
        return result

    def get_executor(self, tool_name: str):
        """Return the execute callable for a tool, or None if not found."""
        self._reload_if_changed()
        return self._executors.get(tool_name)

    def get_tool_config(self, tool_name: str) -> dict:
        """Return the yaml config dict for a tool (empty dict if no yaml)."""
        return self._tool_configs.get(tool_name, {})

    def get_context_injections(self, core, session: dict = None) -> list[str]:
        """
        Call all registered context providers in priority order and return
        a list of non-empty strings to inject into the system prompt.

        Session is passed through to providers so they can adapt their
        injection based on interface_mode, agent_mode, speaker etc.

        Lower context_priority = earlier in the prompt.
        """
        self._reload_if_changed()
        self._core_ref = core
        results = []
        for priority, name, provider_fn, tool_config in self._context_providers:
            try:
                text = provider_fn(core, tool_config, session)
                if text and text.strip():
                    results.append(text.strip())
            except Exception as e:
                log.error(f"Context provider error in {name}", extra={'data': str(e)})
        return results
    
    def get_turn_context_injections(self, core, session: dict, user_input: str) -> list[str]:
        """
        Call all turn context providers in priority order and return
        a list of non-empty strings to inject as system messages
        immediately before the user message each turn.

        Unlike get_context_injections(), these are NOT added to the static
        system prompt — they go at the end of the message list so the
        Ollama KV cache prefix remains stable.

        Lower turn_context_priority = earlier in the injection order.
        """
        self._reload_if_changed()
        results = []
        for priority, name, provider_fn, tool_config in self._turn_context_providers:
            try:
                text = provider_fn(core, tool_config, session, user_input)
                if text and text.strip():
                    results.append(text.strip())
            except Exception as e:
                log.error(f"Turn context provider error in {name}", extra={'data': str(e)})
        return results
    
    def call_session_end_handlers(self, core, session: dict):
        """
        Call all registered on_session_end handlers.
        Called by CoreProcessor.close_session() when a session ends cleanly.
        Plugins use this hook to summarise, persist, or clean up session data.
        """
        self._reload_if_changed()
        for name, fn, tool_config in self._session_end_handlers:
            try:
                fn(core, tool_config, session)
            except Exception as e:
                log.error(f"Session end handler error in {name}",
                        extra={'data': str(e)})

    # ── Filtering helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _matches_interface(entry: dict, interface_mode: InterfaceMode) -> bool:
        """
        Return True if this tool entry should be included for the given interface.

        interfaces: []                   → all interfaces (default)
        interfaces: [speaker, phone]     → voice interfaces only
        interfaces: [general]            → text interfaces only
        """
        interfaces = entry.get('_interfaces', [])
        if not interfaces:
            return True
        return interface_mode in interfaces

    @staticmethod
    def _matches_agent_mode(entry: dict, agent_mode) -> bool:
        """
        Return True if this tool entry should be included for the given agent mode.

        agent_modes not set or []:    → general only (default)
        agent_modes: [all]            → all modes
        agent_modes: [deep_research]  → that mode only (whitelist)
        blocked_modes: [deep_research] → all modes EXCEPT deep_research (blacklist)

        blocked_modes takes precedence over agent_modes if both are set.
        """
        mode_name     = str(agent_mode) if agent_mode else 'general'
        raw           = entry.get('agent_modes', [])
        blocked_modes = entry.get('blocked_modes', [])

        # Normalise — empty or absent → general only
        agent_modes = raw if raw else ['general']

        # blocked_modes takes precedence
        if blocked_modes and mode_name in blocked_modes:
            return False

        # 'all' keyword — available in every mode
        if 'all' in agent_modes:
            return True

        return mode_name in agent_modes

    # ── Change detection ──────────────────────────────────────────────────────

    def _current_mtime(self) -> float:
        """
        Return the latest mtime across all .py and .yaml files in both
        the tools dir and the config dir.
        """
        latest = 0.0
        for directory in (self.tools_dir, self.config_dir):
            try:
                latest = max(latest, os.path.getmtime(directory))
                for fname in os.listdir(directory):
                    if fname.endswith(('.py', '.yaml')) and not fname.startswith('_'):
                        fpath = os.path.join(directory, fname)
                        try:
                            latest = max(latest, os.path.getmtime(fpath))
                        except OSError:
                            pass
            except OSError:
                pass
        return latest

    def _reload_if_changed(self):
        mtime = self._current_mtime()
        if mtime != self._last_mtime:
            self._reload()

    # ── Core reload logic ─────────────────────────────────────────────────────

    def _reload(self):
        """Scan tools/ and config/ and rebuild all tables."""
        tools            = []
        executors        = {}
        tool_configs     = {}
        context_providers = []
        turn_providers   = []
        session_end_handlers = []

        try:
            tool_files = sorted(
                f for f in os.listdir(self.tools_dir)
                if f.endswith('.py') and not f.startswith('_')
            )
        except OSError as e:
            log.error("Cannot read tools dir", extra={'data': str(e)})
            return

        for filename in tool_files:
            name = filename[:-3]  # strip .py

            # Load sidecar yaml
            yaml_path   = os.path.join(self.config_dir, f"{name}.yaml")
            tool_config = {}
            if os.path.exists(yaml_path):
                try:
                    with open(yaml_path) as f:
                        tool_config = yaml.safe_load(f) or {}
                except Exception as e:
                    log.error(f"Error reading {yaml_path}", extra={'data': str(e)})

            tool_configs[name] = tool_config

            # Check enabled flag (default true)
            if not tool_config.get('enabled', True):
                log.debug(f"Skipping {name} (disabled)")
                continue

            # Check requires_config
            requires = tool_config.get('requires_config')
            if requires and not getattr(self.app_config, requires, None):
                log.debug(f"Skipping {name} (requires config.{requires})")
                continue

            # Resolve interface filter — supports new 'interfaces' list and
            # legacy 'voice_only' / 'phone_only' flags for backwards compat
            interfaces = self._resolve_interfaces(tool_config)
            tool_config['_interfaces'] = interfaces  # store coerced version

            # Load (or reload) the module
            module = self._load_module(name, os.path.join(self.tools_dir, filename))
            if module is None:
                continue

            # ── Context provider (optional) ───────────────────────────────────
            provider_fn = getattr(module, 'provide_context', None)
            if provider_fn is not None and callable(provider_fn):
                priority = tool_config.get('context_priority', 50)
                context_providers.append((priority, name, provider_fn, tool_config))
                log.debug(f"Registered context provider: {name} (priority={priority})")

            # ── Turn context provider (optional) ───────────────────────────────
            turn_fn = getattr(module, 'provide_turn_context', None)
            if turn_fn is not None and callable(turn_fn):
                priority = tool_config.get('turn_context_priority', 50)
                turn_providers.append((priority, name, turn_fn, tool_config))
                log.debug(f"Registered turn context provider: {name} (priority={priority})")

            # ── Session end handler (optional) ───────────────────────────────
            end_fn = getattr(module, 'on_session_end', None)
            if end_fn is not None and callable(end_fn):
                session_end_handlers.append((name, end_fn, tool_config))
                log.debug(f"Registered session end handler: {name}")

            # ── Executor closure ──────────────────────────────────────────────
            def make_executor(fn, tc):
                def executor(tool_args, session, core):
                    return fn(tool_args, session, core, tc)
                return executor

            # ── Agent mode filter from yaml ───────────────────────────────────
            agent_modes   = tool_config.get('agent_modes', [])
            blocked_modes = tool_config.get('blocked_modes', [])

            # ── get_schema hook (optional dynamic schema) ─────────────────────
            get_schema_fn = getattr(module, 'get_schema', None)

            # ── Multi-tool export (TOOLS list) ────────────────────────────────
            multi = getattr(module, 'TOOLS', None)
            if multi is not None:
                for entry in multi:
                    t_schema  = entry.get('schema')
                    t_name    = entry.get('name')
                    t_execute = entry.get('execute')
                    if not (t_schema and t_name and t_execute):
                        log.warning(f"Malformed TOOLS entry in {name}.py, skipping")
                        continue
                    tools.append({
                        'name':          t_name,
                        'schema':        t_schema,
                        '_interfaces':   interfaces,
                        'agent_modes':   agent_modes,
                        'blocked_modes': blocked_modes,
                    })
                    executors[t_name] = make_executor(t_execute, tool_config)
                    log.info(f"Loaded tool: {t_name}",
                             extra={'data': f"interfaces={[str(i) for i in interfaces]} agent_modes={agent_modes} blocked_modes={blocked_modes}"})
                continue

            # ── Single-tool convention ────────────────────────────────────────
            if get_schema_fn is not None and callable(get_schema_fn):
                try:
                    schema_fn = get_schema_fn(tool_config, self._core_ref)
                    log.debug(f"Used get_schema hook for {name}")
                except Exception as e:
                    log.error(f"get_schema error in {name}", extra={'data': str(e)})
                    schema_fn = None
            else:
                schema_fn = getattr(module, name, None)

            if schema_fn is None or not callable(schema_fn):
                log.warning(f"{name}.py has no schema function and no TOOLS list, skipping")
                continue

            execute_fn = getattr(module, 'execute', None)
            if execute_fn is None or not callable(execute_fn):
                log.warning(f"{name}.py has no execute() function, skipping")
                continue

            tools.append({
                'name':          name,
                'schema':        schema_fn,
                '_interfaces':   interfaces,
                'agent_modes':   agent_modes,
                'blocked_modes': blocked_modes,
            })
            executors[name] = make_executor(execute_fn, tool_config)
            log.info(f"Loaded tool: {name}",
                     extra={'data': f"interfaces={[str(i) for i in interfaces]} agent_modes={agent_modes} blocked_modes={blocked_modes}"})

        # Sort context providers by priority
        context_providers.sort(key=lambda x: x[0])
        turn_providers.sort(key=lambda x: x[0])

        self._tools             = tools
        self._executors         = executors
        self._tool_configs      = tool_configs
        self._context_providers = context_providers
        self._turn_context_providers = turn_providers
        self._session_end_handlers   = session_end_handlers
        self._last_mtime        = self._current_mtime()

        general_count = sum(1 for t in tools if not t['_interfaces'])
        filtered_count = len(tools) - general_count
        log.info(
            "Tools loaded",
            extra={'data': (
                f"{len(tools)} total — "
                f"{general_count} general, "
                f"{filtered_count} interface/mode filtered, "
                f"{len(context_providers)} context providers"
            )}
        )

    def _resolve_interfaces(self, tool_config: dict) -> list[InterfaceMode]:
        """
        Resolve the interface filter for a tool from its yaml config.

        interfaces: [speaker, phone]   → voice interfaces only
        interfaces: [general]          → text interfaces only
        interfaces: []  or absent      → available on all interfaces
        """
        raw = tool_config.get('interfaces', None)
        if not raw:
            return []
        coerced = []
        for val in (raw if isinstance(raw, list) else [raw]):
            try:
                coerced.append(InterfaceMode.from_str(str(val)))
            except ValueError:
                log.warning("Unknown interface in tool config",
                            extra={'data': f"value={val!r}"})
        return coerced

    def _load_module(self, name: str, path: str):
        """Import (or reimport) a tool module by file path."""
        module_key = f"_supernova_tool_{name}"
        try:
            spec   = importlib.util.spec_from_file_location(module_key, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_key] = module
            spec.loader.exec_module(module)
            return module
        except Exception as e:
            log.error(f"Error loading {path}", extra={'data': str(e)})
            return None