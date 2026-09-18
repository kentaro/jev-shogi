"""ロリポップ！AIゲートウェイ経由で Jev (typesafe/jev-latest) を呼ぶ。キーは AI_GATEWAY_API_KEY_FILE のファイルから読む。"""
import json
import os
import time
import urllib.error
import urllib.request

URL = os.environ.get("AI_GATEWAY_BASE_URL", "https://ai-gateway.lolipop.jp") + "/v1/systemone"
MODEL = "typesafe/jev-latest"
# 入力 $0.042 / 100万トークン、出力は無料（2026-09-18 のお知らせ。9/18〜9/24 は無料キャンペーン）
USD_PER_INPUT_TOKEN = 0.042 / 1_000_000


def _key():
    return open(os.environ["AI_GATEWAY_API_KEY_FILE"]).read().strip()


def call(state, questions, timeout=60):
    """(status, ms, response) を返す。失敗時は response に error を入れる。"""
    body = json.dumps({"model": MODEL, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {_key()}", "Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data, status = json.load(r), r.status
    except urllib.error.HTTPError as e:
        data, status = {"error": e.read().decode()[:300]}, e.code
    except (TimeoutError, urllib.error.URLError) as e:
        data, status = {"error": repr(e)[:300]}, 0
    return status, (time.perf_counter() - t0) * 1000, data
