#!/usr/bin/env python3
import argparse
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


def _launch_browser(p):
    # Prefer real Chrome/Edge so UA and fingerprint match the binary; avoid automation flags.
    for channel in ("chrome", "msedge", None):
        try:
            b = p.chromium.launch(
                channel=channel,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=[
                    "--enable-automation",
                    "--disable-extensions",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-popup-blocking",
                ],
            )
            return b
        except Exception:
            continue
    raise RuntimeError("No usable browser. Run: patchright install chrome")


def _is_entry_page(url: str) -> bool:
    """Exclude the local entry.html from network capture."""
    return "entry.html" in (url or "")


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


def main(
    out_dir: Path | None,
    state_file: Path,
    domains: list[str] | None,
    no_session: bool,
):
    entries = []
    index_by_request = {}
    dynamic_scripts: list[dict] = []  # eval / new Function code (unused when using CDP)
    inline_scripts: list[dict] = []   # { "url": str, "code": str } per inline block
    cdp_parsed_scripts: list[dict] = []  # CDP Debugger.scriptParsed: scriptId, url, source
    load_state = not no_session and state_file.exists()
    final_page_url: str | None = None  # set after user presses Enter; used for output dir

    def on_request(request):
        if not should_capture(request.url, domains):
            return
        try:
            post_data = request.post_data
        except (UnicodeDecodeError, Exception):
            post_data = None  # binary body (e.g. gzip); Patchright decodes as utf-8
        req = {
            "url": request.url,
            "method": request.method,
            "headers": dict(request.headers) if request.headers else {},
            "post_data": post_data,
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
        # Don't override user_agent: real browser UA matches the binary and avoids Turnstile/detection.
        context_opts = dict(
            no_viewport=True,
            ignore_https_errors=True,
            locale="en-US",
        )
        if load_state:
            context_opts["storage_state"] = str(state_file)
        context = browser.new_context(**context_opts)
        page = context.new_page()
        page.on("request", on_request)
        page.on("response", on_response)

        # CDP: capture all parsed scripts (inline, eval, new Function, network)
        cdp_script_ids: list[dict] = []  # {scriptId, url} from scriptParsed; sources filled later

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
        final_page_url = page.url or None

        # Fetch CDP script sources (deferred to avoid calling send() from inside event callback)
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

        # Read inline scripts from DOM at save time (redundant with CDP but kept for compatibility)
        try:
            captured = page.evaluate("""
                (function() {
                    var inlineEls = Array.from(document.querySelectorAll('script:not([src])'));
                    var inline = inlineEls
                        .map(function(s) { return s.textContent || ''; })
                        .filter(function(c) { return c.trim().length > 0; });
                    return { url: location.href, inline: inline };
                })()
            """)
            if captured and isinstance(captured, dict):
                page_url = captured.get("url") or ""
                for code in captured.get("inline") or []:
                    if code.strip():
                        inline_scripts.append({"url": page_url, "code": code})
        except Exception:
            pass

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
