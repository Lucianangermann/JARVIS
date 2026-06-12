"""SystemMacExecMixin — vision, system commands, macOS dispatcher, Safari.

Mixed into Brain. All self.* attributes are satisfied by Brain.__init__.
"""
from __future__ import annotations

import json
from typing import Any


class SystemMacExecMixin:
    """Exec methods for vision tools, system_command, mac_action,
    confirm_action, and Safari browser control."""

    def _exec_vision_tool(
        self, name: str, tool_input: dict[str, Any],
    ) -> tuple[str, bool]:
        if self.vision is None:  # type: ignore[attr-defined]
            return "vision unavailable (deps missing or init failed)", True
        try:
            if name == "analyze_screen":
                question = (tool_input or {}).get("question") or "describe"
                if not isinstance(question, str):
                    return "`question` must be a string.", True
                result = self.vision.screen.analyze_screen(question)  # type: ignore[attr-defined]
            elif name == "check_screen_for_errors":
                result = self.vision.screen.detect_error_on_screen()  # type: ignore[attr-defined]
            elif name == "read_screen_text":
                result = self.vision.screen.analyze_screen("read")  # type: ignore[attr-defined]
            elif name == "scan_document":
                image = (tool_input or {}).get("image") or ""
                doc_type = str((tool_input or {}).get("doc_type") or "auto")
                if not image:
                    return "image (base64) ist erforderlich.", True
                res = self.vision.scanner.scan_document(image, doc_type=doc_type)  # type: ignore[attr-defined]
                if res is None:
                    return "Dokument konnte nicht verarbeitet werden.", True
                result = res.summary or res.raw_text or "Kein Inhalt extrahiert."
            elif name == "translate_image":
                image = (tool_input or {}).get("image") or ""
                target = str((tool_input or {}).get("target_language") or "de")
                if not image:
                    return "image (base64) ist erforderlich.", True
                res = self.vision.translator.translate_image(image, target_language=target)  # type: ignore[attr-defined]
                if res is None:
                    return "Übersetzung fehlgeschlagen.", True
                result = (f"Original: {res.original}\n"
                          f"Übersetzung ({res.target_language}): {res.translated}")
            elif name == "identify_object":
                image = (tool_input or {}).get("image") or ""
                subject = str((tool_input or {}).get("subject") or "object").lower()
                if not image:
                    return "image (base64) ist erforderlich.", True
                recognizer = self.vision.recognizer  # type: ignore[attr-defined]
                _method_map = {
                    "plant": recognizer.identify_plant,
                    "food": recognizer.identify_food,
                    "animal": recognizer.identify_animal,
                    "damage": recognizer.assess_damage,
                    "style": recognizer.style_advice,
                }
                fn = _method_map.get(subject, recognizer.identify)
                res = fn(image)
                if res is None:
                    return "Objekt konnte nicht erkannt werden.", True
                result = res.summary
            else:
                return f"Unknown vision tool {name!r}.", True
        except Exception as exc:  # noqa: BLE001
            return f"vision tool {name!r} crashed: {exc}", True
        if not result:
            return (
                "vision call returned no result — likely Screen "
                "Recording permission missing or capture failed",
                True,
            )
        return result, False

    def _exec_system_command(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        from .. import command_guard as _cg
        command = (tool_input or {}).get("command", "")
        args = (tool_input or {}).get("args") or {}
        if not isinstance(args, dict):
            return "`args` must be an object.", True
        try:
            return _cg.execute(command, args), False
        except ValueError as exc:
            return str(exc), True

    def _exec_mac_action(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        from ..mac_control import dispatcher as _md
        action_name = (tool_input or {}).get("action", "")
        params = (tool_input or {}).get("params") or {}
        if not isinstance(params, dict):
            return "`params` must be an object.", True
        envelope = _md.dispatch(action_name, params)
        is_error = envelope.get("status") == "rejected"
        return json.dumps(envelope, ensure_ascii=False), is_error

    def _exec_confirm_action(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        from ..mac_control import dispatcher as _md
        from ..mac_control import confirmation as _cf
        pid = (tool_input or {}).get("id", "")
        approve = bool((tool_input or {}).get("approve", False))
        if not isinstance(pid, str) or not pid:
            return "`id` is required and must be a string.", True
        peek = _cf.peek(pid)
        if peek is None:
            return (json.dumps({"status": "rejected",
                                "reason": "Pending id unknown or expired."}), True)
        if peek.requires_password:
            return (json.dumps({
                "status": "rejected",
                "tier": peek.tier,
                "reason": ("Tier 4 cannot be confirmed via chat. Ask the user "
                           "to enter the JARVIS password in the web UI."),
            }), True)
        envelope = _md.consume(pid) if approve else _md.cancel(pid)
        is_error = envelope.get("status") == "rejected"
        return json.dumps(envelope, ensure_ascii=False), is_error

    def _exec_safari_control(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        from ..tools.safari_tool import (
            open_url, search_in_safari, current_url, current_title,
            current_page_text, navigate_back, navigate_forward, open_new_tab,
        )
        inp = tool_input or {}
        action = inp.get("action", "")
        if action == "open_url":
            url = inp.get("url", "")
            if not url:
                return "url ist erforderlich.", True
            return open_url(url)
        if action == "search":
            query = inp.get("query", "")
            if not query:
                return "query ist erforderlich.", True
            return search_in_safari(query)
        if action == "current_url":
            return current_url()
        if action == "current_title":
            return current_title()
        if action == "read_page":
            return current_page_text()
        if action == "back":
            return navigate_back()
        if action == "forward":
            return navigate_forward()
        if action == "new_tab":
            return open_new_tab(inp.get("url"))
        return f"Unbekannte Aktion: {action}", True
