import os, httpx, json


key = os.environ.get("My_OPENAI_API_KEY")
payload = {
    "model": "gpt-5-mini",
    "messages": [{"role":"user","content":"say hi"}],
    "max_completion_tokens": 50,
    "temperature": 0.7,
}


r = httpx.post("https://api.openai.com/v1/chat/completions",
               headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=60)


print("status:", r.status_code)
print(r.text)
