# jev-shogi

Jev（TypeSafe AI の判定特化モデル）に将棋を指させる実験。Jev はロリポップ！AIゲートウェイ経由で呼ぶ。

## 構成
- `jev_shogi/jev.py` — ゲートウェイの `POST /v1/systemone` 呼び出し。キーは `AI_GATEWAY_API_KEY_FILE` のファイルから読む
- `jev_shogi/player.py` — 局面から質問を組む。1段目で全合法手（1〜2手先の事実付き）から上位6手に絞り、2段目で指した後の局面を並べて選ばせる。判断はすべて Jev、コードはルール上の事実だけ
- `jev_shogi/local.py` — 手元の Fairy-Stockfish（USI、brew）と対局。棋譜・1手ごとの記録・動画を `games/<日時>-skill<N>/` に書く
- `jev_shogi/render.py` — moves.jsonl から動画を作り直す
- `jev_shogi/calibrate.py` — 相手の手と最強設定の手の一致率で相手の強さを測る

## 実行
```
brew install fairy-stockfish
AI_GATEWAY_API_KEY_FILE=<キーのファイル> uv run python -m jev_shogi.local --skill -10 --movetime 100 --video
```

## 禁止事項
- **Web の対局サイト（lishogi 等）で Jev に指させない。** lishogi は「コンピューターの力を借りての対局は禁止」と明記しており、AI 相手の例外も書かれていない（2026-09-18 に2局試して中止）。対局相手はローカルで動かす
- API キーの値をログ・コミット・標準出力に出さない
- 同時に多数の対局を回さない。ゲートウェイは同時10本以上で 503/500 が出る
