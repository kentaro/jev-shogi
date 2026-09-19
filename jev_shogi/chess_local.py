"""チェス版。将棋の2段階版（player.py）と同じ仕組みで、Jev（白）と Fairy-Stockfish（黒、チェスモード）を対局させる。

1段目: 合法手すべてを choice の候補にし、1〜2手先の事実を説明に付ける（将棋版と同じ7種類）
2段目: 上位6手を指した後の局面を並べて選ばせる
質問文・説明文は将棋版と同じく日本語。

  AI_GATEWAY_API_KEY_FILE=... uv run python -m jev_shogi.chess_local --skill -10
  uv run python -m jev_shogi.chess_local --calibrate games/chess-...   # 相手の一致率
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess

import chess
import chess.pgn

from .jev import MODEL, USD_PER_INPUT_TOKEN, call

VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100}
NAME = {chess.PAWN: "ポーン", chess.KNIGHT: "ナイト", chess.BISHOP: "ビショップ", chess.ROOK: "ルーク", chess.QUEEN: "クイーン", chess.KING: "キング"}
SHORTLIST = 6
GUIDE = ("チェスの基本方針: 序盤は中央を支配し、駒を展開してキャスリングでキングを安全にする。"
         "同じ駒を行ったり来たりさせる手（手損）は避ける。駒をタダで取られる手、他の駒をタダで取られる状態にする手は避ける。"
         "指した後に相手に1手詰みがある手は絶対に避ける。相手キングを詰ませられるなら詰ませる。駒得できるなら駒得する。")


def val(p):
    return VALUE.get(p.piece_type, 0) if p else 0


def cheapest_attacker(board, color, sq):
    vals = [val(board.piece_at(s)) for s in board.attackers(color, sq)]
    return min(vals) if vals else None


def hanging(board, color):
    out = []
    for sq, p in board.piece_map().items():
        if p.color != color or p.piece_type == chess.KING:
            continue
        a = cheapest_attacker(board, not color, sq)
        if a is None:
            continue
        if not board.attackers(color, sq) or a < val(p):
            out.append((val(p), f"{chess.square_name(sq)}の{NAME[p.piece_type]}"))
    return sorted(out, reverse=True)


def opponent_mate_in_one(board):
    for m in board.legal_moves:
        board.push(m)
        try:
            if board.is_checkmate():
                return m
        finally:
            board.pop()
    return None


def facts(board, move, prev_mine):
    me = board.turn
    f = []
    cap = board.piece_at(move.to_square) if not board.is_en_passant(move) else chess.Piece(chess.PAWN, not me)
    if cap:
        f.append(f"相手の{NAME[cap.piece_type]}を取る（駒得+{val(cap)}）")
    if move.promotion:
        f.append(f"{NAME[move.promotion]}に昇格する")
    if board.is_castling(move):
        f.append("キャスリング")
    if prev_mine and prev_mine.to_square == move.from_square and prev_mine.from_square == move.to_square:
        f.append("直前の自分の手を戻す往復の手（手損）")
    board.push(move)
    try:
        if board.is_checkmate():
            f.append("相手キングが詰む（勝ち）")
            return "、".join(f), 1000
        if board.is_check():
            f.append("チェック")
        piece = board.piece_at(move.to_square)
        a = cheapest_attacker(board, board.turn, move.to_square)
        risk = 0
        if a is not None:
            if not board.attackers(me, move.to_square):
                f.append(f"動かした{NAME[piece.piece_type]}はタダで取られる（駒損-{val(piece)}）")
                risk = val(piece)
            elif a < val(piece):
                f.append(f"動かした{NAME[piece.piece_type]}はより安い駒で取られる")
                risk = val(piece) - a
        h = [s for v, s in hanging(board, me) if not s.startswith(chess.square_name(move.to_square) + "の")]
        if h:
            f.append("指した後に浮く駒: " + "・".join(h[:3]))
        if opponent_mate_in_one(board):
            f.append("指した後に相手に1手詰みがある（負け）")
            risk = 1000
    finally:
        board.pop()
    return "、".join(f) or "駒の取り合いなし", (val(cap) if cap else 0) - risk


def board_text(board):
    return str(board) + "\n（大文字が白、小文字が黒。上が8段目、左がaファイル）\nFEN: " + board.fen()


def state_of(board, history):
    me = "白" if board.turn == chess.WHITE else "黒"
    threats = []
    board.push(chess.Move.null())
    try:
        if opponent_mate_in_one(board):
            threats.append("相手は次に1手詰みを狙っている。防がないと負ける")
    finally:
        board.pop()
    h = hanging(board, board.turn)
    if h:
        threats.append("いま相手に取られそうな自分の駒: " + "・".join(s for _, s in h[:4]))
    return {"game": "チェス", "you_are": me, "move_number": board.fullmove_number, "board": board_text(board),
            "recent_moves": history[-12:], "in_check": board.is_check(), "threats": threats or ["特になし"], "guide": GUIDE}


def decide(board, history, prev_mine=None):
    me = "白" if board.turn == chess.WHITE else "黒"
    moves = list(board.legal_moves)
    info = {m.uci(): facts(board, m, prev_mine) for m in moves}
    criteria = {m.uci(): f"{board.san(m)}。{info[m.uci()][0]}" for m in moves}
    q1 = {"move": {"type": "choice", "criteria": criteria,
                   "instructions": f"あなたは{me}。この局面で最善の手はどれか。state の guide と threats に従う。"},
          "eval": {"type": "score", "instructions": "現在の形勢は？",
                   "criteria": ["黒が勝勢", "黒が優勢", "互角", "白が優勢", "白が勝勢"]},
          "danger": {"type": "noul", "instructions": f"{me}のキングは数手以内に詰まされる危険があるか？"}}
    s1, ms1, r1 = call(state_of(board, history), q1)
    res = {"status": s1, "ms": ms1, "requests": 1, "response": r1, "candidates": len(moves), "criteria": criteria,
           "usage": dict(r1.get("usage") or {})}
    if s1 != 200:
        return res
    probs = r1["answers"]["move"]["probabilities"]
    res["stage1_choice"] = r1["answers"]["move"]["choice"]
    short = sorted(probs, key=lambda k: -probs[k])[:SHORTLIST]
    if len(short) <= 1:
        res["final"] = short[0]
        return res
    cands = {}
    for i, u in enumerate(short):
        m = chess.Move.from_uci(u)
        san = board.san(m)
        board.push(m)
        cands[f"c{i}"] = {"move": san, "facts": info[u][0], "board_after": board_text(board)}
        board.pop()
    q2 = {"best": {"type": "choice", "instructions": f"あなたは{me}。candidates の中で最善の手はどれか。指した後の局面（board_after）と facts を比べて選ぶ。",
                   "criteria": {k: f"{v['move']}。{v['facts']}" for k, v in cands.items()}}}
    for k, v in cands.items():
        q2[f"eval_{k}"] = {"type": "score", "instructions": f"{k}（{v['move']}）を指した後の局面は{me}にとってどうか？",
                           "criteria": ["大きく不利", "やや不利", "互角", "やや有利", "大きく有利"]}
    s2, ms2, r2 = call({"game": "チェス", "you_are": me, "board_now": board_text(board), "recent_moves": history[-12:],
                        "candidates": cands, "guide": GUIDE}, q2)
    res["requests"] = 2
    res["ms"] = ms1 + ms2
    for k, v in (r2.get("usage") or {}).items():
        res["usage"][k] = res["usage"].get(k, 0) + v
    res["shortlist"] = [cands[f"c{i}"]["move"] for i in range(len(short))]
    if s2 != 200:
        res["final"] = short[0]
        return res
    res["final"] = short[int(r2["answers"]["best"]["choice"][1:])]
    res["stage2"] = {"probabilities": {cands[k]["move"]: p for k, p in r2["answers"]["best"]["probabilities"].items()}}
    return res


class Engine:
    def __init__(self, skill, movetime):
        self.p = subprocess.Popen([shutil.which("fairy-stockfish")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.movetime = movetime
        self.send("uci")
        self.until("uciok")
        self.send("setoption name UCI_Variant value chess")
        self.send(f"setoption name Skill Level value {skill}")
        self.send("isready")
        self.until("readyok")
        self.send("ucinewgame")

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


def calibrate(game):
    recs = [json.loads(l) for l in open(os.path.join(game, "moves.jsonl"))]
    usi = [r["uci"] for r in recs]
    ref = Engine(20, 1000)
    hit = n = 0
    for i, r in enumerate(recs):
        if r["side"] == "black":
            n += 1
            hit += ref.best(usi[:i]) == r["uci"]
    ref.close()
    out = {"game": os.path.basename(game), "black_moves": n, "match_with_strongest": hit, "match_rate": round(hit / n, 3) if n else None}
    json.dump(out, open(os.path.join(game, "opponent_strength.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", type=int, default=-20)
    ap.add_argument("--movetime", type=int, default=100)
    ap.add_argument("--max-plies", type=int, default=250)
    ap.add_argument("--out", default="games")
    ap.add_argument("--calibrate", nargs="*")
    ap.add_argument("--analyze", nargs="*")
    a = ap.parse_args()
    if a.analyze:
        analyze(a.analyze, os.path.join(a.out, "chess_move_quality.jsonl"))
        return
    if a.calibrate:
        for g in a.calibrate:
            calibrate(g)
        return

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    out = os.path.join(a.out, f"chess-{stamp}-skill{a.skill}")
    os.makedirs(out, exist_ok=True)
    log = open(os.path.join(out, "moves.jsonl"), "w")
    eng = Engine(a.skill, a.movetime)
    opp = f"Fairy-Stockfish 14.0.1 chess (Skill Level {a.skill}, {a.movetime}ms/手)"
    b = chess.Board()
    uci, hist = [], []
    tot = {"requests": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0, "ms": []}
    prev_mine = None
    while len(uci) < a.max_plies and not b.is_game_over(claim_draw=True):
        ply = len(uci) + 1
        if b.turn == chess.WHITE:
            d = decide(b, hist, prev_mine)
            tot["requests"] += d["requests"]
            if d["status"] != 200:
                tot["failed"] += 1
                if tot["failed"] >= 5:
                    break
                continue
            mv = chess.Move.from_uci(d["final"])
            u = d["usage"]
            tot["input_tokens"] += u.get("input_tokens", 0)
            tot["output_tokens"] += u.get("output_tokens", 0)
            tot["ms"].append(d["ms"])
            ans = d["response"]["answers"]
            rec = {"ply": ply, "side": "white", "player": "Jev", "uci": mv.uci(), "san": b.san(mv), "jev_ms": round(d["ms"]),
                   "requests": d["requests"], "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                   "cost_usd": round(u.get("input_tokens", 0) * USD_PER_INPUT_TOKEN, 8), "candidates": d["candidates"],
                   "shortlist": d.get("shortlist"), "facts": d["criteria"].get(mv.uci()),
                   "eval": ans["eval"]["score"], "danger": ans["danger"]["noul"]}
            prev_mine = mv
        else:
            mv = chess.Move.from_uci(eng.best(uci))
            rec = {"ply": ply, "side": "black", "player": opp, "uci": mv.uci(), "san": b.san(mv)}
        hist.append(rec["san"])
        b.push(mv)
        uci.append(mv.uci())
        log.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log.flush()
    eng.close()

    outcome = b.outcome(claim_draw=True)
    if outcome is None:
        result = f"{len(uci)}手で打ち切り"
    elif outcome.winner is None:
        result = f"{len(uci)}手で引き分け（{outcome.termination.name}）"
    else:
        result = f"{len(uci)}手で{'Jev（白）' if outcome.winner == chess.WHITE else '相手（黒）'}の勝ち（{outcome.termination.name}）"
    g = chess.pgn.Game.from_board(b)
    g.headers.update({"Event": "Jev vs Fairy-Stockfish", "Date": datetime.date.today().strftime("%Y.%m.%d"),
                      "White": f"Jev ({MODEL}, ロリポップ！AIゲートウェイ経由)", "Black": opp})
    open(os.path.join(out, "game.pgn"), "w").write(str(g) + "\n")
    ms = sorted(tot["ms"])
    summary = {"game": "chess", "result": result, "plies": len(uci), "opponent": opp, "jev_moves": len(ms),
               "jev_requests": tot["requests"], "jev_failed": tot["failed"], "input_tokens": tot["input_tokens"],
               "output_tokens": tot["output_tokens"], "cost_usd_list_price": round(tot["input_tokens"] * USD_PER_INPUT_TOKEN, 6),
               "jev_ms_p50": round(ms[len(ms) // 2]) if ms else None}
    json.dump(summary, open(os.path.join(out, "summary.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps({"out": out, **summary}, ensure_ascii=False), flush=True)



def analyze(games, out_path):
    """Jev の各手の損失（最善手の評価値 - Jev の手の後の評価値、cp、白から見る）。将棋版 analyze.py と同じ定義。"""
    import re
    ev = Engine(20, 300)

    def score(moves):
        ev.send("position startpos" + (" moves " + " ".join(moves) if moves else ""))
        ev.send(f"go movetime {ev.movetime}")
        last, bm = None, None
        while True:
            line = ev.p.stdout.readline()
            m = re.search(r" score (cp|mate) (-?\d+)", line)
            if m:
                v = int(m.group(2))
                last = v if m.group(1) == "cp" else (30000 if v > 0 else -30000)
            if line.startswith("bestmove"):
                return last, line.split()[1]

    with open(out_path, "w") as f:
        for game in games:
            recs = [json.loads(l) for l in open(os.path.join(game, "moves.jsonl"))]
            uci = [r["uci"] for r in recs]
            for i, r in enumerate(recs):
                if r["side"] != "white":
                    continue
                best, bm = score(uci[:i])
                after, _ = score(uci[:i + 1])
                jev = -after if after is not None else None
                row = {"game": os.path.basename(game), "ply": r["ply"], "candidates": r.get("candidates"), "move": r["san"],
                       "same_as_best": bm == r["uci"], "best_cp": best, "jev_cp": jev,
                       "loss_cp": max(0, best - jev) if best is not None and jev is not None else None}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    ev.close()


if __name__ == "__main__":
    main()
