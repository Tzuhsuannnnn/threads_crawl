import asyncio
from playwright.async_api import async_playwright
import json

async def scrape_threads_replies(username):
    async with async_playwright() as p:
        # 啟動瀏覽器 (headless=False 可以讓你看到爬取過程)
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # 直接進入「回覆」標籤頁
        url = f"https://www.threads.net/@{username}/replies"
        print(f"正在前往: {url}")
        await page.goto(url)

        # 等待內容載入
        try:
            await page.wait_for_selector('div[dir="auto"]', timeout=10000)
        except:
            print("載入逾時，可能是需要登入或是頁面結構改變")

        replies_data = []
        seen_texts = set()

        # 模擬滾動以加載更多留言
        for _ in range(10):  # 調整循環次數以決定爬取深度
            # 抓取所有看起來像是留言內容的元素
            # Threads 的留言通常在具有 dir="auto" 的 span 或 div 內
            elements = await page.query_selector_all('span[dir="auto"]')
            
            for el in elements:
                text = await el.inner_text()
                # 過濾掉太短的、重複的，或是明顯是按鈕文字的內容
                if text and text not in seen_texts and len(text) > 1:
                    # 這裡可以根據需求加入更精密的判斷，例如判斷父元素是否包含帳號名
                    print(f"發現留言: {text}")
                    replies_data.append({
                        "author": username,
                        "content": text
                    })
                    seen_texts.add(text)

            # 向下滾動
            await page.evaluate("window.scrollBy(0, 800)")
            await asyncio.sleep(2) # 等待 API 加載新內容

        # 存檔
        with open(f"{username}_replies.json", "w", encoding="utf-8") as f:
            json.dump(replies_data, f, ensure_ascii=False, indent=4)
        
        print(f"爬取完成，共存取 {len(replies_data)} 條留言。")
        await browser.close()

# 執行
if __name__ == "__main__":
    asyncio.run(scrape_threads_replies("shopee_tw"))