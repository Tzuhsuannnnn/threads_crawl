import asyncio
import json
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

    text = text.replace(",", "")
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


async def scroll_feed_and_wait(page, wait_ms=6000):  # 由 4500 增加到 6000 毫秒（6 秒）
    """Scroll the most likely feed container and wait for potential lazy-loaded content."""
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
    """簡化版本：直接提取 time 周圍的資訊"""
    return await time_locator.evaluate(
        r"""
        (node, username) => {
            function normalize(value) {
                return (value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
            }

            function normalizeLine(value) {
                return (value || '').replace(/\u00a0/g, ' ').trim();
            }

            function getAuthorLinks(root) {
                return Array.from(root.querySelectorAll('a[href^="/@"]'))
                    .filter((a) => {
                        const href = a.getAttribute('href') || '';
                        // Keep profile links only, ignore /post/ links.
                        return /^\/@[^/]+$/.test(href);
                    });
            }

            function getMetricButtons(root) {
                return Array.from(root.querySelectorAll('div[role="button"]')).filter((btn) => {
                    const text = normalize(btn.innerText || '');
                    const svg = btn.querySelector('svg[aria-label]');
                    return !!svg || !!text;
                });
            }

            function escapeRegExp(value) {
                return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            }

            function isMetricLikeLine(line) {
                const text = normalize(line);
                if (!text) return false;
                if (/^\d+\s*\/\s*\d+$/.test(text)) return true; // e.g. 1/2
                if (/^\d{1,3}(?:,\d{3})*(?:\.\d+)?(?:\s*[萬KMB])?$/i.test(text)) return true;
                return false;
            }

            function isLowQualityContent(text) {
                const t = normalize(text);
                if (!t) return true;
                // If there is no letter/number in any language, treat as low quality.
                if (!/[\p{L}\p{N}]/u.test(t)) return true;
                if (/^[天年月日時分秒週日]$/.test(t)) return true; // residual single time unit char
                if (t.length <= 1) return true;
                return false;
            }

            // 從 time 元素向上找卡片根元素（避免抓到過淺的 header 容器）
            function findCardRoot(timeNode) {
                let current = timeNode;
                let depth = 0;
                let fallback = null;
                
                while (current && current !== document.body && depth < 20) {
                    const authorLinks = getAuthorLinks(current);
                    const metricButtons = getMetricButtons(current);
                    const hasPostAnchor = !!timeNode.closest('a[href*="/post/"]');
                    
                    // 優先：同時包含作者、時間、互動按鈕的卡片。
                    if (authorLinks.length > 0 && metricButtons.length >= 2 && current.contains(timeNode)) {
                        const text = normalize(current.innerText || '');
                        if (text.length >= 16 && hasPostAnchor) {
                            return current;
                        }
                    }

                    // 次佳：記錄包含作者+時間的較完整容器。
                    if (!fallback && authorLinks.length > 0 && current.contains(timeNode)) {
                        const text = normalize(current.innerText || '');
                        if (text.length >= 10) {
                            fallback = current;
                        }
                    }
                    
                    depth++;
                    current = current.parentElement;
                }
                
                return fallback;
            }

            const timeElement = node;
            const timeOuter = timeElement;
            const cardRoot = findCardRoot(timeElement);
            
            if (!cardRoot) {
                return null;
            }

            // 提取基本資訊
            const authorLinks = getAuthorLinks(cardRoot);
            const authorLink = authorLinks.find((link) => normalize(link.textContent)) || authorLinks[0] || null;
            
            const timeNode = cardRoot.querySelector('time');
            const timeText = timeNode ? normalize(timeNode.innerText) : '';
            const timeTitle = timeNode ? timeNode.getAttribute('title') : null;
            
            const postLink = timeNode ? timeNode.closest('a[href*="/post/"]') : null;
            
            // 多媒體偵測
            const imgElements = cardRoot.querySelectorAll('img');
            const videoElements = cardRoot.querySelectorAll('video');
            const hasMedia = imgElements.length > 0 || videoElements.length > 0;
            const imageAlts = Array.from(imgElements).map(img => img.alt).filter(Boolean);

            // 提取文本內容（保留換行，避免內容被壓成一行）
            const rawTextOriginal = (cardRoot.innerText || '').replace(/\u00a0/g, ' ');
            const lines = rawTextOriginal
                .split('\n')
                .map((line) => normalizeLine(line))
                .filter(Boolean);
            
            // 從時間開始向後提取內容
            const timeIndex = timeText ? lines.indexOf(timeText) : -1;
            
            // 提取互動按鈕信息
            const buttons = getMetricButtons(cardRoot);
            const metrics = {};
            
            // 尋找帶有 aria-label 的 svg（指示互動類型）
            for (const button of buttons) {
                const svg = button.querySelector('svg[aria-label]');
                if (!svg) continue;
                
                const label = svg.getAttribute('aria-label');
                // 保留所有找到的互動類型
                const count = normalize(button.innerText);
                if (count && label) {
                    metrics[label.toLowerCase()] = {
                        label,
                        raw: count || null,
                    };
                }
            }
            
            // 提取回覆人信息
            const replyLine = lines.find((line) => /^(Replying to @|正在回覆@)/.test(line)) || null;
            let replyTo = null;
            let replyInlineContent = null;
            if (replyLine) {
                const m = replyLine.match(/^(?:Replying to\s+@|正在回覆@)([^\s]+)\s*(.*)$/);
                if (m) {
                    replyTo = '@' + normalize(m[1]);
                    replyInlineContent = normalize(m[2] || '');
                }
            }
            
            // 提取內容（介於時間和互動之間）
            const contentStart = timeIndex >= 0 ? timeIndex + 1 : 1;
            let metric_start = lines.length;
            for (let i = contentStart; i < lines.length; i++) {
                if (isMetricLikeLine(lines[i])) {
                    metric_start = i;
                    break;
                }
            }

            let contentLines = lines
                .slice(contentStart, metric_start)
                .map((line) => {
                    if (replyLine && line === replyLine) {
                        return replyInlineContent || '';
                    }
                    return line;
                })
                .filter(line => line !== 'Translate' && line !== '翻譯')
                .filter(line => !/^\d+\s*\/\s*\d+$/.test(normalize(line)))
                .filter(line => !/^\d+$/.test(normalize(line)))
                .filter(line => !/^(?:\d+\s*(?:秒|分|小時|天|週|月|年)|[秒分天週月年])$/.test(normalize(line)))
                .map(line => normalize(line));

            // Fallback: when line slicing fails (common on short brand replies), derive content from raw text.
            if (contentLines.length === 0) {
                let fallback = normalize(rawTextOriginal);

                const authorText = authorLink ? normalize(authorLink.textContent || '') : '';
                if (authorText) {
                    fallback = fallback.replace(new RegExp('^' + escapeRegExp(authorText) + '\\s*'), '');
                }

                if (timeText) {
                    fallback = fallback.replace(new RegExp(escapeRegExp(timeText), 'g'), ' ');
                }

                if (replyLine) {
                    fallback = fallback.replace(new RegExp(escapeRegExp(normalize(replyLine))), ' ');
                }

                fallback = fallback.replace(/\b(Translate|翻譯)\b/g, ' ');
                fallback = fallback.replace(/\b\d+\s*\/\s*\d+\b/g, ' ');
                fallback = fallback.replace(/(?:^|\s)\d+\s*(?:秒|分|小時|天|週|月|年)(?=\s|$)/g, ' ');
                fallback = fallback.replace(/(?:^|\s)(?:秒|分|小時|天|週|月|年)(?=\s|$)/g, ' ');

                // Remove known metric raw values (usually trailing numbers)
                for (const metric of Object.values(metrics)) {
                    const raw = normalize(metric.raw || '');
                    if (!raw) continue;
                    fallback = fallback.replace(new RegExp('\\b' + escapeRegExp(raw) + '\\b', 'g'), ' ');
                }

                fallback = normalize(fallback);
                if (fallback && !isLowQualityContent(fallback)) {
                    contentLines = [fallback];
                }
            }

            if (contentLines.length > 0) {
                const joined = normalize(contentLines.join(' '));
                if (isLowQualityContent(joined) || /^(?:\d+\s*(?:秒|分|小時|天|週|月|年)|[秒分天週月年])$/.test(joined)) {
                    contentLines = [];
                }
            }
            
            return {
                author: {
                    username: authorLink ? normalize(authorLink.textContent) : null,
                    profile_url: authorLink ? authorLink.getAttribute('href') : null,
                    is_official_account: authorLink ? normalize(authorLink.textContent) === username : false,
                },
                post_url: postLink ? postLink.href : null,
                timestamp: {
                    display: timeText || null,
                    exact: timeTitle || null,
                },
                replying_to: replyTo,
                content: contentLines.join('\n') || null,
                metrics,
                raw_text: normalize(rawTextOriginal).slice(0, 500),
                has_media: hasMedia,
                image_alts: imageAlts,
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


async def run_auto_scrape_on_page(
    page,
    username,
    max_posts=50,
    max_scroll_rounds=None,
    no_growth_limit=12,
    no_growth_recovery_limit=3,
):
    replies_data = []
    seen_post_urls = set()
    stop_reason = "unknown"

    try:
        await page.wait_for_selector("time", timeout=15000)
    except Exception as e:
        print(f"⚠️ 頁面載入失敗：{e}")

    time_count_initial = await page.locator("time").count()
    print(f"✓ 找到 {time_count_initial} 個回覆")

    no_growth_rounds = 0
    scroll_round = 0
    no_growth_recoveries = 0

    while True:
        if max_scroll_rounds is not None and scroll_round >= max_scroll_rounds:
            stop_reason = "max_scroll_rounds"
            break

        scroll_round += 1
        batch_added, reached_max_posts = await collect_visible_posts(
            page, username, replies_data, seen_post_urls, max_posts
        )

        if reached_max_posts:
            stop_reason = "max_posts"
            break

        if batch_added == 0:
            no_growth_rounds += 1
            print(f"  [輪次 {scroll_round}] 沒有新貼文 (連續 {no_growth_rounds} 輪)")
        else:
            no_growth_rounds = 0
            print(f"  [輪次 {scroll_round}] 新增 {batch_added} 則，共 {len(replies_data)} 則")

        if batch_added == 0 and await detect_login_gate(page):
            stop_reason = "login_gate"
            print("\n⚠️ 偵測到登入牆！")
            break

        if no_growth_rounds >= no_growth_limit:
            can_try_recovery = (
                max_posts is not None
                and len(replies_data) < max_posts
                and no_growth_recoveries < no_growth_recovery_limit
            )

            if can_try_recovery:
                no_growth_recoveries += 1
                no_growth_rounds = 0
                print(
                    f"  [復原 {no_growth_recoveries}/{no_growth_recovery_limit}] 無增長，重新載入頁面後續抓..."
                )
                await page.reload(wait_until="domcontentloaded")
                try:
                    await page.wait_for_selector("time", timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(1200)
                continue

            stop_reason = "no_growth"
            print("  已達到無增長限制，停止爬取")
            break

        scroll_result = await scroll_feed_and_wait(page)
        before_count = scroll_result["before_count"]
        after_count = scroll_result["after_count"]
        info = scroll_result["scroll_info"]
        print(
            f"  [輪次 {scroll_round}] 滾動目標={info.get('target')} | time: {before_count} -> {after_count}"
        )

    return replies_data, stop_reason


async def login_and_scrape_auto(
    max_posts=50,
    storage_state_path="threads_storage_state.json",
):
    """Open browser, wait for user to log in and navigate, then auto-scrape."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(user_agent=build_user_agent())
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = await context.new_page()

        await page.goto("https://www.threads.net/")

        print("\n[登入後自動爬取模式]")
        print("1) 請在瀏覽器手動登入")
        print("2) 登入完成後，手動將網址導航到該品牌的 replies 頁面 (例如: https://www.threads.net/@shopee_tw/replies)")
        print("3) 當你確認已在目標的 replies 頁面後，回終端按 Enter")
        await asyncio.to_thread(input, "按 Enter 開始自動爬取... ")

        # 從當前網址解析出 username
        current_url = page.url
        print(f"\n目前網址: {current_url}")
        
        username_match = re.search(r"/@([^/]+)/replies", current_url)
        if not username_match:
            print("⚠️ 警告：當前網址不是標準的 replies 頁面，系統將嘗試使用 default_user 儲存，或可能無法順利擷取。")
            username = "default_user"
        else:
            username = username_match.group(1)
            print(f"✓ 偵測到目標帳號: {username}")

        await context.storage_state(path=storage_state_path)
        print(f"✓ 已更新並儲存登入狀態：{storage_state_path}\n")

        replies_data, stop_reason = await run_auto_scrape_on_page(
            page,
            username,
            max_posts=max_posts,
            no_growth_limit=12,
            no_growth_recovery_limit=3,
        )

        output = {
            "username": username,
            "source_url": current_url,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "max_posts": max_posts,
            "stop_reason": stop_reason,
            "posts": replies_data,
        }

        with open(f"{username}_replies.json", "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"\n🎉 爬取完成，共存取 {len(replies_data)} 則貼文至 {username}_replies.json。")
        await context.close()
        await browser.close()

# 執行
if __name__ == "__main__":
    storage_state_path = "threads_storage_state.json"
    
    print("🚀 啟動 Threads 爬蟲程式...")
    asyncio.run(
        login_and_scrape_auto(
            max_posts=1200,
            storage_state_path=storage_state_path,
        )
    )