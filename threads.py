import asyncio
import json
import random
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright


COUNT_RE = re.compile(r"^\d+(?:,\d{3})*(?:\.\d+)?(?:[KMB]|萬)?$")
TIME_RE = re.compile(
    r"^(?:\d+[smhdw]|\d+秒|\d+分|\d+小時|\d+天|\d+週|\d+個?月|\d+年)$",
    re.IGNORECASE,
)


def normalize_text(value):
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def parse_count(value):
    if value is None:
        return None

    text = normalize_text(str(value))
    if not text:
        return None

    # 新增這行：強制濾除字串開頭的任何中文字或非數字字元 (例如把 "讚5 萬" 變成 "5 萬")
    text = re.sub(r"^[^\d]+", "", text)
    if not text:
        return None

    multiplier = 1
    suffix = text[-1]
    if suffix in {"K", "k"}:
        multiplier = 1000
        text = text[:-1]
    elif suffix in {"M", "m"}:
        multiplier = 1000000
        text = text[:-1]
    elif text.endswith("萬"):
        multiplier = 10000
        text = text[:-1]

    text = text.replace(",", "").strip() # 加入 strip() 確保沒有殘留空白
    try:
        number = float(text)
    except ValueError:
        return None

    return int(round(number * multiplier))


def build_user_agent():
    return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"


async def detect_login_gate(page):
    checks = [
        "Log in to see more from",
        "Log in or sign up for Threads",
        "Say more with Threads",
    ]
    for text in checks:
        if await page.get_by_text(text).count() > 0:
            return True
    return False


async def scroll_feed_and_wait(page, wait_ms=None):
    """Scroll the most likely feed container and wait for potential lazy-loaded content."""

    if wait_ms is None:
        wait_ms = random.randint(7000, 15000)
    before_count = await page.locator("time").count()

    scroll_info = await page.evaluate(
        r"""
        () => {
            function isScrollable(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const overflowY = style.overflowY || '';
                const canOverflow = overflowY === 'auto' || overflowY === 'scroll';
                return canOverflow && el.scrollHeight > el.clientHeight + 20;
            }

            const candidates = Array.from(document.querySelectorAll('main, div, section'))
                .filter(isScrollable)
                .map((el) => ({
                    el,
                    score: el.scrollHeight - el.clientHeight,
                }))
                .sort((a, b) => b.score - a.score);

            if (candidates.length > 0) {
                const target = candidates[0].el;
                const beforeTop = target.scrollTop;
                const beforeHeight = target.scrollHeight;
                target.scrollTo({ top: target.scrollHeight, behavior: 'auto' });
                const afterTop = target.scrollTop;
                return {
                    target: 'container',
                    beforeTop,
                    afterTop,
                    beforeHeight,
                };
            }

            const beforeTop = window.scrollY || document.documentElement.scrollTop;
            const beforeHeight = document.documentElement.scrollHeight;
            window.scrollTo(0, document.documentElement.scrollHeight);
            const afterTop = window.scrollY || document.documentElement.scrollTop;
            return {
                target: 'window',
                beforeTop,
                afterTop,
                beforeHeight,
            };
        }
        """
    )

    # Simulate user-like scrolling to trigger lazy loading paths.
    for _ in range(3):
        await page.mouse.wheel(0, 1400)
        await page.wait_for_timeout(300)

    await page.keyboard.press("PageDown")
    await page.wait_for_timeout(350)
    await page.keyboard.press("End")

    await page.wait_for_timeout(wait_ms)

    # Give lazy loading one more short chance if count has not changed yet.
    mid_count = await page.locator("time").count()
    if mid_count == before_count:
        await page.wait_for_timeout(1800)

    after_count = await page.locator("time").count()
    return {
        "before_count": before_count,
        "after_count": after_count,
        "scroll_info": scroll_info,
    }


async def extract_post_card(time_locator, username):
    """
    DOM 物理分離版：
    1. 透過尋找「讚/回覆」的圖示來定位卡片，不再受按鈕數量的影響。
    2. 透過 DOM 節點直接抓取數據，讓內文的 1/2 完全無法干擾 Metrics。
    """
    return await time_locator.evaluate(
        r"""
        (node, username) => {
            function normalize(value) {
                return (value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
            }

            // 1. 尋找卡片根節點 (最安全的找法：找到包含作者與互動圖示的容器)
            function findCardRoot(timeNode) {
                let current = timeNode;
                let depth = 0;
                while (current && current !== document.body && depth < 30) {
                    const hasAuthor = !!current.querySelector('a[href^="/@"]');
                    // 只要有這些圖示存在 (即使沒人按讚，圖示也一定在)，就代表這是卡片容器！
                    const hasActionBar = !!current.querySelector('svg[aria-label="讚"]') || 
                                         !!current.querySelector('svg[aria-label="Like"]') ||
                                         !!current.querySelector('svg[aria-label="回覆"]') ||
                                         !!current.querySelector('svg[aria-label="Reply"]');
                    
                    if (hasAuthor && hasActionBar) {
                        return current;
                    }
                    current = current.parentElement;
                    depth++;
                }
                
                // 備用方案
                let fallback = timeNode;
                for(let i=0; i<8; i++) { if(fallback.parentElement) fallback = fallback.parentElement; }
                return fallback;
            }

            const cardRoot = findCardRoot(node);
            if (!cardRoot) return null;

            // 2. 抓取作者與時間
            const authorLinks = Array.from(cardRoot.querySelectorAll('a[href^="/@"]'))
                .filter(a => /^\/@[^/]+$/.test(a.getAttribute('href') || ''));
            const mainAuthor = authorLinks[0] ? normalize(authorLinks[0].textContent) : null;
            
            const timeText = normalize(node.innerText);
            const postLink = node.closest('a') ? node.closest('a').href : (cardRoot.querySelector('a[href*="/post/"]')?.href || null);

            // 3. 物理提取 Metrics (絕對不受內文 1/2 干擾)
            const metrics = {};
            const buttons = Array.from(cardRoot.querySelectorAll('div[role="button"]'));
            for (const btn of buttons) {
                const svg = btn.querySelector('svg[aria-label]');
                if (svg) {
                    const label = svg.getAttribute('aria-label');
                    const count = normalize(btn.innerText);
                    if (label) {
                        metrics[label.toLowerCase()] = { label, raw: count || null };
                    }
                }
            }

            // 4. 抓取 回覆給誰
            let replyingTo = null;
            const rawText = normalize(cardRoot.innerText);
            const replyMatch = rawText.match(/(?:正在回覆|Replying to)\s*(@[^\s]+)/);
            if (replyMatch) {
                replyingTo = replyMatch[1];
            }

            // 5. 抓取內文
            let contentLines = [];
            // Threads 的主要文字都放在 dir="auto" 的 span 裡
            const textSpans = Array.from(cardRoot.querySelectorAll('span[dir="auto"]'));
            
            textSpans.forEach(span => {
                // 如果文字是在按鈕裡，跳過
                if (span.closest('div[role="button"]')) return;
                // 如果文字是作者名稱或頭像連結，跳過
                if (span.closest('a[href^="/@"]')) return;
                
                const text = normalize(span.innerText);
                if (!text) return;
                
                if (text === timeText) return;
                if (text === '>' || text === '·' || text === 'Translate' || text === '翻譯') return;
                if (/^\d+\s*[秒分天週月年]$/.test(text) || /^\d+\s*小時$/.test(text)) return;
                if (text.includes('正在回覆') || text.includes('Replying to')) return;
                
                // 剩下的就是純淨內文！(包含您提供的 HTML 中 1/2 的外層 span)
                contentLines.push(text);
            });

            // 萬一 DOM 抓取失敗的備用提取法 (確保一定有內容)
            let finalContent = contentLines.join('\n');
            if (!finalContent) {
                let fbText = rawText;
                if (mainAuthor) fbText = fbText.replace(mainAuthor, '');
                if (timeText) fbText = fbText.replace(timeText, '');
                for (const m of Object.values(metrics)) {
                    if (m.raw) fbText = fbText.replace(m.raw, '');
                }
                fbText = fbText.replace(/正在回覆|Replying to|Translate|翻譯|·|>/g, '');
                fbText = fbText.replace(/@[^\s]+/g, ''); 
                finalContent = normalize(fbText);
            }

            return {
                author: {
                    username: mainAuthor,
                    is_official_account: mainAuthor === username,
                },
                post_url: postLink,
                timestamp: {
                    display: timeText,
                    exact: node.getAttribute('title') || null,
                },
                replying_to: replyingTo,
                content: finalContent || null,
                metrics,
                raw_text: rawText.slice(0, 500),
                has_media: cardRoot.querySelectorAll('img, video').length > 0,
                image_alts: Array.from(cardRoot.querySelectorAll('img')).map(img => img.alt).filter(a => a && !a.includes('大頭貼'))
            };
        }
        """,
        username,
    )


async def collect_visible_posts(page, username, replies_data, seen_post_urls, max_posts, max_no_growth=5):
    posts_count = len(replies_data)
    no_growth_rounds = 0
    batch_added = 0

    while posts_count < max_posts and no_growth_rounds < max_no_growth:
        time_elements = await page.locator("time").element_handles()
        growth = False

        for time_element in time_elements:
            try:
                record = await extract_post_card(time_element, username)
            except Exception as e:
                print(f"Error at index {i}: {e}")
                continue

            if not record or not record.get("post_url"):
                continue

            for metric in record.get("metrics", {}).values():
                metric["value"] = parse_count(metric.get("raw"))

            if record["post_url"] in seen_post_urls:
                continue

            seen_post_urls.add(record["post_url"])
            replies_data.append(record)
            batch_added += 1
            posts_count += 1
            growth = True
            print(f"✓ {record['author']['username']} | {record['timestamp']['display']}")

            if max_posts is not None and posts_count >= max_posts:
                return batch_added, True

        if not growth:
            no_growth_rounds += 1
        else:
            no_growth_rounds = 0

        if posts_count < max_posts:
            await page.mouse.wheel(0, 1500)
            await page.wait_for_timeout(4000)  # 由 2000 增加到 4000 毫秒（4 秒)

    return batch_added, posts_count >= max_posts


async def run_auto_scrape(page, username, target_new_posts, seen_urls):
    new_replies = [] # 建立一個乾淨的 list，只用來裝「這次新抓到的」
    no_growth = 0
    
    while len(new_replies) < target_new_posts:
        time_elements = await page.locator("time").element_handles()
        initial_count = len(new_replies)
        
        if time_elements:
            last_el = time_elements[-1]
            last_record = await extract_post_card(last_el, username)
            
            if last_record and last_record.get("post_url") in seen_urls:
                print(f"⏩ 快速推進中... 目前已路過至 {last_record['timestamp']['display']}")
                await page.evaluate("window.scrollBy(0, 8000)") 
                await asyncio.sleep(2) 
                continue

        for el in time_elements:
            try:
                record = await extract_post_card(el, username)
                if not record or not record.get("post_url") or record["post_url"] in seen_urls:
                    continue
                
                for m in record["metrics"].values(): 
                    m["value"] = parse_count(m["raw"])
                
                # 加入記憶庫 (防止這輪重複抓)
                seen_urls.add(record["post_url"])
                # 只把新的資料存進 new_replies
                new_replies.append(record)
                
                total_seen = len(seen_urls) # 加上舊資料的總數
                print(f"✓ [本次新抓:{len(new_replies)} | 歷史總和:{total_seen}] {record['timestamp']['display']} | {record['author']['username']}")
                
                if len(new_replies) >= target_new_posts: break
            except: continue

        if len(new_replies) == initial_count:
            no_growth += 1
            if no_growth > 10: 
                print("⚠️ 偵測到加載停滯，可能觸發限流，存檔中...")
                break
        else:
            no_growth = 0
            await scroll_feed_and_wait(page)
            
    return new_replies


async def login_and_scrape_auto(
    target_new_posts=2560, # 假設你這次想「再」抓 2000 則
    storage_state_path="threads_storage_state.json",
):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        context_args = {"user_agent": build_user_agent()}
        if Path(storage_state_path).exists():
            context_args["storage_state"] = storage_state_path
            
        context = await browser.new_context(**context_args)
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = await context.new_page()

        await page.goto("https://www.threads.net/")

        print("\n[安全分檔：中斷續傳模式已啟動]")
        print(f"🎯 本次目標：只抓取【全新】的 {target_new_posts} 則貼文")
        await asyncio.to_thread(input, "當你確認已在目標 replies 頁面後，按 Enter 開始自動抓取... ")

        current_url = page.url
        username = re.search(r"/@([^/]+)/replies", current_url).group(1) if re.search(r"/@([^/]+)/replies", current_url) else "default_user"
        
        # ==========================================
        # 核心修改：將「歷史檔案」與「新存檔案」分開
        # ==========================================
        history_file = Path(f"{username}_replies1.json") # 你已經備份好的 7000 則
        output_file = Path(f"{username}_replies2.json")  # 這次新抓完要存的地方
        
        seen_urls = set()

        # 1. 讀取歷史檔案 (唯讀模式，絕對安全)
        if history_file.exists():
            with open(history_file, "r", encoding="utf-8") as f:
                old_data = json.load(f)
                seen_urls = {p["post_url"] for p in old_data.get("posts", []) if p.get("post_url")}
            print(f"✅ 成功載入防護罩：已記憶 {len(seen_urls)} 則歷史貼文，將自動跳過它們！")
        else:
            print("🆕 找不到 _replies1.json，將從頭開始抓取。")

        # 2. 執行抓取 (回傳的 updated_data 只會包含新的資料)
        new_data = await run_auto_scrape(page, username, target_new_posts, seen_urls)

        # 3. 儲存新檔案 (存進 replies2.json)
        output = {
            "username": username,
            "source_url": current_url,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "new_posts_count": len(new_data),
            "posts": new_data, # 這裡面只有新的貼文！
        }

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"\n🎉 爬取完成！已將 {len(new_data)} 則【全新貼文】安全存入 {output_file}。")
        await context.storage_state(path=storage_state_path)
        await context.close()
        await browser.close()

if __name__ == "__main__":
    storage_state_path = "threads_storage_state.json"
    
    print("🚀 啟動 Threads 安全分檔爬蟲...")
    asyncio.run(
        login_and_scrape_auto(
            # 注意：這裡是設定「本次要新增多少則」
            # 如果你想要總數達到 9000，且已有 7000，這裡就設 2000
            # 如果你要額外再抓 9000 則，這裡就設 9000
            target_new_posts=2560, 
            storage_state_path=storage_state_path,
        )
    )