"""Bounded stdio transport for the dashboard's native Codex agent turns.

Authentication stays in the signed-in user's CODEX_HOME. All model tools except
the explicitly registered dashboard functions are disabled before loading a thread.
"""
from collections import deque
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time
import tomllib

from . import call_analysis


TURN_TIMEOUT = 900
STARTUP_TIMEOUT = 30
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_TOOL_BYTES = 15 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024

# Keep these explicit so a CLI update cannot silently re-enable a capability.
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "shell_snapshot", "apps", "plugins", "remote_plugin",
    "plugin_discovery_prefetch", "plugin_sharing", "hooks", "browser_use",
    "browser_use_external", "browser_use_full_cdp_access", "in_app_browser",
    "computer_use", "image_generation", "view_image", "multi_agent", "multi_agent_v2",
    "code_mode", "artifact", "workspace_dependencies", "goals",
    "memories", "skill_search", "skill_mcp_dependency_install", "sleep_tool",
    "request_permissions_tool", "default_mode_request_user_input", "tool_suggest",
    "recommended_plugins", "unbounded_connection_retries", "standalone_web_search",
    "guardian_approval", "guardianv2", "guardianv3", "agent_message_board",
)


def _config_overrides(cwd):
    """Disable exact inherited entries because TOML table overrides deep-merge."""
    settings = [f"features.{name}=false" for name in DISABLED_FEATURES]
    settings += [
        'model_provider="openai"', 'approval_policy="never"', 'approvals_reviewer="user"',
        'sandbox_mode="read-only"', 'web_search="disabled"', 'notify=[]',
        'project_doc_max_bytes=0', 'developer_instructions=""',
        'orchestrator.mcp.enabled=false', 'cloud.skills.enabled=false',
        'features.skip_host_skill_discovery=true', 'skills.include_instructions=false',
        'features.code_mode_host=true',
        'agents.enabled=false',
        'plugins._default.enabled=false', 'apps._default.enabled=false',
        'analytics.enabled=false', 'otel.exporter="none"', 'otel.metrics_exporter="none"',
    ]
    user_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    paths = {user_home / "config.toml"}
    paths.update(parent / ".codex" / "config.toml" for parent in (cwd, *cwd.parents))
    inherited = {section: set() for section in ("mcp_servers", "plugins", "apps")}

    def disable_entries(config):
        for section in ("mcp_servers", "plugins", "apps"):
            for name in config.get(section, {}):
                inherited[section].add(name)
        for profile in config.get("profiles", {}).values():
            disable_entries(profile)

    for path in sorted(paths):
        if path.is_file():
            try:
                disable_entries(tomllib.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, AttributeError):
                raise RuntimeError("Не удалось проверить изоляцию настроек Codex") from None
    for section, names in inherited.items():
        if section == "mcp_servers":
            for name in sorted(names):
                if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                    raise RuntimeError("Не удалось изолировать имя MCP-сервера в настройках Codex")
                settings.append(f"mcp_servers.{name}.enabled=false")
            continue
        # CLI dotted paths do not unquote segments. Quote map keys inside a TOML
        # value instead, preserving names containing dots, @ signs or spaces.
        entries = ", ".join(json.dumps(name) + "={enabled=false}" for name in sorted(names))
        settings.append(f"{section}={{{entries}}}")
    return settings


def _verify_isolation(config):
    features = config.get("features") or {}
    safe = all(features.get(name) is False for name in DISABLED_FEATURES)
    safe = safe and config.get("web_search") == "disabled" and config.get("notify") == []
    safe = safe and config.get("project_doc_max_bytes") == 0
    safe = safe and config.get("developer_instructions") == ""
    safe = safe and (config.get("orchestrator", {}).get("mcp") or {}).get("enabled") is False
    safe = safe and config.get("skills", {}).get("include_instructions") is False
    safe = safe and features.get("code_mode_host") is True
    for section in ("mcp_servers", "plugins", "apps"):
        safe = safe and all(entry is None or isinstance(entry, dict) and entry.get("enabled") is False
                            for entry in (config.get(section) or {}).values())
    # A custom override of the built-in provider could forward company data elsewhere.
    safe = safe and config.get("model_provider") == "openai"
    safe = safe and not (config.get("model_providers") or {}).get("openai")
    if not safe:
        raise RuntimeError("Не удалось подтвердить изоляцию инструментов Codex")


class AppServer:
    def __init__(self, config, cwd):
        self.config = config
        self.cwd = Path(cwd).resolve()
        self.process = None
        self._readers = []
        self._messages = queue.Queue(maxsize=256)
        self._writes = queue.Queue(maxsize=2)
        self._backlog = deque()
        self._reader_failed = threading.Event()
        self._writer_failed = threading.Event()
        self._sequence = 0
        self._thread_id = None
        self._turn_id = None
        self._cancel_event = None
        self._deadline = time.monotonic() + STARTUP_TIMEOUT
        self._catalog_dir = None
        self._models = set()

    def _create_catalog(self):
        """Retain public model metadata while removing native file/tool capabilities."""
        user_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        try:
            cache = user_home / "models_cache.json"
            if cache.stat().st_size > MAX_MESSAGE_BYTES:
                raise ValueError("oversized model catalog")
            models = json.loads(cache.read_text(encoding="utf-8"))["models"]
            if not isinstance(models, list) or not models:
                raise ValueError("missing model catalog")
            for model in models:
                self._models.add(model["slug"])
                model.update(apply_patch_tool_type=None, experimental_supported_tools=[],
                             node_repl_disabled=True,
                             include_apps_usage_instructions=False, include_plugin_usage_instructions=False,
                             include_skills_usage_instructions=False)
        except (OSError, ValueError, KeyError, TypeError):
            raise RuntimeError("Не удалось загрузить список моделей Codex. Откройте Codex CLI под подключённой учётной записью и повторите запрос.") from None
        self._catalog_dir = tempfile.TemporaryDirectory(prefix="native-models-", dir=self.cwd)
        catalog_path = Path(self._catalog_dir.name) / "models.json"
        # Only the public models list is copied. Account identity and auth stay in CODEX_HOME.
        catalog_path.write_text(json.dumps({"models": models}, ensure_ascii=False), encoding="utf-8")
        return catalog_path

    def __enter__(self):
        try:
            self.cwd.mkdir(parents=True, exist_ok=True)
            command = [call_analysis.codex_path(self.config), "app-server", "--stdio"]
            for setting in _config_overrides(self.cwd):
                command.extend(("-c", setting))
            command.extend(("-c", "model_catalog_json=" + json.dumps(str(self._create_catalog()))))
            env = os.environ.copy()
            # Host integration pipes and API credentials must not reach this child.
            for key in tuple(env):
                if key.startswith(("BROWSER_USE_", "SKY_", "NODE_REPL_", "OPENAI_")) or key in (
                        "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"):
                    env.pop(key)
            self.process = subprocess.Popen(
                command, cwd=str(self.cwd), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=os.name != "nt", creationflags=0x08000000 if os.name == "nt" else 0,
            )
            self._readers = [threading.Thread(target=self._read_stdout, daemon=True),
                             threading.Thread(target=self._drain_stderr, daemon=True),
                             threading.Thread(target=self._write_stdin, daemon=True)]
            for reader in self._readers:
                reader.start()
            self._rpc("initialize", {"clientInfo": {"name": "ttkgpt", "version": "1"},
                                     "capabilities": {"experimentalApi": True}})
            self._send({"method": "initialized"})
            effective = self._rpc("config/read", {"cwd": str(self.cwd), "includeLayers": False})
            _verify_isolation(effective.get("config") or {})
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_args):
        self.close()

    def close(self):
        process = self.process
        if process is not None:
            try:
                self._writes.put_nowait(None)
            except queue.Full:
                pass
            if process.poll() is None:
                call_analysis._kill_cli_tree(process)
            for reader in self._readers:
                reader.join(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()
        self.process = None
        if self._catalog_dir is not None:
            self._catalog_dir.cleanup()
            self._catalog_dir = None

    def _read_stdout(self):
        stream = self.process.stdout
        try:
            while True:
                line = stream.readline(MAX_MESSAGE_BYTES + 1)
                if not line:
                    self._messages.put_nowait(None)
                    return
                if len(line) > MAX_MESSAGE_BYTES:
                    raise ValueError("oversized message")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("invalid message")
                self._messages.put_nowait(message)
        except (OSError, ValueError, queue.Full):
            self._reader_failed.set()

    def _drain_stderr(self):
        # Discard provider diagnostics. They may contain paths, credentials or tool data.
        stream = self.process.stderr
        try:
            while stream.read(8192):
                pass
        except (OSError, ValueError):
            pass

    def _write_stdin(self):
        stream = self.process.stdin
        while True:
            task = self._writes.get()
            if task is None:
                return
            data, done = task
            try:
                stream.write(data)
                stream.flush()
            except (OSError, ValueError):
                self._writer_failed.set()
                done.set()
                return
            done.set()

    def _send(self, message, wait=True):
        data = (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(data) > MAX_MESSAGE_BYTES:
            raise RuntimeError("Превышен допустимый размер сообщения Codex")
        done = threading.Event()
        try:
            self._writes.put_nowait((data, done))
        except queue.Full:
            raise RuntimeError("Codex превысил допустимый поток сообщений") from None
        if wait:
            while not done.wait(0.05):
                self._check_wait()
            if self._writer_failed.is_set():
                raise RuntimeError("Соединение с Codex завершено")

    def _check_wait(self):
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise RuntimeError("Запрос отменён")
        if time.monotonic() >= self._deadline:
            raise RuntimeError("Превышено время ожидания ответа Codex")
        if self._reader_failed.is_set():
            raise RuntimeError("Codex вернул некорректное сообщение")
        if self._writer_failed.is_set():
            raise RuntimeError("Соединение с Codex завершено")

    def _receive(self):
        while True:
            self._check_wait()
            try:
                message = self._messages.get(timeout=min(0.1, max(0.001, self._deadline - time.monotonic())))
            except queue.Empty:
                continue
            if message is None:
                raise RuntimeError("Соединение с Codex завершено")
            return message

    def _rpc(self, method, params):
        self._sequence += 1
        request_id = self._sequence
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            message = self._receive()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RuntimeError("Codex не смог выполнить запрос")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("Codex вернул некорректный ответ")
                return result
            incoming = message.get("method")
            if "id" in message and incoming and not (method == "turn/start" and incoming == "item/tool/call"):
                self._deny_request(message)
                continue
            if method == "turn/start" and incoming == "turn/started":
                notification = message.get("params") or {}
                if notification.get("threadId") == self._thread_id:
                    self._turn_id = (notification.get("turn") or {}).get("id")
            if len(self._backlog) >= 256:
                raise RuntimeError("Codex превысил допустимый поток сообщений")
            self._backlog.append(message)

    def _deny_request(self, message):
        self._send({"id": message["id"], "error": {"code": -32601,
                    "message": "This client does not permit this request"}})

    def _interrupt(self):
        if self._thread_id and self._turn_id and self.process is not None:
            try:
                self._sequence += 1
                self._send({"id": self._sequence, "method": "turn/interrupt",
                            "params": {"threadId": self._thread_id, "turnId": self._turn_id}}, wait=False)
                # Allow the peer to consume the interrupt before terminating its tree.
                time.sleep(0.05)
            except RuntimeError:
                pass

    def _tool_call(self, message, allowed_tools, tool_handler):
        params = message.get("params") or {}
        valid = (params.get("threadId") == self._thread_id and params.get("turnId") == self._turn_id
                 and not params.get("namespace") and params.get("tool") in allowed_tools
                 and isinstance(params.get("arguments"), dict))
        result = {"success": False, "contentItems": [{"type": "inputText",
                  "text": "Вызов инструмента отклонён. Проверьте имя, аргументы и доступ."}]}
        if valid:
            completed = queue.Queue(maxsize=1)

            def invoke():
                try:
                    completed.put((True, tool_handler(params["tool"], params["arguments"])))
                except Exception:
                    completed.put((False, None))

            threading.Thread(target=invoke, daemon=True).start()
            while True:
                self._check_wait()
                try:
                    success, content = completed.get(timeout=0.1)
                    break
                except queue.Empty:
                    continue
            if success and self._valid_content(content):
                result = {"success": True, "contentItems": content}
            else:
                result["contentItems"][0]["text"] = "Не удалось выполнить инструмент. Уточните запрос или выберите другой инструмент."
        self._send({"id": message["id"], "result": result})

    @staticmethod
    def _valid_content(content):
        if not isinstance(content, list) or len(content) > 100:
            return False
        for item in content:
            if not isinstance(item, dict):
                return False
            if item.get("type") == "inputText":
                if not isinstance(item.get("text"), str):
                    return False
            elif item.get("type") == "inputImage":
                if not isinstance(item.get("imageUrl"), str) or not item["imageUrl"].startswith("data:image/"):
                    return False
            else:
                return False
        return len(json.dumps(content, ensure_ascii=False).encode("utf-8")) <= MAX_TOOL_BYTES

    def run(self, thread_id, master, input, model, effort, fast, output_schema, tools,
            tool_handler, cancel_event, on_progress, on_thread, ephemeral=False):
        if self.process is None:
            raise RuntimeError("Соединение с Codex не открыто")
        self._deadline = time.monotonic() + TURN_TIMEOUT
        self._cancel_event = cancel_event
        self._turn_id = None
        try:
            self._check_wait()
            if model not in self._models:
                raise RuntimeError("Выбранная модель недоступна в подключённой учётной записи Codex")
            params = {"cwd": str(self.cwd), "model": model, "modelProvider": "openai",
                      "approvalPolicy": "never", "approvalsReviewer": "user", "sandbox": "read-only",
                      "serviceTier": "fast" if fast else "default", "runtimeWorkspaceRoots": []}
            if self._thread_id != thread_id or thread_id is None:
                if thread_id:
                    params.update(threadId=thread_id, excludeTurns=True)
                    response = self._rpc("thread/resume", params)
                else:
                    params["ephemeral"] = ephemeral
                    params.update(dynamicTools=tools, environments=[], selectedCapabilityRoots=[],
                                  allowProviderModelFallback=False, baseInstructions=master, developerInstructions="")
                    response = self._rpc("thread/start", params)
                actual_id = (response.get("thread") or {}).get("id")
                if not isinstance(actual_id, str) or not actual_id or thread_id and actual_id != thread_id:
                    raise RuntimeError("Codex вернул другой идентификатор чата")
                self._thread_id = actual_id
            on_thread(self._thread_id)
            response = self._rpc("turn/start", {
                "threadId": self._thread_id, "input": input, "model": model, "effort": effort,
                "serviceTier": "fast" if fast else "default", "outputSchema": output_schema,
                "approvalPolicy": "never", "approvalsReviewer": "user", "environments": [],
                "runtimeWorkspaceRoots": [], "sandboxPolicy": {"type": "readOnly"},
            })
            self._turn_id = (response.get("turn") or {}).get("id")
            if not isinstance(self._turn_id, str) or not self._turn_id:
                raise RuntimeError("Codex не вернул идентификатор ответа")
            on_progress("Обрабатываю запрос…")
            allowed_tools = {spec["name"] for spec in tools if spec.get("type") == "function"}
            answers = {}
            while True:
                self._check_wait()
                message = self._backlog.popleft() if self._backlog else self._receive()
                method = message.get("method")
                params = message.get("params") or {}
                if "id" in message and method:
                    if method == "item/tool/call":
                        self._tool_call(message, allowed_tools, tool_handler)
                    else:
                        self._deny_request(message)
                    continue
                if params.get("threadId") != self._thread_id:
                    continue
                if method == "turn/completed":
                    turn = params.get("turn") or {}
                    if turn.get("id") != self._turn_id:
                        continue
                    if turn.get("status") != "completed":
                        raise RuntimeError("Codex не завершил ответ. Повторите запрос.")
                    for item in turn.get("items", []):
                        if item.get("type") == "agentMessage":
                            answers[item["id"]] = item
                    final = [item for item in answers.values() if item.get("phase") == "final_answer"]
                    text = (final or list(answers.values()))[-1].get("text", "") if answers else ""
                    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                        raise RuntimeError("Ответ Codex превышает допустимый размер")
                    try:
                        result = json.loads(text)
                    except (ValueError, TypeError):
                        raise RuntimeError("Codex вернул ответ в неверном формате") from None
                    if not isinstance(result, dict):
                        raise RuntimeError("Codex вернул ответ в неверном формате")
                    self._turn_id = None
                    return result
                if params.get("turnId") != self._turn_id:
                    continue
                if method in ("item/started", "item/completed"):
                    item = params.get("item") or {}
                    if item.get("type") not in ("userMessage", "agentMessage", "reasoning", "dynamicToolCall", "contextCompaction"):
                        raise RuntimeError("Codex попытался использовать недоступный инструмент")
                    if method == "item/completed" and item.get("type") == "agentMessage":
                        answers[item["id"]] = item
                    if item.get("type") == "dynamicToolCall":
                        on_progress("Проверяю данные…")
                elif method == "item/agentMessage/delta":
                    # Final JSON may contain private intermediate data. Publish only safe status.
                    on_progress("Готовлю ответ…")
        except BaseException:
            self._interrupt()
            self.close()
            raise
        finally:
            self._cancel_event = None
