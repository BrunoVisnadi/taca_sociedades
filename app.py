import os

from flask import Flask, jsonify, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import select, func, case, distinct
from db import SessionLocal
from models import (
    Edition, EditionSociety, Society,
    Round, Debate, DebatePosition, Speech,
    EditionMember, Person, User, DebateJudge  # User vem do seu models.py
)

app = Flask(__name__, static_folder="static", static_url_path="/static")
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
    """ Wrapper para integrar seu User do banco com Flask-Login """
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
    """Decorator para exigir um ou mais papéis (ex.: 'director', 'admin')."""
    def wrapper(fn):
        @login_required
        def inner(*args, **kwargs):
            if getattr(current_user, "role", None) not in roles:
                # 403 simples
                return render_template("403.html"), 403
            return fn(*args, **kwargs)
        # preserve attrs
        inner.__name__ = fn.__name__
        return inner
    return wrapper

def get_current_edition(sess):
    return sess.execute(
        select(Edition).order_by(Edition.year.desc()).limit(1)
    ).scalar_one_or_none()

# --- Rotas de Login/Logout ---
@app.get("/login")
def login():
    return render_template("login.html")

@app.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    sess = SessionLocal()
    try:
        user = sess.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if not user or not check_password_hash(user.password_hash, password) or not user.is_active:
            flash("Credenciais inválidas.", "error")
            return redirect(url_for("login"))
        login_user(LoginUser(user))
        return redirect(url_for("home"))
    finally:
        sess.close()

@app.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("home"))

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
            if not short:
                # fallback mínimo (evita string vazia)
                short = f"S{s_id}"
            agg[es_id] = {
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
