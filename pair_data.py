import json
import csv

def main():
    input_file = 'shopee_tw_replies.json'
    output_file = 'shopee_tw_paired.csv'

    # 1. 讀取爬蟲抓下來的原始資料
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"找不到 {input_file}，請確認檔案存在。")
        return

    posts = data.get('posts', [])
    paired_data = []

    # 2. 利用迴圈與 Sliding Window 進行「[非官方/使用者] ➔ [官方/品牌]」的連續配對
    for i in range(len(posts) - 1):
        current_post = posts[i]
        next_post = posts[i+1]
        
        # 判斷是否為「使用者貼文/留言 + 蝦皮官方回覆」的相鄰模式
        is_user = not current_post['author']['is_official_account']
        is_brand = next_post['author']['is_official_account']
        
        if is_user and is_brand:
            # 3. 整理後續回歸分析(Regression)與 WEI 計算會用到的欄位 (ETL Process)
            pair = {
                # --- 自變數預備 (X) / 控制變數 (CV) ---
                'user_name': current_post['author']['username'],
                'user_post_url': current_post['post_url'],
                'user_content': current_post['content'],
                'user_timestamp': current_post['timestamp']['exact'],
                
                'brand_reply_url': next_post['post_url'],
                'brand_reply_content': next_post['content'],
                'brand_reply_timestamp': next_post['timestamp']['exact'],
                
                # --- 應變數預備 (Y) : 用於計算 WEI (Weighted Engagement Index) ---
                'brand_reply_likes': next_post['metrics'].get('讚', {}).get('value', 0),
                'brand_reply_replies': next_post['metrics'].get('回覆', {}).get('value', 0),
                'brand_reply_reposts': next_post['metrics'].get('轉發', {}).get('value', 0),
                'brand_reply_shares': next_post['metrics'].get('分享', {}).get('value', 0),
            }
            paired_data.append(pair)

    # 4. 輸出成 CSV 供 SPSS 或 statsmodels 分析使用
    if paired_data:
        keys = paired_data[0].keys()
        # 加上 utf-8-sig 以避免 Excel 開啟 CSV 時出現亂碼
        with open(output_file, 'w', encoding='utf-8-sig', newline='') as csvfile:
            dict_writer = csv.DictWriter(csvfile, fieldnames=keys)
            dict_writer.writeheader()
            dict_writer.writerows(paired_data)

        print(f"✅ 成功提取並配對 {len(paired_data)} 筆互動數據！")
        print(f"✅ 分析用資料已儲存至：{output_file}")
    else:
        print("⚠️ 未找到任何符合條件的配對資料。")

if __name__ == '__main__':
    main()
