"""
Dynamic tool loader for Supernova.

Scans the tools/ folder for .py files and the config/ folder for matching .yaml
sidecar files. Reloads automatically when any file changes — no restart needed.

Each tool module must follow this convention:

    tools/my_tool.py
    ├── A schema function named the same as the file (e.g. def my_tool(...))
    │   with type annotations and a Google-style docstring — this is what gets
    │   passed to Ollama as the tool definition.
    ├── An execute(tool_args, session, core, tool_config) function — the actual
    │   implementation called when the LLM invokes the tool.
    └── An optional provide_context(core, tool_config) -> str function — if
        present, called on every request to inject text into the system prompt.

    config/my_tool.yaml  (optional sidecar)
    ├── enabled: true           # set false to exclude without deleting the file
    ├── voice_only: false       # only include when mode != PLAIN
    ├── requires_config: ptv    # only include if AppConfig has this attribute set
    ├── context_priority: 50    # lower = earlier in system prompt if tool injects to system prompt (default 50)
    └── ... any other tool-specific config values

Multi-tool files export a TOOLS list instead of a single schema function:
    TOOLS = [
        {'schema': fn, 'name': 'tool_name', 'execute': execute_fn},
        ...
    ]

The loader passes tool_config (the parsed yaml dict) into execute() and
provide_context() so tools can read their own settings without touching
AppConfig directly.
"""

import importlib
import importlib.util
import os
import sys
import yaml

from core.precontext import VoiceMode


class ToolLoader:
    def __init__(self, tools_dir: str, config_dir: str, app_config):
        self.tools_dir  = tools_dir
        self.config_dir = config_dir
        self.app_config = app_config

        # Cached state — rebuilt whenever files change
        self._schema_functions       = []   # general tools passed to Ollama
        self._voice_schema_functions = []   # voice-only tool additions
        self._executors              = {}   # name -> callable(tool_args, session, core)
        self._tool_configs           = {}   # name -> yaml dict
        self._context_providers      = []   # list of (priority, name, callable(core, tool_config), tool_config)
        self._last_mtime             = 0.0

        # Force an initial load
        self._reload()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_tools(self, mode: VoiceMode = VoiceMode.PLAIN) -> list:
        """Return the current schema function list for Ollama's tools= parameter."""
        self._reload_if_changed()
        if mode != VoiceMode.PLAIN:
            return self._schema_functions + self._voice_schema_functions
        return list(self._schema_functions)

    def get_executor(self, tool_name: str):
        """Return the execute callable for a tool, or None if not found."""
        self._reload_if_changed()
        return self._executors.get(tool_name)

    def get_tool_config(self, tool_name: str) -> dict:
        """Return the yaml config dict for a tool (empty dict if no yaml)."""
        return self._tool_configs.get(tool_name, {})

    def get_context_injections(self, core) -> list[str]:
        """
        Call all registered context providers in priority order and return
        a list of non-empty strings to inject into the system prompt.

        Lower context_priority number = earlier in the prompt.
        """
        self._reload_if_changed()
        results = []
        for priority, name, provider_fn, tool_config in self._context_providers:
            try:
                text = provider_fn(core, tool_config)
                if text and text.strip():
                    results.append(text.strip())
            except Exception as e:
                print(f"[tool_loader] Context provider error in {name}: {e}")
        return results

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    def _current_mtime(self) -> float:
        """
        Return the latest mtime across all .py and .yaml files in both
        the tools dir and the config dir. Catches new files, deletions,
        and edits to existing files.
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

    # ------------------------------------------------------------------
    # Core reload logic
    # ------------------------------------------------------------------

    def _reload(self):
        """Scan tools/ and config/ and rebuild the schema, executor, and context provider tables."""
        schema_functions       = []
        voice_schema_functions = []
        executors              = {}
        tool_configs           = {}
        context_providers      = []   # list of (priority, name, fn, tool_config)

        try:
            tool_files = sorted(
                f for f in os.listdir(self.tools_dir)
                if f.endswith('.py') and not f.startswith('_')
            )
        except OSError as e:
            print(f"[tool_loader] Cannot read tools dir: {e}")
            return

        for filename in tool_files:
            name = filename[:-3]  # strip .py

            # Load sidecar yaml from config dir if present
            yaml_path   = os.path.join(self.config_dir, f"{name}.yaml")
            tool_config = {}
            if os.path.exists(yaml_path):
                try:
                    with open(yaml_path) as f:
                        tool_config = yaml.safe_load(f) or {}
                except Exception as e:
                    print(f"[tool_loader] Error reading {yaml_path}: {e}")

            tool_configs[name] = tool_config

            # Check enabled flag (default true)
            if not tool_config.get('enabled', True):
                print(f"[tool_loader] Skipping {name} (disabled in yaml)")
                continue

            # Check requires_config — skip if AppConfig doesn't have that attribute set
            requires = tool_config.get('requires_config')
            if requires and not getattr(self.app_config, requires, None):
                print(f"[tool_loader] Skipping {name} (requires config.{requires} which is not set)")
                continue

            # Load (or reload) the module
            module = self._load_module(name, os.path.join(self.tools_dir, filename))
            if module is None:
                continue

            # ── Context provider (optional) ───────────────────────────
            # If the module exposes provide_context(core, tool_config) -> str,
            # register it sorted by context_priority (default 50).
            provider_fn = getattr(module, 'provide_context', None)
            if provider_fn is not None and callable(provider_fn):
                priority = tool_config.get('context_priority', 50)
                context_providers.append((priority, name, provider_fn, tool_config))
                print(f"[tool_loader] Registered context provider: {name} (priority={priority})")

            # ── Executor closure ──────────────────────────────────────
            # Captures tool_config at load time so each tool gets its own config
            def make_executor(fn, tc):
                def executor(tool_args, session, core):
                    return fn(tool_args, session, core, tc)
                return executor

            is_voice_only = tool_config.get('voice_only', False)

            # ── Multi-tool export (TOOLS list) ────────────────────────
            multi = getattr(module, 'TOOLS', None)
            if multi is not None:
                for entry in multi:
                    t_schema  = entry.get('schema')
                    t_name    = entry.get('name')
                    t_execute = entry.get('execute')
                    if not (t_schema and t_name and t_execute):
                        print(f"[tool_loader] Malformed TOOLS entry in {name}.py, skipping entry")
                        continue
                    if is_voice_only:
                        voice_schema_functions.append(t_schema)
                    else:
                        schema_functions.append(t_schema)
                    executors[t_name] = make_executor(t_execute, tool_config)
                    print(f"[tool_loader] Loaded tool: {t_name} (from {name}.py)" + (" (voice only)" if is_voice_only else ""))
                continue

            # ── Single-tool convention ────────────────────────────────
            schema_fn = getattr(module, name, None)
            if schema_fn is None or not callable(schema_fn):
                print(f"[tool_loader] {name}.py has no schema function named '{name}' and no TOOLS list, skipping")
                continue

            execute_fn = getattr(module, 'execute', None)
            if execute_fn is None or not callable(execute_fn):
                print(f"[tool_loader] {name}.py has no execute() function, skipping")
                continue

            if is_voice_only:
                voice_schema_functions.append(schema_fn)
            else:
                schema_functions.append(schema_fn)

            executors[name] = make_executor(execute_fn, tool_config)
            print(f"[tool_loader] Loaded tool: {name}" + (" (voice only)" if is_voice_only else ""))

        # Sort context providers by priority so lower numbers appear first
        context_providers.sort(key=lambda x: x[0])

        self._schema_functions       = schema_functions
        self._voice_schema_functions = voice_schema_functions
        self._executors              = executors
        self._tool_configs           = tool_configs
        self._context_providers      = context_providers
        self._last_mtime             = self._current_mtime()
        print(
            f"[tool_loader] Loaded {len(schema_functions)} general tool(s), "
            f"{len(voice_schema_functions)} voice-only tool(s), "
            f"{len(context_providers)} context provider(s)"
        )

    def _load_module(self, name: str, path: str):
        """Import (or reimport) a tool module by file path."""
        module_key = f"_supernova_tool_{name}"
        try:
            spec   = importlib.util.spec_from_file_location(module_key, path)
            module = importlib.util.module_from_spec(spec)
            # Always reload from disk — don't use the cached sys.modules version
            sys.modules[module_key] = module
            spec.loader.exec_module(module)
            return module
        except Exception as e:
            print(f"[tool_loader] Error loading {path}: {e}")
            return None