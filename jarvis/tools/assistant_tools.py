"""Assistant-level tools: memory, asking the user, progress updates, shortcuts."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ..security.policy import Risk
from .base import Tool, ToolContext, ToolResult, ToolSpec, Verification

log = logging.getLogger(__name__)


class RememberTool(Tool):
    spec = ToolSpec(
        name="remember",
        description=(
            "Save something durable about the user for future sessions: a "
            "preference ('calls me Alex'), a frequently used application or "
            "folder, favourite music, a routine. category: preference, app, "
            "location, music, person, routine, device, workflow, other. Do NOT "
            "save passwords, PINs, tokens, card numbers or any other secret - "
            "they are rejected. Everything stays in a local SQLite file."),
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string",
                        "description": "Short label, e.g. 'favourite album'."},
                "value": {"type": "string", "description": "What to remember."},
                "category": {"type": "string",
                             "enum": ["preference", "app", "location", "music",
                                      "person", "routine", "device", "workflow",
                                      "other"]},
                "confidence": {"type": "number", "description": "0..1 (default 0.8)."},
                "pinned": {"type": "boolean",
                           "description": "Always include this in the prompt."},
            },
            "required": ["key", "value"],
        },
        category="memory", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, key: str = "", value: str = "", category: str = "other",
            confidence: float = 0.8, pinned: bool = False, **_: Any) -> ToolResult:
        store = self.ctx.memory
        if store is None:
            return self._fail("Memory is not available.")
        if not store.enabled:
            return ToolResult(ok=False,
                              error="Memory is disabled in Settings > Memory.")
        fact = store.add_fact(category=category, key=key, value=value,
                              source="conversation",
                              confidence=max(0.0, min(1.0, float(confidence or 0.8))),
                              pinned=bool(pinned))
        if fact is None:
            return ToolResult(
                ok=False,
                error="That value looks like a credential, so it was not stored. "
                      "JARVIS never keeps passwords, PINs, tokens or card numbers.")
        return ToolResult(ok=True, message=f"Remembered: {fact.key} = {fact.value}",
                          data=fact.as_dict(), evidence={"stored": True,
                                                         "id": fact.id})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        if not ev.get("stored"):
            return Verification(checked=True, succeeded=False,
                                detail="Nothing was stored", evidence=ev)
        facts = self.ctx.memory.search_facts(str(args.get("key", "")), limit=3)
        ok = any(f.id == ev.get("id") for f in facts) or bool(facts)
        return Verification(checked=True, succeeded=ok,
                            detail=("Read back from the database" if ok
                                    else "Could not read the fact back"),
                            evidence=ev)


class RecallTool(Tool):
    spec = ToolSpec(
        name="recall",
        description="Search JARVIS's saved memory for facts, preferences, past "
                    "tasks and user-defined shortcuts matching a query. Use it "
                    "when the user refers to something previously discussed "
                    "('my usual folder', 'that project I told you about').",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"},
                           "limit": {"type": "integer"}},
            "required": ["query"],
        },
        category="read_only", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, query: str = "", limit: int = 8, **_: Any) -> ToolResult:
        store = self.ctx.memory
        if store is None:
            return self._fail("Memory is not available.")
        facts = store.search_facts(query, limit=max(1, min(int(limit or 8), 30)))
        tasks = store.similar_tasks(query, limit=3)
        shortcut = store.match_shortcut(query)
        data = {
            "query": query,
            "facts": [f.as_dict() for f in facts],
            "past_tasks": [{"request": t.request, "status": t.status,
                            "summary": t.summary,
                            "when": t.ts} for t in tasks],
            "shortcut": shortcut,
        }
        if not facts and not tasks and not shortcut:
            return ToolResult(ok=False,
                              error=f"Nothing in memory matches '{query}'.",
                              data=data, evidence={"count": 0})
        return ToolResult(ok=True,
                          message=f"{len(facts)} memory item(s) for '{query}'",
                          data=data,
                          evidence={"facts": len(facts), "tasks": len(tasks),
                                    "shortcut": bool(shortcut)})


class ForgetTool(Tool):
    spec = ToolSpec(
        name="forget",
        description="Delete a stored memory. Give either the fact id, or a query "
                    "to match. Use 'everything' to wipe all memories and "
                    "conversation history (this asks the user first).",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to match, or 'everything'."},
                "fact_id": {"type": "integer"},
                "scope": {"type": "string",
                          "enum": ["fact", "facts_matching", "everything"]},
            },
        },
        category="memory", risk=Risk.ALLOWED, reversible=False)

    def dynamic_risk(self, args: Dict[str, Any]) -> Risk:
        scope = str(args.get("scope", "")).lower()
        query = str(args.get("query", "")).lower()
        if scope == "everything" or query == "everything":
            return Risk.APPROVAL
        return Risk.ALLOWED

    def run(self, query: str = "", fact_id: Optional[int] = None,
            scope: str = "fact", **_: Any) -> ToolResult:
        store = self.ctx.memory
        if store is None:
            return self._fail("Memory is not available.")
        if fact_id:
            ok = store.delete_fact(int(fact_id))
            return ToolResult(ok=ok,
                              message=("Deleted that memory" if ok else
                                       "No memory with that id"),
                              data={"deleted": ok, "id": fact_id},
                              evidence={"deleted": ok})
        scope = (scope or "fact").lower()
        if scope == "everything" or (query or "").lower() == "everything":
            report = store.clear_all()
            return ToolResult(ok=True, message="All memories and history cleared",
                              data=report, evidence=report)
        matches = store.search_facts(query, limit=25)
        if not matches:
            return ToolResult(ok=False, error=f"No memory matches '{query}'.",
                              data={"query": query})
        if scope == "fact":
            matches = matches[:1]
        deleted = [f.id for f in matches if store.delete_fact(int(f.id))]
        return ToolResult(ok=bool(deleted), message=f"Deleted {len(deleted)} memory item(s)",
                          data={"deleted": deleted,
                                "items": [f.key for f in matches]},
                          evidence={"deleted": len(deleted)})


class AskUserTool(Tool):
    spec = ToolSpec(
        name="ask_user",
        description=(
            "Ask the user a question and wait for the answer. Use it when you are "
            "genuinely blocked: an ambiguous request, several equally good "
            "options, a repeated failure, or missing information you cannot "
            "observe. Provide 2-4 short options when you can. This is NOT how "
            "sensitive actions get approved - those go through the permission "
            "system automatically."),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"},
                            "description": "Optional short list of choices."},
                "timeout_s": {"type": "number", "description": "Default 240."},
            },
            "required": ["question"],
        },
        category="query", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, question: str = "", options: Optional[List[str]] = None,
            timeout_s: float = 240.0, **_: Any) -> ToolResult:
        broker = self.ctx.approvals
        if broker is None:
            return self._fail("There is no channel to the user right now.")
        answer = broker.ask(question or "", options=options or [], timeout_s=timeout_s)
        if not answer:
            return ToolResult(ok=False,
                              error="The user did not answer. Continue with the "
                                    "safest interpretation, or stop and report "
                                    "what you need.",
                              data={"question": question, "answer": ""},
                              evidence={"answered": False})
        return ToolResult(ok=True, message=f"The user answered: {answer}",
                          data={"question": question, "answer": answer,
                                "options": options or []},
                          evidence={"answered": True, "answer": answer[:200]})


class ReportProgressTool(Tool):
    spec = ToolSpec(
        name="report_progress",
        description=(
            "Give the user a short spoken/visible progress update during a long "
            "task ('Searching Spotify now', 'Waiting for Chrome to load'). Keep "
            "it under about eight words. Use it sparingly - not for every click. "
            "The final result must come from your last message, not from here."),
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "speak": {"type": "boolean", "description": "Also say it aloud "
                                                            "(default from settings)."},
                "percent": {"type": "number", "description": "Optional 0-100."},
            },
            "required": ["message"],
        },
        category="query", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, message: str = "", speak: Optional[bool] = None,
            percent: Optional[float] = None, **_: Any) -> ToolResult:
        message = (message or "").strip()
        if not message:
            return self._fail("Nothing to report.")
        message = message[:240]
        from ..events import EventType

        self.ctx.emit(EventType.TASK_PROGRESS,
                      {"message": message, "percent": percent})
        should_speak = speak if speak is not None else \
            bool(self.settings.speech.speak_progress_updates)
        spoken = False
        if should_speak and self.ctx.speech is not None:
            try:
                spoken = bool(self.ctx.speech.tts.speak_async(message))
            except Exception as exc:  # noqa: BLE001
                log.debug("progress speech failed: %s", exc)
        return ToolResult(ok=True, message=message,
                          data={"spoken": spoken, "percent": percent},
                          evidence={"delivered": True, "spoken": spoken})


class SaveShortcutTool(Tool):
    spec = ToolSpec(
        name="save_shortcut",
        description=(
            "Save a user-defined shortcut: a phrase that should replay a plan. "
            "For example phrase='morning setup' with steps=['open Spotify', "
            "'play my daily mix', 'open Chrome to the news']. When the user later "
            "says the phrase, JARVIS follows the saved steps. Ask the user to "
            "confirm the phrase and the steps before saving."),
        parameters={
            "type": "object",
            "properties": {
                "phrase": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "string"}},
                "description": {"type": "string"},
            },
            "required": ["phrase", "steps"],
        },
        category="memory", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, phrase: str = "", steps: Optional[List[str]] = None,
            description: str = "", **_: Any) -> ToolResult:
        store = self.ctx.memory
        if store is None:
            return self._fail("Memory is not available.")
        phrase = (phrase or "").strip()
        steps = [str(s).strip() for s in (steps or []) if str(s).strip()][:20]
        if not phrase or not steps:
            return self._fail("Both a phrase and at least one step are required.")
        payload = {"steps": steps, "description": description}
        ok = store.add_shortcut(phrase, payload)
        return ToolResult(ok=ok, message=(f"Saved shortcut '{phrase}'" if ok
                                          else "Could not save the shortcut"),
                          data={"phrase": phrase, "steps": steps},
                          evidence={"saved": ok})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        store = self.ctx.memory
        match = store.match_shortcut(str(args.get("phrase", ""))) if store else None
        ok = bool(match)
        return Verification(checked=True, succeeded=ok,
                            detail=("Shortcut reads back from the database" if ok
                                    else "Shortcut was not stored"),
                            evidence={"matched": ok})


class ListMemoryTool(Tool):
    spec = ToolSpec(
        name="list_memory",
        description="List everything JARVIS currently remembers: facts by "
                    "category, saved shortcuts and recent tasks. Use it to answer "
                    "'what do you know about me?' and to double-check before "
                    "saving something new.",
        parameters={
            "type": "object",
            "properties": {"category": {"type": "string"},
                           "limit": {"type": "integer"}},
        },
        category="read_only", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, category: str = "", limit: int = 40, **_: Any) -> ToolResult:
        store = self.ctx.memory
        if store is None:
            return self._fail("Memory is not available.")
        facts = store.list_facts(category=category, limit=max(1, min(int(limit or 40), 200)))
        data = {"facts": [f.as_dict() for f in facts],
                "shortcuts": store.shortcuts(),
                "recent_tasks": [{"request": t.request, "status": t.status,
                                  "summary": t.summary} for t in store.list_tasks(limit=8)],
                "stats": store.stats()}
        return ToolResult(ok=True, message=f"{len(facts)} memory item(s)", data=data,
                          evidence={"facts": len(facts)})


def build(ctx: ToolContext) -> List[Tool]:
    return [RememberTool(ctx), RecallTool(ctx), ForgetTool(ctx), AskUserTool(ctx),
            ReportProgressTool(ctx), SaveShortcutTool(ctx), ListMemoryTool(ctx)]
