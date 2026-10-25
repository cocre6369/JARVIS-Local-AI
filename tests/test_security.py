"""The security layer, tested as code - not as prompts.

Every rule here is enforced by :mod:`jarvis.security.policy` and the
authorisation path in the agent.  A model that "agrees" to something prohibited
changes nothing.
"""

from __future__ import annotations

import pytest

from jarvis.security.policy import PolicyEngine, RequestContext, Risk
from jarvis import config as config_mod


def _ctx(tool: str, args: dict, baseline: Risk = Risk.ALLOWED,
         category: str = "test") -> RequestContext:
    return RequestContext(tool=tool, args=args, category=category, baseline=baseline,
                          task_id="t", description="test", reversible=True,
                          target_label=str(args)[:80]).analyse()


def make_policy(settings=None) -> PolicyEngine:
    settings = settings or config_mod.load()
    return PolicyEngine(settings_provider=lambda: settings)


def test_registry_rules_refuse_prohibited_operations():
    policy = make_policy()
    prohibited = [
        ("run_command", {"command": "reg delete HKLM\\Software /f"}),
        ("run_command", {"command": "net user hacker /add"}),
        ("run_command", {"command": "sc stop windefend"}),
        ("run_command", {"command": "powershell -c Start-Process msiexec -Verb runAs"}),
        ("file_write", {"path": "C:\\Windows\\System32\\drivers\\etc\\hosts"}),
    ]
    categories = {"run_command": "shell", "file_write": "file_write"}
    for tool, args in prohibited:
        decision = policy.evaluate(_ctx(tool, args, Risk.APPROVAL,
                                        categories.get(tool, "test")))
        assert decision.risk is Risk.PROHIBITED, f"{tool} {args} -> {decision.risk}"


def test_credential_paths_are_never_readable():
    policy = make_policy()
    for path in ("C:\\Users\\me\\AppData\\Local\\Google\\Chrome\\User Data\\Default\\Cookies",
                 "~/.ssh/id_rsa", "C:\\Users\\me\\AppData\\Roaming\\Microsoft\\Credentials\\x"):
        decision = policy.evaluate(_ctx("file_read", {"path": path}, Risk.ALLOWED,
                                        "file_read"))
        assert decision.risk is Risk.PROHIBITED, path


def test_deletes_and_downloads_require_approval():
    policy = make_policy()
    cases = (("delete_file", {"path": "~/Documents/a.docx"}, Risk.APPROVAL,
              "file_delete"),
             ("open_url", {"url": "https://example.com/setup.exe"}, Risk.ALLOWED,
              "download"),
             ("send_message", {"text": "hello", "app": "slack"}, Risk.ALLOWED,
              "communication"))
    for tool, args, baseline, category in cases:
        decision = policy.evaluate(_ctx(tool, args, baseline, category))
        assert decision.risk in (Risk.APPROVAL, Risk.PROHIBITED), (tool, decision.risk)


def test_overrides_can_only_tighten():
    settings = config_mod.load()
    settings.security.tool_overrides = {"read_screen_text": "block",
                                        "delete_file": "allow"}
    policy = make_policy(settings)
    assert policy.evaluate(_ctx("read_screen_text", {}, Risk.ALLOWED)).risk \
        is Risk.PROHIBITED
    # "allow" on an approval-gated tool must NOT downgrade it to allowed.
    decision = policy.evaluate(_ctx("delete_file", {"path": "~/x.txt"},
                                    Risk.APPROVAL, "file_delete"))
    assert decision.risk is not Risk.ALLOWED


def test_strict_mode_demands_approval_for_writes():
    settings = config_mod.load()
    settings.security.mode = "strict"
    policy = make_policy(settings)
    decision = policy.evaluate(_ctx("write_file", {"path": "~/notes.txt",
                                                    "content": "hi"}, Risk.ALLOWED,
                                    "file_write"))
    assert decision.risk in (Risk.APPROVAL, Risk.PROHIBITED)


def test_session_grants_are_scoped_to_tool_and_target():
    settings = config_mod.load()
    policy = make_policy(settings)
    grants = policy.grants
    key = type(grants).key("write_file", "~/notes.txt")
    grants.grant(key)
    assert grants.is_granted(key)
    assert not grants.is_granted(type(grants).key("write_file", "~/other.txt"))
    assert not grants.is_granted(type(grants).key("delete_file", "~/notes.txt"))
    grants.clear()
    assert not grants.is_granted(key)


def test_resource_ceilings_are_hard_capped():
    settings = config_mod.load()
    settings.security.max_ram_gb = 999.0
    settings.security.max_vram_gb = 64.0
    settings.sanitize()
    assert settings.security.max_ram_gb == 12.0
    assert settings.security.max_vram_gb == 8.0


def test_model_supplied_consent_is_stripped():
    """An argument claiming approval is not approval."""
    policy = make_policy()
    args = {"path": "~/x.txt", "user_approved": True, "consent": "yes",
            "approval": "granted"}
    context = _ctx("delete_file", args, Risk.APPROVAL)
    assert context.self_approval_attempt is True
    decision = policy.evaluate(context)
    assert decision.risk is not Risk.ALLOWED
    assert decision.bypass_attempt is True
