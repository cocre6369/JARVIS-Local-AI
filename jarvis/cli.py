"""Command-line front end.

The graphical interface is the product; this exists for three real reasons:

* **diagnosis** - ``jarvis doctor`` tells a user (or a support thread) exactly
  what is installed, what is reachable and what is simulated, without a display;
* **headless use** - ``jarvis run "…"`` executes one task and prints every step,
  every verification and every refusal;
* **packaging sanity** - the frozen executable can be smoke-tested from a shell.

Approvals are honoured here too: when a task needs one, the CLI prints the same
explanation the dialog shows and waits for y / s / n on the terminal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__, paths
from .events import EventType

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_FAILED = 4


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    else:
        print(payload)


def _runtime(args: argparse.Namespace, with_speech: bool = True) -> Any:
    from .app import build_application

    return build_application(demo=bool(getattr(args, "demo", False)) or None,
                             with_speech=with_speech,
                             realtime=True,
                             data_dir=getattr(args, "data_dir", None))


def _install_console_approvals(runtime: Any) -> Dict[str, Any]:
    """Answer approval requests from the terminal instead of a dialog."""
    state: Dict[str, Any] = {"asked": 0, "approved": 0, "denied": 0}
    from .core.broker import Outcome

    def _on_request(event: Any) -> None:
        data = dict(event.data or {})
        request_id = data.get("id")
        state["asked"] += 1
        print("\n" + "=" * 68)
        print("APPROVAL REQUIRED")
        for label, key in (("Action", "description"), ("Tool", "tool"),
                           ("Risk", "risk"), ("Rule", "rule"),
                           ("Why", "reason"), ("Consequences", "consequences"),
                           ("Undo", "reversibility"), ("Scope", "scope_note")):
            value = data.get(key)
            if value:
                print(f"  {label:<13}: {value}")
        if data.get("kind") == "question":
            print(f"  Question     : {data.get('question', '')}")
            options = data.get("options") or []
            if options:
                print("  Options      : " + " | ".join(str(o) for o in options))
        print("=" * 68)
        if data.get("kind") == "question":
            try:
                answer = input("  Your answer (Enter to skip): ").strip()
            except EOFError:
                answer = ""
            runtime.broker.resolve(int(request_id), Outcome.APPROVED, answer=answer)
            return
        try:
            choice = input("  [y]es once / [s]ession / [n]o (default n): ").strip().lower()
        except EOFError:
            choice = ""
        outcome = {"y": Outcome.APPROVED, "yes": Outcome.APPROVED,
                   "s": Outcome.APPROVED_FOR_SESSION,
                   "session": Outcome.APPROVED_FOR_SESSION}.get(choice, Outcome.DENIED)
        if outcome is Outcome.DENIED:
            state["denied"] += 1
        else:
            state["approved"] += 1
        runtime.broker.resolve(int(request_id), outcome,
                               note="console" if outcome is not Outcome.DENIED else
                               "declined at the terminal")

    unsubscribe = runtime.bus.subscribe(EventType.APPROVAL_REQUESTED, _on_request)
    state["unsubscribe"] = unsubscribe
    return state


def _print_task(reply: Any, as_json: bool) -> int:
    if as_json:
        print(json.dumps(reply.as_dict(), indent=2, default=str, ensure_ascii=False))
        return EXIT_OK if reply.ok else EXIT_FAILED
    print("-" * 68)
    intent = reply.intent if isinstance(reply.intent, dict) else {}
    print(f"Intent : {intent.get('kind', '?')}"
          + (f"  (confidence {float(intent.get('confidence', 0)):.2f})"
             if intent.get("confidence") else ""))
    plan = reply.plan if isinstance(reply.plan, dict) else {}
    plan_steps = plan.get("steps") or []
    if plan_steps:
        titles = [(step.get("title") or step.get("tool") or "?")
                  if isinstance(step, dict) else str(step) for step in plan_steps]
        print("Plan   : " + " → ".join(str(t) for t in titles))
    if reply.used_shortcut:
        print("Route  : remembered shortcut (no model call)")
    print(f"Model  : {reply.model or '-'}    tokens: {reply.tokens or 0}"
          f"    took: {reply.duration_ms / 1000:.1f}s")
    for step in reply.steps:
        mark = "✓" if step.ok else ("⛔" if getattr(step, "blocked", False) else "✗")
        verified = ""
        if getattr(step, "verified", None) is True:
            verified = "  [verified]"
        elif getattr(step, "verified", None) is False:
            verified = "  [verification FAILED]"
        print(f"  {mark} {step.index}. {step.tool} "
              f"({getattr(step, 'risk', '?')}, {step.duration_ms:.0f}ms){verified}")
        if step.message:
            print(f"      {step.message[:200]}")
    if reply.approvals:
        print(f"Approved by you   : {', '.join(reply.approvals)}")
    if reply.blocked:
        print(f"Refused by policy : {', '.join(reply.blocked)}")
    if reply.denied:
        print(f"Declined by you   : {', '.join(reply.denied)}")
    print("-" * 68)
    print(reply.text or "(no answer)")
    if reply.error:
        print(f"\nerror: {reply.error}", file=sys.stderr)
    if reply.cancelled:
        print("(cancelled)")
    return EXIT_OK if reply.ok else EXIT_FAILED


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------
def cmd_gui(args: argparse.Namespace) -> int:
    from .app import run_gui

    return run_gui(demo=bool(args.demo), show_wizard=bool(args.wizard),
                   data_dir=args.data_dir)


def cmd_doctor(args: argparse.Namespace) -> int:
    from .app import print_report

    runtime = _runtime(args, with_speech=not args.no_speech)
    approvals = _install_console_approvals(runtime)
    runtime.start(background=False)
    try:
        try:
            runtime.guard.sample()          # a real reading, not a stale zero
        except Exception:  # noqa: BLE001
            pass
        status = runtime.status()
        if args.json:
            print(json.dumps(status.as_dict(), indent=2, default=str))
        else:
            print(print_report(runtime))
        if not status.ollama_reachable:
            print("\nOllama is not reachable. Install it from "
                  "https://ollama.com/download, pull a model, then re-run "
                  "'jarvis doctor'. Everything else above is unaffected.",
                  file=sys.stderr)
            return EXIT_UNAVAILABLE
        if not status.main_model:
            print("\nOllama is reachable but no main model is set. Pull one "
                  "(for example 'ollama pull qwen2.5:7b-instruct'), or open the "
                  "Models page and click 'Pick the best available'.",
                  file=sys.stderr)
        return EXIT_OK
    finally:
        approvals.get("unsubscribe", lambda: None)()
        runtime.shutdown()


def cmd_run(args: argparse.Namespace) -> int:
    text = " ".join(args.text).strip()
    if not text:
        print("Nothing to do: pass a command, e.g. jarvis run \"open notepad\"",
              file=sys.stderr)
        return EXIT_USAGE
    runtime = _runtime(args, with_speech=not args.no_speech)
    approvals = _install_console_approvals(runtime)
    runtime.start(background=False)
    try:
        reply = runtime.agent.handle(text, speak=not args.no_speech)
        return _print_task(reply, args.json)
    finally:
        approvals.get("unsubscribe", lambda: None)()
        runtime.shutdown()


def cmd_chat(args: argparse.Namespace) -> int:
    runtime = _runtime(args, with_speech=not args.no_speech)
    approvals = _install_console_approvals(runtime)
    runtime.start(background=False)
    print(f"JARVIS {__version__} - type a command, 'exit' to quit, "
          "'status' for a health check.")
    try:
        while True:
            try:
                text = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not text:
                continue
            if text.lower() in ("exit", "quit"):
                break
            if text.lower() == "status":
                from .app import print_report

                print(print_report(runtime))
                continue
            reply = runtime.agent.handle(text, speak=not args.no_speech)
            _print_task(reply, args.json)
    finally:
        approvals.get("unsubscribe", lambda: None)()
        runtime.shutdown()
    return EXIT_OK


def cmd_models(args: argparse.Namespace) -> int:
    runtime = _runtime(args, with_speech=False)
    info = runtime.ollama_install_info or {}
    status = runtime.ollama.status()
    if not status.get("connected"):
        print(f"Ollama is not reachable at "
              f"{runtime.settings.ollama.host}:{runtime.settings.ollama.port}.")
        print(f"Installed: {'yes' if info.get('installed') else 'no'}. "
              f"{info.get('hint', '')}")
        print(f"Download: {info.get('install_url', 'https://ollama.com/download')}")
        return EXIT_UNAVAILABLE
    models = runtime.ollama.list_models(force=True)
    router = runtime.router.describe()
    roles = router.get("roles", {})
    if args.json:
        print(json.dumps({"models": [m.as_dict() for m in models], "roles": roles},
                         indent=2, default=str))
        return EXIT_OK
    print(f"{len(models)} model(s) installed:")
    for model in models:
        data = model.as_dict()
        caps = []
        if data.get("supports_tools"):
            caps.append("tools")
        if data.get("supports_vision"):
            caps.append("vision")
        print(f"  {data['name']:<38} {data.get('size_gb', 0):>5.1f} GB  "
              f"ctx {data.get('context_length') or '?'}  "
              f"{'/'.join(caps) or '-'}")
    print("\nRoles:")
    for role in ("main", "vision", "fast"):
        entry = roles.get(role) or {}
        print(f"  {role:<7}: {entry.get('model') or '(unset)'}"
              f"{'  - ' + str(entry.get('reason')) if entry.get('reason') else ''}")
    return EXIT_OK


def cmd_observe(args: argparse.Namespace) -> int:
    runtime = _runtime(args, with_speech=False)
    runtime.start(background=False)
    try:
        started = time.time()
        observation = runtime.vision.observe(reason="cli")
        took = (time.time() - started) * 1000
        if args.json:
            print(json.dumps(observation.to_dict(), indent=2, default=str)[:8000])
            return EXIT_OK
        window = observation.active_window or {}
        elements = observation.element_dicts(limit=14)
        print(f"Took {took:.0f} ms (vision service reported "
              f"{float(observation.duration_ms or 0):.0f} ms)")
        print(f"Focused window : {window.get('title') or '(none)'} "
              f"[{window.get('process_name') or '?'}]")
        print(f"Windows open   : {len(observation.windows)}")
        print(f"Image          : {observation.width}x{observation.height}"
              f"{'  (cached)' if observation.cached else ''}"
              f"{'  [simulated]' if observation.simulated else ''}")
        print(f"Controls found : {len(observation.elements)}")
        for element in elements:
            name = element.get("name") or element.get("label") or ""
            print(f"    - {str(element.get('control_type', '?')):<14} {str(name)[:50]}")
        print(f"OCR            : {len(observation.ocr.words)} words, "
              f"{len(observation.ocr.lines)} lines "
              f"(engine: {observation.ocr.engine or 'none'})")
        for line in observation.ocr.lines[:6]:
            print(f"    | {line[:78]}")
        if observation.signals:
            print(f"Signals        : "
                  f"{json.dumps(observation.signals, default=str)[:300]}")
        for note in observation.notes[:6]:
            print(f"Note           : {note}")
        return EXIT_OK
    finally:
        runtime.shutdown()


def cmd_listen(args: argparse.Namespace) -> int:
    runtime = _runtime(args, with_speech=True)
    speech = getattr(runtime, "speech", None)
    if speech is None:
        print("The speech subsystem is not available on this machine.", file=sys.stderr)
        return EXIT_UNAVAILABLE
    print(f"Listening for {args.timeout:.0f}s…")
    transcript = speech.listen_once(timeout_s=float(args.timeout), source="cli")
    if transcript.ok:
        _emit({"ok": True, "text": transcript.text, "engine": transcript.engine,
               "took_ms": transcript.took_ms}, args.json)
        return EXIT_OK
    _emit({"ok": False, "error": getattr(transcript, "error", "no speech detected"),
           "reason": getattr(transcript, "reason", "")}, args.json)
    return EXIT_FAILED


def cmd_speak(args: argparse.Namespace) -> int:
    text = " ".join(args.text).strip()
    if not text:
        return EXIT_USAGE
    runtime = _runtime(args, with_speech=True)
    speech = getattr(runtime, "speech", None)
    if speech is None:
        print("The speech subsystem is not available on this machine.", file=sys.stderr)
        return EXIT_UNAVAILABLE
    spoken = bool(speech.speak(text))
    if not spoken:
        status = speech.status() if hasattr(speech, "status") else {}
        detail = (status.get("tts") or {}).get("detail", "")
        print("No voice is available on this machine"
              + (f": {detail}" if detail else "."), file=sys.stderr)
        runtime.shutdown()
        return EXIT_UNAVAILABLE
    # speak() queues; wait for the utterance to finish before shutting down.
    deadline = time.time() + 60.0
    while time.time() < deadline:
        if not getattr(speech, "is_speaking", lambda: False)():
            break
        time.sleep(0.2)
    runtime.shutdown()
    return EXIT_OK


def cmd_audit(args: argparse.Namespace) -> int:
    runtime = _runtime(args, with_speech=False)
    rows = runtime.audit.recent(limit=int(args.limit))
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return EXIT_OK
    stats = runtime.audit.stats()
    print(f"Audit log: {paths.data_dir() / 'security'}   totals: "
          f"{json.dumps(stats, default=str)}")
    if not rows:
        print("  (no recorded decisions yet)")
    for row in rows:
        print(f"  {row.get('ts', '')[:19]:<20} {str(row.get('decision', '')):<9} "
              f"{str(row.get('tool', '')):<22} {str(row.get('risk', '')):<9} "
              f"{str(row.get('reason', '') or row.get('rule', ''))[:60]}")
    return EXIT_OK


def cmd_memory(args: argparse.Namespace) -> int:
    runtime = _runtime(args, with_speech=False)
    memory = runtime.memory
    if args.stats:
        _emit(memory.stats(), args.json)
        return EXIT_OK
    if args.facts:
        facts = memory.list_facts(limit=int(args.limit), include_disabled=True)
        if args.json:
            print(json.dumps([f.as_dict() for f in facts], indent=2, default=str))
            return EXIT_OK
        for fact in facts:
            flags = []
            if fact.pinned:
                flags.append("pinned")
            if fact.disabled:
                flags.append("disabled")
            print(f"  [{fact.category}] {fact.key} = {fact.value}"
                  + (f"   ({', '.join(flags)})" if flags else ""))
        return EXIT_OK
    if args.sessions:
        sessions = memory.sessions(limit=int(args.limit))
        if args.json:
            print(json.dumps(sessions, indent=2, default=str))
            return EXIT_OK
        for session in sessions:
            print(f"  {session.get('session_id', '?')[:12]}  "
                  f"{session.get('messages', 0):>4} messages  "
                  f"{str(session.get('first_user_message', ''))[:50]}")
        return EXIT_OK
    _emit(memory.stats(), args.json)
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    from . import config as config_mod

    settings = config_mod.load()
    if args.set:
        changes: Dict[str, Dict[str, Any]] = {}
        for item in args.set:
            if "=" not in item:
                print(f"Expected section.key=value, got '{item}'", file=sys.stderr)
                return EXIT_USAGE
            dotted, raw = item.split("=", 1)
            if dotted.count(".") != 1:
                print(f"Expected section.key=value, got '{item}'", file=sys.stderr)
                return EXIT_USAGE
            section, key = dotted.split(".")
            value: Any = raw
            if raw.lower() in ("true", "false"):
                value = raw.lower() == "true"
            else:
                try:
                    value = int(raw)
                except ValueError:
                    try:
                        value = float(raw)
                    except ValueError:
                        value = raw
            changes.setdefault(section, {})[key] = value

        def _apply(target: Any) -> None:
            for section, values in changes.items():
                holder = getattr(target, section, None)
                if holder is None:
                    print(f"No settings section called '{section}'", file=sys.stderr)
                    continue
                for key, value in values.items():
                    if not hasattr(holder, key):
                        print(f"'{section}' has no setting called '{key}'",
                              file=sys.stderr)
                        continue
                    setattr(holder, key, value)

        settings = config_mod.update(_apply)
        print(f"Saved {paths.config_file()}")
    if args.get:
        section, _, key = args.get.partition(".")
        holder = getattr(settings, section, None)
        if holder is None:
            print(f"No settings section called '{section}'", file=sys.stderr)
            return EXIT_USAGE
        value = holder.to_dict().get(key) if key else holder.to_dict()
        print(json.dumps(value, indent=2, default=str))
        return EXIT_OK
    if args.reset:
        answer = input("Delete all JARVIS settings and start from defaults? [y/N] ")
        if answer.strip().lower() in ("y", "yes"):
            path = paths.config_file()
            if os.path.exists(str(path)):
                os.remove(str(path))
            print(f"Removed {path}")
        return EXIT_OK
    if args.json:
        print(json.dumps(settings.to_dict(), indent=2, default=str))
    else:
        print(f"Settings file: {paths.config_file()}")
        for section in ("general", "ollama", "speech", "vision", "automation",
                        "security", "memory", "ui"):
            holder = getattr(settings, section, None)
            if holder is None:
                continue
            print(f"\n[{section}]")
            for key, value in holder.to_dict().items():
                if key in ("tool_overrides", "writable_roots") and not value:
                    value = "{}"
                print(f"  {key} = {value}")
    return EXIT_OK


def cmd_diagnostics(args: argparse.Namespace) -> int:
    path = dump_diagnostics_path()
    print(path)
    return EXIT_OK


def dump_diagnostics_path() -> str:
    from .logging_setup import dump_diagnostics

    return str(dump_diagnostics())


def cmd_version(args: argparse.Namespace) -> int:
    print(f"JARVIS {__version__}")
    print(f"Python {sys.version.split()[0]} on {sys.platform}")
    print(f"Data dir: {paths.data_dir()}")
    return EXIT_OK


# ----------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------
def _common_flags() -> argparse.ArgumentParser:
    """Global switches, accepted before *and* after the subcommand.

    ``SUPPRESS`` defaults matter: a subparser must not overwrite a value the
    user gave before the subcommand with its own default.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=argparse.SUPPRESS,
                        help="Use a different folder for settings, memory and logs")
    common.add_argument("--demo", action="store_true", default=argparse.SUPPRESS,
                        help="Demo Mode: simulate mouse, keyboard, windows and apps")
    common.add_argument("--log-level", default=argparse.SUPPRESS,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    common.add_argument("--no-speech", action="store_true",
                        default=argparse.SUPPRESS,
                        help="Do not load speech recognition or synthesis")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="Machine-readable output")
    return common


#: Every command gets these, so ``jarvis doctor --json`` and
#: ``jarvis --json doctor`` both work.
GLOBAL_DEFAULTS = {"data_dir": None, "demo": False, "log_level": None,
                   "no_speech": False, "json": False}


def build_parser() -> argparse.ArgumentParser:
    common = _common_flags()
    parser = argparse.ArgumentParser(
        prog="jarvis",
        parents=[common],
        description="JARVIS - a private, local AI assistant that can see and "
                    "operate your computer.",
        epilog="With no command, JARVIS opens its window. Try 'jarvis doctor' if "
               "something is missing.")
    parser.add_argument("--version", action="version", version=f"JARVIS {__version__}")
    parser.add_argument("--wizard", action="store_true", default=argparse.SUPPRESS,
                        help="Run the first-time setup wizard on start")
    sub = parser.add_subparsers(dest="command")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], help=help_text)

    gui = add("gui", "Open the JARVIS window (default)")
    gui.add_argument("--wizard", action="store_true", default=argparse.SUPPRESS,
                     help="Run the setup wizard first")
    gui.set_defaults(func=cmd_gui)

    doctor = add("doctor", "Report what is installed and working")
    doctor.set_defaults(func=cmd_doctor)

    run = add("run", "Execute one task and print every step")
    run.add_argument("text", nargs="+")
    run.set_defaults(func=cmd_run)

    chat = add("chat", "Interactive text session (no window)")
    chat.set_defaults(func=cmd_chat)

    models = add("models", "List installed models and role choices")
    models.set_defaults(func=cmd_models)

    observe = add("observe", "Take one look at the screen and report")
    observe.set_defaults(func=cmd_observe)

    listen = add("listen", "Capture one spoken command")
    listen.add_argument("--timeout", type=float, default=8.0)
    listen.set_defaults(func=cmd_listen)

    speak = add("speak", "Say something with the configured voice")
    speak.add_argument("text", nargs="+")
    speak.set_defaults(func=cmd_speak)

    audit = add("audit", "Show recent security decisions")
    audit.add_argument("--limit", type=int, default=25)
    audit.set_defaults(func=cmd_audit)

    memory = add("memory", "Inspect what JARVIS remembers")
    memory.add_argument("--stats", action="store_true")
    memory.add_argument("--facts", action="store_true")
    memory.add_argument("--sessions", action="store_true")
    memory.add_argument("--limit", type=int, default=40)
    memory.set_defaults(func=cmd_memory)

    config = add("config", "Show or change settings")
    config.add_argument("--get", default=None, metavar="SECTION.KEY")
    config.add_argument("--set", action="append", default=[],
                        metavar="SECTION.KEY=VALUE")
    config.add_argument("--reset", action="store_true",
                        help="Delete the settings file and start from defaults")
    config.set_defaults(func=cmd_config)

    diagnostics = add("diagnostics", "Write a diagnostics file")
    diagnostics.set_defaults(func=cmd_diagnostics)

    version = add("version", "Print versions and paths")
    version.set_defaults(func=cmd_version)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    for key, default in GLOBAL_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, default)
    if not hasattr(args, "wizard"):
        args.wizard = False
    if args.log_level:
        os.environ["JARVIS_LOG_LEVEL"] = str(args.log_level)
    if args.data_dir:
        os.environ["JARVIS_DATA_DIR"] = str(args.data_dir)
    if not getattr(args, "command", None):
        # No subcommand: open the window, unless there is no display at all.
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") \
                and not os.environ.get("WAYLAND_DISPLAY"):
            parser.print_help()
            print("\nNo display server was found, so the window cannot open. "
                  "Try 'jarvis doctor' or 'jarvis run \"…\"'.", file=sys.stderr)
            return EXIT_UNAVAILABLE
        from .app import run_gui

        return run_gui(demo=bool(args.demo), show_wizard=bool(args.wizard),
                       data_dir=args.data_dir)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"jarvis: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
