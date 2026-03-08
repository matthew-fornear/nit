#!/usr/bin/env python3
import argparse
import json
import platform
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


def _safe_host(host: str) -> str:
    return re.sub(r"[^\w.-]", "_", host).strip("._") or "unknown"


def _output_dir_from_url(url: str) -> Path:
    host = urlparse(url).hostname or "unknown"
    return OUTPUT_DIR / _safe_host(host)


def _output_dir_from_entries(entry_list: list) -> Path:
    from collections import Counter
    hosts: Counter[str] = Counter()
    for e in entry_list:
        url = e.get("url") or ""
        try:
            host = urlparse(url).hostname or ""
            if host and not host.startswith(".") and host not in ("localhost", "127.0.0.1"):
                hosts[host] += 1
        except Exception:
            pass
    if not hosts:
        name = "capture"
    else:
        name = _safe_host(hosts.most_common(1)[0][0])
    return OUTPUT_DIR / name


def _user_agent() -> str:
    if platform.system() == "Linux":
        return (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )


def _launch_browser(p):
    for channel in ("chrome", "msedge", None):
        try:
            b = p.chromium.launch(
                channel=channel,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
            return b
        except Exception:
            continue
    raise RuntimeError("No usable browser. Run: patchright install chrome")


def should_capture(url: str, domains: list[str] | None) -> bool:
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


def main(
    out_dir: Path | None,
    state_file: Path,
    domains: list[str] | None,
    no_session: bool,
):
    entries = []
    index_by_request = {}
    load_state = not no_session and state_file.exists()

    def on_request(request):
        if not should_capture(request.url, domains):
            return
        req = {
            "url": request.url,
            "method": request.method,
            "headers": dict(request.headers) if request.headers else {},
            "post_data": request.post_data,
        }
        idx = len(entries)
        entries.append({"request": req, "response": None})
        index_by_request[id(request)] = idx

    def on_response(response):
        request = response.request
        idx = index_by_request.get(id(request))
        if idx is None:
            return
        content_type = (response.headers.get("content-type") or "")
        body = None
        if should_capture_body(request.url, content_type):
            try:
                raw = response.body()
                body = raw.decode("utf-8", errors="replace")
            except BaseException:
                pass
        entries[idx]["response"] = {
            "status": response.status,
            "status_text": response.status_text,
            "headers": dict(response.headers) if response.headers else {},
            "body": body,
        }

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context_opts = dict(
            no_viewport=True,
            user_agent=_user_agent(),
            ignore_https_errors=True,
        )
        if load_state:
            context_opts["storage_state"] = str(state_file)
        context = browser.new_context(**context_opts)
        page = context.new_page()
        page.on("request", on_request)
        page.on("response", on_response)

        entry_html = SCRIPT_DIR / "entry.html"
        if entry_html.exists():
            page.goto(entry_html.as_uri(), wait_until="domcontentloaded", timeout=10000)
        else:
            page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)

        input("Press Enter to save capture and session, then close... ")
        time.sleep(2)
        try:
            context.storage_state(path=str(state_file))
        except Exception as e:
            print("Warning: could not save session:", e, file=sys.stderr)
        browser.close()

    from datetime import datetime, timezone
    meta = {
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "total_entries": len(entries),
    }
    entry_list = [
        {
            "url": e["request"]["url"],
            "method": e["request"]["method"],
            "request_headers": e["request"]["headers"],
            "request_post_data": e["request"]["post_data"],
            "response": (
                {
                    "status": e["response"]["status"],
                    "status_text": e["response"]["status_text"],
                    "response_headers": e["response"]["headers"],
                    "body": e["response"]["body"],
                }
                if e["response"] is not None
                else None
            ),
        }
        for e in entries if e.get("request")
    ]
    if out_dir is None:
        out_dir = _output_dir_from_entries(entry_list)
    out_dir.mkdir(parents=True, exist_ok=True)
    script_entries = [e for e in entry_list if _is_script_entry(e)]

    networkcalls_path = out_dir / "networkcalls.json"
    scripts_path = out_dir / "scripts.json"
    networkcalls_path.write_text(
        json.dumps({**meta, "entries": entry_list}, indent=2), encoding="utf-8"
    )
    scripts_path.write_text(
        json.dumps({**meta, "total_scripts": len(script_entries), "entries": script_entries}, indent=2),
        encoding="utf-8",
    )
    print("Saved", len(entry_list), "requests to", networkcalls_path)
    print("Saved", len(script_entries), "scripts to", scripts_path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Network and script inspection tool (browser session)")
    ap.add_argument("--url", default=None, help="Output hostname only (default: derive from captured requests)")
    ap.add_argument("--out", type=Path, default=None, help="Output directory (default: output/<hostname>/)")
    ap.add_argument("--session-file", type=Path, default=SCRIPT_DIR / "session.json", help="Session state path")
    ap.add_argument("--domains", type=str, default=None, help="Comma-separated hosts to capture; omit to capture all")
    ap.add_argument("--no-session", action="store_true", help="Do not load/save session")
    args = ap.parse_args()
    domains = [d.strip() for d in args.domains.split(",")] if args.domains else None
    out_dir = args.out.resolve() if args.out else (_output_dir_from_url(args.url) if args.url else None)
    main(
        out_dir=out_dir,
        state_file=args.session_file.resolve(),
        domains=domains,
        no_session=args.no_session,
    )
