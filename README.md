# jev-shogi

判定特化モデル Jev に将棋を指させる。毎手、合法手すべてを選択肢として1回の推論で選ばせる（探索なし）。
形勢判断と自玉の危険度も同じリクエストで返させる。

対局相手は手元の Fairy-Stockfish。棋譜（KIF）、1手ごとのレイテンシ・トークン数・コスト、対局動画を `games/` に残す。

```
AI_GATEWAY_API_KEY_FILE=<キーのファイル> uv run python -m jev_shogi.local --skill -10 --movetime 100 --video
```
