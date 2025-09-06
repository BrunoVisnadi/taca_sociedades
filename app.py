import os
from pathlib import Path
from functools import wraps

from flask import Flask, jsonify, render_template, request, redirect, url_for, flash, session, abort
from flask_login import LoginManager, UserMixin
from werkzeug.security import check_password_hash
from sqlalchemy import cast, literal, case, distinct, desc, exists, select, func, case, and_
from sqlalchemy.orm import aliased
from sqlalchemy.dialects.postgresql import aggregate_order_by
from db import SessionLocal
from models import (
    Edition, EditionSociety, Society,
    Round, Debate, DebatePosition, Speech,
    EditionMember, Person, User, DebateJudge,
    SocietyAccount, MemberKindEnum, JudgeRoleEnum
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
    SESSION_COOKIE_SECURE=True  # se seu domínio usa HTTPS (Render usa)
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
    # subconsulta correlacionada: existe algum speech com score na rodada?
    any_scored = (
        select(literal(1))
        .select_from(Speech)
        .join(Debate, Debate.id == Speech.debate_id)
        .where(
            Debate.round_id == Round.id,     # correlaciona com Round externo
            Speech.score.is_not(None),
        )
        .limit(1)
    )

    row = sess.execute(
        select(
            Round.id,
            Round.number,
            Round.name,
            Round.scheduled_date,
        )
        .where(
            Round.edition_id == edition_id,
            ~exists(any_scored)              # NÃO existe score lançado
        )
        .order_by(Round.number.asc(), Round.id.asc())
        .limit(1)
    ).first()

    if not row:
        return None
    r_id, r_num, r_name, r_date = row
    return {"id": r_id, "number": r_num, "name": r_name, "date": r_date}

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


def _debates_of_round_for_soc(sess, round_id: int, edition_society_id: int):
    rows = sess.execute(
        select(
            Debate.id.label("debate_id"),
            Debate.number_in_round.label("number_in_round"),
            DebatePosition.position.label("position"),
            # locked = existe algum score não-nulo nesse slot (posição do nosso time)
            func.bool_or(Speech.score.is_not(None)).label("locked"),
            # lineup ordenado por seq: [{id, name, seq, score}, ...]
            func.json_agg(
                aggregate_order_by(
                    func.json_build_object(
                        literal("id"), EditionMember.id,
                        literal("name"), Person.full_name,
                        literal("seq"), Speech.sequence_in_team,
                        literal("score"), Speech.score,
                    ),
                    Speech.sequence_in_team.asc(),
                )
            ).label("lineup_json"),
        )
        .select_from(DebatePosition)
        .join(Debate, Debate.id == DebatePosition.debate_id)
        .outerjoin(
            Speech,
            (Speech.debate_id == Debate.id) &
            (Speech.position == DebatePosition.position)
        )
        .outerjoin(EditionMember, EditionMember.id == Speech.edition_member_id)
        .outerjoin(Person, Person.id == EditionMember.person_id)
        .where(
            Debate.round_id == round_id,
            DebatePosition.edition_society_id == edition_society_id,
        )
        .group_by(Debate.id, Debate.number_in_round, DebatePosition.position)
        .order_by(Debate.number_in_round.asc(), DebatePosition.position.asc())
    ).all()

    out = []
    for d_id, num, pos, locked, lineup_json in rows:
        s1 = s2 = None
        if lineup_json:
            # lineup_json já vem ordenado por seq
            if len(lineup_json) >= 1:
                s1 = {"id": lineup_json[0]["id"], "name": lineup_json[0]["name"]}
            if len(lineup_json) >= 2:
                s2 = {"id": lineup_json[1]["id"], "name": lineup_json[1]["name"]}
        out.append({
            "debate_id": d_id,
            "number_in_round": num,
            "position": pos,
            "s1": s1,
            "s2": s2,
            "locked": bool(locked),
        })
    return out
# lista de debatedores ELEGÍVEIS (< 4 usos em rodadas anteriores) para a próxima rodada
def _eligible_debaters_for_next_round(sess, edition_id: int, base_society_id: int, next_round_number: int):
    EM = aliased(EditionMember)

    used_count = func.count(Speech.id).filter(
        (Speech.score.is_not(None))
        & (Round.edition_id == edition_id)
        & (Round.number < next_round_number)
    ).label("used_count")

    rows = sess.execute(
        select(
            EM.id,
            Person.full_name,
            used_count,
        )
        .select_from(EM)
        .join(Person, Person.id == EM.person_id)
        # JOINs para contar usos anteriores; LEFT para permitir 0
        .outerjoin(Speech, Speech.edition_member_id == EM.id)
        .outerjoin(Debate, Debate.id == Speech.debate_id)
        .outerjoin(Round, Round.id == Debate.round_id)
        .where(
            EM.edition_id == edition_id,
            EM.kind == cast(literal("debater"), MemberKindEnum),   # enum OK
            Person.society_id == base_society_id,
        )
        .group_by(EM.id, Person.full_name)
        .order_by(Person.full_name.asc())
    ).all()

    return [{"id": mid, "name": name} for (mid, name, used) in rows if int(used or 0) < 4]


@app.get("/health-check")
def health_check():
    return "ok"

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


@app.get("/sociedade/<int:edsoc_id>")
def view_society_history(edsoc_id: int):
    sess = SessionLocal()
    EM2 = aliased(EditionMember)
    try:
        # 0) EditionSociety + Society nomes (uma ida só)
        edsoc_row = sess.execute(
            select(
                EditionSociety.id,
                EditionSociety.edition_id,
                EditionSociety.society_id,
                Society.short_name,
                Society.name
            ).join(Society, Society.id == EditionSociety.society_id
            ).where(EditionSociety.id == edsoc_id)
        ).first()

        if not edsoc_row:
            abort(404)

        _, edition_id, society_id, short_name, full_name = edsoc_row

        # 1) Debatedores da sociedade na edição + contagem de usos (scores não-nulos)
        #    LEFT JOIN em Speech/Debate/Round permite "0" usos naturalmente
        used_count = func.count(Speech.id).filter(Speech.score.is_not(None)).label("used_count")

        deb_rows = sess.execute(
            select(
                EM2.id.label("member_id"),
                Person.full_name,
                used_count,
            )
            .join(Person, Person.id == EM2.person_id)
            .outerjoin(Speech, Speech.edition_member_id == EM2.id)
            .outerjoin(Debate, Debate.id == Speech.debate_id)
            .outerjoin(Round, Round.id == Debate.round_id)
            .where(
                EM2.edition_id == edition_id,
                EM2.kind == cast(literal("debater"), MemberKindEnum),
                Person.society_id == society_id,
                # conta apenas falas desta edição; permite None para quem não falou
                (Round.edition_id == edition_id) | (Round.id.is_(None)),
            )
            .group_by(EM2.id, Person.full_name)
            .order_by(desc(used_count))
        ).all()

        debaters_table = [
            {"id": mid, "name": name, "times": int(used or 0)}
            for (mid, name, used) in deb_rows
        ]

        # 2) HISTÓRICO em uma query com CTEs (debates, rank, mapas e speakers)
        # our_debates: debates da sociedade na edição corrente
        our_debates = (
            select(
                Round.id.label("round_id"),
                Round.number.label("round_number"),
                Round.name.label("round_name"),
                Round.scheduled_date.label("round_date"),
                Round.scores_published.label("scores_published"),
                Round.silent.label("silent"),
                Debate.id.label("debate_id"),
                Debate.number_in_round.label("debate_number"),
                DebatePosition.position.label("our_position"),
            )
            .join(Debate, Debate.round_id == Round.id)
            .join(DebatePosition, DebatePosition.debate_id == Debate.id)
            .where(
                Round.edition_id == edition_id,
                DebatePosition.edition_society_id == edsoc_id,
            )
            .cte("our_debates")
        )

        # team_scores: soma por (debate, equipe) quando houver 2 discursos com nota
        # mapeia equipe via DebatePosition (por posição), garantindo edição/silent já filtrados no join de Round
        team_scores = (
            select(
                Speech.debate_id.label("debate_id"),
                DebatePosition.edition_society_id.label("es_id"),
                func.sum(Speech.score).label("team_total"),
                func.count(Speech.id).label("speech_count"),
            )
            .join(Debate, Debate.id == Speech.debate_id)
            .join(Round, Round.id == Debate.round_id)
            .join(
                DebatePosition,
                and_(
                    DebatePosition.debate_id == Speech.debate_id,
                    DebatePosition.position == Speech.position,
                ),
            )
            .where(
                Round.edition_id == edition_id,
                Speech.score.is_not(None),
            )
            .group_by(Speech.debate_id, DebatePosition.edition_society_id)
            .having(func.count(Speech.id) == 2)  # exige 2 discursos com nota
            .cte("team_scores")
        )

        # ranked: rank por debate (3/2/1/0 será mapeado depois)
        ranked = (
            select(
                team_scores.c.debate_id,
                team_scores.c.es_id,
                team_scores.c.team_total,
                func.rank().over(
                    partition_by=team_scores.c.debate_id,
                    order_by=team_scores.c.team_total.desc(),
                ).label("rnk"),
            )
        ).cte("ranked")

        # our_rank: rank/points do nosso time em cada debate (LEFT JOIN para casos sem 2 falas)
        points_expr = case(
            (ranked.c.rnk == 1, literal(3)),
            (ranked.c.rnk == 2, literal(2)),
            (ranked.c.rnk == 3, literal(1)),
            else_=literal(0),
        )

        our_rank = (
            select(
                ranked.c.debate_id,
                ranked.c.rnk,
                points_expr.label("points"),
                ranked.c.team_total.label("our_total"),
            )
            .where(ranked.c.es_id == edsoc_id)
        ).cte("our_rank")

        # teams_map: por debate, position->short_name (todas as equipes do debate)
        teams_map = (
            select(
                DebatePosition.debate_id.label("debate_id"),
                func.jsonb_object_agg(
                    DebatePosition.position,
                    Society.short_name
                ).label("teams_json")
            )
            .select_from(DebatePosition)  # <-- define o FROM
            .join(EditionSociety, EditionSociety.id == DebatePosition.edition_society_id)
            .join(Society, Society.id == EditionSociety.society_id)
            .group_by(DebatePosition.debate_id)
        ).cte("teams_map")

        # totals_map: por debate, position->total (somente quando time tem 2 falas)
        totals_map = (
            select(
                DebatePosition.debate_id.label("debate_id"),
                func.jsonb_object_agg(
                    DebatePosition.position,
                    team_scores.c.team_total
                ).label("totals_json")
            )
            .select_from(DebatePosition)  # <-- define o FROM
            .join(
                team_scores,
                DebatePosition.edition_society_id == team_scores.c.es_id
            )
            .group_by(DebatePosition.debate_id)
        ).cte("totals_map")

        # our_speakers: nomes + score, ordenados por sequence_in_team (sempre retornamos nomes; score pode ser NULL)
        our_speakers = (
            select(
                Speech.debate_id.label("debate_id"),
                func.json_agg(
                    aggregate_order_by(
                        func.json_build_object(
                            literal("name"), Person.full_name,
                            literal("score"), Speech.score,
                        ),
                        Speech.sequence_in_team.asc(),  # <— ordena dentro do json_agg
                    )
                ).label("speakers_json")
            )
            .select_from(Speech)  # define FROM explícito p/ evitar ambiguidade no JOIN
            .join(EditionMember, EditionMember.id == Speech.edition_member_id)
            .join(Person, Person.id == EditionMember.person_id)
            .join(
                DebatePosition,
                and_(
                    DebatePosition.debate_id == Speech.debate_id,
                    DebatePosition.position == Speech.position,
                )
            )
            .where(DebatePosition.edition_society_id == edsoc_id)
            .group_by(Speech.debate_id)
        ).cte("our_speakers")

        # SELECT final: uma linha por debate nosso, com mapas JSON prontos
        history_rows = sess.execute(
            select(
                our_debates.c.round_id,
                our_debates.c.round_number,
                our_debates.c.round_name,
                our_debates.c.round_date,
                our_debates.c.scores_published,
                our_debates.c.silent,
                our_debates.c.debate_id,
                our_debates.c.debate_number,
                our_debates.c.our_position,

                our_rank.c.rnk,
                our_rank.c.points,
                our_rank.c.our_total,

                teams_map.c.teams_json,
                totals_map.c.totals_json,
                our_speakers.c.speakers_json,
            )
            .join(our_rank, our_rank.c.debate_id == our_debates.c.debate_id, isouter=True)
            .join(teams_map, teams_map.c.debate_id == our_debates.c.debate_id, isouter=True)
            .join(totals_map, totals_map.c.debate_id == our_debates.c.debate_id, isouter=True)
            .join(our_speakers, our_speakers.c.debate_id == our_debates.c.debate_id, isouter=True)
            .order_by(our_debates.c.round_number.asc(), our_debates.c.debate_number.asc())
        ).all()

        # Montagem final (aplica regra de exibição de totals somente quando published)
        history = []
        for (round_id, rnum, rname, rdate, published, silent,
             debate_id, dnum, our_pos, rnk, pts, our_total,
             teams_json, totals_json, speakers_json) in history_rows:

            # se não publicados, não exibimos totals (mas mantemos structure vazia)
            totals_map_py = totals_json if published else None

            history.append({
                "round_id": round_id,
                "round_number": rnum,
                "round_name": rname,
                "round_date": rdate,
                "scores_published": bool(published),
                "silent": bool(silent),
                "debate_id": debate_id,
                "deb_number": dnum,
                "position": our_pos,
                "rank": int(rnk) if rnk is not None else None,
                "points": int(pts) if pts is not None else None,
                "totals": totals_map_py,           # dict position->total, ou None se não publicado
                "teams": teams_json or {},         # dict position->short_name
                "our_speakers": speakers_json or []  # [{name, score}], score pode ser NULL
            })

        return render_template(
            "society_history.html",
            society={"short": short_name, "full": full_name},
            debaters=debaters_table,
            history=history
        )

    finally:
        sess.close()



@app.post("/soc/escalacao")
@society_required
def post_escalacao():
    """Salva/atualiza escalação (2 debatedores) para o debate/posição da sociedade.
       Regras:
       - debate/posição devem pertencer à sociedade logada;
       - debatedores devem ser EditionMember(kind='debater') da MESMA sociedade/edição;
       - não pode escolher a mesma pessoa pros dois slots;
       - se já houver score (resultado lançado) naquele debate/posição, BLOQUEIA edição.
    """
    data = request.form or request.json or {}
    debate_id = int(data.get("debate_id", 0))
    s1_id = int(data.get("s1_id", 0))
    s2_id = int(data.get("s2_id", 0))

    if not debate_id or not s1_id or not s2_id:
        flash("Preencha os dois debatedores.", "error")
        return redirect(request.referrer or url_for("page_escalacao"))

    if s1_id == s2_id:
        flash("Os dois debatedores devem ser pessoas diferentes.", "error")
        return redirect(request.referrer or url_for("page_escalacao"))

    sess = SessionLocal()
    try:
        edsoc, edition_id, base_soc_id = _get_soc_context(sess)
        if not edsoc:
            return redirect(url_for("login"))

        # Verifica que o debate pertence à mesma edição e contém a sociedade (descobre a posição)
        pos_row = sess.execute(
            select(DebatePosition.position, Debate.round_id)
            .join(Debate, Debate.id == DebatePosition.debate_id)
            .where(
                DebatePosition.debate_id == debate_id,
                DebatePosition.edition_society_id == edsoc.id
            )
        ).first()
        if not pos_row:
            flash("Você não possui permissão para este debate.", "error")
            return redirect(request.referrer or url_for("page_escalacao"))
        position, round_id = pos_row

        # Debatedores válidos? (da mesma sociedade base e edição, e kind='debater')
        def _valid_deb(member_id: int) -> bool:
            row = sess.execute(
                select(EditionMember.id)
                .join(Person, Person.id == EditionMember.person_id)
                .where(
                    EditionMember.id == member_id,
                    EditionMember.edition_id == edition_id,
                    EditionMember.kind == "debater",
                    Person.society_id == base_soc_id
                )
            ).scalar_one_or_none()
            return bool(row)

        if not (_valid_deb(s1_id) and _valid_deb(s2_id)):
            flash("Debatedor inválido para esta sociedade/edição.", "error")
            return redirect(request.referrer or url_for("page_escalacao"))

        # Verifica se já há resultado (score != NULL) -> bloqueia
        scored = sess.execute(
            select(func.count(Speech.id))
            .where(
                Speech.debate_id == debate_id,
                Speech.position == position,
                Speech.score.isnot(None)
            )
        ).scalar_one()
        if scored and scored > 0:
            flash("Edição bloqueada: este debate já possui resultado lançado.", "error")
            return redirect(request.referrer or url_for("page_escalacao"))

        # Upsert da escalação (cria/atualiza os dois slots com score=NULL)
        slots = {
            1: s1_id,
            2: s2_id,
        }
        existing = sess.execute(
            select(Speech.id, Speech.sequence_in_team)
            .where(Speech.debate_id == debate_id, Speech.position == position)
        ).all()
        existing_by_seq = {seq: sid for (sid, seq) in existing}

        for seq, member_id in slots.items():
            if seq in existing_by_seq:
                # update
                sp = sess.get(Speech, existing_by_seq[seq])
                sp.edition_member_id = member_id
                sp.score = None
            else:
                # insert
                sess.add(Speech(
                    debate_id=debate_id,
                    position=position,
                    sequence_in_team=seq,
                    edition_member_id=member_id,
                    score=None
                ))
        sess.commit()
        flash("Escalação salva com sucesso.", "success")
        # redireciona mantendo a rodada atual
        return redirect(url_for("home"))
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

        # 1) Rodadas completas, não-silent (1 query)
        r_rows = sess.execute(
            select(
                Round.id,
                Round.number,
                Round.name,
                Round.scheduled_date,
                Round.scores_published,
                func.count(distinct(Debate.id)).label("deb_count"),
                func.count(Speech.id).label("sp_total"),
            )
            .join(Debate, Debate.round_id == Round.id, isouter=True)
            .join(Speech, Speech.debate_id == Debate.id, isouter=True)
            .where(Round.edition_id == edition.id, Round.silent.is_(False))
            .group_by(Round.id)
            .having(func.count(distinct(Debate.id)) > 0)
            .having(func.count(Speech.id) == 8 * func.count(distinct(Debate.id)))
            .order_by(Round.number.asc())
        ).all()

        round_ids = [r_id for (r_id, *_rest) in r_rows]
        if not round_ids:
            return render_template("results_list.html", rounds=[])

        # Preparar CASE p/ ordenar posições
        ORDER_POS = case(
            (DebatePosition.position == "OG", 1),
            (DebatePosition.position == "OO", 2),
            (DebatePosition.position == "CG", 3),
            (DebatePosition.position == "CO", 4),
            else_=99,
        )
        ORDER_POS_SPEECH = case(
            (Speech.position == "OG", 1),
            (Speech.position == "OO", 2),
            (Speech.position == "CG", 3),
            (Speech.position == "CO", 4),
            else_=99,
        )

        # Subquery: posições (sociedade por posição) agregadas por debate
        positions_subq = (
            select(
                DebatePosition.debate_id.label("debate_id"),
                func.json_agg(
                    aggregate_order_by(
                        func.json_build_object(
                            literal("position"), DebatePosition.position,
                            literal("short_name"), Society.short_name,
                        ),
                        ORDER_POS.asc(),
                    )
                ).label("positions_json"),
            )
            .select_from(DebatePosition)
            .join(EditionSociety, EditionSociety.id == DebatePosition.edition_society_id)
            .join(Society, Society.id == EditionSociety.society_id)
            .group_by(DebatePosition.debate_id)
        ).subquery()

        # Subquery: speeches agregados por debate, ordenados por posição e seq
        speeches_subq = (
            select(
                Speech.debate_id.label("debate_id"),
                func.json_agg(
                    aggregate_order_by(
                        func.json_build_object(
                            literal("position"), Speech.position,
                            literal("seq"), Speech.sequence_in_team,
                            literal("name"), Person.full_name,
                            literal("score"), Speech.score,
                        ),
                        ORDER_POS_SPEECH.asc(),
                        Speech.sequence_in_team.asc(),
                    )
                ).label("speeches_json"),
            )
            .select_from(Speech)
            .join(EditionMember, EditionMember.id == Speech.edition_member_id)
            .join(Person, Person.id == EditionMember.person_id)
            .group_by(Speech.debate_id)
        ).subquery()

        # Subquery: juízes agregados por debate (chair + wings)
        chair_role = cast(literal("chair"), JudgeRoleEnum)
        wing_role = cast(literal("wing"), JudgeRoleEnum)
        judge_string = func.concat(
            func.trim(func.coalesce(Society.short_name, literal(""))),
            literal(" — "),
            Person.full_name,
        )

        judges_subq = (
            select(
                DebateJudge.debate_id.label("debate_id"),
                # um chair por debate (string_agg com filtro; se houver mais de 1, concatena)
                func.string_agg(judge_string, literal(", ")).filter(DebateJudge.role == chair_role).label("chair"),
                # wings como array ordenada por nome
                func.array_agg(
                    aggregate_order_by(judge_string, Person.full_name.asc())
                ).filter(DebateJudge.role == wing_role).label("wings"),
            )
            .select_from(DebateJudge)
            .join(EditionMember, EditionMember.id == DebateJudge.edition_member_id)
            .join(Person, Person.id == EditionMember.person_id)
            .outerjoin(Society, Society.id == Person.society_id)
            .group_by(DebateJudge.debate_id)
        ).subquery()

        # Subquery: totals por posição (somente quando há 2 falas com nota)
        team_totals_subq = (
            select(
                DebatePosition.debate_id.label("debate_id"),
                DebatePosition.position.label("position"),
                func.sum(Speech.score).label("total"),
            )
            .select_from(Speech)
            .join(Debate, Debate.id == Speech.debate_id)
            .join(
                DebatePosition,
                (DebatePosition.debate_id == Speech.debate_id)
                & (DebatePosition.position == Speech.position),
            )
            .where(Speech.score.is_not(None))
            .group_by(DebatePosition.debate_id, DebatePosition.position)
            .having(func.count(Speech.id) == 2)  # exige 2 falas com nota por posição
        ).subquery()

        # 2ª etapa: agrega em JSON por debate
        totals_subq = (
            select(
                team_totals_subq.c.debate_id.label("debate_id"),
                func.jsonb_object_agg(
                    team_totals_subq.c.position,
                    team_totals_subq.c.total,
                ).label("totals_json"),
            )
            .group_by(team_totals_subq.c.debate_id)
        ).subquery()

        # 2) Debates prontos por rodada, com todas as agregações (1 query)
        debates_rows = sess.execute(
            select(
                Debate.round_id,
                Debate.id.label("debate_id"),
                Debate.number_in_round.label("debate_number"),
                positions_subq.c.positions_json,
                speeches_subq.c.speeches_json,
                judges_subq.c.chair,
                judges_subq.c.wings,
                totals_subq.c.totals_json,
            )
            .select_from(Debate)
            .join(positions_subq, positions_subq.c.debate_id == Debate.id, isouter=True)
            .join(speeches_subq, speeches_subq.c.debate_id == Debate.id, isouter=True)
            .join(judges_subq, judges_subq.c.debate_id == Debate.id, isouter=True)
            .join(totals_subq, totals_subq.c.debate_id == Debate.id, isouter=True)
            .where(Debate.round_id.in_(round_ids))
            .order_by(Debate.round_id.asc(), Debate.number_in_round.asc())
        ).all()

        # 3) Montagem final em memória (linear, sem next()/buscas aninhadas)
        by_round = {
            r_id: {
                "id": r_id,
                "number": r_num,
                "date": r_date,
                "scores_published": bool(scores_pub),
                "debates": [],
            }
            for (r_id, r_num, _rname, r_date, scores_pub, _dc, _st) in r_rows
        }

        for (rid, deb_id, dnum, positions_json, speeches_json, chair, wings, totals_json) in debates_rows:
            # reconstruir estruturas simples p/ a view
            positions = sorted(
                [
                    {"position": obj["position"], "short_name": obj["short_name"]}
                    for obj in (positions_json or [])
                ],
                key=lambda x: {"OG": 1, "OO": 2, "CG": 3, "CO": 4}.get(x["position"], 99),
            )

            # speeches: agrupar por posição mantendo ordem seq
            speeches_by_pos = {"OG": [], "OO": [], "CG": [], "CO": []}
            for obj in (speeches_json or []):
                speeches_by_pos.setdefault(obj["position"], []).append(
                    {"name": obj["name"], "score": obj["score"], "seq": int(obj["seq"])}
                )
            for posk in list(speeches_by_pos.keys()):
                speeches_by_pos[posk].sort(key=lambda it: it["seq"])

            # calcular totals/ranks (independente de published — a view decide exibir)
            totals_map = {}
            if totals_json:
                # totals_json vem {position: total} apenas quando há 2 falas com nota
                totals_map = {k: int(v) if v is not None else None for k, v in totals_json.items()}
            else:
                # manter None quando não há 2 falas válidas
                totals_map = {
                    posk: (sum((s["score"] or 0) for s in speeches_by_pos[posk]) if len(speeches_by_pos[posk]) == 2 else None)
                    for posk in ["OG", "OO", "CG", "CO"]
                }

            order_for_rank = sorted(
                [(posk, totals_map.get(posk)) for posk in ["OG", "OO", "CG", "CO"]],
                key=lambda t: (-t[1] if t[1] is not None else 10**9),
            )
            rank_by_pos = {posk: (idx + 1) for idx, (posk, _tot) in enumerate(order_for_rank)}

            by_round[rid]["debates"].append({
                "id": deb_id,
                "number": dnum,
                "positions": positions,
                "speeches": speeches_by_pos,
                "judges": {"chair": chair, "wings": wings or []},
                "rank_by_pos": rank_by_pos,
                "total_by_pos": totals_map,
            })

        # ordenar debates dentro de cada round
        result_rounds = []
        for rid, rdata in sorted(by_round.items(), key=lambda kv: kv[1]["number"]):
            rdata["debates"].sort(key=lambda d: d["number"])
            result_rounds.append({
                "id": rid,
                "number": rdata["number"],
                "date": rdata["date"],
                "scores_published": rdata["scores_published"],
                "debates": rdata["debates"],
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

        #return render_template("results.html", rounds=rounds_with_status, debates=debates)
        return render_template("results.html", rounds=rounds_with_status, debates=[])
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
@roles_required("director", "admin")  # mantenha protegido
def api_debate_detail():
    debate_id = request.args.get("debate_id", type=int)
    if not debate_id:
        return jsonify({"error": "debate_id inválido"}), 400

    sess = SessionLocal()
    try:
        # ---------------------------------------------
        # Query 1: meta + posições + lineup (1 ida só)
        # ---------------------------------------------
        # lineup por posição neste debate
        lineup_subq = (
            select(
                Speech.position.label("position"),
                func.array_agg(
                    aggregate_order_by(Speech.edition_member_id, Speech.sequence_in_team.asc())
                ).label("lineup"),
            )
            .select_from(Speech)
            .where(Speech.debate_id == debate_id)
            .group_by(Speech.position)
            .subquery()
        )

        pos_rows = sess.execute(
            select(
                Round.edition_id,                             # para queries seguintes
                DebatePosition.position,
                DebatePosition.edition_society_id,
                EditionSociety.society_id,                    # para filtrar juízes de fora
                Society.short_name,
                lineup_subq.c.lineup,                         # [edition_member_id, ...] ordenado
            )
            .select_from(DebatePosition)
            .join(Debate, Debate.id == DebatePosition.debate_id)
            .join(Round, Round.id == Debate.round_id)
            .join(EditionSociety, EditionSociety.id == DebatePosition.edition_society_id)
            .join(Society, Society.id == EditionSociety.society_id)
            .join(lineup_subq, lineup_subq.c.position == DebatePosition.position, isouter=True)
            .where(DebatePosition.debate_id == debate_id)
            .order_by(ORDER_POS.asc())
        ).all()
        print(pos_rows)
        if not pos_rows:
            return jsonify({"error": "Debate não encontrado"}), 404

        # extrai edition_id / societies do debate e monta payload "positions"
        edition_id = pos_rows[0][0]
        positions = []
        edsoc_ids = []
        team_soc_ids = set()

        for (_edition_id, position, edsoc_id, base_soc_id, short_name, lineup) in pos_rows:
            positions.append({
                "position": position,
                "team_short": short_name,
                "edition_society_id": edsoc_id,
                "lineup": list(lineup or []),  # já ordenado por sequence_in_team
            })
            edsoc_ids.append(edsoc_id)
            team_soc_ids.add(base_soc_id)

        # ---------------------------------------------
        # Query 2: debatedores elegíveis (4 sociedades)
        # ---------------------------------------------
        EM = aliased(EditionMember)
        deb_rows = sess.execute(
            select(
                EM.id,
                Person.full_name,
                Society.short_name,
            )
            .select_from(EM)
            .join(Person, Person.id == EM.person_id)
            .join(Society, Society.id == Person.society_id)
            .where(
                EM.edition_id == edition_id,
                EM.kind == cast(literal("debater"), MemberKindEnum),  # <-- cast para o enum
                Person.society_id.in_(team_soc_ids),
            )
            .order_by(Society.short_name.asc(), Person.full_name.asc())
        ).all()

        debaters = [
            {"edition_member_id": mid, "name": name, "soc": short}
            for (mid, name, short) in deb_rows
        ]

        # ---------------------------------------------
        # Query 3: juízes elegíveis (fora das 4 sociedades)
        # ---------------------------------------------
        J = aliased(EditionMember)
        judge_rows = sess.execute(
            select(
                J.id,
                Person.full_name,
                Society.short_name,
            )
            .select_from(J)
            .join(Person, Person.id == J.person_id)
            .join(Society, Society.id == Person.society_id)   # mantém a mesma semântica do seu código
            .where(
                J.edition_id == edition_id,
                J.kind == cast(literal("judge"), MemberKindEnum),     # <-- cast para o enum
                ~Person.society_id.in_(team_soc_ids),          # exclui as 4 sociedades
            )
            .order_by(Society.short_name.asc(), Person.full_name.asc())
        ).all()

        judges = [
            {"edition_member_id": mid, "name": name, "soc": short}
            for (mid, name, short) in judge_rows
        ]

        return jsonify({"data": {
            "positions": positions,
            "debaters": debaters,
            "judges": judges
        }})

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
def api_standings(debug=False):
    sess = SessionLocal()
    try:
        # --- edição alvo ---
        if debug:
            edition_param = 2025
        else:
            edition_param = request.args.get("edition", "current")

        if edition_param == "current":
            edition = get_current_edition(sess)
        else:
            edition = sess.execute(
                select(Edition).where(Edition.year == int(edition_param))
            ).scalar_one_or_none()

        if not edition:
            return jsonify(data=[])

        # ------------------------------------------------------------
        # 1) Totais por (debate, posição/equipe) com contagem de falas
        #    (Speech -> Debate -> Round) + DebatePosition para mapear ES
        # ------------------------------------------------------------
        team_scores_per_team_sq = (
            select(
                Speech.debate_id.label("debate_id"),
                Speech.position.label("position"),
                DebatePosition.edition_society_id.label("es_id"),
                func.sum(Speech.score).label("team_total"),
                func.count(Speech.id).label("speech_count"),
                Round.scores_published.label("scores_published"),
            )
            .join(Debate, Debate.id == Speech.debate_id)
            .join(Round, Round.id == Debate.round_id)
            .join(
                DebatePosition,
                and_(
                    DebatePosition.debate_id == Speech.debate_id,
                    DebatePosition.position == Speech.position,
                ),
            )
            .where(
                Round.edition_id == edition.id,
                Round.silent.is_(False),
                Speech.score.is_not(None),
            )
            .group_by(
                Speech.debate_id,
                Speech.position,
                DebatePosition.edition_society_id,
                Round.scores_published,
            )
            .subquery()
        )

        # ------------------------------------------------------------
        # 2) Debates completos = 4 linhas com speech_count=2 (OG/OO/CG/CO)
        # ------------------------------------------------------------
        complete_debates_sq = (
            select(team_scores_per_team_sq.c.debate_id)
            .where(team_scores_per_team_sq.c.speech_count == 2)
            .group_by(team_scores_per_team_sq.c.debate_id)
            .having(func.count() == 4)
            .subquery()
        )

        # ------------------------------------------------------------
        # 3) Subconsulta com rank() por debate (apenas debates completos)
        #    IMPORTANTE: a janela fica AQUI (subquery), não no nível agregado
        # ------------------------------------------------------------
        ranked_sq = (
            select(
                team_scores_per_team_sq.c.debate_id,
                team_scores_per_team_sq.c.es_id,
                team_scores_per_team_sq.c.team_total,
                team_scores_per_team_sq.c.scores_published,
                func.rank().over(
                    partition_by=team_scores_per_team_sq.c.debate_id,
                    order_by=team_scores_per_team_sq.c.team_total.desc(),
                ).label("rnk"),
            )
            .join(
                complete_debates_sq,
                complete_debates_sq.c.debate_id == team_scores_per_team_sq.c.debate_id,
            )
            .where(team_scores_per_team_sq.c.speech_count == 2)
            .subquery()
        )

        # mapeamento 3-2-1-0 + contagens
        points_expr = case(
            (ranked_sq.c.rnk == 1, literal(3)),
            (ranked_sq.c.rnk == 2, literal(2)),
            (ranked_sq.c.rnk == 3, literal(1)),
            else_=literal(0),
        )
        firsts_expr = case((ranked_sq.c.rnk == 1, literal(1)), else_=literal(0))
        seconds_expr = case((ranked_sq.c.rnk == 2, literal(1)), else_=literal(0))
        sp_expr = case(
            (ranked_sq.c.scores_published.is_(True), ranked_sq.c.team_total),
            else_=literal(0),
        )

        # ------------------------------------------------------------
        # 4) Agregado final por equipe (EditionSociety)
        #    (agora SIM podemos somar, pois o rank ficou na subquery)
        # ------------------------------------------------------------
        standings_sq = (
            select(
                ranked_sq.c.es_id.label("es_id"),
                func.sum(points_expr).label("points"),
                func.sum(sp_expr).label("speaker_points"),
                func.sum(firsts_expr).label("firsts"),
                func.sum(seconds_expr).label("seconds"),
                func.count().label("debates"),
            )
            .group_by(ranked_sq.c.es_id)
            .subquery()
        )

        # ------------------------------------------------------------
        # 5) Base (todas as sociedades inscritas != "Independente")
        #    + LEFT JOIN com o agregado e ordenação final no banco
        # ------------------------------------------------------------
        base_q = (
            select(
                EditionSociety.id.label("es_id"),
                Society.id.label("society_id"),
                Society.short_name.label("short_name"),
            )
            .join(Society, Society.id == EditionSociety.society_id)
            .where(
                EditionSociety.edition_id == edition.id,
                func.trim(func.coalesce(Society.short_name, "")) != "Independente",
            )
            .subquery()
        )

        final_q = (
            select(
                base_q.c.es_id.label("edsoc_id"),  # <— novo
                base_q.c.society_id,
                base_q.c.short_name,
                func.coalesce(standings_sq.c.points, literal(0)).label("points"),
                func.coalesce(standings_sq.c.speaker_points, literal(0)).label("speaker_points"),
                func.coalesce(standings_sq.c.firsts, literal(0)).label("firsts"),
                func.coalesce(standings_sq.c.seconds, literal(0)).label("seconds"),
                func.coalesce(standings_sq.c.debates, literal(0)).label("debates"),
            )
            .join(standings_sq, standings_sq.c.es_id == base_q.c.es_id, isouter=True)
            .order_by(
                func.coalesce(standings_sq.c.points, literal(0)).desc(),
                func.coalesce(standings_sq.c.speaker_points, literal(0)).desc(),
                func.coalesce(standings_sq.c.firsts, literal(0)).desc(),
                func.coalesce(standings_sq.c.seconds, literal(0)).desc(),
                base_q.c.short_name.asc(),
            )
        )

        rows = sess.execute(final_q).all()

        data = [
            dict(
                edsoc_id=es_id,  # <— novo campo no payload
                society_id=sid,
                short_name=(sn or "").strip(),
                points=int(p),
                speaker_points=int(sp),
                firsts=int(f),
                seconds=int(s2),
                debates=int(db),
            )
            for es_id, sid, sn, p, sp, f, s2, db in rows
        ]
        return jsonify(data=data) if not debug else data
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
        # --- edição alvo ---
        edition_param = request.args.get("edition", "current")
        if edition_param == "current":
            edition = get_current_edition(sess)
        else:
            edition = sess.execute(
                select(Edition).where(Edition.year == int(edition_param))
            ).scalar_one_or_none()

        if not edition:
            return jsonify(data=[])

        # ------------------------------------------------------------
        # 1) Encontrar a primeira rodada SEM resultados
        #    Critério: NÃO existe Speech com score != NULL em debates da rodada
        # ------------------------------------------------------------
        any_scored_subq = (
            select(literal(1))   # <---- aqui é literal(1), não func.literal
            .select_from(Debate)
            .join(Speech, Speech.debate_id == Debate.id)
            .where(
                Debate.round_id == Round.id,   # correlacionada com Round externo
                Speech.score.is_not(None),
            )
            .limit(1)
        )

        next_round_row = sess.execute(
            select(Round.id, Round.number)
            .where(
                Round.edition_id == edition.id,
                ~exists(any_scored_subq)       # NOT EXISTS
            )
            .order_by(Round.number.asc())
            .limit(1)
        ).first()

        if not next_round_row:
            return jsonify(data=[])

        next_round_id, next_round_number = next_round_row

        # ------------------------------------------------------------
        # 2) Buscar TODOS os pareamentos da rodada encontrada
        # ------------------------------------------------------------
        rows = sess.execute(
            select(
                Debate.id.label("debate_id"),
                Debate.number_in_round.label("debate_number"),
                DebatePosition.position,
                Society.short_name,
            )
            .join(DebatePosition, DebatePosition.debate_id == Debate.id)
            .join(EditionSociety, EditionSociety.id == DebatePosition.edition_society_id)
            .join(Society, Society.id == EditionSociety.society_id)
            .where(Debate.round_id == next_round_id)
            .order_by(Debate.number_in_round.asc())
        ).all()

        from collections import defaultdict
        by_debate = defaultdict(lambda: {"OG": "", "OO": "", "CG": "", "CO": ""})
        debate_numbers = {}
        for d_id, d_num, pos, short in rows:
            debate_numbers[d_id] = d_num
            by_debate[d_id][pos] = (short or f"D{d_id}")

        debates_sorted = sorted(debate_numbers.items(), key=lambda kv: kv[1])
        data = [
            {
                "round_number": next_round_number,
                "debate_number": d_num,
                "positions": by_debate[d_id],
            }
            for d_id, d_num in debates_sorted
        ]

        return jsonify(data=data)
    finally:
        sess.close()

@app.get("/admin")
@roles_required("director", "admin")
def admin_panel():
    sess = SessionLocal()
    try:
        edition = get_current_edition(sess)
        if not edition:
            return render_template("admin_panel.html", rounds=[])
        rows = sess.execute(
            select(Round.id, Round.number, Round.name, Round.scheduled_date,
                   Round.scores_published, Round.silent)
            .where(Round.edition_id == edition.id)
            .order_by(Round.number.asc())
        ).all()
        rounds = [
            {
                "id": r_id,
                "number": r_num,
                "name": r_name,
                "date": r_date,
                "scores_published": bool(scores),
                "silent": bool(sil)
            }
            for (r_id, r_num, r_name, r_date, scores, sil) in rows
        ]
        return render_template("admin_panel.html", rounds=rounds)
    finally:
        sess.close()


@app.post("/api/rounds/<int:round_id>/settings")
@roles_required("director", "admin")
def api_update_round_settings(round_id: int):
    """
    Body JSON (qualquer um dos dois campos; ambos opcionais):
    { "scores_published": true|false, "silent": true|false }
    """
    payload = request.get_json(silent=True) or {}
    sess = SessionLocal()
    try:
        rnd = sess.get(Round, round_id)
        if not rnd:
            return jsonify({"error": "Rodada não encontrada"}), 404

        changed = False
        if "scores_published" in payload:
            val = bool(payload["scores_published"])
            if rnd.scores_published != val:
                rnd.scores_published = val
                changed = True

        if "silent" in payload:
            val = bool(payload["silent"])
            if rnd.silent != val:
                rnd.silent = val
                changed = True

        if changed:
            sess.commit()

        return jsonify({
            "ok": True,
            "data": {
                "id": rnd.id,
                "scores_published": rnd.scores_published,
                "silent": rnd.silent
            }
        })
    finally:
        sess.close()


if __name__ == "__main__":
    app.run(debug=True)
