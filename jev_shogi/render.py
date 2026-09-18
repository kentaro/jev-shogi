"""moves.jsonl から対局動画を作り直す（Jev は呼ばない）。

  uv run python -m jev_shogi.render games/<対局ディレクトリ>
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import shogi

from .jev import USD_PER_INPUT_TOKEN
from .local import Renderer


def main():
    game = sys.argv[1]
    recs = [json.loads(l) for l in open(os.path.join(game, "moves.jsonl"))]
    summary = json.load(open(os.path.join(game, "summary.json")))
    skill = int(game.rstrip("/").rsplit("skill", 1)[1])  # ディレクトリ名 <日時>[-v2]-skill<N>
    label = {-20: "最弱", -10: "弱", 0: "中", 10: "強", 20: "最強"}.get(skill, str(skill))
    sp = os.path.join(game, "opponent_strength.json")
    rate = f"・最強設定との一致率{json.load(open(sp))['match_rate'] * 100:.0f}%" if os.path.exists(sp) else ""
    rnd = Renderer(f"▲Jev（判定特化AI）vs △将棋エンジン［{label}{rate}］")
    frames = tempfile.mkdtemp()
    fi = 0
    b = shogi.Board()
    last, panel, tokens = None, [("対局開始", None, None)], 0

    def snap(n):
        nonlocal fi
        img = rnd.draw(b, last, panel)
        for _ in range(n):
            img.save(f"{frames}/f{fi:06d}.png")
            fi += 1

    snap(24)
    for r in recs:
        mv = shogi.Move.from_usi(r["usi"])
        if r["side"] == "sente":
            tokens += r["input_tokens"] or 0
            panel = [(f"{r['ply']}手目 ▲Jev: {r['kif']}", rnd.fb, (255, 255, 255)),
                     (f"合法手 {r['candidates']} 手 → 上位 {len(r.get('shortlist') or [])} 手を比較", None, (140, 220, 255)),
                     (f"判断 {r['jev_ms']} ms（{r.get('requests', 1)}リクエスト）入力 {r['input_tokens']} トークン", rnd.fs, (140, 220, 255)),
                     ("", None, None), ("最終候補（確率）", rnd.fs, (170, 170, 170))]
            panel += [(f"{k}　{v * 100:.0f}%", None, (255, 230, 150) if i == 0 else None) for i, (k, v) in enumerate(r["top3"])]
            panel += [("", None, None), (f"形勢（Jev の見立て）: {r['eval']:.2f}", None, None),
                      ("0=後手勝勢 2=互角 4=先手勝勢", rnd.fs, (150, 150, 150)),
                      (f"自玉の危険度: {r['danger'] * 100:.0f}%", None, None),
                      ("", None, None),
                      (f"累計 入力 {tokens:,} トークン", rnd.fs, (170, 170, 170)),
                      (f"通常単価換算 ${tokens * USD_PER_INPUT_TOKEN:.5f}（今週は無料）", rnd.fs, (170, 170, 170))]
        else:
            panel = [(f"{r['ply']}手目 △相手: {r['kif']}", rnd.fb, (200, 200, 200))] + panel[1:]
        b.push(mv)
        last = mv
        snap(12)
    mated = "▲Jev" if b.is_checkmate() and b.turn == shogi.BLACK else "△相手" if b.is_checkmate() else None
    panel = [(summary["result"], rnd.fb, (255, 230, 150)),
             (f"{mated}の玉に王手がかかり、逃げ場も受けもない" if mated else "", None, (255, 150, 150)),
             ("", None, None),
             (f"Jev の判断 {summary['jev_moves']} 回　p50 {summary['jev_ms_p50']} ms", None, None),
             (f"入力 {summary['input_tokens']:,} トークン", None, None),
             (f"通常単価換算 ${summary['cost_usd_list_price']:.5f}", None, None),
             ("（2026-09-18〜24 は無料）", rnd.fs, (150, 150, 150))]
    snap(96)
    out = os.path.join(game, "game.mp4")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", "24", "-i", f"{frames}/f%06d.png",
                    "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out], check=True)
    shutil.rmtree(frames)
    print(out)


if __name__ == "__main__":
    main()
