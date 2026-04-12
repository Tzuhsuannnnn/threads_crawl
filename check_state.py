import json
from pathlib import Path

state_file = Path('threads_storage_state.json')
if state_file.exists():
    state = json.loads(state_file.read_text())
    print(f"✓ Cookies: {len(state.get('cookies', []))}")
    print(f"✓ Origins with localStorage: {len(state.get('origins', []))}")
    
    # 按域名分类 cookies
    cookies_by_domain = {}
    for c in state.get('cookies', []):
        domain = c.get('domain', 'unknown')
        if domain not in cookies_by_domain:
            cookies_by_domain[domain] = []
        cookies_by_domain[domain].append(c['name'])
    
    print(f"\nCookies by domain:")
    for domain, names in sorted(cookies_by_domain.items()):
        print(f"  {domain}: {', '.join(names)}")
    
    # 检查关键的 cookies
    all_cookies = {c['name']: c for c in state.get('cookies', [])}
    print(f"\n检查认证 cookies:")
    auth_keys = ['ds_user_id', 'sessionid', 'ig_did', 'csrftoken', 'datr', 'mid']
    for name in auth_keys:
        if name in all_cookies:
            value = all_cookies[name]['value']
            print(f"  ✓ {name}: {value[:30]}{'...' if len(value) > 30 else ''}")
        else:
            print(f"  ✗ {name}: 不存在")
else:
    print("✗ threads_storage_state.json 不存在")

