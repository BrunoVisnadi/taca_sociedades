import os
from pathlib import Path
from functools import wraps

from flask import Flask, jsonify, render_template, request, redirect, url_for, flash, session, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
from werkzeug.security import check_password_hash
from sqlalchemy import select, func, case, distinct, exists, desc
from sqlalchemy.orm import aliased
from db import SessionLocal
from models import (
    Edition, EditionSociety, Society,
    Round, Debate, DebatePosition, Speech,
    EditionMember, Person, User, DebateJudge,
    SocietyAccount
)

BASE_DIR = Path(__file__).resolve().parent

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
    static_url_path="/static",
)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Em produção:
    # SESSION_COOKIE_SECURE=True  # se seu domínio usa HTTPS (Render usa)
)
app.secret_key = os.environ.get("SECRET_KEY", "dev-unsafe")

# -------- Flask-Login --------
login_manager = LoginManager(app)
login_manager.login_view = "login"

ORDER_POS = case(
    (DebatePosition.position == "OG", 1),
    (DebatePosition.position == "OO", 2),
    (DebatePosition.position == "CG", 3),
    (DebatePosition.position == "CO", 4),
    else_=99,
)

def society_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("auth_kind") != "society" or not session.get("soc_acc_id"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper

def _next_round_without_results(sess, edition_id: int):
    # conta quantas speeches têm score NÃO nulo por rodada
    scored_count = func.count(
        case((Speech.score.isnot(None), 1))
    )

    row = sess.execute(
        select(
            Round.id,
            Round.number,
            Round.name,
            Round.scheduled_date,
            scored_count.label("scored_count"),
        )
        .select_from(Round)
        .join(Debate, Debate.round_id == Round.id, isouter=True)
        .join(Speech, Speech.debate_id == Debate.id, isouter=True)
        .where(Round.edition_id == edition_id)
        .group_by(Round.id)
        .having(scored_count == 0)           # <-- nenhuma nota lançada na rodada
        .order_by(Round.number.asc(), Round.id.asc())
        .limit(1)
    ).first()

    if not row:
        return None
    r_id, r_num, r_name, r_date, _ = row
    return {"id": r_id, "number": r_num, "name": r_name, "date": r_date}


# -------- debates/posição desta sociedade + escalação atual (mesmo que sem notas) --------
def _debates_of_round_for_soc(sess, round_id: int, edition_society_id: int):
    debs = sess.execute(
        select(
            Debate.id, Debate.number_in_round,
            DebatePosition.position
        )
        .join(DebatePosition, DebatePosition.debate_id == Debate.id)
        .where(
            Debate.round_id == round_id,
            DebatePosition.edition_society_id == edition_society_id
        )
        .order_by(Debate.number_in_round.asc())
    ).all()
    deb_ids = [d_id for (d_id, _n, _p) in debs]

    sp_rows = []
    if deb_ids:
        sp_rows = sess.execute(
            select(
                Speech.debate_id, Speech.position, Speech.sequence_in_team,
                Speech.score,
                EditionMember.id.label("member_id"),
                Person.full_name
            )
            .join(EditionMember, EditionMember.id == Speech.edition_member_id)
            .join(Person, Person.id == EditionMember.person_id)
            .where(Speech.debate_id.in_(deb_ids))
            .order_by(Speech.debate_id.asc(), Speech.position.asc(), Speech.sequence_in_team.asc())
        ).all()

    by_key = {}
    for (d_id, pos, seq, score, mid, name) in sp_rows:
        key = (d_id, pos)
        by_key.setdefault(key, {"s1": None, "s2": None, "locked": False})
        if seq == 1:
            by_key[key]["s1"] = {"id": mid, "name": name}
        elif seq == 2:
            by_key[key]["s2"] = {"id": mid, "name": name}
        if score is not None:
            by_key[key]["locked"] = True  # já tem nota => bloqueado

    out = []
    for (d_id, n, pos) in debs:
        info = by_key.get((d_id, pos), {"s1": None, "s2": None, "locked": False})
        out.append({
            "debate_id": d_id,
            "number_in_round": n,
            "position": pos,
            "s1": info["s1"],
            "s2": info["s2"],
            "locked": info["locked"],
        })
    return out

def _get_soc_context(sess):
    """Retorna (edition_society, edition_id, base_society_id) da conta logada de sociedade."""
    soc_acc_id = session.get("soc_acc_id")
    edsoc_id = session.get("edition_society_id")
    if not soc_acc_id or not edsoc_id:
        return None, None, None
    edsoc = sess.get(EditionSociety, edsoc_id)
    if not edsoc:
        return None, None, None
    return edsoc, edsoc.edition_id, edsoc.society_id

def _list_rounds_for_society(sess, edition_id, edition_society_id):
    """Rodadas onde a sociedade participa (tem posição em algum debate)."""
    rows = sess.execute(
        select(Round.id, Round.number, Round.name, Round.scheduled_date)
        .join(Debate, Debate.round_id == Round.id)
        .join(DebatePosition, DebatePosition.debate_id == Debate.id)
        .where(
            Round.edition_id == edition_id,
            DebatePosition.edition_society_id == edition_society_id
        )
        .group_by(Round.id)
        .order_by(Round.number.asc())
    ).all()
    return rows


def _debates_of_round_for_soc(sess, round_id, edition_society_id):
    """Debates da rodada onde esta sociedade participa, incluindo posição e escalação atual."""
    # Debates + posição da sociedade
    debs = sess.execute(
        select(
            Debate.id, Debate.number_in_round,
            DebatePosition.position
        )
        .join(DebatePosition, DebatePosition.debate_id == Debate.id)
        .where(
            Debate.round_id == round_id,
            DebatePosition.edition_society_id == edition_society_id
        )
        .order_by(Debate.number_in_round.asc())
    ).all()
    deb_ids = [d_id for (d_id, _n, _p) in debs]

    # Falas já cadastradas para esses debates/posições
    # (podem estar sem score = escalação; score != NULL -> bloqueado)
    sp_rows = []
    if deb_ids:
        sp_rows = sess.execute(
            select(
                Speech.debate_id, Speech.position, Speech.sequence_in_team,
                Speech.score,
                EditionMember.id.label("member_id"),
                Person.full_name
            )
            .join(EditionMember, EditionMember.id == Speech.edition_member_id)
            .join(Person, Person.id == EditionMember.person_id)
            .where(Speech.debate_id.in_(deb_ids))
            .order_by(Speech.debate_id.asc(), Speech.position.asc(), Speech.sequence_in_team.asc())
        ).all()

    # Agrupa por debate+posição
    by_key = {}
    for (d_id, pos, seq, score, mid, name) in sp_rows:
        key = (d_id, pos)
        by_key.setdefault(key, {"s1": None, "s2": None, "locked": False})
        if seq == 1:
            by_key[key]["s1"] = {"id": mid, "name": name}
        elif seq == 2:
            by_key[key]["s2"] = {"id": mid, "name": name}
        if score is not None:
            by_key[key]["locked"] = True

    # Monta estrutura final
    out = []
    for (d_id, n, pos) in debs:
        k = (d_id, pos)
        info = by_key.get(k, {"s1": None, "s2": None, "locked": False})
        out.append({
            "debate_id": d_id,
            "number_in_round": n,
            "position": pos,
            "s1": info["s1"],
            "s2": info["s2"],
            "locked": info["locked"],
        })
    return out

from sqlalchemy import select, func, case, and_, or_, literal_column

# conta quantas vezes um EditionMember já DEBATEU com nota (score != NULL) em rodadas anteriores da edição
def _used_times_member(sess, member_id: int, edition_id: int, lt_round_number: int | None = None) -> int:
    q = (
        select(func.count(Speech.id))
        .join(Debate, Debate.id == Speech.debate_id)
        .join(Round, Round.id == Debate.round_id)
        .where(
            Speech.edition_member_id == member_id,
            Speech.score.isnot(None),
            Round.edition_id == edition_id
        )
    )
    if lt_round_number is not None:
        q = q.where(Round.number < lt_round_number)
    return sess.execute(q).scalar_one()

# lista de debatedores ELEGÍVEIS (< 4 usos em rodadas anteriores) para a próxima rodada
def _eligible_debaters_for_next_round(sess, edition_id: int, base_society_id: int, next_round_number: int):
    EM = aliased(EditionMember)

    # quantas vezes ESTE membro já debateu (com nota) em rodadas anteriores
    used_subq = (
        select(func.count(Speech.id))
        .join(Debate, Debate.id == Speech.debate_id)
        .join(Round, Round.id == Debate.round_id)
        .where(
            Speech.edition_member_id == EM.id,   # <- referencia tipada
            Speech.score.isnot(None),
            Round.edition_id == edition_id,
            Round.number < next_round_number,
        )
        .correlate(EM)                           # <- correlaciona ao alias EM
        .scalar_subquery()
    )

    rows = sess.execute(
        select(
            EM.id,
            Person.full_name,
            used_subq.label("used_count"),
        )
        .join(Person, Person.id == EM.person_id)
        .where(
            EM.edition_id == edition_id,
            EM.kind == "debater",                # <- comparação tipada com ENUM
            Person.society_id == base_society_id,
        )
        .order_by(Person.full_name.asc())
    ).all()

    # filtra quem já debateu 4 vezes
    return [{"id": mid, "name": name} for (mid, name, used) in rows if (used or 0) < 4]


@app.get("/sociedade/<int:edsoc_id>")
def view_society_history(edsoc_id: int):
    sess = SessionLocal()
    EM2 = aliased(EditionMember)
    try:
        edsoc = sess.get(EditionSociety, edsoc_id)
        if not edsoc:
            abort(404)
        edition_id = edsoc.edition_id

        # Dados básicos da sociedade
        soc = sess.execute(
            select(Society.short_name, Society.name).where(Society.id == edsoc.society_id)
        ).first()
        short_name, full_name = soc if soc else (None, None)

        # --- Tabela de debatedores + contagem de vezes que debateram (score != NULL) na edição ---
        used_subq_all = (
            select(func.count(Speech.id))
            .join(Debate, Debate.id == Speech.debate_id)
            .join(Round, Round.id == Debate.round_id)
            .where(
                Speech.edition_member_id == EM2.id,
                Speech.score.isnot(None),
                Round.edition_id == edition_id,
            )
            .correlate(EM2)
            .scalar_subquery()
        )
        deb_rows = sess.execute(
            select(
                EM2.id,
                Person.full_name,
                used_subq_all.label("used_count"),
            )
            .join(Person, Person.id == EM2.person_id)
            .where(
                EM2.edition_id == edition_id,
                EM2.kind == "debater",
                Person.society_id == edsoc.society_id,
            )
            .order_by(desc(used_subq_all))
        ).all()
        debaters_table = [{"id": mid, "name": name, "times": used or 0} for (mid, name, used) in deb_rows]

        # --- Histórico: rodadas com o debate desta sociedade, posição, rank, e (se permitido) pontos ---
        # 1) Debates da sociedade (rodada, número, debate, posição)
        soc_debates = sess.execute(
            select(
                Round.id.label("round_id"), Round.number, Round.name, Round.scheduled_date,
                Debate.id.label("debate_id"), Debate.number_in_round,
                DebatePosition.position,
                Round.scores_published, Round.silent,
            )
            .join(Debate, Debate.round_id == Round.id)
            .join(DebatePosition, DebatePosition.debate_id == Debate.id)
            .where(
                Round.edition_id == edition_id,
                DebatePosition.edition_society_id == edsoc_id
            )
            .order_by(Round.number.asc(), Debate.number_in_round.asc())
        ).all()

        if not soc_debates:
            return render_template(
                "society_history.html",
                society={"short": short_name, "full": full_name},
                debaters=debaters_table,
                history=[]
            )

        deb_ids = [row.debate_id for row in soc_debates]

        # 2) Totais por posição (somatório das duas falas) para CADA debate
        totals = sess.execute(
            select(
                Speech.debate_id, Speech.position,
                func.sum(Speech.score).label("total")
            )
            .where(
                Speech.debate_id.in_(deb_ids),
                Speech.score.isnot(None)
            )
            .group_by(Speech.debate_id, Speech.position)
        ).all()
        totals_by_debate = {}
        for d_id, pos, tot in totals:
            totals_by_debate.setdefault(d_id, {})[pos] = int(tot)

        # 3) Posições ↔ short_name de TODAS as equipes do debate (para contexto)
        all_pos = sess.execute(
            select(
                DebatePosition.debate_id, DebatePosition.position,
                Society.short_name
            )
            .join(EditionSociety, EditionSociety.id == DebatePosition.edition_society_id)
            .join(Society, Society.id == EditionSociety.society_id)
            .where(DebatePosition.debate_id.in_(deb_ids))
        ).all()
        opp_by_debate = {}
        for d_id, pos, sshort in all_pos:
            opp_by_debate.setdefault(d_id, {})[pos] = sshort

        # 4) Monta estrutura de histórico
        history = []
        for row in soc_debates:
            d_totals = totals_by_debate.get(row.debate_id, {})
            # rank: ordenar desc os totais; se faltar algum total, rank=None
            rank = None
            if len(d_totals) == 4 and all(v is not None for v in d_totals.values()):
                order = sorted(d_totals.items(), key=lambda kv: kv[1], reverse=True)
                rank_map = {pos: i+1 for i, (pos, _t) in enumerate(order)}
                rank = rank_map.get(row.position)

            # pontos por colocação (3/2/1/0)
            points = {1: 3, 2: 2, 3: 1, 4: 0}.get(rank, None)

            history.append({
                "round_id": row.round_id,
                "round_number": row.number,
                "round_name": row.name,
                "round_date": row.scheduled_date,
                "scores_published": bool(row.scores_published),
                "silent": bool(row.silent),
                "debate_id": row.debate_id,
                "deb_number": row.number_in_round,
                "position": row.position,
                "rank": rank,
                "points": points,
                "totals": d_totals,            # só mostrar se scores_published=True
                "teams": opp_by_debate.get(row.debate_id, {}),
            })

        return render_template(
            "society_history.html",
            society={"short": short_name, "full": full_name},
            debaters=debaters_table,
            history=history
        )
    finally:
        sess.close()


# ---------- Página: Escalação (sociedade) ----------
@app.get("/soc/escalacao")
@society_required
def page_escalacao():
    sess = SessionLocal()
    try:
        edsoc, edition_id, base_soc_id = _get_soc_context(sess)
        if not edsoc:
            return redirect(url_for("login"))
        next_round = _next_round_without_results(sess, edition_id)
        if not next_round:
            # não há rodada aberta para escalação
            return render_template("escalacao.html",
                                   next_round=None, debates=[], debaters=[])

        debates = _debates_of_round_for_soc(sess, next_round["id"], edsoc.id)
        debaters = _eligible_debaters_for_next_round(sess, edition_id, base_soc_id, next_round["number"])

        return render_template("escalacao.html",
                               next_round=next_round,
                               debates=debates,
                               debaters=debaters)
    finally:
        sess.close()

@app.post("/soc/escalacao")
@society_required
def post_escalacao():
    # ... (validações iguais às anteriores)
    sess = SessionLocal()
    try:
        edsoc, edition_id, base_soc_id = _get_soc_context(sess)
        # ... (checks iguais)
        # upsert dos dois slots (score=None) iguais

        sess.commit()
        flash("Escalação salva com sucesso.", "success")
        return redirect(url_for("home"))   # <— volta pra principal
    finally:
        sess.close()


@app.get("/pairings")
def view_pairings():
    sess = SessionLocal()
    try:
        edition = get_current_edition(sess)
        if not edition:
            return render_template("pairings.html", rounds=[])

        # Rodadas sem nenhum resultado (nenhuma Speech em nenhum debate da rodada)
        rows = sess.execute(
            select(
                Round.id, Round.number, Round.name, Round.scheduled_date,
                func.count(Speech.id).label("sp_total"),
                func.count(distinct(Debate.id)).label("deb_count"),
            )
            .join(Debate, Debate.round_id == Round.id, isouter=True)
            .join(Speech, Speech.debate_id == Debate.id, isouter=True)
            .where(Round.edition_id == edition.id)
            .group_by(Round.id)
            .having(func.count(Speech.id) == 0)  # nenhuma Speech na rodada
            .order_by(Round.number.asc())
        ).all()
        round_ids = [r_id for (r_id, *_rest) in rows]

        # Posições de todos os debates dessas rodadas
        pos_rows = []
        if round_ids:
            pos_rows = sess.execute(
                select(
                    Round.id.label("round_id"),
                    Round.number.label("round_number"),
                    Round.scheduled_date,
                    Debate.id.label("debate_id"),
                    Debate.number_in_round,
                    DebatePosition.position,
                    Society.short_name,
                )
                .join(Debate, Debate.round_id == Round.id)
                .join(DebatePosition, DebatePosition.debate_id == Debate.id)
                .join(EditionSociety, EditionSociety.id == DebatePosition.edition_society_id)
                .join(Society, Society.id == EditionSociety.society_id)
                .where(Round.id.in_(round_ids))
                .order_by(Round.number.asc(), Debate.number_in_round.asc(), ORDER_POS.asc())
            ).all()

        # Agrupa para o template
        by_round = {}
        for (r_id, r_num, r_date, d_id, d_num, pos, short) in pos_rows:
            by_round.setdefault(r_id, {
                "id": r_id,
                "number": r_num,
                "date": r_date,  # pode ser None
                "debates": {}
            })
            rd = by_round[r_id]
            rd["debates"].setdefault(d_id, {"id": d_id, "number": d_num, "positions": []})
            rd["debates"][d_id]["positions"].append({"position": pos, "short_name": short})

        rounds = []
        # rows garante ordem por Round.number
        for (r_id, r_num, _r_name, r_date, _sp_total, _deb_count) in rows:
            rd = by_round.get(r_id, {"id": r_id, "number": r_num, "date": r_date, "debates": {}})
            debates = [rd["debates"][k] for k in sorted(rd["debates"], key=lambda x: rd["debates"][x]["number"])]
            for d in debates:
                d["positions"].sort(key=lambda x: {"OG":1,"OO":2,"CG":3,"CO":4}.get(x["position"], 99))
            rounds.append({"id": r_id, "number": r_num, "date": r_date, "debates": debates})

        return render_template("pairings.html", rounds=rounds)
    finally:
        sess.close()


@app.get("/resultados")
def view_results_list():
    sess = SessionLocal()
    try:
        edition = get_current_edition(sess)
        if not edition:
            return render_template("results_list.html", rounds=[])

        # Rodadas completas (todas com 8 speeches por debate), não-silent
        # total_speeches == 8 * num_debates   e   num_debates > 0
        r_rows = sess.execute(
            select(
                Round.id, Round.number, Round.name, Round.scheduled_date,
                func.count(distinct(Debate.id)).label("deb_count"),
                func.count(Speech.id).label("sp_total"),
            )
            .join(Debate, Debate.round_id == Round.id, isouter=True)
            .join(Speech, Speech.debate_id == Debate.id, isouter=True)
            .where(Round.edition_id == edition.id, Round.silent == False)
            .group_by(Round.id)
            .having(func.count(distinct(Debate.id)) > 0)
            .having(func.count(Speech.id) == 8 * func.count(distinct(Debate.id)))
            .order_by(Round.number.asc())
        ).all()
        round_ids = [r_id for (r_id, *_rest) in r_rows]

        if not round_ids:
            return render_template("results_list.html", rounds=[])

        # Posições (sociedades) por debate
        debates_sq = (
            select(Debate.id)
            .where(Debate.round_id.in_(round_ids))
            .subquery()
        )

        # CASE para ordenar por posição nas falas
        ORDER_POS_SPEECH = case(
            (Speech.position == "OG", 1),
            (Speech.position == "OO", 2),
            (Speech.position == "CG", 3),
            (Speech.position == "CO", 4),
            else_=99,
        )

        # Posições (sociedades) por debate  — OK como estava
        pos = sess.execute(
            select(
                Debate.id.label("debate_id"),
                Debate.number_in_round,
                Debate.round_id,
                DebatePosition.position,
                Society.short_name,
            )
            .join(DebatePosition, DebatePosition.debate_id == Debate.id)
            .join(EditionSociety, EditionSociety.id == DebatePosition.edition_society_id)
            .join(Society, Society.id == EditionSociety.society_id)
            .where(Debate.round_id.in_(round_ids))
            .order_by(Debate.round_id.asc(), Debate.number_in_round.asc(), ORDER_POS.asc())
        ).all()

        # Speeches (pessoas + notas) — usa subquery em .in_ e ORDER_POS_SPEECH
        sp = sess.execute(
            select(
                Speech.debate_id, Speech.position, Speech.sequence_in_team,
                Speech.score,
                Person.full_name
            )
            .join(EditionMember, EditionMember.id == Speech.edition_member_id)
            .join(Person, Person.id == EditionMember.person_id)
            .where(Speech.debate_id.in_(select(debates_sq.c.id)))
            .order_by(Speech.debate_id.asc(), ORDER_POS_SPEECH.asc(), Speech.sequence_in_team.asc())
        ).all()

        # Juízes por debate — idem: subquery em .in_
        jgs = sess.execute(
            select(
                DebateJudge.debate_id, DebateJudge.role,
                Person.full_name, Society.short_name
            )
            .join(EditionMember, EditionMember.id == DebateJudge.edition_member_id)
            .join(Person, Person.id == EditionMember.person_id)
            .outerjoin(Society, Society.id == Person.society_id)
            .where(DebateJudge.debate_id.in_(select(debates_sq.c.id)))
            .order_by(DebateJudge.debate_id.asc(), DebateJudge.role.asc(), Person.full_name.asc())
        ).all()

        # Monta estrutura → rounds -> debates -> {positions, speeches, judges}
        by_round = {r_id: {"id": r_id, "number": r_num, "date": r_date, "debates": {}}
                    for (r_id, r_num, _rname, r_date, _dc, _st) in r_rows}

        # posições
        for (deb_id, dnum, rid, posi, short) in pos:
            rd = by_round[rid]["debates"].setdefault(deb_id, {
                "id": deb_id, "number": dnum,
                "positions": [], "speeches": {}, "judges": {"chair": None, "wings": []}
            })
            rd["positions"].append({"position": posi, "short_name": short})

        # speeches
        for (deb_id, posi, seq, score, pname) in sp:
            # acha round id do debate (map rápido)
            # para eficiência, poderíamos mapear debates->round, mas o dataset é pequeno
            target_round_id = None
            for rid, rdata in by_round.items():
                if deb_id in rdata["debates"]:
                    target_round_id = rid
                    break
            if target_round_id is None:
                continue
            rddeb = by_round[target_round_id]["debates"][deb_id]
            rddeb["speeches"].setdefault(posi, [])
            rddeb["speeches"][posi].append({"name": pname, "score": int(score), "seq": int(seq)})

        # judges
        for (deb_id, role, pname, sshort) in jgs:
            target_round_id = None
            for rid, rdata in by_round.items():
                if deb_id in rdata["debates"]:
                    target_round_id = rid
                    break
            if target_round_id is None:
                continue
            rddeb = by_round[target_round_id]["debates"][deb_id]
            if role == "chair":
                rddeb["judges"]["chair"] = f"{(sshort or '').strip()} — {pname}"
            else:
                rddeb["judges"]["wings"].append(f"{(sshort or '').strip()} — {pname}")

        # ordena e normaliza listas
        # ordena e normaliza listas + calcula ranks
        result_rounds = []
        for rid, rdata in sorted(by_round.items(), key=lambda kv: kv[1]["number"]):
            debates = [d for _, d in sorted(rdata["debates"].items(), key=lambda kv: kv[1]["number"])]
            for d in debates:
                # garantir lista de posições sempre OG, OO, CG, CO
                d["positions"].sort(key=lambda x: {"OG": 1, "OO": 2, "CG": 3, "CO": 4}.get(x["position"], 99))
                # ordenar speeches por seq
                for posk in list(d["speeches"].keys()):
                    d["speeches"][posk].sort(key=lambda it: it["seq"])

                # ---- calcular total por posição e rank 1..4 (sem empates por regra de input) ----
                totals = []
                for posk in ["OG", "OO", "CG", "CO"]:
                    sp = d["speeches"].get(posk, [])
                    total = sum(s["score"] for s in sp) if len(sp) == 2 else None
                    totals.append((posk, total))
                # ordenar por total desc; se algum None, empurra para o fim (não deve ocorrer aqui)
                ordered = sorted(totals, key=lambda t: (-t[1] if t[1] is not None else 10 ** 9))
                rank_by_pos = {}
                for idx, (posk, _tot) in enumerate(ordered, start=1):
                    rank_by_pos[posk] = idx
                d["rank_by_pos"] = rank_by_pos
                d["total_by_pos"] = {posk: tot for (posk, tot) in totals}

            result_rounds.append({
                "id": rid, "number": rdata["number"], "date": rdata["date"], "debates": debates
            })

        return render_template("results_list.html", rounds=result_rounds)

    finally:
        sess.close()


class LoginUser(UserMixin):
    """ Wrapper para integrar User do banco com Flask-Login """
    def __init__(self, db_user: User):
        self.id = str(db_user.id)
        self.email = db_user.email
        self.role = db_user.role
        self.is_active_flag = db_user.is_active

    @property
    def is_active(self):  # Flask-Login integration
        return True if self.is_active_flag else False

@login_manager.user_loader
def load_user(user_id):
    sess = SessionLocal()
    try:
        u = sess.get(User, int(user_id))
        return LoginUser(u) if u else None
    finally:
        sess.close()

def roles_required(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            # precisa estar logado como staff
            if session.get('auth_kind') != 'staff':
                flash("Faça login como staff.", "error")
                return redirect(url_for('login', next=request.path))
            # precisa ter um dos papéis exigidos
            user_role = session.get('role')
            if roles and user_role not in roles:
                return render_template("403.html"), 403
            return fn(*args, **kwargs)
        return wrapped
    return decorator

def get_current_edition(sess):
    return sess.execute(
        select(Edition).order_by(Edition.year.desc()).limit(1)
    ).scalar_one_or_none()


def get_db():  # se já usa SessionLocal(), ignore
    return SessionLocal()

def current_staff(sessdb):
    uid = session.get("user_id")
    if not uid or session.get("auth_kind") != "staff":
        return None
    return sessdb.get(User, uid)

def current_society(sessdb):
    sid = session.get("soc_acc_id")
    if not sid or session.get("auth_kind") != "society":
        return None
    return sessdb.get(SocietyAccount, sid)

# Mantém seu roles_required existente. Adicione:


def login_required_any(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("auth_kind") in ("staff", "society"):
            return f(*args, **kwargs)
        return redirect(url_for("login", next=request.path))
    return wrapper

# ---------- Rotas de login/logout unificadas ----------
@app.get("/login")
def login():
    return render_template("login_unified.html")

@app.post("/login")
def do_login():
    mode = (request.form.get("mode") or "staff").lower()  # 'staff' ou 'society'
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    nxt = request.args.get("next") or url_for("home")

    if not email or not password:
        flash("Informe e-mail e senha.", "error")
        return redirect(url_for("login", next=nxt))

    dbs = SessionLocal()
    try:
        if mode == "staff":
            u = dbs.execute(
                select(User).where(User.email == email, User.is_active == True)
            ).scalar_one_or_none()
            if not u or not check_password_hash(u.password_hash, password):
                flash("E-mail ou senha inválidos.", "error")
                return redirect(url_for("login", next=nxt))
            session.clear()
            session["auth_kind"] = "staff"
            session["user_id"] = u.id
            session["role"] = u.role
            return redirect(nxt)

        # mode == "society"
        acc = dbs.execute(
            select(SocietyAccount).where(SocietyAccount.email == email, SocietyAccount.is_active == True)
        ).scalar_one_or_none()
        if not acc or not check_password_hash(acc.password_hash, password):
            flash("E-mail ou senha inválidos.", "error")
            return redirect(url_for("login", next=nxt))
        session.clear()
        session["auth_kind"] = "society"
        session["soc_acc_id"] = acc.id
        session["edition_society_id"] = acc.edition_society_id
        return redirect(nxt)
    finally:
        dbs.close()

@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def home():
    # página inicial (templates/index.html)
    return render_template("index.html")


# --- Página Inserir Resultados (somente diretor/admin) ---
@app.get("/results")
@roles_required("director", "admin")
def results_form():
    sess = SessionLocal()
    try:
        edition = get_current_edition(sess)
        if not edition:
            flash("Nenhuma edição encontrada.", "error")
            return redirect(url_for("home"))

        # todas as rodadas da edição
        rounds = sess.execute(
            select(Round.id, Round.number, Round.name)
            .where(Round.edition_id == edition.id)
            .order_by(Round.number.asc())
        ).all()
        round_ids = [r_id for (r_id, _n, _nm) in rounds]

        # conta speeches por debate para saber se está completo (>= 8 speeches)
        deb_speech_counts = sess.execute(
            select(Debate.round_id, Debate.id, func.count(Speech.id))
            .join(Speech, Speech.debate_id == Debate.id, isouter=True)
            .where(Debate.round_id.in_(round_ids))
            .group_by(Debate.id)
        ).all()

        # round_id -> [True/False por debate]
        from collections import defaultdict
        round_done = defaultdict(list)
        for r_id, d_id, cnt in deb_speech_counts:
            round_done[r_id].append((cnt or 0) >= 8)

        # rounds com flag "completed"
        rounds_with_status = []
        for (r_id, r_num, r_name) in rounds:
            flags = round_done.get(r_id, [])
            completed = bool(flags) and all(flags)
            rounds_with_status.append((r_id, r_num, r_name, completed))

        # debates da 1ª rodada para popular inicial (completude também)
        first_round_id = rounds_with_status[0][0] if rounds_with_status else None
        debates = []
        if first_round_id:
            debates = sess.execute(
                select(
                    Debate.id,
                    Debate.number_in_round,
                    func.count(Speech.id).label("sp_count"),
                )
                .join(Speech, Speech.debate_id == Debate.id, isouter=True)
                .where(Debate.round_id == first_round_id)
                .group_by(Debate.id)
                .order_by(Debate.number_in_round.asc())
            ).all()
            # vira lista de dicts: {id, number_in_round, completed}
            debates = [
                {"id": d_id, "number_in_round": n, "completed": (spc or 0) >= 8}
                for (d_id, n, spc) in debates
            ]

        return render_template("results.html", rounds=rounds_with_status, debates=debates)
    finally:
        sess.close()

# --- APIs auxiliares para o form ---

@app.get("/api/round_debates")
@roles_required("director", "admin")
def api_round_debates():
    round_id = int(request.args.get("round_id"))
    sess = SessionLocal()
    try:
        rows = sess.execute(
            select(
                Debate.id,
                Debate.number_in_round,
                func.count(Speech.id).label("sp_count"),
            )
            .join(Speech, Speech.debate_id == Debate.id, isouter=True)
            .where(Debate.round_id == round_id)
            .group_by(Debate.id)
            .order_by(Debate.number_in_round.asc())
        ).all()
        data = [
            {"id": d_id, "number_in_round": n, "completed": (spc or 0) >= 8}
            for (d_id, n, spc) in rows
        ]
        return jsonify(data=data)
    finally:
        sess.close()


@app.get("/api/debate_detail")
@roles_required("director", "admin")
def api_debate_detail():
    """Retorna posições (short_name) e listas elegíveis (debater/judge) para o form.
       Juízes da(s) mesma(s) sociedade(s) do debate são excluídos.
    """
    debate_id = int(request.args.get("debate_id"))
    sess = SessionLocal()
    try:
        # posições -> short_name e edition_society_id
        order_case = case(
            (DebatePosition.position == "OG", 1),
            (DebatePosition.position == "OO", 2),
            (DebatePosition.position == "CG", 3),
            (DebatePosition.position == "CO", 4),
            else_=99,
        )

        pos_rows = sess.execute(
            select(
                DebatePosition.position,
                Society.short_name,
                EditionSociety.id
            )
            .join(EditionSociety, EditionSociety.id == DebatePosition.edition_society_id)
            .join(Society, Society.id == EditionSociety.society_id)
            .where(DebatePosition.debate_id == debate_id)
            .order_by(order_case.asc())
        ).all()
        team_shorts = { (short or "").strip() for (_pos, short, _esid) in pos_rows }

        # edição do debate
        edition_id = sess.execute(
            select(Round.edition_id)
            .join(Debate, Debate.round_id == Round.id)
            .where(Debate.id == debate_id)
        ).scalar_one()

        # debaters (todos da edição)
        deb_rows = sess.execute(
            select(EditionMember.id, Person.full_name, Society.short_name)
            .join(Person, Person.id == EditionMember.person_id)
            .join(Edition, Edition.id == EditionMember.edition_id)
            .outerjoin(Society, Society.id == Person.society_id)
            .where(EditionMember.edition_id == edition_id, EditionMember.kind == "debater")
            .order_by(Society.short_name.asc(), Person.full_name.asc())
        ).all()

        # judges (exclui quem tem society no debate)
        judge_rows = sess.execute(
            select(EditionMember.id, Person.full_name, Society.short_name)
            .join(Person, Person.id == EditionMember.person_id)
            .join(Edition, Edition.id == EditionMember.edition_id)
            .outerjoin(Society, Society.id == Person.society_id)
            .where(EditionMember.edition_id == edition_id, EditionMember.kind == "judge")
            .order_by(Society.short_name.asc(), Person.full_name.asc())
        ).all()
        judge_rows = [r for r in judge_rows if (r[2] or "").strip() not in team_shorts]

        data = {
            "positions": [
                {"position": pos, "team_short": (short or ""), "edition_society_id": esid}
                for (pos, short, esid) in pos_rows
            ],
            "debaters": [
                {"edition_member_id": mid, "name": name, "soc": (short or "")}
                for (mid, name, short) in deb_rows
            ],
            "judges": [
                {"edition_member_id": mid, "name": name, "soc": (short or "")}
                for (mid, name, short) in judge_rows
            ],
        }
        return jsonify(data=data)
    finally:
        sess.close()

@app.post("/api/results")
@roles_required("director", "admin")
def api_save_results():
    payload = request.get_json(force=True, silent=False)
    debate_id = int(payload.get("debate_id"))
    speeches = payload.get("speeches") or []
    judges = payload.get("judges") or {}

    sess = SessionLocal()
    try:
        # validar debate -> edição
        round_row = sess.execute(
            select(Round.id, Round.edition_id)
            .join(Debate, Debate.round_id == Round.id)
            .where(Debate.id == debate_id)
        ).one_or_none()
        if not round_row:
            return jsonify(error="Debate inexistente"), 400
        _, edition_id = round_row

        # helper correto: monta o statement e só então executa
        def valid_em(em_id, kind=None):
            if not em_id:
                return False
            stmt = (
                select(EditionMember.id)
                .where(
                    EditionMember.id == int(em_id),
                    EditionMember.edition_id == edition_id,
                )
            )
            if kind:
                stmt = stmt.where(EditionMember.kind == kind)
            return sess.execute(stmt).scalar_one_or_none() is not None

        # valida & upsert speeches
        valid_positions = {"OG", "OO", "CG", "CO"}
        for item in speeches:
            pos = (item.get("position") or "").upper()
            s1_id = item.get("s1_id")
            s1_score = item.get("s1_score")
            s2_id = item.get("s2_id")
            s2_score = item.get("s2_score")

            if pos not in valid_positions:
                return jsonify(error=f"Posição inválida: {pos}"), 400

            if not (valid_em(s1_id, "debater") and valid_em(s2_id, "debater")):
                return jsonify(error=f"Debaters inválidos para {pos}"), 400

            # scores inteiros 50–100
            def vs(x):
                try:
                    xi = int(x)
                    return 50 <= xi <= 100
                except Exception:
                    return False

            if not (vs(s1_score) and vs(s2_score)):
                return jsonify(error=f"Scores inválidos (50–100) em {pos}"), 400

            # upsert Speech (seq 1 e 2)
            sp1 = sess.execute(
                select(Speech).where(
                    Speech.debate_id == debate_id,
                    Speech.position == pos,
                    Speech.sequence_in_team == 1,
                )
            ).scalar_one_or_none()
            if not sp1:
                sess.add(
                    Speech(
                        debate_id=debate_id,
                        position=pos,
                        sequence_in_team=1,
                        edition_member_id=int(s1_id),
                        score=int(s1_score),
                    )
                )
            else:
                sp1.edition_member_id = int(s1_id)
                sp1.score = int(s1_score)

            sp2 = sess.execute(
                select(Speech).where(
                    Speech.debate_id == debate_id,
                    Speech.position == pos,
                    Speech.sequence_in_team == 2,
                )
            ).scalar_one_or_none()
            if not sp2:
                sess.add(
                    Speech(
                        debate_id=debate_id,
                        position=pos,
                        sequence_in_team=2,
                        edition_member_id=int(s2_id),
                        score=int(s2_score),
                    )
                )
            else:
                sp2.edition_member_id = int(s2_id)
                sp2.score = int(s2_score)

        # juízes: chair + até 2 wings (todos distintos entre si)
        chair_id = judges.get("chair")
        wings = [w for w in (judges.get("wings") or []) if w]

        if chair_id and not valid_em(chair_id, "judge"):
            return jsonify(error="Chair inválido"), 400
        for w in wings:
            if not valid_em(w, "judge"):
                return jsonify(error="Wing inválido"), 400
        # não permitir duplicados (chair igual a wing, ou wings repetidos)
        all_judges = [int(chair_id)] + [int(w) for w in wings] if chair_id else [int(w) for w in wings]
        if len(all_judges) != len(set(all_judges)):
            return jsonify(error="Juízes duplicados não são permitidos"), 400
        if len(wings) > 2:
            return jsonify(error="No máximo 2 wings"), 400

        from models import DebateJudge  # evitar import circular no topo

        # zera chair atual e insere o novo (se houver)
        sess.execute(
            DebateJudge.__table__.delete().where(
                DebateJudge.debate_id == debate_id, DebateJudge.role == "chair"
            )
        )
        if chair_id:
            sess.add(
                DebateJudge(
                    debate_id=debate_id, edition_member_id=int(chair_id), role="chair"
                )
            )

        # zera wings e recria
        sess.execute(
            DebateJudge.__table__.delete().where(
                DebateJudge.debate_id == debate_id, DebateJudge.role == "wing"
            )
        )
        for w in wings:
            sess.add(
                DebateJudge(debate_id=debate_id, edition_member_id=int(w), role="wing")
            )

        sess.commit()
        return jsonify(ok=True)
    except Exception as e:
        sess.rollback()
        return jsonify(error=str(e)), 500
    finally:
        sess.close()



@app.get("/api/standings")
def api_standings():
    """
    Classificação da edição vigente (ou ?edition=YYYY).

    Ordenação: pontos desc, speaker_points desc, firsts desc, seconds desc.
    Payload (só short_name):
      [{ society_id, short_name, points, speaker_points, firsts, seconds, debates }]
    """
    sess = SessionLocal()
    try:
        edition_param = request.args.get("edition", "current")
        if edition_param == "current":
            edition = get_current_edition(sess)
        else:
            edition = sess.execute(
                select(Edition).where(Edition.year == int(edition_param))
            ).scalar_one_or_none()

        if not edition:
            return jsonify(data=[])

        # Sociedades inscritas na edição
        esocs = sess.execute(
            select(EditionSociety.id, Society.id, Society.short_name)
            .join(Society, Society.id == EditionSociety.society_id)
            .where(EditionSociety.edition_id == edition.id)
        ).all()

        # agregador
        agg = {}
        for es_id, s_id, s_short in esocs:
            short = (s_short or "").strip()
            if short == "Independente":
                continue
            agg[es_id] = {
                "edsoc_id": es_id,
                "society_id": s_id,
                "short_name": short,
                "points": 0,
                "speaker_points": 0,
                "firsts": 0,
                "seconds": 0,
                "debates": 0,
            }

        # Debates da edição
        debates = sess.execute(
            select(Debate.id, Debate.number_in_round)
            .join(Round, Round.id == Debate.round_id)
            .where(
                Round.edition_id == edition.id,
                Round.silent.is_(False)
            )
        ).all()
        debate_ids = [debate_id for debate_id, _ in debates]
        if not debate_ids:
            data = sorted(
                agg.values(),
                key=lambda x: (-x["points"], -x["speaker_points"], -x["firsts"], -x["seconds"], x["short_name"])
            )
            return jsonify(data=data)

        # Posições por debate
        pos_rows = sess.execute(
            select(DebatePosition.debate_id, DebatePosition.position, DebatePosition.edition_society_id)
            .where(DebatePosition.debate_id.in_(debate_ids))
        ).all()
        from collections import defaultdict
        debate_positions = defaultdict(dict)
        for d_id, pos, es_id in pos_rows:
            debate_positions[d_id][pos] = es_id

        # Speeches
        speech_rows = sess.execute(
            select(Speech.debate_id, Speech.position, Speech.sequence_in_team, Speech.score)
            .where(Speech.debate_id.in_(debate_ids))
        ).all()
        debate_team_scores = defaultdict(lambda: {"OG": [], "OO": [], "CG": [], "CO": []})
        for d_id, pos, _seq, score in speech_rows:
            if score is not None:
                debate_team_scores[d_id][pos].append(int(score))

        # Consolidação por debate completo (8 notas)
        for d_id in debate_ids:
            teams = debate_positions.get(d_id, {})
            if set(teams.keys()) != {"OG", "OO", "CG", "CO"}:
                continue
            sums = {}
            complete = True
            for pos, es_id in teams.items():
                scores = debate_team_scores[d_id][pos]
                if len(scores) != 2:
                    complete = False
                    break
                sums[es_id] = sum(scores)
            if not complete:
                continue

            # speaker points + contagem de debates
            for es_id, total in sums.items():
                agg[es_id]["speaker_points"] += total
                agg[es_id]["debates"] += 1

            # ranking no debate: 3/2/1/0
            ordered = sorted(sums.items(), key=lambda kv: kv[1], reverse=True)
            for rank, (es_id, _tot) in enumerate(ordered):
                if rank == 0:
                    agg[es_id]["points"] += 3
                    agg[es_id]["firsts"] += 1
                elif rank == 1:
                    agg[es_id]["points"] += 2
                    agg[es_id]["seconds"] += 1
                elif rank == 2:
                    agg[es_id]["points"] += 1
                # rank 3 → +0

        data = sorted(
            agg.values(),
            key=lambda x: (-x["points"], -x["speaker_points"], -x["firsts"], -x["seconds"], x["short_name"])
        )
        return jsonify(data=data)
    finally:
        sess.close()

@app.get("/api/next_pairings")
def api_next_pairings():
    """
    Pareamentos (OG/OO/CG/CO) da próxima rodada sem resultados.
    Retorna apenas short_name nas posições.
    """
    sess = SessionLocal()
    try:
        edition_param = request.args.get("edition", "current")
        if edition_param == "current":
            edition = get_current_edition(sess)
        else:
            edition = sess.execute(
                select(Edition).where(Edition.year == int(edition_param))
            ).scalar_one_or_none()

        if not edition:
            return jsonify(data=[])

        rounds = sess.execute(
            select(Round.id, Round.number)
            .where(Round.edition_id == edition.id)
            .order_by(Round.number.asc())
        ).all()
        if not rounds:
            return jsonify(data=[])

        # acha a primeira rodada sem notas
        next_round_id = None
        next_round_number = None
        for r_id, r_num in rounds:
            debate_ids = sess.execute(
                select(Debate.id).where(Debate.round_id == r_id)
            ).scalars().all()
            if not debate_ids:
                next_round_id, next_round_number = r_id, r_num
                break
            any_score = sess.execute(
                select(func.count(Speech.id))
                .where(Speech.debate_id.in_(debate_ids), Speech.score.isnot(None))
            ).scalar_one()
            if any_score == 0:
                next_round_id, next_round_number = r_id, r_num
                break

        if not next_round_id:
            return jsonify(data=[])

        debates = sess.execute(
            select(Debate.id, Debate.number_in_round)
            .where(Debate.round_id == next_round_id)
            .order_by(Debate.number_in_round.asc())
        ).all()
        debate_ids = [debate_id for debate_id, _ in debates]

        # posições com short_name
        pos_rows = sess.execute(
            select(
                DebatePosition.debate_id,
                DebatePosition.position,
                Society.short_name
            )
            .join(EditionSociety, EditionSociety.id == DebatePosition.edition_society_id)
            .join(Society, Society.id == EditionSociety.society_id)
            .where(DebatePosition.debate_id.in_(debate_ids))
        ).all()

        from collections import defaultdict
        by_debate = defaultdict(dict)  # debate_id -> { OG/OO/CG/CO: short_name }
        for d_id, pos, short in pos_rows:
            by_debate[d_id][pos] = (short or f"D{d_id}")

        data = []
        for d_id, d_num in debates:
            positions = by_debate.get(d_id, {})
            data.append({
                "round_number": next_round_number,
                "debate_number": d_num,
                "positions": {
                    "OG": positions.get("OG", ""),
                    "OO": positions.get("OO", ""),
                    "CG": positions.get("CG", ""),
                    "CO": positions.get("CO", ""),
                }
            })
        return jsonify(data=data)
    finally:
        sess.close()


if __name__ == "__main__":
    app.run(debug=True)
