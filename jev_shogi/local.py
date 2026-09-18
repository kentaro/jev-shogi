"""手元で Jev（先手）と Fairy-Stockfish（後手、USI）を対局させ、棋譜・1手ごとの記録・動画を残す。

  AI_GATEWAY_API_KEY_FILE=... uv run python -m jev_shogi.local --skill -20 --video

出力（games/<日時>-skill<N>/）:
  game.kif       棋譜
  moves.jsonl    1手ごとの記録（Jev の手は候補数・上位3手と確率・形勢・レイテンシ・トークン・コスト）
  summary.json   対局結果と合計（トークン・コスト・レイテンシ）
  game.mp4       盤面の動画（--video のとき）
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import tempfile

import shogi
import shogi.KIF
from PIL import Image, ImageDraw, ImageFont

from .jev import MODEL, USD_PER_INPUT_TOKEN
from .player import decide, jp

FONT = "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc"
FONT_BOLD = "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc"
KANJI = {"P": "歩", "L": "香", "N": "桂", "S": "銀", "G": "金", "B": "角", "R": "飛", "K": "玉",
         "+P": "と", "+L": "杏", "+N": "圭", "+S": "全", "+B": "馬", "+R": "龍"}
HAND_ORDER = [shogi.ROOK, shogi.BISHOP, shogi.GOLD, shogi.SILVER, shogi.KNIGHT, shogi.LANCE, shogi.PAWN]


class Engine:
    def __init__(self, path, skill, movetime):
        self.p = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.movetime = movetime
        self.send("usi")
        self.until("usiok")
        self.send(f"setoption name Skill_Level value {skill}")
        self.send("isready")
        self.until("readyok")
        self.send("usinewgame")

    def send(self, s):
        self.p.stdin.write(s + "\n")
        self.p.stdin.flush()

    def until(self, tok):
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("engine exited")
            if line.startswith(tok):
                return line.strip()

    def best(self, moves):
        self.send("position startpos" + (" moves " + " ".join(moves) if moves else ""))
        self.send(f"go movetime {self.movetime}")
        return self.until("bestmove").split()[1]

    def close(self):
        self.send("quit")
        self.p.wait(timeout=5)


class Renderer:
    SQ = 64

    def __init__(self, title):
        self.title = title
        self.f = ImageFont.truetype(FONT, 20)
        self.fs = ImageFont.truetype(FONT, 16)
        self.fb = ImageFont.truetype(FONT_BOLD, 26)
        self.piece = ImageFont.truetype(FONT_BOLD, 34)

    def draw(self, b, last, info):
        S = self.SQ
        ox, oy = 40, 130
        img = Image.new("RGB", (1280, 820), (245, 240, 228))
        d = ImageDraw.Draw(img)
        d.text((40, 20), self.title, font=self.fb, fill=(30, 30, 30))
        d.rectangle([ox, oy, ox + 9 * S, oy + 9 * S], fill=(224, 186, 120), outline=(60, 40, 20), width=3)
        for i in range(10):
            d.line([ox + i * S, oy, ox + i * S, oy + 9 * S], fill=(60, 40, 20), width=1)
            d.line([ox, oy + i * S, ox + 9 * S, oy + i * S], fill=(60, 40, 20), width=1)
        for i in range(9):
            d.text((ox + i * S + S / 2 - 6, oy - 26), "９８７６５４３２１"[i], font=self.fs, fill=(80, 80, 80))
            d.text((ox + 9 * S + 8, oy + i * S + S / 2 - 10), "一二三四五六七八九"[i], font=self.fs, fill=(80, 80, 80))
        if last is not None:
            r, c = divmod(last.to_square, 9)
            d.rectangle([ox + c * S + 2, oy + r * S + 2, ox + (c + 1) * S - 2, oy + (r + 1) * S - 2], fill=(240, 210, 140))
        for sq in shogi.SQUARES:
            p = b.piece_at(sq)
            if not p:
                continue
            r, c = divmod(sq, 9)
            sym = p.symbol()
            ch = KANJI[sym.upper()]
            col = (180, 20, 20) if sym.startswith("+") else (20, 20, 20)
            tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text((S / 2, S / 2), ch, font=self.piece, fill=col, anchor="mm")
            if p.color == shogi.WHITE:
                tile = tile.rotate(180)
            img.paste(tile, (ox + c * S, oy + r * S), tile)

        def hand(color):
            items = [f"{KANJI[shogi.PIECE_SYMBOLS[pt].upper()]}{n if n > 1 else ''}"
                     for pt in HAND_ORDER for n in [b.pieces_in_hand[color].get(pt, 0)] if n]
            return " ".join(items) or "なし"

        d.text((ox, oy + 9 * S + 16), f"▲Jev 持ち駒: {hand(shogi.BLACK)}", font=self.f, fill=(30, 30, 30))
        d.text((ox, oy - 52), f"△相手 持ち駒: {hand(shogi.WHITE)}", font=self.f, fill=(30, 30, 30))

        x = 700
        d.rectangle([x - 20, 130, 1250, 800], fill=(34, 36, 42))
        y = 150
        for text, font, color in info:
            d.text((x, y), text, font=font or self.f, fill=color or (230, 230, 230))
            y += (font or self.f).size + 12
        return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=shutil.which("fairy-stockfish") or "fairy-stockfish")
    ap.add_argument("--skill", type=int, default=-20, help="Fairy-Stockfish の Skill_Level（-20〜20）")
    ap.add_argument("--movetime", type=int, default=100, help="エンジンの1手あたりの思考時間 ms")
    ap.add_argument("--max-plies", type=int, default=256)
    ap.add_argument("--out", default="games")
    ap.add_argument("--video", action="store_true")
    a = ap.parse_args()

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(a.out, f"{stamp}-skill{a.skill}")
    os.makedirs(out, exist_ok=True)
    log = open(os.path.join(out, "moves.jsonl"), "w")
    started = datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    eng = Engine(a.engine, a.skill, a.movetime)
    opp = f"Fairy-Stockfish 14.0.1 (Skill_Level {a.skill}, {a.movetime}ms/手)"
    rnd = Renderer(f"Jev（判定特化モデル）▲ vs △ {opp}")
    frames = tempfile.mkdtemp() if a.video else None
    fi = 0

    b = shogi.Board()
    usi, history = [], []
    tot = {"requests": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0, "ms": []}
    last, panel = None, [("対局開始", None, None)]
    prev_mine = None
    result = None

    def snap(n):
        nonlocal fi
        if not frames:
            return
        img = rnd.draw(b, last, panel)
        for _ in range(n):
            img.save(f"{frames}/f{fi:06d}.png")
            fi += 1

    snap(24)
    while len(usi) < a.max_plies and not b.is_game_over():
        ply = len(usi) + 1
        if b.turn == shogi.BLACK:
            d = decide(b, history, prev_mine)
            tot["requests"] += d.get("requests", 1)
            if d["status"] != 200:
                tot["failed"] += 1
                print(json.dumps({"ply": ply, "error": d["status"], "detail": d["response"]}, ensure_ascii=False), flush=True)
                if tot["failed"] >= 5:
                    result = "Jev の呼び出しが5回失敗したので中断"
                    break
                continue
            ans = d["response"]["answers"]
            mv = shogi.Move.from_usi(d["final"])
            u = d["usage"]
            tot["input_tokens"] += u.get("input_tokens", 0)
            tot["output_tokens"] += u.get("output_tokens", 0)
            tot["ms"].append(d["ms"])
            name = jp(b, mv)
            st2 = d.get("stage2") or {}
            top = sorted(st2.get("probabilities", {}).items(), key=lambda x: -x[1])[:3] or                 [[shogi.KIF.Exporter.kif_move_from(k, b), v] for k, v in sorted(ans["move"]["probabilities"].items(), key=lambda x: -x[1])[:3]]
            rec = {"ply": ply, "side": "sente", "player": "Jev", "usi": mv.usi(), "kif": name,
                   "jev_ms": round(d["ms"]), "jev_ms_stage": [round(x) for x in d.get("ms_stage", [d["ms"]])],
                   "requests": d.get("requests", 1),
                   "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                   "cost_usd": round(u.get("input_tokens", 0) * USD_PER_INPUT_TOKEN, 8),
                   "candidates": d["candidates"], "truncated": d["truncated"],
                   "stage1_choice": jp(b, shogi.Move.from_usi(d["stage1_choice"])), "shortlist": d.get("shortlist"),
                   "stage2_evals": st2.get("evals"), "facts": d["criteria"].get(mv.usi()),
                   "top3": [list(t) for t in top], "eval": ans["eval"]["score"], "danger": ans["danger"]["noul"]}
            panel = [(f"{ply}手目 ▲Jev: {name}", rnd.fb, (255, 255, 255)),
                     (f"合法手 {d['candidates']} 手 → 上位 {len(d.get('shortlist') or [])} 手を比較", None, (140, 220, 255)),
                     (f"判断 {d['ms']:.0f} ms（{d.get('requests', 1)}リクエスト）入力 {u.get('input_tokens')} トークン", rnd.fs, (140, 220, 255)),
                     ("", None, None), ("最終候補（確率）", rnd.fs, (170, 170, 170))]
            panel += [(f"{k}　{v * 100:.0f}%", None, (255, 230, 150) if i == 0 else None) for i, (k, v) in enumerate(top)]
            ev = ans["eval"]["score"]
            panel += [("", None, None), (f"形勢（Jev の見立て）: {ev:.2f}", None, None),
                      ("0=後手勝勢 2=互角 4=先手勝勢", rnd.fs, (150, 150, 150)),
                      (f"自玉の危険度: {ans['danger']['noul'] * 100:.0f}%", None, None),
                      ("", None, None),
                      (f"累計 入力 {tot['input_tokens']:,} トークン", rnd.fs, (170, 170, 170)),
                      (f"通常単価換算 ${tot['input_tokens'] * USD_PER_INPUT_TOKEN:.5f}（今週は無料）", rnd.fs, (170, 170, 170))]
            prev_mine = mv
            history.append("▲" + name)
            print(json.dumps(rec, ensure_ascii=False), flush=True)
        else:
            u_ = eng.best(usi)
            if u_ in ("resign", "win"):
                result = "後手（Fairy-Stockfish）の投了" if u_ == "resign" else u_
                break
            mv = shogi.Move.from_usi(u_)
            name = jp(b, mv)
            rec = {"ply": ply, "side": "gote", "player": opp, "usi": u_, "kif": name}
            panel = panel[:1] + [(f"{ply}手目 △Fairy-Stockfish: {name}", None, (200, 200, 200))] + panel[1:]
            history.append("△" + name)
        log.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log.flush()
        b.push(mv)
        usi.append(mv.usi())
        last = mv
        snap(12)
    eng.close()

    if b.is_checkmate():
        winner = "後手" if b.turn == shogi.BLACK else "先手"
        result_line = f"まで{len(usi)}手で{winner}の勝ち"
    elif result:
        result_line = f"まで{len(usi)}手（{result}）"
    elif b.is_fourfold_repetition():
        result_line = f"まで{len(usi)}手で千日手"
    else:
        result_line = f"まで{len(usi)}手（{a.max_plies}手で打ち切り）"
    panel = [(result_line, rnd.fb, (255, 230, 150))]
    snap(72)

    ended = datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    bk = shogi.Board()
    lines = [f"開始日時：{started}", f"終了日時：{ended}", "手合割：平手",
             f"先手：Jev ({MODEL}, ロリポップ！AIゲートウェイ経由)", f"後手：{opp}", "手数----指手---------消費時間--"]
    for i, u_ in enumerate(usi, 1):
        lines.append(f"{i:>4} {shogi.KIF.Exporter.kif_move_from(u_, bk)}")
        bk.push_usi(u_)
    lines.append(result_line)
    open(os.path.join(out, "game.kif"), "w", encoding="utf-8").write("\n".join(lines) + "\n")

    ms = sorted(tot["ms"])
    summary = {"result": result_line, "plies": len(usi), "opponent": opp,
               "jev_moves": len(ms), "jev_requests": tot["requests"], "jev_failed": tot["failed"],
               "input_tokens": tot["input_tokens"], "output_tokens": tot["output_tokens"],
               "cost_usd_list_price": round(tot["input_tokens"] * USD_PER_INPUT_TOKEN, 6),
               "cost_note": "2026-09-18〜24 は無料キャンペーン。通常単価（入力 $0.042/100万トークン、出力無料）での換算",
               "jev_ms_p50": round(ms[len(ms) // 2]) if ms else None,
               "jev_ms_p95": round(ms[min(len(ms) - 1, int(len(ms) * 0.95))]) if ms else None,
               "jev_ms_max": round(ms[-1]) if ms else None}
    json.dump(summary, open(os.path.join(out, "summary.json"), "w"), ensure_ascii=False, indent=1)
    if frames:
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", "24", "-i", f"{frames}/f%06d.png",
                        "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                        os.path.join(out, "game.mp4")], check=True)
        shutil.rmtree(frames)
    print(json.dumps({"out": out, **summary}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
