"""Local smoke test for the complete browser-model request path."""
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright
from model_gateway import ModelGateway


def main():
    html = """<div contenteditable="true" data-lexical-editor="true"></div>
    <button aria-label="Send Message">Go</button>
    <div class="font-claude-response">{"content":"ready"}</div>"""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        editor = page.locator("[contenteditable='true']")
        editor.fill("test")
        assert editor.inner_text() == "test"
        assert page.locator(".font-claude-response").inner_text() == '{"content":"ready"}'
        browser.close()
    dynamic = """<div contenteditable="true" data-lexical-editor="true"></div>
    <button aria-label="Send Message">Go</button><div class="responses"></div>
    <script>document.querySelector('button').onclick=()=>document.querySelector('.responses').insertAdjacentHTML(
    'beforeend','<div class="assistant">{"content":"ready"}<span data-complete="true" style="display:block">&nbsp;</span></div>')</script>"""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = dynamic.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            provider = {"name": "smoke", "type": "playwright", "profile_dir": str(Path(directory) / "profile"),
                        "url": f"http://127.0.0.1:{server.server_port}/", "headless": True,
                        "input_selector": "[contenteditable='true']", "submit_selector": "button",
                        "response_selector": ".assistant", "completion_selector": "[data-complete='true']",
                        "structured_text": True, "stable_polls": 2, "stable_poll_seconds": 0.2, "timeout": 15}
            message, usage = ModelGateway({"providers": [provider]})._browser(provider, [{"role": "user", "content": "test"}], [])
            assert message == {"content": "ready"}
            assert usage["transport"] == "browser"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    print("playwright chromium smoke: ok")


if __name__ == "__main__":
    main()
