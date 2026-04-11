import asyncio
import json
import re
from datetime import datetime, timezone

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


async def extract_post_card(time_locator, username):
    return await time_locator.evaluate(
        r"""
        (node, username) => {
            function normalize(value) {
                return (value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
            }

            function getMetricButtons(root) {
                return Array.from(root.querySelectorAll('div[role="button"]'))
                    .filter((button) => {
                        const labelNode = button.querySelector('svg[aria-label]');
                        if (!labelNode) {
                            return false;
                        }

                        return ['Like', 'Comment', 'Repost', 'Share'].includes(labelNode.getAttribute('aria-label'));
                    });
            }

            function getCardRoot(startNode) {
                let current = startNode;
                while (current && current !== document.body) {
                    const metricButtons = getMetricButtons(current);
                    const metricLabels = new Set(
                        metricButtons
                            .map((button) => button.querySelector('svg[aria-label]'))
                            .filter(Boolean)
                            .map((svg) => svg.getAttribute('aria-label'))
                    );

                    if (
                        current.querySelector('a[href^="/@"]') &&
                        current.querySelector('time') &&
                        current.querySelectorAll('time').length === 1 &&
                        metricLabels.has('Like') &&
                        metricLabels.has('Comment') &&
                        metricLabels.has('Repost') &&
                        metricLabels.has('Share')
                    ) {
                        return current;
                    }

                    current = current.parentElement;
                }

                return null;
            }

            const timeElement = node;
            const cardRoot = getCardRoot(timeElement);
            if (!cardRoot) {
                return null;
            }

            const authorLinks = Array.from(cardRoot.querySelectorAll('a[href^="/@"]'));
            const authorLink = authorLinks.find((link) => normalize(link.textContent)) || authorLinks[0] || null;
            const timeNode = cardRoot.querySelector('time');
            const postLink = timeNode ? timeNode.closest('a[href*="/post/"]') : null;
            const metricButtons = getMetricButtons(cardRoot);
            const lines = (cardRoot.innerText || '')
                .replace(/\u00a0/g, ' ')
                .split('\n')
                .map((line) => normalize(line))
                .filter(Boolean);

            const timeText = timeNode ? normalize(timeNode.innerText) : '';
            const timeIndex = timeText ? lines.indexOf(timeText) : -1;
            const replyLine = lines.find((line) => /^Replying to @/.test(line)) || null;
            const replyTo = replyLine ? replyLine.replace(/^Replying to\s+/, '') : null;

            const metrics = {};
            for (const button of metricButtons) {
                const labelNode = button.querySelector('svg[aria-label]');
                const label = labelNode ? labelNode.getAttribute('aria-label') : null;
                if (!label) {
                    continue;
                }

                const count = normalize(button.innerText);
                metrics[label.toLowerCase()] = {
                    label,
                    raw: count || null,
                };
            }

            const metricValues = metricButtons
                .map((button) => normalize(button.innerText))
                .filter(Boolean);

            const metricsStart = metricValues.length > 0 ? Math.max(lines.length - metricValues.length, 0) : lines.length;
            const bodyLines = timeIndex >= 0 ? lines.slice(timeIndex + 1, metricsStart) : lines.slice(1, metricsStart);
            const contentLines = bodyLines.filter((line) => line !== 'Translate' && line !== replyLine);

            return {
                author: {
                    username: authorLink ? normalize(authorLink.textContent) : null,
                    profile_url: authorLink ? authorLink.getAttribute('href') : null,
                    is_official_account: authorLink ? normalize(authorLink.textContent) === username : false,
                },
                post_url: postLink ? postLink.href : null,
                timestamp: {
                    display: timeText || null,
                    exact: timeNode ? timeNode.getAttribute('title') : null,
                },
                replying_to: replyTo,
                content: contentLines.join('\n') || null,
                metrics,
                raw_text: normalize(cardRoot.innerText),
            };
        }
        """,
        username,
    )

async def scrape_threads_replies(username):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        url = f"https://www.threads.net/@{username}/replies"
        print(f"正在前往: {url}")
        await page.goto(url)

        try:
            await page.wait_for_selector("time", timeout=10000)
        except:
            print("載入逾時，可能是需要登入或是頁面結構改變")

        replies_data = []
        seen_post_urls = set()

        no_growth_rounds = 0
        for _ in range(15):
            time_count = await page.locator("time").count()
            batch_added = 0

            for index in range(time_count):
                time_locator = page.locator("time").nth(index)
                try:
                    record = await extract_post_card(time_locator, username)
                except:
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
                print(f"發現貼文: {record['author']['username']} | {record['timestamp']['display']} | {record['post_url']}")

            if batch_added == 0:
                no_growth_rounds += 1
            else:
                no_growth_rounds = 0

            if no_growth_rounds >= 2:
                break

            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1800)

        output = {
            "username": username,
            "source_url": url,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "posts": replies_data,
        }

        with open(f"{username}_replies.json", "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        
        print(f"爬取完成，共存取 {len(replies_data)} 則貼文。")
        await browser.close()

# 執行
if __name__ == "__main__":
    asyncio.run(scrape_threads_replies("shopee_tw"))
