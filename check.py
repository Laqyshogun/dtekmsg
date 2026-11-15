import asyncio, os, json, hashlib, re
import httpx
from playwright.async_api import async_playwright

URL = "https://www.dtek-dnem.com.ua/ua/shutdowns"
TOKEN = os.environ["BOT_TOKEN"]
ADDRS = json.loads(os.environ["ADDRESSES"])

try:
    state = json.load(open("state.json"))
except FileNotFoundError:
    state = {}


async def on_response(r):
    try:
        if "dtek-dnem.com.ua" in r.url and r.request.resource_type in ("xhr", "fetch"):
            try:
                body = (await r.text())[:300].replace("\n", " ")
            except Exception:
                body = ""
            post = (r.request.post_data or "")[:200]
            print("NET:", r.request.method, r.url, r.status, "| post:", post, "| body:", body)
    except Exception:
        pass


async def close_popups(page):
    # закрываем всплывающее окно "Шановні клієнти!"
    for sel in [
        "[class*='modal'] [class*='close']:visible",
        "[class*='popup'] [class*='close']:visible",
        "button[class*='close']:visible",
        "[class*='close']:visible",
    ]:
        try:
            el = page.locator(sel).first
            if await el.count():
                await el.click(timeout=2000)
                await page.wait_for_timeout(500)
                break
        except Exception:
            pass
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


JS_STREETS = """
([city, q]) => {
  const norm = s => s.replace(/[’ʼ`]/g, "'").toLowerCase().trim();
  const D = (typeof DisconSchedule !== 'undefined') ? DisconSchedule : window.DisconSchedule;
  const streets = (D && D.streets) || {};
  const key = Object.keys(streets).find(k => norm(k) === norm(city))
           || Object.keys(streets).find(k => norm(k).includes(norm(city)) || norm(city).includes(norm(k)));
  if (!key) return {key: null, sample: Object.keys(streets).slice(0, 10), matches: []};
  const matches = streets[key].filter(s => norm(s).includes(norm(q)));
  return {key, matches};
}
"""

JS_FIND = """
(v) => {
  const out = [];
  document.querySelectorAll('body *').forEach(el => {
    if (el.children.length === 0 && el.tagName !== 'SCRIPT' &&
        el.textContent.toLowerCase().includes(v.toLowerCase())) {
      const r = el.getBoundingClientRect();
      const chain = [];
      let p = el;
      for (let i = 0; i < 4 && p; i++) {
        chain.push(p.tagName + '.' + (p.className || '') + '#' + (p.id || ''));
        p = p.parentElement;
      }
      out.push({visible: r.width > 0 && r.height > 0, text: el.textContent.trim().slice(0, 60), chain});
    }
  });
  return out.slice(0, 15);
}
"""


async def debug_dump(page, value):
    try:
        found = await page.evaluate(JS_FIND, value)
        print("ELEMENTS WITH TEXT:", json.dumps(found, ensure_ascii=False))
        form = page.locator("#discon_form")
        html = await form.inner_html()
        html = re.sub(r"<svg.*?</svg>", "", html, flags=re.S)
        print("FORM HTML:", html[:2500])
    except Exception as e:
        print("debug error:", e)


async def fill(page, field_id, value):
    form = page.locator("#discon_form")
    inp = form.locator(f"#{field_id}")
    await inp.click()
    await inp.press_sequentially(value, delay=120)

    target = None
    for _ in range(40):  # ждём до 10 секунд
        exact = form.get_by_text(value, exact=True).locator("visible=true")
        if await exact.count():
            target = exact.first
            break
        part = form.get_by_text(value).locator("visible=true")
        if await part.count():
            target = part.first
            break
        await page.wait_for_timeout(250)
    if target is None:
        print("INPUT VALUE:", await inp.input_value())
        await debug_dump(page, value)
        raise Exception(f"не нашёл подсказку для поля {field_id}")
    await target.click()
    await page.wait_for_timeout(1500)


async def get_text(browser, a, idx):
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page.on("response", on_response)
    try:
        await page.goto(URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(2000)
        await close_popups(page)

        await fill(page, "city", a["city"])
        city_val = await page.locator("#discon_form #city").input_value()
        print("CITY CHOSEN:", city_val)

        # достаём полное название улицы из данных страницы (например "вул. Харківська")
        info = await page.evaluate(JS_STREETS, [city_val, a["street"]])
        print("STREETS INFO:", json.dumps(info, ensure_ascii=False)[:600])
        street_full = info["matches"][0] if info.get("matches") else a["street"]
        print("STREET FULL:", street_full)

        await fill(page, "street", street_full)
        await fill(page, "house_num", a["house"])
        await page.wait_for_timeout(3000)
        queue = (await page.locator("#group-name").inner_text()).strip()
        text = (await page.locator("#showCurOutage").inner_text()).strip()
        return queue, text
    except Exception:
        await page.screenshot(path=f"error{idx}.png")
        raise
    finally:
        await page.close()


async def main():
    async with async_playwright() as p, httpx.AsyncClient() as http:
        browser = await p.chromium.launch()
        for i, a in enumerate(ADDRS):
            key = str(a["chat_id"])
            try:
                queue, text = await get_text(browser, a, i)
            except Exception as e:
                print("fail", key, e)
                continue
            # дату обновления убираем, чтобы она не вызывала лишние сообщения
            core = re.sub(r"Дата оновлення.*", "", text, flags=re.S).strip()
            h = hashlib.sha256((queue + core).encode()).hexdigest()
            if state.get(key) != h:
                r = await http.post(
                    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                    json={"chat_id": a["chat_id"], "text": f"{queue}\n\n{text}"},
                )
                print("telegram:", r.status_code, r.text[:200])
                if r.status_code == 200:
                    state[key] = h
            else:
                print("no changes", key)
        await browser.close()
    json.dump(state, open("state.json", "w"))


asyncio.run(main())
