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


async def fill(page, field_id, value):
    form = page.locator("#discon_form")
    inp = form.locator(f"#{field_id}")
    await inp.click()
    await inp.press_sequentially(value, delay=120)
    opt = form.locator(".autocomplete-items div", has_text=value).first
    await opt.wait_for(timeout=10000)
    await opt.click()


async def get_text(browser, a, idx):
    page = await browser.new_page()
    try:
        await page.goto(URL, wait_until="networkidle", timeout=60000)
        await fill(page, "city", a["city"])
        await fill(page, "street", a["street"])
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
