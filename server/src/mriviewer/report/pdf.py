"""Server-side PDF: headless Chromium renders the print view of our own page.

The PDF is the print view, exactly. There is no second renderer to drift from
the web page; what the doctor approved on screen is what prints.

Playwright's *sync* API is used from a plain (non-async) FastAPI handler,
which FastAPI runs in a worker thread - the sync API refuses to run on the
event-loop thread. A file lock serialises renders across the uvicorn
workers; one Chromium at a time is plenty at this scale and keeps memory
bounded.

On an air-gapped host the browser and its shared libraries come from the
install bundle (see packaging/build_bundle.sh); nothing is downloaded here.
"""
from __future__ import annotations

import os
import shutil
import threading
from contextlib import contextmanager
from html import escape
from pathlib import Path
from typing import Any

PDF_TIMEOUT_S = 90
_THREAD_LOCK = threading.Lock()

CJK_FONT_STACK = '"Noto Sans CJK SC","Source Han Sans SC","AR PL UMing CN","WenQuanYi Micro Hei",sans-serif'


def pdf_available(cfg: Any) -> dict[str, Any]:
    """Can this host render a PDF? Detail is meant for a person, in Chinese."""
    out: dict[str, Any] = {"available": False, "playwright": False, "chromium": None,
                           "fonts": [], "detail": None}
    try:
        import playwright  # noqa: F401
        out["playwright"] = True
    except ImportError:
        out["detail"] = "服务器未安装 playwright（pip install playwright）"
        return out

    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or getattr(cfg.pdf, "browsers_path", None)
    if browsers:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    # Only a real launch proves the browser is usable: headless mode uses the
    # separate "chromium-headless-shell" build, and executable_path reports
    # the full Chromium path whether or not either one is installed.
    try:
        from playwright.sync_api import sync_playwright
        args = ["--no-sandbox"] if getattr(cfg.pdf, "no_sandbox", False) else []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=args)
            out["chromium"] = browser.version
            browser.close()
    except Exception as exc:                      # noqa: BLE001
        msg = str(exc)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            out["detail"] = "未安装 Chromium（在联网构建机上运行 playwright install chromium-headless-shell 并随离线包分发，或检查 PLAYWRIGHT_BROWSERS_PATH）"
        elif "sandbox" in msg.lower() or "namespace" in msg.lower():
            out["detail"] = "Chromium 沙箱启动失败；内核可能禁用了非特权用户命名空间，可在 [pdf] 中设置 no_sandbox = true"
        else:
            out["detail"] = "Chromium 启动失败：%s" % msg[:200]
        return out

    fc = shutil.which("fc-list")
    if fc:
        try:
            import subprocess
            res = subprocess.run([fc, ":lang=zh", "family"], capture_output=True, text=True, timeout=10)
            fams = sorted({f.strip() for f in res.stdout.splitlines() if f.strip()})
            out["fonts"] = fams[:8]
            if not fams:
                out["detail"] = "系统没有中文字体（安装 fonts-noto-cjk）"
                return out
        except Exception:                          # noqa: BLE001
            pass
    out["available"] = True
    return out


@contextmanager
def _file_lock(path: Path):
    """Cross-process lock. fcntl on Linux; a no-op elsewhere (dev boxes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        fh.close()


def _header_html(meta: dict[str, Any]) -> str:
    parts = [meta.get("titleZh") or "膝关节 MRI 结构化报告"]
    for k in ("lateralityZh", "sexZh", "ageBand"):
        if meta.get(k):
            parts.append(str(meta[k]))
    if meta.get("caseLabel"):
        parts.append("病例 %s" % meta["caseLabel"])
    if meta.get("generatedAt"):
        parts.append(str(meta["generatedAt"])[:10])
    text = " · ".join(escape(str(x)) for x in parts)
    return ('<div style="width:100%%;font-size:9px;color:#666;padding:0 14mm;'
            'font-family:%s;display:flex;justify-content:space-between">'
            '<span>%s</span><span>第 <span class="pageNumber"></span> / '
            '<span class="totalPages"></span> 页</span></div>' % (CJK_FONT_STACK, text))


def _footer_html(disclaimer: str) -> str:
    return ('<div style="width:100%%;font-size:8px;color:#777;padding:0 14mm;'
            'font-family:%s">%s</div>' % (CJK_FONT_STACK, escape(disclaimer or "")))


def render_report_pdf(cfg: Any, seg_id: int, meta: dict[str, Any], disclaimer: str) -> bytes:
    """Render ``#/report/<seg_id>?view=print&pdf=1`` to A4 PDF bytes."""
    from playwright.sync_api import sync_playwright

    browsers = getattr(cfg.pdf, "browsers_path", None)
    if browsers:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    base = getattr(cfg.pdf, "base_url", None) or "http://127.0.0.1:%d" % cfg.bind_port
    url = "%s/#/report/%d?view=print&pdf=1" % (base.rstrip("/"), seg_id)
    timeout_ms = int(getattr(cfg.pdf, "timeout_s", PDF_TIMEOUT_S) * 1000)
    args = ["--no-sandbox"] if getattr(cfg.pdf, "no_sandbox", False) else []

    with _THREAD_LOCK, _file_lock(Path(cfg.state_dir) / "pdf.lock"):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=args)
            try:
                ctx = browser.new_context(locale="zh-CN", viewport={"width": 1000, "height": 1400})
                page = ctx.new_page()
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                page.wait_for_selector("[data-report-ready='1']", timeout=timeout_ms)
                page.emulate_media(media="print")
                pdf = page.pdf(
                    format="A4", print_background=True, prefer_css_page_size=True,
                    display_header_footer=True,
                    margin={"top": "18mm", "bottom": "18mm", "left": "14mm", "right": "14mm"},
                    header_template=_header_html(meta),
                    footer_template=_footer_html(disclaimer),
                )
            finally:
                browser.close()
    return pdf


def selftest_pdf(cfg: Any, out_dir: Path) -> dict[str, Any]:
    """Render a fixed CJK sample to PDF and PNG for a human glyph check.

    Tofu boxes are drawn without error, so a machine cannot tell a missing
    font from a present one; a person looking at the PNG can.
    """
    from playwright.sync_api import sync_playwright

    browsers = getattr(cfg.pdf, "browsers_path", None)
    if browsers:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html = ('<html><body style="font-family:%s;font-size:16px;padding:20px">'
            '<h2>PDF 自检：膝关节 MRI 结构化报告</h2>'
            '<p>软骨厚度 2.22 mm，内外侧不对称度 −12.8%%，Outerbridge II 级。</p>'
            '<p>如果这段中文显示为方块，说明缺少 CJK 字体。</p></body></html>' % CJK_FONT_STACK)
    args = ["--no-sandbox"] if getattr(cfg.pdf, "no_sandbox", False) else []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=args)
        try:
            page = browser.new_page()
            page.set_content(html)
            page.screenshot(path=str(out_dir / "pdf-selftest.png"))
            pdf = page.pdf(format="A4", print_background=True)
            (out_dir / "pdf-selftest.pdf").write_bytes(pdf)
        finally:
            browser.close()
    return {"png": str(out_dir / "pdf-selftest.png"), "pdf": str(out_dir / "pdf-selftest.pdf"),
            "bytes": len(pdf)}
