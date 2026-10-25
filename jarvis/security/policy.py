"""The permission enforcement layer.

Design rules
------------
1. **The model never decides.**  A tool call produced by Ollama is only a
   *proposal*.  :class:`PolicyEngine.evaluate` is the only thing that can turn
   a proposal into an execution, and it is called by the agent loop, not by the
   tools themselves.
2. **Prohibited is prohibited.**  No setting, no user override, no prompt and
   no tool argument can turn a ``PROHIBITED`` decision into anything else.
   Overrides may only *tighten* an action (``allow -> ask -> block``).
3. **Self-approval is impossible.**  Arguments such as ``approved=true`` or
   ``user_confirmed=true`` are stripped and recorded as an attempted bypass.
   Approval can only arrive through the :class:`ApprovalBroker`, which is wired
   to the user interface and is *not* registered as a tool.
4. **Every decision is audited** (locally, redacted) - see :mod:`.audit`.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import redaction

log = logging.getLogger(__name__)


class Risk(str, Enum):
    ALLOWED = "allowed"
    APPROVAL = "approval_required"
    PROHIBITED = "prohibited"

    @property
    def rank(self) -> int:
        return {"allowed": 0, "approval_required": 1, "prohibited": 2}[self.value]


#: Argument names a model might try to use to fake user consent.
SELF_APPROVAL_KEYS = {
    "approved", "approve", "user_approved", "user_confirmed", "confirmed",
    "confirmation", "consent", "permission_granted", "skip_confirmation",
    "skip_approval", "force", "no_confirm", "silent", "admin", "elevate",
    "run_as_admin", "as_admin", "sudo",
}

# ---------------------------------------------------------------------------
# Protected locations & credential stores
# ---------------------------------------------------------------------------
CRITICAL_PATH_PREFIXES: Tuple[str, ...] = (
    "c:\\windows", "c:\\winnt", "c:\\boot", "c:\\recovery",
    "c:\\system volume information", "c:\\programdata\\microsoft\\windows defender",
    "c:\\programdata\\microsoft\\windows defender offline",
    "c:\\program files\\windowsapps", "c:\\program files\\windows defender",
    "c:\\program files (x86)\\windows defender",
    "c:\\perflogs", "\\\\?\\globalroot", "\\\\?\\global??",
    "/etc", "/boot", "/bin", "/sbin", "/lib", "/lib64", "/usr", "/sys",
    "/proc", "/dev", "/var/lib/dpkg", "/var/lib/rpm", "/System", "/Library",
)

#: Directories that require elevation to write to; writing there implies an
#: attempt to modify machine-wide state.
ELEVATION_REQUIRED_PREFIXES: Tuple[str, ...] = (
    "c:\\windows", "c:\\program files", "c:\\program files (x86)",
    "c:\\programdata\\microsoft", "/etc", "/usr", "/bin", "/sbin", "/lib",
)

CREDENTIAL_STORE_MARKERS: Tuple[str, ...] = (
    "login data", "web data", "cookies.sqlite", "cookies.db", "logins.json",
    "key3.db", "key4.db", "cert9.db", "cert8.db", "signons.sqlite",
    "credentials", "credential store", ".kdbx", ".kdb", "1password",
    "bitwarden", "lastpass", "dashlane", "enpass", "keepass", "nordpass",
    "rob form", "sticky password", "vault.db", "idbstorage", "wallet.dat",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".ppk", ".netrc",
    ".git-credentials", ".aws/credentials", ".aws\\credentials", ".kube/config",
    "master key", "local state", "dpapi", "protected storage",
    "keychain", "login.keychain", "token_store", "tokens.json",
    ".pgpass", ".my.cnf", "shadow", "sam.hiv", "security.hiv",
    # Browser cookie jars are named plainly ("…\Default\Cookies",
    # "…\Network\Cookies"); the folder context is what identifies them, and a
    # bare trailing "cookies" only ever appears in a browser profile.
    "\\cookies", "/cookies", "cookies-journal", "sessionstorage",
)

FINANCIAL_URL_MARKERS: Tuple[str, ...] = (
    "bank", "banking", "internetbanking", "onlinebanking", "westpac",
    "commbank", "commonwealthbank", "nab.com.au", "anz.com", "macquarie",
    "ing.com.au", "bendigobank", "citi", "hsbc", "barclays", "lloyds",
    "paypal", "stripe", "wise.com", "revolut", "coinbase", "binance",
    "kraken", "crypto.com", "commsec", "sharesight", "stake", "superannuation",
    "australian-super", "ausuper", "mygov", "ato.gov", "centrelink",
    "servicesaustralia", "medicare", "tax", "invoice", "payment", "checkout",
)

#: Read-only tools that remain usable in "strict" mode.
READ_ONLY_TOOLS: Set[str] = {
    "observe_screen", "capture_screenshot", "read_screen_text", "list_windows",
    "list_running_processes", "get_active_window", "find_ui_elements",
    "search_files", "read_document", "list_directory", "get_system_status",
    "get_current_track", "list_installed_models", "get_memory",
    "search_memory", "get_clipboard_text", "web_search_summary",
}

# ---------------------------------------------------------------------------
# Command pattern rules
# ---------------------------------------------------------------------------
Rule = Tuple[str, str, str, "re.Pattern[str]"]


def _r(rule_id: str, category: str, reason: str, pattern: str) -> Rule:
    return (rule_id, category, reason, re.compile(pattern, re.IGNORECASE))


#: Hard prohibitions.  Matching any of these aborts the action immediately.
PROHIBITED_COMMAND_RULES: List[Rule] = [
    # -- privilege escalation -------------------------------------------
    _r("ELEV-01", "privilege_escalation",
       "Running a program with administrator rights is not permitted.",
       r"\brunas\b|runas\s*/(user|trustlevel|savecreds)|-verb\s+runas|"
       r"start-process\s+.*-verb\s+runas|\bgsudo\b|\bsudo\b|"
       r"run-as-admin|runasadmin|elevate(\.exe)?\b|shellexecute.*runas"),
    _r("ELEV-02", "privilege_escalation",
       "Creating a task or service that runs as SYSTEM is not permitted.",
       r"schtasks\b.*(/ru\s*(system|\"?nt authority)|/it\s+/rl\s*highest)|"
       r"sc(\.exe)?\s+(create|config|start|delete|failure)\b|"
       r"psexec(\.exe)?\b|pslist\b.*-s\b|new-service\b|"
       r"register-scheduledtask\b.*(runlevel\s*highest|-user\s*system)"),
    _r("ELEV-03", "privilege_escalation",
       "Bypassing User Account Control is not permitted.",
       r"\benablelua\b|consentpromptbehavioradmin|promptonsecuredesktop|"
       r"validateadminapprovals|uacme|fodhelper|sdclt\s*/kickoffelevated|"
       r"computerdefaults|silentcleanup.*bypass|uac\s*bypass"),
    # -- uninstalling software ------------------------------------------
    _r("UNINST-01", "uninstall",
       "JARVIS is not allowed to uninstall software.",
       r"\b(winget|appx|choco|scoop|npm|pip|pip3)\s+uninstall\b|"
       r"msiexec(\.exe)?\s+.*(/x|/uninstall)|uninstallstring|"
       r"quietuninstallstring|remove-appxpackage|remove-appxprovisionedpackage|"
       r"dism(\.exe)?\b.*/remove-package|remove-windowspackage|"
       r"appwiz\.cpl|get-package\b.*uninstall|program\s*&?\s*features|"
       r"\buninstall(ation)?\b.*\b(program|software|app|application)\b"),
    # -- registry --------------------------------------------------------
    _r("REG-01", "registry",
       "JARVIS is not allowed to modify the Windows registry.",
       r"\breg(\.exe)?\s+(add|delete|import|load|unload|restore|copy|save)\b|"
       r"regedit(\.exe)?\s*(/s|/e|\"|\s)|\breg\b.*\bhklm\b|"
       r"new-itemproperty|set-itemproperty\s+.*\bhk(lm|cr|u|cu|pd|dd)\b|"
       r"remove-itemproperty|remove-item\s+.*\bhk(lm|cr|u|cu)\b|"
       r"new-item\s+.*\bhk(lm|cr|u|cu)\b|reg\s+add\s+hk"),
    # -- security software / firewall / logging --------------------------
    _r("SEC-01", "security_software",
       "JARVIS is not allowed to change antivirus or Windows Defender settings.",
       r"set-mppreference|add-mppreference|remove-mppreference|"
       r"get-mppreference\s*\|\s*set|disable(-|_)?(realtime|defender|antivirus)|"
       r"mpcmdrun(\.exe)?\b|-disablerealtimemonitoring|"
       r"set-mpcomputersetting|windows\s*defender.*(disable|turn off)|"
       r"tamper\s*protection"),
    _r("SEC-02", "security_software",
       "JARVIS is not allowed to stop security processes.",
       r"taskkill\b.*\b(msmpeng|windefend|wscsvc|securityhealthservice|"
       r"sense|sgrmbroker|mpdefendercore|nis|avp|avpui|mbam|mbamservice|"
       r"ekrn|egui|zhudongfangyu|bdagent|f-secure|savservice)\b|"
       r"stop-process\b.*\b(msmpeng|windefend|wscsvc|securityhealthservice|"
       r"sense|mbamservice|avp|ekrn)\b|"
       r"stop-service\b.*\b(windefend|wscsvc|sense|mpservice|mbamservice)\b|"
       r"\bsc(\.exe)?\s+(stop|pause)\b[^|;\n]{0,60}\b(windefend|wscsvc|sense|"
       r"mpservice|securityhealthservice|msmpeng|mpdefendercore)\b|"
       r"\bnet(\.exe)?\s+stop\b[^|;\n]{0,60}\b(windefend|wscsvc|sense|mpservice|"
       r"securityhealthservice)\b"),
    _r("SEC-03", "firewall",
       "JARVIS is not allowed to modify firewall or network security rules.",
       r"netsh\s+advfirewall|\bnetsh\s+firewall|new-netfirewallrule|"
       r"set-netfirewall(profile|rule)|remove-netfirewallrule|"
       r"enable-netfirewallrule|disable-netfirewallrule|\bufw\s+(disable|allow|deny)|"
       r"firewall-cpl|wf\.msc"),
    _r("SEC-04", "tampering",
       "JARVIS is not allowed to disable script safety, tamper with backups "
       "or clear the event log.",
       r"set-executionpolicy\s+(bypass|unrestricted)\b.*(-scope\s+"
       r"(machinepolicy|userpolicy|localmachine)|\s+-force)|"
       r"vssadmin\s+(delete|resize)\s+shadows|wbadmin\s+delete|"
       r"wevtutil\s+cl\b|clear-eventlog|clear-recyclebin\b.*-force|"
       r"cipher\s+/w|fsutil\s+usn\s+deletejournal|bcdedit\b|"
       r"bootcfg\b|winload|takeown\s+/f\s+[a-z]:\\(windows|program)"),
    # -- accounts & permissions ------------------------------------------
    _r("ACCT-01", "account_changes",
       "JARVIS is not allowed to change user accounts or permissions.",
       r"\bnet\s+user\b|\bnet\s+localgroup\b|\bnet\s+group\b|"
       r"new-localuser|set-localuser|remove-localuser|add-localgroupmember|"
       r"remove-localgroupmember|enable-localuser|disable-localuser|"
       r"\bicacls\b.*(/grant|/reset|/setowner|/inheritance:r)|"
       r"\bcacls\b|set-acl\b|get-acl\b.*\|\s*set-acl|"
       r"useraccountcontrolsettings|control\s+userpasswords2|"
       r"netplwiz|lusrmgr\.msc"),
    # -- credential theft --------------------------------------------------
    _r("CRED-01", "credential_access",
       "JARVIS is not allowed to read, export or decrypt stored credentials.",
       r"cmdkey\s+/list|vaultcli|dpapi|convertfrom-securestring|"
       r"convertto-securestring.*-asplaintext|"
       r"\[system\.security\.cryptography\.protecteddata\]::unprotect|"
       r"mimikatz|sekurlsa|procdump\b.*lsass|\blsass\b|"
       r"netsh\s+wlan\s+show\s+profile.*key=clear|"
       r"security\s+find-(generic|internet)-password|"
       r"certutil\s+-exportpfx|openssl\s+(pkcs12|x509).*-inkey|"
       r"get-storedcredential|read-browserpasswords|cookies?\s+extract"),
    # -- disk level destruction -------------------------------------------
    _r("DISK-01", "destructive",
       "JARVIS is not allowed to run disk-level destructive commands.",
       r"\bformat(\.com)?\s+[a-z]:|\bdiskpart\b|\bdd\s+if=.*of=/dev|"
       r"\bmkfs(\.\w+)?\b|\bfdisk\b|\bcleanmgr\b.*/verylowdisk|"
       r"rd\s+/s\s+/q\s+[a-z]:\\(\s|$|\"|windows|program)|"
       r"rmdir\s+/s\s+/q\s+[a-z]:\\(\s|$|\"|windows|program)|"
       r"del\s+/[sfq]{1,3}\s+[a-z]:\\(\s|$|\*)|"
       r"remove-item\s+(-path\s+)?[a-z]:\\\s*-recurse|"
       r"rm\s+-[rf]{1,2}\s+/(\s|$|\*)"),
    _r("DISK-02", "destructive",
       "JARVIS is not allowed to modify system-critical files.",
       r"\b(sysprep|slmgr|sfc|dism)\b.*(/oobe|/generalize|/scannow|"
       r"/online\s*/(add|remove|cleanup|enable|disable))|"
       r"\battrib\b.*[+-][shr]\s+[a-z]:\\(windows|boot)|"
       r"hosts\s*file|drivers\\etc\\hosts|"
       r"(del|erase|remove-item)\b.*(pagefile|hiberfil|swapfile|ntuser\.dat|"
       r"sam\b|system\.hive)"),
    # -- arbitrary code execution helpers ---------------------------------
    _r("EXEC-01", "code_execution",
       "JARVIS will not download and execute remote code.",
       r"(invoke-webrequest|invoke-restmethod|iwr|irm|curl|wget)\b.*\|\s*"
       r"(iex|invoke-expression|powershell|cmd|sh|bash)|"
       r"downloadstring\s*\(.*\)\s*\|\s*iex|bitsadmin\s+/transfer.*\.(exe|bat|ps1|scr|vbs|dll)|"
       r"certutil\s+-urlcache|mshta\s+(http|javascript|vbscript)|"
       r"rundll32\s+.*(javascript|http|\\\\)|regsvr32\s+.*(http|/i:http|scrobj)|"
       r"\bpowershell\b.*-(enc|encodedcommand|e)\s+[a-z0-9+/=]{20,}"),
    _r("EXEC-02", "code_execution",
       "JARVIS will not launch scripts from temporary or download folders "
       "without approval.",
       r"(start|invoke-item|ii)\b.*\\(temp|tmp|downloads|appdata\\local\\temp)\\"
       r"[^\s]+\.(exe|bat|cmd|ps1|vbs|js|scr|msi|com|pif|hta)\b"),
]

#: Command patterns that are legal but need explicit user approval.
APPROVAL_COMMAND_RULES: List[Rule] = [
    _r("APPR-DL-01", "download",
       "Downloading files or installers requires your approval.",
       r"\b(invoke-webrequest|iwr|invoke-restmethod|irm|curl|wget|bitsadmin|"
       r"start-bitstransfer|aria2c?|ncftp|ftp)\b.*"
       r"(-outfile|-outfilename|-o\s|-out\s|/transfer|>|-urlcache|-download)|"
       r"\bdownload(ing|s)?\b.*(file|installer|exe|zip|msi|package)|"
       r"save\s+(target|link|file)\s+as\b"),
    _r("APPR-SHUTDOWN-01", "power",
       "Shutting down or restarting the computer requires your approval.",
       r"\bshutdown\b\s+/[srfp]|shutdown\s+-[srh]\b|restart-computer|"
       r"stop-computer|\breboot\b|logoff(\.exe)?\b|\bsign\s?out\b"),
    _r("APPR-MSG-01", "communication",
       "Sending a message, email or post requires your approval.",
       r"\b(send-mailmessage|smtp|mail\b.*send|sendmessage|post\s+comment|"
       r"publish|tweet|share\s+file)\b"),
    _r("APPR-DEL-01", "destructive",
       "Deleting files requires your approval.",
       r"\b(del|erase|remove-item|rmdir|rd|unlink|trash|shred|rm)\b"),
    _r("APPR-INSTALL-01", "install",
       "Installing software requires your approval.",
       r"\b(winget|choco|scoop|npm|pip3?|dotnet\s+tool)\s+install\b|"
       r"msiexec(\.exe)?\s+(/i|\"[^\"]+\.msi\")|\bsetup\.exe\b|"
       r"\binstall(ing|er)?\b.*(software|program|package|driver)"),
]

# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------
_PATH_RE = re.compile(
    r"(?:\"[a-zA-Z]:[\\/][^\"]{1,240}\")|(?:[a-zA-Z]:[\\/][^\s\"'|<>]{0,240})|"
    r"(?:\\\\[^\s\"'|<>]{2,120})|"
    r"(?:/(?:home|Users|etc|var|tmp|opt|usr|mnt|media)[^\s\"'|<>]{0,240})"
)
#: Argument names whose *whole value* is a path.  Regexing free text cannot see
#: paths that contain spaces ("C:\Users\me\User Data\…Cookies"), and truncating
#: those would silently switch off the credential/system-path rules - so
#: structured arguments are taken verbatim, ahead of the text scan.
_PATH_KEYS = {
    "path", "file", "filepath", "file_path", "filename", "folder", "directory",
    "dir", "source", "src", "destination", "dest", "dst", "target", "target_path",
    "output", "output_path", "input", "input_path", "archive", "shortcut",
}
_URL_RE = re.compile(r"\b(?:https?|ftp|file)://[^\s\"'<>|)]{3,300}", re.IGNORECASE)
_COMMAND_KEYS = {
    "command", "cmd", "command_line", "commandline", "script", "code",
    "shell", "program", "executable", "path", "args", "arguments", "run",
    "powershell", "batch", "expression",
}


def _walk_strings(value: Any, out: List[str], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            _walk_strings(v, out, depth + 1)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            _walk_strings(v, out, depth + 1)


def _walk_path_args(value: Any, out: List[str], depth: int = 0) -> None:
    """Collect whole values whose *key* names a path (see :data:`_PATH_KEYS`)."""
    if depth > 8:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower().strip() in _PATH_KEYS and isinstance(v, str):
                out.append(v)
            _walk_path_args(v, out, depth + 1)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            _walk_path_args(v, out, depth + 1)


@dataclass
class RequestContext:
    """Everything the policy engine knows about one proposed action."""

    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    category: str = "general"
    baseline: Risk = Risk.ALLOWED
    task_id: str = ""
    #: Short human description produced by the tool (for the approval dialog).
    description: str = ""
    reversible: bool = True
    target_label: str = ""

    # ---- derived (filled by :meth:`analyse`) ----
    text: str = ""
    paths: List[str] = field(default_factory=list)
    urls: List[str] = field(default_factory=list)
    commands: List[str] = field(default_factory=list)
    self_approval_attempt: bool = False
    stripped_keys: List[str] = field(default_factory=list)

    def analyse(self) -> "RequestContext":
        strings: List[str] = []
        stripped: List[str] = []
        for k in list(self.args.keys()):
            if str(k).lower().strip() in SELF_APPROVAL_KEYS:
                stripped.append(str(k))
        if stripped:
            self.self_approval_attempt = True
            for k in stripped:
                self.args.pop(k, None)
        _walk_strings(self.tool, strings)
        _walk_strings(self.args, strings)
        _walk_strings(self.description, strings)
        self.stripped_keys = stripped
        self.text = "\n".join(strings)
        lowered = self.text.lower()
        explicit: List[str] = []
        _walk_path_args(self.args, explicit)
        self.paths = sorted({_norm_path(p) for p in
                             explicit + _PATH_RE.findall(self.text) if p.strip()})
        self.urls = sorted({u.rstrip(".,);]") for u in _URL_RE.findall(self.text)})
        cmds: List[str] = []
        for key, value in self.args.items():
            if str(key).lower() in _COMMAND_KEYS:
                sub: List[str] = []
                _walk_strings(value, sub)
                cmds.extend(sub)
        cmds.extend(m.group(0) for m in re.finditer(
            r"(?i)\b(powershell|pwsh|cmd|bash|sh|python|python3)\b(\.exe)?\s+.{0,300}",
            lowered))
        self.commands = [c for c in cmds if c and c.strip()]
        return self

    def matches_path_prefix(self, prefixes: Iterable[str]) -> List[str]:
        hits = []
        for p in self.paths:
            for pref in prefixes:
                if p.startswith(pref.lower()):
                    hits.append(p)
                    break
        return hits

    def credential_targets(self) -> List[str]:
        hits = []
        for p in self.paths:
            if any(m in p for m in CREDENTIAL_STORE_MARKERS):
                hits.append(p)
        for u in self.urls:
            if any(m in u.lower() for m in CREDENTIAL_STORE_MARKERS):
                hits.append(u)
        return hits

    def financial_targets(self) -> List[str]:
        return [u for u in self.urls
                if any(m in u.lower() for m in FINANCIAL_URL_MARKERS)]


def _norm_path(p: str) -> str:
    p = p.strip().strip('"').strip("'").rstrip("\\/.,;:")
    p = p.replace("/", "\\") if re.match(r"^[a-zA-Z]:\\", p) else p
    return p.lower()


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------
@dataclass
class PolicyDecision:
    risk: Risk
    rule: str
    reason: str
    category: str = ""
    reversible: bool = True
    consequences: str = ""
    target: str = ""
    needs_approval: bool = False
    bypass_attempt: bool = False
    tool: str = ""

    @property
    def allowed(self) -> bool:
        return self.risk is Risk.ALLOWED

    @property
    def blocked(self) -> bool:
        return self.risk is Risk.PROHIBITED

    def dialog_text(self) -> str:
        """The four points section 15.C requires in a confirmation request."""
        return (
            f"What: {self.reason or self.rule}\n"
            f"Target: {self.target or '(not specified)'}\n"
            f"Consequences: {self.consequences or 'See description above.'}\n"
            f"Reversible: {'Yes' if self.reversible else 'No - this cannot be undone.'}"
        )


# ---------------------------------------------------------------------------
# Session grants
# ---------------------------------------------------------------------------
class SessionGrants:
    """Remembers 'always allow for this session' answers.

    Grants are keyed on (tool, target-signature) so approving one action never
    blanket-approves everything else - see section 15.C:
    "Approval must apply only to the specific action described."
    """

    def __init__(self) -> None:
        self._grants: Dict[str, float] = {}
        self._denials: Dict[str, float] = {}
        self._lock = threading.RLock()

    @staticmethod
    def key(tool: str, target: str = "") -> str:
        return f"{tool}::{(target or '*').lower()[:200]}"

    def grant(self, key: str) -> None:
        with self._lock:
            self._grants[key] = time.time()
            self._denials.pop(key, None)

    def deny(self, key: str) -> None:
        with self._lock:
            self._denials[key] = time.time()
            self._grants.pop(key, None)

    def is_granted(self, key: str) -> bool:
        with self._lock:
            return key in self._grants

    def is_denied(self, key: str) -> bool:
        with self._lock:
            return key in self._denials

    def clear(self) -> None:
        with self._lock:
            self._grants.clear()
            self._denials.clear()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"granted": sorted(self._grants), "denied": sorted(self._denials)}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class PolicyEngine:
    """Stateless-ish classifier.  Constructed once, thread safe."""

    def __init__(self, settings_provider: Callable[[], Any],
                 grants: Optional[SessionGrants] = None) -> None:
        self._settings_provider = settings_provider
        self.grants = grants or SessionGrants()

    # -- helpers ---------------------------------------------------------
    @property
    def settings(self):
        return self._settings_provider()

    @property
    def security(self):
        return self.settings.security

    # -- main entry point ------------------------------------------------
    def evaluate(self, ctx: RequestContext) -> PolicyDecision:
        ctx.analyse()
        sec = self.security

        # 0. master kill switch -----------------------------------------
        if not sec.automation_master_switch and ctx.category not in (
                "read_only", "query", "information"):
            return PolicyDecision(
                risk=Risk.PROHIBITED, rule="MASTER-SWITCH",
                reason="Computer control is switched off in Permissions.",
                category="automation", tool=ctx.tool,
                reversible=True, target=ctx.target_label,
                consequences="No action was taken.",
            )

        # 1. context-based prohibitions ---------------------------------
        dec = self._check_prohibited_context(ctx)
        if dec:
            return self._finalise(dec, ctx)

        # 2. command pattern prohibitions --------------------------------
        haystacks = [ctx.text.lower()] + [c.lower() for c in ctx.commands]
        for hay in haystacks:
            for rule_id, category, reason, pattern in PROHIBITED_COMMAND_RULES:
                if pattern.search(hay):
                    return self._finalise(PolicyDecision(
                        risk=Risk.PROHIBITED, rule=rule_id, reason=reason,
                        category=category, tool=ctx.tool, reversible=True,
                        target=ctx.target_label or _first(ctx.paths + ctx.urls),
                        consequences="The action was rejected before it ran. "
                                     "Nothing on your computer was changed.",
                        bypass_attempt=ctx.self_approval_attempt,
                    ), ctx)

        # 3. baseline from the tool + user overrides ----------------------
        baseline = ctx.baseline
        override = str(sec.tool_overrides.get(ctx.tool, "")).lower()
        if override == "block":
            baseline = Risk.PROHIBITED
        elif override == "ask" and baseline.rank < Risk.APPROVAL.rank:
            baseline = Risk.APPROVAL
        elif override == "allow":
            # Only honoured when the code already considers it safe.
            if baseline is Risk.ALLOWED:
                pass
            else:
                log.info("Ignoring 'allow' override for %s: code classification "
                         "is %s (overrides may only tighten).", ctx.tool, baseline.value)

        # 4. strict mode --------------------------------------------------
        if sec.mode == "strict" and baseline is Risk.ALLOWED:
            if ctx.tool not in READ_ONLY_TOOLS and ctx.category != "read_only":
                baseline = Risk.APPROVAL

        # 5. contextual approval rules ------------------------------------
        decision = self._check_approval_context(ctx, baseline)

        # 6. command-pattern approvals ------------------------------------
        if decision.risk is Risk.ALLOWED:
            for hay in [ctx.text.lower()] + [c.lower() for c in ctx.commands]:
                for rule_id, category, reason, pattern in APPROVAL_COMMAND_RULES:
                    if pattern.search(hay):
                        decision = PolicyDecision(
                            risk=Risk.APPROVAL, rule=rule_id, reason=reason,
                            category=category, tool=ctx.tool,
                            reversible=ctx.reversible,
                            target=ctx.target_label or _first(ctx.paths + ctx.urls),
                            consequences=_consequence_for(category),
                            bypass_attempt=ctx.self_approval_attempt,
                        )
                        break
                if decision.risk is not Risk.ALLOWED:
                    break

        # 7. a model that tried to fake consent always needs a real human --
        if ctx.self_approval_attempt and decision.risk is Risk.ALLOWED:
            decision = PolicyDecision(
                risk=Risk.APPROVAL, rule="SELF-APPROVAL",
                reason="The assistant supplied its own approval flags "
                       f"({', '.join(ctx.stripped_keys)}). Those are ignored; "
                       "only you can approve this action.",
                category="security", tool=ctx.tool, reversible=ctx.reversible,
                target=ctx.target_label,
                consequences="Nothing happened yet.",
                bypass_attempt=True,
            )

        # 8. session grants (never apply to prohibitions) ------------------
        if decision.risk is Risk.APPROVAL and sec.remember_session_grants:
            gkey = SessionGrants.key(decision.tool, decision.target)
            if self.grants.is_granted(gkey):
                decision = PolicyDecision(
                    risk=Risk.ALLOWED, rule=decision.rule + "+session-grant",
                    reason=decision.reason, category=decision.category,
                    reversible=decision.reversible, target=decision.target,
                    consequences=decision.consequences, tool=decision.tool,
                )
        if decision.risk is Risk.APPROVAL:
            decision.needs_approval = True

        return self._finalise(decision, ctx)

    # -- rule groups ------------------------------------------------------
    def _check_prohibited_context(self, ctx: RequestContext) -> Optional[PolicyDecision]:
        sec = self.security

        if sec.block_password_field_reading:
            creds = ctx.credential_targets()
            if creds:
                return PolicyDecision(
                    risk=Risk.PROHIBITED, rule="CRED-CTX-01",
                    reason="Access to stored passwords, tokens, cookies or "
                           "credential databases is prohibited.",
                    category="credential_access", tool=ctx.tool,
                    reversible=True, target=_first(creds),
                    consequences="The file/location was not opened.",
                    bypass_attempt=ctx.self_approval_attempt,
                )
            low = ctx.text.lower()
            if any(m in low for m in (
                    "read my passwords", "saved passwords", "password manager",
                    "extract cookies", "steal credentials", "get my login",
                    "show me my passwords", "dump credentials")):
                return PolicyDecision(
                    risk=Risk.PROHIBITED, rule="CRED-CTX-02",
                    reason="Searching for or reading saved credentials is "
                           "prohibited.",
                    category="credential_access", tool=ctx.tool,
                    reversible=True, target=ctx.description or ctx.tool,
                    consequences="No credential data was accessed.",
                )

        if sec.prohibit_registry_writes:
            if re.search(r"(?i)\bhk(ey_)?(lm|cr|u|cu|pd|dd|classes_root|"
                         r"local_machine|current_user)\b", ctx.text) and re.search(
                         r"(?i)\b(add|delete|write|set|modify|create|remove|import)\b",
                         ctx.text):
                return PolicyDecision(
                    risk=Risk.PROHIBITED, rule="REG-CTX-01",
                    reason="Modifying registry keys is prohibited.",
                    category="registry", tool=ctx.tool, reversible=True,
                    target=ctx.target_label or _first(ctx.paths),
                    consequences="The registry was not touched.",
                )

        hits = ctx.matches_path_prefix(CRITICAL_PATH_PREFIXES)
        if hits and ctx.category in ("file_write", "file_delete", "execute",
                                     "system", "automation"):
            return PolicyDecision(
                risk=Risk.PROHIBITED, rule="PATH-CTX-01",
                reason="Modifying system-critical locations is prohibited.",
                category="system_files", tool=ctx.tool, reversible=False,
                target=_first(hits),
                consequences="No file or folder in that location was changed.",
            )

        return None

    def _check_approval_context(self, ctx: RequestContext, baseline: Risk) -> PolicyDecision:
        sec = self.security
        cat = ctx.category
        target = ctx.target_label or _first(ctx.paths + ctx.urls)
        reasons = {
            "download": ("DL-CTX", "Downloading content requires your approval."),
            "communication": ("MSG-CTX", "Sending a message or email requires "
                                         "your approval."),
            "file_delete": ("DEL-CTX", "Deleting files requires your approval."),
            "file_overwrite": ("OVR-CTX", "Overwriting an existing file requires "
                                          "your approval."),
            "bulk_file_move": ("MOVE-CTX", "Moving a large collection of files "
                                           "requires your approval."),
            "install": ("INST-CTX", "Installing software requires your approval."),
            "shell": ("SHELL-CTX", "Running a shell command requires your approval."),
            "power": ("PWR-CTX", "Changing the power state requires your approval."),
            "sensitive_personal": ("PII-CTX", "Accessing sensitive personal "
                                              "information requires your approval."),
        }
        if cat in reasons:
            rule, reason = reasons[cat]
            return PolicyDecision(
                risk=Risk.APPROVAL if baseline.rank <= Risk.APPROVAL.rank else baseline,
                rule=rule, reason=reason, category=cat, tool=ctx.tool,
                reversible=ctx.reversible, target=target,
                consequences=_consequence_for(cat),
                bypass_attempt=ctx.self_approval_attempt,
            )

        # Financial / banking sites.
        if sec.require_approval_for_financial_sites:
            fin = ctx.financial_targets()
            if fin:
                return PolicyDecision(
                    risk=Risk.APPROVAL, rule="FIN-CTX",
                    reason="Opening a banking or financial site requires your "
                           "approval.",
                    category="financial", tool=ctx.tool, reversible=True,
                    target=_first(fin),
                    consequences="JARVIS will not log in, read balances or move "
                                 "money without a separate approval for each step.",
                    bypass_attempt=ctx.self_approval_attempt,
                )

        # Writing outside the user's own folders.
        if cat in ("file_write", "file_move", "file_create") and ctx.paths:
            outside = [p for p in ctx.paths if not _inside_user_space(p)]
            if outside:
                return PolicyDecision(
                    risk=Risk.APPROVAL, rule="PATH-CTX-02",
                    reason="Writing outside your personal folders requires your "
                           "approval.",
                    category="file_write", tool=ctx.tool, reversible=True,
                    target=_first(outside),
                    consequences="A file or folder would be created or changed "
                                 "outside your Documents/Desktop/Downloads area.",
                    bypass_attempt=ctx.self_approval_attempt,
                )
            elev = ctx.matches_path_prefix(ELEVATION_REQUIRED_PREFIXES)
            if elev:
                return PolicyDecision(
                    risk=Risk.PROHIBITED, rule="PATH-CTX-03",
                    reason="That location requires administrator rights, which "
                           "JARVIS never uses.",
                    category="privilege_escalation", tool=ctx.tool,
                    reversible=True, target=_first(elev),
                    consequences="Nothing was written.",
                )

        return PolicyDecision(
            risk=baseline, rule="BASELINE", reason=ctx.description or ctx.tool,
            category=cat, tool=ctx.tool, reversible=ctx.reversible, target=target,
            consequences=_consequence_for(cat),
            bypass_attempt=ctx.self_approval_attempt,
        )

    def _finalise(self, decision: PolicyDecision, ctx: RequestContext) -> PolicyDecision:
        if decision.risk is Risk.PROHIBITED:
            log.warning("BLOCKED tool=%s rule=%s reason=%s target=%s",
                        ctx.tool, decision.rule, decision.reason, decision.target)
        return decision

    # -- public helpers ---------------------------------------------------
    def classify_tool(self, tool) -> Risk:
        """Convenience wrapper used by the UI permissions page."""
        return self.evaluate(RequestContext(
            tool=tool.name, args={}, category=tool.category,
            baseline=tool.risk, description=tool.description,
            reversible=tool.reversible,
        )).risk

    def describe_tool_rules(self, tool) -> List[str]:
        out = [f"category: {tool.category}", f"code risk: {tool.risk.value}"]
        override = self.security.tool_overrides.get(tool.name)
        if override:
            out.append(f"your override: {override}")
        return out


# ---------------------------------------------------------------------------
def _first(items: Sequence[str]) -> str:
    return items[0] if items else ""


def _consequence_for(category: str) -> str:
    return {
        "download": "A file will be written to your disk and could contain "
                    "anything. Nothing is executed automatically.",
        "communication": "A message leaves your computer and cannot be recalled "
                         "once sent.",
        "file_delete": "Deleted files may not be recoverable.",
        "file_overwrite": "The previous contents of the file will be lost.",
        "bulk_file_move": "Many files change location; apps relying on the old "
                          "paths may stop working.",
        "install": "New software is added to your system.",
        "shell": "The command runs with your user rights. JARVIS cannot "
                 "guarantee what a free-form command will do.",
        "power": "Unsaved work in other applications may be lost.",
        "sensitive_personal": "Private personal information would be read.",
        "financial": "Financial account details could be exposed.",
        "file_write": "A new file or folder is created.",
    }.get(category, "The action changes the state of your computer.")


def _inside_user_space(path: str) -> bool:
    """True when ``path`` is inside the current user's own folders."""
    import os

    p = _norm_path(path)
    home = _norm_path(os.path.expanduser("~"))
    roots = [home]
    for env in ("USERPROFILE", "HOME", "DESKTOP", "DOCUMENTS", "DOWNLOADS",
                "MUSIC", "PICTURES", "VIDEOS", "APPDATA", "LOCALAPPDATA",
                "TMP", "TEMP", "ONEDRIVE", "ONEDRIVECOMMERCIAL"):
        v = os.environ.get(env)
        if v:
            roots.append(_norm_path(v))
    # Well-known Windows user locations.
    roots.extend([
        "c:\\users", "\\users\\public", "/home", "/tmp", "/var/folders",
    ])
    return any(p == r or p.startswith(r + "\\") or p.startswith(r + "/")
               for r in roots)
