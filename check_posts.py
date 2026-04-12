import json
from pathlib import Path

data = json.loads(Path('shopee_tw_replies.json').read_text())

print(f"共 {len(data['posts'])} 則貼文")
print(f"Stop reason: {data['stop_reason']}")
print(f"Scraped at: {data['scraped_at']}\n")

print("貼文内容:")
for i, p in enumerate(data['posts'], 1):
    print(f"{i:2d}. {p['author']['username']:20s} | {p['timestamp']['display']:6s} | {p['post_url']}")
