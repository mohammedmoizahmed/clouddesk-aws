"""The attachment pop-up viewer, driven in a real browser (Chromium) at desktop and phone sizes.

Skipped automatically if Playwright or its browser is not installed:
    pip install -r requirements-dev.txt && python -m playwright install chromium
"""
import base64
import struct
import threading
import zlib

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

VIEWPORTS = {
    "desktop": dict(viewport={"width": 1440, "height": 900}),
    "phone": dict(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True),
}


def make_png(width=400, height=260):
    """A small gradient image, built without any imaging library."""
    rows = b"".join(
        b"\x00" + b"".join(bytes((x * 255 // width, y * 255 // height, 160)) for x in range(width))
        for y in range(height))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        try:
            chromium = playwright.chromium.launch()
        except Exception as exc:  # browser binaries not installed
            pytest.skip(f"Chromium is not available: {exc}")
        yield chromium
        chromium.close()


@pytest.fixture
def live_server(clouddesk, aws):
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, clouddesk.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.mark.parametrize("device", list(VIEWPORTS))
def test_image_pop_up_opens_and_every_way_of_closing_works(browser, live_server, site, aws, carol, alice, device):
    key = "ticket-1/printer.png"
    site.add_ticket("Printer chip error", alice.id, attachment=key)
    aws.table.set(key, "accepted")
    aws.s3.link_override = "data:image/png;base64," + base64.b64encode(make_png()).decode()

    context = browser.new_context(**VIEWPORTS[device])
    page = context.new_page()
    downloads = []
    page.on("download", lambda download: downloads.append(download))

    page.goto(live_server + "/login")
    page.fill("#email", carol.email)
    page.fill("#password", carol.password)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(live_server + "/incidents/1")
    page.wait_for_load_state("networkidle")
    address = page.url
    viewer = page.locator("#viewer")

    assert not viewer.is_visible()
    assert page.evaluate("document.querySelector('.thumb img').naturalWidth") > 0

    page.click(".thumb")
    page.wait_for_timeout(250)
    assert viewer.is_visible()
    assert page.evaluate("document.getElementById('viewer-img').naturalWidth") == 400
    assert "printer.png" in page.inner_text("#viewer-name")

    page.click("#viewer-close")
    page.wait_for_timeout(150)
    assert not viewer.is_visible() and page.url == address

    page.click("text=View image")
    page.wait_for_timeout(150)
    assert viewer.is_visible()
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    assert not viewer.is_visible()

    page.click(".thumb")
    page.wait_for_timeout(150)
    page.click("#viewer-back")
    page.wait_for_timeout(150)
    assert not viewer.is_visible()

    page.click(".thumb")
    page.wait_for_timeout(150)
    page.mouse.click(5, 5)
    page.wait_for_timeout(150)
    assert not viewer.is_visible()
    assert page.evaluate("!document.getElementById('viewer-img').getAttribute('src')")

    assert downloads == [], "viewing an image must never download it"
    context.close()
