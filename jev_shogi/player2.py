"""2手読み版。判断はすべて Jev、コードはルール上の事実だけを渡す。

1段目（1リクエスト）: 合法手すべてから候補を選ばせる。説明には駒の損得に加えて玉の安全の事実を付ける。
2段目（1リクエスト）: 上位 K 手それぞれについて、指した後の局面で「相手の最善の応手」を選ばせる（K 問をまとめて聞く）。
3段目（1リクエスト）: 各候補について、相手の応手まで進めた局面の形勢を聞き、最も良い候補を指す。
"""
import shogi

from .jev import call
from .player import GUIDE, MAX_CHOICES, VALUE, cheapest_attacker, facts, hanging, jp, opponent_mate_in_one, sqjp, state_of, value

SHORTLIST = 6
REPLY_CHOICES = 60  # 相手の応手の候補は事実のスコア順に上位だけ渡す（王手・駒取り・詰みは必ず入る）


def material(board, color):
    """color から見た駒割（盤上と持ち駒の点数の差）。点数は player.VALUE、玉は数えない。"""
    total = 0
    for sq in shogi.SQUARES:
        p = board.piece_at(sq)
        if p and p.piece_type != shogi.KING:
            total += value(p) if p.color == color else -value(p)
    for c in (shogi.BLACK, shogi.WHITE):
        for pt, n in board.pieces_in_hand[c].items():
            v = VALUE.get(pt, 0) * n
            total += v if c == color else -v
    return total


def neighbors(sq):
    r, c = divmod(sq, 9)
    return [(r + dr) * 9 + (c + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)
            if (dr or dc) and 0 <= r + dr < 9 and 0 <= c + dc < 9]


def king_safety(board, color):
    """color の玉の周りの味方の駒数、周り8マスへの相手の利きの数、相手が指せる王手の数（相手の手番で数える）。"""
    k = board.king_squares[color]
    around = neighbors(k)
    guards = sum(1 for s in around if (p := board.piece_at(s)) and p.color == color)
    pressure = sum(len(board.attackers(color ^ 1, s)) for s in around)
    return guards, pressure


def checks_available(board):
    """board の手番側が指せる王手の数。"""
    n = 0
    for m in board.legal_moves:
        board.push(m)
        n += board.is_check()
        board.pop()
    return n


def facts2(board, move, prev_mine):
    me = board.turn
    base, score = facts(board, move, prev_mine)
    g0, p0 = king_safety(board, me)
    board.push(move)
    try:
        if board.is_checkmate():
            return base, score
        g1, p1 = king_safety(board, me)
        chk = checks_available(board)
    finally:
        board.pop()
    extra = [f"自玉の周りの味方{g1}枚（{g1 - g0:+d}）", f"自玉の周りへの相手の利き{p1}（{p1 - p0:+d}）"]
    if chk:
        extra.append(f"指した後に相手が指せる王手{chk}通り")
    return base + "、" + "、".join(extra), score - (p1 - p0) - chk * 0.5


def _stage1(board, history_jp, prev_mine):
    me = "先手" if board.turn == shogi.BLACK else "後手"
    moves = list(board.legal_moves)
    info = {m.usi(): facts2(board, m, prev_mine) for m in moves}
    truncated = 0
    if len(moves) > MAX_CHOICES:
        moves.sort(key=lambda m: -info[m.usi()][1])
        truncated = len(moves) - MAX_CHOICES
        moves = moves[:MAX_CHOICES]
    criteria = {m.usi(): f"{jp(board, m)}。{info[m.usi()][0]}" for m in moves}
    g, p = king_safety(board, board.turn)
    state = state_of(board, history_jp)
    state["my_king"] = f"自玉の周りの味方{g}枚、自玉の周りへの相手の利き{p}"
    state["material"] = f"現在の駒割（自分から見た駒の点数差。歩1・香3・桂4・銀5・金6・角8・飛10）: {material(board, board.turn):+d}"
    q = {"move": {"type": "choice", "criteria": criteria,
                  "instructions": f"あなたは{me}。この局面で最善の手はどれか。state の guide・threats・my_king を踏まえ、玉の安全を崩さない手を選ぶ。"},
         "eval": {"type": "score", "instructions": "現在の形勢は？",
                  "criteria": ["後手が勝勢", "後手が優勢", "互角", "先手が優勢", "先手が勝勢"]},
         "danger": {"type": "noul", "instructions": f"{me}の玉は数手以内に詰まされる危険があるか？"}}
    return call(state, q), info, criteria, len(moves) + truncated, truncated


def decide(board, history_jp, prev_mine=None):
    me_color = board.turn
    me = "先手" if me_color == shogi.BLACK else "後手"
    opp = "後手" if me == "先手" else "先手"
    (s1, ms1, r1), info, criteria, ncand, truncated = _stage1(board, history_jp, prev_mine)
    res = {"status": s1, "ms": ms1, "ms_stage": [ms1], "requests": 1, "response": r1, "candidates": ncand,
           "truncated": truncated, "criteria": criteria, "usage": dict(r1.get("usage") or {}),
           "stage1_choice": None, "shortlist": None}
    if s1 != 200:
        return res
    a1 = r1["answers"]
    res["stage1_choice"] = a1["move"]["choice"]
    probs = a1["move"]["probabilities"]
    short = sorted(probs, key=lambda k: -probs[k])[:SHORTLIST]
    # 詰ませる手があれば読むまでもない。1手詰みを外す候補は最初から除く（事実のスコアで判定済み）
    for u in short:
        if info[u][1] >= 1000:
            res["final"] = u
            return res
    short = [u for u in short if info[u][1] > -1000] or short[:1]
    if len(short) == 1:
        res["final"] = short[0]
        return res

    def add_usage(r):
        for k, v in (r.get("usage") or {}).items():
            res["usage"][k] = res["usage"].get(k, 0) + v

    # 2段目: 各候補のあとの相手の最善応手を Jev に選ばせる（候補ぶんの質問を1リクエストで）
    q2, replies = {}, {}
    for i, u in enumerate(short):
        m = shogi.Move.from_usi(u)
        board.push(m)
        opp_moves = list(board.legal_moves)
        if not opp_moves:  # 詰み（facts で拾えているはずだが念のため）
            board.pop()
            res["final"] = u
            return res
        of = {om.usi(): facts(board, om, None) for om in opp_moves}
        opp_moves.sort(key=lambda om: -of[om.usi()][1])
        opp_moves = opp_moves[:REPLY_CHOICES]
        replies[f"c{i}"] = (u, {om.usi(): of[om.usi()] for om in opp_moves})
        q2[f"reply_c{i}"] = {
            "type": "choice",
            "instructions": {"question": f"{me}が{jp_after(board, m)}と指した直後の局面。{opp}の最善の応手はどれか。{opp}は駒得・王手・{me}玉への攻めを狙う。",
                             "board": board.kif_str()},
            "criteria": {ou: f"{jp(board, shogi.Move.from_usi(ou))}。{f[0]}。応手後の{opp}から見た駒割 {mat_after(board, shogi.Move.from_usi(ou)):+d}"
                         for ou, f in replies[f"c{i}"][1].items()}}
        board.pop()
    s2, ms2, r2 = call({"game": "本将棋（平手）", "note": "各質問の board はその候補を指した直後の局面", "guide": GUIDE}, q2)
    res["requests"] += 1
    res["ms"] += ms2
    res["ms_stage"].append(ms2)
    add_usage(r2)
    if s2 != 200:
        res["final"] = short[0]
        res["stage2_failed"] = s2
        return res

    # 3段目: 相手の応手まで進めた局面の形勢を聞き、最も良い候補を選ぶ
    q3, lines = {}, {}
    mat_now = material(board, me_color)
    for i, u in enumerate(short):
        key = f"c{i}"
        ou = r2["answers"][f"reply_{key}"]["choice"]
        m, om = shogi.Move.from_usi(u), shogi.Move.from_usi(ou)
        name = jp(board, m)
        board.push(m)
        oname = jp(board, om)
        board.push(om)
        mated = board.is_checkmate()
        mat = material(board, me_color)
        g, p = king_safety(board, me_color)
        h = hanging(board, me_color)
        board_txt = board.kif_str()
        board.pop()
        board.pop()
        lines[key] = {"line": f"{name} → {oname}", "facts_move": info[u][0], "facts_reply": replies[key][1][ou][0],
                      "material_after": f"{mat:+d}（現在 {mat_now:+d} から {mat - mat_now:+d}）",
                      "after": {"board": board_txt, "my_king": f"自玉の周りの味方{g}枚、相手の利き{p}",
                                "my_hanging": [s for _, s in h[:4]] or ["なし"], "i_am_mated": mated}}
        q3[f"eval_{key}"] = {"type": "score",
                             "instructions": {"question": f"{name}と指し、{opp}が{oname}と応じた後の局面は{me}にとってどうか？",
                                              "position": lines[key]["after"], "material_after": lines[key]["material_after"]},
                             "criteria": ["負けに近い", "不利", "互角", "有利", "勝ちに近い"]}
    q3["best"] = {"type": "choice", "instructions": f"あなたは{me}。lines（自分の手→相手の最善応手）を比べて、最善の候補を選ぶ。",
                  "criteria": {k: f"{v['line']}。手順後の駒割 {v['material_after']}。自分の手: {v['facts_move']}。相手の応手: {v['facts_reply']}"
                               for k, v in lines.items()}}
    s3, ms3, r3 = call({"game": "本将棋（平手）", "you_are": me, "board_now": board.kif_str(), "lines": lines, "guide": GUIDE}, q3)
    res["requests"] += 1
    res["ms"] += ms3
    res["ms_stage"].append(ms3)
    add_usage(r3)
    res["shortlist"] = [lines[f"c{i}"]["line"] for i in range(len(short))]
    if s3 != 200:
        res["final"] = short[0]
        res["stage3_failed"] = s3
        return res
    a3 = r3["answers"]
    evals = {k: a3[f"eval_{k}"]["score"] for k in lines}
    best_p = a3["best"]["probabilities"]
    # Jev の2つの判断（局面評価と候補比較）を足して最大の候補を指す
    total = {k: evals[k] + 2 * best_p.get(k, 0) for k in lines}
    pick = max(total, key=total.get)
    res["final"] = short[int(pick[1:])]
    res["stage2"] = {"probabilities": {lines[k]["line"]: best_p.get(k, 0) for k in lines},
                     "evals": {lines[k]["line"]: evals[k] for k in lines}}
    return res


def mat_after(board, move):
    """board の手番側が move を指した後の、その手番側から見た駒割。"""
    me = board.turn
    board.push(move)
    try:
        return material(board, me)
    finally:
        board.pop()


def jp_after(board, move):
    """board は move を指した後。表記用に1手戻して名前を得る。"""
    board.pop()
    try:
        return jp(board, move)
    finally:
        board.push(move)
