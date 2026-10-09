"""Render the demo video (docs/demo.mp4 + docs/demo.en.srt) from the real dashboard and the real CLI.

* Serves docs/ on a local port and drives it in headless Chromium (Playwright), capturing frames on a
  virtual 30 fps timeline, so motion is smooth regardless of machine speed.
* Terminal scenes show the real output of commands executed while recording (live public endpoints).
* English captions are burned in with ffmpeg (libx264 + libass); an .srt is written next to the video.
* No voice, no face.

    pip install -e ".[video]" && python -m playwright install chromium   # or reuse an existing install
    python scripts/record_demo.py [--baw path/to/@binance/agentic-wallet/dist/index.js]
"""
from __future__ import annotations

import argparse
import functools
import html
import http.server
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
FPS = 30
VIEW_W, VIEW_H, DPR = 1600, 775, 1.2          # -> 1920x930 frames
OUT_W, OUT_H = 1920, 1080                      # 150 px caption band under the app
BG = "0a0e15"
READ_CPS = 17                                  # caption reading speed, characters per second
REPO = "github.com/Minkhanov/weekend-gap-guard"


def find_ffmpeg() -> str:
    if os.environ.get("FFMPEG"):
        return os.environ["FFMPEG"]
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("ffmpeg not found: install it, set FFMPEG, or pip install imageio-ffmpeg")


def run(args: list[str], cwd: Path = ROOT) -> tuple[int, str, str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)
    return p.returncode, p.stdout, p.stderr


def ts(t: float, sep: str) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    if sep == ".":
        return f"{int(h)}:{int(m):02d}:{s:05.2f}"
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}"


def serve(directory: Path) -> tuple[socketserver.TCPServer, int]:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    handler = functools.partial(Quiet, directory=str(directory))
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


CURSOR_JS = r"""
(() => {
  if (window.__cur) return;
  const c = document.createElement('div');
  c.innerHTML = '<svg width="30" height="30" viewBox="0 0 28 28"><path d="M3 2 L3 22 L8.5 17 L12.5 26 L16 24.5 L12 15.8 L19.5 15.8 Z" fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  Object.assign(c.style, {position: 'fixed', left: '-3px', top: '-2px', zIndex: 2147483647, pointerEvents: 'none',
    filter: 'drop-shadow(0 2px 3px rgba(0,0,0,.55))', transform: 'translate(-100px,-100px)'});
  const r = document.createElement('div');
  Object.assign(r.style, {position: 'fixed', width: '44px', height: '44px', marginLeft: '-22px', marginTop: '-22px',
    borderRadius: '50%', border: '3px solid #f0b90b', opacity: '0', zIndex: 2147483646, pointerEvents: 'none'});
  document.body.append(r, c);
  window.__cur = (x, y) => { c.style.transform = `translate(${x}px,${y}px)`; };
  window.__rip = (x, y, t) => { r.style.left = x + 'px'; r.style.top = y + 'px';
    r.style.opacity = String(t >= 1 ? 0 : 1 - t); r.style.transform = `scale(${0.4 + 1.1 * t})`; };
  document.documentElement.style.scrollBehavior = 'auto';
})();
"""


class Recorder:
    def __init__(self, frames_dir: Path):
        self.dir = frames_dir
        self.entries: list[list] = []
        self.captions: list[list] = []
        self.page = None
        self.pos: dict = {}
        self.n = 0

    @property
    def frame(self) -> int:
        return sum(e[1] for e in self.entries)

    def use(self, page):
        self.page = page
        page.bring_to_front()
        self.pos.setdefault(id(page), (VIEW_W * 0.55, VIEW_H * 0.55))

    def snap(self):
        self.n += 1
        p = self.dir / f"f{self.n:05d}.png"
        self.page.screenshot(path=str(p))
        self.entries.append([p, 1])

    def hold(self, sec: float):
        if not self.entries:
            self.snap()
        self.entries[-1][1] += max(1, round(sec * FPS))

    def cap(self, text: str | None):
        if self.captions and self.captions[-1][1] is None:
            start, _, prev = self.captions[-1]
            need = round(max(3.0, len(prev) / READ_CPS) * FPS) - (self.frame - start)
            if need > 0:
                self.hold(need / FPS)
            self.captions[-1][1] = self.frame
        if text:
            self.captions.append([self.frame, None, text])

    def _cursor(self, x, y):
        self.page.evaluate("([x, y]) => window.__cur && window.__cur(x, y)", [x, y])
        self.pos[id(self.page)] = (x, y)

    def move(self, x: float, y: float, dur: float = 0.6):
        x0, y0 = self.pos[id(self.page)]
        steps = max(1, round(dur * FPS))
        for i in range(1, steps + 1):
            t = i / steps
            e = t * t * (3 - 2 * t)
            self._cursor(x0 + (x - x0) * e, y0 + (y - y0) * e)
            self.snap()

    def scroll_to(self, y: float, dur: float = 0.8):
        y0 = self.page.evaluate("window.scrollY")
        steps = max(1, round(dur * FPS))
        for i in range(1, steps + 1):
            t = i / steps
            e = t * t * (3 - 2 * t)
            self.page.evaluate("y => window.scrollTo(0, y)", y0 + (y - y0) * e)
            self.snap()

    def show(self, loc, top: float = 90, dur: float = 0.8):
        """Scroll so that the element's top sits `top` px below the viewport top."""
        cur = self.page.evaluate("window.scrollY")
        b = loc.bounding_box()
        self.scroll_to(max(0, cur + b["y"] - top), dur)

    def click(self, loc, dur: float = 0.6, settle: float = 0.3):
        b = loc.bounding_box()
        if b["y"] < 60 or b["y"] + b["height"] > VIEW_H - 20:
            self.show(loc, VIEW_H * 0.35)
            b = loc.bounding_box()
        x, y = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
        self.move(x, y, dur)
        for t in (0.15, 0.35):
            self.page.evaluate("([x, y, t]) => window.__rip(x, y, t)", [x, y, t])
            self.snap()
        loc.click()
        self.page.wait_for_timeout(120)
        for t in (0.6, 0.85, 1.0):
            self.page.evaluate("([x, y, t]) => window.__rip(x, y, t)", [x, y, t])
            self.snap()
        self.hold(settle)

    def hover(self, loc, fx: float = 0.5, fy: float = 0.5, dur: float = 0.6):
        b = loc.bounding_box()
        self.move(b["x"] + b["width"] * fx, b["y"] + b["height"] * fy, dur)


STUDIO = """<!doctype html><html><head><meta charset="utf-8"><style>
:root{--bg:#0a0e15;--line:#1e2735;--text:#e6e9ef;--muted:#8b97a8;--accent:#f0b90b;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:radial-gradient(1200px 600px at 80% -10%,#2a2410 0%,var(--bg) 55%);color:var(--text);font:18px/1.5 "Segoe UI",Inter,system-ui,sans-serif;overflow:hidden}
.scene{position:absolute;inset:0;display:none;padding:48px 70px}.scene.on{display:flex}
#title{flex-direction:column;justify-content:center;align-items:center;text-align:center;gap:22px}
#title h1{font-size:74px;margin:0;letter-spacing:-.03em}#title p{margin:0;font-size:29px;color:#c9d2e0;max-width:1200px}
.mark{width:96px;height:96px}
.badges{display:flex;gap:12px;flex-wrap:wrap;justify-content:center}.badge{font-size:17px;padding:6px 14px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
.badge.y{color:var(--accent);border-color:#5b4a10}
#card{flex-direction:column;justify-content:center;align-items:center;gap:24px}
#card h2{margin:0;font-size:46px;letter-spacing:-.02em}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;max-width:1460px}
.grid3 div{padding:16px 18px;border:1px solid var(--line);border-radius:14px;background:#0f141dcc;font-size:21px}
.grid3 div small{display:block;color:var(--muted);font-size:16px;margin-top:4px}
.link{font:24px Consolas,monospace;color:var(--accent)}.fine{color:var(--muted);font-size:17px}
#term{align-items:center;justify-content:center}
.win{width:1480px;height:690px;border:1px solid var(--line);border-radius:14px;background:#06080c;box-shadow:0 30px 80px #000a;display:flex;flex-direction:column;overflow:hidden}
.bar{height:38px;display:flex;align-items:center;gap:8px;padding:0 16px;background:#111723;border-bottom:1px solid var(--line);color:var(--muted);font-size:15px}
.dot{width:12px;height:12px;border-radius:50%}.bar .t{margin-left:14px}
pre#tb{margin:0;flex:1;padding:16px 20px;font:15px/1.5 Consolas,"Cascadia Mono",ui-monospace,monospace;color:#c9d2e0;white-space:pre-wrap;overflow:hidden}
.p{color:var(--accent)}.c{color:#fff}.o{color:#b8c2d1}.k{color:var(--ok)}.e{color:var(--warn)}
.cursor{display:inline-block;width:9px;height:18px;background:#c9d2e0;vertical-align:-3px}
</style></head><body>
<section id="title" class="scene"></section><section id="card" class="scene"></section>
<section id="term" class="scene"><div class="win"><div class="bar"><span class="dot" style="background:#ff5f57"></span><span class="dot" style="background:#febc2e"></span><span class="dot" style="background:#28c840"></span><span class="t">weekend-gap-guard</span></div><pre id="tb"></pre></div></section>
<script>
function show(id, html){document.querySelectorAll('.scene').forEach(s=>s.classList.toggle('on', s.id===id)); if(html!==undefined) document.getElementById(id).innerHTML=html;}
function term(segs, cursor){const tb=document.getElementById('tb'); tb.replaceChildren();
  for(const [cls,text] of segs){const s=document.createElement('span'); s.className=cls; s.textContent=text; tb.append(s);}
  if(cursor){const c=document.createElement('span'); c.className='cursor'; tb.append(c);} tb.scrollTop=tb.scrollHeight;}
</script></body></html>"""

MARK = ('<svg class="mark" viewBox="0 0 32 32"><path d="M16 2 4 7v8c0 7.5 5.1 13.4 12 15 6.9-1.6 12-7.5 12-15V7L16 2Z" '
        'fill="none" stroke="#e6e9ef" stroke-width="1.8" stroke-linejoin="round"/><path d="M9 18h5l2-6 2 9 2-3h3" fill="none" '
        'stroke="#f0b90b" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>')


class Terminal:
    def __init__(self, rec: Recorder, page):
        self.rec, self.page, self.segs = rec, page, []

    def clear(self):
        self.segs = []
        self.page.evaluate("([s, c]) => term(s, c)", [self.segs, True])

    def command(self, shown: str, out: str = "", cps: int = 75, style: str = "o"):
        self.segs += [["p", "$ "], ["c", ""]]
        per = max(1, round(cps / FPS))
        for i in range(0, len(shown), per):
            self.segs[-1][1] = shown[: i + per]
            self.page.evaluate("([s, c]) => term(s, c)", [self.segs, True])
            self.rec.snap()
        self.segs[-1][1] = shown + "\n"
        self.rec.hold(0.4)
        lines = out.rstrip("\n").splitlines()
        for i in range(0, len(lines), 3):
            for ln in lines[i:i + 3]:
                self.segs.append([style, ln + "\n"])
            self.page.evaluate("([s, c]) => term(s, c)", [self.segs, True])
            self.rec.snap()


def film(rec: Recorder, app, studio, baw_js: str | None, py: str):
    q = app.locator
    T = Terminal(rec, studio)

    # 0. title
    rec.use(studio)
    studio.evaluate("h => show('title', h)", (
        f'{MARK}<h1>Weekend Gap Guard</h1>'
        '<p>A pre-trade risk gate for tokenized stocks on BNB Chain: what to allow, cut or block while the stock market is closed.</p>'
        '<div class="badges"><span class="badge y">Ondo · xStocks · bStocks on BSC</span>'
        '<span class="badge">public Binance Wallet endpoints, no API key</span>'
        '<span class="badge">ALLOW · REDUCE · BLOCK, every check explained</span></div>'))
    rec.snap()
    rec.cap("Weekend Gap Guard: a pre-trade risk gate for tokenized stocks on BNB Chain.")
    rec.hold(4)

    # 1. the problem, on the dashboard hero
    rec.cap(None)
    rec.use(app)
    rec.snap()
    rec.cap("From Friday 16:00 to Monday 09:30 New York time the stock market prints nothing. "
            "Ondo, xStocks and bStocks tokens keep trading on BSC.")
    rec.hover(q("h1"), 0.6, 0.7, 0.8)
    rec.hold(2.5)
    rec.cap("A stop-loss cannot fill at a price the market never prints. The guard answers every order "
            "with ALLOW, REDUCE or BLOCK, and says why.")
    rec.hover(q(".lede .pill.reduce"), dur=0.7)
    rec.hold(2.5)
    rec.cap("All numbers come from real BSC data pulled from the public Binance Wallet endpoints, with no API key.")
    rec.hover(q("#kpis .kpi").nth(0), 0.4, 0.4, 0.7)
    rec.hold(2.0)

    # 2. replay TSLA weekend
    rec.cap(None)
    rec.show(q("#scenario-blurb"), 70, 1.0)
    rec.cap("A real weekend, hour by hour: TSLA from all three issuers. The dashed line is Friday's close, "
            "the frozen reference; red and amber strips are the guard's verdicts.")
    chart = q("#chart")
    for fx in (0.08, 0.3, 0.55, 0.8):
        rec.hover(chart, fx, 0.45, 0.7)
        rec.hold(0.5)
    rec.show(q("#decision"), 70, 0.9)
    mom = q("#moments button")
    rec.cap("Friday, market open: the stop can fill, so a 2,000 dollar buy is allowed.")
    rec.click(mom.nth(0))
    rec.hold(2.2)
    rec.cap("Last hour before the close: you are about to carry weekend risk. The cap is halved.")
    rec.click(mom.nth(1))
    rec.hold(2.2)
    rec.cap("Saturday: risk is priced on the weekend gap, 9.6% here, not on the 3% stop. 2,000 becomes 875.")
    rec.click(mom.nth(2))
    rec.hover(q(".checks li").nth(4), 0.3, 0.5, 0.7)
    rec.hold(2.5)
    rec.cap("The xStocks pool for TSLA on BSC had not traded for hours: no live mark, so the guard blocks it.")
    rec.click(q("#issuer-tabs button").nth(1))
    rec.hold(2.5)

    # 3. MSTR: drift
    rec.cap(None)
    rec.show(q("#scenario-tabs"), 120, 0.8)
    rec.cap("MSTR, the largest Monday gap in our record: +8.9%. The token climbs into Monday's pre-open...")
    rec.click(q("#scenario-tabs button").nth(1))
    rec.hover(chart, 0.82, 0.3, 0.8)
    rec.hold(1.5)
    rec.show(q("#decision"), 70, 0.8)
    rec.cap("...and once the weekend drift passes one sigma of a weekend move, the cap is halved again: 625 dollars.")
    rec.click(q("#moments button").nth(4))
    rec.hold(3)

    # 4. live board
    rec.cap(None)
    rec.show(q("#live"), 70, 1.0)
    rec.cap("The live board, recorded from the public endpoints: Ondo and bStocks agree within 0.15%. "
            "Most xStocks pools on BSC are stale or up to 20% off the stock, and are blocked.")
    rec.hover(q("#board tr").nth(5), 0.55, 0.5, 0.8)
    rec.hold(3)

    # 5. evidence
    rec.cap(None)
    rec.show(q("#evidence"), 70, 1.0)
    rec.cap("Out of sample, no Monday gap in 540 ticker-weekends exceeded the guard's 99% estimate.")
    rec.hover(q("#calib"), 0.5, 0.4, 0.8)
    rec.hold(2)
    rec.cap("Saturday buys of 5,000 dollars over 129 real weekends: worst Monday loss minus 218 dollars raw, "
            "minus 38 guarded.")
    rec.hover(q("#stress"), 0.5, 0.45, 0.8)
    rec.hold(2.5)

    # 6. terminal: live CLI + Agentic Wallet hand-off + tests
    rec.cap(None)
    rec.use(studio)
    studio.evaluate("() => show('term')")
    T.clear()
    rec.snap()
    rec.cap("The same engine runs as a CLI and as an Agent Skill. This is a live call to the public endpoints.")
    rc, out, err = run([py, "-m", "gapguard", "check", "TSLAB", "--usd", "2000", "--wallet-plan"])
    if rc != 0:
        raise SystemExit(f"live check failed while recording: {err[-400:]}")
    T.command("gapguard check TSLAB --usd 2000 --wallet-plan", out)
    rec.hold(4)
    rec.cap("It hands the allowed size to the Binance Agentic Wallet as a quote, with a slippage cap.")
    rec.hold(2)
    if baw_js:
        quote = next(ln.strip() for ln in out.splitlines() if ln.strip().startswith("baw market-order quote"))
        rc2, out2, _ = run(["node", baw_js, *quote.split()[1:]])
        if '"NOT_LOGGED_IN"' not in out2:
            raise SystemExit("baw did not stop at NOT_LOGGED_IN — check the command syntax before publishing")
        T.clear()
        rec.cap("Checked against the real baw 1.10.0 CLI: the command parses and stops at sign-in. "
                "Signing needs a Binance account, so the guard never executes.")
        T.command(quote.replace("--fromToken ", "\\\n    --fromToken "), out2, style="e")
        rec.hold(3.5)
    T.clear()
    rec.cap("Everything is tested offline.")
    rc3, out3, _ = run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider"])
    if rc3:
        raise SystemExit("tests failed while recording")
    T.command("python -m pytest -q", "\n".join(ln for ln in out3.splitlines() if ln.strip()), style="k")
    rec.hold(2.5)

    # 7. closing card
    rec.cap(None)
    studio.evaluate("h => show('card', h)", (
        f'<div style="display:flex;gap:22px;align-items:center">{MARK}<h2 style="font-size:56px">Weekend Gap Guard</h2></div>'
        '<div class="grid3">'
        '<div>Priced on the gap, not the stop<small>99 % weekend-gap quantile while the market is shut</small></div>'
        '<div>Stale-reference signals<small>reference age, weekend drift, cross-issuer median, live premium</small></div>'
        '<div>Account guards<small>static floor, daily loss limit, 40 % consistency rule</small></div>'
        '<div>Fail closed<small>no price, stale pool, paused venue: BLOCK</small></div>'
        '<div>Agentic Wallet hand-off<small>baw quote with the allowed size; never signs</small></div>'
        '<div>Agent Skill + CLI + dashboard<small>real BSC data, public endpoints, MIT</small></div></div>'
        f'<div class="link">{html.escape(REPO)}</div>'
        '<div class="fine">A risk limit, not investment advice. Demo account and replays use recorded BSC data.</div>'))
    rec.snap()
    rec.cap("Weekend Gap Guard: size tokenized-stock trades for the market that is actually open.")
    rec.hold(5)
    rec.cap(None)


def write_subs(captions, ass_path: Path, srt_path: Path):
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 0\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n"
            "Style: Cap,Segoe UI,34,&H00EFE9E6,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,2,140,140,36,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ass, srt = [head], []
    for i, (a, b, text) in enumerate(captions, 1):
        ta, tb = a / FPS, b / FPS
        ass.append(f"Dialogue: 0,{ts(ta, '.')},{ts(tb, '.')},Cap,,0,0,0,,{text}\n")
        srt.append(f"{i}\n{ts(ta, ',')} --> {ts(tb, ',')}\n{text}\n")
    ass_path.write_text("".join(ass), encoding="utf-8")
    srt_path.write_text("\n".join(srt), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "demo.mp4"))
    ap.add_argument("--baw", default=os.environ.get("GAPGUARD_BAW_JS"),
                    help="path to @binance/agentic-wallet/dist/index.js to show the real CLI check")
    ap.add_argument("--py", default=str(next((p for p in (ROOT / ".venv" / "Scripts" / "python.exe",
                                                             ROOT / ".venv" / "bin" / "python") if p.exists()),
                                                  Path(sys.executable))),
                    help="interpreter that has gapguard's dependencies (default: ./.venv)")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    ffmpeg = find_ffmpeg()
    work = Path(tempfile.mkdtemp(prefix="gapguard-demo-"))
    frames = work / "frames"
    frames.mkdir()
    srv, port = serve(ROOT / "docs")
    rec = Recorder(frames)
    errors: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": VIEW_W, "height": VIEW_H}, device_scale_factor=DPR,
                                      color_scheme="dark")
            app = ctx.new_page()
            app.on("pageerror", lambda e: errors.append(str(e)))
            app.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            app.wait_for_selector("#decision-body .verdict")
            app.evaluate(CURSOR_JS)
            studio_file = work / "studio.html"
            studio_file.write_text(STUDIO, encoding="utf-8")
            studio = ctx.new_page()
            studio.goto(studio_file.as_uri())
            studio.evaluate(CURSOR_JS)
            film(rec, app, studio, a.baw, a.py)
            browser.close()
    finally:
        srv.shutdown()
    if errors:
        raise SystemExit(f"page errors while recording: {errors}")

    fonts = work / "fonts"
    fonts.mkdir()
    for name in ("segoeui.ttf", "segoeuib.ttf"):
        src = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if src.exists():
            shutil.copy(src, fonts / name)
    out = Path(a.out)
    write_subs(rec.captions, frames / "subs.ass", out.with_suffix(".en.srt"))
    vf = (f"pad={OUT_W}:{OUT_H}:0:0:color=0x{BG},"
          f"drawbox=x=0:y={int(VIEW_H * DPR)}:w={OUT_W}:h=1:color=0x1e2735:t=fill,"
          f"ass=subs.ass:fontsdir=../fonts,format=yuv420p")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", str(FPS),
           "-c:v", "png", "-i", "-", "-vf", vf, "-c:v", "libx264", "-preset", "slow", "-crf", "20",
           "-tune", "stillimage", "-movflags", "+faststart", str(out.resolve())]
    proc = subprocess.Popen(cmd, cwd=frames, stdin=subprocess.PIPE)
    for path, n in rec.entries:
        data = path.read_bytes()
        for _ in range(n):
            proc.stdin.write(data)
    proc.stdin.close()
    if proc.wait():
        raise SystemExit("ffmpeg failed")
    print(f"wrote {out} ({rec.frame / FPS:.1f} s, {len(rec.entries)} unique frames) and {out.with_suffix('.en.srt')}")
    if a.keep:
        print("frames kept in", frames)
    else:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
