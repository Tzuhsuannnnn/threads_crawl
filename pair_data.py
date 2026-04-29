import json
import csv

def filter_alts(alts):
    """
    過濾掉包含『大頭貼照』字眼的 alt 描述，只保留內容描述。
    """
    if not alts:
        return []
    
    # 定義要排除的關鍵字
    exclude_keywords = ["的大頭貼照", "用戶大頭貼照"]
    
    # 只要 alt 字串中包含任一排除關鍵字，就不保留
    return [alt for alt in alts if not any(k in alt for k in exclude_keywords)]

def main():
    input_file = 'netflixtw_replies.json'
    output_file = 'netflixtw_paired.csv'

    # 1. 讀取原始資料
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"找不到 {input_file}，請確認檔案存在。")
        return

    posts = data.get('posts', [])
    paired_data = []

    # 2. 進行配對處理
    for i in range(len(posts) - 1):
        current_post = posts[i]
        next_post = posts[i+1]

        # 判斷是否為「使用者」接「官方品牌」
        is_user = not current_post['author']['is_official_account']
        is_brand = next_post['author']['is_official_account']

        if is_user and is_brand:
            # 處理使用者貼文的圖片描述
            user_alts_cleaned = filter_alts(current_post.get('image_alts', []))
            # 處理品牌回覆的圖片描述
            brand_alts_cleaned = filter_alts(next_post.get('image_alts', []))

            # 3. 整理 CSV 欄位（包含所有 metrics 與媒體狀態）
            pair = {
                # --- 原貼文資料 (User Post) ---
                'user_name': current_post['author']['username'],
                'user_post_url': current_post['post_url'],
                'user_content': current_post['content'],
                'user_timestamp': current_post['timestamp']['exact'],
                'user_replying_to': current_post.get('replying_to', None),
                'user_image_alts': ' | '.join(user_alts_cleaned),

                # 原貼文 Metrics (Label 分開呈現)
                'user_likes': current_post['metrics'].get('讚', {}).get('value', 0),
                'user_replies': current_post['metrics'].get('回覆', {}).get('value', 0),
                'user_reposts': current_post['metrics'].get('轉發', {}).get('value', 0),
                'user_shares': current_post['metrics'].get('分享', {}).get('value', 0),

                # --- 品牌回覆資料 (Brand Reply) ---
                'brand_reply_url': next_post['post_url'],
                'brand_reply_content': next_post['content'],
                'brand_reply_timestamp': next_post['timestamp']['exact'],
                'brand_replying_to': next_post.get('replying_to', None),
                'brand_reply_image_alts': ' | '.join(brand_alts_cleaned),

                # 品牌回覆 Metrics (Label 分開呈現)
                'brand_reply_likes': next_post['metrics'].get('讚', {}).get('value', 0),
                'brand_reply_replies': next_post['metrics'].get('回覆', {}).get('value', 0),
                'brand_reply_reposts': next_post['metrics'].get('轉發', {}).get('value', 0),
                'brand_reply_shares': next_post['metrics'].get('分享', {}).get('value', 0),
            }
            paired_data.append(pair)

    # 4. 輸出 CSV
    if paired_data:
        keys = paired_data[0].keys()
        # 使用 utf-8-sig 以確保 Excel 開啟時中文不會亂碼
        with open(output_file, 'w', encoding='utf-8-sig', newline='') as csvfile:
            dict_writer = csv.DictWriter(csvfile, fieldnames=keys)
            dict_writer.writeheader()
            dict_writer.writerows(paired_data)

        print(f"✅ 成功配對 {len(paired_data)} 筆數據！")
        print(f"✅ 圖片描述已過濾，僅保留內容相關描述。")
        print(f"✅ 檔案已儲存至：{output_file}")
    else:
        print("⚠️ 未找到任何符合「使用者+品牌」順序的配對資料。")

if __name__ == '__main__':
    main()