#!/usr/bin/env python3

# File: gitnotify.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-04-28
# Description: Production-grade CLI tool for monitoring new GitHub notifications
#              and pushing desktop alerts via GNTP.
# License: MIT
#
# gitnotify.ini keys:
#   [auth]     token       = classic PAT with the "notifications" scope (comma-separated for multi-user)
#   [interval] seconds     = polling interval in seconds (default 60)
#   [growl]    host        = comma-separated GNTP hosts (default 127.0.0.1)
#   [growl]    sticky      = 1/0, sticky GNTP notifications
#   [subject]  exceptions  = comma-separated repo full_name substrings to IGNORE
#   [subject]  always      = 1/0, re-notify even if already seen this run
#   [try]      max         = max GNTP register attempts per host (default 2)
#   [status]   clear       = 1 to clear the "seen" cache + screen on next cycle

tprint = None

import logging
import argparse
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from configset import configset
from github import Github
from github.GithubException import (
    BadCredentialsException,
    GithubException,
    RateLimitExceededException,
    TwoFactorException,
)
from rich.console import Console
from rich.logging import RichHandler

console = Console()

try:
    from github import Auth
    HAS_AUTH = True
except ImportError:  # older PyGithub without the Auth module
    HAS_AUTH = False

def is_debug() -> bool:
    return str(os.getenv("GITNOTIFY_DEBUG", "0")).lower() in ("1", "true", "yes", "ok", "on")

def dprint(text):
    import inspect
    # Convert non-string inputs to string for processing
    str_text = str(text) if not isinstance(text, str) else text
    
    if is_debug():
        # Get the caller's stack frame
        caller_frame = inspect.currentframe().f_back  # type: ignore
        filename = caller_frame.f_code.co_filename  # type: ignore
        line_no = caller_frame.f_lineno  # type: ignore

        lines = f"{filename}:{line_no}"

        console.print(
            f"[bold #FFAA00]🐞[/] [bold #550000 on #FFAA00]{str_text}[/] [white on"
            f" blue]\\[{lines}][/]"
        )

try:
    from gntplib import Publisher, SocketCallback  # type: ignore
except ImportError:  # pragma: no cover
    Publisher = None
    SocketCallback = object

class DummySubject:
    title = "Test notification github notification"
    url = "https://github.com/dummy/test/pull/1"

class DummyRepository:
    full_name = "dummy/test-repo"

class DummyNotification:
    id = "test-00000"
    subject = DummySubject()
    repository = DummyRepository()

    def mark_as_read(self):
        # The callbacks will trigger this when you click the notification
        pass

class Callback(SocketCallback):
    """Structured GNTP callback: distinct handlers per click/close/timeout event."""

    def __init__(self, notification):
        super().__init__(notification)
        self.notification = notification

    def on_click(self, response):
        try:
            self.notification.mark_as_read()
            console.print(f"[bold green]Notification marked as read: {self.notification.subject.title}[/]")
        except Exception as e:
            console.print(f"[red]Error marking notification as read: {e}[/]")

    def on_close(self, response):
        pass

    def on_timeout(self, response):
        pass


class SimpleCallback:
    """Fallback callback shape for gntplib versions that expect a plain callable."""

    def __init__(self, notification):
        self.notification = notification

    def __call__(self, response=None):
        try:
            self.notification.mark_as_read()
            console.print(f"[bold green]Notification marked as read: {self.notification.subject.title}[/]")
        except Exception as e:
            console.print(f"[red]Error marking notification as read: {e}[/]")

if str(os.getenv('PYDEBUGGER', '0')).lower() in ('1', 'true', 'ok', 'yes', 'on'):
    try:
        from pydebugger.debug import debug  # type: ignore
    except ImportError:  # pragma: no cover
        def debug(*args, **kwargs):
            pass
else:
    def debug(*args, **kwargs):
        pass

LOG_LEVEL = os.getenv('LOG_LEVEL', 'CRITICAL')

try:
    from richcolorlog import setup_logging, print_exception
    log = setup_logging("gitnotify", level=LOG_LEVEL)
    tprint = print_exception
except:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, show_path=False, markup=True, rich_tracebacks=True)],
    )
    log = logging.getLogger("gitnotify")

CONFIG_PATH = Path(__file__).parent / "gitnotify.ini"
ICON_PATH = Path(__file__).parent / "icon.png"

SHUTDOWN = False


def _handle_signal(signum, frame):
    global SHUTDOWN
    SHUTDOWN = True
    console.print(
        f"\n[bold #FFAA00]{get_date()}[/] - [bold #FF5555]Received signal {signum}, shutting down gracefully ...[/]"
    )


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def get_date() -> str:
    return datetime.now().strftime("%Y/%m/%d %H:%M:%S.%f")


class Config:
    """Thin typed wrapper around configset for this tool's config needs."""

    def __init__(self, path: Path):
        debug(configfile=str(path))
        log.debug(f"configfile: {str(path)}")
        self._c = configset(str(path))

    def get(self, section, option, default=None):
        val = self._c.get_config(section, option)
        if isinstance(val, str):
            val = val.strip().strip("'").strip('"').strip()
        return val if val not in (None, "") else default

    def get_int(self, section, option, default):
        val = self.get(section, option, default)
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def get_bool(self, section, option, default=False):
        val = self.get(section, option, default)
        if isinstance(val, bool):
            return val
        if val is None:
            return default
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def get_list(self, section, option):
        return self._c.get_config_as_list(section, option) or []

    def set(self, section, option, value):
        self._c.write_config(section, option, value)


debug(CONFIG_PATH = CONFIG_PATH)
log.debug(f"CONFIG_PATH: {CONFIG_PATH}")

CONFIG = Config(CONFIG_PATH)


def _clean_token_string(val: str) -> str:
    """Completely strip wrapping quotes, parentheses, brackets, and tuple artifacts from token strings."""
    if not isinstance(val, str):
        val = str(val)
    val = val.strip()
    # Strip lingering tuple or list wrapping like ('...', ) or ["..."]
    while (val.startswith("(") and val.endswith(")")) or (val.startswith("[") and val.endswith("]")):
        val = val[1:-1].strip()
    return val.strip("'").strip('"').strip()


def diagnose_token(token):
    """Hit the GitHub API directly (bypassing PyGithub) to surface the exact
    reason a token is being rejected."""
    masked = f"{token[:7]}...{token[-4:]}" if len(token) > 11 else "(too short to mask safely)"
    console.print(f"[dim]token length: {len(token)}, looks like: {masked}[/]")

    if any(ch.isspace() for ch in token):
        console.print("[bold yellow]Warning: token contains whitespace characters (likely corrupted in the ini file).[/]")

    try:
        headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        debug(headers = headers)
        log.debug(f"headers: {headers}")
        r = requests.get(
            "https://api.github.com/user",
            headers=headers,
            timeout=10,
        )
        console.print(f"[dim]Raw GitHub API check: HTTP {r.status_code}[/]")
        if r.status_code == 200:
            scopes = r.headers.get("X-OAuth-Scopes", "")
            console.print(f"[dim]Token scopes reported by GitHub: {scopes or '(none reported / fine-grained token)'}[/]")
            console.print("[bold yellow]Raw call succeeded but PyGithub rejected it — likely a PyGithub/library version mismatch.[/]")
        else:
            try:
                msg = r.json().get("message")
                debug(msg = msg)
                log.info(f"msg: {msg}")
            except Exception:
                msg = r.text[:200]
                debug(msg = msg)
                log.info(f"msg: {msg}")
            console.print(f"[dim]GitHub says: {msg}[/]")
    except Exception as e:
        if is_debug():
            tprint("Raw diagnostic request failed")  # type: ignore
        else:
            console.print(f"[dim]Raw diagnostic request failed: {e}[/]")


def resolve_tokens(cli_tokens=None):
    raw_candidates = []

    # 1. From CLI
    if cli_tokens:
        for t in cli_tokens:
            raw_candidates.append(str(t))
    else:
        # 2. Check [auth] section first, then fallback to [token] section
        for sec in ("auth", "token"):
            cfg_list = CONFIG.get_list(sec, "token")
            debug(cfg_list = cfg_list)
            log.debug(f"cfg_list: {cfg_list}")
            if cfg_list:
                raw_candidates.extend([str(item) for item in cfg_list])
            else:
                raw_val = CONFIG.get(sec, "token")
                if raw_val:
                    raw_candidates.append(str(raw_val))

    # 3. Environment fallback
    if not raw_candidates and os.getenv("GITHUB_TOKEN"):
        raw_candidates.append(os.getenv("GITHUB_TOKEN"))

    tokens = []
    for raw in raw_candidates:
        # Clean tuple string artifacts
        cleaned_raw = _clean_token_string(raw)
        # Split on commas for multi-token entries
        parts = cleaned_raw.split(",")
        for p in parts:
            clean_p = _clean_token_string(p)
            if clean_p:
                tokens.append(clean_p)

    if any(t.lower() in ("q", "quit", "exit", "x") for t in tokens):
        console.print("[bold red]Aborted by user.[/]")
        sys.exit(1)

    while not tokens:
        token_input = console.input(
            "[#00FFFF bold]x|exit|q|quit = exit/quit[/] [white on red]TOKEN(S) (comma-separated):[/] "
        ).strip()
        if token_input.lower() in ("q", "quit", "exit", "x"):
            console.print("[bold red]Aborted by user.[/]")
            sys.exit(1)
        for p in token_input.split(","):
            clean_p = _clean_token_string(p)
            if clean_p:
                tokens.append(clean_p)

        # Write clean plain string format back to config to avoid configset tuple serialization
        CONFIG.set("auth", "token", ", ".join(tokens))

    return tokens


def make_client(token):
    if HAS_AUTH:
        return Github(auth=Auth.Token(token))
    return Github(token)  # fallback for old PyGithub versions


def mark_as_read(notification):
    try:
        notification.mark_as_read()
    except GithubException as e:
        log.warning("Failed to mark notification %s as read: %s", notification.id, e)


def build_publishers(hosts, max_try=2):
    """Create and register one GNTP publisher per host, retrying registration."""
    debug(Publisher = Publisher)
    log.debug(f"Publisher: {Publisher}")
    if Publisher is None:
        log.warning("gntplib is not installed; desktop notifications are disabled (console output only).")
        debug("gntplib is not installed; desktop notifications are disabled (console output only).")
        return []

    publishers = []
    icon = str(ICON_PATH) if ICON_PATH.exists() else None
    debug(icon = icon)
    log.debug(f"icon: {icon}")

    for h in hosts:
        target_host = None if h in ("127.0.0.1", "localhost") else h
        debug(target_host = target_host)
        log.debug(f"target_host: {target_host}")
        pub = Publisher("Github Notify", ["New Notification"], icon=icon, host=target_host)
        for attempt in range(1, max_try + 1):
            try:
                pub.register()
                publishers.append(pub)
                break
            except Exception as e:
                if is_debug():
                    tprint()  # type: ignore
                log.debug("GNTP register attempt %d/%d failed for host %r: %s", attempt, max_try, h, e)
                if attempt < max_try:
                    time.sleep(0.5)
        else:
            log.warning("Could not register GNTP publisher for host %r after %d attempts.", h, max_try)

    return publishers


def send_notification(publishers, notification, sticky=False):
    if not publishers:
        return

    title = "New Notification"
    message = f"{notification.subject.title} ({notification.repository.full_name})"

    for pub in publishers:
        try:
            # Primary: structured callback object (on_click/on_close/on_timeout)
            pub.publish(title, message, gntp_callback=Callback(notification), sticky=sticky)
        except Exception as e1:
            console.print(f"[bold red]Primary callback failed with error:[/] {e1}") 
            if str(e1).lower() == "timed out":
                continue
            try:
                # Fallback: plain callable object
                pub.publish(title, message, callback=SimpleCallback(notification), sticky=sticky)
            except Exception as e2:
                if is_debug():
                    tprint()  # type: ignore
                else:
                    if str(e2).lower() != "timed out":
                        log.warning("GNTP publish failed (both callback fallbacks): %s / %s", e1, e2)


def fetch_notifications(gh):
    notifications = gh.get_user().get_notifications()
    debug(notifications=notifications, debug=1)
    return notifications


def maybe_clear_seen(seen):
    if CONFIG.get_int("status", "clear", 0) == 1:
        seen.clear()
        CONFIG.set("status", "clear", "0")
        os.system("cls" if sys.platform == "win32" else "clear")


def monitor(gh, user_login, publishers, exceptions, always, sticky, seen):
    maybe_clear_seen(seen)
    console.print(f"[bold #00FFFF]{get_date()}[/] - [bold #FFFF00]START monitoring [{user_login}] ...[/]")

    new_count = 0
    for notification in fetch_notifications(gh):
        if SHUTDOWN:
            break

        repo_name = notification.repository.full_name
        title = notification.subject.title

        if os.getenv("VERBOSE") == "1":
            console.print(
                f"[bold #FFAA00]{get_date()}[/] - [bold cyan][{user_login}][/] [bold #00FFFF]{title}:[/] "
                f"[bold #FFFF00]{repo_name}[/] [link={notification.subject.url}]:point_right:[/]"
            )

        is_excluded = bool(exceptions) and any(k.lower() in repo_name.lower() for k in exceptions)
        if is_excluded:
            mark_as_read(notification)
            continue

        seen_key = (user_login, notification.id)
        if not always and seen_key in seen:
            continue

        console.print(
            f"[bold #FFAA00]{get_date()}[/] - [bold cyan][{user_login}][/] [bold #00FFFF]{title}:[/] "
            f"[bold #FFFF00]{repo_name}[/] [link={notification.subject.url}]:point_right:[/]"
        )

        send_notification(publishers, notification, sticky=sticky)
        new_count += 1

        if not always:
            seen.add(seen_key)

    console.print(f"[bold #00FFFF]{get_date()}[/] - [bold #FFAA00]END monitoring [{user_login}] ... ({new_count} new)[/]")
    return new_count


def _sleep_interruptible(seconds):
    for _ in range(max(int(seconds), 0)):
        if SHUTDOWN:
            break
        time.sleep(1)


def init_github_clients(tokens):
    clients = []
    for token in tokens:
        gh = make_client(token)
        try:
            user_login = gh.get_user().login
            console.print(f"[bold #00FFFF]{get_date()}[/] - [bold green]Authenticated as {user_login}[/]")
            clients.append((gh, user_login))
        except BadCredentialsException:
            console.print("[bold white on red]Invalid or expired GitHub token (per PyGithub).[/]")
            diagnose_token(token)
            console.print(
                "[yellow]If the raw check above also failed: the token itself is bad/expired/revoked — "
                "generate a new classic token with the 'notifications' scope. "
                "If the raw check succeeded: run 'pip install -U PyGithub' and retry.[/]"
            )
        except TwoFactorException:
            console.print("[bold white on red]This operation requires two-factor authentication on the account.[/]")

    if not clients:
        console.print("[bold red]No valid GitHub clients available. Exiting.[/]")
        sys.exit(1)

    return clients


def run(args):
    # 1. Setup GNTP publishers first
    hosts = args.host or CONFIG.get_list("growl", "host") or ["127.0.0.1"]
    debug(hosts = hosts)
    log.debug(f"hosts: {hosts}")
    max_try = CONFIG.get_int("try", "max", 2)
    debug(max_try = max_try)
    log.debug(f"max_try: {max_try}")
    publishers = build_publishers(hosts, max_try=max_try)
    debug(publishers = publishers)
    log.debug(f"publishers: {publishers}")
    sticky = args.sticky or CONFIG.get_bool("growl", "sticky", False)
    debug(sticky = sticky)
    log.debug(f"sticky: {sticky}")

    # 2. Handle Test Notification
    if args.test:
        if not publishers:
            console.print("[bold red]No GNTP publishers available to send the test notification.[/]")
            return
        
        console.print("[bold yellow]Sending dummy notification...[/]")
        dummy_notification = DummyNotification()
        send_notification(publishers, dummy_notification, sticky=sticky)
        return

    # 3. Standard execution
    tokens = resolve_tokens(args.token)
    clients = init_github_clients(tokens)

    exceptions = args.exceptions or CONFIG.get_list("subject", "exceptions")
    always = args.always or CONFIG.get_bool("subject", "always", False)
    interval = args.interval or CONFIG.get_int("interval", "seconds", 60)

    seen = set()

    if args.once:
        for gh, user_login in clients:
            if SHUTDOWN:
                break
            monitor(gh, user_login, publishers, exceptions, always, sticky, seen)
        return

    backoff = 5
    while not SHUTDOWN:
        try:
            for gh, user_login in clients:
                if SHUTDOWN:
                    break
                monitor(gh, user_login, publishers, exceptions, always, sticky, seen)

            backoff = 5
            _sleep_interruptible(interval)
        except RateLimitExceededException as e:
            wait = 60
            reset = getattr(e, "headers", {}) or {}
            reset_ts = reset.get("x-ratelimit-reset")
            if reset_ts:
                try:
                    wait = max(int(reset_ts) - int(time.time()), 30)
                except ValueError:
                    pass
            console.print(f"[black on #FFFF00]Rate limit exceeded, waiting {wait}s ...[/]")
            _sleep_interruptible(wait)
        except BadCredentialsException:
            console.print("[bold white on red]Token became invalid/expired. Exiting.[/]")
            sys.exit(1)
        except GithubException as e:
            log.error("GitHub API error: %s", e)
            _sleep_interruptible(min(backoff, 60))
            backoff = min(backoff * 2, 60)
        except Exception as e:
            if is_debug():
                tprint()  # type: ignore
            else:
                log.exception("Unexpected error: %s", e)
            if "HTTPSConnectionPool" in str(e):
                console.print("[black on #FFFF00]Network issue, retrying ...[/]")
            _sleep_interruptible(min(backoff, 60))
            backoff = min(backoff * 2, 60)

    console.print(f"[bold #00FFFF]{get_date()}[/] - [bold #FF5555]Stopped.[/]")

def parse_args():
    p = argparse.ArgumentParser(
        prog="gitnotify",
        description="Monitor GitHub notifications and push desktop alerts via GNTP.",
    )
    p.add_argument("-t", "--token", action="append", help="GitHub classic PAT with 'notifications' scope (can be specified multiple times or comma-separated). Overrides config/env.")
    p.add_argument("-i", "--interval", type=int, help="Polling interval in seconds (default: config or 60).")
    p.add_argument("-H", "--host", action="append", help="GNTP host to notify (repeatable). Default: 127.0.0.1.")
    p.add_argument("-x", "--exceptions", action="append", help="Repo full_name substrings to ignore (repeatable).")
    p.add_argument("-a", "--always", action="store_true", help="Always re-notify, ignoring the seen cache.")
    p.add_argument("-s", "--sticky", action="store_true", help="Send sticky (persistent) GNTP notifications.")
    p.add_argument("--once", action="store_true", help="Run a single check and exit instead of looping.")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose output (equivalent to VERBOSE=1).")
    p.add_argument("-T", "--test", action="store_true", help="Send a dummy test notification and exit.")
    return p.parse_args()


def main():
    args = parse_args()
    if args.verbose:
        os.environ["GITNOTIFY_DEBUG"] = "1"
        os.environ["LOG_LEVEL"] = "DEBUG"
        os.environ["PYDEBUGGER"] = "1"
        log.setLevel(logging.DEBUG)

    try:
        run(args)
    except KeyboardInterrupt:
        console.print(f"\n[bold #00FFFF]{get_date()}[/] - [bold #FF5555]Interrupted.[/]")
        sys.exit(0)


if __name__ == "__main__":
    main()
