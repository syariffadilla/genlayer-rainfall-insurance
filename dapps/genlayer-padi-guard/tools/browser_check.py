"""Headless-Chromium check of the built frontend against the LIVE Studionet contract.

    cd frontend && npm run build && cd ..
    pip install playwright && python -m playwright install chromium
    python tools/browser_check.py read                      # read-only: overview + policy page
    python tools/browser_check.py farmer <burner_key>       # faucet button + farmer tab
    python tools/browser_check.py quote  <burner_key> 300   # request_quote via the UI (drought < 300 mm)
    python tools/browser_check.py buy    <burner_key>       # buy the latest quote via the UI
    python tools/browser_check.py pool   <burner_key>       # open a second insurer pool via the UI
    python tools/browser_check.py fund   <burner_key>       # fund that pool with 1 GEN (prompt dialog)
    python tools/browser_check.py insurer-view <burner_key> # "My pools" view
    python tools/browser_check.py mine   <burner_key>       # farmer's quotes and policies
    python tools/browser_check.py settle <burner_key>       # "try settle anyway" on policy #2 (expected revert before the window)

<burner_key> is a throwaway Studionet private key injected as the app's burner account.
Never use a key that holds real funds. Screenshots go to docs/screenshots/.
If the machine reaches the internet through HTTPS_PROXY, the proxy is passed to Chromium.
"""
import sys, threading, functools, http.server, socketserver, time, json
from playwright.sync_api import sync_playwright
DIST=str(__import__("pathlib").Path(__file__).resolve().parents[1] / "frontend" / "dist")
SHOTS=str(__import__("pathlib").Path(__file__).resolve().parents[1] / "docs" / "screenshots")
H=functools.partial(http.server.SimpleHTTPRequestHandler, directory=DIST)
class Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self,*a): pass
H=functools.partial(Q, directory=DIST)
class S(socketserver.ThreadingTCPServer): allow_reuse_address=True
srv=S(("127.0.0.1",8765),H); threading.Thread(target=srv.serve_forever,daemon=True).start()
step=sys.argv[1]
with sync_playwright() as p:
    import os, urllib.parse
    u=urllib.parse.urlparse(os.environ.get("HTTPS_PROXY",""))
    proxy={"server": f"{u.scheme}://{u.hostname}:{u.port}", "bypass": "127.0.0.1,localhost"}
    if u.username: proxy["username"]=urllib.parse.unquote(u.username)
    if u.password: proxy["password"]=urllib.parse.unquote(u.password)
    b=p.chromium.launch(proxy=proxy if u.hostname else None)
    ctx=b.new_context(viewport={"width":1280,"height":900})
    if len(sys.argv)>2:
        ctx.add_init_script(f"localStorage.setItem('padiguard.burnerKey','{sys.argv[2]}');localStorage.setItem('padiguard.accountMode','burner');")
    pg=ctx.new_page()
    logs=[]
    pg.on("console", lambda m: logs.append(m.type+": "+m.text))
    pg.on("pageerror", lambda e: logs.append("PAGEERROR "+str(e)))
    pg.goto("http://127.0.0.1:8765/index.html")
    try:
        pg.wait_for_selector(".tiles", timeout=30000)
    except Exception as e:
        print("LOGS", "\n".join(logs)[:3000]); print(pg.inner_text("main")[:1000]); pg.screenshot(path=SHOTS+"/fail.png"); raise
    if step=="read":
        pg.screenshot(path=SHOTS+"/overview.png", full_page=True)
        print(pg.inner_text(".tiles"))
        pg.click("[data-tab=policies]"); pg.wait_for_selector("#all-policies table", timeout=30000)
        pg.click("[data-policy='1']"); pg.wait_for_selector("#policy-detail .kv", timeout=30000)
        pg.screenshot(path=SHOTS+"/policy.png", full_page=True)
        print(pg.inner_text("#policy-detail")[:800])
    elif step=="farmer":
        pg.wait_for_selector("#btn-faucet", timeout=20000); pg.click("#btn-faucet"); time.sleep(6)
        pg.click("[data-tab=farmer]"); time.sleep(4)
        pg.screenshot(path=SHOTS+"/farmer.png", full_page=True)
        print(pg.inner_text("#account")); print(pg.inner_text("#my-stuff")[:600])
    elif step=="quote":
        pg.click("[data-tab=farmer]"); time.sleep(2)
        pg.fill("input[name=drought]", sys.argv[3] if len(sys.argv)>3 else "300")
        pg.click("#quote-form button[type=submit]")
        pg.wait_for_selector("#btn-buy", timeout=105000)
        pg.screenshot(path=SHOTS+"/quote.png", full_page=True)
        print(pg.inner_text("#quote-card")[:1500]); print(pg.inner_text("#txlog"))
    elif step=="pool":
        pg.click("[data-tab=insurer]"); time.sleep(2)
        pg.fill("#pool-form input[name=name]", "Java Mutual (demo, 2nd insurer)")
        pg.fill("#pool-form input[name=min]", "8")
        pg.fill("#pool-form input[name=loading]", "3")
        pg.select_option("#pool-form select[name=region]", "Java")
        pg.fill("#pool-form input[name=deposit]", "5")
        pg.click("#pool-form button[type=submit]")
        pg.wait_for_selector(".tx.ok, .tx.err", timeout=100000); time.sleep(5)
        pg.click("[data-tab=insurer]"); time.sleep(1)
        pg.screenshot(path=SHOTS+"/insurer.png", full_page=True)
        print(pg.inner_text("#txlog")); print(pg.inner_text("#tab-insurer")[:1500])
    elif step=="insurer-view":
        pg.click("[data-tab=insurer]"); pg.wait_for_selector(".poolcard", timeout=30000); time.sleep(1)
        pg.screenshot(path=SHOTS+"/insurer.png", full_page=True)
        print(pg.inner_text(".poolcard"))
    elif step=="fund":
        pg.on("dialog", lambda d: d.accept("1"))
        pg.click("[data-tab=insurer]"); pg.wait_for_selector(".poolcard [data-fund]", timeout=30000)
        pg.click(".poolcard [data-fund]")
        pg.wait_for_selector(".tx.ok, .tx.err", timeout=100000); time.sleep(4)
        pg.click("[data-tab=insurer]"); time.sleep(1)
        pg.screenshot(path=SHOTS+"/insurer.png", full_page=True)
        print(pg.inner_text("#txlog")); print(pg.inner_text(".poolcard"))
    elif step=="mine":
        pg.click("[data-tab=farmer]"); pg.wait_for_selector("#my-stuff h3", timeout=30000); time.sleep(3)
        pg.screenshot(path=SHOTS+"/farmer-mine.png", full_page=True)
        print(pg.inner_text("#my-stuff")[:1200])
    elif step=="settle":
        pg.click("[data-tab=policies]"); pg.wait_for_selector("#all-policies [data-policy='2']", timeout=30000)
        pg.click("#all-policies [data-policy='2']"); pg.wait_for_selector("#btn-settle-anyway", timeout=30000)
        pg.click("#btn-settle-anyway")
        pg.wait_for_selector(".tx.ok, .tx.err", timeout=100000); time.sleep(3)
        pg.screenshot(path=SHOTS+"/settle-early.png", full_page=True)
        print(pg.inner_text("#txlog"))
    elif step=="buy":
        pg.click("[data-tab=farmer]"); time.sleep(4)
        pg.click("[data-showq]"); pg.wait_for_selector("#btn-buy", timeout=20000)
        pg.click("#btn-buy")
        pg.wait_for_selector(".tx.ok, .tx.err", timeout=100000)
        time.sleep(4)
        pg.screenshot(path=SHOTS+"/bought.png", full_page=True)
        print(pg.inner_text("#txlog")); print(pg.inner_text("#my-stuff")[:800])
    print("\n".join(l for l in logs if "error" in l.lower())[:2000])
    b.close()
srv.shutdown()
