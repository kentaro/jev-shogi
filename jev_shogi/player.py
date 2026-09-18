"""局面から Jev への質問を組み立て、指し手を決める。判断はすべて Jev が行い、コードはルール上の事実だけを渡す。

1段目: 合法手すべてを choice の候補にし、各候補に1〜2手先の事実を説明として付ける。
       （取る駒、王手・詰み、動かした駒がタダで取られるか、他の駒が浮くか、指した後に相手の1手詰みがあるか、往復の手か）
2段目: 1段目の上位 K 手について、指した後の局面を並べ、どれが最善かと各局面の形勢を1リクエストでまとめて判定させる。
探索エンジンは使わない。
"""
import shogi
import shogi.KIF

from .jev import call

MAX_CHOICES = 255  # Jev の choice の上限
SHORTLIST = 6
VALUE = {shogi.PAWN: 1, shogi.LANCE: 3, shogi.KNIGHT: 4, shogi.SILVER: 5, shogi.GOLD: 6, shogi.BISHOP: 8, shogi.ROOK: 10,
         shogi.KING: 100, shogi.PROM_PAWN: 6, shogi.PROM_LANCE: 6, shogi.PROM_KNIGHT: 6, shogi.PROM_SILVER: 6,
         shogi.PROM_BISHOP: 12, shogi.PROM_ROOK: 14}
GUIDE = ("将棋の基本方針: 序盤は玉を囲って（美濃囲い・矢倉など）安全にし、飛車・角・銀で攻めの形を作る。"
         "同じ駒を行ったり来たりさせる手（手損）は避ける。駒をタダで取られる手、他の駒をタダで取られる状態にする手は避ける。"
         "指した後に相手に1手詰みがある手は絶対に避ける。相手玉を詰ませられるなら詰ませる。駒得できるなら駒得する。")


def jp(board, move):
    return shogi.KIF.Exporter.kif_move_from(move.usi(), board)


def sqjp(sq):
    r, c = divmod(sq, 9)
    return "９８７６５４３２１"[c] + "一二三四五六七八九"[r]


def value(piece):
    return VALUE.get(piece.piece_type, 0) if piece else 0


def cheapest_attacker(board, color, sq):
    atk = board.attackers(color, sq)
    vals = [value(board.piece_at(s)) for s in atk]
    return min(vals) if vals else None


def hanging(board, color):
    """color の駒で、相手の利きがあり、タダ取り（味方の利きなし）か、より安い駒で取られるもの。"""
    opp = color ^ 1
    out = []
    for sq in shogi.SQUARES:
        p = board.piece_at(sq)
        if not p or p.color != color or p.piece_type == shogi.KING:
            continue
        a = cheapest_attacker(board, opp, sq)
        if a is None:
            continue
        defended = bool(board.attackers(color, sq))
        if not defended or a < value(p):
            out.append((value(p), f"{sqjp(sq)}の{p.japanese_symbol()}"))
    return sorted(out, reverse=True)


def opponent_mate_in_one(board):
    """board は相手の手番。相手に1手詰みがあればその手を返す。"""
    for m in board.legal_moves:
        board.push(m)
        try:
            if board.is_check() and board.is_checkmate():
                return m
        finally:
            board.pop()
    return None


def facts(board, move, prev_mine):
    me = board.turn
    f = []
    cap = board.piece_at(move.to_square)
    if cap:
        f.append(f"相手の{cap.japanese_symbol()}を取る（駒得+{value(cap)}）")
    if move.promotion:
        f.append("成る")
    if prev_mine and not move.drop_piece_type and prev_mine.to_square == move.from_square \
            and prev_mine.from_square == move.to_square:
        f.append("直前の自分の手を戻す往復の手（手損）")
    board.push(move)
    try:
        if board.is_checkmate():
            f.append("相手玉が詰む（勝ち）")
            return "、".join(f), 1000
        if board.is_check():
            f.append("王手")
        piece = board.piece_at(move.to_square)
        a = cheapest_attacker(board, board.turn, move.to_square)
        risk = 0
        if a is not None:
            defended = bool(board.attackers(me, move.to_square))
            if not defended:
                f.append(f"動かした{piece.japanese_symbol()}はタダで取られる（駒損-{value(piece)}）")
                risk = value(piece)
            elif a < value(piece):
                f.append(f"動かした{piece.japanese_symbol()}はより安い駒で取られる")
                risk = value(piece) - a
        h = [s for v, s in hanging(board, me) if s != f"{sqjp(move.to_square)}の{piece.japanese_symbol()}"]
        if h:
            f.append("指した後に浮く駒: " + "・".join(h[:3]))
        m1 = opponent_mate_in_one(board)
        if m1:
            f.append("指した後に相手に1手詰みがある（負け）")
            risk = 1000
    finally:
        board.pop()
    gain = value(cap) if cap else 0
    return "、".join(f) or "駒の取り合いなし", gain - risk


def state_of(board, history_jp):
    me = "先手（▲）" if board.turn == shogi.BLACK else "後手（△）"
    threats = []
    board.push(shogi.Move.null())
    try:
        m1 = opponent_mate_in_one(board)
    finally:
        board.pop()
    if m1:
        threats.append("相手は次に1手詰みを狙っている。防がないと負ける")
    h = hanging(board, board.turn)
    if h:
        threats.append("いま相手に取られそうな自分の駒: " + "・".join(s for _, s in h[:4]))
    return {"game": "本将棋（平手）", "you_are": me, "move_number": board.move_number, "board": board.kif_str(),
            "recent_moves": history_jp[-12:], "in_check": board.is_check(), "threats": threats or ["特になし"],
            "guide": GUIDE}


def decide(board, history_jp, prev_mine=None):
    me = "先手" if board.turn == shogi.BLACK else "後手"
    moves = list(board.legal_moves)
    info = {m.usi(): facts(board, m, prev_mine) for m in moves}
    truncated = 0
    if len(moves) > MAX_CHOICES:
        moves.sort(key=lambda m: -info[m.usi()][1])
        truncated = len(moves) - MAX_CHOICES
        moves = moves[:MAX_CHOICES]
    criteria = {m.usi(): f"{jp(board, m)}。{info[m.usi()][0]}" for m in moves}
    q1 = {"move": {"type": "choice", "criteria": criteria,
                   "instructions": f"あなたは{me}。この局面で最善の手はどれか。state の guide と threats に従う。"},
          "eval": {"type": "score", "instructions": "現在の形勢は？",
                   "criteria": ["後手が勝勢", "後手が優勢", "互角", "先手が優勢", "先手が勝勢"]},
          "danger": {"type": "noul", "instructions": f"{me}の玉は数手以内に詰まされる危険があるか？"}}
    state = state_of(board, history_jp)
    s1, ms1, r1 = call(state, q1)
    res = {"status": s1, "ms": ms1, "requests": 1, "response": r1, "candidates": len(moves) + truncated,
           "truncated": truncated, "criteria": criteria,
           "usage": dict(r1.get("usage") or {}), "stage1_choice": None, "shortlist": None}
    if s1 != 200:
        return res
    a1 = r1["answers"]
    res["stage1_choice"] = a1["move"]["choice"]
    probs = a1["move"]["probabilities"]
    short = sorted(probs, key=lambda k: -probs[k])[:SHORTLIST]
    if len(short) <= 1:
        res["final"] = short[0]
        return res

    # 2段目: 候補ごとに指した後の局面を並べて比べる
    cands = {}
    for i, u in enumerate(short):
        m = shogi.Move.from_usi(u)
        name = jp(board, m)
        board.push(m)
        cands[f"c{i}"] = {"move": name, "facts": info[u][0], "board_after": board.kif_str()}
        board.pop()
    q2 = {"best": {"type": "choice", "instructions": f"あなたは{me}。candidates の中で最善の手はどれか。指した後の局面（board_after）と facts を比べて選ぶ。",
                   "criteria": {k: f"{v['move']}。{v['facts']}" for k, v in cands.items()}}}
    for k, v in cands.items():
        q2[f"eval_{k}"] = {"type": "score", "instructions": f"{k}（{v['move']}）を指した後の局面は{me}にとってどうか？",
                           "criteria": ["大きく不利", "やや不利", "互角", "やや有利", "大きく有利"]}
    s2, ms2, r2 = call({"game": "本将棋（平手）", "you_are": me, "board_now": board.kif_str(),
                        "recent_moves": history_jp[-12:], "candidates": cands, "guide": GUIDE}, q2)
    res["requests"] = 2
    res["ms"] = ms1 + ms2
    res["ms_stage"] = [ms1, ms2]
    for k, v in (r2.get("usage") or {}).items():
        res["usage"][k] = res["usage"].get(k, 0) + v
    res["shortlist"] = [cands[f"c{i}"]["move"] for i in range(len(short))]
    if s2 != 200:
        res["final"] = res["stage1_choice"]
        res["stage2_failed"] = s2
        return res
    a2 = r2["answers"]
    best = a2["best"]["choice"]
    res["final"] = short[int(best[1:])]
    res["stage2"] = {"probabilities": {cands[k]["move"]: p for k, p in a2["best"]["probabilities"].items()},
                     "evals": {cands[k]["move"]: a2[f"eval_{k}"]["score"] for k in cands}}
    return res
