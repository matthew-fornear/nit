#!/usr/bin/env python3
import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    from patchright.sync_api import sync_playwright
except ImportError:
    raise SystemExit("pip install patchright && patchright install chrome")

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"
CHROME_PROFILE_DIR = SCRIPT_DIR / "chrome-profile"


def _safe_host(host: str) -> str:
    return re.sub(r"[^\w.-]", "_", host).strip("._") or "unknown"


def _output_dir_from_url(url: str) -> Path:
    host = urlparse(url).hostname or "unknown"
    return OUTPUT_DIR / _safe_host(host)


# Hosts that dominate request volume (ads, trackers, tag managers) but are not the site under test.
# Without this, _output_dir_from_entries() often picks e.g. pagead2.googlesyndication.com over the real site.
_HOST_SUFFIX_DENY = frozenset(
    (
        "googlesyndication.com",
        "doubleclick.net",
        "googleadservices.com",
        "googletagmanager.com",
        "google-analytics.com",
        "g.doubleclick.net",
        "facebook.com",
        "facebook.net",
        "scorecardresearch.com",
        "adsafeprotected.com",
        "rubiconproject.com",
        "3lift.com",
        "pubmatic.com",
        "criteo.com",
        "amazon-adsystem.com",
        "adnxs.com",
        "liadm.com",
        "tiktok.com",
        "tiktokw.us",
    )
)


def _is_third_party_noise_host(host: str) -> bool:
    h = (host or "").lower()
    if not h:
        return True
    for suf in _HOST_SUFFIX_DENY:
        if h == suf or h.endswith("." + suf):
            return True
    return False


def _output_dir_from_entries(entry_list: list) -> Path:
    from collections import Counter

    hosts: Counter[str] = Counter()
    for e in entry_list:
        url = e.get("url") or ""
        try:
            host = urlparse(url).hostname or ""
            if host and not host.startswith(".") and host not in ("localhost", "127.0.0.1"):
                if not _is_third_party_noise_host(host):
                    hosts[host] += 1
        except Exception:
            pass
    if not hosts:
        name = "capture"
    else:
        name = _safe_host(hosts.most_common(1)[0][0])
    return OUTPUT_DIR / name


# Chrome's own DevTools remote-debugging refuses to open the default user data directory.
# Warn early so the user doesn't see a cryptic Chrome crash.
_CHROME_DEFAULT_DIRS = {
    "linux": Path.home() / ".config" / "google-chrome",
    "darwin": Path.home() / "Library" / "Application Support" / "Google" / "Chrome",
    "win32": Path(
        __import__("os").environ.get("LOCALAPPDATA", "")
    ) / "Google" / "Chrome" / "User Data",
}


def _is_default_chrome_dir(p: Path) -> bool:
    import sys as _sys
    default = _CHROME_DEFAULT_DIRS.get(_sys.platform) or _CHROME_DEFAULT_DIRS.get("linux")
    try:
        return p.resolve() == default.resolve()
    except Exception:
        return False


_IGNORE_DEFAULT_ARGS = [
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-popup-blocking",
]


def _launch_browser(p):
    # Prefer real Chrome/Edge so UA and fingerprint match the binary; avoid automation flags.
    # Patchright already strips --enable-automation and sets --disable-blink-features=AutomationControlled
    # internally — do not re-add them here or pass extra args that could alter its patched defaults.
    for channel in ("chrome", "msedge", None):
        try:
            b = p.chromium.launch(
                channel=channel,
                headless=False,
                # Do not list "--disable-extensions" here — listing it removes Playwright's default
                # and leaves extensions enabled (extra tabs, noise, million ad requests).
                ignore_default_args=_IGNORE_DEFAULT_ARGS,
            )
            return b
        except Exception:
            continue
    raise RuntimeError("No usable browser. Run: patchright install chrome")


def _launch_persistent_context(p, user_data_dir: Path, context_opts: dict):
    """
    Launch with a real on-disk Chrome profile instead of a managed context.

    This is the patchright-recommended approach for sites that use reCAPTCHA Enterprise
    or heavy browser fingerprinting (LinkedIn, Google, etc.).  A persistent profile has
    real browsing history, stored credentials, and a stable fingerprint that reCAPTCHA
    scores as human.  The profile directory must NOT be the default Chrome user data
    directory (Chrome's DevTools remote-debugging rejects it).
    """
    if _is_default_chrome_dir(user_data_dir):
        raise SystemExit(
            f"Cannot use the default Chrome profile directory ({user_data_dir}).\n"
            "Chrome's DevTools debugging rejects the default dir.  Copy it into this repo first:\n"
            f"  cp -r '{user_data_dir}' {CHROME_PROFILE_DIR}\n"
            f"Then pass:  --user-data-dir {CHROME_PROFILE_DIR}"
        )
    user_data_dir.mkdir(parents=True, exist_ok=True)
    for channel in ("chrome", "msedge", None):
        try:
            ctx = p.chromium.launch_persistent_context(
                str(user_data_dir),
                channel=channel,
                headless=False,
                ignore_default_args=_IGNORE_DEFAULT_ARGS,
                **context_opts,
            )
            return ctx
        except Exception:
            continue
    raise RuntimeError("No usable browser for persistent context. Run: patchright install chrome")


def _is_entry_page(url: str) -> bool:
    """Exclude the local file entry.html document only (not random URLs containing 'entry.html')."""
    try:
        p = urlparse(url or "")
        if p.scheme != "file":
            return False
        return p.path.replace("\\", "/").rstrip("/").endswith("entry.html")
    except Exception:
        return False


def _is_boring_page_url(url: str) -> bool:
    u = url or ""
    if not u or u in ("about:blank",):
        return True
    if _is_entry_page(u):
        return True
    if u.startswith("chrome://") or u.startswith("devtools://") or u.startswith("edge://"):
        return True
    return False


def resolve_best_site_url(context) -> str | None:
    """Prefer the last open tab with a normal http(s) URL (handles browsing in a 2nd tab)."""
    try:
        pages = list(context.pages)
    except Exception:
        return None
    for pg in reversed(pages):
        try:
            u = pg.url or ""
        except Exception:
            continue
        if _is_boring_page_url(u):
            continue
        if u.startswith("https://") or u.startswith("http://"):
            try:
                h = urlparse(u).hostname
                if h and h not in ("localhost", "127.0.0.1"):
                    return u
            except Exception:
                continue
    return None


def should_capture(url: str, domains: list[str] | None) -> bool:
    if _is_entry_page(url):
        return False
    if not domains:
        return True
    try:
        host = urlparse(url).hostname or ""
        return any(host == d or host.endswith("." + d) for d in domains)
    except Exception:
        return False


def should_capture_body(url: str, content_type: str) -> bool:
    if not content_type:
        return False
    ct = content_type.lower()
    return (
        "application/json" in ct
        or "application/javascript" in ct
        or "text/javascript" in ct
        or "graphql" in url
        or "/api/" in url
        or url.rstrip("/").endswith(".js")
    )


def _is_script_entry(entry: dict) -> bool:
    resp = entry.get("response")
    if not resp or resp.get("body") is None:
        return False
    url = entry.get("url") or ""
    ct = (resp.get("response_headers") or {}).get("content-type") or ""
    ct = ct.lower()
    return (
        "application/javascript" in ct
        or "text/javascript" in ct
        or url.rstrip("/").endswith(".js")
    )


def _har_headers_to_dict(headers: list | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in headers or []:
        name = (h.get("name") or "").strip()
        if not name:
            continue
        out[name] = h.get("value") or ""
    return out


def har_file_to_entry_list(har_path: Path, domains: list[str] | None) -> list[dict]:
    """Convert Playwright HAR 1.2 to the same shape as listener-based networkcalls entries."""
    if not har_path.is_file():
        return []
    try:
        data = json.loads(har_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = (data.get("log") or {}).get("entries") or []
    out: list[dict] = []
    for item in raw:
        req = item.get("request") or {}
        url = req.get("url") or ""
        if not should_capture(url, domains):
            continue
        method = req.get("method") or "GET"
        pd = req.get("postData")
        post_data = None
        if isinstance(pd, dict):
            post_data = pd.get("text")
        elif isinstance(pd, str):
            post_data = pd

        resp = item.get("response") or {}
        status = resp.get("status")
        status_text = resp.get("statusText") or ""
        resp_headers = _har_headers_to_dict(resp.get("headers"))
        content = resp.get("content") or {}
        mime = (content.get("mimeType") or "").lower()
        text = content.get("text")
        encoding = content.get("encoding")
        body: str | None = None
        if text is not None:
            if encoding == "base64":
                try:
                    rawb = base64.b64decode(text)
                    body = rawb.decode("utf-8", errors="replace")
                except Exception:
                    body = None
            else:
                body = text
        # Same body retention rules as live capture (keeps JSON small).
        ct_hint = mime or resp_headers.get("Content-Type") or resp_headers.get("content-type") or ""
        if body is not None and not should_capture_body(url, ct_hint):
            body = None

        out.append(
            {
                "url": url,
                "method": method,
                "request_headers": _har_headers_to_dict(req.get("headers")),
                "request_post_data": post_data,
                "response": {
                    "status": status,
                    "status_text": status_text,
                    "response_headers": resp_headers,
                    "body": body,
                },
            }
        )
    return out


def main(
    out_dir: Path | None,
    state_file: Path,
    domains: list[str] | None,
    no_session: bool,
    cdp_debugger: bool,
    user_data_dir: Path | None,
):
    dynamic_scripts: list[dict] = []  # eval / new Function code (unused when using CDP)
    inline_scripts: list[dict] = []   # { "url": str, "code": str } per inline block
    cdp_parsed_scripts: list[dict] = []  # CDP Debugger.scriptParsed: scriptId, url, source
    # --user-data-dir uses a persistent Chrome profile; session.json is not used in that mode.
    use_persistent = user_data_dir is not None
    load_state = not use_persistent and not no_session and state_file.exists()
    final_page_url: str | None = None  # set after user presses Enter; used for output dir

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    har_path = OUTPUT_DIR / "_last_capture.har"
    if har_path.exists():
        try:
            har_path.unlink()
        except OSError:
            pass

    with sync_playwright() as p:
        # HAR recording context options shared by both launch paths.
        # ignore_https_errors and service_workers="block" are intentionally omitted:
        # both use CDP commands (Security.setIgnoreCertificateErrors / ServiceWorker intercept)
        # that are detectable by anti-bot fingerprinting.
        context_opts: dict = dict(
            no_viewport=True,
            locale="en-US",
            record_har_path=str(har_path),
            record_har_content="embed",
            record_har_mode="full",
        )

        if use_persistent:
            # launch_persistent_context: real on-disk Chrome profile → best reCAPTCHA/anti-bot score.
            # Session state lives in the profile dir; session.json is not used.
            context = _launch_persistent_context(p, user_data_dir, context_opts)
            browser = None
        else:
            browser = _launch_browser(p)
            if load_state:
                context_opts["storage_state"] = str(state_file)
            context = browser.new_context(**context_opts)

        page = context.new_page() if not use_persistent else (context.pages[0] if context.pages else context.new_page())

        # CDP Debugger is opt-in: Debugger.enable + thousands of getScriptSource calls after Enter
        # stress Chrome, can open odd targets, and look like a "URL flood". Inline scripts still
        # come from DOM below; network from HAR.
        cdp = None
        cdp_script_ids: list[dict] = []
        if cdp_debugger:
            try:
                cdp = context.new_cdp_session(page)
                cdp.send("Debugger.enable")

                def on_script_parsed(params):
                    script_id = params.get("scriptId") or ""
                    url = params.get("url") or ""
                    if script_id:
                        cdp_script_ids.append({"scriptId": script_id, "url": url})

                cdp.on("Debugger.scriptParsed", on_script_parsed)
            except Exception as e:
                print("Warning: CDP session failed, dynamic script capture disabled:", e, file=sys.stderr)
                cdp = None

        entry_html = SCRIPT_DIR / "entry.html"
        if entry_html.exists():
            page.goto(entry_html.as_uri(), wait_until="domcontentloaded", timeout=10000)
        else:
            page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)

        input("Press Enter to save capture and session, then close... ")

        final_page_url = resolve_best_site_url(context) or (page.url or None)

        # Fetch CDP script sources only when --cdp-debugger (can be very slow / noisy)
        if cdp is not None and cdp_script_ids:
            try:
                for item in cdp_script_ids:
                    try:
                        result = cdp.send("Debugger.getScriptSource", {"scriptId": item["scriptId"]})
                        source = result.get("scriptSource", "")
                    except Exception:
                        source = ""
                    cdp_parsed_scripts.append({
                        "scriptId": item["scriptId"],
                        "url": item["url"],
                        "source": source,
                    })
            except Exception as e:
                print("Warning: CDP getScriptSource failed:", e, file=sys.stderr)
            try:
                cdp.send("Debugger.disable")
                cdp.detach()
            except Exception:
                pass

        _inline_js = """
            (function() {
                var inlineEls = Array.from(document.querySelectorAll('script:not([src])'));
                return inlineEls
                    .map(function(s) { return s.textContent || ''; })
                    .filter(function(c) { return c.trim().length > 0; });
            })()
        """
        for pg in list(context.pages):
            try:
                codes = pg.evaluate(_inline_js)
                loc = pg.url or ""
                if isinstance(codes, list):
                    for code in codes:
                        if isinstance(code, str) and code.strip():
                            inline_scripts.append({"url": loc, "code": code})
            except Exception:
                pass

        if not use_persistent:
            try:
                context.storage_state(path=str(state_file))
            except Exception as e:
                print("Warning: could not save session:", e, file=sys.stderr)

        # Playwright writes the HAR when the browser *context* closes — not only when the browser exits.
        try:
            context.close()
        except Exception as e:
            print("Warning: context.close failed (HAR may be incomplete):", e, file=sys.stderr)
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    entry_list = har_file_to_entry_list(har_path, domains)

    from datetime import datetime, timezone
    meta = {
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "total_entries": len(entry_list),
        "har_source": str(har_path),
    }
    if out_dir is None:
        # Prefer the page the user was on (e.g. underdogsportsbook.com) over the most-requested
        # host (e.g. fonts.gstatic.com from third-party resources).
        page_host = None
        if final_page_url and not _is_entry_page(final_page_url):
            try:
                h = urlparse(final_page_url).hostname
                if h and h not in ("localhost", "127.0.0.1"):
                    page_host = h
            except Exception:
                pass
        if page_host:
            out_dir = OUTPUT_DIR / _safe_host(page_host)
        else:
            out_dir = _output_dir_from_entries(entry_list)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Single scripts.json: network + inline + dynamic, each with script_type
    script_entries_network = [e for e in entry_list if _is_script_entry(e)]
    combined = []
    for e in script_entries_network:
        combined.append({"script_type": "network", **e})
    for e in inline_scripts:
        combined.append({
            "script_type": "inline",
            "url": e.get("url", ""),
            "body": e.get("code", ""),
        })
    for e in cdp_parsed_scripts:
        if (e.get("url") or "").strip() == "":
            combined.append({
                "script_type": "dynamic",
                "scriptId": e.get("scriptId", ""),
                "url": e.get("url", ""),
                "body": e.get("source", ""),
            })

    networkcalls_path = out_dir / "networkcalls.json"
    scripts_path = out_dir / "scripts.json"
    networkcalls_path.write_text(
        json.dumps({**meta, "entries": entry_list}, indent=2), encoding="utf-8"
    )
    scripts_meta = {
        **meta,
        "total_scripts": len(combined),
        "total_network": len(script_entries_network),
        "total_inline": len(inline_scripts),
        "total_dynamic": sum(1 for e in cdp_parsed_scripts if (e.get("url") or "").strip() == ""),
    }
    scripts_path.write_text(
        json.dumps({**scripts_meta, "entries": combined}, indent=2),
        encoding="utf-8",
    )
    print("Saved", len(entry_list), "requests to", networkcalls_path)
    print("Saved", len(combined), "scripts (network/inline/dynamic) to", scripts_path)
    if len(entry_list) == 0:
        print(
            "\nNo HTTP requests were recorded (HAR empty or filtered). Common causes:\n"
            "  • Press Enter only after you have loaded pages (https://) in this browser.\n"
            "  • --domains filtered out every request.\n"
            f"  • Expected HAR at {har_path!s}\n",
            file=sys.stderr,
        )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Network and script inspection tool (browser session)")
    ap.add_argument("--url", default=None, help="Output hostname only (default: derive from captured requests)")
    ap.add_argument("--out", type=Path, default=None, help="Output directory (default: output/<hostname>/)")
    ap.add_argument("--session-file", type=Path, default=SCRIPT_DIR / "session.json", help="Session state path")
    ap.add_argument("--domains", type=str, default=None, help="Comma-separated hosts to capture; omit to capture all")
    ap.add_argument("--no-session", action="store_true", help="Do not load/save session")
    ap.add_argument(
        "--user-data-dir",
        type=Path,
        default=None,
        help=(
            "Path to a Chrome user data directory for a persistent profile session. "
            "Enables launch_persistent_context, which gives reCAPTCHA/anti-bot a real browser "
            "fingerprint and browsing history. Must NOT be the default Chrome profile directory. "
            f"Create once with: cp -r ~/.config/google-chrome {CHROME_PROFILE_DIR}  "
            f"then pass: --user-data-dir {CHROME_PROFILE_DIR}"
        ),
    )
    ap.add_argument(
        "--cdp-debugger",
        action="store_true",
        help="Enable CDP Debugger.getScriptSource for eval/dynamic scripts (slow; can confuse Chrome on shutdown)",
    )
    args = ap.parse_args()
    domains = [d.strip() for d in args.domains.split(",")] if args.domains else None
    out_dir = args.out.resolve() if args.out else (_output_dir_from_url(args.url) if args.url else None)
    user_data_dir = args.user_data_dir.resolve() if args.user_data_dir else None
    main(
        out_dir=out_dir,
        state_file=args.session_file.resolve(),
        domains=domains,
        no_session=args.no_session,
        cdp_debugger=args.cdp_debugger,
        user_data_dir=user_data_dir,
    )
