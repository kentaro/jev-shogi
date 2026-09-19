"""Jev の各手の損失（最善手との評価値の差）を、候補手の数と並べて出す。

Fairy-Stockfish（Skill 20）で、Jev の手番の局面を評価する。
  best: その局面の最善手を指したときの評価値（Jev から見た centipawn）
  jev : Jev の手を指した後の局面を相手番で評価し、符号を反転した値
  loss = best - jev（0 以上に丸める）

  uv run python -m jev_shogi.analyze games/<対局ディレクトリ> ...
"""
import json
import os
import re
import shutil
import sys

from .local import Engine


class Evaluator(Engine):
    def score(self, moves):
        """手番側から見た評価値（cp）。詰みは ±30000 とする。"""
        self.send("position startpos" + (" moves " + " ".join(moves) if moves else ""))
        self.send(f"go movetime {self.movetime}")
        last = None
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("engine exited")
            m = re.search(r" score (cp|mate) (-?\d+)", line)
            if m:
                v = int(m.group(2))
                last = v if m.group(1) == "cp" else (30000 if v > 0 else -30000)
            if line.startswith("bestmove"):
                return last, line.split()[1]


def main():
    ev = Evaluator(shutil.which("fairy-stockfish"), 20, 300)
    rows = []
    for game in sys.argv[1:]:
        recs = [json.loads(l) for l in open(os.path.join(game, "moves.jsonl"))]
        usi = [r["usi"] for r in recs]
        for i, r in enumerate(recs):
            if r["side"] != "sente":
                continue
            best, bm = ev.score(usi[:i])
            after, _ = ev.score(usi[:i + 1])
            jev = -after if after is not None else None
            loss = max(0, best - jev) if best is not None and jev is not None else None
            rows.append({"game": os.path.basename(game), "ply": r["ply"], "candidates": r.get("candidates"),
                         "move": r["kif"], "best_usi": bm, "same_as_best": bm == r["usi"],
                         "best_cp": best, "jev_cp": jev, "loss_cp": loss})
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    ev.close()


if __name__ == "__main__":
    main()
