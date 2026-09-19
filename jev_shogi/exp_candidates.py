"""候補手の数が Jev の選択に影響するかを調べる実験。

対局ログから Jev の手番の局面を取り出し、候補を「全合法手」「エンジン上位20」「上位10」「上位5」の4通りにして、
1回のリクエスト（player の1段目と同じ質問・同じ事実）で Jev に選ばせる。
エンジン（Fairy-Stockfish Skill 20, MultiPV 500, 深さ DEPTH）で全合法手を評価しておき、
Jev が選んだ手の損失を、同じ候補からランダムに選んだ場合の期待損失と比べる。
候補の絞り込みにエンジンを使うのは、この確認実験のためだけ。

  uv run python -m jev_shogi.exp_candidates games/move_quality.jsonl --positions 60
"""
import argparse
import json
import os
import random
import re
import shutil
import statistics
import subprocess
from concurrent.futures import ThreadPoolExecutor

import shogi

from .jev import call
from .player import facts, jp, state_of

DEPTH = 12
CONDITIONS = [("all", None), ("top20", 20), ("top10", 10), ("top5", 5)]
CAP = 2000  # 詰み絡みの極端な値を丸める


class MultiPV:
    def __init__(self):
        self.p = subprocess.Popen([shutil.which("fairy-stockfish")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.send("usi")
        self.until("usiok")
        self.send("setoption name MultiPV value 500")
        self.send("isready")
        self.until("readyok")

    def send(self, s):
        self.p.stdin.write(s + "\n")
        self.p.stdin.flush()

    def until(self, tok):
        out = []
        while True:
            line = self.p.stdout.readline()
            out.append(line)
            if line.startswith(tok):
                return out

    def all_scores(self, moves):
        """合法手ごとの評価値（手番側から見た cp、詰みは ±CAP 相当に丸める）。"""
        self.send("position startpos" + (" moves " + " ".join(moves) if moves else ""))
        self.send(f"go depth {DEPTH}")
        scores = {}
        for line in self.until("bestmove"):
            m = re.search(r"depth (\d+) .*multipv \d+ score (cp|mate) (-?\d+).* pv (\S+)", line)
            if m and int(m.group(1)) == DEPTH:
                v = int(m.group(3))
                scores[m.group(4)] = max(-CAP, min(CAP, v)) if m.group(2) == "cp" else (CAP if v > 0 else -CAP)
        return scores


def ask(board, history, subset):
    me = "先手" if board.turn == shogi.BLACK else "後手"
    criteria = {m.usi(): f"{jp(board, m)}。{facts(board, m, None)[0]}" for m in subset}
    q = {"move": {"type": "choice", "criteria": criteria,
                  "instructions": f"あなたは{me}。この局面で最善の手はどれか。state の guide と threats に従う。"}}
    st, ms, r = call(state_of(board, history), q)
    return st, ms, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("quality")
    ap.add_argument("--positions", type=int, default=60)
    ap.add_argument("--games", default="games")
    ap.add_argument("--out", default="games/exp_candidates.jsonl")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    rows = [json.loads(l) for l in open(a.quality)]
    rows = [r for r in rows if r["best_cp"] is not None and abs(r["best_cp"]) < 3000 and r["candidates"] and r["candidates"] > 20]
    rng = random.Random(a.seed)
    rng.shuffle(rows)
    rows = rows[:a.positions]

    eng = MultiPV()
    tasks = []
    for r in rows:
        recs = [json.loads(l) for l in open(os.path.join(a.games, r["game"], "moves.jsonl"))]
        idx = next(i for i, x in enumerate(recs) if x["ply"] == r["ply"])
        usi = [x["usi"] for x in recs[:idx]]
        hist = [("▲" if x["side"] == "sente" else "△") + x["kif"] for x in recs[:idx]]
        b = shogi.Board()
        for u in usi:
            b.push_usi(u)
        scores = eng.all_scores(usi)
        legal = list(b.legal_moves)
        legal = [m for m in legal if m.usi() in scores]
        ranked = sorted(legal, key=lambda m: -scores[m.usi()])
        for name, k in CONDITIONS:
            subset = ranked if k is None else ranked[:k]
            if k is not None and len(ranked) <= k:
                continue
            tasks.append({"game": r["game"], "ply": r["ply"], "cond": name, "n": len(subset), "sfen": b.sfen(),
                          "hist": hist, "subset": [m.usi() for m in subset], "scores": {m.usi(): scores[m.usi()] for m in subset},
                          "best_all": scores[ranked[0].usi()]})
    eng.send("quit")

    def run(t):
        b = shogi.Board(t["sfen"])
        subset = [shogi.Move.from_usi(u) for u in t["subset"]]
        st, ms, r = ask(b, t["hist"], subset)
        if st != 200:
            return {**t, "status": st}
        pick = r["answers"]["move"]["choice"]
        sc = t["scores"]
        best = max(sc.values())
        return {"game": t["game"], "ply": t["ply"], "cond": t["cond"], "n": t["n"], "status": st, "ms": round(ms),
                "input_tokens": r["usage"]["input_tokens"], "pick": pick,
                "pick_is_set_best": sc[pick] == best, "loss": best - sc[pick],
                "loss_vs_all_best": t["best_all"] - sc[pick],
                "random_loss": statistics.mean(best - v for v in sc.values()),
                "pick_rank_pct": sum(v > sc[pick] for v in sc.values()) / len(sc)}

    with ThreadPoolExecutor(3) as ex, open(a.out, "w") as f:
        for res in ex.map(run, tasks):
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
            f.flush()

    res = [json.loads(l) for l in open(a.out)]
    res = [r for r in res if r.get("status") == 200]
    print(json.dumps({"positions": len(rows), "requests": len(res)}), flush=True)
    for name, _ in CONDITIONS:
        x = [r for r in res if r["cond"] == name]
        if not x:
            continue
        print(json.dumps({
            "cond": name, "n_positions": len(x), "candidates_median": statistics.median(r["n"] for r in x),
            "set_best_rate": round(sum(r["pick_is_set_best"] for r in x) / len(x), 3),
            "chance_best_rate": round(statistics.mean(1 / r["n"] for r in x), 3),
            "jev_loss_median": statistics.median(r["loss"] for r in x),
            "random_loss_median": round(statistics.median(r["random_loss"] for r in x)),
            "jev_rank_pct_mean": round(statistics.mean(r["pick_rank_pct"] for r in x), 3),
            "loss_vs_all_best_median": statistics.median(r["loss_vs_all_best"] for r in x),
        }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
