"""相手エンジンの強さを「最強設定と同じ手を指した割合」で測る。

対局ログの後手の各局面を、Skill_Level 20・1手1秒の Fairy-Stockfish に解かせ、実際の手と一致した割合を出す。

  uv run python -m jev_shogi.calibrate games/<対局ディレクトリ> ...
"""
import json
import os
import shutil
import sys

from .local import Engine

LABEL = {-20: "最弱", -10: "弱", 0: "中", 10: "強", 20: "最強（手加減なし）"}


def main():
    ref = Engine(shutil.which("fairy-stockfish"), 20, 1000)
    for game in sys.argv[1:]:
        recs = [json.loads(l) for l in open(os.path.join(game, "moves.jsonl"))]
        usi = [r["usi"] for r in recs]
        hit = n = 0
        for i, r in enumerate(recs):
            if r["side"] != "gote":
                continue
            n += 1
            hit += ref.best(usi[:i]) == r["usi"]
        out = {"game": os.path.basename(game), "gote_moves": n, "match_with_strongest": hit,
               "match_rate": round(hit / n, 3) if n else None}
        json.dump(out, open(os.path.join(game, "opponent_strength.json"), "w"), ensure_ascii=False, indent=1)
        print(json.dumps(out, ensure_ascii=False), flush=True)
    ref.close()


if __name__ == "__main__":
    main()
