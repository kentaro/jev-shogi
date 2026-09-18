"""読みと評価を分けた版。判断はすべて Jev、コードはルール上の事実（合法手・取る駒・王手・詰み・駒割など）を数えて渡すだけ。

1. 候補（1リクエスト）: 全合法手から上位 K 手に絞る（player2 の1段目）
2. 読み（DEPTH リクエスト）: K 本の手順を並行して進める。各手で「この局面で手番側が指す手」を Jev に選ばせる。K 本分を1リクエストにまとめる
3. 評価（1リクエスト）: 読み切った K 局面の形勢だけを Jev に聞き、最も良い候補を指す
"""
import shogi

from .jev import call
from .player import GUIDE, facts, hanging, jp
from .player2 import _stage1, king_safety, material

SHORTLIST = 6
DEPTH = 3  # 候補手の後に読む手数（相手→自分→相手）
MOVE_CHOICES = 60  # 読みの各手で渡す合法手の上限（事実のスコア順。駒取り・王手・詰みが先に入る）


def side(color):
    return "先手" if color == shogi.BLACK else "後手"


def position_view(board, me_color):
    g, p = king_safety(board, me_color)
    og, op = king_safety(board, me_color ^ 1)
    return {"board": board.kif_str(),
            "material": f"{side(me_color)}から見た駒割 {material(board, me_color):+d}（歩1・香3・桂4・銀5・金6・角8・飛10）",
            "my_king": f"{side(me_color)}玉の周りの味方{g}枚、相手の利き{p}",
            "opp_king": f"{side(me_color ^ 1)}玉の周りの味方{og}枚、相手の利き{op}",
            "my_hanging": [s for _, s in hanging(board, me_color)[:4]] or ["なし"],
            "opp_hanging": [s for _, s in hanging(board, me_color ^ 1)[:4]] or ["なし"]}


def move_question(board, line_jp):
    """手番側が次に指す手を選ばせる質問。"""
    mover = side(board.turn)
    moves = list(board.legal_moves)
    fs = {m.usi(): facts(board, m, None) for m in moves}
    moves.sort(key=lambda m: -fs[m.usi()][1])
    moves = moves[:MOVE_CHOICES]
    crit = {}
    for m in moves:
        board.push(m)
        mat = material(board, board.turn ^ 1)
        board.pop()
        crit[m.usi()] = f"{jp(board, m)}。{fs[m.usi()][0]}。指した後の{mover}から見た駒割 {mat:+d}"
    return {"type": "choice", "criteria": crit,
            "instructions": {"question": f"手順 {' → '.join(line_jp)} の後の局面。{mover}の最善手はどれか。{mover}は駒得・相手玉への攻め・自玉の安全を考える。",
                             "position": position_view(board, board.turn)}}


def decide(board, history_jp, prev_mine=None):
    me_color = board.turn
    me = side(me_color)
    (s1, ms1, r1), info, criteria, ncand, truncated = _stage1(board, history_jp, prev_mine)
    res = {"status": s1, "ms": ms1, "ms_stage": [ms1], "requests": 1, "response": r1, "candidates": ncand,
           "truncated": truncated, "criteria": criteria, "usage": dict(r1.get("usage") or {}),
           "stage1_choice": None, "shortlist": None}
    if s1 != 200:
        return res

    def add(r, ms):
        res["requests"] += 1
        res["ms"] += ms
        res["ms_stage"].append(ms)
        for k, v in (r.get("usage") or {}).items():
            res["usage"][k] = res["usage"].get(k, 0) + v

    a1 = r1["answers"]
    res["stage1_choice"] = a1["move"]["choice"]
    probs = a1["move"]["probabilities"]
    short = sorted(probs, key=lambda k: -probs[k])[:SHORTLIST]
    for u in short:
        if info[u][1] >= 1000:  # 詰ませる手
            res["final"] = u
            return res
    short = [u for u in short if info[u][1] > -1000] or short[:1]
    if len(short) == 1:
        res["final"] = short[0]
        return res

    # 読み: 各候補の手順を並行して DEPTH 手進める
    lines = {}
    for i, u in enumerate(short):
        b = board.copy() if hasattr(board, "copy") else shogi.Board(board.sfen())
        m = shogi.Move.from_usi(u)
        name = jp(b, m)
        b.push(m)
        lines[f"c{i}"] = {"usi": u, "board": b, "moves": [name], "ended": b.is_game_over()}
    for ply in range(DEPTH):
        live = {k: v for k, v in lines.items() if not v["ended"]}
        if not live:
            break
        q = {f"next_{k}": move_question(v["board"], v["moves"]) for k, v in live.items()}
        s, ms, r = call({"game": "本将棋（平手）", "task": "各質問の position は、その手順を進めた後の局面。手番側の最善手を選ぶ",
                         "guide": GUIDE}, q)
        add(r, ms)
        if s != 200:
            break
        for k, v in live.items():
            u = r["answers"][f"next_{k}"]["choice"]
            mv = shogi.Move.from_usi(u)
            v["moves"].append(jp(v["board"], mv))
            v["board"].push(mv)
            v["ended"] = v["board"].is_game_over()

    # 評価: 読み切った局面の形勢だけを聞く
    q = {}
    for k, v in lines.items():
        b = v["board"]
        mated_me = b.is_checkmate() and b.turn == me_color
        mated_opp = b.is_checkmate() and b.turn != me_color
        view = position_view(b, me_color)
        view["checkmate"] = "自玉が詰んでいる" if mated_me else "相手玉が詰んでいる" if mated_opp else "なし"
        q[f"eval_{k}"] = {"type": "score", "criteria": ["負けに近い", "不利", "互角", "有利", "勝ちに近い"],
                          "instructions": {"question": f"手順 {' → '.join(v['moves'])} の後の局面は{me}にとってどうか？",
                                           "position": view}}
    s, ms, r = call({"game": "本将棋（平手）", "you_are": me, "task": "各局面の形勢を評価する"}, q)
    add(r, ms)
    res["shortlist"] = [" → ".join(lines[f"c{i}"]["moves"]) for i in range(len(short))]
    if s != 200:
        res["final"] = short[0]
        res["eval_failed"] = s
        return res
    evals = {k: r["answers"][f"eval_{k}"]["score"] for k in lines}
    # 同点なら候補段階の確率が高い方
    pick = max(lines, key=lambda k: (round(evals[k], 2), probs.get(lines[k]["usi"], 0)))
    res["final"] = lines[pick]["usi"]
    res["stage2"] = {"probabilities": {lines[k]["moves"][0]: probs.get(lines[k]["usi"], 0) for k in lines},
                     "evals": {" → ".join(lines[k]["moves"]): evals[k] for k in lines}}
    return res
