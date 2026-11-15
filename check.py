import asyncio, os, json, hashlib, re
import httpx
from playwright.async_api import async_playwright

URL = "https://www.dtek-dnem.com.ua/ua/shutdowns"
TOKEN = os.environ["BOT_TOKEN"]
ADDRS = json.loads(os.environ["ADDRESSES"])
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

try:
    state = json.load(open("state.json"))
except FileNotFoundError:
    state = {}


def save_state():
    json.dump(state, open("state.json", "w"))


def norm(s):
    return s.replace("’", "'").replace("ʼ", "'").replace("`", "'").lower().strip()


PREFIXES = r"(м\.|с\.|смт\.?|с-ще|вул\.|просп\.|пров\.|бульв\.|пл\.|узвіз|шосе)"


def body(t):
    return re.sub(r"^" + PREFIXES + r"\s*", "", norm(t))


def choose(texts, query, prefix):
    q = norm(query)
    cands = list(enumerate(texts))
    if prefix:
        p = norm(prefix)
        pc = [(i, t) for i, t in cands if norm(t).startswith(p)]
        if pc:
            cands = pc
    for i, t in cands:
        if norm(t) == q or body(t) == q:
            return i
    for i, t in cands:
        if q in norm(t):
            return i
    return cands[0][0] if cands else None


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


async def fill(page, field_id, queries, prefix=None):
    form = page.locator("#discon_form")
    inp = form.locator(f"#{field_id}")
    items = form.locator(".autocomplete-items > div")
    texts = []
    for q in queries:
        await inp.click()
        await inp.fill("")
        await inp.press_sequentially(q, delay=150)
        for _ in range(24):  # ждём до 6 секунд
            n = await items.count()
            if n:
                await page.wait_for_timeout(500)
                n = await items.count()
                texts = [(await items.nth(i).inner_text()).strip() for i in range(n)]
                break
            await page.wait_for_timeout(250)
        if texts:
            break
    print(f"OPTIONS {field_id}:", texts[:15])
    if not texts:
        print("INPUT VALUE:", await inp.input_value())
        raise Exception(f"нет подсказок для поля {field_id}")
    idx = choose(texts, queries[0], prefix)
    if idx is None:
        raise Exception(f"не выбрал подсказку для поля {field_id}")
    await items.nth(idx).click()
    await page.wait_for_timeout(1500)
    print(f"CHOSEN {field_id}:", await inp.input_value())


async def get_text(browser, a):
    ctx = await browser.new_context(
        user_agent=UA,
        locale="uk-UA",
        timezone_id="Europe/Kyiv",
        viewport={"width": 1280, "height": 900},
    )
    page = await ctx.new_page()
    try:
        await page.goto(URL, wait_until="networkidle", timeout=60000)
        try:
            await page.wait_for_function(
                "typeof DisconSchedule !== 'undefined' && DisconSchedule.streets", timeout=20000
            )
        except Exception:
            print("DisconSchedule не загрузился")
        await page.wait_for_timeout(2000)
        await close_popups(page)

        m = re.match(r"^\s*(м\.|с\.|смт\.?|с-ще)\s*(.*)$", a["city"], re.I)
        c_prefix, c_query = (m.group(1), m.group(2)) if m else (None, a["city"])

        await fill(page, "city", [c_query], c_prefix)
        await fill(page, "street", [a["street"], "вул. " + a["street"]])
        await fill(page, "house_num", [a["house"]])

        # ждём, пока сайт покажет очередь (пока грузится, там "undefined")
        try:
            await page.wait_for_function(
                """() => {
                    const el = document.querySelector('#discon_form #group-name');
                    if (!el) return false;
                    const t = (el.textContent || '').trim();
                    return t.length > 0 && !t.includes('undefined');
                }""",
                timeout=15000,
            )
        except Exception:
            print("очередь не появилась, читаю как есть")
        await page.wait_for_timeout(2000)

        queue = ((await page.locator("#discon_form #group-name").first.text_content()) or "").strip()
        queue = re.sub(r"\s+", " ", queue)
        text = (await page.locator("#showCurOutage").first.inner_text()).strip()
        print("QUEUE:", queue)
        return queue, text
    except Exception:
        try:
            await page.screenshot(path=f"error{a['chat_id']}.png")
        except Exception:
            pass
        raise
    finally:
        await ctx.close()


async def send_telegram(http, chat_id, text):
    for attempt in range(1, 4):
        try:
            r = await http.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=30,
            )
            print("telegram:", r.status_code, r.text[:200])
            return r.status_code == 200
        except Exception as e:
            print(f"telegram попытка {attempt} не удалась:", repr(e))
            await asyncio.sleep(3)
    return False


async def main():
    async with async_playwright() as p, httpx.AsyncClient(timeout=30) as http:
        browser = await p.chromium.launch()
        for a in ADDRS:
            key = str(a["chat_id"])
            result = None
            for attempt in range(1, 4):
                try:
                    result = await get_text(browser, a)
                    break
                except Exception as e:
                    print(f"попытка {attempt} не удалась:", e)
                    await asyncio.sleep(5)
            if not result:
                print("fail", key)
                continue
            queue, text = result
            # дату обновления убираем, чтобы она не вызывала лишние сообщения
            core = re.sub(r"Дата оновлення.*", "", text, flags=re.S).strip()
            h = hashlib.sha256((queue + core).encode()).hexdigest()
            if state.get(key) != h:
                if await send_telegram(http, a["chat_id"], f"{queue}\n\n{text}"):
                    state[key] = h
                    save_state()
            else:
                print("no changes", key)
        await browser.close()
    save_state()


asyncio.run(main())
